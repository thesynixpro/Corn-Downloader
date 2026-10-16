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
import secrets
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from telegram.ext import ContextTypes

from . import downloader, errors, media, platforms, tgui, ui
from .config import Settings
from .format_select import (
    best_display_format,
    build_quality_options,
    has_format_within_limit,
    pick_format_string,
    selector_for_height,
)
from .formatting import format_bytes, format_duration
from .hls import DRMProtectedError
from .net import DownloadTooLargeError, NetworkError
from .progress import Progress
from .selections import QualitySelection, QualityUnavailableError, effective_height_cap
from .store import UserStore
from .uploader import UploadError, Uploader
from .urlguard import URLGuardError, redact_url

logger = logging.getLogger("uvd_bot.jobs")


class TooLargeError(Exception):
    """Raised when a video exceeds configured size or storage limits."""


@dataclass(frozen=True)
class PendingOption:
    key: str
    label: str
    target_height: int | None


@dataclass
class PendingSession:
    id: str
    user_id: int
    url: str
    kind: str
    title: str
    duration: float
    label: str
    platform: str
    created_at: float
    options: tuple[PendingOption, ...] = ()
    live: bool = False


@dataclass
class LastRequest:
    url: str
    selection: QualitySelection
    created_at: float


PENDING_TTL_SECONDS = 600.0


