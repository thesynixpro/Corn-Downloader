"""Pure yt-dlp format selection helpers.

These functions operate on plain dictionaries as returned by
``yt_dlp.YoutubeDL.extract_info(..., download=False)`` so they can be unit
tested without importing yt-dlp.
"""

from __future__ import annotations


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
