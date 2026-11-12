from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import re
import sys
import uuid
from typing import Any, Optional

if sys.platform == "win32":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
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
    return {
        "status": "ok",
        "provider": config.PROVIDER,
        "extensionConnected": bool(hub.extension_sockets),
        "maxSteps": config.MAX_STEPS,
        "allowScreenshots": config.ALLOW_SCREENSHOTS,
    }


@app.post("/api/extension/reload")
async def reload_extension() -> dict[str, Any]:
    if not hub.extension_sockets:
        raise HTTPException(status_code=503, detail="extension not connected")
    return await hub.send_action("reload", {"type": "reload_extension"}, timeout_s=5.0)


# ---------------------------------------------------------------------------
# LLM provider selection (/api/llm/*) — state lives in provider_settings.
# The API key is write-only from the API surface: no endpoint returns it.
# ---------------------------------------------------------------------------


class LLMConfigInput(BaseModel):
    provider: Optional[str] = None
    baseUrl: Optional[str] = None
    apiKey: Optional[str] = None
    model: Optional[str] = None
    reset: bool = False


@app.get("/api/llm/config")
async def get_llm_config() -> dict[str, Any]:
    from .providers import provider_settings
    return provider_settings.public_state()


@app.post("/api/llm/config")
async def set_llm_config(body: LLMConfigInput) -> dict[str, Any]:
    from .providers import provider_settings
    try:
        return provider_settings.configure(
            provider=body.provider,
            base_url=body.baseUrl,
            api_key=body.apiKey,
            model=body.model,
            reset=body.reset,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/llm/models")
async def discover_llm_models() -> dict[str, Any]:
    """Discover models from the configured OpenAI-compatible gateway.

    Never falls back silently: on failure we return the reason so the UI can
    offer manual model entry honestly, plus the static seed list either way.
    """
    from .providers import provider_settings
    from .providers.openai_compatible import seeded_models
    seeded = seeded_models()
    try:
        provider = provider_settings.provider_for_runtime()
    except Exception as exc:
        return {"discovered": False, "reason": str(exc), "seeded": seeded, "models": list(seeded)}
    list_models = getattr(provider, "list_models", None)
    if list_models is None:
        return {
            "discovered": False,
            "reason": f"Provider '{provider.name() if hasattr(provider, 'name') else type(provider).__name__}' does not support model discovery.",
            "seeded": seeded,
            "models": list(seeded),
        }
    try:
        models = list_models()
    except Exception as exc:
        return {
            "discovered": False,
            "reason": str(exc),
            "seeded": seeded,
            "models": list(seeded),
        }
    merged: list[str] = []
    seen: set[str] = set()
    for m in list(models) + seeded:
        if m not in seen:
            seen.add(m)
            merged.append(m)
    return {"discovered": True, "models": merged, "seeded": seeded, "reason": None}


@app.post("/api/test/execute_action")
async def test_execute_action(body: dict[str, Any]) -> dict[str, Any]:
    if not hub.extension_sockets:
        raise HTTPException(status_code=503, detail="extension not connected")
    tid = body.get("taskId") or f"test-{uuid.uuid4().hex[:8]}"
    return await hub.send_action(tid, body.get("action", {}), timeout_s=body.get("timeout_s", 15.0))


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
    _spawn(hub.broadcast_progress(task))
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
    _spawn(hub.broadcast_progress(task))
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
            if task.state != "cancelled":
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
        "intent": task.intent,
        "sessionId": task.session_id,
        "primaryTabId": task.primary_tab_id,
        "childTabIds": task.child_tab_ids,
        "isBrowserTask": task.intent != "NON_BROWSER" if task.intent else True,
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
    if getattr(task, "session_id", None):
        from .session_manager import session_manager
        session_manager.update_status(task.session_id, "cancelled")
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
    if task is not None:
        return task
    # Restore from SQLite task_checkpoints if task was saved but pruned from memory
    try:
        from . import db
        from .hub import TaskRecord
        cp = db.get_task_checkpoint(task_id)
        if cp:
            sess_id = cp.get("session_id")
            primary_tab = None
            child_tabs = []
            if sess_id:
                sess_data = db.get_browser_session(sess_id)
                if sess_data:
                    primary_tab = sess_data.get("primary_tab_id")
                    child_tabs = sess_data.get("child_tab_ids", [])
            # A checkpoint alone only proves progress, not completion. Derive the
            # restored state from the recorded final agent message instead of
            # assuming "completed" whenever step_index > 0 (which used to report
            # crashed/interrupted tasks as successful).
            state = "failed"
            message = (
                f"Backend restarted before this task finished; "
                f"progress preserved at step {cp.get('step_index', 0)}."
            )
            chat = None
            if cp.get("conversation_id"):
                try:
                    chat = db.get_chat(cp["conversation_id"])
                except Exception:
                    chat = None
            messages = (chat or {}).get("messages") or []
            last_agent = next(
                (m for m in reversed(messages) if m.get("role") == "agent"), None
            )
            if last_agent:
                evidence = (last_agent.get("evidence") or "").lower()
                if evidence.startswith("completed"):
                    state = "completed"
                    message = last_agent.get("content") or "Task completed."
                elif last_agent.get("content"):
                    message = last_agent["content"]
            restored = TaskRecord(
                task_id=task_id,
                instruction=cp.get("working_memory", {}).get("instruction") or "Task",
                chat_id=cp.get("conversation_id"),
                state=state,
                steps_done=cp.get("step_index", 0),
                message=message,
                session_id=sess_id,
                primary_tab_id=primary_tab,
                child_tab_ids=child_tabs,
            )
            hub.tasks[task_id] = restored
            return restored
    except Exception:
        pass
    raise HTTPException(404, "task not found")


# ---------------------------------------------------------------------------
# Artifact Deliverables & Storage API
# ---------------------------------------------------------------------------

ARTIFACTS_DIR = (Path(__file__).resolve().parents[1] / "data" / "artifacts").resolve()
ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)


