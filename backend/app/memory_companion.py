"""Chat-only memory entry (2026-11-18): the "Remember a fact" form is
gone, so facts are learned from the conversation. When the operator
says "remember ..." in chat, this stores the fact directly via
SemanticMemory — the same store (and the same PII/credential
rejection) the form used — and confirms in the conversation. The
facts/preferences lists in the Memory view stay: they are the
transparency surface (what the agent knows, with Forget/Delete).
"""
from __future__ import annotations

import re
from typing import Optional

from .memory.tiers import SemanticMemory

semantic = SemanticMemory()

_REMEMBER_LEAD = re.compile(r"^\s*(?:please\s+)?remember\b\s*:?\s*", re.IGNORECASE)
# Recall questions ("do you remember ...?", "what do you remember?") are
# not store instructions — they ask what is already known.
_RECALL_LEAD = re.compile(
    r"^\s*(?:do you|can you|do u|what do you|what can you)\s+remember\b", re.IGNORECASE
)
# "remember to <verb> ..." is a reminder, not a fact to store.
_REMINDER_LEAD = re.compile(r"^\s*(?:please\s+)?remember\s+to\s+\w+", re.IGNORECASE)

_CODING_WORDS = (
    "code", "coding", "language", "programming", "python", "javascript",
    "typescript", "java", "c++", "rust", "go", "framework", "library",
    "ide", "editor", "terminal", "git", "database", "api", "frontend",
    "backend", "devops",
)
_RETAIL_WORDS = (
    "price", "store", "buy", "shopping", "order", "brand", "product",
    "delivery", "discount", "deal", "shop",
)


def is_remember_directive(text: str) -> bool:
    """True when the operator is telling the agent to store a fact."""
    if not text or not isinstance(text, str):
        return False
    lowered = text.strip().lower()
    if _RECALL_LEAD.match(text):
        return False
    if _REMINDER_LEAD.match(text):
        return False
    if not _REMEMBER_LEAD.match(text):
        return False
    # There must be a fact after "remember" — bare "remember" or
    # "remember this" with nothing attached is not storable.
    remainder = _REMEMBER_LEAD.sub("", text, count=1).strip(" .!\"'")
    if len(remainder) < 3 or remainder.lower() in ("this", "that", "it"):
        return False
    return True


def _parse_fact(text: str) -> tuple[str, str]:
    """Split a "remember ..." instruction into (key, value)."""
    remainder = _REMEMBER_LEAD.sub("", text, count=1).strip()
    # "remember that my preferred language is Python"
    remainder = re.sub(r"^that\s+", "", remainder, flags=re.IGNORECASE).strip()
    # "my preferred language is Python" / "preferred language is Python"
    match = re.match(r"^(?:my\s+)?(.+?)\s+is\s+(.+)$", remainder, re.IGNORECASE | re.DOTALL)
    if match:
        key, value = match.group(1).strip(), match.group(2).strip()
        return key[:80], value[:1000]
    # "preferred language: Python" / "preferred language = Python"
    match = re.match(r"^(.+?)\s*[:=]\s*(.+)$", remainder, re.DOTALL)
    if match:
        return match.group(1).strip()[:80], match.group(2).strip()[:1000]
    # Fallback: the whole remainder is the fact, filed under "Note".
    return "Note", remainder[:1000]


def _infer_category(key: str, value: str) -> str:
    haystack = f"{key} {value}".lower()
    if any(word in haystack for word in _CODING_WORDS):
        return "coding"
    if any(word in haystack for word in _RETAIL_WORDS):
        return "retail"
    return "personal"


def handle_remember_instruction(text: str, *, db_path: Optional[str] = None) -> str:
    """Store a fact from chat and confirm in the conversation."""
    key, value = _parse_fact(text)
    if not key or not value:
        return (
            "I didn't catch a fact to remember — tell me like \"remember my "
            "preferred language is Python\" and I'll store it."
        )
    category = _infer_category(key, value)
    try:
        semantic.remember(category, key, value, ttl_days=90, db_path=db_path)
    except ValueError as exc:
        # PII / credentials / invalid input: the store refuses, and so
        # does the chat path — the refusal is the same as the form's.
        return f"I can't store that: {exc}"
    display_key = key[0].upper() + key[1:] if key else key
    if key == "Note":
        remembered = f"Got it — I'll remember that: {value}."
    else:
        remembered = f"Got it — I'll remember that your {display_key.lower()} is {value}."
    return (
        f"{remembered} "
        f"({category.title()} · kept for 90 days.) You can see everything I "
        f"remember in the Memory view, or ask me to forget it any time."
    )
