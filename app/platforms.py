"""Platform identification and canonical host normalization.

This module is intentionally data-driven and free of network calls: it maps a
submitted URL's host to a friendly platform label so the bot can explain what
it is dealing with and pick an extraction strategy hint.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Platform:
    key: str
    label: str
    hosts: tuple[str, ...] = ()
    requires_auth: bool = False
    notes: str = ""


PLATFORMS: tuple[Platform, ...] = (
    Platform("youtube", "YouTube", ("youtube.com", "youtu.be", "youtube-nocookie.com"), notes="Bot checks may require sign-in; login flows are not bypassed."),
    Platform("vimeo", "Vimeo", ("vimeo.com",)),
    Platform("dailymotion", "Dailymotion", ("dailymotion.com", "dai.ly")),
    Platform("twitter", "X / Twitter", ("x.com", "twitter.com", "t.co")),
    Platform("tiktok", "TikTok", ("tiktok.com",)),
    Platform("instagram", "Instagram", ("instagram.com",), requires_auth=True),
    Platform("facebook", "Facebook", ("facebook.com", "fb.watch"), requires_auth=True),
    Platform("reddit", "Reddit", ("reddit.com", "redd.it")),
    Platform("twitch", "Twitch", ("twitch.tv",)),
    Platform("xhamster", "xHamster", ("xhamster.com",)),
    Platform("pornhub", "Pornhub", ("pornhub.com",)),
    Platform("xvideos", "XVideos", ("xvideos.com",)),
    Platform("xnxx", "XNXX", ("xnxx.com",)),
    Platform("redtube", "RedTube", ("redtube.com",)),
    Platform("youporn", "YouPorn", ("youporn.com",)),
    Platform("soundcloud", "SoundCloud", ("soundcloud.com",)),
)

GENERIC = Platform("generic", "Website")
HLS = Platform("hls", "HLS stream")
DIRECT = Platform("direct", "Direct video link")

_MIRROR_RE = re.compile(r"^(?:www\.)?xhamster\d*\.", re.IGNORECASE)


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _host_matches(host: str, candidate: str) -> bool:
    return host == candidate or host.endswith("." + candidate)


def detect_platform(url_or_host: str) -> Platform:
    """Identify the platform for a URL or bare hostname."""
    host = host_of(url_or_host) if "://" in url_or_host else (url_or_host or "").lower().rstrip(".")
    if host:
        for platform in PLATFORMS:
            if any(_host_matches(host, candidate) for candidate in platform.hosts):
                return platform
        return GENERIC
    lowered = (url_or_host or "").lower()
    if ".m3u8" in lowered:
        return HLS
    if re.search(r"\.(mp4|m4v|webm|mov|mkv)(?:$|[?#])", lowered):
        return DIRECT
    return GENERIC


def is_probable_hls(url: str) -> bool:
    try:
        path = urlsplit(url).path.lower()
    except ValueError:
        return False
    return path.endswith(".m3u8") or path.endswith(".m3u")


def is_probable_direct_media(url: str) -> bool:
    try:
        path = urlsplit(url).path.lower()
    except ValueError:
        return False
    return path.endswith((".mp4", ".m4v", ".webm", ".mov", ".mkv", ".ts"))


def canonicalize_url(url: str) -> str:
    """Rewrite known mirror domains to the canonical host of the same site.

    Example: ``xhamster46.desi/videos/...`` -> ``xhamster.com/videos/...``
    The path is untouched; only the misleading host is normalized so yt-dlp's
    extractor matching works.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    host = (parts.hostname or "").lower()
    if _MIRROR_RE.match(host):
        try:
            return parts._replace(netloc="xhamster.com").geturl()
        except ValueError:
            return url
    return url
