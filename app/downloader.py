"""Download engine router.

Three engines are available:

* **native HLS** — for direct ``.m3u8`` URLs, using :mod:`app.hls_download`,
* **direct HTTP** — for direct media file URLs (``.mp4``, ``.webm``, …),
* **yt-dlp** — for platform pages (YouTube, Vimeo, …) and as a fallback.

Direct URLs use their engine first and fall back to yt-dlp; platform pages go
straight to yt-dlp.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import yt_dlp
from yt_dlp.utils import DownloadError

from . import hls_download, media, net, platforms
from .config import Settings
from .format_select import build_format_selector
from .hls import DRMProtectedError, PlaylistError
from .progress import Progress
from .selections import QualitySelection, QualityUnavailableError

logger = logging.getLogger("uvd_bot.downloader")


class DownloadFailedError(Exception):
    """Raised when an engine fails and no fallback succeeded."""


@dataclass
class DownloadResult:
    path: str
    title: str = "video"
    duration: float = 0.0
    width: int | None = None
    height: int | None = None
    label: str = ""
    source_info: dict = field(default_factory=dict)


@dataclass
class SourceInspection:
    kind: str = "ytdlp"
    label: str = ""
    title: str = ""
    duration: float = 0.0
    estimated_bytes: int | None = None
    is_live: bool = False
    hls: hls_download.HlsInspection | None = None
    headers: dict[str, str] | None = None


def _detect_impersonate() -> dict:
    """Probe yt-dlp for the accepted browser-impersonation option form."""
    try:
        import curl_cffi  # noqa: F401
    except ImportError:
        logger.warning("curl_cffi not installed; browser impersonation disabled.")
        return {}
    candidates: list = []
    try:
        from yt_dlp.networking.impersonate import ImpersonateTarget

        from_str = getattr(ImpersonateTarget, "from_str", None)
        if callable(from_str):
            try:
                candidates.append(from_str("chrome"))
            except Exception:
                pass
        try:
            candidates.append(ImpersonateTarget("chrome", None, None, None))
        except Exception:
            pass
    except ImportError:
        pass
    candidates.append("chrome")
    for target in candidates:
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "impersonate": target}):
                pass
            logger.info("Browser impersonation enabled.")
            return {"impersonate": target}
        except Exception:
            continue
    logger.warning("Browser impersonation unavailable; continuing without it.")
    return {}


IMPERSONATE_OPT: dict = _detect_impersonate()


def _progress_hook(progress: Progress):
    def hook(data: dict) -> None:
        try:
            status = data.get("status")
            if status == "downloading":
                progress.stage = "downloading"
                progress.downloaded = data.get("downloaded_bytes") or 0
                progress.total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
                speed = data.get("speed")
                progress.speed = float(speed) if isinstance(speed, (int, float)) else 0.0
                eta = data.get("eta")
                progress.eta = float(eta) if isinstance(eta, (int, float)) else None
                progress.detail = ""
            elif status == "finished":
                progress.stage = "processing"
        except Exception:
            pass

    return hook


def build_ydl_opts(
    settings: Settings,
    tempdir: str,
    progress: Progress,
    format_override: str | None = None,
) -> dict:
    return {
        "format": format_override or build_format_selector(settings.effective_max_bytes),
        "merge_output_format": "mp4",
        "outtmpl": os.path.join(tempdir, "%(title).80s [%(id)s].%(ext)s"),
        "restrictfilenames": True,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "socket_timeout": settings.request_timeout,
        "retries": 3,
        "extractor_retries": 2,
        "concurrent_fragment_downloads": 1,
        "progress_hooks": [_progress_hook(progress)],
        "allow_unplayable_formats": False,
        **IMPERSONATE_OPT,
    }


def extract_metadata(url: str, settings: Settings) -> dict:
    """Blocking metadata extraction (runs in a worker thread)."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": settings.request_timeout,
        "skip_download": True,
        "retries": 2,
        "extractor_retries": 1,
        **IMPERSONATE_OPT,
    }
    last_exc: Exception | None = None
    for attempt in (1, 2):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            break
        except Exception as exc:
            low = str(exc).lower()
            if attempt == 1 and ("403" in low or "forbidden" in low):
                last_exc = exc
                time.sleep(5)
                continue
            raise
    else:
        raise last_exc or DownloadError("No metadata returned.")
    if info is None:
        raise DownloadError("No metadata returned.")
    if info.get("_type") == "playlist":
        entries = [entry for entry in (info.get("entries") or []) if entry]
        if not entries:
            raise DownloadError("Playlist contains no downloadable entries.")
        info = entries[0]
    return info


def _find_output_file(tempdir: str, prepared: str | None = None) -> str | None:
    if prepared:
        base, _ = os.path.splitext(prepared)
        for candidate in (prepared, base + ".mp4", base + ".mkv", base + ".webm"):
            if os.path.exists(candidate):
                return candidate
    media_files = []
    for ext in (".mp4", ".mkv", ".webm", ".mov", ".m4v"):
        media_files.extend(Path(tempdir).glob(f"*{ext}"))
    if not media_files:
        return None
    return str(max(media_files, key=lambda p: p.stat().st_mtime))