class JobManager:
    """Tracks per-user tasks, queue positions, temp dirs, UI sessions, and stats."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.uploader = Uploader(settings)
        self.store = UserStore(settings.data_dir / "uvd_bot.sqlite3")
        self.semaphore = asyncio.Semaphore(settings.max_concurrent_downloads)
        self._lock = asyncio.Lock()
        self._tasks: dict[int, asyncio.Task] = {}
        self._tempdirs: dict[int, str] = {}
        self._statuses: dict[int, str] = {}
        self._waiting: dict[int, float] = {}
        self._pending: dict[str, PendingSession] = {}
        self._reserved: set[int] = set()
        self._last_requests: dict[int, LastRequest] = {}
        self._feedback_pending: set[int] = set()

    async def register(self, user_id: int, task: asyncio.Task, tempdir: str) -> None:
        async with self._lock:
            self._tasks[user_id] = task
            self._tempdirs[user_id] = tempdir
            self._statuses[user_id] = "queued"
            self._reserved.discard(user_id)
        task.add_done_callback(lambda finished: asyncio.create_task(self._forget(user_id, finished)))

    async def try_reserve(self, user_id: int) -> bool:
        """Atomically reserve a user for an imminent job to prevent duplicates."""
        async with self._lock:
            task = self._tasks.get(user_id)
            if (task is not None and not task.done()) or user_id in self._reserved:
                return False
            self._reserved.add(user_id)
            return True

    async def release_reservation(self, user_id: int) -> None:
        async with self._lock:
            self._reserved.discard(user_id)

    async def get_prefs(self, user_id: int) -> dict[str, str]:
        return await asyncio.to_thread(self.store.get_preferences, user_id)

    async def set_pref(self, user_id: int, key: str, value: str) -> dict[str, str]:
        return await asyncio.to_thread(self.store.set_preference, user_id, key, value)

    async def record_completion(
        self, user_id: int, platform: str, mode: str, parts: int, size_bytes: int
    ) -> None:
        await asyncio.to_thread(
            self.store.record_download, user_id, platform, mode, parts, size_bytes
        )

    async def get_stats(self, user_id: int) -> dict[str, int | float | None]:
        return await asyncio.to_thread(self.store.get_stats, user_id)

    def _prune_pending(self) -> None:
        now = time.monotonic()
        expired = [
            session_id
            for session_id, session in self._pending.items()
            if now - session.created_at > PENDING_TTL_SECONDS
        ]
        for session_id in expired:
            self._pending.pop(session_id, None)

    def create_pending_session(
        self,
        *,
        user_id: int,
        url: str,
        kind: str,
        title: str,
        duration: float,
        label: str,
        platform: str,
        options: tuple[PendingOption, ...],
        live: bool = False,
    ) -> PendingSession:
        self._prune_pending()
        session = PendingSession(
            id=secrets.token_hex(6),
            user_id=user_id,
            url=url,
            kind=kind,
            title=title,
            duration=duration,
            label=label,
            platform=platform,
            created_at=time.monotonic(),
            options=options,
            live=live,
        )
        self._pending[session.id] = session
        return session

    def peek_pending_session(self, user_id: int, session_id: str) -> PendingSession | None:
        self._prune_pending()
        session = self._pending.get(session_id)
        if session is None or session.user_id != user_id:
            return None
        return session

    def pop_pending_session(self, user_id: int, session_id: str) -> PendingSession | None:
        self._prune_pending()
        session = self._pending.pop(session_id, None)
        if session is None or session.user_id != user_id:
            if session is not None:
                self._pending[session.id] = session
            return None
        return session

    def clear_pending_for_user(self, user_id: int) -> None:
        self._prune_pending()
        for session_id in [sid for sid, session in self._pending.items() if session.user_id == user_id]:
            self._pending.pop(session_id, None)

    def set_last_request(self, user_id: int, url: str, selection: QualitySelection) -> None:
        self._last_requests[user_id] = LastRequest(
            url=url, selection=selection, created_at=time.monotonic()
        )

    def get_last_request(self, user_id: int) -> LastRequest | None:
        return self._last_requests.get(user_id)

    def set_feedback_pending(self, user_id: int) -> None:
        self._feedback_pending.add(user_id)

    def clear_feedback_pending(self, user_id: int) -> None:
        self._feedback_pending.discard(user_id)

    def is_feedback_pending(self, user_id: int) -> bool:
        return user_id in self._feedback_pending

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
            self._reserved.discard(user_id)
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
            self._pending.clear()
            self._reserved.clear()
            self._feedback_pending.clear()
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


async def _status_loop(
    progress: Progress,
    status_msg,
    manager: JobManager,
    user_id: int,
    interval: float = 3.0,
) -> None:
    last_text = ""
    while True:
        await asyncio.sleep(max(1.0, interval))
        card = ui.status_card(
            title=progress.title or "Video",
            label=progress.label or "Source",
            phase=progress.stage,
            downloaded=progress.downloaded,
            total=progress.total,
            speed=progress.speed,
            eta=progress.eta,
            detail=progress.detail,
            queue_position=manager.queue_position(user_id),
            show_cancel=True,
        )
        if card.text != last_text:
            last_text = card.text
            await tgui.edit_card(status_msg, card)


def failure_card(
    exc: BaseException,
    settings: Settings,
    *,
    retry_available: bool,
    support_enabled: bool,
) -> ui.Card:
    if isinstance(exc, DRMProtectedError):
        return ui.error_card(
            kind="drm",
            message=f"{exc} The bot does not bypass DRM.",
            support_enabled=support_enabled,
        )
    if isinstance(exc, TooLargeError):
        return ui.error_card(kind="too_large", message=str(exc), support_enabled=support_enabled)
    if isinstance(exc, DownloadTooLargeError):
        return ui.error_card(
            kind="too_large",
            message=f"The video exceeds the configured size limit ({settings.max_file_mb} MB).",
            support_enabled=support_enabled,
        )
    if isinstance(exc, URLGuardError):
        return ui.error_card(
            kind="blocked",
            message="This URL is not allowed: the host is blocked, private, or not on the allow-list.",
            support_enabled=support_enabled,
        )
    if isinstance(exc, UploadError):
        return ui.error_card(
            kind="failed",
            message="The download completed, but Telegram could not upload the file.",
            detail=errors.short_detail(exc),
            retry=retry_available,
            support_enabled=support_enabled,
        )
    if isinstance(exc, AssertionError):
        return ui.error_card(
            kind="failed",
            message=(
                "This video's page layout isn't readable right now (the site may have changed). "
                "Please try again later or use another link."
            ),
            detail=errors.short_detail(exc),
            retry=retry_available,
            support_enabled=support_enabled,
        )
    if isinstance(exc, media.MediaError):
        return ui.error_card(
            kind="failed",
            message="Video validation failed.",
            detail=errors.short_detail(exc),
            retry=retry_available,
            support_enabled=support_enabled,
        )
    if isinstance(exc, NetworkError):
        return ui.error_card(
            kind="network",
            message="The download failed due to a network error. Please try again later.",
            detail=errors.short_detail(exc),
            retry=retry_available,
            support_enabled=support_enabled,
        )
    if isinstance(exc, QualityUnavailableError):
        return ui.error_card(
            kind="quality",
            message=str(exc),
            retry=retry_available,
            support_enabled=support_enabled,
        )
    mapped = errors.map_download_error(exc)
    detail = errors.short_detail(exc)
    lowered = mapped.lower()
    if "not currently supported" in lowered:
        kind, include_platforms, show_detail = "unsupported", True, False
    elif "login" in lowered or "cannot and will not bypass" in lowered:
        kind, include_platforms, show_detail = "auth", False, False
    elif "drm" in lowered:
        kind, include_platforms, show_detail = "drm", False, False
    elif "geo-restricted" in lowered:
        kind, include_platforms, show_detail = "geo", False, False
    elif "expired" in lowered:
        kind, include_platforms, show_detail = "expired", False, True
    elif "403" in lowered or "timed out" in lowered:
        kind, include_platforms, show_detail = "network", False, True
    else:
        kind, include_platforms, show_detail = "failed", False, True
    if show_detail and detail and detail.lower() in lowered:
        detail = ""
    return ui.error_card(
        kind=kind,
        message=mapped,
        detail=detail if show_detail else "",
        retry=retry_available and kind in ("failed", "network", "expired", "quality", "unexpected"),
        support_enabled=support_enabled,
        include_platforms=include_platforms,
    )


async def run_download_job(
    url: str,
    update,
    context: ContextTypes.DEFAULT_TYPE,
    status_msg,
    user_id: int,
    tempdir: str,
    manager: JobManager,
    *,
    selection: QualitySelection | None = None,
    default_quality: str = "auto",
    upload_mode: str = "stream",
    progress_interval: float = 3.0,
) -> None:
    settings = manager.settings
    progress = Progress(stage="inspecting")
    poller = asyncio.create_task(
        _status_loop(progress, status_msg, manager, user_id, interval=progress_interval)
    )
    try:
        await _pipeline(
            url,
            update,
            context,
            status_msg,
            user_id,
            tempdir,
            manager,
            progress,
            selection=selection or QualitySelection(),
            default_quality=default_quality,
            upload_mode=upload_mode,
        )
    except asyncio.CancelledError:
        await manager.set_status(user_id, "cancelled")
        await tgui.edit_card(status_msg, ui.cancelled_card())
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
        await tgui.edit_card(
            status_msg,
            failure_card(
                exc,
                settings,
                retry_available=manager.get_last_request(user_id) is not None,
                support_enabled=manager.settings.support_chat_id is not None,
            ),
        )
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
    *,
    selection: QualitySelection,
    default_quality: str,
    upload_mode: str,
) -> None:
    settings = manager.settings
    chat_id = update.effective_chat.id
    platform = platforms.detect_platform(url)
    logger.info("Job start | user=%d | platform=%s | url=%s", user_id, platform.key, redact_url(url))
    await manager.set_status(user_id, "checking URL")

    use_single_engine = platforms.is_probable_hls(url) or platforms.is_probable_direct_media(url)
    height_cap = effective_height_cap(settings.max_height, default_quality, selection)
    inspection = None
    info: dict | None = None
    title = ""
    duration_s = 0.0
    label = platform.label
    estimated: int | None = None

    if use_single_engine:
        await manager.set_status(user_id, "inspecting stream")
        progress.stage = "inspecting"
        inspection = await downloader.inspect_source(
            url, settings, None, selection=selection, max_height=height_cap
        )
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
        format_override = pick_format_string(info, settings.effective_max_bytes, height_cap)
        if selection.is_explicit and format_override is None:
            raise QualityUnavailableError(
                f"{selection.target_height}p is not available within the current limits."
            )
        if format_override:
            logger.info("Pinned format %s for user %d", format_override, user_id)

    progress.title = title or "Video"
    progress.label = label
    storage_used = manager.disk_usage_bytes()
    if estimated and storage_used + estimated > settings.max_temp_disk_bytes:
        raise TooLargeError(
            "Temporary storage budget exceeded. Try again later or download a smaller video."
        )

    if not media.ffmpeg_available():
        logger.warning("FFmpeg missing; merging/remuxing may fail.")

    await tgui.edit_card(
        status_msg,
        ui.status_card(
            title=progress.title,
            label=progress.label,
            phase=progress.stage,
            downloaded=progress.downloaded,
            total=progress.total,
            speed=progress.speed,
            eta=progress.eta,
            detail=f"⏱ {format_duration(duration_s)} • Ready to queue",
            queue_position=manager.queue_position(user_id),
            show_cancel=True,
        ),
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
            selection=selection,
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
    progress.title = title or "Video"
    progress.label = result.label or label
    logger.info(
        "Download complete | user=%d | size=%s | duration=%s | label=%s",
        user_id,
        format_bytes(size),
        format_duration(duration_s),
        result.label or label,
    )

    async def _milestone(text: str) -> None:
        clean = text.strip().replace("\n", " ")
        if len(clean) > 220:
            clean = clean[:219].rstrip() + "…"
        progress.detail = clean
        await tgui.edit_card(
            status_msg,
            ui.status_card(
                title=progress.title,
                label=progress.label,
                phase=progress.stage,
                downloaded=progress.downloaded,
                total=progress.total,
                speed=progress.speed,
                eta=progress.eta,
                detail=clean,
                queue_position=manager.queue_position(user_id),
                show_cancel=True,
            ),
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
        status_cb=_milestone,
        upload_mode=upload_mode,
    )
    try:
        await manager.record_completion(user_id, platform.key, mode, part_count, size)
    except Exception as exc:
        logger.warning("Recording download stats failed for user %d: %s", user_id, type(exc).__name__)
    await manager.set_status(user_id, "completed")
    await tgui.edit_card(
        status_msg,
        ui.completion_card(
            file_name=os.path.basename(result.path),
            size_bytes=size,
            quality=result.label or label,
            mode=mode,
            parts=part_count,
            support_enabled=manager.settings.support_chat_id is not None,
        ),
    )
