"""Ephemeral user-selected attachments. No bytes reach planner, logs or SQLite."""
from __future__ import annotations

import base64
import re
import secrets
import time

MAX_BYTES = 5 * 1024 * 1024
TTL_SECONDS = 600
_NAME = re.compile(r"^[\w .()-]{1,120}\.(?:pdf|png|jpe?g|txt|csv|docx|xlsx)$", re.I)
_uploads: dict[str, tuple[float, str, str]] = {}


def put(file_name: str, encoded: str) -> str:
    if not _NAME.fullmatch(file_name) or len(encoded) > 7_000_000:
        raise ValueError("unsupported filename or attachment size")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except Exception:
        raise ValueError("invalid attachment encoding") from None
    if not raw or len(raw) > MAX_BYTES:
        raise ValueError("attachment must be between 1 byte and 5 MiB")
    purge()
    token = secrets.token_urlsafe(32)
    _uploads[token] = (time.monotonic() + TTL_SECONDS, file_name, encoded)
    return token


def get(token: str) -> tuple[str, str] | None:
    purge()
    found = _uploads.get(token)
    return (found[1], found[2]) if found else None


def purge() -> None:
    now = time.monotonic()
    for key, record in list(_uploads.items()):
        if record[0] <= now:
            del _uploads[key]


def remove(token: str) -> None:
    _uploads.pop(token, None)
