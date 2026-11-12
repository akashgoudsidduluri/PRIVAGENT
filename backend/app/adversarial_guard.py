"""Untrusted page, PDF and search text stay data, never instruction."""
from __future__ import annotations

import html
import re
from typing import Any

from .privacy import scrub_persistent

_CONTROL = re.compile(r"[\u200b-\u200f\u2060\ufeff\x00-\x08\x0b\x0c\x0e-\x1f]")
_INSTRUCTIONS = [
    re.compile(r"(?i)ignore\s+(?:all\s+)?(?:previous|above|prior)\s+instructions"),
    re.compile(r"(?i)(?:reveal|print|repeat)\s+(?:your|the)\s+(?:system|developer)\s+(?:prompt|instructions)"),
    re.compile(r"(?i)(?:send|exfiltrate|upload)\s+(?:the\s+)?(?:token|secret|password|api key)"),
]


def cleanse_untrusted(text: str, limit: int = 20_000) -> str:
    if not isinstance(text, str):
        raise ValueError("untrusted text must be a string")
    clean = scrub_persistent(_CONTROL.sub("", text[:limit * 2]))
    for pattern in _INSTRUCTIONS:
        clean = pattern.sub("[quarantined instruction]", clean)
    return clean[:limit]


def quarantine_text(text: str, source: str = "page") -> str:
    if source not in {"page", "pdf", "search", "connector", "mcp"}:
        raise ValueError("unknown untrusted source")
    return f'<untrusted_page_content source="{source}">{html.escape(cleanse_untrusted(text))}</untrusted_page_content>'


def filter_hidden_dom(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Server-side second pass for structured DOM observations."""
    visible = []
    for node in nodes:
        style = node.get("style") or {}
        try:
            opacity = float(style.get("opacity", 1))
            font_size = float(str(style.get("fontSize", "16")).removesuffix("px"))
            z_index = int(style.get("zIndex", 0) or 0)
        except (TypeError, ValueError):
            continue
        if (opacity <= 0 or font_size <= 0 or z_index <= -999 or
            style.get("display") == "none" or style.get("visibility") == "hidden" or
            node.get("hidden") or node.get("aria-hidden") == "true"):
            continue
        visible.append(node)
    return visible
