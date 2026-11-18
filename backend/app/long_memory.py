"""Long-term operator memory (Task 9): durable facts, skills, files.

Three tiers, all explicit and operator-owned:

* **Facts** — user-stated durable facts ("my home airport is DEL",
  "I prefer window seats"). Stored with a source and a confidence.
* **Skills** — reusable learned procedures ("how I book a flight on
  site X"), stored as prose steps, versioned, with a success count.
* **Files** — user-attached reference files. The planner never sees a
  whole file: it gets a short summary and may request one excerpt.

Provenance rule (the whole point of this module): memory is NEVER
created from page content. A fact or skill may only come from the
operator directly (``source="user"``) or from a task's confirmed
outcomes that the operator explicitly confirmed
(``source="task:<id>:confirmed"``). Anything else is rejected in code.
Page text is untrusted data (Task 3 checker, Task 6 ASR 0.50) — letting
it write memory would turn every injection into a persistent one.

Storage reuses the Task 2 pattern: SQLite (``app.db``) with Fernet
encryption at the accessor layer for values, files under
``backend/data/files/`` (dir 0700, files 0600). No embeddings, no LLM:
recall is deterministic keyword scoring.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import db
from . import secrets as secrets_store
from .privacy import Sensitivity, classify_sensitivity, scrub_persistent

logger = logging.getLogger(__name__)

FACT_CATEGORIES = frozenset({"general", "personal", "retail", "coding", "travel"})
MAX_FACT_CHARS = 1000
MAX_SKILL_NAME_CHARS = 120
MAX_SKILL_STEPS_CHARS = 4000
MAX_FILE_SUMMARY_CHARS = 500
MAX_FILE_BYTES = 5 * 1024 * 1024

_FILES_DIR = Path(__file__).resolve().parents[1] / "data" / "files"

_SOURCE_RE = re.compile(r"^(?:user|task:[A-Za-z0-9_-]+:confirmed)$")

# Credential guard: the shared classifier (privacy.classify_sensitivity)
# only flags structured PII (email / card / phone). A bare password or
# API key ("password hunter2", "api key is abc123") scores as ordinary
# text there, so long-term memory gets its own explicit check.
#
# REVIEW (Task 9 audit): the first version of this guard was bypassable
# in at least eight ways, all verified by running the guard against a
# battery of inputs before this fix:
#   "password hunter" / "api key abcdef" (bare value, no digit),
#   "hunter2 is my password" (value BEFORE the keyword),
#   "pin 1234" (short numeric PIN, the old >= 6-char rule),
#   "my token is abc123" / "my secret is abc123" / "my bearer token is
#   xyz789" / "private key is abc123" / "seed phrase is ..." (keywords
#   missing from the list), "the password for my bank is 123456"
#   (keyword, filler words, then "is <value>"),
#   "password correcthorsebatterystaple" (long passphrase, no digit).
# The guard below closes those holes. Two genuinely ambiguous cases
# stay unflagged on purpose: "password hunter" (reads as a noun phrase,
# not a value) and "api key abcdef" (a 6-letter bare token with no digit
# is indistinguishable from an ordinary word). A value that looks like
# a secret is: >= 4 chars with a digit, >= 12 chars, mixed case >= 8,
# or carrying punctuation.
_CRED_RE = re.compile(
    r"\b(?:(?P<strong>password|passwd|pwd|api[-_ ]?key|client[-_ ]?secret"
    r"|access[-_ ]?token|auth[-_ ]?token|bearer(?:[-_ ]?token)?"
    r"|private[-_ ]?key|seed(?:[-_ ]?phrase)?|otp|pin)"
    r"|(?P<weak>token|secret))\b",
    re.IGNORECASE,
)
# A keyword followed by one of these nouns is a compound concept, not a
# value ("my password manager is 1Password", "api key rotation policy",
# "password reset link") — skip that occurrence, don't flag the sentence.
_CRED_NOUN_AFTER = frozenset({
    "manager", "reset", "rotation", "policy", "recovery", "change",
    "requirements", "requirement", "strength", "field", "prompt",
    "screen", "page", "login", "signin", "sign-in", "authentication",
    "verification", "hint", "question", "manager's",
})
_ASSIGNED_VALUE_RE = re.compile(r"^\s*[:=]", re.IGNORECASE)
_IS_VALUE_RE = re.compile(r"^\s+(?:is|are)\b", re.IGNORECASE)
_VALUE_TOKEN_RE = re.compile(r"^\s*(\S+)")
# "<value> is my/the <keyword>" — the value comes first.
_VALUE_BEFORE_RE = re.compile(
    r"\b([A-Za-z0-9._\-!@#$%^&*+]{4,})\s+(?:is|was)\s+(?:my\s+)?(?:the\s+)?"
    r"(?:password|passwd|pwd|api[-_ ]?key|client[-_ ]?secret|access[-_ ]?token"
    r"|auth[-_ ]?token|bearer(?:[-_ ]?token)?|private[-_ ]?key"
    r"|seed(?:[-_ ]?phrase)?|otp|pin|token|secret)\b(?:[.,;!?]|$)",
    re.IGNORECASE,
)


def _is_secret_like_token(token: str) -> bool:
    token = (token or "").strip(".,;:!?\"'()[]{}")
    if len(token) < 4:
        return False
    if any(c.isdigit() for c in token):
        return True
    if len(token) >= 12:
        return True
    if len(token) >= 8 and any(c.islower() for c in token) and any(c.isupper() for c in token):
        return True
    if len(token) >= 6 and any(not c.isalnum() and c not in "-_." for c in token):
        return True
    return False


def _looks_like_credential(text: str) -> bool:
    for match in _CRED_RE.finditer(text or ""):
        strong = match.group("strong") is not None
        rest = text[match.end():]
        nxt = re.match(r"\s*([A-Za-z]+)", rest)
        if nxt and nxt.group(1).lower() in _CRED_NOUN_AFTER:
            continue
        assigned = re.match(r"\s*[:=]\s*(\S+)", rest)
        if assigned:
            if strong:
                return True
            if _is_secret_like_token(assigned.group(1)):
                return True
            continue
        is_match = _IS_VALUE_RE.match(rest)
        if is_match:
            if strong:
                return True
            value = _VALUE_TOKEN_RE.match(rest[is_match.end():])
            if value and _is_secret_like_token(value.group(1)):
                return True
            continue
        if strong:
            # "the password for my bank is 123456": the keyword, a few
            # filler words, then "is <secret-like value>".
            far = re.match(r"(?:\s+[A-Za-z]{1,20}){1,5}?\s+(?:is|are)\s+(\S+)", rest)
            if far and _is_secret_like_token(far.group(1)):
                return True
        token = _VALUE_TOKEN_RE.match(rest)
        if token and _is_secret_like_token(token.group(1)):
            return True
    for match in _VALUE_BEFORE_RE.finditer(text or ""):
        if _is_secret_like_token(match.group(1)):
            return True
    return False


def _redact_credential_lines(text: str) -> str:
    """Redact the value out of credential-looking lines, keeping the
    label. Used for file excerpts: a file may legitimately contain a
    password line, and the excerpt path previously returned it verbatim
    (``scrub_persistent`` only redacts emails/phones/URLs)."""
    out: list[str] = []
    for line in (text or "").splitlines():
        before = _VALUE_BEFORE_RE.search(line)
        if before and _is_secret_like_token(before.group(1)):
            out.append(line[:before.start(1)] + "[REDACTED]" + line[before.end(1):])
            continue
        if _looks_like_credential(line):
            redacted = re.sub(
                r"((?:[:=]\s*)|(?:\b(?:is|are)\b\s+))\S+",
                r"\1[REDACTED]",
                line, count=1, flags=re.IGNORECASE,
            )
            out.append(redacted if redacted != line else "[credential redacted]")
        else:
            out.append(line)
    return "\n".join(out)


def _reject_credential_text(text: str, what: str) -> None:
    if _looks_like_credential(text):
        raise ValueError(
            f"{what} looks like a credential with a value — memory never "
            "stores secrets; rephrase without the value"
        )

_STOPWORDS = frozenset(
    "a an the and or of to in on for with me my i is are was were be been "
    "please do does did can could will would should it its this that these "
    "those from at by as not no yes if then than so such very just".split()
)


def _tokens(text: str) -> set[str]:
    return {
        t for t in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(t) >= 3 and t not in _STOPWORDS
    }


def _score(query_tokens: set[str], text_tokens: set[str]) -> float:
    """Deterministic relevance: shared-token count, ties broken by
    recency at the call site (rows are ordered newest-first)."""
    if not query_tokens:
        return 0.0
    return float(len(query_tokens & text_tokens))


def _validate_source(source: str) -> str:
    """The provenance gate. Only the operator, or a confirmed task
    outcome the operator explicitly approved, may create memory.
    ``"page"``, ``"observation"``, ``"planner"`` and anything else is
    rejected — page content can never write memory."""
    if not isinstance(source, str) or not _SOURCE_RE.fullmatch(source):
        raise ValueError(
            "invalid memory source: memory may only come from the operator "
            "('user') or a confirmed task outcome ('task:<id>:confirmed'); "
            "page content can never create a fact or skill"
        )
    return source


def _check_not_sensitive(text: str) -> None:
    if classify_sensitivity(text) >= Sensitivity.RESTRICTED_PII:
        raise ValueError("credentials and PII are not allowed in long-term memory")


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------

def create_fact(
    text: str,
    category: str = "general",
    source: str = "user",
    confidence: float = 1.0,
    *,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    text = (text or "").strip()
    if not text or len(text) > MAX_FACT_CHARS:
        raise ValueError("fact must be 1-1000 characters")
    if category not in FACT_CATEGORIES:
        raise ValueError(f"unknown fact category: {category}")
    _validate_source(source)
    if not 0.0 < confidence <= 1.0:
        raise ValueError("confidence must be in (0, 1]")
    _reject_credential_text(text, "fact")
    _check_not_sensitive(text)
    identifier = uuid.uuid4().hex
    now = time.time()
    db.init_db(db_path)
    with db.get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO long_memory_facts(id, text, category, source, confidence, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (identifier, secrets_store.encrypt_text(scrub_persistent(text)), category, source, confidence, now, now),
        )
        conn.commit()
    return {
        "id": identifier, "text": text, "category": category,
        "source": source, "confidence": confidence,
        "created_at": now, "updated_at": now,
    }


def _decrypt_column(value: Any, kind: str, row_id: str) -> str | None:
    """Decrypt one encrypted column, or return ``None`` when the row is
    undecryptable (lost/rotated master key, corrupt data).

    Task 13 — the old code let ``decrypt_text``'s ``ValueError`` escape
    from ``list_facts``/``list_skills``, so ONE bad row made the whole
    list endpoint 500 (Task 2 design flaw, found by the Task 10
    review). Only the LIST read degrades gracefully: the skip is
    counted and logged (row id only — never the value), and the row is
    dropped from the result. Writes stay fail-closed: a value that
    cannot be encrypted still raises at write time, and a single-row
    read (``get_fact``/``get_skill``-style) still raises rather than
    silently returning a partial row.
    """
    try:
        return secrets_store.decrypt_text(value)
    except ValueError:
        logger.warning(
            "long_memory: skipping undecryptable %s row %s "
            "(wrong master key or corrupt data; row kept in the "
            "database, excluded from list results)",
            kind, row_id,
        )
        return None


def list_facts_with_skips(
    category: Optional[str] = None, *, db_path: Optional[str] = None
) -> tuple[list[dict[str, Any]], int]:
    """Like :func:`list_facts`, plus the count of undecryptable rows
    skipped (see :func:`_decrypt_column`)."""
    db.init_db(db_path)
    with db.get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM long_memory_facts WHERE (? IS NULL OR category = ?) ORDER BY updated_at DESC",
            (category, category),
        ).fetchall()
    out = []
    skipped = 0
    for row in rows:
        d = dict(row)
        text = _decrypt_column(d.get("text"), "fact", str(d.get("id") or "?"))
        if text is None:
            skipped += 1
            continue
        d["text"] = text or ""
        out.append(d)
    return out, skipped


def list_facts(category: Optional[str] = None, *, db_path: Optional[str] = None) -> list[dict[str, Any]]:
    rows, _skipped = list_facts_with_skips(category, db_path=db_path)
    return rows


def recall_facts(
    instruction: str,
    limit: Optional[int] = None,
    max_chars: Optional[int] = None,
    *,
    db_path: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Top-K facts by keyword overlap with the instruction, bounded by
    a total char budget. Facts with no shared token are not recalled —
    an irrelevant fact in the prompt is noise the planner will act on."""
    from . import config

    limit = config.MEMORY_RECALL_FACTS if limit is None else limit
    max_chars = config.MEMORY_RECALL_MAX_CHARS if max_chars is None else max_chars
    query = _tokens(instruction)
    scored = []
    for fact in list_facts(db_path=db_path):
        score = _score(query, _tokens(fact["text"]))
        if score > 0:
            scored.append((score, fact["updated_at"], fact))
    scored.sort(key=lambda item: (-item[0], -item[1]))
    picked: list[dict[str, Any]] = []
    used = 0
    for _score_value, _updated, fact in scored:
        if len(picked) >= limit:
            break
        if used + len(fact["text"]) > max_chars and picked:
            continue
        picked.append(fact)
        used += len(fact["text"])
    return picked


