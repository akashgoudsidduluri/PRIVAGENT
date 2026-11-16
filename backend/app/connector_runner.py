"""Connector execution (Task 14).

Runs a :class:`app.connector_router.ConnectorRoute` against the native
API connector. Rules, in order:

1. **Never silently fall back to the browser.** When the connector
   has no token, the outcome is an honest failure naming the connector
   and the remedy (connect it in Control Center -> Connectors, or ask
   for the browser UI explicitly). No browser navigation is attempted.
2. **Reads execute directly; writes need the operator.** Draft/trash/
   create go through the confirmation gate first (``confirm_fn``,
   injected so tests can stub it). A denied or timed-out confirmation
   fails the task honestly; nothing is executed.
3. **Egress is pinned.** Every connector call is checked against
   :data:`app.allowlist.CONNECTOR_EGRESS_HOSTS` before any request is
   made — Google APIs only.
4. **Results never reach the planner.** A connector read is answered
   directly to the operator; the planner is never called with
   connector data. The answer is still scrubbed (credential/PII
   patterns) before it is returned, so a secret inside an email body
   or a Drive file does not become a durable chat message verbatim.
   Persistence itself is the caller's job
   (:func:`app.loop_checkpoints._persist_agent_message`, which already
   honours zero retention) — this module persists nothing.
5. **Audit, metadata only.** Every call appends one audit event
   (connector + operation + outcome); summaries never carry content.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from .connector_router import CONNECTOR_LABELS, ConnectorRoute
from .allowlist import CONNECTOR_EGRESS_HOSTS, egress_allowed

__all__ = ["ConnectorOutcome", "run_connector_route", "not_connected_message"]

_CONFIRM_TIMEOUT_S = 120.0


def not_connected_message(connector: str) -> str:
    """Honest failure for an unconfigured connector (no silent fallback)."""
    label = CONNECTOR_LABELS.get(connector, connector)
    return (
        f"### {label} connector is not connected\n\n"
        f"I can't answer this through the {label} API because no account "
        "is connected.\n\n"
        "To connect it:\n"
        "1. Open the **Connectors** tab in the Control Center.\n"
        "2. Sign in with Google OAuth or paste an access token.\n"
        "3. Ask me again.\n\n"
        "Or ask me to open it in the browser instead — e.g. "
        f"'open {label}' — and I'll drive the website UI."
    )


@dataclass
class ConnectorOutcome:
    ok: bool
    message: str
    route: ConnectorRoute
    executed: bool = False  # True only when an API call actually ran


async def _default_confirm(message: str, action: dict[str, Any], record: Any, task_id: str) -> bool:
    """Confirmation through the loop's own gate (production path)."""
    from .loop_gates import _ConfirmGate, _request_confirmation

    gate = _ConfirmGate(record)
    return await _request_confirmation(
        gate, action, task_id, _CONFIRM_TIMEOUT_S, custom_message=message
    )


def _audit(connector: str, operation: str, outcome: str, task_id: str) -> None:
    try:
        from .audit import append_event

        append_event(
            "connector_call",
            f"connector {connector} op {operation} outcome {outcome}",
            actor="system",
            task_id=task_id,
        )
    except Exception:
        pass


def _scrub_answer(text: str) -> str:
    """Scrub credential/PII patterns from an operator-facing answer.

    The shared scrubber misses a bare labelled value ("my password
    is hunter2") — a pattern email bodies do contain — so labelled
    secrets are redacted here too. An unlabelled bare value cannot be
    told apart from ordinary text and is left alone (the operator
    owns the mailbox; this scrub is defence-in-depth for the
    persisted chat copy, not a leak to a third party).
    """
    try:
        from .privacy import scrub_persistent

        text = str(scrub_persistent(text))
    except Exception:
        pass
    try:
        import re

        text = re.sub(
            r"(?i)\b(password|passwd|pwd|api[-_ ]?key|secret|token|otp)\b"
            r"(\s*(?:is|:|=)\s*)\S+",
            r"\1\2[redacted]",
            text,
        )
    except Exception:
        pass
    return text


def _gmail_search_answer(connector: Any, query: str, limit: int = 10) -> str:
    summaries = connector.summarize_messages(query, limit=limit)
    if not summaries:
        return (
            "No emails matched"
            + (f" '{query}'" if query else "")
            + " in your Gmail inbox."
        )
    lines = ["Here is what I found in your Gmail inbox:\n"]
    for s in summaries[:10]:
        frm = (s.get("from") or "(unknown sender)").strip()
        subj = (s.get("subject") or "(no subject)").strip()
        snip = (s.get("snippet") or "").strip()
        lines.append(f"• **{subj}** — from {frm}")
        if snip:
            lines.append(f"  {snip[:240]}")
    lines.append(f"\n({len(summaries)} result(s) via the Gmail connector.)")
    return "\n".join(lines)


