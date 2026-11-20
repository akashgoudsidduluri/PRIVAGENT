"""Connector auto-routing (Task 14).

Detects Gmail / Google Calendar / Google Drive intents in the user's
instruction and routes them to the native API connectors instead of
driving the web UI in the browser. Detection is data-driven (regex
tables below, no per-site playbooks — Task 8 rule) and deliberately
narrow: only *read* intents and a small set of *write* intents route.

What routes, and what does not
-------------------------------
* Gmail reads — "check my inbox", "find the email from X", "show me
  unread emails", "what did X email me" — route to the Gmail
  connector (list/search/get).
* Gmail writes — "draft an email to X", "trash the email from X" —
  route to the connector but only behind the confirmation gate (the
  runner asks first; see :mod:`app.connector_runner`).
* Calendar reads/creates — "what's on my calendar tomorrow",
  "schedule a meeting with X" — route to the Google Calendar
  connector (reads execute directly; creates need confirmation).
* Drive reads — "find the Q3 deck in Drive", "search Drive for X" —
  route to the Drive connector (search, plus text extraction for a
  named file when the format is supported).
* Never routes: explicit UI navigation ("open Gmail", "go to my
  inbox" — the operator asked to *see* the UI), email dispatch
  ("email me this report", "send an email to X" — owned by
  :mod:`app.email_dispatch`), email-mission / companion queries
  ("continuously scan my emails", "is my email connected" — owned by
  the email-intelligence modules), and anything without a connector
  verb ("what is email" is a question, not a mailbox read).

A route is only a *decision*: execution lives in
:mod:`app.connector_runner`, which fails honestly when the connector
is not configured instead of silently falling back to the browser.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

__all__ = ["ConnectorRoute", "route_instruction", "CONNECTOR_LABELS"]

CONNECTOR_LABELS = {
    "gmail": "Gmail",
    "google_calendar": "Google Calendar",
    "google_drive": "Google Drive",
}

# Operations that change state. Everything else is a read.
MUTATING_OPERATIONS = frozenset({"create", "draft", "trash"})

# ---------------------------------------------------------------------------
# Detection tables (data, not code paths — one row per intent shape)
# ---------------------------------------------------------------------------

# Gmail reads. Each pattern optionally captures a query in group "q":
# the sender / subject / keyword the operator named. "check my inbox"
# with no query is a plain list.
_GMAIL_READ_PATTERNS = [
    # check/show/view/list/read/search/get my inbox|emails|mail (+ optional query)
    # REVIEW (Task 14 audit): "get" was missing ("get my emails" is a
    # plain inbox read), and "the" was missing ("check the mail").
    re.compile(
        r"\b(?:check|show|view|list|read|search|get|look\s+(?:at|through|in))\s+"
        r"(?:my\s+|the\s+)?(?:inbox|e-?mails?|mail|gmail)\b"
        r"(?:\s+(?:for|about|from)\s+(?P<q>.+?))?\s*$",
        re.IGNORECASE,
    ),
    # find/search (for) the email(s) from/about X
    re.compile(
        r"\b(?:find|search(?:\s+for)?|look\s+for|get|show\s+me)\s+"
        r"(?:the\s+)?(?:e-?mails?\s+)?(?:from|about|regarding|on)\s+(?P<q>.+?)\s*$",
        re.IGNORECASE,
    ),
    # find the email from X / the latest email from X / read the email about X
    # REVIEW (Task 14 audit): "about/regarding/on" were missing, so
    # "read the email about the invoices" — a plain read — did not route.
    re.compile(
        r"\b(?:find|show\s+me|get|read)\s+(?:the\s+)?(?:latest|newest|most\s+recent|first)?\s*"
        r"(?:e-?mail|mail|message)\s+(?:from|by|about|regarding|on)\s+(?P<q>.+?)\s*$",
        re.IGNORECASE,
    ),
    # what did X email me / did X email me
    re.compile(
        r"\bwhat\s+did\s+(?P<q>.+?)\s+e-?mail\s+me\b",
        re.IGNORECASE,
    ),
    # show me unread emails / any new mail
    re.compile(
        r"\b(?:show\s+me|list|get|any)\s+(?:my\s+)?(?P<q>unread|new)\s+(?:e-?mails?|mail|messages?)\b",
        re.IGNORECASE,
    ),
    # what's in my inbox
    re.compile(
        r"\bwhat(?:'s| is)\s+(?:in|on)\s+my\s+(?:inbox|e-?mail|mail)\b",
        re.IGNORECASE,
    ),
]

_GMAIL_WRITE_PATTERNS = [
    # draft an email to X (about Y) (saying Z)
    (
        "draft",
        re.compile(
            r"\b(?:draft|compose|write)\s+(?:an?\s+)?(?:e-?mail|mail|message)\s+"
            r"to\s+(?P<to>[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|[A-Za-z][\w .'-]{1,60})"
            r"(?:\s+(?:about|regarding|on|subject:?)\s+(?P<subject>.+?))?"
            r"(?:\s+(?:saying|that\s+says|with\s+body:?)\s+(?P<body>.+))?\s*$",
            re.IGNORECASE,
        ),
    ),
    # trash/delete the email from/about X
    (
        "trash",
        re.compile(
            r"\b(?:trash|delete|remove|move\s+to\s+trash)\s+(?:the\s+)?"
            r"(?:e-?mail|mail|message)\s+(?:from|about|regarding|on)\s+(?P<q>.+?)\s*$",
            re.IGNORECASE,
        ),
    ),
]

# Drive reads. Query is the file name / keyword after "for" or the
# noun phrase before "in Drive".
_DRIVE_PATTERNS = [
    # find/search/show me the X file in Drive / find X in Drive
    re.compile(
        r"\b(?:find|search(?:\s+for)?|look\s+for|show\s+me|get|open\s+the\s+file)\s+"
        r"(?:the\s+)?(?P<q>.+?)\s+(?:in|on|from)\s+(?:my\s+)?(?:google\s+)?drive\b",
        re.IGNORECASE,
    ),
    # search Drive for X / find in Drive: X
    re.compile(
        r"\b(?:search|look|find)\s+(?:in\s+|on\s+)?(?:my\s+)?(?:google\s+)?drive\s+"
        r"(?:for\s+)?(?P<q>.+?)\s*$",
        re.IGNORECASE,
    ),
    # what files in Drive match X
    re.compile(
        r"\b(?:what|which)\s+(?:files?|documents?|decks?|sheets?)\s+"
        r"(?:do\s+i\s+have\s+)?in\s+(?:my\s+)?(?:google\s+)?drive\s+"
        r"(?:(?:about|for|named|called)\s+)?(?P<q>.+?)\s*$",
        re.IGNORECASE,
    ),
]

# Calendar create (mutating). Reads are detected by the existing
# calendar companion detector, reused here rather than duplicated.
# "create a calendar event" has "calendar" between the verb and the
# noun — the optional middle group covers it.
_CALENDAR_CREATE_RE = re.compile(
    r"\b(?:schedule|create|add|book)\s+(?:an?\s+)?(?:calendar\s+)?"
    r"(?:event|meeting|invite|appointment|call)\b",
    re.IGNORECASE,
)

# Explicit UI navigation — the operator asked to SEE the site, so the
# browser owns it even when a connector could answer the same question.
_EXPLICIT_NAVIGATION_RE = re.compile(
    r"\b(?:open|go\s+to|navigate\s+to|visit|browse\s+to|show\s+me)\s+"
    r"(?:the\s+)?(?:gmail(?:\s+app|\s+website|\s+inbox)?|google\s+(?:calendar|drive)|"
    r"my\s+inbox|my\s+google\s+drive|drive\s+app)\b"
    r"|\bopen\s+(?:my\s+)?inbox\b",
    re.IGNORECASE,
)


def _clean_query(raw: Optional[str]) -> str:
    if not raw:
        return ""
    q = raw.strip().strip("\"'").strip()
    q = re.sub(r"\s+", " ", q)
    return q[:300]


@dataclass(frozen=True)
class ConnectorRoute:
    """One routing decision: which connector, which operation, with what."""

    connector: str  # "gmail" | "google_calendar" | "google_drive"
    operation: str  # "search" | "list" | "get" | "create" | "draft" | "trash"
    query: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    reason: str = ""

    @property
    def mutating(self) -> bool:
        return self.operation in MUTATING_OPERATIONS

    @property
    def label(self) -> str:
        """Operator-facing route label, e.g. "via Gmail connector"."""
        return f"via {CONNECTOR_LABELS.get(self.connector, self.connector)} connector"

    def to_dict(self) -> dict[str, Any]:
        """Record-safe form: names and reasons only, never content."""
        return {
            "via": self.label,
            "connector": self.connector,
            "operation": self.operation,
            "mutating": self.mutating,
            "reason": self.reason,
        }


def _is_disallowed_overlap(text: str) -> bool:
    """True when another subsystem already owns this instruction.

    The router must never steal email dispatch ("email me this") or
    email-mission setup ("continuously scan my emails"). Email
    companion *status* queries ("is my email connected", "what did you
    delete", "pause email monitoring") are deliberately NOT excluded
    here: none of them matches a router pattern (verified case by
    case — they carry no connector verb + mailbox object), while a
    plain read such as "check my inbox" DOES match the companion's
    over-broad "check my email" status pattern and must still route
    to the Gmail connector.
    """
    try:
        from .email_dispatch import is_email_dispatch_instruction

        is_dispatch, _ = is_email_dispatch_instruction(text)
        if is_dispatch:
            return True
    except Exception:
        pass
    try:
        from .email_intelligence.mission_setup import is_email_mission_setup_instruction

        if is_email_mission_setup_instruction(text):
            return True
    except Exception:
        pass
    return False


def route_instruction(instruction: str) -> Optional[ConnectorRoute]:
    """Detect a connector intent, or return None (browser/conversation owns it)."""
    text = (instruction or "").strip()
    if not text:
        return None

    # Explicit UI navigation wins over connector routing: the operator
    # asked to see the site, not to have the API answer.
    if _EXPLICIT_NAVIGATION_RE.search(text):
        return None
    if _is_disallowed_overlap(text):
        return None

    # Calendar: reuse the existing companion detector for reads, and
    # the create pattern for mutating intent. The import is lazy
    # (calendar_companion builds an OAuthManager at import time); a
    # failure here is logged, never swallowed — silently losing
    # calendar routing would send calendar reads to the browser.
    try:
        from .connectors.calendar_companion import is_calendar_query

        if is_calendar_query(text):
            if _CALENDAR_CREATE_RE.search(text):
                return ConnectorRoute(
                    connector="google_calendar",
                    operation="create",
                    confidence=0.9,
                    reason="calendar create intent routes to the Google Calendar connector (confirmation required)",
                )
            return ConnectorRoute(
                connector="google_calendar",
                operation="list",
                confidence=0.95,
                reason="calendar read intent routes to the Google Calendar connector",
            )
    except Exception as exc:
        import logging

        logging.getLogger(__name__).warning("connector router: calendar detector unavailable (%s)", exc)

    # Gmail writes (checked before reads: "draft an email" contains
    # "email" but is not a read).
    for operation, pattern in _GMAIL_WRITE_PATTERNS:
        m = pattern.search(text)
        if m:
            params: dict[str, Any] = {}
            gd = m.groupdict()
            if gd.get("to"):
                params["to"] = gd["to"].strip()
            if gd.get("subject"):
                params["subject"] = _clean_query(gd["subject"])
            if gd.get("body"):
                params["body"] = gd["body"].strip()[:4000]
            return ConnectorRoute(
                connector="gmail",
                operation=operation,
                query=_clean_query(gd.get("q")),
                params=params,
                confidence=0.9,
                reason=f"gmail {operation} intent routes to the Gmail connector (confirmation required)",
            )

    # Gmail reads.
    for pattern in _GMAIL_READ_PATTERNS:
        m = pattern.search(text)
        if m:
            q = _clean_query(m.groupdict().get("q"))
            # "unread"/"new" is a filter, not a keyword: translate it
            # to the Gmail search operator so the connector (not the
            # operator's eyes) does the filtering.
            if q.lower() in {"unread", "new"}:
                q = "is:unread"
            # "for anything new" is a plain inbox check, not a search
            # for the literal words "anything new".
            elif q.lower() in {"anything new", "anything", "new mail", "new emails", "new messages"}:
                q = ""
            op = "search" if q else "list"
            return ConnectorRoute(
                connector="gmail",
                operation=op,
                query=q,
                confidence=0.9,
                reason="gmail read intent routes to the Gmail connector",
            )

    # Drive reads.
    for pattern in _DRIVE_PATTERNS:
        m = pattern.search(text)
        if m:
            q = _clean_query(m.groupdict().get("q"))
            if not q:
                continue
            return ConnectorRoute(
                connector="google_drive",
                operation="search",
                query=q,
                confidence=0.9,
                reason="drive search intent routes to the Google Drive connector",
            )

    return None
