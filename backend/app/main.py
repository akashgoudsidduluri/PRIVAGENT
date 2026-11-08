from __future__ import annotations

import asyncio
import logging
import re
import sys
from typing import Any, Optional

if sys.platform == "win32":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import config, db
from .hub import hub
from .schemas import TaskInput, ConfirmInput, ChatCreateInput, ChatMessageInput

logger = logging.getLogger("navigator.api")

db.init_db()

# Only local UIs and browser extensions may talk to this backend. Without
# this, any website the user visits can create tasks and read page data from
# localhost.
_ALLOWED_ORIGIN_PATTERN = (
    r"^(?:https?://(?:localhost|127\.0\.0\.1|\[::1\])(?::\d{1,5})?"
    r"|(?:chrome|moz)-extension://[A-Za-z0-9_-]+)$"
)
_ALLOWED_ORIGIN_RE = re.compile(_ALLOWED_ORIGIN_PATTERN)

TERMINAL_STATES = {"completed", "failed", "cancelled"}

app = FastAPI(title="NAVIGATOR backend", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_origin_regex=_ALLOWED_ORIGIN_PATTERN,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["content-type"],
    allow_credentials=False,
)

# Fire-and-forget task handles kept alive for the lifetime of the process.
_background_tasks: set[asyncio.Task] = set()


def _origin_allowed(origin: Optional[str]) -> bool:
    if not origin:
        return True  # non-browser clients (curl, tests, extension service worker)
    return bool(_ALLOWED_ORIGIN_RE.fullmatch(origin))


@app.middleware("http")
async def origin_guard(request, call_next):
    """Reject cross-site browser requests, including simple form posts that
    CORS would not otherwise block."""
    origin = request.headers.get("origin")
    if origin is not None and not _origin_allowed(origin):
        return JSONResponse({"detail": "origin not allowed"}, status_code=403)
    return await call_next(request)


class TaskCreated(BaseModel):
    task_id: str = Field(alias="taskId")
    chat_id: Optional[str] = Field(default=None, alias="chatId")
    model_config = {"populate_by_name": True}


@app.get("/health")
@app.get("/api/health")
async def health() -> dict[str, Any]:
    selection = config.resolve_provider()
    return {
        "status": "ok",
        "provider": selection["effective"],
        "requestedProvider": selection["requested"],
        "providerFallback": selection["fallback"],
        "providerFallbackReason": selection["reason"],
        "extensionConnected": bool(hub.extension_sockets),
    }


@app.get("/api/chats")
async def get_chats() -> list[dict[str, Any]]:
    return db.list_chats()


@app.post("/api/chat")
async def create_chat_session(body: Optional[ChatCreateInput] = None) -> dict[str, Any]:
    title = body.title if body else None
    chat = db.create_chat(title=title)
    return {
        "chatId": chat["id"],
        "chat_id": chat["id"],
        "title": chat["title"],
        "status": chat["status"],
    }


@app.get("/api/chat/{chat_id}")
async def get_chat_session(chat_id: str) -> dict[str, Any]:
    chat = db.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="chat not found")
    return chat


@app.post("/api/chat/{chat_id}/message")
async def post_chat_message(chat_id: str, body: ChatMessageInput) -> dict[str, Any]:
    chat = db.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="chat not found")
    task = hub.new_task(body.text, chat_id=chat_id)
    task.state = "planning"
    _spawn(_run_task_later(task.task_id))
    return {
        "taskId": task.task_id,
        "task_id": task.task_id,
        "chatId": chat_id,
        "chat_id": chat_id,
    }


@app.delete("/api/chat/{chat_id}")
async def delete_chat_session(chat_id: str) -> dict[str, bool]:
    ok = db.delete_chat(chat_id)
    if not ok:
        raise HTTPException(status_code=404, detail="chat not found")
    return {"ok": True}


@app.get("/api/preferences")
async def get_preferences() -> dict[str, str]:
    return db.get_user_preferences()


@app.post("/api/preferences")
async def set_preferences(body: dict[str, Any]) -> dict[str, str]:
    for k, v in body.items():
        if k and v is not None:
            db.set_user_preference(str(k), str(v))
    return db.get_user_preferences()


@app.delete("/api/preferences/{key}")
async def delete_preference(key: str) -> dict[str, bool]:
    ok = db.delete_user_preference(key)
    return {"ok": ok}