def _gmail_list_answer(connector: Any, limit: int = 10) -> str:
    data = connector.list_messages(query="", limit=limit)
    messages = data.get("messages", []) if isinstance(data, dict) else []
    if not messages:
        return "Your Gmail inbox is empty (no messages returned)."
    lines = ["Here is your Gmail inbox:\n"]
    for item in messages[:10]:
        try:
            msg = connector.get_message(item["id"], format="metadata")
        except Exception:
            lines.append(f"• message {item.get('id', '?')} (could not load details)")
            continue
        headers = {
            h.get("name", "").lower(): h.get("value", "")
            for h in msg.get("payload", {}).get("headers", [])
        }
        frm = (headers.get("from") or "(unknown sender)").strip()
        subj = (headers.get("subject") or "(no subject)").strip()
        snip = (msg.get("snippet") or "").strip()
        lines.append(f"• **{subj}** — from {frm}")
        if snip:
            lines.append(f"  {snip[:240]}")
    lines.append(f"\n({len(messages)} message(s) via the Gmail connector.)")
    return "\n".join(lines)


def _drive_search_answer(connector: Any, query: str, limit: int = 10) -> str:
    files = connector.search_documents(query, limit=limit)
    if not files:
        return f"No files matching '{query}' were found in your Google Drive."
    lines = [f"Here is what I found in your Google Drive for '{query}':\n"]
    for f in files[:10]:
        name = (f.get("name") or "(unnamed)").strip()
        link = f.get("webViewLink") or ""
        lines.append(f"• **{name}**" + (f" — {link}" if link else ""))
    lines.append(f"\n({len(files)} result(s) via the Google Drive connector.)")
    return "\n".join(lines)