def delete_fact(identifier: str, *, db_path: Optional[str] = None) -> bool:
    db.init_db(db_path)
    with db.get_connection(db_path) as conn:
        return conn.execute("DELETE FROM long_memory_facts WHERE id = ?", (identifier,)).rowcount > 0


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------

def create_skill(
    name: str,
    steps: str,
    domain: str = "",
    source: str = "user",
    *,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    name = (name or "").strip()
    steps = (steps or "").strip()
    if not name or len(name) > MAX_SKILL_NAME_CHARS:
        raise ValueError("skill name must be 1-120 characters")
    if not steps or len(steps) > MAX_SKILL_STEPS_CHARS:
        raise ValueError("skill steps must be 1-4000 characters")
    _validate_source(source)
    _reject_credential_text(name, "skill name")
    _reject_credential_text(steps, "skill steps")
    _check_not_sensitive(steps)
    identifier = uuid.uuid4().hex
    now = time.time()
    db.init_db(db_path)
    with db.get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO long_memory_skills(id, name, steps, domain, version, success_count, source, created_at, updated_at)"
            " VALUES(?,?,?,?,1,0,?,?,?)",
            (identifier, name, secrets_store.encrypt_text(scrub_persistent(steps)), domain.strip().lower(), source, now, now),
        )
        conn.commit()
    return {
        "id": identifier, "name": name, "steps": steps, "domain": domain.strip().lower(),
        "version": 1, "success_count": 0, "source": source,
        "created_at": now, "updated_at": now,
    }


