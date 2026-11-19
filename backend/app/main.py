from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import re
import sys
from typing import Any, Optional
from urllib.parse import urlparse

if sys.platform == "win32":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
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
    # Task 4 (latency): warm the loop's one-time costs at boot — Fernet
    # key derivation + redaction modules' first imports — so the first
    # task after a (re)start does not pay ~200 ms extra on its first
    # step. Best-effort; a failure just means the first task pays the
    # cost as before. _run_loop warms again per task as a backstop for
    # paths that never went through this lifespan (~0 ms once warm).
    try:
        from .loop_helpers import warm_loop_caches

        warm_loop_caches()
    except Exception as exc:
        logger.warning("Loop warm-up failed (continuing): %s", exc)
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
_AUTH_EXEMPT_PATHS = {"/api/health", "/health", "/api/auth/signup", "/api/auth/login"}
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
        token_from_header = False
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()
            token_from_header = True
        elif request.method == "GET":
            # Anchor-navigation fallback (artifact preview/download).
            token = request.query_params.get("token")
        # A user session token is accepted as well as the local token —
        # but only from the Authorization header. Session tokens must
        # never travel in URLs: query strings land in access logs.
        authed = verify_token(token)
        user_id: Optional[str] = None
        if not authed and token_from_header and token:
            from .auth_users import verify_session

            user_id = verify_session(token)
            authed = user_id is not None
        request.state.user_id = user_id
        request.state.auth_kind = "session" if user_id is not None else "local"
        if not authed:
            # Task 10: audit — failed local-auth attempts (no token
            # value is ever recorded, only the path and method).
            try:
                from .audit import append_event as _audit_event3

                _audit_event3("auth_failure",
                              f"invalid auth token for {request.method} {path}",
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
def connector_status(request: Request = None) -> list[dict]:
    from .connectors.oauth import oauth_manager
    # A signed-in user sees their own connections; the local-token
    # operator sees the legacy global connections (unchanged).
    return oauth_manager.status(getattr(request.state, "user_id", None))


# ---------------------------------------------------------------- user auth
# Email + password accounts for the website (app/auth_users.py). Signup and
# login are exempt from the guard above — nobody has a credential yet.
# /api/auth/me and /api/profile need a user session: the local token alone
# gets a 401 there. No email verification / password reset yet: the backend
# has no mailer, so that is a later step, not a silent gap.


class AuthSignupInput(BaseModel):
    email: str
    password: str
    display_name: Optional[str] = Field(default=None, alias="displayName")
    model_config = {"populate_by_name": True}


class AuthLoginInput(BaseModel):
    email: str
    password: str


class ProfileUpdateInput(BaseModel):
    display_name: Optional[str] = Field(default=None, alias="displayName")
    model_config = {"populate_by_name": True}


def _require_user(request: Request = None) -> str:
    """Return the session user id, or 401 for local-token/no-token callers."""
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(401, "a user session is required")
    return user_id


def _caller_user_id(request: Request = None) -> Optional[str]:
    """The authenticated user id for this request, or None for the
    local-token operator. Identity comes from the session only — never
    from a request body."""
    if request is None:
        return None
    return getattr(request.state, "user_id", None)


def _can_access(request: Request, owner_id: Optional[str]) -> bool:
    """May the caller access a row owned by owner_id? The operator
    (user_id None) may access everything. A session user may access
    only rows they own; legacy NULL-owned rows are operator-owned."""
    uid = _caller_user_id(request)
    return uid is None or (owner_id is not None and owner_id == uid)


def _require_owner(request: Request, owner_id: Optional[str]) -> None:
    """404 (not 403 — do not confirm existence) when the caller may
    not access a row owned by owner_id."""
    if not _can_access(request, owner_id):
        raise HTTPException(404, "not found")


@app.post("/api/auth/signup", status_code=201)
def auth_signup(body: AuthSignupInput) -> dict[str, Any]:
    from .auth_users import AuthError, signup

    try:
        user, session_token = signup(body.email, body.password, body.display_name)
    except AuthError as exc:
        raise HTTPException(exc.status, exc.detail) from None
    return {"user": user, "session_token": session_token}


@app.post("/api/auth/login")
def auth_login(body: AuthLoginInput) -> dict[str, Any]:
    from .auth_users import AuthError, login

    try:
        user, session_token = login(body.email, body.password)
    except AuthError as exc:
        raise HTTPException(exc.status, exc.detail) from None
    return {"user": user, "session_token": session_token}


@app.post("/api/auth/logout")
def auth_logout(request: Request = None) -> dict[str, bool]:
    from .auth_users import revoke_session

    _require_user(request)
    auth_header = request.headers.get("authorization", "")
    token = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else None
    revoke_session(token)
    return {"ok": True}


@app.get("/api/auth/me")
def auth_me(request: Request = None) -> dict[str, Any]:
    from .auth_users import get_profile

    user_id = _require_user(request)
    profile = get_profile(user_id)
    if profile is None:
        raise HTTPException(401, "a user session is required")
    return profile


@app.get("/api/profile")
def get_user_profile(request: Request = None) -> dict[str, Any]:
    from .auth_users import get_profile
    from .connectors.oauth import oauth_manager

    user_id = _require_user(request)
    profile = get_profile(user_id)
    if profile is None:
        raise HTTPException(401, "a user session is required")
    # The user's own connections — never the operator's legacy globals.
    profile["connected_providers"] = oauth_manager.status(user_id)
    return profile


@app.patch("/api/profile")
def update_user_profile(request: Request, body: ProfileUpdateInput) -> dict[str, Any]:
    from .auth_users import AuthError, update_profile
    from .connectors.oauth import oauth_manager

    user_id = _require_user(request)
    try:
        profile = update_profile(user_id, display_name=body.display_name)
    except AuthError as exc:
        raise HTTPException(exc.status, exc.detail) from None
    if profile is None:
        raise HTTPException(401, "a user session is required")
    profile["connected_providers"] = oauth_manager.status(user_id)
    return profile


@app.post("/api/connectors/{provider}/start")
async def start_connector(provider: str, request: Request = None) -> dict[str, str]:
    from .connectors.oauth import oauth_manager
    # A user session starts the per-user DB-backed flow; the local-token
    # operator keeps exactly today's behaviour (legacy in-memory state).
    # This is a deliberate deviation from the per-user-only spec: the
    # operator path is the machine owner's own trust domain.
    try:
        return {"authorizationUrl": oauth_manager.start(provider, user_id=getattr(request.state, "user_id", None))}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


class ConnectorConfigureInput(BaseModel):
    # Client credentials ONLY. The old access-token/refresh-token paste
    # path is gone: manual config stays only where genuinely required,
    # and all four OAuth providers support the real authorization flow.
    client_id: Optional[str] = Field(default=None, alias="clientId")
    client_secret: Optional[str] = Field(default=None, alias="clientSecret")
    model_config = {"populate_by_name": True}


def _web_origin() -> str:
    """Workspace origin for OAuth callback redirects.

    ``PRIVAGENT_WEB_ORIGIN`` (default http://localhost:5173, the Vite dev
    server) — pinned to localhost so a misconfigured env cannot bounce
    users to an arbitrary host after authorization. Only scheme + host
    are kept; any path in the env value is dropped.
    """
    origin = os.environ.get("PRIVAGENT_WEB_ORIGIN", "http://localhost:5173").rstrip("/")
    parsed = urlparse(origin)
    if parsed.scheme not in ("http", "https") or parsed.hostname not in ("localhost", "127.0.0.1"):
        return "http://localhost:5173"
    return f"{parsed.scheme}://{parsed.netloc}"


def _attach_email_mission(provider: str, user_id: Optional[str]) -> None:
    """After a Google/Microsoft connection, attach the primary email mission.

    Moved here from the old token-paste configure path: it now runs when
    the real OAuth flow completes. Best-effort — a failure must never
    break the callback redirect.

    Ownership: the mission is scoped to the connecting user only. It
    never rebinds another user's mission (the old code took
    ``missions[0]`` — the first mission in the whole DB, whoever owned
    it — and paused the rest), and it never pauses another user's
    missions. A duplicate mission for the same mailbox of the same user
    is reactivated instead of creating a competing sync loop.
    """
    try:
        from .connectors.oauth import oauth_manager
        conn = oauth_manager.connector("gmail" if provider == "google" else "outlook", user_id=user_id)
        if not getattr(conn, "configured", False):
            return
        prof = conn.get_profile()
        email_addr = prof.get("emailAddress") or prof.get("mail")
        if not email_addr:
            return
        logger.info("Live OAuth connected for %s: %s", provider, email_addr)
        from .email_intelligence.sync_engine import run_email_mission_tick
        # The connection id of the account just connected, so the
        # mission's ticks resolve this exact connection rather than
        # whichever row is primary later.
        connection_id = oauth_manager.find_connection_id(user_id, provider, email_addr) if user_id else None
        missions = db.list_email_missions(user_id=user_id)
        same_account = [m for m in missions if (m.get("account_email") or "").lower() == email_addr.lower()]
        if same_account:
            primary = same_account[0]
            db.update_email_mission(primary["id"], {"account_email": email_addr, "sync_cursor": "", "status": "active"})
            for m in same_account[1:]:
                db.update_email_mission(m["id"], {"status": "paused"})
            asyncio.create_task(run_email_mission_tick(primary["id"]))
        else:
            new_m = db.create_email_mission(
                account_email=email_addr,
                provider="gmail" if provider == "google" else "outlook",
                user_id=user_id,
                connection_id=connection_id,
            )
            asyncio.create_task(run_email_mission_tick(new_m["id"]))
    except Exception as exc:
        logger.warning("Could not sync email profile for %s: %s", provider, exc)


# The four OAuth providers. Used for the provider-exists check so a
# configure/disconnect call does not trigger a live verification of
# all four providers just to check a static name.
_OAUTH_PROVIDERS = {"google", "microsoft", "slack", "github"}


class ConnectorMigrateInput(BaseModel):
    # Claiming the operator's legacy tokens requires the user to name
    # the account and confirm explicitly — never a silent transfer.
    account_email: Optional[str] = Field(default=None, alias="accountEmail")
    confirm: bool = False
    model_config = {"populate_by_name": True}


@app.post("/api/connectors/{provider}/configure")
async def configure_connector(provider: str, body: ConnectorConfigureInput, request: Request = None) -> dict[str, Any]:
    from .connectors.oauth import oauth_manager
    if provider not in _OAUTH_PROVIDERS:
        raise HTTPException(404, "unknown connector")
    auth_url = None
    if body.client_id and body.client_secret:
        oauth_manager.set_credentials(provider, body.client_id, body.client_secret)
        try:
            auth_url = oauth_manager.start(provider, user_id=getattr(request.state, "user_id", None))
        except Exception:
            pass
    else:
        raise HTTPException(400, "Must provide clientId and clientSecret")
    return {
        "ok": True,
        "authorizationUrl": auth_url,
        "status": [item for item in oauth_manager.status(getattr(request.state, "user_id", None)) if item["provider"] == provider][0],
    }


@app.get("/api/connectors/{provider}/callback")
async def connector_callback(provider: str, code: str = "", state: str = "", error: str = ""):
    from .connectors.oauth import oauth_manager
    origin = _web_origin()
    # The provider reports a denial (user clicked Deny) as ?error=...:
    # land back in the workspace with an error the UI can show, not a
    # bare 400 page. The error value is sanitized before it is reflected
    # into the redirect — it is a short code, never free text.
    if error:
        if error == "access_denied":
            err_code = "denied"
        elif re.fullmatch(r"[a-z0-9_-]{1,64}", error):
            err_code = error
        else:
            err_code = "failed"
        return RedirectResponse(f"{origin}/agent.html?oauth_error={err_code}#connectors", status_code=303)
    try:
        user_id = oauth_manager.complete(provider, code, state)
    except PermissionError:
        return RedirectResponse(f"{origin}/agent.html?oauth_error=invalid_state#connectors", status_code=303)
    except (RuntimeError, KeyError, ValueError):
        return RedirectResponse(f"{origin}/agent.html?oauth_error=exchange_failed#connectors", status_code=303)
    if provider in ("google", "microsoft"):
        _attach_email_mission(provider, user_id)
    return RedirectResponse(f"{origin}/agent.html#connectors", status_code=303)


@app.delete("/api/connectors/{provider}")
async def disconnect_connector(provider: str, request: Request, connection_id: str = "") -> dict[str, Any]:
    from .connectors.oauth import oauth_manager
    if provider not in _OAUTH_PROVIDERS:
        raise HTTPException(404, "unknown connector")
    # Revokes at the provider, then deletes locally; the result says
    # whether the provider revoke succeeded. With no connection_id the
    # primary connection is disconnected.
    try:
        return oauth_manager.disconnect(
            provider, user_id=getattr(request.state, "user_id", None),
            connection_id=connection_id or None,
        )
    except PermissionError as exc:
        raise HTTPException(404, "connection not found") from exc


@app.post("/api/connectors/{provider}/connections/{connection_id}/primary")
async def set_primary_connection(provider: str, connection_id: str, request: Request = None) -> dict[str, Any]:
    from .connectors.oauth import oauth_manager
    if provider not in _OAUTH_PROVIDERS:
        raise HTTPException(404, "unknown connector")
    user_id = _require_user(request)
    try:
        oauth_manager.set_primary(user_id, provider, connection_id)
    except PermissionError as exc:
        raise HTTPException(404, "connection not found") from exc
    return {"ok": True}


@app.post("/api/connectors/{provider}/migrate")
async def migrate_legacy_connection(provider: str, body: ConnectorMigrateInput, request: Request = None) -> dict[str, Any]:
    from .connectors.oauth import oauth_manager
    if provider not in _OAUTH_PROVIDERS:
        raise HTTPException(404, "unknown connector")
    user_id = _require_user(request)
    if not body.confirm or not body.account_email:
        raise HTTPException(400, "migration requires accountEmail and confirm=true")
    try:
        return oauth_manager.migrate_legacy(provider, user_id, body.account_email)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc


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
async def get_chats(request: Request = None) -> list[dict[str, Any]]:
    return db.list_chats(user_id=_caller_user_id(request))


@app.post("/api/chat")
async def create_chat_session(body: Optional[ChatCreateInput] = None, request: Request = None) -> dict[str, Any]:
    title = body.title if body else None
    chat = db.create_chat(title=title, user_id=_caller_user_id(request))
    return {
        "chatId": chat["id"],
        "chat_id": chat["id"],
        "title": chat["title"],
        "status": chat["status"],
    }


@app.get("/api/chat/{chat_id}")
async def get_chat_session(chat_id: str, request: Request = None) -> dict[str, Any]:
    chat = db.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="chat not found")
    _require_owner(request, chat.get("user_id"))
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
async def post_chat_message(chat_id: str, body: ChatMessageInput, request: Request = None) -> dict[str, Any]:
    chat = db.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="chat not found")
    _require_owner(request, chat.get("user_id"))
    if body.file_token and not file_uploads.get(body.file_token):
        raise HTTPException(status_code=422, detail="attachment expired")
    # Task 3: chat messages inherit the global allow-list (no per-task
    # list is available on this route); None when unconfigured.
    task = hub.new_task(
        body.text,
        chat_id=chat_id,
        allowed_domains=list(config.ALLOWED_DOMAINS) if config.ALLOWED_DOMAINS else None,
        user_id=_caller_user_id(request),
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
async def delete_chat_session(chat_id: str, request: Request = None) -> dict[str, bool]:
    chat = db.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="chat not found")
    _require_owner(request, chat.get("user_id"))
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
async def create_task(body: TaskInput, request: Request = None) -> TaskCreated:
    chat_id = body.chat_id
    if not chat_id:
        chat = db.create_chat(title=body.instruction[:48].strip() or "New Mission", user_id=_caller_user_id(request))
        chat_id = chat["id"]
    else:
        # A task may only be attached to a chat the caller owns.
        chat = db.get_chat(chat_id)
        if chat is not None:
            _require_owner(request, chat.get("user_id"))
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
    task = hub.new_task(body.instruction, chat_id=chat_id, allowed_domains=effective, long_horizon=body.long_horizon, background=body.background, user_id=_caller_user_id(request))
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
async def get_task(task_id: str, request: Request = None) -> dict[str, Any]:
    task = _require(task_id, user_id=_caller_user_id(request))
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
async def cancel_task(task_id: str, request: Request = None) -> dict[str, str]:
    task = _require(task_id, user_id=_caller_user_id(request))
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
        save_cancel_checkpoint(task_id, task.instruction, task.steps_done, last_url,
                               user_id=task.user_id, conversation_id=task.chat_id)
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
    # The extension's takeover flag is global: if the loop already
    # exited (or never entered its wait), no TAKEOVER_END would ever
    # arrive and every later task would fail its first observation
    # with TAKEOVER_ACTIVE. A cancel means the operator takes the
    # browser back, so clearing the flag here is correct — but only
    # when no OTHER task is waiting in takeover: clearing unconditionally
    # would let this task's cancel kill another task's genuine manual
    # step (password / payment in progress). Best effort — no socket,
    # no flag to clear.
    other_awaiting = any(
        getattr(t, "task_id", None) != task_id
        and getattr(t, "state", None) == "awaiting_takeover"
        for t in hub.tasks.values()
    )
    if not other_awaiting:
        try:
            await hub.notify_extension({"type": "TAKEOVER_END", "taskId": task_id})
        except Exception:
            pass
    hub.abort_task_actions(task_id)
    if getattr(task, "session_id", None):
        from .session_manager import session_manager
        session_manager.update_status(task.session_id, "cancelled")
    await hub.broadcast_progress(task)
    return {"taskState": "cancelled"}


