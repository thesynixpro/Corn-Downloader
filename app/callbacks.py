"""Compact, safe Telegram callback-data encoding.

Telegram limits ``callback_data`` to 64 bytes. Payloads therefore contain only
short action names, session identifiers, and small option indexes. URLs,
format IDs, titles, and other user-controlled values are never embedded.
"""

from __future__ import annotations

import re

VERSION = "v1"
MAX_CALLBACK_BYTES = 64
_PART_RE = re.compile(r"^[A-Za-z0-9_-]{1,20}$")


class CallbackError(ValueError):
    """Raised when callback data is malformed or unsafe."""


def encode(action: str, *parts: str) -> str:
    tokens = [VERSION, action, *parts]
    for token in tokens:
        if not _PART_RE.match(token):
            raise CallbackError(f"Invalid callback token: {token!r}")
    data = ":".join(tokens)
    if len(data.encode("utf-8")) > MAX_CALLBACK_BYTES:
        raise CallbackError("Callback data exceeds Telegram's 64-byte limit.")
    return data


def decode(data: str) -> tuple[str, tuple[str, ...]]:
    text = data or ""
    if len(text.encode("utf-8")) > MAX_CALLBACK_BYTES:
        raise CallbackError("Callback data exceeds Telegram's 64-byte limit.")
    tokens = text.split(":")
    if len(tokens) < 2 or tokens[0] != VERSION:
        raise CallbackError("Unknown callback version.")
    action, parts = tokens[1], tuple(tokens[2:])
    if not _PART_RE.match(action):
        raise CallbackError(f"Invalid callback action: {action!r}")
    for token in parts:
        if not _PART_RE.match(token):
            raise CallbackError(f"Invalid callback token: {token!r}")
    return action, parts
