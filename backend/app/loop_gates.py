"""Confirmation / takeover gates (Task 13).

Split out of :mod:`app.agentLoop` (which re-exports every name here,
so existing imports keep working). The gate classes bridge the loop's
confirm/takeover contracts onto the Hub ``TaskRecord`` a REST client
posts to; event + decision live on the record itself.

Why the hub is resolved lazily: :meth:`_ConfirmGate.add_request`
and :meth:`_TakeoverGate.add_request` broadcast progress through the
hub, and tests patch ``agentLoop.real_hub`` with a fake hub
(``monkeypatch.setattr(agentLoop, "real_hub", mock_hub)``). A
top-level ``from .hub import hub as real_hub`` here would bind the
real hub at import time and silently ignore that patch, so the gates
read ``agentLoop.real_hub`` at call time instead. The import is
function-local, so this module stays importable on its own and there
is no import cycle (``agentLoop`` imports this module at its top;
this module only touches ``agentLoop`` inside a method, long after
both modules are loaded).
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

from .hub import TaskRecord

__all__ = [
    "HANDOFF_MESSAGE",
    "_ConfirmGate",
    "_TakeoverGate",
    "_gate_message",
    "_request_confirmation",
]

HANDOFF_MESSAGE = "Please solve the CAPTCHA / sign in manually in your browser, then click Continue."


def _resolve_hub() -> Any:
    """The hub the gates broadcast through.

    ``agentLoop.real_hub`` is the patch point tests use; read it
    lazily so a swapped hub is honored. Falls back to the hub
    singleton when agentLoop is not (yet) importable.
    """
    try:
        from . import agentLoop

        return agentLoop.real_hub
    except Exception:
        from .hub import hub

        return hub


def _gate_message(action: dict[str, Any], is_wa_qr: bool, is_auth_page: bool) -> Optional[str]:
    """Operator-facing instruction shown with the confirmation gate."""
    if is_wa_qr:
        return ("WhatsApp Web requires QR code scan. Please scan the QR code in your "
                "WhatsApp tab to log in, then click Continue.")
    if action.get("type") == "wait" and action.get("risk") == "sensitive":
        # The planner's human-in-the-loop handoff (CAPTCHA / login wall).
        return HANDOFF_MESSAGE
    if is_auth_page:
        return ("Site requires Sign-In or Payment Credentials. Please sign in or complete "
                "authentication in your browser tab, then click Continue.")
    return None


async def _request_confirmation(gate: Any, action: dict[str, Any], task_id: str, timeout_s: float, custom_message: Optional[str] = None, plan: Optional[list[dict[str, Any]]] = None) -> bool:
    """Pause and await a human decision. Returns True if approved.
    gate contract: confirm_requests list + asyncio.Event confirm_event + decision:bool|None.

    Task 12: ``plan`` is the whole validated plan as preview dicts
    (see plan_preview.py — structured fields only). It rides along
    on the request so GET /api/task/{id} can show the operator the
    plan before they decide. It changes nothing about the gate: the
    decision is still one boolean for the pending action.
    """
    req = {
        "taskId": task_id,
        "action": action,
        "message": custom_message or f"NAVIGATOR wants to {action.get('type')} (risk {action.get('risk')})"
    }
    if plan is not None:
        req["plan"] = plan
    if hasattr(gate, "add_request"):
        gate.add_request(req)
    else:
        gate.confirm_requests.append(req)
    gate.decision = None
    gate.confirm_event.clear()
    try:
        await asyncio.wait_for(gate.confirm_event.wait(), timeout_s)
    except asyncio.TimeoutError:
        return False
    return bool(gate.decision)


class _ConfirmGate:
    """Bridges the loop's confirm contract onto the Hub TaskRecord a REST
    client posts to. Event + decision live on the record itself."""
    def __init__(self, record: TaskRecord) -> None:
        self._record = record
        self.confirm_requests = []
        self.strict_write_confirm = False

    @property
    def confirm_event(self) -> asyncio.Event:
        return self._record.confirm_event

    @property
    def decision(self) -> Optional[bool]:
        return self._record.confirm_decision

    @decision.setter
    def decision(self, v: Optional[bool]) -> None:
        self._record.confirm_decision = v

    # Task 12: "approve this step only" — the REST endpoint records
    # the operator's choice on the task record; the loop reads it
    # here, right after the gate resolves, and consumes it in the
    # batch gate. It is a bool on the record (not the gate decision),
    # so the gate contract — one boolean per request — is unchanged.
    @property
    def step_only(self) -> bool:
        return bool(getattr(self._record, "confirm_step_only", False))

    def add_request(self, req: dict[str, Any]) -> None:
        self.confirm_requests.append(req)
        self._record.pending_confirmation = req
        self._record.state = "awaiting_confirmation"
        self._record.message = req.get("message", "Awaiting confirmation")
        hub = _resolve_hub()
        hub.spawn(hub.broadcast_progress(self._record))


class _TakeoverGate:
    """Task 4: bridges the loop's takeover contract onto the Hub
    TaskRecord a REST client posts to. Event + decision live on the
    record itself, exactly like the confirmation gate.

    On entry the record's last observation and screenshot are cleared:
    whatever the operator was looking at before takeover is not the
    agent's business, and it must not be re-sent to any planner."""
    def __init__(self, record: TaskRecord) -> None:
        self._record = record
        self.takeover_requests = []

    @property
    def event(self) -> asyncio.Event:
        return self._record.takeover_event

    @property
    def decision(self) -> Optional[bool]:
        return self._record.takeover_decision

    @decision.setter
    def decision(self, v: Optional[bool]) -> None:
        self._record.takeover_decision = v

    def add_request(self, req: dict[str, Any]) -> None:
        self.takeover_requests.append(req)
        self._record.takeover = req
        self._record.last_observation = None
        self._record.screenshot = None
        self._record.state = "awaiting_takeover"
        self._record.message = req.get("reason", "Awaiting takeover")
        hub = _resolve_hub()
        hub.spawn(hub.broadcast_progress(self._record))
