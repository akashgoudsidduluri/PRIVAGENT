"""Append-only, hash-chained audit log (Task 10).

Security-relevant events — auth failures, allow-list denials,
confirmation decisions, takeover start/end, payment attempts, memory
writes/deletes, forget-all, scheduler job claims — are appended as one
JSON object per line to ``backend/data/audit.log`` (dir 0700, file
0600). Each entry carries the SHA-256 hash of the previous entry, so
any later edit, deletion (other than truncating the tail — see below),
or reordering is detectable with :func:`verify_chain`.

Honest limit: a backward-only hash chain cannot detect truncation of
the *last* entries — deleting the final lines leaves a shorter but
still-valid chain. The log proves later modification of surviving
entries, not that no entries were removed from the end, and it
assumes the operator's filesystem is trusted (same assumption as the
encrypted secret store). An attacker with write access could rewrite
the whole file, chain included.

Summaries are scrubbed before they are written: URLs are redacted and
credential-like / PII-like values are removed (see
:func:`_scrub_summary`), and summaries are truncated. An audit entry
must never become a second copy of a secret. Appending never raises:
if the log cannot be written, the failure is logged to the standard
logger and the caller's flow continues — a broken audit trail must
not break the task it records.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("navigator.audit")

_lock = threading.Lock()

_GENESIS_PREV = "0" * 64
_MAX_SUMMARY_CHARS = 500

# The shared redactor misses bare credential values ("password
# hunter2" — Task 9 review found the same gap in memory writes). The
# audit log must never carry a secret, so a credential-kind word
# followed by a value is redacted here even without a ":" / "=" /
# "is" separator. Over-redaction is safe in an audit summary: call
# sites pass metadata (ids, counts, decisions), never free text.
_CRED_KIND_VALUE_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|api[-_ ]?key|client[-_ ]?secret|secret|token|otp|pin|private[-_ ]?key)\b"
    r"(\s*(?:[:=]|\bis\b|\bare\b)?\s*)(\S{4,})"
)


def _default_log_path() -> Path:
    try:
        from . import config

        return Path(config.AUDIT_LOG_PATH)
    except Exception:
        return Path(__file__).resolve().parent.parent / "data" / "audit.log"


def scrub_summary(summary: str) -> str:
    """Remove anything secret- or PII-like from a log/trajectory summary.

    Public so the trajectory writer (zero_retention) can apply the
    same scrub: summaries are metadata, never free-form page text."""
    text = str(summary or "")
    try:
        from .privacy import redact_text_urls, scrub_persistent

        # scrub_persistent redacts credential/PII patterns in bare
        # text; redact_text_urls strips secrets from query strings.
        # Neither is perfect (Task 6 measured a 0.26 leak rate on the
        # shared redactor), so summaries are also kept short and must
        # be written as metadata (ids, counts, decisions) — never as
        # free-form page text — by the call sites.
        text = str(scrub_persistent(text))
        text = redact_text_urls(text)
    except Exception:
        pass
    # Bare credential values the shared redactor misses ("password
    # hunter2", "api key abc123") — redact the value, keep the label.
    try:
        text = _CRED_KIND_VALUE_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)
    except Exception:
        pass
    return text[:_MAX_SUMMARY_CHARS]


def _entry_hash(prev_hash: str, canonical: str) -> str:
    return hashlib.sha256(f"{prev_hash}\n{canonical}".encode("utf-8")).hexdigest()


def _ensure_log_path(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except Exception:
        pass


def _last_hash(path: Path) -> str:
    """Hash of the last entry, or the genesis value for a new log."""
    try:
        if not path.exists():
            return _GENESIS_PREV
        last = ""
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.strip():
                    last = line
        if not last:
            return _GENESIS_PREV
        return str(json.loads(last).get("hash") or _GENESIS_PREV)
    except Exception:
        return _GENESIS_PREV


def append_event(
    event_type: str,
    summary: str,
    *,
    actor: str = "operator",
    task_id: Optional[str] = None,
    log_path: Optional[Path | str] = None,
) -> Optional[dict[str, Any]]:
    """Append one audit event. Returns the entry, or None on failure.

    Never raises. ``event_type`` is a short snake_case name
    (``auth_failure``, ``confirmation_denied``, ...). ``summary`` is
    scrubbed and truncated; call sites must pass metadata, never page
    text, credentials, or raw user input.
    """
    path = Path(log_path) if log_path is not None else _default_log_path()
    entry: dict[str, Any] = {
        "ts": time.time(),
        "event": str(event_type),
        "actor": str(actor),
        "task_id": task_id,
        "summary": scrub_summary(summary),
    }
    try:
        with _lock:
            _ensure_log_path(path)
            prev = _last_hash(path)
            canonical = json.dumps(entry, sort_keys=True, separators=(",", ":"))
            entry["prev"] = prev
            entry["hash"] = _entry_hash(prev, canonical)
            line = json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n"
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)
            try:
                path.chmod(0o600)
            except Exception:
                pass
        return entry
    except Exception as exc:  # noqa: BLE001 — audit must never break a task
        logger.warning("audit log append failed (%s); event dropped: %s", exc, event_type)
        return None


def read_events(
    log_path: Optional[Path | str] = None,
    *,
    event: Optional[str] = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Read audit entries (oldest first), optionally filtered by type.

    ``limit`` caps the number returned, keeping the most recent ones.
    A corrupt line is skipped, not fatal — verify_chain is the tool
    that reports corruption.
    """
    path = Path(log_path) if log_path is not None else _default_log_path()
    entries: list[dict[str, Any]] = []
    try:
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    if event:
        entries = [e for e in entries if e.get("event") == event]
    if limit is not None and limit >= 0:
        entries = entries[-limit:] if limit else []
    return entries


def verify_chain(log_path: Optional[Path | str] = None) -> tuple[bool, Optional[int]]:
    """Verify the hash chain. Returns ``(ok, broken_index)``.

    ``broken_index`` is the 0-based index of the first entry whose
    hash does not match its contents or whose ``prev`` does not match
    the previous entry's hash; ``None`` when the chain is intact. An
    empty or missing log verifies as intact.
    """
    path = Path(log_path) if log_path is not None else _default_log_path()
    try:
        if not path.exists():
            return True, None
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            lines = [ln.strip() for ln in fh if ln.strip()]
    except Exception:
        return False, 0
    prev = _GENESIS_PREV
    for idx, line in enumerate(lines):
        try:
            entry = json.loads(line)
        except Exception:
            return False, idx
        stored_hash = entry.get("hash")
        stored_prev = entry.get("prev")
        if stored_prev != prev:
            return False, idx
        unsigned = {k: v for k, v in entry.items() if k not in ("prev", "hash")}
        canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":"))
        if _entry_hash(prev, canonical) != stored_hash:
            return False, idx
        prev = stored_hash
    return True, None
