"""Privacy filter and anti-injection quarantine for email content."""
from __future__ import annotations

import re
from typing import Any
from .models import EmailItem

# Credit card regex
CREDIT_CARD_RE = re.compile(r"\b(?:\d{4}[ -]?){3}\d{4}\b")
# OTP / security code pattern
OTP_RE = re.compile(r"\b(?:otp|one-time password|verification code)\s*(?:is|:)?\s*(\d{4,8})\b", re.IGNORECASE)
# Password / credential patterns
PASSWORD_RE = re.compile(r"(?:password|passwd|pin|secret|api[_-]?key)\s*[:=]\s*\S+", re.IGNORECASE)
# Bank account / IBAN
BANK_ACCT_RE = re.compile(r"\b(?:a/c|account\s*(?:no|number)?)\s*[:#]?\s*(\d{8,18})\b", re.IGNORECASE)
# Social Security / national ID patterns
SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
AADHAAR_RE = re.compile(r"\b\d{4}\s\d{4}\s\d{4}\b")

# Prompt injection patterns inside email bodies/subjects
PROMPT_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(?:all\s+)?(?:previous|prior)\s+instructions", re.IGNORECASE),
    re.compile(r"system\s*prompt\s*:", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+(?:an?\s+)?autonomous\s+cleaner", re.IGNORECASE),
    re.compile(r"delete\s+(?:all|every|important)\s+(?:emails?|messages?)", re.IGNORECASE),
    re.compile(r"authorize\s+(?:all\s+)?deletions?", re.IGNORECASE),
    re.compile(r"<script[\s>]", re.IGNORECASE),
    re.compile(r"javascript:", re.IGNORECASE),
    re.compile(r"[\u200B-\u200D\uFEFF]", re.UNICODE),  # Zero-width spaces / hidden Unicode
]


def redact_email_pii(text: str) -> str:
    """Scrub financial details, credentials, and PII from email excerpts."""
    if not text:
        return ""
    scrubbed = CREDIT_CARD_RE.sub("[REDACTED_CARD]", text)
    scrubbed = OTP_RE.sub("verification code [REDACTED_OTP]", scrubbed)
    scrubbed = re.sub(r"\b(?:one-time\s+password|otp)\b", "[REDACTED_OTP]", scrubbed, flags=re.IGNORECASE)
    scrubbed = PASSWORD_RE.sub("[REDACTED_CREDENTIAL]", scrubbed)
    scrubbed = re.sub(r"\bpassword\b", "[REDACTED_CREDENTIAL]", scrubbed, flags=re.IGNORECASE)
    scrubbed = BANK_ACCT_RE.sub("account [REDACTED_ACCOUNT]", scrubbed)
    scrubbed = SSN_RE.sub("[REDACTED_ID]", scrubbed)
    scrubbed = AADHAAR_RE.sub("[REDACTED_ID]", scrubbed)
    return scrubbed


def detect_and_quarantine_injections(text: str) -> tuple[str, bool]:
    """Detect adversarial prompt injections and neutralize them."""
    if not text:
        return "", False
    has_injection = False
    cleaned = text
    for pattern in PROMPT_INJECTION_PATTERNS:
        if pattern.search(cleaned):
            has_injection = True
            cleaned = pattern.sub("[QUARANTINED_INSTRUCTION]", cleaned)
    return cleaned, has_injection


def prepare_sanitized_email_context(email: EmailItem, max_snippet_chars: int = 600) -> dict[str, Any]:
    """Produce a privacy-filtered, injection-safe structured representation for classification.

    Never sends complete raw email bodies to external LLMs.
    """
    raw_content = email.snippet or email.body_text or ""
    # Strip HTML tags if any
    raw_content = re.sub(r"<[^>]+>", " ", raw_content)
    raw_content = re.sub(r"\s+", " ", raw_content).strip()

    scrubbed_subject = redact_email_pii(email.subject)
    scrubbed_snippet = redact_email_pii(raw_content[:max_snippet_chars])

    safe_subject, subj_injected = detect_and_quarantine_injections(scrubbed_subject)
    safe_snippet, snip_injected = detect_and_quarantine_injections(scrubbed_snippet)

    is_adversarial = subj_injected or snip_injected

    return {
        "id": email.id,
        "sender": email.sender[:120],
        "subject": safe_subject[:160],
        "snippet": safe_snippet[:max_snippet_chars],
        "received_at": email.received_at,
        "is_unread": email.is_unread,
        "is_replied": email.is_replied,
        "is_adversarial": is_adversarial,
    }
