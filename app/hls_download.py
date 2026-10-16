"""HLS download engine.

Takes a validated ``.m3u8`` URL, inspects the playlist hierarchy, selects the
best variant and downloads it:

* unencrypted streams: segments are fetched by this application (per-segment
  SSRF validation, bounded concurrency and retries) and losslessly remuxed
  with FFmpeg,
* AES-128 encrypted streams: handed to FFmpeg, which fetches the key from the
  playlist-declared URI (standard HLS, not DRM). All referenced hosts are
  SSRF-validated before FFmpeg starts,
* SAMPLE-AES / Widevine / other DRM: refused.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from dataclasses import dataclass

import httpx

from . import hls, media, net
from .config import Settings
from .hls import DRMProtectedError, MasterPlaylist, MediaPlaylist, PlaylistError, Variant
from .progress import Progress, RateMeter
from .selections import QualityUnavailableError
from .urlguard import ensure_public_url

logger = logging.getLogger("uvd_bot.hls")

MAX_SEGMENT_BYTES = 128 * 1024 * 1024
MAX_PLAYLIST_BYTES = 8 * 1024 * 1024


@dataclass
class HlsInspection:
    playlist: MediaPlaylist
    variant: Variant | None = None
    audio_playlist: MediaPlaylist | None = None
    audio_label: str = ""
    label: str = "HLS stream"
    is_live: bool = False
    estimated_bytes: int | None = None


@dataclass
class HlsOutcome:
    path: str
    label: str
    duration: float
    is_live: bool


@dataclass(frozen=True)
class HlsVariantChoice:
    index: int
    width: int | None
    height: int | None
    resolution: str
    bitrate_kbps: int
    has_audio: bool


async def fetch_master_playlist(
    url: str,
    settings: Settings,
    headers: dict[str, str] | None = None,
) -> MasterPlaylist:
    async with net.make_client(settings, headers) as client:
        text, _final_url = await net.fetch_text(
            client, url, settings, headers, max_bytes=MAX_PLAYLIST_BYTES
        )
    if hls.detect_playlist_kind(text) != hls.PlaylistKind.MASTER:
        raise PlaylistError("The URL did not return an HLS master playlist.")
    master = hls.parse_master(text, url)
    if master.has_drm:
        raise DRMProtectedError("The playlist declares DRM-protected session keys.")
    return master


def describe_variant_choices(master: MasterPlaylist) -> list[HlsVariantChoice]:
    choices: list[HlsVariantChoice] = []
    for index, variant in enumerate(master.variants):
        has_audio = bool(
            variant.audio_group
            and any(
                rendition.type == "AUDIO"
                and rendition.group_id == variant.audio_group
                and rendition.uri
                for rendition in master.renditions
            )
        )
        choices.append(
            HlsVariantChoice(
                index=index,
                width=variant.width,
                height=variant.height,
                resolution=variant.resolution,
                bitrate_kbps=variant.bitrate_kbps,
                has_audio=has_audio,
            )
        )
    return choices


def _hls_cap(settings: Settings, max_height: int | None) -> int:
    if max_height is not None:
        return max(0, max_height)
    return max(0, settings.max_height)


def _choose_variant(
    master: MasterPlaylist,
    settings: Settings,
    *,
    variant_index: int | None = None,
    target_height: int | None = None,
    max_height: int | None = None,
) -> Variant:
    cap = _hls_cap(settings, max_height)
    if variant_index is not None:
        if variant_index < 0 or variant_index >= len(master.variants):
            raise QualityUnavailableError("The selected HLS quality is no longer available.")
        variant = master.variants[variant_index]
        if cap > 0 and variant.height and variant.height > cap:
            raise QualityUnavailableError("The selected HLS quality exceeds the quality limit.")
        return variant
    if target_height is not None:
        candidates = [
            variant
            for variant in master.variants
            if variant.height == target_height and (cap <= 0 or (variant.height or 0) <= cap)
        ]
        if not candidates:
            raise QualityUnavailableError(f"{target_height}p is not available for this HLS stream.")
        return hls.select_variant(candidates, 0)
    return hls.select_variant(master.variants, cap)


async def _inspect_variant(
    master_url: str,
    master: MasterPlaylist,
    variant: Variant,
    settings: Settings,
    headers: dict[str, str] | None = None,
) -> HlsInspection:
    async with net.make_client(settings, headers) as client:
        variant_text, _ = await net.fetch_text(
            client, variant.uri, settings, headers, max_bytes=MAX_PLAYLIST_BYTES
        )
        if hls.detect_playlist_kind(variant_text) == hls.PlaylistKind.MASTER:
            raise PlaylistError("Nested master playlists are not supported.")
        playlist = hls.parse_media(variant_text, variant.uri)
        audio_rendition = hls.select_audio_rendition(master.renditions, variant)
        audio_playlist = None
        audio_label = ""
        if audio_rendition is not None and audio_rendition.uri:
            audio_text, _ = await net.fetch_text(
                client, audio_rendition.uri, settings, headers, max_bytes=MAX_PLAYLIST_BYTES
            )
            audio_playlist = hls.parse_media(audio_text, audio_rendition.uri)
            audio_label = audio_rendition.language or audio_rendition.name or "audio"
        if playlist.has_drm:
            raise DRMProtectedError("The media playlist is DRM-protected (SAMPLE-AES).")
        if audio_playlist is not None and audio_playlist.has_drm:
            raise DRMProtectedError("The audio playlist is DRM-protected (SAMPLE-AES).")
        estimate = playlist.estimate_bytes(variant.bandwidth)
        if estimate and audio_playlist is not None:
            estimate += audio_playlist.estimate_bytes(128_000) or 0
        label = f"HLS {variant.resolution}"
        if variant.bitrate_kbps:
            label += f" @ {variant.bitrate_kbps} kbps"
        if audio_playlist is not None:
            label += f" + audio ({audio_label})"
        return HlsInspection(
            playlist=playlist,
            variant=variant,
            audio_playlist=audio_playlist,
            audio_label=audio_label,
            label=label,
            is_live=playlist.is_live,
            estimated_bytes=estimate,
        )


async def inspect_hls(
    url: str,
    settings: Settings,
    headers: dict[str, str] | None = None,
    *,
    variant_index: int | None = None,
    target_height: int | None = None,
    max_height: int | None = None,
) -> HlsInspection:
    """Fetch and parse the playlist hierarchy, choosing a variant."""
    async with net.make_client(settings, headers) as client:
        text, _final_url = await net.fetch_text(client, url, settings, headers, max_bytes=MAX_PLAYLIST_BYTES)
        kind = hls.detect_playlist_kind(text)
        if kind == hls.PlaylistKind.MASTER:
            master = hls.parse_master(text, url)
            if master.has_drm:
                raise DRMProtectedError("The playlist declares DRM-protected session keys.")
            variant = _choose_variant(
                master,
                settings,
                variant_index=variant_index,
                target_height=target_height,
                max_height=max_height,
            )
            return await _inspect_variant(url, master, variant, settings, headers)
        if kind == hls.PlaylistKind.MEDIA:
            if variant_index is not None or target_height is not None:
                raise QualityUnavailableError(
                    "Quality selection is unavailable for this single-stream playlist."
                )
            playlist = hls.parse_media(text, url)
            if playlist.has_drm:
                raise DRMProtectedError("The media playlist is DRM-protected (SAMPLE-AES).")
            return HlsInspection(
                playlist=playlist,
                variant=None,
                label="HLS media playlist (direct)",
                is_live=playlist.is_live,
                estimated_bytes=None,
            )
        raise PlaylistError("The URL did not return an HLS playlist.")


async def download_hls(
    inspection: HlsInspection,
    dest: str,
    settings: Settings,
    progress: Progress,
    headers: dict[str, str] | None = None,
) -> HlsOutcome:
    """Download the inspected HLS stream to ``dest`` (an MP4 path)."""
    if not media.ffmpeg_available():
        raise media.MediaError("FFmpeg is required for HLS processing but was not found.")
    progress.stage = "downloading"
    video_playlist = inspection.playlist
    audio_playlist = inspection.audio_playlist
    encrypted = video_playlist.is_encrypted or (audio_playlist is not None and audio_playlist.is_encrypted)
    workdir = os.path.dirname(dest) or "."

    async with net.make_client(settings, headers) as client:
        if encrypted:
            video_source = await _download_encrypted(
                video_playlist, workdir, "video", settings, headers, progress
            )
        else:
            video_source = await _download_plain(
                client, video_playlist, os.path.join(workdir, "video_raw.ts"), workdir, settings, headers, progress
            )
        audio_source = None
        if audio_playlist is not None:
            if audio_playlist.is_encrypted:
                audio_source = await _download_encrypted(
                    audio_playlist, workdir, "audio", settings, headers, progress
                )
            else:
                audio_source = await _download_plain(
                    client,
                    audio_playlist,
                    os.path.join(workdir, "audio_raw.aac"),
                    workdir,
                    settings,
                    headers,
                    progress,
                    weight=0.0,
                )

    progress.stage = "processing"
    if audio_source:
        await asyncio.to_thread(
            media.mux_av, video_source, audio_source, dest, max(600, min(settings.download_timeout or 3600, 7200))
        )
    else:
        await asyncio.to_thread(
            media.remux_to_mp4, video_source, dest, max(600, min(settings.download_timeout or 3600, 7200))
        )
    duration = video_playlist.total_duration
    return HlsOutcome(path=dest, label=inspection.label, duration=duration, is_live=inspection.is_live)


async def _download_plain(
    client: httpx.AsyncClient,
    playlist: MediaPlaylist,
    raw_dest: str,
    workdir: str,
    settings: Settings,
    headers: dict[str, str] | None,
    progress: Progress,
    weight: float = 1.0,
) -> str:
    """Download unencrypted segments ourselves, validating every URL."""
    total_segments = len(playlist.segments)
    meter = RateMeter()
    estimate = progress.total

    async def fetch(segment: hls.Segment) -> bytes:
        return await _fetch_segment(client, segment, settings, headers)

    async def on_progress(done: int, total: int, bytes_total: int) -> None:
        progress.downloaded = bytes_total
        if progress.total <= 0 and estimate:
            progress.total = estimate
        progress.detail = f"{done}/{total} segments"
        speed, eta = meter.update(bytes_total, progress.total)
        progress.speed = speed
        if eta is not None:
            progress.eta = eta
        progress.stage = "downloading"

    await hls.download_segments(
        playlist.segments,
        fetch,
        raw_dest,
        os.path.join(workdir, "segments"),
        concurrency=settings.hls_segment_concurrency,
        retries=3,
        retry_delay=1.5,
        progress_cb=on_progress if weight > 0 else None,
    )
    progress.detail = ""
    return raw_dest


async def _download_encrypted(
    playlist: MediaPlaylist,
    workdir: str,
    tag: str,
    settings: Settings,
    headers: dict[str, str] | None,
    progress: Progress,
) -> str:
    """AES-128 playlists are delegated to FFmpeg after SSRF-validating all URLs."""
    for uri in hls.media_uris(playlist):
        await ensure_public_url(uri, settings.allowed_hosts)
    playlist_url = playlist.base_url
    if not playlist_url:
        raise PlaylistError("Playlist has no base URL; cannot delegate to FFmpeg.")
    out = os.path.join(workdir, f"{tag}_enc.mp4")
    progress.detail = "AES-128 stream (FFmpeg)"
    timeout = max(600, min(settings.download_timeout or 3600, 7200))
    await asyncio.to_thread(_ffmpeg_hls, playlist_url, out, settings, headers, timeout)
    return out


def _ffmpeg_hls(
    url: str,
    out: str,
    settings: Settings,
    headers: dict[str, str] | None,
    timeout: int,
) -> str:
    exe = media.ffmpeg_path()
    if not exe:
        raise media.MediaError("FFmpeg is not installed.")
    header_blob = "".join(
        f"{key}: {value}\r\n" for key, value in (headers or {}).items() if value
    )
    cmd = [exe, "-y", "-hide_banner", "-loglevel", "error"]
    if header_blob:
        cmd += ["-headers", header_blob]
    cmd += ["-i", url, "-c", "copy", "-movflags", "+faststart", out]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise media.MediaError(f"FFmpeg HLS download failed to run: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise media.MediaError(f"FFmpeg HLS download failed: {detail[-1] if detail else 'unknown error'}")
    return out


async def _fetch_segment(
    client: httpx.AsyncClient,
    segment: hls.Segment,
    settings: Settings,
    headers: dict[str, str] | None,
) -> bytes:
    current = segment.uri
    for _hop in range(6):
        current = await ensure_public_url(current, settings.allowed_hosts)
        request_headers: dict[str, str] = {}
        if headers:
            request_headers.update({k: v for k, v in headers.items() if v})
        if segment.byte_range:
            start, length = segment.byte_range
            request_headers["Range"] = f"bytes={start}-{start + length - 1}"
        async with client.stream("GET", current, headers=request_headers) as response:
            if 300 <= response.status_code < 400 and response.headers.get("location"):
                from urllib.parse import urljoin

                current = urljoin(str(response.url), response.headers["location"])
                continue
            if response.status_code >= 400:
                raise net.NetworkError(f"HTTP {response.status_code} while fetching segment.")
            buffer = bytearray()
            async for chunk in response.aiter_bytes():
                buffer.extend(chunk)
                if len(buffer) > MAX_SEGMENT_BYTES:
                    raise net.NetworkError("Segment is unreasonably large; aborting.")
            return bytes(buffer)
    raise net.NetworkError("Too many redirects while fetching segment.")
