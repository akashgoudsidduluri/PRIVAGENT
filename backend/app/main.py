from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import re
import sys
from typing import Any, Optional

if sys.platform == "win32":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from . import config, db
from . import file_uploads
from .auth import verify_token
from .hub import hub
from .monitoring_scheduler import monitoring_lifespan
from .schemas import TaskInput, ConfirmInput, ChatCreateInput, ChatMessageInput, VaultCredentialInput, PaymentValidateInput

logger = logging.getLogger("navigator.api")

db.init_db()


@asynccontextmanager
async def _lifespan(app_instance):
    # Task 2: one-time migration of plaintext secrets out of backend/.env
    # into the encrypted secret store. Idempotent (no-op once migrated)
    # and skipped under pytest. A failed migration is logged and the app
    # keeps running on the .env values — secrets are never deleted
    # without being safely stored first (see secrets.migrate_env_file).
    try:
        from . import secrets as secrets_store

        if not secrets_store.is_pytest():
            secrets_store.migrate_env_file()
    except Exception as exc:
        logger.warning("Secret migration from .env failed (continuing): %s", exc)
    # Task 3: one-time setup for the isolated agent profile. The
    # directory is created (with a marker file so /api/isolation/status
    # can report that it exists); Chrome itself must still be launched
    # with --user-data-dir=<that dir> — see scripts/launch-agent-chrome.*.
    try:
        profile_dir = config.AGENT_PROFILE_DIR
        profile_dir.mkdir(parents=True, exist_ok=True)
        marker = profile_dir / ".privagent-agent-profile"
        if not marker.exists():
            marker.write_text(
                "PrivAgent isolated agent profile.\n"
                "Launch Chrome with --user-data-dir=<this directory> for agent work only.\n"
                "Never point a daily-use Chrome profile at this directory.\n",
                encoding="utf-8",
            )
    except Exception as exc:
        logger.warning("Could not prepare isolated agent profile dir: %s", exc)
    async with monitoring_lifespan(app_instance):
        # Task 9: the in-process scheduler for scheduled tasks. It is
        # best-effort (a restart re-claims due jobs from SQLite); a
        # real deployment should use the OS scheduler instead — see
        # docs/MEMORY_LONG_HORIZON.md. Skipped under pytest.
        from .scheduler import scheduler_lifespan

        async with scheduler_lifespan(app_instance):
            yield

# Only local UIs and browser extensions may talk to this backend. Without
# this, any website the user visits can create tasks and read page data from
# localhost.
_ALLOWED_ORIGIN_PATTERN = (
    r"^(?:https?://(?:localhost|127\.0\.0\.1|\[::1\])(?::\d{1,5})?"
    r"|(?:chrome|moz)-extension://[A-Za-z0-9_-]+)$"
)
_ALLOWED_ORIGIN_RE = re.compile(_ALLOWED_ORIGIN_PATTERN)

TERMINAL_STATES = {"completed", "failed", "cancelled"}

app = FastAPI(title="NAVIGATOR backend", version="0.1.0", lifespan=_lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_origin_regex=_ALLOWED_ORIGIN_PATTERN,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    # "authorization" is required: the web UI sends the local operator
    # token as a Bearer header, and browsers preflight it.
    allow_headers=["content-type", "authorization"],
    allow_credentials=False,
)

# Fire-and-forget task handles kept alive for the lifetime of the process.
_background_tasks: set[asyncio.Task] = set()


def _origin_allowed(origin: Optional[str]) -> bool:
    # A missing Origin used to mean "non-browser client, allow". That
    # let any local process (curl, another local app) bypass the origin
    # check entirely. Missing Origin is now *not* an allow-list pass:
    # such requests must still present a valid auth token (see
    # ``security_guard``). Only an explicitly allow-listed Origin
    # passes this check.
    if not origin:
        return False
    return bool(_ALLOWED_ORIGIN_RE.fullmatch(origin))


# Paths that do not require a bearer token. /api/health is a liveness
# probe. The OAuth callback is a top-level browser redirect from the
# provider: it cannot carry an Authorization header, and its `state`
# parameter is validated server-side by oauth_manager.complete().
_AUTH_EXEMPT_PATHS = {"/api/health", "/health"}
_OAUTH_CALLBACK_RE = re.compile(r"^/api/connectors/[^/]+/callback$")
_STATE_CHANGING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _auth_exempt(path: str) -> bool:
    return path in _AUTH_EXEMPT_PATHS or bool(_OAUTH_CALLBACK_RE.fullmatch(path))


