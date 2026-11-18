"""Credential vault with placeholders (Task 4).

The agent must be able to *use* a saved login without ever *seeing*
the password. The pattern (borrowed from Browser Use's ``sensitive_data``)
is:

* The operator enters a credential once, in the workspace UI (never in
  chat, never in a task instruction). It is sent once over the
  authenticated local API and stored encrypted in the Task 2 secret
  store (Fernet + PBKDF2, ``backend/data/secrets.enc.json``, mode 0600).
* The planner / LLM sees only a placeholder, e.g.
  ``{{CREDENTIAL:example.com:login:default}}``. Placeholders survive
  the privacy redaction untouched (see :mod:`app.privacy`) and are the
  only form a credential ever takes in a prompt, a log, a trace, or
  task history.
* The real value is substituted at DOM-execution time only — in the
  agent loop, after the confirmation gate, just before the action is
  sent to the extension — and the substituted value is scrubbed out
  of the action evidence before anything is recorded.

What is stored, and what is not
--------------------------------
* ``login``: a username + password for one domain. The password is
  stored encrypted; the username is metadata (it is not a secret on
  its own, but it is still only returned as metadata, never in a
  planner payload).
* ``card``: **last-4 digits only**. A full card number (PAN) is never
  stored, never typed by the agent, and never sent to a planner. If
  you need to pay with a card, do it yourself in takeover mode.
* ``otp`` / ``totp`` / one-time codes: **never stored, ever**. Adding
  one is rejected. When an OTP field is detected the loop forces
  takeover instead (see :mod:`app.takeover`).

The vault never logs a value. Listing returns metadata only
(domain, kind, label, username, last-4, created date) — never the
stored secret.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from . import secrets as secrets_store

__all__ = [
    "CREDENTIAL_PLACEHOLDER_RE",
    "VAULT_KINDS",
    "CredentialMeta",
    "placeholder",
    "parse_placeholder",
    "placeholder_value",
    "contains_placeholder",
    "add_credential",
    "get_credential_meta",
    "list_credentials",
    "delete_credential",
    "resolve_placeholder",
    "substitute_placeholders",
    "substitute_placeholders_in_action",
    "scrub_value_from_text",
]

VAULT_KINDS = ("login", "card")

#: Never-stored kinds. Rejecting these at the API boundary is the
#: defence; the loop's OTP detection is the second layer.
REJECTED_KINDS = ("otp", "totp", "one-time-code", "one_time_code", "verification-code")

_STORE_PREFIX = "vault:"

CREDENTIAL_PLACEHOLDER_RE = re.compile(
    r"\{\{CREDENTIAL:([A-Za-z0-9._-]+):(login|card):([A-Za-z0-9][A-Za-z0-9_-]{0,63})\}\}"
)

_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,}$"
)
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_LAST4_RE = re.compile(r"^\d{4}$")


def _normalise_domain(domain: str) -> str:
    domain = (domain or "").strip().lower().rstrip(".")
    if not _DOMAIN_RE.match(domain):
        raise ValueError(f"invalid domain for vault credential: {domain!r}")
    return domain


def _validate_label(label: str) -> str:
    label = (label or "").strip()
    if not _LABEL_RE.match(label):
        raise ValueError(
            "credential label must start with a letter/digit and contain only "
            "letters, digits, '-' and '_' (max 64 chars)"
        )
    return label


def _store_name(domain: str, kind: str, label: str) -> str:
    return f"{_STORE_PREFIX}{domain}:{kind}:{label}"


@dataclass
class CredentialMeta:
    """Metadata only — never carries the stored secret value."""

    domain: str
    kind: str
    label: str
    username: Optional[str] = None
    last4: Optional[str] = None
    created_at: Optional[str] = None

    @property
    def placeholder(self) -> str:
        """The only form this credential ever takes outside the vault."""
        return placeholder(self.domain, self.kind, self.label)

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "kind": self.kind,
            "label": self.label,
            "username": self.username,
            "last4": self.last4,
            "createdAt": self.created_at,
            "placeholder": placeholder(self.domain, self.kind, self.label),
        }


def placeholder(domain: str, kind: str, label: str) -> str:
    """The only form a credential ever takes outside the vault."""
    return "{{CREDENTIAL:%s:%s:%s}}" % (domain, kind, label)


def parse_placeholder(text: str) -> Optional[tuple[str, str, str]]:
    """Parse one placeholder, or None. The first match wins."""
    if not text:
        return None
    match = CREDENTIAL_PLACEHOLDER_RE.search(text)
    if not match:
        return None
    return (match.group(1), match.group(2), match.group(3))


def contains_placeholder(text: str) -> bool:
    return parse_placeholder(text) is not None


def _read_envelope(domain: str, kind: str, label: str) -> Optional[dict[str, Any]]:
    raw = secrets_store.get_secret(_store_name(domain, kind, label))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def add_credential(
    domain: str,
    kind: str,
    label: str,
    *,
    username: Optional[str] = None,
    value: str,
) -> CredentialMeta:
    """Store one credential, encrypted, and return its metadata.

    ``kind="login"`` stores a password (``value``) with an optional
    username. ``kind="card"`` stores the last-4 digits only — ``value``
    must be exactly four digits; anything longer is rejected, because
    a full card number must never enter the vault.
    OTP / TOTP kinds are rejected outright.
    """
    kind = (kind or "").strip().lower()
    if kind in REJECTED_KINDS:
        raise ValueError(
            "one-time codes (OTP/TOTP) are never stored in the vault — "
            "enter them yourself in takeover mode"
        )
    if kind not in VAULT_KINDS:
        raise ValueError(f"unsupported credential kind {kind!r}; expected one of {list(VAULT_KINDS)}")
    domain = _normalise_domain(domain)
    label = _validate_label(label)
    if value is None or str(value) == "":
        raise ValueError("credential value must not be empty")
    value = str(value)
    username = (username or "").strip() or None
    if len(username or "") > 320:
        raise ValueError("username is too long")

    last4: Optional[str] = None
    if kind == "card":
        # Last-4 only. A full PAN is refused, not truncated: silently
        # keeping the last four of a pasted full number would teach the
        # operator that pasting a full number is fine. It is not.
        if not _LAST4_RE.match(value.strip()):
            raise ValueError(
                "card credentials store the last-4 digits only — enter exactly "
                "four digits. A full card number is never stored; complete "
                "card payments yourself in takeover mode"
            )
        last4 = value.strip()
    elif kind == "login" and len(value) > 4096:
        raise ValueError("credential value is too long")

    envelope = {
        "domain": domain,
        "kind": kind,
        "label": label,
        "username": username,
        "last4": last4,
        "value": value,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    secrets_store.set_secret(_store_name(domain, kind, label), json.dumps(envelope))
    # Verify the round-trip before reporting success — the same rule
    # as the Task 2 .env migration: never claim a secret is stored
    # when it is not.
    stored = _read_envelope(domain, kind, label)
    if not stored or stored.get("value") != value:
        raise ValueError("credential could not be verified in the encrypted store")
    return CredentialMeta(
        domain=domain, kind=kind, label=label,
        username=username, last4=last4, created_at=envelope["created_at"],
    )


def get_credential_meta(domain: str, kind: str, label: str) -> Optional[CredentialMeta]:
    """Metadata for one credential, or None. Never returns the value."""
    try:
        domain = _normalise_domain(domain)
        label = _validate_label(label)
    except ValueError:
        return None
    data = _read_envelope(domain, kind, label)
    if not data:
        return None
    return CredentialMeta(
        domain=data.get("domain", domain),
        kind=data.get("kind", kind),
        label=data.get("label", label),
        username=data.get("username"),
        last4=data.get("last4"),
        created_at=data.get("created_at"),
    )


def list_credentials() -> list[CredentialMeta]:
    """Metadata for every stored credential. Values are never listed."""
    out: list[CredentialMeta] = []
    for name in secrets_store.list_secret_names():
        if not name.startswith(_STORE_PREFIX):
            continue
        rest = name[len(_STORE_PREFIX):]
        parts = rest.split(":")
        if len(parts) != 3:
            continue
        meta = get_credential_meta(parts[0], parts[1], parts[2])
        if meta is not None:
            out.append(meta)
    out.sort(key=lambda m: (m.domain, m.kind, m.label))
    return out


def delete_credential(domain: str, kind: str, label: str) -> bool:
    """Remove one credential. Returns True if something was removed."""
    domain = _normalise_domain(domain)
    label = _validate_label(label)
    return secrets_store.delete_secret(_store_name(domain, kind, label))


def resolve_placeholder(text: str) -> str:
    """Resolve the *first* placeholder in *text* to its real value.

    Execution-path only: callers are the agent loop just before an
    action is sent to the extension, and the vault tests. Never call
    this while building a planner prompt, a log line, or history.
    Raises ``KeyError`` for an unknown placeholder (fail closed — the
    loop must not type a placeholder literally into a page).
    """
    parsed = parse_placeholder(text)
    if parsed is None:
        return text
    domain, kind, label = parsed
    data = _read_envelope(domain, kind, label)
    if not data or not data.get("value"):
        raise KeyError(f"unknown credential placeholder {placeholder(domain, kind, label)}")
    return CREDENTIAL_PLACEHOLDER_RE.sub(
        lambda _m: str(data["value"]), text, count=1
    )


def placeholder_value(domain: str, kind: str, label: str) -> str:
    """The stored secret for one placeholder, or raises ``KeyError``.

    Execution-path only (see :func:`resolve_placeholder`). Used by the
    agent loop to collect the values it substituted, so it can scrub
    them out of the action evidence before recording it.
    """
    data = _read_envelope(domain, kind, label)
    if not data or not data.get("value"):
        raise KeyError(f"unknown credential placeholder {placeholder(domain, kind, label)}")
    return str(data["value"])


def substitute_placeholders(text: str) -> str:
    """Replace *every* placeholder in *text* with its real value.

    Execution-path only (see :func:`resolve_placeholder`). Raises
    ``KeyError`` on the first unknown placeholder.
    """
    out = text
    while True:
        parsed = parse_placeholder(out)
        if parsed is None:
            return out
        domain, kind, label = parsed
        data = _read_envelope(domain, kind, label)
        if not data or not data.get("value"):
            raise KeyError(f"unknown credential placeholder {placeholder(domain, kind, label)}")
        out = CREDENTIAL_PLACEHOLDER_RE.sub(
            lambda _m: str(data["value"]), out, count=1
        )


def substitute_placeholders_in_action(action: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *action* with placeholders resolved in text params.

    Only ``params.text`` is substituted: that is the one field the
    executor types into the page. Nothing else in an action may carry
    a credential — a placeholder anywhere else is a plan error and
    raises ``ValueError`` instead of being silently typed.
    """
    import copy

    new_action = copy.deepcopy(action)
    params = new_action.get("params") or {}
    text = params.get("text")
    if isinstance(text, str) and contains_placeholder(text):
        params["text"] = substitute_placeholders(text)
        new_action["params"] = params
    # A placeholder outside params.text means the planner (or an
    # injected plan) tried to smuggle a credential reference somewhere
    # it does not belong. Fail closed.
    rest = json.dumps({k: v for k, v in new_action.items() if k != "params"},
                      default=str)
    if contains_placeholder(rest):
        raise ValueError("credential placeholder outside params.text is forbidden")
    return new_action


def scrub_value_from_text(text: str, value: Optional[str]) -> str:
    """Remove one real secret value from a string (evidence / history).

    Best-effort scrub for the loop: after an action that typed a vault
    value, any echo of that value in the action evidence is replaced
    with ``[REDACTED]`` before the evidence is recorded. An empty or
    very short value is never scrubbed by substring (it would redact
    ordinary words) — values under 4 characters are refused by the
    vault for login kinds only if empty; short passwords still work,
    they just cannot be scrubbed this way, so the loop also never
    records raw evidence for a substituted action (see agentLoop).
    """
    if not text or not value or len(value) < 4:
        return text
    return text.replace(value, "[REDACTED]")
