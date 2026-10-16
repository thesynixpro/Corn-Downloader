"""Environment-driven configuration.

All settings are read once at startup via :func:`load_settings`. Nothing in the
application reads ``os.environ`` after that, which keeps modules testable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

MAX_URL_LENGTH = 2048
CLOUD_UPLOAD_LIMIT = 50 * 1024 * 1024
VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    try:
        load_dotenv(override=False)
    except Exception:
        pass


def _env_str(name: str, default: str = "") -> str:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip()


def _env_int(name: str, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    raw = os.environ.get(name)
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _env_hosts(name: str) -> frozenset[str]:
    raw = os.environ.get(name, "")
    return frozenset(
        h.strip().lower().rstrip(".") for h in raw.split(",") if h.strip()
    )


def normalize_api_url(raw: str) -> str:
    """Normalize a self-hosted Bot API base URL; '' when unusable."""
    url = (raw or "").strip().rstrip("/")
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    scheme, sep, rest = url.partition("://")
    if scheme.lower() not in ("http", "https") or not sep or not rest:
        return ""
    host = rest.split("/", 1)[0]
    if not host:
        return ""
    return url


@dataclass
class Settings:
    bot_token: str = ""
    max_file_mb: int = 700
    download_dir: Path = field(default_factory=lambda: Path("/tmp/video_downloads"))
    max_concurrent_downloads: int = 3
    request_timeout: int = 120
    connect_timeout: int = 30
    download_timeout: int = 3600
    telegram_api_url: str = ""
    api_id: int = 0
    api_hash: str = ""
    log_level: str = "INFO"
    split_part_mb: int = 45
    split_max_parts: int = 15
    max_height: int = 2160
    allowed_hosts: frozenset[str] = frozenset()
    max_temp_disk_mb: int = 5000
    hls_segment_concurrency: int = 4
    hls_referer: str = ""
    hls_user_agent: str = DEFAULT_USER_AGENT
    data_dir: Path = field(default_factory=lambda: Path("/tmp/video_downloads/bot_data"))
    support_chat_id: int | None = None

    @property
    def max_bytes(self) -> int:
        return self.max_file_mb * 1024 * 1024

    @property
    def mtproto_enabled(self) -> bool:
        return bool(self.api_id and self.api_hash)

    @property
    def effective_max_mb(self) -> int:
        if self.telegram_api_url or self.mtproto_enabled:
            return self.max_file_mb
        return min(self.max_file_mb, 50)

    @property
    def effective_max_bytes(self) -> int:
        return self.effective_max_mb * 1024 * 1024

    @property
    def upload_timeout(self) -> int:
        return max(self.request_timeout, 600)

    @property
    def max_temp_disk_bytes(self) -> int:
        return self.max_temp_disk_mb * 1024 * 1024


def _env_chat_id(name: str) -> int | None:
    raw = _env_str(name)
    if not raw:
        return None
    normalized = raw[1:] if raw.startswith("-") else raw
    if not normalized.isdigit():
        return None
    try:
        return -int(normalized) if raw.startswith("-") else int(normalized)
    except ValueError:
        return None


def load_settings() -> Settings:
    _load_dotenv()
    download_dir = Path(_env_str("DOWNLOAD_DIR", "/tmp/video_downloads"))
    data_dir_raw = _env_str("DATA_DIR")
    data_dir = Path(data_dir_raw) if data_dir_raw else download_dir / "bot_data"
    log_level = _env_str("LOG_LEVEL", "INFO").upper()
    if log_level not in VALID_LOG_LEVELS:
        log_level = "INFO"
    api_id_raw = _env_str("API_ID")
    api_id = int(api_id_raw) if api_id_raw.isdigit() else 0
    split_part_mb = min(max(_env_int("SPLIT_PART_MB", 45), 20), 49)
    split_max_parts = min(max(_env_int("SPLIT_MAX_PARTS", 15), 2), 25)
    return Settings(
        bot_token=_env_str("BOT_TOKEN"),
        max_file_mb=_env_int("MAX_FILE_MB", 700, minimum=1, maximum=2000),
        download_dir=download_dir,
        data_dir=data_dir,
        support_chat_id=_env_chat_id("SUPPORT_CHAT_ID"),
        max_concurrent_downloads=_env_int("MAX_CONCURRENT_DOWNLOADS", 3, minimum=1, maximum=16),
        request_timeout=_env_int("REQUEST_TIMEOUT", 120, minimum=5, maximum=3600),
        connect_timeout=_env_int("CONNECT_TIMEOUT", 30, minimum=3, maximum=120),
        download_timeout=_env_int("DOWNLOAD_TIMEOUT", 3600, minimum=0, maximum=86400),
        telegram_api_url=normalize_api_url(_env_str("TELEGRAM_API_URL")),
        api_id=api_id,
        api_hash=_env_str("API_HASH"),
        log_level=log_level,
        split_part_mb=split_part_mb,
        split_max_parts=split_max_parts,
        max_height=_env_int("MAX_VIDEO_HEIGHT", 2160, minimum=0, maximum=4320),
        allowed_hosts=_env_hosts("ALLOWED_HOSTS"),
        max_temp_disk_mb=_env_int("MAX_TEMP_DISK_MB", 5000, minimum=100, maximum=100000),
        hls_segment_concurrency=_env_int("HLS_SEGMENT_CONCURRENCY", 4, minimum=1, maximum=16),
        hls_referer=_env_str("HLS_REFERER"),
        hls_user_agent=_env_str("HLS_USER_AGENT", DEFAULT_USER_AGENT) or DEFAULT_USER_AGENT,
    )