@app.post("/api/task", response_model=TaskCreated)
async def create_task(body: TaskInput) -> TaskCreated:
    chat_id = body.chat_id
    if not chat_id:
        chat = db.create_chat(title=body.instruction[:48].strip() or "New Mission")
        chat_id = chat["id"]
    task = hub.new_task(body.instruction, chat_id=chat_id)
    task.state = "planning"
    _spawn(_run_task_later(task.task_id))
    return TaskCreated(taskId=task.task_id, chatId=chat_id)


def _spawn(coro) -> "asyncio.Task[Any]":
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


async def _run_task_later(task_id: str) -> None:
    await asyncio.sleep(0.08)
    try:
        from .agentLoop import run_task
        await run_task(task_id)
    except Exception as exc:
        task = hub.tasks.get(task_id)
        if task:
            # A cancellation that races with shutdown must not be reported as
            # a failure.
            from .agentLoop import is_cancelled
            cancelled = task.cancelled or is_cancelled(task_id)
            task.state = "cancelled" if cancelled else "failed"
            task.message = "" if cancelled else f"backend error: {exc}"
            await hub.broadcast_progress(task)


@app.get("/api/task/{task_id}")
async def get_task(task_id: str) -> dict[str, Any]:
    task = _require(task_id)
    return {
        "taskId": task.task_id,
        "instruction": task.instruction,
        "taskState": task.state,
        "stepsDone": task.steps_done,
        "message": task.message,
        "pending_confirmation": task.pending_confirmation,
        "screenshot": task.screenshot,
        "plannerMs": task.planner_ms,
        "observeMs": task.observe_ms,
        "actMs": task.act_ms,
    }


@app.post("/api/task/{task_id}/cancel")
async def cancel_task(task_id: str) -> dict[str, str]:
    task = _require(task_id)
    if task.state in TERMINAL_STATES:
        return {"taskState": task.state}

    # The loop only sees this marker; setting the UI state alone used to leave
    # the agent running and executing further actions.
    from .agentLoop import request_cancel
    request_cancel(task_id)

    task.cancelled = True
    task.state = "cancelled"
    task.pending_confirmation = None
    # Wake a pending confirmation gate and any in-flight extension action so
    # the loop observes cancellation immediately instead of after a timeout.
    task.confirm_decision = False
    task.confirm_event.set()
    hub.abort_task_actions(task_id)
    await hub.broadcast_progress(task)
    return {"taskState": "cancelled"}


@app.post("/api/task/{task_id}/confirm")
async def confirm_task(task_id: str, body: ConfirmInput) -> dict[str, str]:
    task = _require(task_id)
    if task.state != "awaiting_confirmation":
        raise HTTPException(409, f"task is {task.state}, not awaiting_confirmation")
    task.confirm_decision = body.decision == "approve"
    task.pending_confirmation = None
    task.confirm_event.set()
    return {"received": "ok"}


def _require(task_id: str):
    task = hub.tasks.get(task_id)
    if task is None:
        raise HTTPException(404, "task not found")
    return task


@app.websocket("/ws/extension")
async def ws_extension(ws: WebSocket) -> None:
    if not _origin_allowed(ws.headers.get("origin")):
        await ws.close(code=1008, reason="origin not allowed")
        return
    await ws.accept()
    hub.set_extension(ws)
    try:
        while True:
            msg = await ws.receive_json()
            if not isinstance(msg, dict):
                continue
            if msg.get("type") != "RESULT":
                continue
            action_id = msg.get("taskId")
            payload = msg.get("payload")
            if isinstance(action_id, str) and isinstance(payload, dict):
                hub.resolve_action(action_id, payload)
            else:
                logger.warning("dropped malformed RESULT message")
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 — never crash the socket handler
        logger.exception("extension websocket failed")
    finally:
        hub.drop_extension(ws)


@app.websocket("/ws/client")
async def ws_client(ws: WebSocket) -> None:
    if not _origin_allowed(ws.headers.get("origin")):
        await ws.close(code=1008, reason="origin not allowed")
        return
    await ws.accept()
    hub.client_sockets.append(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        logger.exception("client websocket failed")
    finally:
        if ws in hub.client_sockets:
            hub.client_sockets.remove(ws)
