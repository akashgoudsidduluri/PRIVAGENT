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
from starlette.websockets import WebSocketDisconnect, WebSocketState

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
    # Task 12: set by POST /api/task/{id}/confirm when the operator
    # chooses "approve this step only". The loop reads it through the
    # confirm gate and consumes it in the batch gate; it never
    # overrides a refusal or any safety gate.
    confirm_step_only: bool = False
    # Task 4: takeover — the operator has the browser while the agent
    # observes nothing. ``takeover`` is the operator-facing record
    # (kind/reason/url/started_at only — never a secret); the event +
    # decision live here so the REST takeover endpoints and the loop
    # share exactly one wait.
    takeover: Optional[dict[str, Any]] = None
    takeover_event: asyncio.Event = field(default_factory=asyncio.Event)
    takeover_decision: Optional[bool] = None
    screenshot: Optional[str] = None
    # Phase timings for the UI telemetry (milliseconds).
    planner_ms: Optional[int] = None
    observe_ms: Optional[int] = None
    act_ms: Optional[int] = None
    # Task 11: per-step loop timings summary (durations, counts,
    # action types only — never page text, names, or values).
    # In-memory only; never persisted to a checkpoint.
    timings: Optional[dict[str, Any]] = None
    created_at: float = field(default_factory=time.monotonic)
    # P5.2 Task Intent & Browser Session Management
    intent: Optional[str] = None
    session_id: Optional[str] = None
    primary_tab_id: Optional[int] = None
    child_tab_ids: list[int] = field(default_factory=list)
    terminal_emitted: bool = False
    last_observation: Any = None
    # Task 3: domain allow-list for this task (None = not configured,
    # navigation allowed everywhere with a warning at task creation).
    allowed_domains: Optional[list[str]] = None
    # Task 9: long-horizon tasks get the 100-step cap + history
    # compaction; background tasks run without a live WebSocket client
    # and are pollable via GET /api/task/{id}. Neither flag weakens
    # any gate — allow-list, confirmation, takeover and payment limits
    # apply identically.
    long_horizon: bool = False
    background: bool = False
    # Task 9: the task's own tabs. Each entry is
    # {"tab_id", "url", "title", "active"}; the primary tab is the
    # first entry. The planner prompt lists them; switch_tab /
    # select_tab may only target a tab in this registry (unknown tabs
    # are rejected before anything is sent to the extension).
    tabs: list[dict[str, Any]] = field(default_factory=list)
    # Task 14: how this task is being executed — {"via", "connector",
    # "operation", "mutating", "reason"} for a connector-routed task,
    # or {"via": "webmcp_tool", "tool": ...} / {"via": "ucp_checkout",
    # "merchant": ...} once the loop uses a standard tool. Names and
    # reasons only, never content — safe for the UI and the API.
    route: Optional[dict[str, Any]] = None
    # Compound sub-task: one clause of a multi-clause instruction run
    # as its own mission under a parent task. The parent owns the chat
    # message and the final summary; a part never adds its own user
    # message or retitles the chat.
    compound_part: bool = False
    parent_task_id: Optional[str] = None


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
        # Task 3: operator-attested profile report from the extension
        # (see /api/isolation/status). Chrome does not expose the profile
        # directory to extensions, so this is a self-report, not proof.
        self.extension_profile: dict[str, Any] = {}

    def set_extension_profile(self, info: dict[str, Any]) -> None:
        if isinstance(info, dict):
            self.extension_profile = dict(info)

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
            if not self.extension_sockets:
                self.extension_profile = {}

    def spawn(self, coro: Coroutine[Any, Any, Any]) -> "asyncio.Task[Any]":
        """Keep a strong reference to fire-and-forget broadcasts so the
        event loop cannot garbage-collect them mid-flight."""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    def new_task(self, instruction: str, chat_id: Optional[str] = None, allowed_domains: Optional[list[str]] = None, long_horizon: bool = False, background: bool = False) -> TaskRecord:
        self._prune_tasks()
        task = TaskRecord(task_id=uuid.uuid4().hex, instruction=instruction, chat_id=chat_id, allowed_domains=allowed_domains, long_horizon=long_horizon, background=background)
        self.tasks[task.task_id] = task
        return task

    # -- Task 9: multi-tab registry --------------------------------------
    # NOTE (Task 9 review): the loop's registry lives in the loop's
    # working memory (agentLoop._register_tab_in_memory) and is mirrored
    # onto the record by run_task's progress handler — the helpers below
    # are NOT called by the loop. They exist for tests and for any future
    # caller that owns a record directly; do not assume the loop uses them.
    def register_tab(self, task_id: str, tab_id: int, url: str = "", title: str = "", active: bool = False) -> None:
        record = self.tasks.get(task_id)
        if record is None:
            return
        for tab in record.tabs:
            if tab.get("tab_id") == tab_id:
                tab["url"] = url or tab.get("url", "")
                tab["title"] = title or tab.get("title", "")
                break
        else:
            record.tabs.append({"tab_id": tab_id, "url": url, "title": title, "active": bool(active)})
        if active:
            self.set_active_tab(task_id, tab_id)

    def set_active_tab(self, task_id: str, tab_id: int) -> bool:
        record = self.tasks.get(task_id)
        if record is None:
            return False
        found = False
        for tab in record.tabs:
            tab["active"] = tab.get("tab_id") == tab_id
            found = found or tab.get("tab_id") == tab_id
        return found

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
        message: dict[str, Any] = {"type": "EXECUTE_ACTION", "taskId": action_id, "payload": action}
        # Task 3: pass the task's allow-list to the extension so it can
        # refuse a disallowed navigation itself (defence in depth). The
        # backend loop is the authoritative gate; the extension check
        # only matters if a bad action somehow reaches it.
        _record = self.tasks.get(task_id)
        if _record is not None and _record.allowed_domains:
            message["allowedDomains"] = list(_record.allowed_domains)

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

    async def notify_extension(self, message: dict[str, Any]) -> None:
        """Fire-and-forget message to the extension (e.g. TAKEOVER_START).

        Unlike :meth:`send_action` this expects no RESULT: takeover
        notifications must not block the loop waiting on a browser the
        operator is currently using. Dead sockets are dropped.
        """
        if not isinstance(message, dict):
            return
        for ws in list(self.extension_sockets):
            try:
                await ws.send_json(message)
            except Exception:
                self.drop_extension(ws)

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
                # Task 11: per-step loop timings (durations + counts +
                # action types only).
                "timings": task.timings,
                "intent": task.intent,
                "sessionId": task.session_id,
                "primaryTabId": task.primary_tab_id,
                "childTabIds": task.child_tab_ids,
                # Task 4: the UI shows the takeover banner from this
                # field (kind/reason/url only — never a secret).
                "takeover": task.takeover,
                # Task 9: long-horizon / background flags and the
                # task's tab registry (tab ids + urls only).
                "longHorizon": task.long_horizon,
                "background": task.background,
                "tabs": task.tabs,
                # Task 14: routing decision (connector / browser /
                # webmcp tool / ucp checkout) — names only.
                "route": task.route,
                "isBrowserTask": task.intent != "NON_BROWSER" if task.intent else True,
            },
        }
        for ws in list(self.client_sockets):
            if getattr(ws, "client_state", None) == WebSocketState.DISCONNECTED or getattr(ws, "application_state", None) == WebSocketState.DISCONNECTED:
                if ws in self.client_sockets:
                    self.client_sockets.remove(ws)
                continue
            try:
                await ws.send_json(payload)
            except (WebSocketDisconnect, ConnectionResetError, BrokenPipeError, OSError):
                if ws in self.client_sockets:
                    self.client_sockets.remove(ws)
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
