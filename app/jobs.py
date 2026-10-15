"""Job manager and download pipeline.

One active job per user; a global semaphore caps concurrent downloads and
queued jobs report their position. Temporary directories live under
``DOWNLOAD_DIR`` and are removed on completion, failure, and cancellation.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path

from telegram.ext import ContextTypes

from . import downloader, errors, media, platforms
from .config import Settings
from .format_select import best_display_format, has_format_within_limit, pick_format_string
from .formatting import format_bytes, format_duration, render_progress
from .hls import DRMProtectedError
from .net import DownloadTooLargeError, NetworkError
from .progress import Progress
from .uploader import UploadError, Uploader
from .urlguard import URLGuardError, redact_url

logger = logging.getLogger("uvd_bot.jobs")


class TooLargeError(Exception):
    """Raised when a video exceeds configured size or storage limits."""


class JobManager:
    """Tracks per-user tasks, queue positions, temp dirs and statuses."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.uploader = Uploader(settings)
        self.semaphore = asyncio.Semaphore(settings.max_concurrent_downloads)
        self._lock = asyncio.Lock()
        self._tasks: dict[int, asyncio.Task] = {}
        self._tempdirs: dict[int, str] = {}
        self._statuses: dict[int, str] = {}
        self._waiting: dict[int, float] = {}

    async def register(self, user_id: int, task: asyncio.Task, tempdir: str) -> None:
        async with self._lock:
            self._tasks[user_id] = task
            self._tempdirs[user_id] = tempdir
            self._statuses[user_id] = "queued"
        task.add_done_callback(lambda finished: asyncio.create_task(self._forget(user_id, finished)))

    async def _forget(self, user_id: int, task: asyncio.Task) -> None:
        async with self._lock:
            if self._tasks.get(user_id) is task:
                self._tasks.pop(user_id, None)
        with contextlib.suppress(Exception):
            task.exception()

    async def untrack(self, user_id: int) -> None:
        async with self._lock:
            self._tempdirs.pop(user_id, None)

    async def has_active(self, user_id: int) -> bool:
        async with self._lock:
            task = self._tasks.get(user_id)
        return task is not None and not task.done()

    async def set_status(self, user_id: int, status: str) -> None:
        async with self._lock:
            self._statuses[user_id] = status

    async def get_status(self, user_id: int) -> str:
        async with self._lock:
            return self._statuses.get(user_id, "idle (no downloads yet)")

    async def cancel(self, user_id: int) -> bool:
        async with self._lock:
            task = self._tasks.get(user_id)
            tempdir = self._tempdirs.pop(user_id, None)
        if task is None or task.done():
            cleanup_dir(tempdir, self.settings)
            return False
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        cleanup_dir(tempdir, self.settings)
        return True

    async def acquire_slot(self, user_id: int) -> None:
        async with self._lock:
            self._waiting[user_id] = time.monotonic()
        await self.semaphore.acquire()
        async with self._lock:
            self._waiting.pop(user_id, None)

    def release_slot(self) -> None:
        self.semaphore.release()

    def queue_position(self, user_id: int) -> int | None:
        if user_id not in self._waiting:
            return None
        ordered = sorted(self._waiting.items(), key=lambda item: item[1])
        for index, (uid, _) in enumerate(ordered, start=1):
            if uid == user_id:
                return index
        return None

    def disk_usage_bytes(self) -> int:
        total = 0
        root = self.settings.download_dir
        if not root.exists():
            return 0
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                try:
                    total += os.path.getsize(os.path.join(dirpath, name))
                except OSError:
                    continue
        return total

    def cleanup_stale(self, max_age_hours: int = 24) -> int:
        removed = 0
        root = self.settings.download_dir
        if not root.exists():
            return 0
        cutoff = time.time() - max_age_hours * 3600
        for child in root.iterdir():
            if not child.name.startswith("uvd_") or not child.is_dir():
                continue
            try:
                if child.stat().st_mtime < cutoff:
                    shutil.rmtree(child, ignore_errors=True)
                    removed += 1
            except OSError:
                continue
        if removed:
            logger.info("Removed %d stale temp directories.", removed)
        return removed

    async def close(self) -> None:
        async with self._lock:
            tasks = list(self._tasks.items())
            self._tasks.clear()
            tempdirs = list(self._tempdirs.values())
            self._tempdirs.clear()
        for _uid, task in tasks:
            task.cancel()
        for _uid, task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        for tempdir in tempdirs:
            cleanup_dir(tempdir, self.settings)
        await self.uploader.close()


