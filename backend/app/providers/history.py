"""Normalize stored chat history into provider-safe message roles.

The database stores assistant turns with the local role name ``agent``. That is
not a valid OpenAI chat role, so forwarding it verbatim made every follow-up
conversational turn fail with HTTP 400 once a prior answer existed. Keep the
mapping in one place so every OpenAI-compatible provider behaves the same.
"""
from __future__ import annotations

from typing import Any

_ROLE_MAP = {
    "user": "user",
    "assistant": "assistant",
    "agent": "assistant",
}


def normalize_history(history: list[dict[str, Any]] | None, limit: int = 6) -> list[dict[str, str]]:
    """Return the last ``limit`` usable turns with valid provider roles.

    Non-message entries, empty content, and unknown roles (for example a
    ``system`` or ``tool`` row that must not be replayed from untrusted chat
    storage) are dropped.
    """
    messages: list[dict[str, str]] = []
    for item in (history or [])[-max(0, limit):]:
        if not isinstance(item, dict):
            continue
        role = _ROLE_MAP.get(str(item.get("role") or "").strip().lower())
        content = item.get("content")
        if role is None or not isinstance(content, str) or not content.strip():
            continue
        messages.append({"role": role, "content": content})
    return messages