@app.middleware("http")
async def security_guard(request, call_next):
    """Origin + CSRF + local-auth gate for every HTTP request.

    Order matters:
    1. A present-but-disallowed Origin (or Referer) is rejected with
       403, even for otherwise authenticated requests — the token is
       a local secret, and a cross-site page must never be able to
       spend it on the user's behalf.
    2. Every ``/api/*`` path except the exemptions above requires a
       valid bearer token (``Authorization: Bearer <token>``), checked
       in constant time. GET requests may alternatively carry
       ``?token=`` — plain ``<a href>`` artifact preview/download
       links cannot send headers, and that is the only reason the
       query form exists. A missing Origin does NOT bypass this step.
    3. State-changing requests additionally need an allow-listed
       Origin/Referer or a valid bearer token (already enforced by
       step 2 for /api/*), so a simple cross-site form post cannot
       mutate state.
    """
    origin = request.headers.get("origin")
    if origin is not None and not _origin_allowed(origin):
        # Task 10: audit — origin/referer rejections are security events.
        try:
            from .audit import append_event as _audit_event

            _audit_event("auth_failure", f"origin not allowed: {origin[:120]}", actor="system")
        except Exception:
            pass
        return JSONResponse({"detail": "origin not allowed"}, status_code=403)
    referer = request.headers.get("referer")
    if referer is not None:
        referer_origin = re.match(r"^(https?://[^/]+)", referer)
        if referer_origin and not _origin_allowed(referer_origin.group(1)):
            try:
                from .audit import append_event as _audit_event2

                _audit_event2("auth_failure",
                              f"referer not allowed: {referer_origin.group(1)[:120]}", actor="system")
            except Exception:
                pass
            return JSONResponse({"detail": "referer not allowed"}, status_code=403)
    if request.method == "OPTIONS":
        # CORS preflight: browsers never send Authorization on a
        # preflight, so it cannot pass the token check below. Let
        # CORSMiddleware answer it — it only echoes allow-listed
        # origins, so a disallowed origin still gets no ACAO header
        # and the browser blocks the real request. Without this the
        # web UI is blocked by CORS on every authenticated call.
        return await call_next(request)
    path = request.url.path
    if path.startswith("/api/") and not _auth_exempt(path):
        token: Optional[str] = None
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()
        elif request.method == "GET":
            # Anchor-navigation fallback (artifact preview/download).
            token = request.query_params.get("token")
        if not verify_token(token):
            # Task 10: audit — failed local-auth attempts (no token
            # value is ever recorded, only the path and method).
            try:
                from .audit import append_event as _audit_event3

                _audit_event3("auth_failure",
                              f"invalid local auth token for {request.method} {path}",
                              actor="system")
            except Exception:
                pass
            return JSONResponse(
                {"detail": "missing or invalid local auth token"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
    return await call_next(request)


# Backwards-compatible alias: older code/tests referenced origin_guard.
origin_guard = security_guard


class TaskCreated(BaseModel):
    task_id: str = Field(alias="taskId")
    chat_id: Optional[str] = Field(default=None, alias="chatId")
    model_config = {"populate_by_name": True}


class AttachmentInput(BaseModel):
    file_name: str = Field(alias="fileName")
    base64: str


@app.get("/api/connectors")
def connector_status() -> list[dict]:
    from .connectors.oauth import oauth_manager
    return oauth_manager.status()


@app.post("/api/connectors/{provider}/start")
async def start_connector(provider: str) -> dict[str, str]:
    from .connectors.oauth import oauth_manager
    try:
        return {"authorizationUrl": oauth_manager.start(provider)}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


class ConnectorConfigureInput(BaseModel):
    client_id: Optional[str] = Field(default=None, alias="clientId")
    client_secret: Optional[str] = Field(default=None, alias="clientSecret")
    access_token: Optional[str] = Field(default=None, alias="accessToken")
    refresh_token: Optional[str] = Field(default=None, alias="refreshToken")
    model_config = {"populate_by_name": True}


@app.post("/api/connectors/{provider}/configure")
async def configure_connector(provider: str, body: ConnectorConfigureInput) -> dict[str, Any]:
    from .connectors.oauth import oauth_manager
    if provider not in {item["provider"] for item in oauth_manager.status()}:
        raise HTTPException(404, "unknown connector")
    auth_url = None
    if body.access_token or body.refresh_token:
        oauth_manager.set_tokens(provider, access_token=body.access_token, refresh_token=body.refresh_token)
        # Automatically update active email mission account email
        try:
            conn = oauth_manager.connector("gmail" if provider == "google" else "outlook")
            if getattr(conn, "configured", False):
                prof = conn.get_profile()
                email_addr = prof.get("emailAddress") or prof.get("mail")
                if email_addr:
                    logger.info("Live OAuth connected for %s: %s", provider, email_addr)
                    from .email_intelligence.sync_engine import run_email_mission_tick
                    missions = db.list_email_missions()
                    if missions:
                        primary = missions[0]
                        db.update_email_mission(primary["id"], {"account_email": email_addr, "sync_cursor": "", "status": "active"})
                        # Deactivate or clean up any redundant missions to avoid competing sync loops
                        for m in missions[1:]:
                            db.update_email_mission(m["id"], {"status": "paused"})
                        asyncio.create_task(run_email_mission_tick(primary["id"]))
                    else:
                        new_m = db.create_email_mission(account_email=email_addr, provider="gmail" if provider == "google" else "outlook")
                        asyncio.create_task(run_email_mission_tick(new_m["id"]))
        except Exception as exc:
            logger.warning("Could not sync email profile for %s: %s", provider, exc)
    elif body.client_id and body.client_secret:
        oauth_manager.set_credentials(provider, body.client_id, body.client_secret)
        try:
            auth_url = oauth_manager.start(provider)
        except Exception:
            pass
    else:
        raise HTTPException(400, "Must provide accessToken, refreshToken, or (clientId and clientSecret)")
    return {
        "ok": True,
        "authorizationUrl": auth_url,
        "status": [item for item in oauth_manager.status() if item["provider"] == provider][0],
    }


@app.get("/api/connectors/{provider}/callback")
async def connector_callback(provider: str, code: str = "", state: str = ""):
    from .connectors.oauth import oauth_manager
    try:
        oauth_manager.complete(provider, code, state)
    except (PermissionError, RuntimeError, KeyError, ValueError) as exc:
        raise HTTPException(400, "OAuth authorization failed or expired") from None
    return RedirectResponse("http://localhost:5173/agent.html#connectors", status_code=303)


@app.delete("/api/connectors/{provider}")
async def disconnect_connector(provider: str) -> dict[str, bool]:
    from .connectors.oauth import oauth_manager
    if provider not in {item["provider"] for item in oauth_manager.status()}:
        raise HTTPException(404, "unknown connector")
    oauth_manager.disconnect(provider)
    return {"ok": True}


@app.post("/api/attachments")
async def create_attachment(body: AttachmentInput) -> dict[str, str]:
    try:
        token = file_uploads.put(body.file_name, body.base64)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return {"fileToken": token, "fileName": body.file_name}


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
    from .providers.openai_compatible import seeded_models, DEEPAI_FREE_MODELS
    seeded = seeded_models()
    try:
        provider = provider_settings.provider_for_runtime()
    except Exception as exc:
        return {
            "discovered": False,
            "reason": str(exc),
            "seeded": seeded,
            "models": list(seeded),
            "free_models": DEEPAI_FREE_MODELS,
        }
    list_models = getattr(provider, "list_models", None)
    if list_models is None:
        return {
            "discovered": False,
            "reason": f"Provider '{provider.name() if hasattr(provider, 'name') else type(provider).__name__}' does not support model discovery.",
            "seeded": seeded,
            "models": list(seeded),
            "free_models": DEEPAI_FREE_MODELS,
        }
    try:
        models = list_models()
    except Exception as exc:
        return {
            "discovered": False,
            "reason": str(exc),
            "seeded": seeded,
            "models": list(seeded),
            "free_models": DEEPAI_FREE_MODELS,
        }
    merged: list[str] = []
    seen: set[str] = set()
    # Prioritize the 5 free models at top
    for m in seeded + list(models):
        if m not in seen:
            seen.add(m)
            merged.append(m)
    return {
        "discovered": True,
        "models": merged,
        "seeded": seeded,
        "free_models": DEEPAI_FREE_MODELS,
        "reason": None,
    }


# NOTE (Task 1, security): the former POST /api/test/execute_action
# endpoint was removed from the production app. It was unauthenticated
# and drove the extension directly, bypassing the planner, the privacy
# redaction, and the confirmation gate. Driving the extension directly
# is what the agent loop already does internally (hub.send_action);
# tests that need that behaviour should exercise the loop, not an
# unauthenticated HTTP backdoor. Do not re-add an unauthenticated
# action-execution endpoint. If a test-only endpoint is ever needed
# again, it must sit behind BOTH PRIVAGENT_ENABLE_TEST_ENDPOINTS=true
# AND a valid local auth token, and must still pass through privacy
# redaction and the confirmation gate.


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
    from .hub import TERMINAL_STATES
    active_task = None
    for t in hub.tasks.values():
        if getattr(t, "chat_id", None) == chat_id and t.state not in TERMINAL_STATES:
            cp = db.get_task_checkpoint(t.task_id)
            steps = []
            if cp and cp.get("history"):
                steps = db.format_history_steps(cp["history"])
            elif getattr(t, "steps", None):
                steps = t.steps
            active_task = {
                "taskId": t.task_id,
                "taskState": t.state,
                "stepsDone": t.steps_done,
                "message": t.message,
                "screenshot": t.screenshot,
                "intent": t.intent,
                "isBrowserTask": t.intent != "NON_BROWSER" if t.intent else True,
                "sessionId": t.session_id,
                "primaryTabId": t.primary_tab_id,
                "childTabIds": t.child_tab_ids,
                "steps": steps,
            }
            break
    chat["active_task"] = active_task
    return chat


@app.post("/api/chat/{chat_id}/message")
async def post_chat_message(chat_id: str, body: ChatMessageInput) -> dict[str, Any]:
    chat = db.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="chat not found")
    if body.file_token and not file_uploads.get(body.file_token):
        raise HTTPException(status_code=422, detail="attachment expired")
    # Task 3: chat messages inherit the global allow-list (no per-task
    # list is available on this route); None when unconfigured.
    task = hub.new_task(
        body.text,
        chat_id=chat_id,
        allowed_domains=list(config.ALLOWED_DOMAINS) if config.ALLOWED_DOMAINS else None,
    )
    task.file_token = body.file_token
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
            try:
                db.set_user_preference(str(k), str(v))
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
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
    if body.file_token and not file_uploads.get(body.file_token):
        raise HTTPException(status_code=422, detail="attachment expired")
    # Task 3: per-task allow-list wins; otherwise the global
    # PRIVAGENT_ALLOWED_DOMAINS list; otherwise None (not configured).
    # An unconfigured allow-list is a warning, not an error — the
    # operator is expected to set one before real browsing tasks.
    from .allowlist import effective_allowlist

    effective = effective_allowlist(body.allowed_domains, config.ALLOWED_DOMAINS)
    if not effective:
        logger.warning(
            "no domain allow-list configured for task (set PRIVAGENT_ALLOWED_DOMAINS "
            "or pass allowedDomains); navigation is unrestricted for this task"
        )
    task = hub.new_task(body.instruction, chat_id=chat_id, allowed_domains=effective, long_horizon=body.long_horizon, background=body.background)
    task.file_token = body.file_token
    task.state = "planning"
    _spawn(hub.broadcast_progress(task))
    _spawn(_run_task_later(task.task_id))
    return TaskCreated(taskId=task.task_id, chatId=chat_id)


@app.get("/api/isolation/status")
async def isolation_status() -> dict[str, Any]:
    """Task 3: report the isolation posture honestly.

    ``profileVerified`` is True only when the connected extension
    self-reports that it runs in the isolated agent profile. Chrome
    does not expose the profile directory to extensions, so this is an
    operator-attested flag, not proof — the docs say so. ``cloudSandbox``
    is always False: there is no cloud VM in this design.
    """
    profile_dir = config.AGENT_PROFILE_DIR
    return {
        "profileDir": str(profile_dir),
        "profileDirExists": profile_dir.exists(),
        "profileVerified": bool(hub.extension_profile.get("isolatedProfile")),
        "extensionConnected": bool(hub.extension_sockets),
        "extensionProfile": dict(hub.extension_profile),
        "allowedDomainsConfigured": bool(config.ALLOWED_DOMAINS),
        "allowedDomains": list(config.ALLOWED_DOMAINS),
        "trackerBlocking": "declarativeNetRequest ruleset (best-effort, known tracker hosts only)",
        "cloudSandbox": False,
    }


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
            task.message = "" if cancelled else "Backend task failed; inspect local diagnostics."
            await hub.broadcast_progress(task)


@app.get("/api/task/{task_id}")
async def get_task(task_id: str) -> dict[str, Any]:
    task = _require(task_id)
    from .privacy import scrub_persistent
    cp = db.get_task_checkpoint(task_id)
    steps = []
    if cp and cp.get("history"):
        steps = db.format_history_steps(cp["history"])
    elif getattr(task, "steps", None):
        steps = task.steps
    return {
        "taskId": task.task_id,
        "instruction": scrub_persistent(task.instruction),
        "taskState": task.state,
        "stepsDone": task.steps_done,
        "message": scrub_persistent(task.message),
        "pending_confirmation": scrub_persistent(task.pending_confirmation),
        # Task 4: the active takeover request, if any (kind/reason/url
        # only — never a secret). The UI shows its banner from this.
        "pending_takeover": task.takeover,
        "screenshot": task.screenshot,
        "plannerMs": task.planner_ms,
        "observeMs": task.observe_ms,
        "actMs": task.act_ms,
        # Task 11: per-step loop timings (durations + counts + action
        # types only — never page text, names, or values).
        "timings": task.timings,
        "intent": task.intent,
        "sessionId": task.session_id,
        "primaryTabId": task.primary_tab_id,
        "childTabIds": task.child_tab_ids,
        "isBrowserTask": task.intent != "NON_BROWSER" if task.intent else True,
        # Task 9: long-horizon / background flags, the task's tab
        # registry, and the persisted checkpoint (step, url, compaction
        # summary, memory proposals awaiting operator confirmation).
        "longHorizon": task.long_horizon,
        "background": task.background,
        "allowedDomains": task.allowed_domains,
        "tabs": task.tabs,
        # Task 14: routing decision (connector / browser / webmcp tool
        # / ucp checkout) — names and reasons only, never content.
        "route": scrub_persistent(task.route),
        "checkpoint": {
            "stepIndex": cp.get("step_index", 0) if cp else 0,
            "lastUrl": cp.get("last_url", "") if cp else "",
            "summary": (cp.get("working_memory") or {}).get("long_horizon_summary") if cp else None,
            "proposals": (cp.get("working_memory") or {}).get("memory_proposals") if cp else None,
        } if cp else None,
        "steps": steps,
    }


@app.post("/api/task/{task_id}/cancel")
async def cancel_task(task_id: str) -> dict[str, str]:
    task = _require(task_id)
    if task.state in TERMINAL_STATES:
        return {"taskState": task.state}

    # Task 9: checkpoint first, then cancel — so a cancelled task can
    # still be resumed from where it stopped. The loop already
    # checkpoints after every step; this only writes when no
    # checkpoint exists yet (cancelled before the first step).
    try:
        from .agentLoop import save_cancel_checkpoint

        last_url = ""
        obs = getattr(task, "last_observation", None)
        if isinstance(obs, dict):
            last_url = str(obs.get("url") or "")
        elif obs is not None:
            last_url = str(getattr(obs, "url", "") or "")
        save_cancel_checkpoint(task_id, task.instruction, task.steps_done, last_url)
    except Exception:
        pass

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
    # Task 4: a cancel must also resolve a pending takeover, or the loop
    # would wait out the whole takeover timeout.
    task.takeover = None
    task.takeover_decision = False
    task.takeover_event.set()
    hub.abort_task_actions(task_id)
    if getattr(task, "session_id", None):
        from .session_manager import session_manager
        session_manager.update_status(task.session_id, "cancelled")
    await hub.broadcast_progress(task)
    return {"taskState": "cancelled"}


@app.post("/api/task/{task_id}/pause")
async def pause_task(task_id: str) -> dict[str, str]:
    task = _require(task_id)
    if task.state not in {"running", "planning"}:
        raise HTTPException(409, f"task is {task.state}, cannot pause")
    from .agentLoop import request_pause
    request_pause(task_id)
    task.state = "paused"
    task.message = "Mission paused at the next action boundary."
    await hub.broadcast_progress(task)
    return {"taskState": "paused"}


@app.post("/api/task/{task_id}/resume")
async def resume_task(task_id: str) -> dict[str, str]:
    task = _require(task_id)
    if task.state not in ("paused", "interrupted"):
        raise HTTPException(409, f"task is {task.state}, cannot resume")
    from .agentLoop import request_resume, _pause_events
    is_in_memory_paused = task_id in _pause_events
    request_resume(task_id)
    task.state = "running"
    task.message = "Mission resumed."
    await hub.broadcast_progress(task)
    if not is_in_memory_paused:
        _spawn(_run_task_later(task.task_id))
    return {"taskState": "running"}


@app.post("/api/task/{task_id}/confirm")
async def confirm_task(task_id: str, body: ConfirmInput) -> dict[str, str]:
    task = _require(task_id)
    if task.state != "awaiting_confirmation":
        raise HTTPException(409, f"task is {task.state}, not awaiting_confirmation")
    task.confirm_decision = body.decision in ("approve", "approve_step")
    # Task 12: step-by-step approval — the loop consumes this in the
    # batch gate (no pending_batch is pinned after the approved step).
    # A plain approve or a reject clears any stale flag.
    task.confirm_step_only = body.decision == "approve_step"
    task.pending_confirmation = None
    task.confirm_event.set()
    # Task 10: audit — every confirmation decision, approved or denied.
    try:
        from .audit import append_event as _audit_event

        _audit_event(
            "confirmation_approved" if task.confirm_decision else "confirmation_denied",
            f"confirmation {body.decision} for task {task_id}",
            task_id=task_id,
        )
    except Exception:
        pass
    return {"received": "ok"}


# --- Task 4: takeover -------------------------------------------------------
# When the loop reaches a login / payment / OTP / CAPTCHA / 2FA surface it
# stops observing and hands the browser to the operator. These endpoints
# are the operator's side of that handoff: read what is waiting, say
# "I finished it, resume", or cancel the task. All routes are behind the
# security_guard bearer auth (see the middleware above).
@app.get("/api/task/{task_id}/takeover")
async def get_takeover(task_id: str) -> dict[str, Any]:
    task = _require(task_id)
    return {"taskId": task.task_id, "taskState": task.state, "takeover": task.takeover}


@app.post("/api/task/{task_id}/takeover/resume")
async def resume_takeover(task_id: str) -> dict[str, str]:
    task = _require(task_id)
    if task.state != "awaiting_takeover":
        raise HTTPException(409, f"task is {task.state}, not awaiting_takeover")
    # The operator completed the manual step (login / OTP / payment) in
    # the tab. The loop re-observes from scratch on resume; its first
    # post-takeover observation carries no screenshot (see agentLoop).
    task.takeover_decision = True
    task.takeover = None
    task.state = "running"
    task.message = "Takeover complete — resuming."
    task.takeover_event.set()
    await hub.notify_extension({"type": "TAKEOVER_END", "taskId": task_id})
    await hub.broadcast_progress(task)
    return {"taskState": "running"}


@app.post("/api/task/{task_id}/takeover/cancel")
async def cancel_takeover(task_id: str) -> dict[str, str]:
    task = _require(task_id)
    if task.state != "awaiting_takeover":
        raise HTTPException(409, f"task is {task.state}, not awaiting_takeover")
    task.takeover_decision = False
    task.takeover = None
    task.state = "cancelled"
    task.message = "Takeover cancelled by the operator."
    task.takeover_event.set()
    await hub.notify_extension({"type": "TAKEOVER_END", "taskId": task_id})
    await hub.broadcast_progress(task)
    return {"taskState": "cancelled"}


# --- Task 4: credential vault ----------------------------------------------
# Metadata only, always. Values are stored encrypted (Task 2 store) and
# are never returned, logged, or shown in the UI after saving.
@app.post("/api/vault/credentials")
async def add_vault_credential(body: VaultCredentialInput) -> dict[str, Any]:
    from .vault import add_credential

    try:
        meta = add_credential(
            domain=body.domain, kind=body.kind, label=body.label,
            username=body.username, value=body.value,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"stored": True, "credential": meta.to_dict()}


@app.get("/api/vault/credentials")
async def list_vault_credentials() -> dict[str, Any]:
    from .vault import list_credentials

    return {"credentials": [m.to_dict() for m in list_credentials()]}


@app.delete("/api/vault/credentials/{domain}/{kind}/{label}")
async def delete_vault_credential(domain: str, kind: str, label: str) -> dict[str, Any]:
    from .vault import delete_credential

    removed = delete_credential(domain, kind, label)
    if not removed:
        raise HTTPException(status_code=404, detail="no such credential")
    return {"deleted": True}


# --- Task 4: payments (mock-first) ------------------------------------------
# No real card processing. These endpoints expose the configured limit
# and the fail-closed validation the loop uses, and the audit log
# (amount / merchant / last-4 / approval id only — never card details).
@app.get("/api/payments/config")
async def payments_config() -> dict[str, Any]:
    return {
        "provider": "mock",
        "maxPaymentAmount": config.MAX_PAYMENT_AMOUNT,
        "currency": config.PAYMENT_CURRENCY,
        "requiresApproval": True,
        "requiresTakeover": True,
        "note": "Mock provider only. No live Stripe integration exists yet; see docs/CREDENTIALS_PAYMENTS.md.",
    }


@app.post("/api/payments/validate")
async def validate_payment(body: PaymentValidateInput) -> dict[str, Any]:
    from .payments import validate_payment

    decision = validate_payment(
        amount=body.amount,
        currency=body.currency,
        merchant_domain=body.merchant_domain,
        max_amount=config.MAX_PAYMENT_AMOUNT,
        expected_currency=config.PAYMENT_CURRENCY,
    )
    # Task 10: audit — every payment validation attempt (amount /
    # merchant / decision only; never card details).
    try:
        from .audit import append_event as _audit_event

        _audit_event(
            "payment_attempt",
            f"payment validation: {body.amount} {body.currency} at "
            f"{body.merchant_domain} -> {'allowed' if decision.allowed else 'blocked'}"
            f"{'' if decision.allowed else ': ' + (decision.reason or '')}",
        )
    except Exception:
        pass
    return decision.to_dict()


@app.get("/api/payments/audit")
async def payments_audit() -> dict[str, Any]:
    from .payments import read_audit_log

    return {"entries": read_audit_log()}


# --- Task 10: audit log -----------------------------------------------------
# Append-only, hash-chained record of security-relevant events. Behind
# the Task 1 local-auth guard like every other /api route. Use
# GET /api/audit?event=confirmation_denied&limit=50 to filter.
@app.get("/api/audit")
async def get_audit_log(event: Optional[str] = None, limit: int = 100) -> dict[str, Any]:
    from .audit import read_events, verify_chain

    limit = max(1, min(int(limit or 100), 1000))
    ok, broken = verify_chain()
    return {
        "entries": read_events(event=event, limit=limit),
        "chainOk": ok,
        "brokenIndex": broken,
    }


# --- Task 9: scheduled tasks ----------------------------------------------
# Jobs are instructions + a human schedule ("every 6h", "daily 09:00"
# UTC, or a 5-field cron), stored in SQLite. The in-process runner
# claims due jobs and runs them as normal background tasks — through
# the SAME gates as interactive tasks (allow-list, confirmation,
# takeover, payment limits). A scheduled run that needs the operator
# halts in awaiting_confirmation / awaiting_takeover; it never
# self-approves. The runner is best-effort: see docs/MEMORY_LONG_HORIZON.md.
class ScheduledJobInput(BaseModel):
    instruction: str = Field(min_length=1, max_length=config.MAX_INSTRUCTION_CHARS)
    schedule: str = Field(min_length=1, max_length=64)
    allowed_domains: Optional[list[str]] = Field(default=None, alias="allowedDomains")
    long_horizon: bool = Field(default=False, alias="longHorizon")
    model_config = {"populate_by_name": True}


@app.post("/api/scheduled", status_code=201)
async def create_scheduled_job(body: ScheduledJobInput) -> dict[str, Any]:
    from .scheduler import create_job

    try:
        return create_job(
            body.instruction, body.schedule,
            allowed_domains=body.allowed_domains,
            long_horizon=body.long_horizon,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/scheduled")
async def list_scheduled_jobs() -> dict[str, Any]:
    from .scheduler import list_jobs

    return {"jobs": list_jobs()}


@app.delete("/api/scheduled/{job_id}")
async def delete_scheduled_job(job_id: str) -> dict[str, bool]:
    from .scheduler import delete_job

    if not delete_job(job_id):
        raise HTTPException(status_code=404, detail="scheduled job not found")
    return {"deleted": True}


@app.post("/api/scheduled/{job_id}/run-now")
async def run_scheduled_job_now(job_id: str) -> dict[str, Any]:
    from .scheduler import get_job, start_job

    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="scheduled job not found")
    task_id, _task = start_job(job)
    record = hub.tasks.get(task_id)
    if record is not None:
        await hub.broadcast_progress(record)
    return {"taskId": task_id}


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
            state = "interrupted"
            message = (
                f"Backend restarted before this task finished; "
                f"progress preserved at step {cp.get('step_index', 0)}."
            )
            wm = cp.get("working_memory") or {}
            # REVIEW (Task 9 audit): the loop now records its terminal
            # outcome in the checkpoint (final_state). Trust it first:
            # the message heuristic below cannot see scheduled tasks
            # (they have no chat), so they used to come back as
            # "interrupted" even when they had completed. Also restore
            # the allow-list, long-horizon flag, background flag and
            # tab registry the loop checkpointed — without the
            # allow-list a resumed task would run unrestricted.
            final_state = wm.get("final_state")
            if final_state in ("completed", "failed", "cancelled"):
                state = final_state
                message = (
                    f"Task {final_state} before the backend restarted; "
                    f"progress preserved at step {cp.get('step_index', 0)}."
                    if final_state != "completed"
                    else "Task completed."
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
                allowed_domains=list(wm["allowed_domains"]) if isinstance(wm.get("allowed_domains"), list) else None,
                long_horizon=bool(wm.get("long_horizon")),
                background=bool(wm.get("background")),
                tabs=[dict(t) for t in (wm.get("tabs") or []) if isinstance(t, dict)],
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
        # Extension service workers may connect without an Origin
        # header; in that case a valid ?token= is the fallback gate.
        # A wrong token is always rejected, and a missing token is
        # accepted only for now (first-run pairing, before the
        # extension has anywhere to store a token) and logged — a
        # follow-up task should make the token mandatory here once the
        # extension has a token-entry UI.
        if not verify_token(ws.query_params.get("token")):
            await ws.close(code=1008, reason="origin not allowed")
            return
        logger.warning("extension websocket connected without an Origin header; token accepted as fallback")
    elif ws.query_params.get("token") is not None and not verify_token(ws.query_params.get("token")):
        await ws.close(code=1008, reason="invalid token")
        return
    await ws.accept()
    hub.set_extension(ws)
    try:
        while True:
            msg = await ws.receive_json()
            if not isinstance(msg, dict):
                continue
            # Task 3: the extension self-reports its profile on connect
            # (HELLO). Chrome does not expose the profile directory to
            # extensions, so this is operator-attested, not verified —
            # see /api/isolation/status.
            if msg.get("type") == "HELLO":
                profile = msg.get("profile") or {}
                if isinstance(profile, dict):
                    hub.set_extension_profile(profile)
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
        # Same fallback as /ws/extension: a browser tab always sends
        # an Origin, so a missing one means a non-browser client and
        # must present a valid ?token= instead.
        if not verify_token(ws.query_params.get("token")):
            await ws.close(code=1008, reason="origin not allowed")
            return
    elif ws.query_params.get("token") is not None and not verify_token(ws.query_params.get("token")):
        await ws.close(code=1008, reason="invalid token")
        return
    await ws.accept()
    hub.client_sockets.append(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except (ConnectionResetError, BrokenPipeError, OSError):
        pass
    except Exception:  # noqa: BLE001
        logger.exception("client websocket failed")
    finally:
        if ws in hub.client_sockets:
            hub.client_sockets.remove(ws)

# Local control-center surfaces share the application's origin guard.
from .control_center import router as control_center_router
app.include_router(control_center_router)

from .mcp.api import router as mcp_router
app.include_router(mcp_router)
from .capabilities_api import router as capabilities_router
app.include_router(capabilities_router)
from .memory.api import router as memory_router
app.include_router(memory_router)
from .long_memory import router as long_memory_router
app.include_router(long_memory_router)
from .email_api import router as email_router
app.include_router(email_router)