@app.get("/api/tasks/{task_id}/artifacts")
async def get_task_artifacts(task_id: str) -> list[dict[str, Any]]:
    return db.list_task_artifacts(task_id)


@app.get("/api/chats/{chat_id}/artifacts")
async def get_chat_artifacts(chat_id: str) -> list[dict[str, Any]]:
    return db.list_conversation_artifacts(chat_id)


@app.get("/api/artifacts/{artifact_id}")
async def get_artifact_metadata(artifact_id: str) -> dict[str, Any]:
    art = db.get_artifact(artifact_id)
    if not art:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return art


@app.get("/api/artifacts/{artifact_id}/download")
async def download_artifact(artifact_id: str) -> FileResponse:
    art = db.get_artifact(artifact_id)
    if not art:
        raise HTTPException(status_code=404, detail="Artifact not found")

    file_path = Path(art.get("filepath", "")).resolve()

    # Path traversal validation: file must reside strictly inside ARTIFACTS_DIR
    try:
        file_path.relative_to(ARTIFACTS_DIR)
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied: path traversal attempt")

    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Artifact file not found on disk")

    return FileResponse(
        path=str(file_path),
        filename=art["filename"],
        media_type=art["mime_type"],
        headers={"Content-Disposition": f'attachment; filename="{art["filename"]}"'},
    )


@app.get("/api/artifacts/{artifact_id}/preview")
async def preview_artifact(artifact_id: str) -> FileResponse:
    art = db.get_artifact(artifact_id)
    if not art:
        raise HTTPException(status_code=404, detail="Artifact not found")

    file_path = Path(art.get("filepath", "")).resolve()

    # Path traversal validation: file must reside strictly inside ARTIFACTS_DIR
    try:
        file_path.relative_to(ARTIFACTS_DIR)
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied: path traversal attempt")

    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Artifact file not found on disk")

    return FileResponse(
        path=str(file_path),
        filename=art["filename"],
        media_type=art["mime_type"],
        headers={"Content-Disposition": f'inline; filename="{art["filename"]}"'},
    )


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