@app.post("/api/task/{task_id}/pause")
async def pause_task(task_id: str, request: Request = None) -> dict[str, str]:
    task = _require(task_id, user_id=_caller_user_id(request))
    if task.state not in {"running", "planning"}:
        raise HTTPException(409, f"task is {task.state}, cannot pause")
    from .agentLoop import request_pause
    request_pause(task_id)
    task.state = "paused"
    task.message = "Mission paused at the next action boundary."
    await hub.broadcast_progress(task)
    return {"taskState": "paused"}


@app.post("/api/task/{task_id}/resume")
async def resume_task(task_id: str, request: Request = None) -> dict[str, str]:
    task = _require(task_id, user_id=_caller_user_id(request))
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
async def confirm_task(task_id: str, body: ConfirmInput, request: Request = None) -> dict[str, str]:
    task = _require(task_id, user_id=_caller_user_id(request))
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
async def get_takeover(task_id: str, request: Request = None) -> dict[str, Any]:
    task = _require(task_id, user_id=_caller_user_id(request))
    return {"taskId": task.task_id, "taskState": task.state, "takeover": task.takeover}


@app.post("/api/task/{task_id}/takeover/resume")
async def resume_takeover(task_id: str, request: Request = None) -> dict[str, str]:
    task = _require(task_id, user_id=_caller_user_id(request))
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
async def cancel_takeover(task_id: str, request: Request = None) -> dict[str, str]:
    task = _require(task_id, user_id=_caller_user_id(request))
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
async def create_scheduled_job(body: ScheduledJobInput, request: Request = None) -> dict[str, Any]:
    from .scheduler import create_job

    try:
        return create_job(
            body.instruction, body.schedule,
            allowed_domains=body.allowed_domains,
            long_horizon=body.long_horizon,
            user_id=_caller_user_id(request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/scheduled")
async def list_scheduled_jobs(request: Request = None) -> dict[str, Any]:
    from .scheduler import list_jobs

    return {"jobs": list_jobs(user_id=_caller_user_id(request))}


@app.delete("/api/scheduled/{job_id}")
async def delete_scheduled_job(job_id: str, request: Request = None) -> dict[str, bool]:
    from .scheduler import delete_job, get_job

    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="scheduled job not found")
    _require_owner(request, job.get("user_id"))
    if not delete_job(job_id):
        raise HTTPException(status_code=404, detail="scheduled job not found")
    return {"deleted": True}


@app.post("/api/scheduled/{job_id}/run-now")
async def run_scheduled_job_now(job_id: str, request: Request = None) -> dict[str, Any]:
    from .scheduler import get_job, start_job

    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="scheduled job not found")
    _require_owner(request, job.get("user_id"))
    task_id, _task = start_job(job)
    record = hub.tasks.get(task_id)
    if record is not None:
        await hub.broadcast_progress(record)
    return {"taskId": task_id}


