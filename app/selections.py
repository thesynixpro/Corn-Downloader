"""User quality selections and shared selection errors."""

from __future__ import annotations

from dataclasses import dataclass


class QualityUnavailableError(Exception):
    """Raised when an explicitly selected quality is no longer available."""


@dataclass(frozen=True)
class QualitySelection:
    """A validated quality choice for one download.

    ``mode`` is ``"auto"`` or ``"height"``. ``target_height`` is only meaningful
    for ``"height"`` and always denotes an explicit user choice, including for
    HLS variant selection.
    """

    mode: str = "auto"
    target_height: int | None = None

    @property
    def is_explicit(self) -> bool:
        return self.mode == "height" and isinstance(self.target_height, int)


ALLOWED_QUALITY_VALUES = ("auto", "2160", "1440", "1080", "720", "480", "360")


def normalize_quality_value(value: str) -> str:
    cleaned = (value or "").strip().lower()
    if cleaned not in ALLOWED_QUALITY_VALUES:
        raise ValueError(f"Unsupported quality preference: {value!r}")
    return cleaned


def default_height(quality_value: str | None) -> int:
    """Translate a stored default-quality preference into a height cap."""
    if not quality_value or quality_value == "auto":
        return 0
    try:
        return int(quality_value)
    except (TypeError, ValueError):
        return 0


def quality_label(value: str) -> str:
    return "Auto" if value == "auto" else f"{value}p"


def effective_height_cap(
    settings_max_height: int,
    default_quality: str | None,
    selection: QualitySelection | None,
) -> int:
    """Combine global, default-preference, and per-download height caps."""
    caps: list[int] = []
    if isinstance(settings_max_height, int) and settings_max_height > 0:
        caps.append(settings_max_height)
    preferred = default_height(default_quality)
    if preferred > 0:
        caps.append(preferred)
    if selection is not None and selection.is_explicit and selection.target_height:
        caps.append(selection.target_height)
    return min(caps) if caps else 0
