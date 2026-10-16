"""Persistent per-user preferences and accurate download statistics.

SQLite is used through short-lived connections so no extra dependency is
required. Values are validated before storage; statistics count only completed
deliveries.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

QUALITY_VALUES = ("auto", "2160", "1440", "1080", "720", "480", "360")
UPLOAD_MODES = ("stream", "file")
PROGRESS_MODES = ("standard", "quiet")
_PREF_KEYS = ("quality", "upload_mode", "progress_mode")
_DEFAULTS = {"quality": "auto", "upload_mode": "stream", "progress_mode": "standard"}


class StoreError(Exception):
    """Raised for invalid preference values."""


class UserStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self._lock = threading.Lock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS prefs (
                    user_id INTEGER PRIMARY KEY,
                    quality TEXT NOT NULL DEFAULT 'auto',
                    upload_mode TEXT NOT NULL DEFAULT 'stream',
                    progress_mode TEXT NOT NULL DEFAULT 'standard',
                    updated_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS downloads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    platform TEXT NOT NULL DEFAULT '',
                    mode TEXT NOT NULL DEFAULT '',
                    parts INTEGER NOT NULL DEFAULT 1,
                    size_bytes INTEGER NOT NULL DEFAULT 0,
                    occurred_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_downloads_user ON downloads(user_id, occurred_at)"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path), timeout=10.0)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def get_preferences(self, user_id: int) -> dict[str, str]:
        prefs = dict(_DEFAULTS)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT quality, upload_mode, progress_mode FROM prefs WHERE user_id = ?",
                (int(user_id),),
            ).fetchone()
        if row:
            prefs.update(
                {
                    "quality": row[0] if row[0] in QUALITY_VALUES else "auto",
                    "upload_mode": row[1] if row[1] in UPLOAD_MODES else "stream",
                    "progress_mode": row[2] if row[2] in PROGRESS_MODES else "standard",
                }
            )
        return prefs

    def set_preference(self, user_id: int, key: str, value: str) -> dict[str, str]:
        if key not in _PREF_KEYS:
            raise StoreError(f"Unknown preference: {key}")
        allowed = {
            "quality": QUALITY_VALUES,
            "upload_mode": UPLOAD_MODES,
            "progress_mode": PROGRESS_MODES,
        }[key]
        if value not in allowed:
            raise StoreError(f"Unsupported value for {key}: {value!r}")
        prefs = self.get_preferences(user_id)
        prefs[key] = value
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO prefs (user_id, quality, upload_mode, progress_mode, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    quality=excluded.quality,
                    upload_mode=excluded.upload_mode,
                    progress_mode=excluded.progress_mode,
                    updated_at=excluded.updated_at
                """,
                (int(user_id), prefs["quality"], prefs["upload_mode"], prefs["progress_mode"], time.time()),
            )
        return prefs

    def record_download(
        self, user_id: int, platform: str, mode: str, parts: int, size_bytes: int
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO downloads (user_id, platform, mode, parts, size_bytes, occurred_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    int(user_id),
                    str(platform or "")[:64],
                    str(mode or "")[:16],
                    max(1, int(parts or 1)),
                    max(0, int(size_bytes or 0)),
                    time.time(),
                ),
            )

    def get_stats(self, user_id: int) -> dict[str, int | float | None]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*), COALESCE(SUM(size_bytes), 0), MAX(occurred_at)
                FROM downloads WHERE user_id = ?
                """,
                (int(user_id),),
            ).fetchone()
        completed = int(row[0] or 0) if row else 0
        total_bytes = int(row[1] or 0) if row else 0
        last_at = float(row[2]) if row and row[2] else None
        return {"completed": completed, "bytes": total_bytes, "last_at": last_at}
