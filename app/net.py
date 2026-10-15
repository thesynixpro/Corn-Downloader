"""HTTP fetching with per-redirect SSRF validation.

httpx is used without automatic redirect following: every hop is re-validated
through :func:`app.urlguard.ensure_public_url` before the next request is made.
"""

from __future__ import annotations

import logging
from urllib.parse import urljoin

import httpx

from .config import Settings
from .progress import Progress, RateMeter
from .urlguard import ensure_public_url

logger = logging.getLogger("uvd_bot.net")

CHUNK_SIZE = 256 * 1024


class NetworkError(Exception):
    """Raised for HTTP-level failures (non-2xx, too many redirects, timeouts)."""


class DownloadTooLargeError(NetworkError):
    """Raised when a response exceeds the configured maximum size."""


def _base_headers(settings: Settings, headers: dict[str, str] | None = None) -> dict[str, str]:
    merged: dict[str, str] = {"User-Agent": settings.hls_user_agent, "Accept": "*/*"}
    if settings.hls_referer:
        merged["Referer"] = settings.hls_referer
    if headers:
        for key, value in headers.items():
            if value:
                merged[key] = value
    return merged


def make_client(settings: Settings, headers: dict[str, str] | None = None) -> httpx.AsyncClient:
    timeout = httpx.Timeout(
        connect=float(settings.connect_timeout),
        read=float(settings.request_timeout),
        write=float(settings.request_timeout),
        pool=float(settings.connect_timeout),
    )
    limits = httpx.Limits(
        max_connections=max(8, settings.hls_segment_concurrency * 2),
        max_keepalive_connections=max(4, settings.hls_segment_concurrency),
    )
    return httpx.AsyncClient(
        timeout=timeout,
        limits=limits,
        follow_redirects=False,
        trust_env=False,
        headers=_base_headers(settings, headers),
    )


async def fetch_text(
    client: httpx.AsyncClient,
    url: str,
    settings: Settings,
    headers: dict[str, str] | None = None,
    *,
    max_bytes: int = 5 * 1024 * 1024,
    max_redirects: int = 5,
) -> tuple[str, str]:
    """Fetch a small text resource (playlist) with per-hop SSRF validation.

    Returns ``(text, final_url)``.
    """
    current = url
    request_headers = _base_headers(settings, headers)
    for hop in range(max_redirects + 1):
        current = await ensure_public_url(current, settings.allowed_hosts)
        async with client.stream("GET", current, headers=request_headers) as response:
            if 300 <= response.status_code < 400 and response.headers.get("location"):
                if hop >= max_redirects:
                    raise NetworkError("Too many redirects.")
                current = urljoin(str(response.url), response.headers["location"])
                continue
            if response.status_code >= 400:
                raise NetworkError(f"HTTP {response.status_code} while fetching playlist.")
            buffer = bytearray()
            async for chunk in response.aiter_bytes():
                buffer.extend(chunk)
                if len(buffer) > max_bytes:
                    raise NetworkError("Playlist response is unexpectedly large.")
            return buffer.decode("utf-8", errors="replace"), str(response.url)
    raise NetworkError("Too many redirects.")


async def stream_to_file(
    client: httpx.AsyncClient,
    url: str,
    settings: Settings,
    dest: str,
    *,
    headers: dict[str, str] | None = None,
    progress: Progress | None = None,
    max_bytes: int | None = None,
    max_redirects: int = 5,
) -> dict:
    """Stream a URL to a file with progress tracking and size enforcement.

    Returns metadata: ``{"total", "content_type", "final_url"}``.
    """
    current = url
    request_headers = _base_headers(settings, headers)
    meter = RateMeter()
    for hop in range(max_redirects + 1):
        current = await ensure_public_url(current, settings.allowed_hosts)
        async with client.stream("GET", current, headers=request_headers) as response:
            if 300 <= response.status_code < 400 and response.headers.get("location"):
                if hop >= max_redirects:
                    raise NetworkError("Too many redirects.")
                current = urljoin(str(response.url), response.headers["location"])
                continue
            if response.status_code >= 400:
                raise NetworkError(f"HTTP {response.status_code} while downloading media.")
            total = 0
            content_range = response.headers.get("content-range")
            if content_range and "/" in content_range:
                total = _safe_int(content_range.rsplit("/", 1)[-1])
            if not total:
                total = _safe_int(response.headers.get("content-length"))
            downloaded = 0
            if progress is not None:
                progress.stage = "downloading"
                progress.total = total
                progress.downloaded = 0
            with open(dest, "wb") as handle:
                async for chunk in response.aiter_bytes(CHUNK_SIZE):
                    if not chunk:
                        continue
                    downloaded += len(chunk)
                    if max_bytes and downloaded > max_bytes:
                        raise DownloadTooLargeError(
                            f"File exceeds the configured size limit ({max_bytes} bytes)."
                        )
                    handle.write(chunk)
                    if progress is not None:
                        speed, eta = meter.update(downloaded, total)
                        progress.downloaded = downloaded
                        progress.speed = speed
                        progress.eta = eta
            return {
                "total": total or downloaded,
                "content_type": response.headers.get("content-type", ""),
                "final_url": str(response.url),
            }
    raise NetworkError("Too many redirects.")


def _safe_int(value: str | None) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0
