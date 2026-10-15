"""FFmpeg/FFprobe helpers: validation, remuxing, muxing and splitting."""

from __future__ import annotations

import json
import logging
import math
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("uvd_bot.media")


class MediaError(Exception):
    """Raised when media processing fails."""


class SplitError(MediaError):
    """Raised when a video cannot be split into uploadable parts."""


class ProbeUnavailable(MediaError):
    """Raised when ffprobe is not installed."""


@dataclass
class MediaInfo:
    path: str
    size: int
    duration: float
    width: int | None
    height: int | None
    video_codec: str | None
    audio_codec: str | None
    has_video: bool
    has_audio: bool

    @property
    def resolution(self) -> str:
        if self.width and self.height:
            return f"{self.width}x{self.height}"
        if self.height:
            return f"{self.height}p"
        return "unknown"


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def ffprobe_path() -> str | None:
    return shutil.which("ffprobe")


def ffmpeg_available() -> bool:
    return ffmpeg_path() is not None


def probe(path: str, timeout: int = 60) -> MediaInfo:
    """Inspect a media file with ffprobe. Raises MediaError/ProbeUnavailable."""
    exe = ffprobe_path()
    if not exe:
        raise ProbeUnavailable("ffprobe is not installed.")
    cmd = [
        exe,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MediaError(f"ffprobe failed to run: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise MediaError(f"ffprobe rejected the file: {detail[-1] if detail else 'unknown error'}")
    try:
        payload = json.loads(proc.stdout.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError as exc:
        raise MediaError(f"ffprobe produced invalid JSON: {exc}") from exc
    streams = payload.get("streams") or []
    fmt = payload.get("format") or {}
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = 0.0
    for value in (fmt.get("duration"), (video or {}).get("duration"), (audio or {}).get("duration")):
        try:
            duration = float(value)
            if duration > 0:
                break
        except (TypeError, ValueError):
            continue
    try:
        size = int(fmt.get("size") or os.path.getsize(path))
    except (TypeError, ValueError, OSError):
        size = 0
    return MediaInfo(
        path=path,
        size=size,
        duration=max(0.0, duration),
        width=int(video["width"]) if video and video.get("width") else None,
        height=int(video["height"]) if video and video.get("height") else None,
        video_codec=(video or {}).get("codec_name"),
        audio_codec=(audio or {}).get("codec_name"),
        has_video=video is not None,
        has_audio=audio is not None,
    )


def validate_video(path: str) -> MediaInfo | None:
    """Validate a downloaded file. Returns MediaInfo, or None if ffprobe is absent.

    Raises MediaError when the file is empty, unreadable, or contains no video
    stream.
    """
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise MediaError(f"Downloaded file is not readable: {exc}") from exc
    if size <= 0:
        raise MediaError("Downloaded file is empty.")
    try:
        info = probe(path)
    except ProbeUnavailable:
        logger.warning("ffprobe unavailable; skipping stream validation for %s", path)
        return None
    if not info.has_video:
        raise MediaError("Downloaded file contains no video stream.")
    if info.duration <= 0:
        logger.warning("Media duration is unknown for %s", path)
    return info


def remux_to_mp4(src: str, out: str, timeout: int = 1800) -> str:
    """Losslessly remux ``src`` into a faststart MP4 at ``out``."""
    exe = ffmpeg_path()
    if not exe:
        raise MediaError("FFmpeg is not installed.")
    cmd = [
        exe,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        src,
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        out,
    ]
    _run_ffmpeg(cmd, timeout, "remux")
    return out


def mux_av(video_path: str, audio_path: str, out: str, timeout: int = 1800) -> str:
    """Mux separate video and audio streams into one file."""
    exe = ffmpeg_path()
    if not exe:
        raise MediaError("FFmpeg is not installed.")
    cmd = [
        exe,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        video_path,
        "-i",
        audio_path,
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        out,
    ]
    _run_ffmpeg(cmd, timeout, "mux")
    return out


def _run_ffmpeg(cmd: list[str], timeout: int, label: str) -> None:
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MediaError(f"FFmpeg {label} failed to run: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise MediaError(f"FFmpeg {label} failed: {detail[-1] if detail else 'unknown error'}")


def split_video(
    filepath: str,
    parent_dir: str,
    duration_s: float | None,
    part_mb: int,
    max_parts: int,
    cloud_limit: int = 50 * 1024 * 1024,
    timeout: int = 600,
) -> list[str]:
    """Split a video into <= ``part_mb`` MB parts via stream copy."""
    exe = ffmpeg_path()
    if not exe:
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
        raise SplitError("Unknown duration; cannot split this file.")
    part_bytes = part_mb * 1024 * 1024
    n_parts = max(2, math.ceil(total / part_bytes))
    if n_parts > max_parts:
        raise SplitError(
            f"Needs {n_parts} parts (limit {max_parts}). "
            "Use a local Bot API server for very large files."
        )
    seg_time = max(10.0, duration / n_parts * 0.98)
    outdir = tempfile.mkdtemp(prefix="parts_", dir=parent_dir)
    pattern = os.path.join(outdir, "part_%03d.mp4")
    cmd = [
        exe,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        filepath,
        "-c",
        "copy",
        "-map",
        "0",
        "-f",
        "segment",
        "-segment_time",
        f"{seg_time:.2f}",
        "-reset_timestamps",
        "1",
        pattern,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SplitError("Video splitting timed out or failed.") from exc
    if proc.returncode != 0:
        raise SplitError("Video splitting failed.")
    parts = sorted(str(p) for p in Path(outdir).glob("part_*.mp4") if p.stat().st_size > 0)
    if not parts:
        raise SplitError("Video splitting produced no parts.")
    for part in parts:
        if os.path.getsize(part) > cloud_limit:
            raise SplitError(
                "A part still exceeds Telegram's limit. "
                "Use a local Bot API server for this file."
            )
    return parts