async def run_connector_route(
    route: ConnectorRoute,
    *,
    task_id: str = "",
    instruction: str = "",
    record: Any = None,
    confirm_fn: Optional[Callable[[str, dict[str, Any]], Awaitable[bool]]] = None,
    oauth_manager: Any = None,
) -> ConnectorOutcome:
    """Execute one routed connector operation.

    ``instruction`` is the raw user instruction (the calendar handler
    parses title/time from it). ``confirm_fn(message, action)`` is
    awaited for mutating operations; when omitted, the production
    confirmation gate on ``record`` is used (``record`` is then
    required). ``oauth_manager`` is injectable for tests; the
    process-wide manager is the default.
    """
    label = CONNECTOR_LABELS.get(route.connector, route.connector)

    if oauth_manager is None:
        from .connectors.oauth import oauth_manager as _manager

        oauth_manager = _manager

    try:
        connector = oauth_manager.connector(route.connector)
    except Exception:
        _audit(route.connector, route.operation, "unknown_connector", task_id)
        return ConnectorOutcome(False, not_connected_message(route.connector), route)

    if not getattr(connector, "configured", False):
        _audit(route.connector, route.operation, "not_connected", task_id)
        return ConnectorOutcome(False, not_connected_message(route.connector), route)

    origin = getattr(connector, "origin", "")
    if origin and not egress_allowed(origin, extra_hosts=list(CONNECTOR_EGRESS_HOSTS)):
        _audit(route.connector, route.operation, "egress_denied", task_id)
        return ConnectorOutcome(
            False,
            f"The {label} API host is not on the connector egress allow-list; "
            "refusing to call it. This is a configuration error, not a "
            "connection problem.",
            route,
        )

    async def _confirm(message: str, action: dict[str, Any]) -> bool:
        if confirm_fn is not None:
            return bool(await confirm_fn(message, action))
        if record is None:
            return False
        return await _default_confirm(message, action, record, task_id)

    # -- Mutating operations: confirm first, execute only on approval.
    if route.mutating:
        if route.connector == "gmail" and route.operation == "draft":
            to = (route.params.get("to") or "").strip()
            subject = (route.params.get("subject") or "").strip()
            if not to:
                return ConnectorOutcome(
                    False,
                    "I couldn't tell who the draft is for — say who to email, "
                    "e.g. 'draft an email to priya@example.com about the report'.",
                    route,
                )
            approved = await _confirm(
                f"Create a Gmail draft to {to}"
                + (f" with subject '{subject}'" if subject else " (no subject yet)")
                + "? The draft is created in your account; nothing is sent.",
                {"type": "connector_draft", "connector": "gmail", "to": to, "subject": subject},
            )
            if not approved:
                _audit(route.connector, route.operation, "denied", task_id)
                return ConnectorOutcome(False, "Draft not created — you declined the confirmation.", route)
            try:
                res = connector.draft_email(to, subject, route.params.get("body") or "")
            except Exception as exc:
                _audit(route.connector, route.operation, "failed", task_id)
                return ConnectorOutcome(False, f"Creating the Gmail draft failed: {exc}", route)
            _audit(route.connector, route.operation, "ok", task_id)
            draft_id = res.get("id", "") if isinstance(res, dict) else ""
            return ConnectorOutcome(
                True,
                f"Draft created in Gmail to {to}"
                + (f" with subject '{subject}'" if subject else "")
                + (f" (draft id {draft_id})." if draft_id else ".")
                + " Nothing was sent — open Gmail to review and send it.",
                route,
                executed=True,
            )

        if route.connector == "gmail" and route.operation == "trash":
            try:
                listing = connector.list_messages(query=route.query, limit=1)
                messages = listing.get("messages", []) if isinstance(listing, dict) else []
            except Exception as exc:
                _audit(route.connector, route.operation, "failed", task_id)
                return ConnectorOutcome(False, f"Searching Gmail failed: {exc}", route)
            if not messages:
                return ConnectorOutcome(
                    False, f"No email matching '{route.query}' was found, so nothing was trashed.", route
                )
            message_id = messages[0].get("id", "")
            try:
                msg = connector.get_message(message_id, format="metadata")
            except Exception as exc:
                _audit(route.connector, route.operation, "failed", task_id)
                return ConnectorOutcome(False, f"Loading the email failed: {exc}", route)
            headers = {
                h.get("name", "").lower(): h.get("value", "")
                for h in msg.get("payload", {}).get("headers", [])
            }
            subj = (headers.get("subject") or "(no subject)").strip()
            frm = (headers.get("from") or "(unknown sender)").strip()
            approved = await _confirm(
                f"Move this email to Trash?\n• Subject: {subj}\n• From: {frm}\n"
                "Trash is reversible for 30 days; the email is not permanently deleted.",
                {"type": "connector_trash", "connector": "gmail", "subject": subj},
            )
            if not approved:
                _audit(route.connector, route.operation, "denied", task_id)
                return ConnectorOutcome(False, "Nothing was trashed — you declined the confirmation.", route)
            try:
                connector.trash_message(message_id)
            except Exception as exc:
                _audit(route.connector, route.operation, "failed", task_id)
                return ConnectorOutcome(False, f"Moving the email to Trash failed: {exc}", route)
            _audit(route.connector, route.operation, "ok", task_id)
            return ConnectorOutcome(
                True,
                f"Moved to Trash: '{subj}' from {frm}. You can restore it from Gmail Trash within 30 days.",
                route,
                executed=True,
            )

        if route.connector == "google_calendar" and route.operation == "create":
            # REVIEW (Task 14 audit): parse BEFORE confirming. The
            # operator must approve the actual event (title + the time
            # that will be booked), and an instruction that does not
            # parse as a create must fail here — the handler falls
            # through to a listing for such input, so confirming it
            # would approve a creation and deliver a list of existing
            # events instead. The named time is shown as written AND
            # as booked: the handler books tomorrow 10:00 AM UTC when
            # the instruction gives no usable time, so the two can
            # differ and the operator must see that before approving.
            from .connectors.calendar_companion import parse_calendar_create

            _parsed = parse_calendar_create(instruction)
            if _parsed is None:
                _audit(route.connector, route.operation, "failed", task_id)
                return ConnectorOutcome(
                    False,
                    "I couldn't tell what event to create from that — say the "
                    "title and when, e.g. 'schedule a meeting called Standup "
                    "on Monday at 10am'. Nothing was created.",
                    route,
                )
            _when = _parsed["start"].strftime("%A, %B %d, %Y at %I:%M %p UTC")
            _attendees = _parsed["attendees"]
            approved = await _confirm(
                "Create this calendar event?\n"
                f"• Event: {_parsed['title']}\n"
                f"• When: {_when} (you wrote: '{_parsed['time_text']}')"
                + (f"\n• Attendees: {', '.join(_attendees)}" if _attendees else "")
                + "\nThe event is created on your primary calendar and invites are sent to any attendees you named.",
                {"type": "connector_create", "connector": "google_calendar"},
            )
            if not approved:
                _audit(route.connector, route.operation, "denied", task_id)
                return ConnectorOutcome(False, "Calendar event not created — you declined the confirmation.", route)
            try:
                from .connectors.calendar_companion import handle_calendar_instruction

                answer = await handle_calendar_instruction(instruction, chat_id=None)
            except Exception as exc:
                _audit(route.connector, route.operation, "failed", task_id)
                return ConnectorOutcome(False, f"Creating the calendar event failed: {exc}", route)
            _audit(route.connector, route.operation, "ok", task_id)
            return ConnectorOutcome(True, _scrub_answer(answer), route, executed=True)

        return ConnectorOutcome(False, f"Unsupported mutating operation '{route.operation}' for {label}.", route)

    # -- Reads: execute directly (read-only; no gate needed, like the
    #    MCP registry's read-only tools).
    try:
        if route.connector == "gmail":
            if route.operation == "search":
                answer = _gmail_search_answer(connector, route.query)
            else:
                answer = _gmail_list_answer(connector)
        elif route.connector == "google_calendar":
            from .connectors.calendar_companion import handle_calendar_instruction

            answer = await handle_calendar_instruction(instruction, chat_id=None)
        elif route.connector == "google_drive":
            answer = _drive_search_answer(connector, route.query)
        else:
            return ConnectorOutcome(False, f"Unknown connector '{route.connector}'.", route)
    except Exception as exc:
        _audit(route.connector, route.operation, "failed", task_id)
        return ConnectorOutcome(
            False,
            f"The {label} API call failed: {exc}. If this keeps happening, "
            "reconnect the account in Control Center → Connectors.",
            route,
        )
    _audit(route.connector, route.operation, "ok", task_id)
    return ConnectorOutcome(True, _scrub_answer(answer), route, executed=True)
