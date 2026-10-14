import asyncio
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from yt_dlp.utils import DownloadError, ExtractorError, UnsupportedError

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Configuration — all settings come from environment variables.
# Fill them in Railway: Service → Variables tab (or local .env file).
#
#   BOT_TOKEN              (required) Bot token from @BotFather.
#   MAX_FILE_MB            (optional, default 700) Max video size in MB (1-2000).
#                            >50 MB uploads need TELEGRAM_API_URL below.
#   DOWNLOAD_DIR           (optional, default /tmp/video_downloads)
#   MAX_CONCURRENT_DOWNLOADS (optional, default 3) Global download cap.
#   REQUEST_TIMEOUT        (optional, default 120) Extractor/network timeout, seconds.
#                            Uploads always get at least 600 s.
#   TELEGRAM_API_URL       (optional) Self-hosted Bot API base URL, e.g.
#                            http://localhost:8081 — required for >~50 MB uploads.
#                            Leave empty to use Telegram's default cloud API.
#   LOG_LEVEL              (optional, default INFO) DEBUG/INFO/WARNING/ERROR.
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)).strip())
    except (TypeError, ValueError, AttributeError):
        return default


# Max file size is admin-configurable. Default raised to 700 MB per request.
# NOTE: Telegram's default Bot API only accepts ~50 MB uploads from bots.
# Files >50 MB require a self-hosted/local Bot API server (see TELEGRAM_API_URL).
MAX_FILE_MB = _int_env("MAX_FILE_MB", 700)
if MAX_FILE_MB < 1:
    MAX_FILE_MB = 1
if MAX_FILE_MB > 2000:
    MAX_FILE_MB = 2000
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/tmp/video_downloads")
MAX_CONCURRENT_DOWNLOADS = _int_env("MAX_CONCURRENT_DOWNLOADS", 3)
if MAX_CONCURRENT_DOWNLOADS < 1:
    MAX_CONCURRENT_DOWNLOADS = 1
REQUEST_TIMEOUT = _int_env("REQUEST_TIMEOUT", 120)
# Optional custom Bot API server (required for uploads > ~50 MB).
# Must include protocol, e.g. TELEGRAM_API_URL=https://api-example.up.railway.app
# (Bare hostnames without http(s):// are auto-fixed to https://.)
# Leave empty to use Telegram's default cloud API.
def _normalize_api_url(raw: str) -> str:
    url = (raw or "").strip().rstrip("/")
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    scheme = url.split("://", 1)[0].lower()
    if scheme not in ("http", "https"):
        return ""
    return url


TELEGRAM_API_URL = _normalize_api_url(os.environ.get("TELEGRAM_API_URL", ""))
# Large uploads need generous timeouts regardless of REQUEST_TIMEOUT.
UPLOAD_TIMEOUT = max(REQUEST_TIMEOUT, 600)
# Split delivery for the cloud Bot API (~50 MB/bot-upload cap): videos that
# fit MAX_FILE_MB but not the cloud cap are cut into parts and sent as
# multiple messages. Only used when TELEGRAM_API_URL is NOT set.
SPLIT_PART_MB = _int_env("SPLIT_PART_MB", 45)
SPLIT_PART_MB = min(max(SPLIT_PART_MB, 20), 49)
SPLIT_MAX_PARTS = _int_env("SPLIT_MAX_PARTS", 15)
SPLIT_MAX_PARTS = min(max(SPLIT_MAX_PARTS, 2), 25)
# Optional: MTProto direct upload (single files up to ~2 GB) via Telethon,
# logged in as the BOT itself (same BOT_TOKEN, no user session needed).
# Needs API_ID + API_HASH from https://my.telegram.org.
# When set, large files upload directly instead of being split into parts.
API_ID_RAW = os.environ.get("API_ID", "").strip()
API_ID = int(API_ID_RAW) if API_ID_RAW.isdigit() else 0
API_HASH = os.environ.get("API_HASH", "").strip()
MTPROTO_ENABLED = bool(API_ID and API_HASH)
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
if LOG_LEVEL not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
    LOG_LEVEL = "INFO"

MAX_BYTES = MAX_FILE_MB * 1024 * 1024
# Telegram cloud Bot API accepts ~50 MB uploads from bots. Anything bigger
# needs a self-hosted local Bot API server (TELEGRAM_API_URL).
CLOUD_UPLOAD_LIMIT = 50 * 1024 * 1024
# Effective deliverable limit: without a local Bot API server or MTProto
# direct upload, picking a format above ~50 MB is pointless — it downloads
# fine but can never upload as one file (only via splitting, up to MAX).
# (With TELEGRAM_API_URL or API_ID/API_HASH set, the full MAX_FILE_MB applies.)
if TELEGRAM_API_URL or MTPROTO_ENABLED:
    EFFECTIVE_MAX_MB = MAX_FILE_MB
