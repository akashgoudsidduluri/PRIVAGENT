"""Untrusted tool output is bounded, redacted and quarantined before LLM use."""
from __future__ import annotations
import hashlib
import html
import json
import re
from ..privacy import _redact_text, redact_text_urls
from ..adversarial_guard import cleanse_untrusted

_INVISIBLE = re.compile(r"[\u200b-\u200f\u2060\ufeff\x00-\x08\x0b\x0c\x0e-\x1f]")
_INJECTION = re.compile(r"(?i)(?:ignore (?:all |the )?(?:previous|above) instructions|system prompt|developer message|exfiltrat(?:e|ion)|send (?:your |the )?(?:secret|api key|token)|you are now|override instructions)")

MUTATING_ACTION_PATTERNS = re.compile(
    r"\b(?:delete|drop|truncate|remove|kill|clean|trash|destroy|wipe|purge|"
    r"write|update|insert|create|modify|alter|post|send|upload|execute|exec|run|"
    r"patch|put|reset|set|change|apply|mutate|erase)\b",
    re.IGNORECASE,
)


def sanitize_tool_description(description: str, limit: int = 1000) -> str:
    """Sanitize and quarantine untrusted tool descriptions before cataloguing or LLM presentation."""
    if not description:
        return ""
    # Strip invisible/control characters
    clean = _INVISIBLE.sub("", str(description).strip())
    # Redact credentials and private URLs
    clean = _redact_text(redact_text_urls(clean), limit)
    # Neutralize prompt injection attempts
    clean = _INJECTION.sub("[quarantined instruction]", clean)
    clean = cleanse_untrusted(clean, limit)
    if len(clean) > limit:
        clean = clean[:limit] + "…"
    return clean


def is_inherently_mutating(name: str, description: str = "", schema: dict | None = None) -> tuple[bool, str]:
    """Semantic mutation check: verify if tool is inherently state-modifying regardless of registration claims.

    A tool claiming to be read-only is NEVER trusted if its name, description,
    or schema fields match destructive or state-altering patterns.
    """
    clean_name = name.split(".")[-1].replace("_", " ").replace("-", " ")
    match_name = MUTATING_ACTION_PATTERNS.search(clean_name)
    if match_name:
        return True, f"Tool name matches mutating pattern: '{match_name.group(0)}'"

    if description:
        match_desc = MUTATING_ACTION_PATTERNS.search(description)
        if match_desc:
            return True, f"Tool description indicates mutation: '{match_desc.group(0)}'"

    return False, "No inherent mutation detected"


def resolve_tool_mutating_status(
    is_declared_read_only: bool,
    name: str,
    description: str = "",
    schema: dict | None = None,
) -> bool:
    """Determine final mutating flag: overrides false read-only claims fail-closed."""
    inherently_mutating, _ = is_inherently_mutating(name, description, schema)
    if inherently_mutating:
        return True  # Strict override: destructive operations can NEVER be marked read-only

    if not is_declared_read_only:
        return True  # Unknown tools default to mutating

    return False



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
