"""URL validation and SSRF protection.

Every URL the bot fetches on its own behalf (playlists, segments and direct
media files) must pass :func:`ensure_public_url`. That check covers:

* scheme allow-listing (http/https only),
* rejection of userinfo (``user:pass@host``) which is a common parser-confusion
  vector,
* blocked hostnames (localhost, *.local, *.internal, cloud metadata names),
* numeric/ambiguous hostnames that bypass DNS (e.g. ``2130706433``),
* DNS resolution where *every* resolved address must be public
  (private/loopback/link-local/reserved/multicast/unspecified and known
  cloud-metadata ranges are rejected),
* an optional host allow-list (``ALLOWED_HOSTS``).
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import time
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit, urlunsplit

from .config import MAX_URL_LENGTH

_RESOLUTION_CACHE: dict[str, tuple[float, list]] = {}
_RESOLUTION_TTL = 300.0

_BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "metadata",
    "metadata.google.internal",
    "metadata.goog",
    "instance-data",
}
_BLOCKED_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".localdomain",
    ".internal",
    ".lan",
    ".home.arpa",
    ".in-addr.arpa",
    ".ip6.arpa",
)
_METADATA_IPS = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),
        ipaddress.ip_address("fd00:ec2::254"),
        ipaddress.ip_address("100.100.100.200"),
    }
)
_HOSTNAME_RE = re.compile(r"^[a-z0-9]([a-z0-9._-]*[a-z0-9])?$", re.IGNORECASE)
_SAFE_FILENAME_RE = re.compile(r"[^\w.\- ]", re.UNICODE)


class URLGuardError(ValueError):
    """Raised when a URL fails validation. ``reason`` is machine-readable."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class ParsedURL:
    url: str
    scheme: str
    host: str
    port: int | None
    path: str
    query: str


def parse_http_url(url: str, max_length: int = MAX_URL_LENGTH) -> ParsedURL:
    raw = (url or "").strip()
    if not raw:
        raise URLGuardError("empty", "URL is empty.")
    if len(raw) > max_length:
        raise URLGuardError("too_long", f"URL exceeds {max_length} characters.")
    try:
        parts: SplitResult = urlsplit(raw)
    except ValueError as exc:
        raise URLGuardError("invalid", f"Unparseable URL: {exc}") from exc
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise URLGuardError("unsafe_scheme", "Only http/https URLs are supported.")
    if parts.username or parts.password:
        raise URLGuardError("userinfo", "URLs containing credentials are not accepted.")
    host = (parts.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise URLGuardError("invalid", "URL has no hostname.")
    try:
        port = parts.port
    except ValueError as exc:
        raise URLGuardError("invalid", f"Invalid port: {exc}") from exc
    return ParsedURL(url=raw, scheme=scheme, host=host, port=port, path=parts.path, query=parts.query)


def host_is_blocked(host: str) -> bool:
    if not host:
        return True
    host = host.strip().lower().rstrip(".")
    if host in _BLOCKED_HOSTNAMES:
        return True
    if host.endswith(_BLOCKED_HOST_SUFFIXES):
        return True
    if host.isdigit():
        return True
    if not _HOSTNAME_RE.match(host):
        return True
    return False


def ip_is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip in _METADATA_IPS:
        return True
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or not ip.is_global
    )


def host_allowed(host: str, allowed_hosts: frozenset[str] | set[str] | tuple[str, ...]) -> bool:
    if not allowed_hosts:
        return True
    host = host.strip().lower().rstrip(".")
    for entry in allowed_hosts:
        entry = entry.strip().lower().rstrip(".")
        if host == entry or host.endswith("." + entry):
            return True
    return False


def resolve_host_ips(
    host: str, port: int | None = None
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve every address for ``host``. Raises URLGuardError on failure."""
    try:
        infos = socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        raise URLGuardError("dns_error", f"Could not resolve host: {exc}") from exc
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    seen: set[str] = set()
    for info in infos:
        raw_ip = info[4][0]
        if raw_ip in seen:
            continue
        seen.add(raw_ip)
        try:
            addresses.append(ipaddress.ip_address(raw_ip))
        except ValueError:
            continue
    if not addresses:
        raise URLGuardError("dns_error", "Host resolved to no usable address.")
    return addresses


async def resolve_host_ips_async(
    host: str, port: int | None = None
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    return await asyncio.to_thread(resolve_host_ips, host, port)


def invalidate_resolution_cache() -> None:
    _RESOLUTION_CACHE.clear()


async def _resolve_cached(
    host: str, port: int | None = None
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    key = f"{host}|{port or ''}"
    now = time.monotonic()
    cached = _RESOLUTION_CACHE.get(key)
    if cached and now - cached[0] < _RESOLUTION_TTL:
        return cached[1]
    addresses = await resolve_host_ips_async(host, port)
    _RESOLUTION_CACHE[key] = (now, addresses)
    return addresses


async def ensure_public_url(
    url: str,
    allowed_hosts: frozenset[str] | set[str] | tuple[str, ...] = frozenset(),
    *,
    resolve: bool = True,
    max_length: int = MAX_URL_LENGTH,
) -> str:
    """Validate ``url`` and return its normalized form, or raise URLGuardError."""
    parsed = parse_http_url(url, max_length=max_length)
    if host_is_blocked(parsed.host):
        raise URLGuardError("blocked_host", "This host is not reachable from the bot.")
    if not host_allowed(parsed.host, allowed_hosts):
        raise URLGuardError("host_not_allowed", "This host is not on the allow-list.")
    if resolve:
        addresses = await _resolve_cached(parsed.host, parsed.port)
        for address in addresses:
            if ip_is_blocked(address):
                raise URLGuardError(
                    "blocked_address",
                    "This host resolves to a private or reserved address.",
                )
    netloc = parsed.host
    if parsed.port:
        netloc = f"{parsed.host}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, ""))


def redact_url(url: str) -> str:
    """Strip query string and userinfo so URLs can be logged safely."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<unparseable-url>"
    host = parts.hostname or ""
    netloc = host
    try:
        if parts.port:
            netloc = f"{host}:{parts.port}"
    except ValueError:
        pass
    path = parts.path
    if len(path) > 80:
        path = path[:77] + "..."
    return urlunsplit((parts.scheme, netloc, path, "", ""))


def safe_filename(name: str, max_length: int = 120, fallback: str = "video") -> str:
    """Sanitize a title into a filesystem-safe base name (extension preserved)."""
    cleaned = (name or "").strip().replace("\x00", "")
    cleaned = cleaned.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = _SAFE_FILENAME_RE.sub("_", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ._-")
    if not cleaned or cleaned in (".", ".."):
        cleaned = fallback
    root, dot, ext = cleaned.rpartition(".")
    if dot and ext and len(ext) <= 6:
        keep = max(1, max_length - len(ext) - 1)
        cleaned = f"{root[:keep]}.{ext}"
    else:
        cleaned = cleaned[:max_length]
    return cleaned or fallback


def extract_first_url(text: str) -> str | None:
    """Return the first http(s) URL inside a chat message, trailing punctuation removed."""
    match = re.search(r"https?://\S+", text or "", re.IGNORECASE)
    if not match:
        return None
    candidate = match.group(0)
    while candidate and candidate[-1] in ").,;:'\"<>]":
        candidate = candidate[:-1]
    return candidate or None