def cleanup_dir(path: str | None, settings: Settings) -> None:
    if not path:
        return
    try:
        candidate = Path(path)
        base = settings.download_dir
        is_inside_base = base in candidate.parents or candidate == base
        is_sys_tmp = str(candidate).startswith(tempfile.gettempdir())
        if not (is_inside_base or is_sys_tmp):
            logger.warning("Refusing to delete path outside temp dirs.")
            return
        shutil.rmtree(path, ignore_errors=True)
    except Exception as exc:
        logger.warning("Cleanup failed for %s: %s", path, type(exc).__name__)


async def safe_edit(message, text: str) -> None:
    try:
        await message.edit_text(text)
    except Exception:
        pass


async def _status_loop(progress: Progress, status_msg, manager: JobManager, user_id: int) -> None:
    last_text = ""
    while True:
        await asyncio.sleep(3)
        text = render_progress(progress, manager.queue_position(user_id))
        if text and text != last_text:
            last_text = text
            await safe_edit(status_msg, text)


def _failure_text(exc: BaseException, settings: Settings) -> str:
    if isinstance(exc, DRMProtectedError):
        return f"❌ {exc}\n\nThe bot does not bypass DRM."
    if isinstance(exc, TooLargeError):
        return f"❌ {exc}"
    if isinstance(exc, DownloadTooLargeError):
        return f"❌ The video exceeds the configured size limit ({settings.max_file_mb} MB)."
    if isinstance(exc, URLGuardError):
        return "❌ This URL is not allowed: the host is blocked, private, or not on the allow-list."
    if isinstance(exc, UploadError):
        return (
            "❌ The download completed, but Telegram could not upload the file."
            f"\n\n🔧 Detail: {errors.short_detail(exc)}"
        )
    if isinstance(exc, AssertionError):
        return (
            "❌ This video's page layout isn't readable right now (the site may have changed). "
            "Please try again later or use another link."
            f"\n\n🔧 Detail: {errors.short_detail(exc)}"
        )
    if isinstance(exc, media.MediaError):
        return f"❌ Video validation failed: {errors.short_detail(exc)}"
    if isinstance(exc, NetworkError):
        return (
            "❌ The download failed due to a network error. Please try again later."
            f"\n\n🔧 Detail: {errors.short_detail(exc)}"
        )
    mapped = errors.map_download_error(exc)
    detail = errors.short_detail(exc)
    text = f"❌ {mapped}"
    if detail and detail.lower() not in mapped.lower():
        text += f"\n\n🔧 Detail: {detail}"
    return text


async def run_download_job(
    url: str,
    update,
    context: ContextTypes.DEFAULT_TYPE,
    status_msg,
    user_id: int,
    tempdir: str,
    manager: JobManager,
) -> None:
    settings = manager.settings
    progress = Progress(stage="inspecting")
    poller = asyncio.create_task(_status_loop(progress, status_msg, manager, user_id))
    try:
        await _pipeline(url, update, context, status_msg, user_id, tempdir, manager, progress)
    except asyncio.CancelledError:
        await manager.set_status(user_id, "cancelled")
        await safe_edit(status_msg, "🛑 Download cancelled and temporary files removed.")
        logger.info("Download cancelled for user %d", user_id)
        raise
    except Exception as exc:
        logger.info(
            "Download job failed for user %d: %s: %s",
            user_id,
            type(exc).__name__,
            str(exc)[:300],
        )
        await manager.set_status(user_id, "failed")
        await safe_edit(status_msg, _failure_text(exc, settings))
    finally:
        poller.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await poller
        cleanup_dir(tempdir, settings)
        await manager.untrack(user_id)


