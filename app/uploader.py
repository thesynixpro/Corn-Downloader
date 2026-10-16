"""Telegram delivery: Bot API uploads, split delivery, and MTProto uploads.

Delivery strategy:
1. If the file fits the cloud Bot API limit (~50 MB) or a local Bot API server
   is configured → single ``send_video`` call.
2. Otherwise, if MTProto credentials are configured → upload as one video via
   the bot's own MTProto session (up to ~2 GB) with live progress.
3. Otherwise → split the file into <= ``SPLIT_PART_MB`` parts and send them as
   consecutive videos.

Tokens, session files and MTProto credentials are never logged.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Awaitable, Callable

from .config import CLOUD_UPLOAD_LIMIT, Settings
from .formatting import format_bytes
from .media import SplitError, split_video
from .progress import Progress

logger = logging.getLogger("uvd_bot.uploader")

StatusCallback = Callable[[str], Awaitable[None]]


class UploadError(Exception):
    """Raised when all delivery strategies fail."""


class Uploader:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._mtproto_client = None
        self._mtproto_lock = asyncio.Lock()

    async def close(self) -> None:
        client = self._mtproto_client
        self._mtproto_client = None
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception:
            pass

    async def _get_mtproto(self):
        if self._mtproto_client is not None:
            try:
                if self._mtproto_client.is_connected():
                    return self._mtproto_client
            except Exception:
                pass
        if not self.settings.mtproto_enabled:
            return None
        async with self._mtproto_lock:
            if self._mtproto_client is not None:
                try:
                    if self._mtproto_client.is_connected():
                        return self._mtproto_client
                except Exception:
                    pass
            try:
                from telethon import TelegramClient
            except ImportError:
                logger.warning("telethon not installed; MTProto direct upload disabled.")
                return None
            try:
                session_path = str(self.settings.download_dir / "mtproto_bot_session")
                client = TelegramClient(session_path, self.settings.api_id, self.settings.api_hash)
                await client.start(bot_token=self.settings.bot_token)
                self._mtproto_client = client
                logger.info("MTProto connected (direct upload up to ~2 GB available).")
                return client
            except Exception as exc:
                logger.warning("MTProto connect failed: %s: %s", type(exc).__name__, str(exc)[:200])
                return None

    async def deliver(
        self,
        bot,
        chat_id: int,
        path: str,
        *,
        title: str,
        duration_hms: str,
        duration_s: float,
        size: int,
        progress: Progress,
        status_cb: StatusCallback,
        upload_mode: str = "stream",
    ) -> tuple[str, int]:
        """Deliver ``path`` to ``chat_id``. Returns ``(mode, part_count)``."""
        if upload_mode not in ("stream", "file"):
            raise UploadError(f"Unsupported upload preference: {upload_mode}")
        cloud_cap = CLOUD_UPLOAD_LIMIT
        needs_large_route = size > cloud_cap and not self.settings.telegram_api_url

        if needs_large_route and size <= self.settings.max_bytes and self.settings.mtproto_enabled:
            sent = await self._try_mtproto(chat_id, path, title, duration_hms, size, progress, status_cb)
            if sent:
                return "mtproto", 1

        if needs_large_route and size <= self.settings.max_bytes:
            parts = await self._split(path, title, duration_s, status_cb)
            await self._send_parts(bot, chat_id, parts, title, duration_hms, status_cb, upload_mode)
            return "split", len(parts)

        await self._send_single(
            bot, chat_id, path, title, duration_hms, size, duration_s, progress, status_cb, upload_mode
        )
        return "single", 1

    async def _try_mtproto(
        self,
        chat_id: int,
        path: str,
        title: str,
        duration_hms: str,
        size: int,
        progress: Progress,
        status_cb: StatusCallback,
    ) -> bool:
        client = await self._get_mtproto()
        if client is None:
            return False
        await status_cb(f"📤 Uploading large file directly ({format_bytes(size)})…\n🎬 {title[:100]}")
        progress.stage = "uploading"
        progress.downloaded = 0
        progress.total = size

        def on_progress(received: int, total: int) -> None:
            progress.downloaded = int(received or 0)
            progress.total = int(total or size)

        caption = self._caption(title, duration_hms, size)
        try:
            await client.send_file(
                chat_id,
                path,
                caption=caption,
                supports_streaming=True,
                force_document=False,
                progress_callback=on_progress,
            )
        except Exception as exc:
            logger.warning(
                "MTProto upload failed (%s: %s); falling back to split delivery.",
                type(exc).__name__,
                str(exc)[:200],
            )
            progress.stage = "processing"
            return False
        return True

    async def _split(
        self,
        path: str,
        title: str,
        duration_s: float,
        status_cb: StatusCallback,
    ) -> list[str]:
        size = os.path.getsize(path)
        await status_cb(f"✂️ Video is {format_bytes(size)} — splitting into parts for Telegram…")
        try:
            return await asyncio.to_thread(
                split_video,
                path,
                os.path.dirname(path),
                duration_s,
                self.settings.split_part_mb,
                self.settings.split_max_parts,
                CLOUD_UPLOAD_LIMIT,
            )
        except SplitError as exc:
            raise UploadError(str(exc)) from exc

    async def _send_parts(
        self,
        bot,
        chat_id: int,
        parts: list[str],
        title: str,
        duration_hms: str,
        status_cb: StatusCallback,
        upload_mode: str = "stream",
    ) -> None:
        total = len(parts)
        for index, part in enumerate(parts, start=1):
            await status_cb(f"📤 Uploading part {index}/{total}…\n🎬 {title[:100]}")
            caption = self._caption(title, duration_hms, os.path.getsize(part), part=(index, total))
            await self._send_file(bot, chat_id, part, caption, upload_mode=upload_mode)
            if index < total:
                await asyncio.sleep(2)

    async def _send_single(
        self,
        bot,
        chat_id: int,
        path: str,
        title: str,
        duration_hms: str,
        size: int,
        duration_s: float,
        progress: Progress,
        status_cb: StatusCallback,
        upload_mode: str = "stream",
    ) -> None:
        progress.stage = "uploading"
        progress.downloaded = 0
        progress.total = 0
        await status_cb(f"📤 Uploading to Telegram… ({format_bytes(size)})\n🎬 {title[:100]}")
        caption = self._caption(title, duration_hms, size)
        duration_arg = int(duration_s) if duration_s and duration_s > 0 else None
        await self._send_file(bot, chat_id, path, caption, duration=duration_arg, upload_mode=upload_mode)

    async def _send_file(
        self,
        bot,
        chat_id: int,
        path: str,
        caption: str,
        duration: int | None = None,
        upload_mode: str = "stream",
    ) -> None:
        settings = self.settings
        attempts = ("document", "video") if upload_mode == "file" else ("video", "document")
        last_exc: Exception | None = None
        for attempt in attempts:
            try:
                with open(path, "rb") as handle:
                    if attempt == "video":
                        await bot.send_video(
                            chat_id=chat_id,
                            video=handle,
                            caption=caption,
                            duration=duration,
                            supports_streaming=True,
                            read_timeout=settings.upload_timeout,
                            write_timeout=settings.upload_timeout,
                            connect_timeout=settings.connect_timeout,
                        )
                    else:
                        await bot.send_document(
                            chat_id=chat_id,
                            document=handle,
                            caption=caption,
                            read_timeout=settings.upload_timeout,
                            write_timeout=settings.upload_timeout,
                            connect_timeout=settings.connect_timeout,
                        )
                return
            except Exception as exc:
                last_exc = exc
                logger.info("Telegram %s upload failed (%s); trying fallback.", attempt, type(exc).__name__)
        raise UploadError(
            f"Telegram rejected the upload: {type(last_exc).__name__ if last_exc else 'unknown error'}"
        ) from last_exc

    @staticmethod
    def _caption(title: str, duration_hms: str, size: int, part: tuple[int, int] | None = None) -> str:
        text = f"🎬 {(title or 'video')[:700]}"
        meta: list[str] = []
        if duration_hms and duration_hms != "unknown":
            meta.append(f"⏱ {duration_hms}")
        if size:
            meta.append(f"📦 {format_bytes(size)}")
        if part:
            meta.append(f"part {part[0]}/{part[1]}")
        if meta:
            text += "\n" + " | ".join(meta)
        return text[:1024]