def _require(task_id: str, user_id: Optional[str] = None):
    """Resolve a task by id, enforcing ownership for session users
    (404 for another user's task — never confirm existence). The
    operator (user_id=None) may access any task. A task restored from
    a checkpoint inherits its owner from the chat (or from the
    user_id the loop checkpointed in working_memory)."""
    task = hub.tasks.get(task_id)
    if task is not None:
        if user_id is not None and task.user_id != user_id:
            raise HTTPException(404, "task not found")
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
                # Owner: the checkpointed user_id first (set by the
                # loop for tasks that carry one), else the chat owner.
                user_id=wm.get("user_id") or (chat or {}).get("user_id"),
            )
            if user_id is not None and restored.user_id != user_id:
                raise HTTPException(404, "task not found")
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
async def get_task_artifacts(task_id: str, request: Request = None) -> list[dict[str, Any]]:
    # The task may be pruned from the hub (or never have been one):
    # artifacts are the authority here. Ownership is checked against
    # the first artifact's owner; an empty list leaks nothing.
    arts = db.list_task_artifacts(task_id)
    if arts:
        _require_owner(request, _artifact_owner(arts[0]["artifact_id"]))
    return arts


@app.get("/api/chats/{chat_id}/artifacts")
async def get_chat_artifacts(chat_id: str, request: Request = None) -> list[dict[str, Any]]:
    chat = db.get_chat(chat_id)
    if chat is not None:
        _require_owner(request, chat.get("user_id"))
        return db.list_conversation_artifacts(chat_id)
    # The chat row may never have been saved; fall back to the
    # artifacts' own owner instead of 404ing the listing.
    arts = db.list_conversation_artifacts(chat_id)
    if arts:
        _require_owner(request, _artifact_owner(arts[0]["artifact_id"]))
    return arts