else:
    EFFECTIVE_MAX_MB = min(MAX_FILE_MB, 50)
EFFECTIVE_MAX_BYTES = EFFECTIVE_MAX_MB * 1024 * 1024
URL_RE = re.compile(r"^https?://\S+\.\S+", re.IGNORECASE)
MAX_URL_LENGTH = 2048

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)
logger = logging.getLogger("uvd_bot")

# Silence chatty third-party loggers. Their request lines embed the full
# Bot API URL including BOT_TOKEN — that must never land in Railway logs.
for _noisy in ("httpx", "httpcore", "telegram", "apscheduler"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

# Redact token from logs: never log BOT_TOKEN.
# Only log its presence/absence.
if not BOT_TOKEN:
    logger.error("BOT_TOKEN is missing. Set it as an environment variable.")
else:
    logger.info("BOT_TOKEN is configured (value hidden).")
if TELEGRAM_API_URL:
    logger.info("Custom Bot API server: %s", TELEGRAM_API_URL)
if MTPROTO_ENABLED:
    logger.info("MTProto direct upload enabled (single files up to ~2 GB).")
else:
    logger.info("MTProto not configured (set API_ID/API_HASH for >50 MB single-file upload).")

BASE_DOWNLOAD_DIR = Path(DOWNLOAD_DIR)
try:
    BASE_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
except Exception as exc:  # noqa: BLE001 - startup check only
    logger.error("Could not create DOWNLOAD_DIR %s: %s", DOWNLOAD_DIR, exc)

download_semaphore = asyncio.Semaphore(max(1, MAX_CONCURRENT_DOWNLOADS))

# Lazy MTProto client (Telethon), logged in as the BOT itself.
# Uploads via MTProto bypass the cloud Bot API ~50 MB cap (up to ~2 GB).
_mtproto_client = None
_mtproto_lock = asyncio.Lock()


async def get_mtproto_client():
    """Connect once and return the MTProto client, or None if unavailable."""
    global _mtproto_client
    if _mtproto_client is not None:
        try:
            if _mtproto_client.is_connected():
                return _mtproto_client
        except Exception:  # noqa: BLE001
            pass
    if not MTPROTO_ENABLED:
        return None
    async with _mtproto_lock:
        if _mtproto_client is not None:
            try:
                if _mtproto_client.is_connected():
                    return _mtproto_client
            except Exception:  # noqa: BLE001
                pass
        try:
            from telethon import TelegramClient
        except ImportError:
            logger.warning("telethon not installed; MTProto direct upload disabled.")
            return None
        try:
            session_path = str(BASE_DOWNLOAD_DIR / "mtproto_bot_session")
            client = TelegramClient(session_path, API_ID, API_HASH)
            await client.start(bot_token=BOT_TOKEN)
            _mtproto_client = client
            logger.info("MTProto connected (direct upload up to ~2 GB available).")
            return client
        except Exception as exc:  # noqa: BLE001
            logger.warning("MTProto connect failed: %s: %s", type(exc).__name__, str(exc)[:200])
            return None

# user_id -> asyncio.Task
active_tasks: dict[int, asyncio.Task] = {}
# user_id -> status string
user_status: dict[int, str] = {}
# user_id -> tempdir path (for cleanup on cancel)
user_tempdir: dict[int, str] = {}
tasks_lock = asyncio.Lock()

FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None
if not FFMPEG_AVAILABLE:
    logger.warning("FFmpeg binary not found. Merging separate audio/video streams will fail.")
else:
    logger.info("FFmpeg found.")

# Browser TLS impersonation (via curl_cffi) helps against sites that block
# plain datacenter HTTP fingerprints with 403 errors. Legitimate technique:
# it only makes requests look like a regular Chrome browser. No DRM,
# paywall or login bypass involved.
# NOTE: yt-dlp versions disagree on the accepted 'impersonate' form
# (string vs ImpersonateTarget object), so we probe at startup and use
# whatever this install accepts — or nothing at all.
def _detect_impersonate_opt() -> dict:
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
            except Exception:  # noqa: BLE001
                pass
        try:
            candidates.append(ImpersonateTarget("chrome", None, None, None))
        except Exception:  # noqa: BLE001
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
        except Exception:  # noqa: BLE001 - try next form
            continue
    logger.warning("Browser impersonation unavailable in this yt-dlp version; continuing without it.")
    return {}


IMPERSONATE_OPT: dict = _detect_impersonate_opt()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_valid_public_url(url: str) -> tuple[bool, str]:
    """Validate URL. Returns (ok, reason)."""
    url = (url or "").strip()
    if not url or len(url) > MAX_URL_LENGTH:
        return False, "invalid"
    if not URL_RE.match(url):
        return False, "invalid"
    try:
        parsed = urlparse(url)
    except Exception:  # noqa: BLE001
        return False, "invalid"
    if parsed.scheme.lower() not in ("http", "https"):
        return False, "unsafe_scheme"
    if not parsed.netloc or "." not in parsed.netloc:
        return False, "invalid"
    # Block obviously local / unsafe targets.
    host = parsed.hostname or ""
    host = host.lower()
    if host in ("localhost",) or host.startswith("127.") or host in ("::1",):
        return False, "unsafe_scheme"
    return True, ""


def canonicalize_url(url: str) -> str:
    """Rewrite known mirror domains to the canonical host of the same public site.

    Example: xhamster46.desi/videos/... -> xhamster.com/videos/...
    Same site, same paths — only the host differs, which yt-dlp may not recognize.
    """
    try:
        parsed = urlparse(url)
    except Exception:  # noqa: BLE001
        return url
    host = (parsed.hostname or "").lower()
    if re.match(r"^(www\.)?xhamster\d*\.", host):
        try:
            return parsed._replace(netloc="xhamster.com").geturl()
        except Exception:  # noqa: BLE001
            return url
    return url


def format_bytes(n: int | float | None) -> str:
    if n is None:
        return "unknown size"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def format_duration(seconds: int | float | None) -> str:
    if seconds is None:
        return "unknown"
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return "unknown"
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:d}:{s:02d}"


