"""Task registry + WebSocket hub.

Broadcasts TaskProgress to /ws/client, routes EXECUTE_ACTION to a live
extension socket on /ws/extension, and matches RESULTs back to waiting
loops via an asyncio.Future per action id.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Coroutine, Optional

from fastapi import WebSocket

TERMINAL_STATES = {"completed", "failed", "cancelled"}
MAX_RETAINED_TASKS = 200


@dataclass
class TaskRecord:
    task_id: str
    instruction: str
    chat_id: Optional[str] = None
    file_token: Optional[str] = None
    state: str = "planning"
    steps_done: int = 0
    message: str = ""
    cancelled: bool = False
    pending_confirmation: Optional[dict[str, Any]] = None
    confirm_event: asyncio.Event = field(default_factory=asyncio.Event)
    confirm_decision: Optional[bool] = None
    screenshot: Optional[str] = None
    # Phase timings for the UI telemetry (milliseconds).
    planner_ms: Optional[int] = None
    observe_ms: Optional[int] = None
    act_ms: Optional[int] = None
    created_at: float = field(default_factory=time.monotonic)
    # P5.2 Task Intent & Browser Session Management
    intent: Optional[str] = None
    session_id: Optional[str] = None
    primary_tab_id: Optional[int] = None
    child_tab_ids: list[int] = field(default_factory=list)
    terminal_emitted: bool = False
    last_observation: Any = None


class ActionResultSlot:
    """One in-flight extension action.

    The future is bound to the running loop (not ``get_event_loop``, which
    binds to a stale loop on Windows after the first ``asyncio.run``)."""

    def __init__(self, task_id: str = "") -> None:
        self.task_id = task_id
        self.future: asyncio.Future = asyncio.get_running_loop().create_future()


class Hub:
    def __init__(self, max_retained_tasks: int = MAX_RETAINED_TASKS) -> None:
        self.extension_sockets: list[WebSocket] = []
        self.client_sockets: list[WebSocket] = []
        self.tasks: dict[str, TaskRecord] = {}
        self._action_results: dict[str, ActionResultSlot] = {}
        self._bg_tasks: set[asyncio.Task] = set()
        self._max_retained_tasks = max_retained_tasks

    @property
    def extension_ws(self) -> Optional[WebSocket]:
        """Most recent live extension socket, or None."""
        return self.extension_sockets[-1] if self.extension_sockets else None

    def set_extension(self, ws: Optional[WebSocket]) -> None:
        if ws is not None and ws not in self.extension_sockets:
            self.extension_sockets.append(ws)

    def drop_extension(self, ws: WebSocket) -> None:
        if ws in self.extension_sockets:
            self.extension_sockets.remove(ws)

    def spawn(self, coro: Coroutine[Any, Any, Any]) -> "asyncio.Task[Any]":
        """Keep a strong reference to fire-and-forget broadcasts so the
        event loop cannot garbage-collect them mid-flight."""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    def new_task(self, instruction: str, chat_id: Optional[str] = None) -> TaskRecord:
        self._prune_tasks()
        task = TaskRecord(task_id=uuid.uuid4().hex, instruction=instruction, chat_id=chat_id)
        self.tasks[task.task_id] = task
        return task

    def _prune_tasks(self) -> None:
        """Bound memory: drop the oldest terminal task records once the
        registry is over budget. Running tasks are never pruned."""
        excess = len(self.tasks) - self._max_retained_tasks
        if excess <= 0:
            return
        removable = sorted(
            (t for t in self.tasks.values() if t.state in TERMINAL_STATES),
            key=lambda t: t.created_at,
        )
        for task in removable[:excess]:
            self.tasks.pop(task.task_id, None)

    async def send_action(self, task_id: str, action: dict[str, Any], timeout_s: float = 45.0):
        if not self.extension_sockets:
            for _ in range(30):
                await asyncio.sleep(0.1)
                if self.extension_sockets:
                    break
        if not self.extension_sockets:
            raise RuntimeError("EXTENSION_DISCONNECTED: Chrome extension not connected. Ensure NAVIGATOR extension is enabled at chrome://extensions")

        action_id = uuid.uuid4().hex
        slot = ActionResultSlot(task_id)
        self._action_results[action_id] = slot
        message = {"type": "EXECUTE_ACTION", "taskId": action_id, "payload": action}

        # Prefer the newest socket, but fall back to older live ones if a
        # send fails (e.g. a half-closed socket still in the registry).
        sent = False
        last_exc: Optional[Exception] = None
        for ws in reversed(list(self.extension_sockets)):
            try:
                await ws.send_json(message)
                sent = True
                break
            except Exception as exc:  # noqa: BLE001 — any send failure means dead
                last_exc = exc
                self.drop_extension(ws)
        if not sent:
            self._action_results.pop(action_id, None)
            if not slot.future.done():
                slot.future.cancel()
            raise RuntimeError(f"EXTENSION_DISCONNECTED: send failed: {last_exc}")

        try:
            return await asyncio.wait_for(slot.future, timeout_s)
        except asyncio.TimeoutError:
            return {"ok": False, "error": {"code": "TIMEOUT", "message": "extension action timeout", "recoverable": True}}
        except asyncio.CancelledError:
            if not slot.future.done():
                slot.future.cancel()
            raise
        finally:
            # Never leak result slots: every action must clean up exactly once.
            self._action_results.pop(action_id, None)

    async def broadcast_progress(self, task: TaskRecord) -> None:
        from .privacy import scrub_persistent
        if task.state in TERMINAL_STATES:
            if task.terminal_emitted:
                return
            task.terminal_emitted = True

        payload = {
            "type": "PROGRESS",
            "taskId": task.task_id,
            "payload": {
                "taskId": task.task_id,
                "chatId": getattr(task, "chat_id", None),
                "instruction": scrub_persistent(task.instruction),
                "taskState": task.state,
                "stepsDone": task.steps_done,
                "message": scrub_persistent(task.message),
                "screenshot": task.screenshot,
                "plannerMs": task.planner_ms,
                "observeMs": task.observe_ms,
                "actMs": task.act_ms,
                "intent": task.intent,
                "sessionId": task.session_id,
                "primaryTabId": task.primary_tab_id,
                "childTabIds": task.child_tab_ids,
                "isBrowserTask": task.intent != "NON_BROWSER" if task.intent else True,
            },
        }
        for ws in list(self.client_sockets):
            try:
                await ws.send_json(payload)
            except Exception:
                if ws in self.client_sockets:
                    self.client_sockets.remove(ws)

    def resolve_action(self, action_id: str, payload: dict[str, Any]) -> None:
        slot = self._action_results.pop(action_id, None)
        if slot is not None and not slot.future.done():
            slot.future.set_result(payload)

    def abort_task_actions(self, task_id: str) -> None:
        """Fail every in-flight action for a cancelled task immediately.

        Without this, `send_action` kept waiting for the extension timeout,
        so a cancel during a long action did not reach the loop until the
        action finished. The browser may already be executing that one
        action; the loop still stops before the next one.
        """
        for action_id, slot in list(self._action_results.items()):
            if slot.task_id != task_id:
                continue
            self._action_results.pop(action_id, None)
            if not slot.future.done():
                slot.future.set_result({
                    "ok": False,
                    "error": {
                        "code": "CANCELLED",
                        "message": "task cancelled",
                        "recoverable": False,
                    },
                })


hub = Hub()
