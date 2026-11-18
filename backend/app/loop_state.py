"""Per-task loop state: cancel / pause / takeover registries (Task 13).

Split out of :mod:`app.agentLoop` (which re-exports every name here,
so existing imports keep working). The registries are plain module
objects — ``agentLoop._cancelled`` *is* this module's ``_cancelled``,
so a test or endpoint that reads or patches ``agentLoop._pause_events``
sees the same dict the loop itself mutates.

Task 13 dead-code removal: ``request_takeover`` / ``is_in_takeover``
used to live here. They had no callers anywhere in the repo — the
takeover resume/cancel endpoints set the gate event on the task
record directly (``main.py``), and the cancel path is covered by
:func:`request_cancel`, which also wakes a takeover wait. Removed
rather than moved.
"""
from __future__ import annotations

import asyncio
from typing import Any

__all__ = [
    "_await_takeover",
    "_cancelled",
    "_notify_extension_takeover",
    "_pause_events",
    "_takeover_wait_events",
    "is_cancelled",
    "request_cancel",
    "request_pause",
    "request_resume",
]

_cancelled: set[str] = set()

_pause_events: dict[str, asyncio.Event] = {}

# --- Task 4: takeover -------------------------------------------------------
# Events for loops currently waiting in takeover, keyed by task id.
# request_cancel() sets these so a cancel wakes the wait immediately.
_takeover_wait_events: dict[str, asyncio.Event] = {}


def request_cancel(task_id: str) -> None:
    _cancelled.add(task_id)
    event = _pause_events.get(task_id)
    if event:
        event.set()
    # A cancel must also wake a loop that is waiting in takeover: the
    # operator cancelled instead of completing the manual step, and the
    # loop must observe that immediately, not after the takeover timeout.
    takeover_event = _takeover_wait_events.get(task_id)
    if takeover_event:
        takeover_event.set()


def is_cancelled(task_id: str) -> bool:
    return task_id in _cancelled


def request_pause(task_id: str) -> None:
    _pause_events.setdefault(task_id, asyncio.Event()).clear()


def request_resume(task_id: str) -> None:
    event = _pause_events.pop(task_id, None)
    if event:
        event.set()


async def _notify_extension_takeover(ext_hub: Any, task_id: str, active: bool) -> None:
    """Tell the extension takeover started/ended (best effort).

    The extension stops observing and screenshotting while takeover is
    active. Fake hubs in tests have no ``notify_extension`` — that is
    fine, the backend loop is the authoritative gate.
    """
    notify = getattr(ext_hub, "notify_extension", None)
    if notify is None:
        return
    try:
        result = notify({
            "type": "TAKEOVER_START" if active else "TAKEOVER_END",
            "taskId": task_id,
        })
        if asyncio.iscoroutine(result):
            await result
    except Exception:
        pass


async def _await_takeover(
    takeover_gate: Any,
    task_id: str,
    trigger: Any,
    timeout_s: float,
) -> str:
    """Wait for the operator to finish a takeover step.

    Returns ``"completed"`` (operator did it, resume), ``"rejected"``
    (operator cancelled), or ``"timeout"``. The gate contract mirrors
    the confirmation gate: ``add_request(req)`` (or a
    ``takeover_requests`` list), an asyncio ``event``, and a
    ``decision`` that is True (completed) / False (cancelled) / None.
    """
    req = {
        "taskId": task_id,
        "kind": trigger.kind,
        "reason": trigger.reason,
        "url": trigger.url,
    }
    if hasattr(takeover_gate, "add_request"):
        takeover_gate.add_request(req)
    else:
        takeover_gate.takeover_requests.append(req)
    takeover_gate.decision = None
    takeover_gate.event.clear()
    event = takeover_gate.event
    _takeover_wait_events[task_id] = event
    try:
        await asyncio.wait_for(event.wait(), timeout_s)
    except asyncio.TimeoutError:
        return "timeout"
    finally:
        _takeover_wait_events.pop(task_id, None)
    if is_cancelled(task_id):
        return "rejected"
    return "completed" if takeover_gate.decision is True else "rejected"
