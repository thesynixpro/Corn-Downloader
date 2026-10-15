"""Download/upload progress state and rate metering."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Callable


@dataclass
class Progress:
    """Mutable progress snapshot shared between worker threads and handlers.

    Attribute writes are atomic in CPython, which is sufficient for the
    single-writer/multi-reader access pattern used here (yt-dlp hooks run in a
    worker thread while the status poller only reads).
    """

    stage: str = "queued"
    downloaded: int = 0
    total: int = 0
    speed: float = 0.0
    eta: float | None = None
    detail: str = ""

    def reset(self, stage: str, total: int = 0) -> None:
        self.stage = stage
        self.downloaded = 0
        self.total = total
        self.speed = 0.0
        self.eta = None
        self.detail = ""


class RateMeter:
    """Sliding-window transfer rate and ETA estimator."""

    def __init__(self, window: float = 5.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._window = window
        self._clock = clock
        self._samples: deque[tuple[float, int]] = deque()

    def update(self, done: int, total: int = 0) -> tuple[float, float | None]:
        now = self._clock()
        self._samples.append((now, done))
        while len(self._samples) > 2 and now - self._samples[0][0] > self._window:
            self._samples.popleft()
        if len(self._samples) < 2:
            return 0.0, None
        t0, b0 = self._samples[0]
        t1, b1 = self._samples[-1]
        dt = t1 - t0
        if dt <= 0:
            return 0.0, None
        speed = max(0.0, (b1 - b0) / dt)
        eta: float | None = None
        if speed > 0 and total > b1:
            eta = (total - b1) / speed
        return speed, eta

    def reset(self) -> None:
        self._samples.clear()