def build_format_selector() -> str:
    """Prefer MP4 combined streams inside the size limit, with fallbacks."""
    limit = EFFECTIVE_MAX_BYTES
    # yt-dlp understands [filesize<123] predicates.
    return (
        f"best[ext=mp4][filesize<{limit}]/"
        f"best[filesize<{limit}]/"
        f"best[ext=mp4]/"
        f"best"
    )


def base_ydl_opts(tempdir: str, progress: dict) -> dict:
    def _hook(d: dict) -> None:
        # Runs in yt-dlp worker thread; only mutate plain dict.
        try:
            if d.get("status") == "downloading":
                progress["downloaded"] = d.get("downloaded_bytes", 0)
                progress["total"] = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
                progress["stage"] = "downloading"
            elif d.get("status") == "finished":
                progress["stage"] = "processing"
        except Exception:  # noqa: BLE001 - hook must never raise
            pass

    return {
        "format": build_format_selector(),
        "merge_output_format": "mp4",
        "outtmpl": os.path.join(tempdir, "%(title).80s [%(id)s].%(ext)s"),
        "restrictfilenames": True,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": REQUEST_TIMEOUT,
        "retries": 3,
        "extractor_retries": 2,
        "concurrent_fragment_downloads": 1,
        "progress_hooks": [_hook],
        # Do NOT bypass DRM / paywalls / private content.
        "allow_unplayable_formats": False,
        **IMPERSONATE_OPT,
    }


