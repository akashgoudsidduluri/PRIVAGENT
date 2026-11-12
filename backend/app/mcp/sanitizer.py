"""Untrusted tool output is bounded, redacted and quarantined before LLM use."""
from __future__ import annotations
import hashlib
import html
import json
import re
from ..privacy import _redact_text, redact_text_urls
from ..adversarial_guard import cleanse_untrusted

_INVISIBLE = re.compile(r"[\u200b-\u200f\u2060\ufeff\x00-\x08\x0b\x0c\x0e-\x1f]")
_INJECTION = re.compile(r"(?i)(?:ignore (?:all |the )?(?:previous|above) instructions|system prompt|developer message|exfiltrat(?:e|ion)|send (?:your |the )?(?:secret|api key|token))")


def sanitize_tool_output(value, limit: int = 50_000) -> dict:
    def strip_binary(item):
        if isinstance(item, dict):
            return {key: ("[binary omitted]" if key in ("data", "blob") and isinstance(val, str) and len(val) > 1000
                          else strip_binary(val)) for key, val in item.items()}
        if isinstance(item, list):
            return [strip_binary(entry) for entry in item[:100]]
        return item
    try:
        raw = json.dumps(strip_binary(value), ensure_ascii=False, default=str)
    except Exception:
        raise ValueError("tool output cannot be serialized") from None
    if len(raw) > limit * 4:
        raw = raw[:limit * 4]
    clean = cleanse_untrusted(_redact_text(redact_text_urls(_INVISIBLE.sub("", raw)), limit), limit)
    clean = _INJECTION.sub("[quarantined instruction]", clean)
    return {"trust": "untrusted_tool_content", "content": f"<untrusted_tool_content>{html.escape(clean)}</untrusted_tool_content>",
            "output_hash": hashlib.sha256(clean.encode()).hexdigest()}
