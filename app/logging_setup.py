"""Logging configuration with secret redaction.

The Telegram bot token is embedded in every Bot API request URL, and some
third-party libraries log full URLs. Every log record is passed through a
filter that replaces known secrets with ``***`` before it is emitted.
"""

from __future__ import annotations

import logging
import sys
from typing import Iterable, TextIO

NOISY_LOGGERS = ("httpx", "httpcore", "telegram", "telegram.ext", "apscheduler", "telethon")


class SecretRedactingFilter(logging.Filter):
    """Replace registered secrets inside log messages with ``***``."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s and len(s) >= 6]

    def add_secret(self, secret: str) -> None:
        if secret and len(secret) >= 6 and secret not in self._secrets:
            self._secrets.append(secret)

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        try:
            message = record.getMessage()
            redacted = message
            for secret in self._secrets:
                if secret in redacted:
                    redacted = redacted.replace(secret, "***")
            if redacted != message:
                record.msg = redacted
                record.args = ()
        except Exception:
            pass
        return True


def setup_logging(level: str = "INFO", secrets: Iterable[str] = (), stream: TextIO | None = None) -> SecretRedactingFilter:
    """Configure root logging once; returns the installed redaction filter."""
    redactor = SecretRedactingFilter(secrets)
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    )
    handler.addFilter(redactor)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    return redactor
