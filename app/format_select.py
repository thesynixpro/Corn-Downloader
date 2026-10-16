"""Pure yt-dlp format selection helpers.

These functions operate on plain dictionaries as returned by
``yt_dlp.YoutubeDL.extract_info(..., download=False)`` so they can be unit
tested without importing yt-dlp.
"""

from __future__ import annotations

from dataclasses import dataclass

QUALITY_TARGETS = (2160, 1440, 1080, 720, 480, 360)


@dataclass(frozen=True)
class QualityOption:
    target_height: int
    format_id: str
    width: int | None
    height: int
    ext: str
    estimated_bytes: int | None


def build_format_selector(max_bytes: int) -> str:
    """Fallback selector: prefer combined MP4 streams inside the size cap."""
    return (
        f"best[ext=mp4][filesize<{max_bytes}]/"
        f"best[filesize<{max_bytes}]/"
        f"best[ext=mp4]/"
        f"best"
    )


def _size_of(entry: dict) -> int | None:
    size = entry.get("filesize") or entry.get("filesize_approx")
    if isinstance(size, (int, float)) and size > 0:
        return int(size)
    return None


def _has_av(entry: dict) -> bool:
    return entry.get("vcodec") not in (None, "none") and entry.get("acodec") not in (None, "none")


def _height_of(entry: dict) -> int:
    height = entry.get("height")
    return int(height) if isinstance(height, (int, float)) and height > 0 else 0


def has_format_within_limit(info: dict, max_bytes: int) -> tuple[bool, int | None]:
    """Check whether any usable format fits inside ``max_bytes``.

    Returns ``(fits, smallest_known_size_or_None)``. When no size information
    exists at all the function returns ``(True, None)`` so the download is
    attempted and the hard limit is enforced after the fact.
    """
    formats = info.get("formats") or []
    smallest: int | None = None
    fits = False
    for entry in formats:
        size = _size_of(entry)
        if size is None:
            continue
        if smallest is None or size < smallest:
            smallest = size
        if size <= max_bytes:
            fits = True
    for key in ("filesize", "filesize_approx"):
        size = info.get(key)
        if isinstance(size, (int, float)) and size > 0:
            size = int(size)
            if smallest is None or size < smallest:
                smallest = size
            if size <= max_bytes:
                fits = True
    if smallest is None:
        return True, None
    return fits, smallest


def pick_format_string(info: dict, cap: int, max_height: int = 0) -> str | None:
    """Choose an explicit yt-dlp format string fitting inside ``cap``.

    Picks the largest fitting rendition, preferring combined audio+video MP4
    and honouring an optional height cap. Returns ``None`` when no usable size
    information exists (caller falls back to the generic selector) or when
    nothing fits (caller rejects the video).
    """
    formats = [
        entry
        for entry in (info.get("formats") or [])
        if entry.get("url") and entry.get("format_id")
    ]
    sized = [(entry, _size_of(entry)) for entry in formats]
    sized = [(entry, size) for entry, size in sized if size is not None]
    if not sized:
        return None
    fitting = [(entry, size) for entry, size in sized if size <= cap]
    if not fitting:
        return None
    if max_height > 0:
        within_height = [(entry, size) for entry, size in fitting if _height_of(entry) <= max_height]
        if within_height:
            fitting = within_height
    combined = [(entry, size) for entry, size in fitting if _has_av(entry)]
    pool = combined or fitting

    def sort_key(item: tuple[dict, int]) -> tuple[int, int, int]:
        entry, size = item
        is_mp4 = 1 if entry.get("ext") == "mp4" else 0
        return (size, _height_of(entry), is_mp4)

    pool.sort(key=sort_key)
    return str(pool[-1][0]["format_id"])


def _combined_sized_entries(info: dict) -> list[tuple[dict, int, int]]:
    """Return combined audio/video formats with known sizes and heights."""
    usable: list[tuple[dict, int, int]] = []
    for entry in info.get("formats") or []:
        if not entry.get("url") or not entry.get("format_id"):
            continue
        size = _size_of(entry)
        height = _height_of(entry)
        if size is None or height <= 0 or not _has_av(entry):
            continue
        usable.append((entry, size, height))
    return usable


def build_quality_options(
    info: dict, cap_bytes: int, max_height: int = 0, limit: int = 5
) -> list[QualityOption]:
    """Build honest quality options backed by known-size combined formats."""
    usable = [
        (entry, size, height)
        for entry, size, height in _combined_sized_entries(info)
        if size <= cap_bytes and (max_height <= 0 or height <= max_height)
    ]
    usable.sort(
        key=lambda item: (item[2], item[1], 1 if item[0].get("ext") == "mp4" else 0),
        reverse=True,
    )
    options: list[QualityOption] = []
    seen_heights: set[int] = set()
    for entry, size, height in usable:
        if height in seen_heights or len(options) >= max(1, limit):
            continue
        seen_heights.add(height)
        width = entry.get("width")
        options.append(
            QualityOption(
                target_height=height,
                format_id=str(entry.get("format_id")),
                width=int(width) if isinstance(width, (int, float)) and width > 0 else None,
                height=height,
                ext=str(entry.get("ext") or ""),
                estimated_bytes=size,
            )
        )
    return options


def selector_for_height(info: dict, cap_bytes: int, target_height: int) -> str | None:
    """Select a concrete combined format at or below an explicit height."""
    if target_height <= 0:
        return None
    usable = [
        (entry, size, height)
        for entry, size, height in _combined_sized_entries(info)
        if size <= cap_bytes and height <= target_height
    ]
    if not usable:
        return None
    usable.sort(
        key=lambda item: (item[2], item[1], 1 if item[0].get("ext") == "mp4" else 0),
        reverse=True,
    )
    return str(usable[0][0].get("format_id"))


def best_display_format(info: dict) -> dict:
    """Pick a representative format entry for status messages."""
    formats = [entry for entry in (info.get("formats") or []) if entry.get("url")]
    if not formats:
        return info
    best = max(
        formats,
        key=lambda entry: (
            _height_of(entry),
            entry.get("tbr") if isinstance(entry.get("tbr"), (int, float)) else 0,
        ),
    )
    return best