async def _pipeline(
    url: str,
    update,
    context: ContextTypes.DEFAULT_TYPE,
    status_msg,
    user_id: int,
    tempdir: str,
    manager: JobManager,
    progress: Progress,
) -> None:
    settings = manager.settings
    chat_id = update.effective_chat.id
    platform = platforms.detect_platform(url)
    logger.info("Job start | user=%d | platform=%s | url=%s", user_id, platform.key, redact_url(url))
    await manager.set_status(user_id, "checking URL")

    use_single_engine = platforms.is_probable_hls(url) or platforms.is_probable_direct_media(url)
    inspection = None
    info: dict | None = None
    title = ""
    duration_s = 0.0
    label = platform.label
    estimated: int | None = None

    if use_single_engine:
        await manager.set_status(user_id, "inspecting stream")
        progress.stage = "inspecting"
        inspection = await downloader.inspect_source(url, settings)
        if inspection.kind == "hls" and inspection.hls is not None:
            estimated = inspection.estimated_bytes
            duration_s = inspection.duration
            label = inspection.label
            title = inspection.title or "HLS stream"
            if inspection.is_live:
                await safe_edit(
                    status_msg,
                    "ℹ️ Live stream detected — downloading the current window as a snapshot.",
                )
        elif inspection.kind == "direct":
            title = inspection.title or "video"
            label = "direct link"
        else:
            inspection = None

    format_override: str | None = None
    if inspection is None:
        await manager.set_status(user_id, "extracting metadata")
        progress.stage = "extracting"
        info = await asyncio.to_thread(downloader.extract_metadata, url, settings)
        title = str(info.get("title") or "video")
        duration_s = float(info.get("duration") or 0.0)
        display = best_display_format(info)
        if display.get("height"):
            label = f"{display.get('width') or '?'}x{display.get('height')}"
        size_hint = display.get("filesize") or display.get("filesize_approx")
        if isinstance(size_hint, (int, float)) and size_hint > 0:
            estimated = int(size_hint)
        fits, smallest = has_format_within_limit(info, settings.effective_max_bytes)
        if not fits:
            cloud_only = not (settings.telegram_api_url or settings.mtproto_enabled)
            if not cloud_only or not smallest or smallest > settings.max_bytes:
                limit_mb = settings.effective_max_mb if not cloud_only else settings.max_file_mb
                raise TooLargeError(
                    "The video exceeds the configured file-size limit "
                    f"({limit_mb} MB; smallest known format ~{format_bytes(smallest)})."
                )
        format_override = pick_format_string(info, settings.effective_max_bytes, settings.max_height)
        if format_override:
            logger.info("Pinned format %s for user %d", format_override, user_id)

    storage_used = manager.disk_usage_bytes()
    if estimated and storage_used + estimated > settings.max_temp_disk_bytes:
        raise TooLargeError(
            "Temporary storage budget exceeded. Try again later or download a smaller video."
        )

    if not media.ffmpeg_available():
        logger.warning("FFmpeg missing; merging/remuxing may fail.")

    await safe_edit(
        status_msg,
        f"⬇️ Downloading video…\n🎬 {title[:100]}\n⏱ {format_duration(duration_s)} | 🎞 {label}",
    )

    await manager.set_status(user_id, "queued")
    progress.stage = "queued"
    await manager.acquire_slot(user_id)
    try:
        await manager.set_status(user_id, "downloading")
        progress.stage = "downloading"
        download_coro = downloader.download_media(
            url,
            tempdir,
            settings,
            progress,
            inspection=inspection,
            format_override=format_override,
            headers=inspection.headers if inspection else None,
        )
        if settings.download_timeout and settings.download_timeout > 0:
            result = await asyncio.wait_for(download_coro, timeout=settings.download_timeout)
        else:
            result = await download_coro
    finally:
        manager.release_slot()

    await manager.set_status(user_id, "validating")
    progress.stage = "processing"
    media_info = await asyncio.to_thread(media.validate_video, result.path)
    try:
        size = os.path.getsize(result.path)
    except OSError as exc:
        raise media.MediaError(f"Downloaded file is not readable: {exc}") from exc
    if size <= 0:
        raise media.MediaError("Downloaded file is empty.")
    if size > settings.max_bytes:
        raise TooLargeError(
            f"The video exceeds the configured file-size limit "
            f"({format_bytes(size)} > {settings.max_file_mb} MB)."
        )
    title = result.title or title
    if result.duration and result.duration > 0:
        duration_s = result.duration
    elif media_info is not None and media_info.duration > 0:
        duration_s = media_info.duration
    logger.info(
        "Download complete | user=%d | size=%s | duration=%s | label=%s",
        user_id,
        format_bytes(size),
        format_duration(duration_s),
        result.label or label,
    )

    await manager.set_status(user_id, "uploading")
    mode, part_count = await manager.uploader.deliver(
        context.bot,
        chat_id,
        result.path,
        title=title,
        duration_hms=format_duration(duration_s),
        duration_s=duration_s,
        size=size,
        progress=progress,
        status_cb=lambda text: safe_edit(status_msg, text),
    )
    await manager.set_status(user_id, "completed")
    if mode == "split":
        await safe_edit(status_msg, f"✅ Download completed ({part_count} parts).")
    else:
        await safe_edit(status_msg, "✅ Download completed.")