def extract_metadata(url: str) -> dict:
    """Blocking: extract metadata without downloading. Runs in a thread."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": REQUEST_TIMEOUT,
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
        except Exception as exc:  # noqa: BLE001
            # One retry for 403s: datacenter blocks are sometimes transient.
            low = str(exc).lower()
            if attempt == 1 and ("403" in low or "forbidden" in low):
                last_exc = exc
                time.sleep(5)
                continue
            raise
    else:  # pragma: no cover - loop always breaks or raises
        raise last_exc or DownloadError("No metadata returned.")
    if info is None:
        raise DownloadError("No metadata returned.")
    # If a playlist slipped through, take first entry.
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise DownloadError("Playlist contains no downloadable entries.")
        info = entries[0]
    return info


def has_format_within_limit(info: dict) -> tuple[bool, int | None]:
    """Check whether any usable format fits inside EFFECTIVE_MAX_BYTES.

    Returns (fits, smallest_known_size_or_None).
    """
    formats = info.get("formats") or []
    smallest: int | None = None
    for f in formats:
        size = f.get("filesize") or f.get("filesize_approx")
        if isinstance(size, (int, float)) and size > 0:
            if smallest is None or size < smallest:
                smallest = int(size)
            if size <= EFFECTIVE_MAX_BYTES:
                return True, int(size)
    # Direct (non-format-list) info sometimes carries filesize.
    for key in ("filesize", "filesize_approx"):
        size = info.get(key)
        if isinstance(size, (int, float)) and size > 0:
            if smallest is None or size < smallest:
                smallest = int(size)
            if size <= EFFECTIVE_MAX_BYTES:
                return True, int(size)
    if not formats and smallest is None:
        # No size info at all: cannot pre-judge; allow attempt, enforce after.
        return True, None
    if smallest is None:
        return True, None  # unknown sizes -> try, enforce post-download
    return False, smallest


def pick_format_string(info: dict, cap: int) -> str | None:
    """Choose an explicit yt-dlp format string fitting inside cap.

    Picks the largest fitting rendition, preferring combined audio+video MP4.
    Returns None when no usable size info exists (caller falls back to the
    generic selector) or when nothing fits (caller rejects).
    """
    formats = [f for f in (info.get("formats") or []) if f.get("url") and f.get("format_id")]

    def _size(f: dict) -> int | None:
        s = f.get("filesize") or f.get("filesize_approx")
        return int(s) if isinstance(s, (int, float)) and s > 0 else None

    def _has_av(f: dict) -> bool:
        return f.get("vcodec") not in (None, "none") and f.get("acodec") not in (None, "none")

    sized = [(f, _size(f)) for f in formats]
    sized = [(f, s) for f, s in sized if s is not None]
    if not sized:
        return None
    fitting = [(f, s) for f, s in sized if s <= cap]
    if not fitting:
        return None
    # Prefer combined A+V, then MP4, then largest size.
    combined = [(f, s) for f, s in fitting if _has_av(f)]
    pool = combined or fitting
    pool.sort(key=lambda fs: (fs[1], 1 if fs[0].get("ext") == "mp4" else 0))
    return str(pool[-1][0]["format_id"])


def download_video(url: str, tempdir: str, progress: dict, format_override: str | None = None) -> tuple[str, dict]:
    """Blocking: download with yt-dlp. Returns (filepath, info). Runs in thread."""
    opts = base_ydl_opts(tempdir, progress)
    if format_override:
        opts["format"] = f"{format_override}/{build_format_selector()}"
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if info is None:
            raise DownloadError("Download returned no data.")
        if info.get("_type") == "playlist":
            entries = [e for e in (info.get("entries") or []) if e]
            if not entries:
                raise DownloadError("Playlist contains no downloadable entries.")
            info = entries[0]
        filename = ydl.prepare_filename(info)
        # After merge, extension may become mp4.
        base, _ = os.path.splitext(filename)
        candidates = [filename, base + ".mp4", base + ".mkv", base + ".webm"]
        for c in candidates:
            if os.path.exists(c):
                return c, info
        # Fallback: pick the single newest media file in tempdir.
        media = []
        for ext in (".mp4", ".mkv", ".webm", ".mov", ".m4v"):
            media.extend(Path(tempdir).glob(f"*{ext}"))
        if media:
            newest = max(media, key=lambda p: p.stat().st_mtime)
            return str(newest), info
        raise DownloadError("Download finished but no video file was produced.")


def find_downloaded_file(tempdir: str) -> str | None:
    media = []
    for ext in (".mp4", ".mkv", ".webm", ".mov", ".m4v"):
        media.extend(Path(tempdir).glob(f"*{ext}"))
    if not media:
        return None
    return str(max(media, key=lambda p: p.stat().st_mtime))


class SplitError(Exception):
    """Raised when a video cannot be split into uploadable parts."""


def split_video(filepath: str, tempdir: str, duration_s: float | None) -> list[str]:
    """Blocking: split a video into <=SPLIT_PART_MB parts (stream copy).

    Returns sorted part paths. Runs in a thread. Raises SplitError.
    """
    if not shutil.which("ffmpeg"):
        raise SplitError("FFmpeg is required for splitting.")
    try:
        total = os.path.getsize(filepath)
    except OSError as exc:
        raise SplitError("Cannot read downloaded file.") from exc
    try:
        duration = float(duration_s) if duration_s else 0.0
    except (TypeError, ValueError):
        duration = 0.0
    if total <= 0 or duration <= 0:
        raise SplitError("Unknown duration; cannot split.")
    part_bytes = SPLIT_PART_MB * 1024 * 1024
    n_parts = max(2, math.ceil(total / part_bytes))
    if n_parts > SPLIT_MAX_PARTS:
        raise SplitError(
            f"Needs {n_parts} parts (limit {SPLIT_MAX_PARTS}). "
            "Use a local Bot API server for very large files."
        )
    seg_time = max(10.0, duration / n_parts * 0.98)
    outdir = tempfile.mkdtemp(prefix="parts_", dir=tempdir)
    pattern = os.path.join(outdir, "part_%03d.mp4")
    cmd = [
        "ffmpeg", "-y", "-i", filepath,
        "-c", "copy", "-map", "0",
        "-f", "segment", "-segment_time", f"{seg_time:.2f}",
        "-reset_timestamps", "1", pattern,
    ]
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=600
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SplitError("Video splitting timed out or failed.") from exc
    if proc.returncode != 0:
        raise SplitError("Video splitting failed.")
    parts = sorted(
        (str(p) for p in Path(outdir).glob("part_*.mp4") if p.stat().st_size > 0),
    )
    if not parts:
        raise SplitError("Video splitting produced no parts.")
    for p in parts:
        if os.path.getsize(p) > CLOUD_UPLOAD_LIMIT:
            raise SplitError(
                "A part still exceeds Telegram's limit. "
                "Use a local Bot API server for this file."
            )
    return parts


def map_download_error(exc: BaseException) -> str:
    msg = str(exc).lower()
    if isinstance(exc, UnsupportedError) or "unsupported url" in msg:
        return "This website is not currently supported."
    if isinstance(exc, ExtractorError):
        return "Unable to download this video. The website extractor failed. Please check the URL and try again."
    text = str(exc)
    low = text.lower()

    def _has(*words: str) -> bool:
        # Word-boundary match: avoids false hits like "age" inside "webpage".
        return any(re.search(rf"\b{re.escape(w)}\b", low) for w in words)

    if "403" in low or _has("forbidden"):
        return ("The website refused this download (HTTP 403). The site may be blocking "
                "automated/server traffic. Please try again later or try another link.")
    if _has("private", "login", "log in", "sign in", "cookies", "cookie", "age", "paywall", "drm", "geo", "region", "blocked", "unavailable"):
        return "This video is unavailable or requires access that this bot cannot provide."
    if _has("removed", "deleted", "not found", "404", "no video", "unable to extract"):
        return "Unable to download this video. Please check the URL and try again."
    if "timeout" in low or _has("timed out"):
        return "The download timed out. Please try again later."
    if "ffmpeg" in low:
        return "Video processing failed (FFmpeg error). Please try another video."
    if "no video file was produced" in low or "no compatible format" in low or "requested format not available" in low:
        return "No compatible video format is available within the size limit."
    return "Unable to download this video. Please check the URL and try again."


async def safe_edit(message, text: str) -> None:
    try:
        await message.edit_text(text)
    except Exception:  # noqa: BLE001 - message may be unchanged/deleted
        pass


def cleanup_dir(path: str | None) -> None:
    if not path:
        return
    try:
        # Safety: only delete inside base download dir or system temp.
        p = Path(path)
        base = BASE_DOWNLOAD_DIR
        is_inside_base = base in p.parents or p == base
        is_sys_tmp = str(p).startswith(tempfile.gettempdir())
        if not (is_inside_base or is_sys_tmp):
            logger.warning("Refusing to delete path outside temp dirs.")
            return
        shutil.rmtree(path, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Cleanup failed for %s: %s", path, type(exc).__name__)


# ---------------------------------------------------------------------------
# Download pipeline
# ---------------------------------------------------------------------------

async def run_download_job(
    url: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    status_msg,
    user_id: int,
    tempdir: str,
) -> None:
    progress: dict = {"stage": "extracting", "downloaded": 0, "total": 0}

    async def poll_progress() -> None:
        while True:
            await asyncio.sleep(4)
            try:
                if progress.get("stage") == "downloading":
                    dl = progress.get("downloaded") or 0
                    total = progress.get("total") or 0
                    if total:
                        pct = min(100, int(dl * 100 / total))
                        await safe_edit(
                            status_msg,
                            f"⬇️ Downloading video… {pct}% ({format_bytes(dl)} / {format_bytes(total)})",
                        )
                    else:
                        await safe_edit(
                            status_msg,
                            f"⬇️ Downloading video… ({format_bytes(dl)} downloaded)",
                        )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                pass

    poller = asyncio.create_task(poll_progress())
    try:
        user_status[user_id] = "extracting metadata"
        await safe_edit(status_msg, "🔍 Extracting metadata…")

        try:
            info = await asyncio.to_thread(extract_metadata, url)
        except AssertionError as exc:  # noqa: BLE001 - usually a site/extractor layout change
            logger.warning("Metadata assertion failed for user %d: %r", user_id, exc, exc_info=True)
            await safe_edit(
                status_msg,
                "❌ This video's page layout isn't readable right now (site may have changed). "
                "Please try again later or try another link.",
            )
            user_status[user_id] = "failed"
            return
        except Exception as exc:  # noqa: BLE001
            logger.info("Metadata extraction failed for user %d: %s: %s", user_id, type(exc).__name__, str(exc)[:300])
            await safe_edit(status_msg, f"❌ {map_download_error(exc)}")
            user_status[user_id] = "failed"
            return

        title = info.get("title") or "video"
        duration = format_duration(info.get("duration"))
        logger.info("Metadata ok for user %d: title=%.60s duration=%s", user_id, title, duration)

        fits, smallest = has_format_within_limit(info)
        if not fits:
            # On the cloud API the effective cap is ~50 MB, but files up to
            # MAX_FILE_MB can still be delivered by splitting into parts.
            if TELEGRAM_API_URL or not smallest or smallest > MAX_BYTES:
                await safe_edit(
                    status_msg,
                    f"❌ The video exceeds the configured file-size limit "
                    f"({EFFECTIVE_MAX_MB} MB; smallest known format ~{format_bytes(smallest)}).",
                )
                user_status[user_id] = "failed: too large"
                return
            logger.info(
                "Video exceeds cloud cap for user %d (~%s); will split after download",
                user_id, format_bytes(smallest),
            )

        # Pin an explicit fitting rendition when possible. Without this,
        # yt-dlp's fallback chain can grab an over-limit format when sizes
        # are unknown, wasting a full download that the post-check rejects.
        picked = pick_format_string(info, EFFECTIVE_MAX_BYTES)
        if picked:
            logger.info("Pinned format %s for user %d (cap %d MB)", picked, user_id, EFFECTIVE_MAX_MB)

        if not FFMPEG_AVAILABLE:
            logger.warning("FFmpeg missing; attempting download anyway (merge may fail).")

        user_status[user_id] = "downloading"
        progress["stage"] = "downloading"
        await safe_edit(status_msg, f"⬇️ Downloading video…\n🎬 {title[:100]}\n⏱ {duration}")

        try:
            async with download_semaphore:
                filepath, info2 = await asyncio.to_thread(download_video, url, tempdir, progress, picked)
                if info2.get("title"):
                    title = info2["title"]
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.info("Download failed for user %d: %s: %s", user_id, type(exc).__name__, str(exc)[:300])
            await safe_edit(status_msg, f"❌ {map_download_error(exc)}")
            user_status[user_id] = "failed"
            cleanup_dir(tempdir)
            async with tasks_lock:
                user_tempdir.pop(user_id, None)
            return

        # Verify file exists and enforce size limit. Never claim success on failure.
        try:
            size = os.path.getsize(filepath)
        except OSError:
            await safe_edit(status_msg, "❌ Unable to download this video. Please check the URL and try again.")
            user_status[user_id] = "failed"
            cleanup_dir(tempdir)
            async with tasks_lock:
                user_tempdir.pop(user_id, None)
            return

        split_parts: list[str] = []
        if size > EFFECTIVE_MAX_BYTES and (TELEGRAM_API_URL or size > MAX_BYTES):
            logger.info("File too large for user %d: %d bytes > limit %d", user_id, size, EFFECTIVE_MAX_BYTES)
            await safe_edit(
                status_msg,
                f"❌ The video exceeds the configured file-size limit "
                f"({format_bytes(size)} > {EFFECTIVE_MAX_MB} MB).",
            )
            user_status[user_id] = "failed: too large"
            cleanup_dir(tempdir)
            async with tasks_lock:
                user_tempdir.pop(user_id, None)
            return

        if size == 0:
            await safe_edit(status_msg, "❌ Unable to download this video. Please check the URL and try again.")
            user_status[user_id] = "failed"
            cleanup_dir(tempdir)
            async with tasks_lock:
                user_tempdir.pop(user_id, None)
            return

        # Direct single-file upload via MTProto (up to ~2 GB), preferred over
        # splitting whenever the cloud Bot API cap would otherwise apply.
        if (size > CLOUD_UPLOAD_LIMIT and not TELEGRAM_API_URL
                and size <= MAX_BYTES and MTPROTO_ENABLED):
            user_status[user_id] = "uploading"
            await safe_edit(
                status_msg,
                f"📤 Uploading large file directly ({format_bytes(size)})…\n🎬 {title[:100]}",
            )
            try:
                client = await get_mtproto_client()
                if client is None:
                    raise RuntimeError("MTProto unavailable")
                caption = f"🎬 {title[:900]}\n⏱ {duration} | 📦 {format_bytes(size)}"
                await client.send_file(
                    update.effective_chat.id, filepath,
                    caption=caption, supports_streaming=True, force_document=False,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "MTProto upload failed for user %d (%s: %s); falling back to split",
                    user_id, type(exc).__name__, str(exc)[:200],
                )
            else:
                user_status[user_id] = "completed"
                await safe_edit(status_msg, "✅ Download completed.")
                cleanup_dir(tempdir)
                async with tasks_lock:
                    user_tempdir.pop(user_id, None)
                return

        # Cloud API without MTProto: split files within MAX_FILE_MB into parts.
        if (not split_parts and size > CLOUD_UPLOAD_LIMIT and not TELEGRAM_API_URL
                and size <= MAX_BYTES):
            dur_s = info2.get("duration") or info.get("duration")
            user_status[user_id] = "splitting"
            await safe_edit(
                status_msg,
                f"✂️ Video is {format_bytes(size)} — splitting into parts for Telegram…",
            )
            try:
                split_parts = await asyncio.to_thread(split_video, filepath, tempdir, dur_s)
            except SplitError as exc:
                logger.info("Split failed for user %d: %s", user_id, exc)
                await safe_edit(status_msg, f"❌ {exc}")
                user_status[user_id] = "failed: split"
                cleanup_dir(tempdir)
                async with tasks_lock:
                    user_tempdir.pop(user_id, None)
                return
            logger.info("Split %s into %d parts for user %d", format_bytes(size), len(split_parts), user_id)

        user_status[user_id] = "uploading"
        await safe_edit(status_msg, f"📤 Uploading to Telegram… ({format_bytes(size)})\n🎬 {title[:100]}")

        # Upload without loading whole file into RAM (stream from disk).
        try:
            if split_parts:
                total_parts = len(split_parts)
                for idx, part in enumerate(split_parts, start=1):
                    await safe_edit(
                        status_msg,
                        f"📤 Uploading part {idx}/{total_parts}…\n🎬 {title[:100]}",
                    )
                    caption = f"🎬 {title[:800]}\n⏱ {duration} | 📦 part {idx}/{total_parts}"
                    with open(part, "rb") as fh:
                        await context.bot.send_video(
                            chat_id=update.effective_chat.id,
                            video=fh,
                            caption=caption,
                            supports_streaming=True,
                            read_timeout=UPLOAD_TIMEOUT,
                            write_timeout=UPLOAD_TIMEOUT,
                            connect_timeout=30,
                        )
                    if idx < total_parts:
                        await asyncio.sleep(2)  # gentle pace: avoid flood limits
            else:
                caption = f"🎬 {title[:900]}\n⏱ {duration} | 📦 {format_bytes(size)}"
                with open(filepath, "rb") as fh:
                    await context.bot.send_video(
                        chat_id=update.effective_chat.id,
                        video=fh,
                        caption=caption,
                        supports_streaming=True,
                        read_timeout=UPLOAD_TIMEOUT,
                        write_timeout=UPLOAD_TIMEOUT,
                        connect_timeout=30,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Telegram upload failed for user %d: %s: %s", user_id, type(exc).__name__, str(exc)[:300])
            if size > CLOUD_UPLOAD_LIMIT and not TELEGRAM_API_URL:
                await safe_edit(
                    status_msg,
                    f"❌ Downloaded ({format_bytes(size)}), but Telegram's cloud API rejects "
                    f"bot uploads over ~50 MB. The admin needs a local Bot API server "
                    f"(TELEGRAM_API_URL) for larger files.",
                )
            else:
                await safe_edit(
                    status_msg,
                    "❌ The download completed, but Telegram could not upload the file.",
                )
            user_status[user_id] = "failed: upload"
            cleanup_dir(tempdir)
            async with tasks_lock:
                user_tempdir.pop(user_id, None)
            return

        user_status[user_id] = "completed"
        if split_parts:
            await safe_edit(status_msg, f"✅ Download completed ({len(split_parts)} parts).")
        else:
            await safe_edit(status_msg, "✅ Download completed.")
        cleanup_dir(tempdir)
        async with tasks_lock:
            user_tempdir.pop(user_id, None)
    except asyncio.CancelledError:
        logger.info("Download cancelled for user %d", user_id)
        await safe_edit(status_msg, "🛑 Download cancelled and temporary files removed.")
        user_status[user_id] = "cancelled"
        cleanup_dir(tempdir)
        async with tasks_lock:
            user_tempdir.pop(user_id, None)
        raise
    finally:
        poller.cancel()
        try:
            await poller
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            pass
        async with tasks_lock:
            if active_tasks.get(user_id) is asyncio.current_task():
                active_tasks.pop(user_id, None)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

WELCOME = (
    "👋 Welcome to *Universal Video Downloader Bot*!\n\n"
    "Send me a public video URL and I will download it and send the video back here.\n\n"
    "Commands:\n"
    "/start – show this message\n"
    "/help – how to use\n"
    "/about – features and limits\n"
    "/cancel – cancel the active download\n"
    "/status – show your download status\n\n"
    f"Max file size: {EFFECTIVE_MAX_MB} MB."
)

HELP_TEXT = (
    "❓ *How to use*\n\n"
    "1. Copy a public video URL from a yt-dlp-supported site.\n"
    "2. Paste it here as a message.\n"
    "3. Wait for: checking → extracting → downloading → processing → uploading.\n\n"
    "Notes:\n"
    "• Only public videos. Private, login-only, DRM or paywalled videos are not supported.\n"
    "• Very large videos may exceed the size limit and will be rejected.\n"
    "• One active download per user. Use /cancel to stop."
)

ABOUT_TEXT = (
    "*Universal Video Downloader Bot*\n\n"
    "• Downloads publicly accessible videos via yt-dlp.\n"
    "• Supports many public video-sharing sites supported by yt-dlp, including supported adult platforms.\n"
    "• Picks the best MP4 format within the size limit and merges with FFmpeg when needed.\n\n"
    "Limitations:\n"
    f"• Max deliverable size: {EFFECTIVE_MAX_MB} MB (cloud API caps bot uploads at ~50 MB; a local Bot API server unlocks the full {MAX_FILE_MB} MB).\n"
    "• No DRM, paywall, login or private-content bypass.\n"
    "• Website support depends on yt-dlp extractors; not every URL will work."
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME, parse_mode=ParseMode.MARKDOWN)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)


async def cmd_about(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(ABOUT_TEXT, parse_mode=ParseMode.MARKDOWN)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    async with tasks_lock:
        active = user_id in active_tasks
    status = user_status.get(user_id, "idle (no downloads yet)")
    if active:
        await update.message.reply_text(f"⏳ Active: {status}")
    else:
        await update.message.reply_text(f"ℹ️ Status: {status}")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    async with tasks_lock:
        task = active_tasks.get(user_id)
        tmp = user_tempdir.get(user_id)
    if not task or task.done():
        await update.message.reply_text("ℹ️ No active download to cancel.")
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001
        pass
    cleanup_dir(tmp)
    await update.message.reply_text("🛑 Download cancelled and temporary files removed.")


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or not update.message.text:
        return
    user_id = update.effective_user.id
    url = update.message.text.strip().split()[0]

    async with tasks_lock:
        existing = active_tasks.get(user_id)
        if existing and not existing.done():
            await update.message.reply_text("⏳ You already have an active download. Send /cancel to stop it first.")
            return

    ok, reason = is_valid_public_url(url)
    if not ok:
        if reason == "unsafe_scheme":
            await update.message.reply_text("Please send a valid public video URL (http/https only).")
        else:
            await update.message.reply_text("Please send a valid public video URL.")
        return

    canonical = canonicalize_url(url)
    if canonical != url:
        logger.info("Rewrote mirror URL host for user %d", user_id)
        url = canonical

    status_msg = await update.message.reply_text("🔗 URL received. Checking URL…")

    try:
        tempdir = tempfile.mkdtemp(prefix="uvd_", dir=str(BASE_DOWNLOAD_DIR))
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to create tempdir: %s", type(exc).__name__)
        await safe_edit(status_msg, "❌ Unable to download this video. Please check the URL and try again.")
        return

    task = asyncio.create_task(
        run_download_job(url, update, context, status_msg, user_id, tempdir)
    )
    async with tasks_lock:
        active_tasks[user_id] = task
        user_tempdir[user_id] = tempdir
        user_status[user_id] = "queued"
    try:
        await task
    except asyncio.CancelledError:
        pass  # run_download_job already updated the message
    finally:
        async with tasks_lock:
            if active_tasks.get(user_id) is task:
                active_tasks.pop(user_id, None)


async def handle_unknown_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Non-URL text: guide the user.
    if update.message and update.message.text and not update.message.text.startswith("/"):
        await update.message.reply_text("Please send a valid public video URL. See /help for details.")


def build_app() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is not set.")
    builder = Application.builder().token(BOT_TOKEN)
    if TELEGRAM_API_URL:
        base = TELEGRAM_API_URL.rstrip("/")
        builder = builder.base_url(f"{base}/bot").base_file_url(f"{base}/file/bot")
    app = builder.build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("about", cmd_about))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"https?://"), handle_url))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_unknown_text))
    return app


def main() -> None:
    if not BOT_TOKEN:
        print("ERROR: BOT_TOKEN environment variable is not set.")
        print("Set BOT_TOKEN in Railway Variables, or copy .env.example to .env locally.")
        raise SystemExit(1)
    logger.info(
        "Starting bot | max_file=%d MB | effective_max=%d MB | download_dir=%s | max_concurrent=%d",
        MAX_FILE_MB, EFFECTIVE_MAX_MB, DOWNLOAD_DIR, MAX_CONCURRENT_DOWNLOADS,
    )
    try:
        logger.info("yt-dlp version: %s", yt_dlp.version.__version__)
    except Exception:  # noqa: BLE001
        pass
    logger.info(
        "Note: changing MAX_FILE_MB does not override Telegram Bot API upload limits."
    )
    if MAX_FILE_MB > 50 and not TELEGRAM_API_URL:
        logger.warning(
            "MAX_FILE_MB=%d MB exceeds the default Bot API ~50 MB upload limit. "
            "Uploads above ~50 MB will fail unless TELEGRAM_API_URL points to a "
            "self-hosted local Bot API server.",
            MAX_FILE_MB,
        )
    if TELEGRAM_API_URL:
        logger.info("Using custom Telegram Bot API server (value hidden).")
    app = build_app()
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