def list_skills_with_skips(
    domain: Optional[str] = None, *, db_path: Optional[str] = None
) -> tuple[list[dict[str, Any]], int]:
    """Like :func:`list_skills`, plus the count of undecryptable rows
    skipped (see :func:`_decrypt_column`). Only ``steps`` is encrypted;
    ``name`` is plaintext and never causes a skip."""
    db.init_db(db_path)
    with db.get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM long_memory_skills WHERE (? IS NULL OR domain = ?) ORDER BY success_count DESC, updated_at DESC",
            (domain.lower() if domain else None, domain.lower() if domain else None),
        ).fetchall()
    out = []
    skipped = 0
    for row in rows:
        d = dict(row)
        steps = _decrypt_column(d.get("steps"), "skill", str(d.get("id") or "?"))
        if steps is None:
            skipped += 1
            continue
        d["steps"] = steps or ""
        out.append(d)
    return out, skipped


def list_skills(domain: Optional[str] = None, *, db_path: Optional[str] = None) -> list[dict[str, Any]]:
    rows, _skipped = list_skills_with_skips(domain, db_path=db_path)
    return rows


def recall_skills(
    instruction: str,
    limit: Optional[int] = None,
    *,
    db_path: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Top-K skills by keyword overlap with the instruction (name +
    steps), ties broken by success count — a skill that worked before
    outranks one that never ran."""
    from . import config

    limit = config.MEMORY_RECALL_SKILLS if limit is None else limit
    query = _tokens(instruction)
    scored = []
    for skill in list_skills(db_path=db_path):
        score = _score(query, _tokens(skill["name"] + " " + skill["steps"]))
        if score > 0:
            scored.append((score, skill["success_count"], skill["updated_at"], skill))
    scored.sort(key=lambda item: (-item[0], -item[1], -item[2]))
    return [item[3] for item in scored[:limit]]


def update_skill(identifier: str, steps: str, *, db_path: Optional[str] = None) -> Optional[dict[str, Any]]:
    steps = (steps or "").strip()
    if not steps or len(steps) > MAX_SKILL_STEPS_CHARS:
        raise ValueError("skill steps must be 1-4000 characters")
    _reject_credential_text(steps, "skill steps")
    _check_not_sensitive(steps)
    db.init_db(db_path)
    with db.get_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM long_memory_skills WHERE id = ?", (identifier,)).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE long_memory_skills SET steps = ?, version = version + 1, updated_at = ? WHERE id = ?",
            (secrets_store.encrypt_text(scrub_persistent(steps)), time.time(), identifier),
        )
        conn.commit()
    updated = [s for s in list_skills(db_path=db_path) if s["id"] == identifier]
    return updated[0] if updated else None


def record_skill_success(identifier: str, *, db_path: Optional[str] = None) -> bool:
    db.init_db(db_path)
    with db.get_connection(db_path) as conn:
        cur = conn.execute(
            "UPDATE long_memory_skills SET success_count = success_count + 1, updated_at = ? WHERE id = ?",
            (time.time(), identifier),
        )
        conn.commit()
        return cur.rowcount > 0


def delete_skill(identifier: str, *, db_path: Optional[str] = None) -> bool:
    db.init_db(db_path)
    with db.get_connection(db_path) as conn:
        return conn.execute("DELETE FROM long_memory_skills WHERE id = ?", (identifier,)).rowcount > 0


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def _files_dir() -> Path:
    _FILES_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(_FILES_DIR, 0o700)
    except OSError:
        pass
    return _FILES_DIR


def _safe_file_name(name: str) -> str:
    base = Path(name or "file").name.strip() or "file"
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base)[:120]
    return base or "file"


def save_file(
    file_name: str,
    b64: str,
    summary: str = "",
    task_id: str = "",
    *,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    try:
        raw = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("file content is not valid base64") from exc
    if not raw or len(raw) > MAX_FILE_BYTES:
        raise ValueError("file must be 1 byte to 5 MiB")
    safe_name = _safe_file_name(file_name)
    identifier = uuid.uuid4().hex
    stored_name = f"{identifier}_{safe_name}"
    stored_path = _files_dir() / stored_name
    stored_path.write_bytes(raw)
    try:
        os.chmod(stored_path, 0o600)
    except OSError:
        pass
    summary = (summary or "").strip()
    if not summary:
        try:
            text = raw[:400].decode("utf-8", errors="strict")
            summary = text.strip().replace("\n", " ")[:MAX_FILE_SUMMARY_CHARS] or f"{safe_name} ({len(raw)} bytes)"
        except UnicodeDecodeError:
            summary = f"{safe_name} ({len(raw)} bytes, binary)"
    summary = scrub_persistent(summary[:MAX_FILE_SUMMARY_CHARS])
    if _looks_like_credential(summary):
        # A summary derived from the file's first bytes must never carry
        # a credential into the prompt — fall back to a neutral label.
        summary = f"{safe_name} ({len(raw)} bytes)"
    now = time.time()
    db.init_db(db_path)
    with db.get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO long_memory_files(id, file_name, stored_path, size_bytes, mime_type, summary, task_id, created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (identifier, safe_name, str(stored_path), len(raw), "", summary, task_id, now),
        )
        conn.commit()
    return {
        "id": identifier, "file_name": safe_name, "size_bytes": len(raw),
        "summary": summary, "task_id": task_id, "created_at": now,
    }


def list_files(*, db_path: Optional[str] = None) -> list[dict[str, Any]]:
    """Metadata only — never file content. The planner gets summaries;
    content is available only through file_excerpt, scrubbed."""
    db.init_db(db_path)
    with db.get_connection(db_path) as conn:
        rows = conn.execute("SELECT * FROM long_memory_files ORDER BY created_at DESC").fetchall()
    return [dict(row) for row in rows]


def file_excerpt(file_id: str, max_chars: int = 2000, *, db_path: Optional[str] = None) -> Optional[str]:
    db.init_db(db_path)
    with db.get_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM long_memory_files WHERE id = ?", (file_id,)).fetchone()
    if not row:
        return None
    path = Path(row["stored_path"])
    if not path.exists():
        return None
    try:
        text = path.read_bytes()[: max_chars * 2].decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    # REVIEW (Task 9 audit): scrub_persistent only redacts emails,
    # phones and URLs — a "password: hunter2" line inside a file came
    # back verbatim in the excerpt and would go straight into the
    # planner prompt. Redact credential-looking lines as well.
    return _redact_credential_lines(scrub_persistent(text[:max_chars]))


def delete_file(file_id: str, *, db_path: Optional[str] = None) -> bool:
    db.init_db(db_path)
    with db.get_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM long_memory_files WHERE id = ?", (file_id,)).fetchone()
        if not row:
            return False
        conn.execute("DELETE FROM long_memory_files WHERE id = ?", (file_id,))
        conn.commit()
    try:
        Path(row["stored_path"]).unlink(missing_ok=True)
    except OSError:
        pass
    return True


def _clear_all_proposals(*, db_path: Optional[str] = None) -> int:
    """Strip pending memory proposals from every task checkpoint.

    Proposals are not facts — but after a "forget everything" they
    must not survive to be confirmed later and re-create the very
    memory the operator just wiped. Returns how many checkpoints had
    proposals removed."""
    from . import db as db_module

    db_module.init_db(db_path)
    with db_module.get_connection(db_path) as conn:
        task_ids = [r["task_id"] for r in conn.execute("SELECT task_id FROM task_checkpoints").fetchall()]
    cleared = 0
    for task_id in task_ids:
        cp = db_module.get_task_checkpoint(task_id, db_path=db_path)
        if not cp:
            continue
        wm = dict(cp.get("working_memory") or {})
        if "memory_proposals" not in wm:
            continue
        wm.pop("memory_proposals")
        db_module.save_task_checkpoint(
            task_id=task_id,
            conversation_id=cp.get("conversation_id") or "default",
            session_id=cp.get("session_id"),
            step_index=cp.get("step_index", 0),
            last_url=cp.get("last_url") or "",
            history=cp.get("history") or [],
            working_memory=wm,
            db_path=db_path,
        )
        cleared += 1
    return cleared


def forget_all(*, db_path: Optional[str] = None) -> dict[str, int]:
    """Wipe every long-memory tier for the operator (facts, skills,
    files on disk).

    REVIEW (Task 9 audit): the first version wiped only the three
    long tiers, while the UI button says "Forget everything" and the
    status line claimed "all memory tiers are empty". Two things
    survived: the semantic-memory tier (``/api/memory/facts``,
    ``user_semantic_facts``) and pending memory proposals sitting in
    task checkpoints, which could be confirmed afterwards and re-create
    wiped memory. Both are now wiped too. Per-chat session working
    memory (``chat_memory``) is deliberately NOT wiped: it is the
    working state of open chats, not durable memory about the
    operator, and wiping it would destroy resume context."""
    files = list_files(db_path=db_path)
    db.init_db(db_path)
    with db.get_connection(db_path) as conn:
        facts = conn.execute("DELETE FROM long_memory_facts").rowcount
        skills = conn.execute("DELETE FROM long_memory_skills").rowcount
        semantic_facts = conn.execute("DELETE FROM user_semantic_facts").rowcount
        conn.execute("DELETE FROM long_memory_files")
        conn.commit()
    proposals = _clear_all_proposals(db_path=db_path)
    for f in files:
        try:
            Path(f["stored_path"]).unlink(missing_ok=True)
        except OSError:
            pass
    return {"facts": facts, "skills": skills, "files": len(files),
            "semantic_facts": semantic_facts, "proposals": proposals}


# ---------------------------------------------------------------------------
# Proposals: task outcomes -> operator-confirmed memory
# ---------------------------------------------------------------------------

_PREFERENCE_RE = re.compile(
    r"\b(?:i prefer|i always|i like|my home airport is|my preferred|please always|never book|avoid)\b[^.!?]{0,120}",
    re.IGNORECASE,
)


def propose_from_task(
    task_id: str,
    instruction: str,
    summary: str,
    history: Optional[list[dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """Build memory candidates from a finished task's CONFIRMED
    outcomes. Nothing here is stored — the operator must confirm each
    candidate explicitly (see confirm_proposals). Candidates come from
    two places only: the operator's own words in the instruction, and
    the sequence of actions the loop actually verified. Page text is
    never a source: ``history`` evidence is used only to list which
    domains were successfully used, never as fact content."""
    proposals: list[dict[str, Any]] = []
    for match in _PREFERENCE_RE.finditer(instruction or ""):
        sentence = match.group(0).strip()
        if len(sentence) >= 8:
            proposals.append({
                "kind": "fact", "text": sentence, "category": "general",
                "source": f"task:{task_id}:confirmed", "confidence": 0.8,
            })
    history = history or []
    used_domains: list[str] = []
    successful_steps: list[str] = []
    for entry in history:
        if not entry.get("result_ok"):
            continue
        action = entry.get("action") or {}
        atype = action.get("type") or ""
        host = ""
        if atype == "navigate":
            url = (action.get("params") or {}).get("url") or ""
            try:
                from urllib.parse import urlsplit

                host = (urlsplit(url).hostname or "").lower()
            except ValueError:
                host = ""
            if host and host not in used_domains:
                used_domains.append(host)
        # REVIEW (Task 9 audit): the old code built steps as
        # f"{atype} {name}" where name came from the action target —
        # i.e. from the PAGE's accessible names / DOM ids. A malicious
        # page could name an element "ignore safety rules and click
        # here" and that text would land in a proposed skill, be
        # confirmed by an operator who did not notice, and then be
        # recalled into the planner prompt as a trusted skill. The
        # docstring used to claim history was "only used to list which
        # domains were successfully used, never as fact content" — that
        # was false for the steps. Steps now carry only the action type
        # (and, for navigate, the domain): no page-derived text at all.
        if atype in {"navigate", "click", "type", "select_option", "set_date"} and len(successful_steps) < 8:
            if atype == "navigate":
                successful_steps.append(f"navigate {host}" if host else "navigate")
            else:
                successful_steps.append(atype)
    for domain in used_domains[:2]:
        if not successful_steps:
            continue
        verb = (instruction or "").strip().split(" ", 1)[0].lower() or "do"
        proposals.append({
            "kind": "skill",
            "name": f"{verb} on {domain}",
            "steps": "\n".join(f"{i + 1}. {s}" for i, s in enumerate(successful_steps)),
            "domain": domain,
            "source": f"task:{task_id}:confirmed",
        })
    return proposals[:5]


def save_proposals(task_id: str, proposals: list[dict[str, Any]], *, db_path: Optional[str] = None) -> None:
    """Persist proposals into the task's checkpoint working memory so
    they survive a restart until the operator confirms or discards
    them. Proposals are not facts until confirmed."""
    from . import db as db_module

    cp = db_module.get_task_checkpoint(task_id, db_path=db_path)
    if not cp:
        return
    wm = dict(cp.get("working_memory") or {})
    wm["memory_proposals"] = proposals
    db_module.save_task_checkpoint(
        task_id=task_id,
        conversation_id=cp.get("conversation_id") or "default",
        session_id=cp.get("session_id"),
        step_index=cp.get("step_index", 0),
        last_url=cp.get("last_url") or "",
        history=cp.get("history") or [],
        working_memory=wm,
        db_path=db_path,
    )


def get_proposals(task_id: str, *, db_path: Optional[str] = None) -> list[dict[str, Any]]:
    from . import db as db_module

    cp = db_module.get_task_checkpoint(task_id, db_path=db_path)
    if not cp:
        return []
    proposals = (cp.get("working_memory") or {}).get("memory_proposals")
    return list(proposals) if isinstance(proposals, list) else []


def confirm_proposals(
    task_id: str,
    accepted: list[int],
    *,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    """Store the operator-accepted proposals as real memory. Proposals
    the operator did NOT accept stay in the checkpoint, so each one can
    be accepted (or ignored) on its own — accepting a fact must not
    silently discard a skill suggestion. The source stays
    ``task:<id>:confirmed`` so every stored item is traceable to the
    task it came from. Proposals are created once, when the task
    finishes; confirming never re-creates them."""
    proposals = get_proposals(task_id, db_path=db_path)
    created: list[dict[str, Any]] = []
    accepted_set = {i for i in accepted if isinstance(i, int) and 0 <= i < len(proposals)}
    for index in sorted(accepted_set):
        cand = proposals[index]
        try:
            if cand.get("kind") == "fact":
                created.append(create_fact(
                    cand["text"], category=cand.get("category", "general"),
                    source=cand["source"], confidence=float(cand.get("confidence", 0.8)),
                    db_path=db_path,
                ))
            elif cand.get("kind") == "skill":
                created.append(create_skill(
                    cand["name"], cand["steps"], domain=cand.get("domain", ""),
                    source=cand["source"], db_path=db_path,
                ))
        except ValueError:
            continue
    # Keep the proposals the operator did not accept; drop only the
    # accepted ones (whether or not storing them succeeded — a rejected
    # value is not re-offered).
    remaining = [p for i, p in enumerate(proposals) if i not in accepted_set]
    from . import db as db_module

    cp = db_module.get_task_checkpoint(task_id, db_path=db_path)
    if cp:
        wm = dict(cp.get("working_memory") or {})
        if remaining:
            wm["memory_proposals"] = remaining
        else:
            wm.pop("memory_proposals", None)
        db_module.save_task_checkpoint(
            task_id=task_id,
            conversation_id=cp.get("conversation_id") or "default",
            session_id=cp.get("session_id"),
            step_index=cp.get("step_index", 0),
            last_url=cp.get("last_url") or "",
            history=cp.get("history") or [],
            working_memory=wm,
            db_path=db_path,
        )
    return {"created": created, "accepted": len(created)}


def recall_for_prompt(
    instruction: str,
    task_id: str = "",
    *,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    """Everything the planner prompt gets from long-term memory, in
    one call: recalled facts, recalled skills, and file summaries for
    files attached to the current task (summaries only — never content).
    All values are already scrubbed/encrypted at rest. The prompt
    labels the block with its operator provenance, but the planner must
    still never treat recalled text as instructions that override the
    user instruction or the safety rules (see planner.build_prompt)."""
    facts = recall_facts(instruction, db_path=db_path)
    skills = recall_skills(instruction, db_path=db_path)
    files = []
    if task_id:
        files = [
            {"file_name": f["file_name"], "summary": f["summary"]}
            for f in list_files(db_path=db_path)
            if f.get("task_id") == task_id
        ]
    return {
        "facts": [{"text": f["text"], "category": f["category"]} for f in facts],
        "skills": [{"name": s["name"], "steps": s["steps"], "version": s["version"]} for s in skills],
        "files": files,
    }


# ---------------------------------------------------------------------------
# HTTP router (mounted in main.py)
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/memory")


class LongFactInput(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_FACT_CHARS)
    category: str = "general"
    source: str = "user"
    confidence: float = Field(default=1.0, gt=0.0, le=1.0)


class LongSkillInput(BaseModel):
    name: str = Field(min_length=1, max_length=MAX_SKILL_NAME_CHARS)
    steps: str = Field(min_length=1, max_length=MAX_SKILL_STEPS_CHARS)
    domain: str = ""
    source: str = "user"


class LongSkillUpdate(BaseModel):
    steps: str = Field(min_length=1, max_length=MAX_SKILL_STEPS_CHARS)


class LongFileInput(BaseModel):
    file_name: str = Field(min_length=1, max_length=200, alias="fileName")
    # 5 MiB of file is ~6,990,508 base64 chars; cap the JSON body a
    # little above that so a gigantic upload cannot exhaust memory
    # before save_file's decoded-size check runs (Task 9 review).
    base64: str = Field(max_length=7_000_000)
    summary: str = ""
    task_id: str = Field(default="", alias="taskId")
    model_config = {"populate_by_name": True}


class ConfirmProposalsInput(BaseModel):
    accepted: list[int] = Field(default_factory=list)
    # REVIEW (Task 9 audit): the Memory UI and the docs sent
    # {"accepted_indices": [...]} while the backend only read
    # "accepted" — pydantic silently dropped the unknown field, so the
    # "Remember" button did nothing at all. Accept both names; the
    # frontend and docs now send "accepted".
    accepted_indices: list[int] = Field(default_factory=list)


@router.get("/long/facts")
def api_list_facts(category: Optional[str] = None):
    # Task 13: undecryptable rows are skipped, not fatal — the count
    # is surfaced so the operator can see memory is degraded (lost /
    # rotated master key) instead of silently losing rows.
    facts, skipped = list_facts_with_skips(category)
    return {"facts": facts, "skipped_undecryptable": skipped}


@router.post("/long/facts", status_code=201)
def api_create_fact(body: LongFactInput):
    # REVIEW (Task 9 audit): the API used to pass any caller-supplied
    # source through, so any local client could forge
    # "task:<id>:confirmed" provenance on a fact it invented. Direct
    # creation is operator-only; task-confirmed memory is created only
    # by confirming a proposal (confirm_proposals).
    if body.source != "user":
        raise HTTPException(
            422, "direct creation is operator-only ('user'); task-confirmed "
            "memory is created only by confirming a proposal")
    try:
        fact = create_fact(body.text, category=body.category, source="user", confidence=body.confidence)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    # Task 10: audit — memory writes (category + id only, never the text).
    try:
        from .audit import append_event

        append_event("memory_write", f"fact created in category {body.category}",
                     task_id=None)
    except Exception:
        pass
    return fact


@router.delete("/long/facts/{identifier}")
def api_delete_fact(identifier: str):
    if not delete_fact(identifier):
        raise HTTPException(404, "fact not found")
    try:
        from .audit import append_event

        append_event("memory_delete", "fact deleted")
    except Exception:
        pass
    return {"ok": True}


@router.get("/long/skills")
def api_list_skills(domain: Optional[str] = None):
    # Task 13: see api_list_facts — undecryptable rows are skipped and
    # counted, never fatal to the whole list.
    skills, skipped = list_skills_with_skips(domain)
    return {"skills": skills, "skipped_undecryptable": skipped}


@router.post("/long/skills", status_code=201)
def api_create_skill(body: LongSkillInput):
    # Same provenance rule as facts: direct creation is operator-only.
    if body.source != "user":
        raise HTTPException(
            422, "direct creation is operator-only ('user'); task-confirmed "
            "memory is created only by confirming a proposal")
    try:
        skill = create_skill(body.name, body.steps, domain=body.domain, source="user")
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    try:
        from .audit import append_event

        append_event("memory_write", f"skill created in domain {body.domain or 'general'}")
    except Exception:
        pass
    return skill


@router.patch("/long/skills/{identifier}")
def api_update_skill(identifier: str, body: LongSkillUpdate):
    try:
        updated = update_skill(identifier, body.steps)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if updated is None:
        raise HTTPException(404, "skill not found")
    # REVIEW (Task 10 audit): a skill update is a memory write — it
    # rewrites stored steps — but it emitted no audit event.
    try:
        from .audit import append_event

        append_event("memory_write", "skill updated")
    except Exception:
        pass
    return updated


@router.post("/long/skills/{identifier}/success")
def api_skill_success(identifier: str):
    if not record_skill_success(identifier):
        raise HTTPException(404, "skill not found")
    # REVIEW (Task 10 audit): the success counter is memory state;
    # record the mutation like any other memory write.
    try:
        from .audit import append_event

        append_event("memory_write", "skill success recorded")
    except Exception:
        pass
    return {"ok": True}


@router.delete("/long/skills/{identifier}")
def api_delete_skill(identifier: str):
    if not delete_skill(identifier):
        raise HTTPException(404, "skill not found")
    try:
        from .audit import append_event

        append_event("memory_delete", "skill deleted")
    except Exception:
        pass
    return {"ok": True}


@router.get("/long/files")
def api_list_files():
    return {"files": list_files()}


@router.post("/long/files", status_code=201)
def api_save_file(body: LongFileInput):
    try:
        saved = save_file(body.file_name, body.base64, summary=body.summary, task_id=body.task_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    # REVIEW (Task 10 audit): saving a reference file is a memory
    # write, but it emitted no audit event (only its deletion did).
    try:
        from .audit import append_event

        append_event("memory_write", "reference file saved")
    except Exception:
        pass
    return saved


@router.get("/long/files/{identifier}/excerpt")
def api_file_excerpt(identifier: str, max_chars: int = 2000):
    excerpt = file_excerpt(identifier, max_chars=max(1, min(max_chars, 8000)))
    if excerpt is None:
        raise HTTPException(404, "file not found or not readable as text")
    return {"excerpt": excerpt}


@router.delete("/long/files/{identifier}")
def api_delete_file(identifier: str):
    if not delete_file(identifier):
        raise HTTPException(404, "file not found")
    try:
        from .audit import append_event

        append_event("memory_delete", "reference file deleted")
    except Exception:
        pass
    return {"ok": True}


@router.get("/long/proposals/{task_id}")
def api_get_proposals(task_id: str):
    return {"taskId": task_id, "proposals": get_proposals(task_id)}


@router.post("/long/proposals/{task_id}/confirm")
def api_confirm_proposals(task_id: str, body: ConfirmProposalsInput):
    result = confirm_proposals(task_id, list(body.accepted) + list(body.accepted_indices))
    # Task 10: audit — confirmed proposals write task-confirmed memory.
    try:
        from .audit import append_event

        n_accepted = result.get("accepted", 0) if isinstance(result, dict) else 0
        if n_accepted:
            append_event("memory_write",
                         f"{n_accepted} proposal(s) confirmed into memory",
                         task_id=task_id)
    except Exception:
        pass
    return result


@router.delete("")
def api_forget_all():
    """Forget everything in long-term memory (facts, skills, files)
    plus the semantic-memory tier and any pending memory proposals,
    so nothing survives to be re-confirmed afterwards."""
    result = forget_all()
    # Task 10: audit — forget-all is a destructive, security-relevant
    # event; the audit log itself is NOT wiped by it (it is the record
    # that the wipe happened).
    try:
        from .audit import append_event

        counts = ", ".join(f"{k}={v}" for k, v in sorted(result.items()))
        append_event("memory_forget_all", f"forget everything: {counts}")
    except Exception:
        pass
    return {"deleted": result}
