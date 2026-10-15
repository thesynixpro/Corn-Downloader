"""Pure HLS (m3u8) parsing and segment downloading.

This module has no third-party dependencies so it can be unit tested in
isolation. Network fetching is injected through a ``fetch(segment) -> bytes``
callback; :mod:`app.hls_download` provides the httpx-backed implementation
with SSRF validation and header handling.

Supported playlist features:
* master/media playlist detection,
* variant selection by resolution/bandwidth/height cap,
* separate audio renditions (``EXT-X-MEDIA``),
* ``EXT-X-KEY`` (AES-128 permitted; SAMPLE-AES flagged as DRM),
* ``EXT-X-MAP`` initialization segments (fMP4),
* ``EXT-X-BYTERANGE`` segments,
* VOD vs live playlists (``EXT-X-ENDLIST`` / ``EXT-X-PLAYLIST-TYPE``).
"""

from __future__ import annotations

import asyncio
import inspect
import os
import shutil
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable
from urllib.parse import urljoin


class PlaylistError(Exception):
    """Raised when a playlist is malformed, empty, or unsupported."""


class DRMProtectedError(PlaylistError):
    """Raised when the playlist uses DRM (SAMPLE-AES / widevine / etc.)."""


class PlaylistKind(str, Enum):
    MASTER = "master"
    MEDIA = "media"
    UNKNOWN = "unknown"


def detect_playlist_kind(text: str) -> PlaylistKind:
    stripped = (text or "").lstrip()
    if not stripped.startswith("#EXTM3U"):
        return PlaylistKind.UNKNOWN
    if "#EXT-X-STREAM-INF" in text:
        return PlaylistKind.MASTER
    if "#EXTINF" in text or "#EXT-X-TARGETDURATION" in text or "#EXT-X-MEDIA-SEQUENCE" in text:
        return PlaylistKind.MEDIA
    return PlaylistKind.UNKNOWN


def parse_attribute_list(text: str) -> dict[str, str]:
    """Parse an HLS attribute list, honouring quoted values with commas."""
    attrs: dict[str, str] = {}
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i] in " \t,":
            i += 1
        start = i
        while i < n and text[i] not in "=,":
            i += 1
        key = text[start:i].strip()
        if not key or i >= n or text[i] != "=":
            while i < n and text[i] != ",":
                i += 1
            continue
        i += 1
        if i < n and text[i] == '"':
            i += 1
            buf: list[str] = []
            while i < n and text[i] != '"':
                buf.append(text[i])
                i += 1
            value = "".join(buf)
            if i < n:
                i += 1
        else:
            start = i
            while i < n and text[i] != ",":
                i += 1
            value = text[start:i].strip()
        attrs[key] = value
    return attrs


def _to_int(value: str | None) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _to_float(value: str | None) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0


@dataclass
class KeyInfo:
    method: str = "NONE"
    uri: str | None = None
    iv: str | None = None

    @property
    def is_encrypted(self) -> bool:
        return self.method.upper() not in ("", "NONE")


@dataclass
class Segment:
    uri: str
    duration: float = 0.0
    title: str = ""
    is_init: bool = False
    byte_range: tuple[int, int] | None = None
    key: KeyInfo | None = None


@dataclass
class Variant:
    uri: str
    bandwidth: int = 0
    average_bandwidth: int = 0
    width: int | None = None
    height: int | None = None
    codecs: str = ""
    audio_group: str = ""
    name: str = ""

    @property
    def resolution(self) -> str:
        if self.width and self.height:
            return f"{self.width}x{self.height}"
        if self.height:
            return f"{self.height}p"
        return "unknown"

    @property
    def bitrate_kbps(self) -> int:
        rate = self.average_bandwidth or self.bandwidth
        return int(rate / 1000) if rate else 0

    def estimate_bytes(self, duration_s: float) -> int | None:
        rate = self.average_bandwidth or self.bandwidth
        if not rate or duration_s <= 0:
            return None
        return int(rate / 8 * duration_s)


@dataclass
class Rendition:
    type: str
    group_id: str
    name: str = ""
    uri: str | None = None
    language: str = ""
    default: bool = False
    autoselect: bool = False


@dataclass
class MasterPlaylist:
    variants: list[Variant] = field(default_factory=list)
    renditions: list[Rendition] = field(default_factory=list)
    has_drm: bool = False
    base_url: str = ""


@dataclass
class MediaPlaylist:
    segments: list[Segment] = field(default_factory=list)
    target_duration: float = 0.0
    media_sequence: int = 0
    is_endlist: bool = False
    playlist_type: str = ""
    has_drm: bool = False
    base_url: str = ""

    @property
    def total_duration(self) -> float:
        return sum(seg.duration for seg in self.segments if not seg.is_init)

    @property
    def is_live(self) -> bool:
        return not self.is_endlist and self.playlist_type.upper() != "VOD"

    @property
    def encryption_methods(self) -> set[str]:
        return {seg.key.method.upper() for seg in self.segments if seg.key and seg.key.is_encrypted}

    @property
    def is_encrypted(self) -> bool:
        return bool(self.encryption_methods)

    @property
    def key_uris(self) -> list[str]:
        seen: list[str] = []
        for seg in self.segments:
            if seg.key and seg.key.uri and seg.key.uri not in seen:
                seen.append(seg.key.uri)
        return seen

    def estimate_bytes(self, bandwidth: int) -> int | None:
        if not bandwidth or self.total_duration <= 0:
            return None
        return int(bandwidth / 8 * self.total_duration)