def download_with_ytdlp(
    url: str,
    tempdir: str,
    settings: Settings,
    progress: Progress,
    format_override: str | None = None,
) -> DownloadResult:
    """Blocking yt-dlp download (runs in a worker thread)."""
    opts = build_ydl_opts(settings, tempdir, progress, format_override)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if info is None:
            raise DownloadError("Download returned no data.")
        if info.get("_type") == "playlist":
            entries = [entry for entry in (info.get("entries") or []) if entry]
            if not entries:
                raise DownloadError("Playlist contains no downloadable entries.")
            info = entries[0]
        prepared = None
        try:
            prepared = ydl.prepare_filename(info)
        except Exception:
            prepared = None
    path = _find_output_file(tempdir, prepared)
    if not path:
        raise DownloadError("Download finished but no video file was produced.")
    return DownloadResult(
        path=path,
        title=str(info.get("title") or "video"),
        duration=float(info.get("duration") or 0.0),
        width=info.get("width"),
        height=info.get("height"),
        label="yt-dlp",
        source_info=info,
    )


async def _download_direct(
    url: str,
    tempdir: str,
    settings: Settings,
    progress: Progress,
    headers: dict[str, str] | None = None,
) -> DownloadResult:
    split = urlsplit(url)
    raw_ext = os.path.splitext(split.path)[1].lower()
    ext = raw_ext if raw_ext in (".mp4", ".m4v", ".webm", ".mov", ".mkv", ".ts") else ".mp4"
    dest = os.path.join(tempdir, f"direct{ext}")
    async with net.make_client(settings, headers) as client:
        meta = await net.stream_to_file(
            client,
            url,
            settings,
            dest,
            headers=headers,
            progress=progress,
            max_bytes=settings.max_bytes,
        )
    content_type = (meta.get("content_type") or "").lower()
    if "text/html" in content_type or "application/json" in content_type:
        raise DownloadFailedError(
            f"URL returned {content_type or 'a non-media response'} instead of a media file."
        )
    title = os.path.basename(split.path) or "video"
    return DownloadResult(
        path=dest,
        title=title,
        duration=0.0,
        label="direct link",
        source_info={"ext": ext, "content_type": content_type},
    )


async def inspect_source(
    url: str,
    settings: Settings,
    headers: dict[str, str] | None = None,
    *,
    selection: QualitySelection | None = None,
    max_height: int | None = None,
) -> SourceInspection:
    """Decide which engine to use and (for HLS) select a variant up front."""
    if platforms.is_probable_hls(url):
        try:
            inspection = await hls_download.inspect_hls(
                url,
                settings,
                headers,
                target_height=selection.target_height if selection and selection.is_explicit else None,
                max_height=max_height,
            )
            return SourceInspection(
                kind="hls",
                label=inspection.label,
                duration=inspection.playlist.total_duration,
                estimated_bytes=inspection.estimated_bytes,
                is_live=inspection.is_live,
                hls=inspection,
                headers=headers,
            )
        except DRMProtectedError:
            raise
        except (PlaylistError, net.NetworkError, media.MediaError) as exc:
            logger.info("Native HLS inspection failed (%s); falling back to yt-dlp.", exc)
            return SourceInspection(kind="ytdlp", headers=headers)
    if platforms.is_probable_direct_media(url):
        split = urlsplit(url)
        return SourceInspection(
            kind="direct",
            label="direct link",
            title=os.path.basename(split.path) or "video",
            headers=headers,
        )
    return SourceInspection(kind="ytdlp", headers=headers)


async def download_media(
    url: str,
    tempdir: str,
    settings: Settings,
    progress: Progress,
    *,
    inspection: SourceInspection | None = None,
    format_override: str | None = None,
    headers: dict[str, str] | None = None,
    selection: QualitySelection | None = None,
) -> DownloadResult:
    """Download ``url`` using the routed engine, with safe fallbacks."""
    inspection = inspection or await inspect_source(url, settings, headers, selection=selection)
    if inspection.kind == "hls" and inspection.hls is not None:
        dest = os.path.join(tempdir, "video.mp4")
        try:
            outcome = await hls_download.download_hls(
                inspection.hls, dest, settings, progress, inspection.headers or headers
            )
        except DRMProtectedError:
            raise
        except QualityUnavailableError:
            raise
        except (PlaylistError, net.NetworkError, media.MediaError) as exc:
            if selection is not None and selection.is_explicit:
                raise QualityUnavailableError(
                    "The selected HLS quality became unavailable during download."
                ) from exc
            logger.info("Native HLS download failed (%s); falling back to yt-dlp.", exc)
        else:
            return DownloadResult(
                path=outcome.path,
                title=inspection.title or "video",
                duration=outcome.duration,
                label=outcome.label,
            )
    if inspection.kind == "direct":
        try:
            result = await _download_direct(url, tempdir, settings, progress, inspection.headers or headers)
            return result
        except DRMProtectedError:
            raise
        except (DownloadFailedError, net.NetworkError, media.MediaError) as exc:
            logger.info("Direct download failed (%s); falling back to yt-dlp.", exc)
    progress.stage = "downloading"
    return await asyncio.to_thread(
        download_with_ytdlp, url, tempdir, settings, progress, format_override
    )
