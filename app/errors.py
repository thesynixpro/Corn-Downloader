"""Mapping of internal exceptions to user-facing messages.

Rules:
* never leak stack traces, URLs with credentials, or tokens to chat,
* distinguish "requires access we cannot provide" (login, paywall, DRM, geo)
  from transient failures,
* keep the technical detail available via :func:`short_detail` for diagnosis.
"""

from __future__ import annotations

import re

AUTH_MARKERS = (
    "login",
    "log in",
    "sign in",
    "sign-in",
    "cookies",
    "cookie",
    "authentication",
    "account",
    "private video",
    "private",
    "members-only",
    "members only",
    "age",
    "age-restricted",
    "confirm you're not a bot",
    "confirm you are not a bot",
    "bot check",
    "paywall",
    "subscription",
    "premium",
)
DRM_MARKERS = ("drm", "widevine", "fairplay", "playready", "sample-aes", "license")
GEO_MARKERS = ("not available in your country", "geo", "region", "geoblock", "geo-restricted")
EXPIRED_MARKERS = ("expired", "token expired", "url expired", "playlist expired", "signature")
REMOVED_MARKERS = ("removed", "deleted", "not found", "no video", "unable to extract", "404")


def _contains(text: str, markers: tuple[str, ...]) -> bool:
    low = text.lower()
    for marker in markers:
        if " " in marker or "-" in marker or "'" in marker:
            if marker in low:
                return True
        elif re.search(rf"\b{re.escape(marker)}\b", low):
            return True
    return False


def is_auth_required(exc: BaseException) -> bool:
    return _contains(str(exc), AUTH_MARKERS)


def is_drm_error(exc: BaseException) -> bool:
    return _contains(str(exc), DRM_MARKERS)


def short_detail(exc: BaseException, limit: int = 160) -> str:
    """One-line technical reason appended to user-facing failure messages."""
    lines = str(exc).strip().splitlines()
    line = lines[-1].strip() if lines else ""
    line = re.sub(r"(https?://)[^\s/]+@", r"\1", line)
    if len(line) > limit:
        line = line[: limit - 1].rstrip() + "…"
    return line or type(exc).__name__


def map_download_error(exc: BaseException) -> str:
    """Translate an extraction/download failure into an actionable message."""
    text = str(exc)
    low = text.lower()
    name = type(exc).__name__.lower()

    if is_drm_error(exc):
        return (
            "This stream is DRM-protected. The bot does not bypass DRM, so this "
            "video cannot be downloaded."
        )
    if is_auth_required(exc):
        return (
            "This video requires a login, subscription, or age verification that "
            "the bot cannot and will not bypass. Only publicly accessible videos "
            "are supported."
        )
    if _contains(text, GEO_MARKERS):
        return "This video is geo-restricted and is not available from the bot's network."
    if _contains(text, EXPIRED_MARKERS) and ("m3u8" in low or "hls" in low or "playlist" in low):
        return (
            "The stream playlist has expired or its signature is no longer valid. "
            "Fetch a fresh link from the source page and try again."
        )
    if "unsupported url" in low or "no suitable" in low or "unsupported" in name:
        return "This website is not currently supported."
    if "403" in low or _contains(text, ("forbidden",)):
        return (
            "The source refused the request (HTTP 403). The site may be blocking "
            "automated traffic. Try again later or use another link."
        )
    if "410" in low and ("gone" in low or "expired" in low):
        return "The media link has expired (HTTP 410). Please request a fresh link."
    if "timeout" in low or "timed out" in low:
        return "The download timed out. Please try again later."
    if "ffmpeg" in low:
        return "Video processing failed (FFmpeg error). Please try another video."
    if "no video" in low or "no compatible format" in low or "requested format not available" in low:
        return "No compatible video format is available within the size limit."
    if _contains(text, REMOVED_MARKERS):
        return "Unable to download this video. Please check the URL and try again."
    return "Unable to download this video. Please check the URL and try again."