def parse_master(text: str, base_url: str) -> MasterPlaylist:
    variants: list[Variant] = []
    renditions: list[Rendition] = []
    has_drm = False
    pending: dict[str, str] | None = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-STREAM-INF:"):
            pending = parse_attribute_list(line.split(":", 1)[1])
            continue
        if line.startswith("#EXT-X-I-FRAME-STREAM-INF:"):
            continue
        if line.startswith("#EXT-X-MEDIA:"):
            attrs = parse_attribute_list(line.split(":", 1)[1])
            uri = attrs.get("URI")
            renditions.append(
                Rendition(
                    type=attrs.get("TYPE", "").upper(),
                    group_id=attrs.get("GROUP-ID", ""),
                    name=attrs.get("NAME", ""),
                    uri=urljoin(base_url, uri) if uri else None,
                    language=attrs.get("LANGUAGE", ""),
                    default=attrs.get("DEFAULT", "NO").upper() == "YES",
                    autoselect=attrs.get("AUTOSELECT", "NO").upper() == "YES",
                )
            )
            continue
        if line.startswith("#EXT-X-SESSION-KEY:"):
            attrs = parse_attribute_list(line.split(":", 1)[1])
            if attrs.get("METHOD", "").upper() not in ("", "NONE", "AES-128"):
                has_drm = True
            continue
        if line.startswith("#"):
            continue
        if pending is not None:
            width = height = None
            resolution = pending.get("RESOLUTION", "")
            if "x" in resolution:
                parts = resolution.lower().split("x", 1)
                width = _to_int(parts[0]) or None
                height = _to_int(parts[1]) or None
            variants.append(
                Variant(
                    uri=urljoin(base_url, line),
                    bandwidth=_to_int(pending.get("BANDWIDTH")),
                    average_bandwidth=_to_int(pending.get("AVERAGE-BANDWIDTH")),
                    width=width,
                    height=height,
                    codecs=pending.get("CODECS", ""),
                    audio_group=pending.get("AUDIO", ""),
                    name=pending.get("NAME", ""),
                )
            )
            pending = None
    if not variants:
        raise PlaylistError("Master playlist contains no stream variants.")
    return MasterPlaylist(variants=variants, renditions=renditions, has_drm=has_drm, base_url=base_url)


def parse_media(text: str, base_url: str) -> MediaPlaylist:
    segments: list[Segment] = []
    target_duration = 0.0
    media_sequence = 0
    is_endlist = False
    playlist_type = ""
    has_drm = False
    current_key: KeyInfo | None = None
    pending_duration = 0.0
    pending_title = ""
    pending_range: tuple[int, int] | None = None
    init_added = False
    last_range_end = 0
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-TARGETDURATION:"):
            target_duration = _to_float(line.split(":", 1)[1])
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            media_sequence = _to_int(line.split(":", 1)[1])
            continue
        if line.startswith("#EXT-X-PLAYLIST-TYPE:"):
            playlist_type = line.split(":", 1)[1].strip().upper()
            continue
        if line.startswith("#EXT-X-ENDLIST"):
            is_endlist = True
            continue
        if line.startswith("#EXT-X-KEY:"):
            attrs = parse_attribute_list(line.split(":", 1)[1])
            method = attrs.get("METHOD", "NONE").upper()
            if method in ("", "NONE"):
                current_key = None
            else:
                if method not in ("AES-128",):
                    has_drm = True
                uri = attrs.get("URI")
                current_key = KeyInfo(
                    method=method,
                    uri=urljoin(base_url, uri) if uri else None,
                    iv=attrs.get("IV"),
                )
            continue
        if line.startswith("#EXT-X-MAP:"):
            attrs = parse_attribute_list(line.split(":", 1)[1])
            uri = attrs.get("URI")
            if uri and not init_added:
                segments.append(Segment(uri=urljoin(base_url, uri), duration=0.0, is_init=True))
                init_added = True
            continue
        if line.startswith("#EXT-X-BYTERANGE:"):
            value = line.split(":", 1)[1].strip()
            if "@" in value:
                length_s, offset_s = value.split("@", 1)
                length = _to_int(length_s)
                offset = _to_int(offset_s)
            else:
                length = _to_int(value)
                offset = last_range_end
            pending_range = (offset, length)
            continue
        if line.startswith("#EXTINF:"):
            value = line.split(":", 1)[1]
            duration_s, _, title = value.partition(",")
            pending_duration = _to_float(duration_s)
            pending_title = title.strip()
            continue
        if line.startswith("#"):
            continue
        uri = urljoin(base_url, line)
        segments.append(
            Segment(
                uri=uri,
                duration=pending_duration,
                title=pending_title,
                byte_range=pending_range,
                key=current_key,
            )
        )
        if pending_range is not None:
            last_range_end = pending_range[0] + pending_range[1]
        pending_duration = 0.0
        pending_title = ""
        pending_range = None
    if not segments:
        raise PlaylistError("Media playlist contains no segments.")
    return MediaPlaylist(
        segments=segments,
        target_duration=target_duration,
        media_sequence=media_sequence,
        is_endlist=is_endlist,
        playlist_type=playlist_type,
        has_drm=has_drm,
        base_url=base_url,
    )


