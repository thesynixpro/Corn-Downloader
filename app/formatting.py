"""Human-readable rendering of sizes, durations, speeds and progress."""

from __future__ import annotations

from .progress import Progress


def format_bytes(n: int | float | None) -> str:
    if n is None:
        return "unknown size"
    try:
        value = float(n)
    except (TypeError, ValueError):
        return "unknown size"
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def format_duration(seconds: int | float | None) -> str:
    if seconds is None:
        return "unknown"
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return "unknown"
    if s < 0:
        return "unknown"
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:d}:{s:02d}"


def format_speed(bytes_per_second: float | None) -> str:
    if not bytes_per_second or bytes_per_second <= 0:
        return "unknown"
    return f"{format_bytes(bytes_per_second)}/s"


def format_eta(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "unknown"
    return format_duration(int(seconds))


def format_resolution(width: int | None, height: int | None) -> str:
    if width and height:
        return f"{int(width)}x{int(height)}"
    if height:
        return f"{int(height)}p"
    return "unknown"


def render_progress(progress: Progress, queue_position: int | None = None) -> str | None:
    """Render a status line for the current progress stage (None = no update)."""
    stage = progress.stage
    if stage == "queued":
        if queue_position and queue_position > 0:
            return f"⏳ Queued — position {queue_position}."
        return "⏳ Queued…"
    if stage == "extracting":
        return "🔍 Extracting metadata…"
    if stage == "inspecting":
        return "🔍 Inspecting stream…"
    if stage == "downloading":
        if progress.detail:
            head = f"⬇️ Downloading… {progress.detail}"
        elif progress.total:
            pct = min(100, int(progress.downloaded * 100 / progress.total))
            head = f"⬇️ Downloading… {pct}% ({format_bytes(progress.downloaded)} / {format_bytes(progress.total)})"
        else:
            head = f"⬇️ Downloading… ({format_bytes(progress.downloaded)})"
        if progress.speed:
            head += f"\n⚡ {format_speed(progress.speed)}"
            if progress.eta is not None:
                head += f" • ⏳ ETA {format_eta(progress.eta)}"
        return head
    if stage == "processing":
        return "🎞 Processing video (merging / remuxing)…"
    if stage == "uploading":
        if progress.total:
            pct = min(100, int(progress.downloaded * 100 / progress.total))
            line = f"📤 Uploading to Telegram… {pct}% ({format_bytes(progress.downloaded)} / {format_bytes(progress.total)})"
        else:
            line = "📤 Uploading to Telegram…"
        if progress.speed:
            line += f"\n⚡ {format_speed(progress.speed)}"
            if progress.eta is not None:
                line += f" • ⏳ ETA {format_eta(progress.eta)}"
        return line
    if stage == "finalizing":
        return "✅ Finalizing…"
    return None