def _artifact_owner(artifact_id: str) -> Optional[str]:
    """Owner user_id of an artifact, via its chat (or its task's
    checkpoint when the chat is gone). None = operator-owned."""
    art = db.get_artifact(artifact_id)
    if not art:
        return None
    conv = art.get("conversation_id")
    if conv:
        chat = db.get_chat(conv)
        if chat is not None:
            return chat.get("user_id")
    task_id = art.get("task_id")
    if task_id:
        cp = db.get_task_checkpoint(task_id)
        if cp:
            wm = cp.get("working_memory") or {}
            if wm.get("user_id"):
                return wm["user_id"]
            if cp.get("conversation_id"):
                chat = db.get_chat(cp["conversation_id"])
                if chat is not None:
                    return chat.get("user_id")
    return None


@app.get("/api/artifacts/{artifact_id}")
async def get_artifact_metadata(artifact_id: str, request: Request = None) -> dict[str, Any]:
    art = db.get_artifact(artifact_id)
    if not art:
        raise HTTPException(status_code=404, detail="Artifact not found")
    _require_owner(request, _artifact_owner(artifact_id))
    return art


@app.get("/api/artifacts/{artifact_id}/download")
async def download_artifact(request: Request, artifact_id: str) -> FileResponse:
    art = db.get_artifact(artifact_id)
    if not art:
        raise HTTPException(status_code=404, detail="Artifact not found")
    _require_owner(request, _artifact_owner(artifact_id))

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
async def preview_artifact(request: Request, artifact_id: str) -> FileResponse:
    art = db.get_artifact(artifact_id)
    if not art:
        raise HTTPException(status_code=404, detail="Artifact not found")
    _require_owner(request, _artifact_owner(artifact_id))

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