def select_variant(variants: list[Variant], max_height: int = 0) -> Variant:
    """Select the best variant, honouring an optional height cap.

    * With ``max_height > 0``: the highest variant at or below the cap is
      chosen; if every variant exceeds the cap, the smallest one is returned.
    * Without a cap: the highest resolution/bandwidth variant is chosen.
    H.264 (avc1) is preferred when resolutions tie, for Telegram compatibility.
    """
    if not variants:
        raise PlaylistError("No variants available to select from.")

    def key(variant: Variant) -> tuple[int, int, int]:
        avc1_bonus = 1 if "avc1" in (variant.codecs or "").lower() else 0
        return (variant.height or 0, variant.bandwidth or 0, avc1_bonus)

    if max_height and max_height > 0:
        fitting = [v for v in variants if v.height and v.height <= max_height]
        if fitting:
            return max(fitting, key=key)
        with_height = [v for v in variants if v.height]
        if with_height:
            return min(with_height, key=key)
    return max(variants, key=key)


def select_audio_rendition(renditions: list[Rendition], variant: Variant | None) -> Rendition | None:
    """Pick the default/autoselect audio rendition for a variant's AUDIO group."""
    if variant is None or not variant.audio_group:
        return None
    candidates = [
        r
        for r in renditions
        if r.type == "AUDIO" and r.group_id == variant.audio_group and r.uri
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda r: (
            not r.default,
            not r.autoselect,
            r.language.lower() not in ("en", "eng", ""),
            r.name.lower(),
        )
    )
    return candidates[0]


def media_uris(playlist: MediaPlaylist) -> list[str]:
    """All remote URLs referenced by a media playlist (segments + keys)."""
    uris = [seg.uri for seg in playlist.segments]
    uris.extend(playlist.key_uris)
    return uris


ProgressCallback = Callable[[int, int, int], object]


async def download_segments(
    segments: list[Segment],
    fetch: Callable[[Segment], Awaitable[bytes]],
    dest_path: str,
    workdir: str,
    *,
    concurrency: int = 4,
    retries: int = 3,
    retry_delay: float = 1.0,
    progress_cb: ProgressCallback | None = None,
    sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> int:
    """Download all segments (bounded concurrency, bounded retries) and merge.

    Each segment is fetched via the injected ``fetch`` callback and written to
    its own part file, so a single failed segment can be retried without
    holding the whole media in memory. Parts are concatenated in playlist
    order into ``dest_path``. Returns the number of bytes written.
    """
    if not segments:
        raise PlaylistError("Playlist contains no segments to download.")
    os.makedirs(workdir, exist_ok=True)
    total = len(segments)
    semaphore = asyncio.Semaphore(max(1, int(concurrency)))
    lock = asyncio.Lock()
    done = 0
    total_bytes = 0

    async def worker(index: int, segment: Segment) -> None:
        nonlocal done, total_bytes
        async with semaphore:
            data: bytes | None = None
            last_exc: Exception | None = None
            for attempt in range(retries + 1):
                try:
                    data = await fetch(segment)
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - retried below
                    last_exc = exc
                    if attempt >= retries:
                        raise PlaylistError(
                            f"Segment {index + 1}/{total} failed after "
                            f"{retries + 1} attempts: {exc}"
                        ) from exc
                    await sleep_fn(retry_delay * (attempt + 1))
            if not data:
                raise PlaylistError(f"Segment {index + 1}/{total} is empty.")
            part_path = os.path.join(workdir, f"seg_{index:05d}.part")
            with open(part_path, "wb") as handle:
                handle.write(data)
            async with lock:
                done += 1
                total_bytes += len(data)
                snapshot = (done, total, total_bytes)
            if progress_cb is not None:
                result = progress_cb(*snapshot)
                if inspect.isawaitable(result):
                    await result

    tasks = [asyncio.create_task(worker(i, segment)) for i, segment in enumerate(segments)]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    with open(dest_path, "wb") as out:
        for i in range(total):
            part_path = os.path.join(workdir, f"seg_{i:05d}.part")
            with open(part_path, "rb") as part:
                shutil.copyfileobj(part, out, 1024 * 1024)
    return total_bytes
