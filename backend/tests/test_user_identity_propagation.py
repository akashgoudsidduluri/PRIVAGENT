"""Two-user identity propagation.

Proves, on the actual execution paths (not just the route guards),
that a session user's request resolves to THAT user's OAuth
connection, never the legacy operator globals and never another
user's rows: the task loop's connector runner, the email-mission
tick, the capabilities gate, the MCP gate, and the read/control
routes (cross-user access is 404).

The provider is a mock transport, so the token each path actually
sent is observable in the request log: tok-google-1 is A's Gmail,
tok-google-2 is B's, legacy-tok is the operator-global Gmail token.
"""
from __future__ import annotations

import asyncio
import contextlib
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db as db_module
from app.auth_users import signup as signup_user
from app.connector_router import route_instruction
from app.connector_runner import run_connector_route
from app.connectors.oauth import OAuthManager  # noqa: F401  (manager resolved fresh via _current_manager)
from app.main import app
from test_oauth_multiaccount import ProviderMock, _connect, _manager


@pytest.fixture
def identity_db(tmp_path, monkeypatch):
    """Isolated temporary SQLite database for identity tests."""
    db_file = str(tmp_path / "identity_test.db")
    monkeypatch.setenv("PRIVAGENT_DB_PATH", db_file)
    monkeypatch.setenv("PRIVAGENT_GOOGLE_OAUTH_CLIENT_ID", "google-client")
    monkeypatch.setenv("PRIVAGENT_GOOGLE_OAUTH_CLIENT_SECRET", "google-secret")
    monkeypatch.setenv("PRIVAGENT_SLACK_OAUTH_CLIENT_ID", "slack-client")
    monkeypatch.setenv("PRIVAGENT_SLACK_OAUTH_CLIENT_SECRET", "slack-secret")
    monkeypatch.setenv("PRIVAGENT_GITHUB_OAUTH_CLIENT_ID", "gh-client")
    monkeypatch.setenv("PRIVAGENT_GITHUB_OAUTH_CLIENT_SECRET", "gh-secret")
    monkeypatch.setenv("PRIVAGENT_MICROSOFT_OAUTH_CLIENT_ID", "ms-client")
    monkeypatch.setenv("PRIVAGENT_MICROSOFT_OAUTH_CLIENT_SECRET", "ms-secret")
    db_module.init_db(db_file)
    return db_file


class IdentityMock(ProviderMock):
    """ProviderMock plus the Gmail message list/get and draft endpoints
    the runner and the mission tick actually call."""

    _TOKEN_EMAIL = {
        "tok-google": "a@example.com",
        "tok-google-1": "a@example.com",
        "tok-google-2": "b@example.com",
        "tok-google-refreshed": "a@example.com",
        "legacy-tok": "a@example.com",
        "legacy-refreshed": "a@example.com",
    }

    def __init__(self):
        super().__init__()
        # Expired-credential simulation: profile calls with one of
        # these Bearer tokens get 401; refresh_token grants fail when
        # fail_refresh is set. Both are off by default.
        self.expired_tokens = set()
        self.fail_refresh = False

    def __call__(self, request):
        from urllib.parse import urlsplit

        # Record every request (the parent only records the ones it
        # handles itself; the paths below return before delegating).
        self.requests.append(request)
        url = str(request.url)
        path = urlsplit(url).path
        if self.fail_refresh and "oauth2.googleapis.com/token" in url \
                and b"refresh_token" in request.content:
            return httpx.Response(400, json={"error": "invalid_grant"})
        if path.rstrip("/").endswith("/profile"):
            token = request.headers.get("authorization", "").replace("Bearer ", "")
            if token in self.expired_tokens:
                return httpx.Response(401, json={"error": "invalid_token"})
        if request.method == "POST" and path.rstrip("/").endswith("/drafts"):
            return httpx.Response(200, json={"id": "draft-1"})
        if "/gmail/v1/users/me/messages" in url:
            token = request.headers.get("authorization", "").replace("Bearer ", "")
            if token in self.expired_tokens:
                return httpx.Response(401, json={"error": "invalid_token"})
            email = self._TOKEN_EMAIL.get(token, "a@example.com")
            if path.rstrip("/").endswith("/messages"):
                return httpx.Response(200, json={"messages": [{"id": "m1", "threadId": "t1"}]})
            return httpx.Response(200, json={
                "id": "m1",
                "payload": {"headers": [
                    {"name": "From", "value": f"Priya <priya@{email.split('@')[1]}>"},
                    {"name": "Subject", "value": f"Budget for {email}"},
                ]},
                "snippet": "Here is the budget draft.",
            })
        if "www.googleapis.com/calendar/v3" in url:
            token = request.headers.get("authorization", "").replace("Bearer ", "")
            if token in self.expired_tokens:
                return httpx.Response(401, json={"error": "invalid_token"})
            if request.method == "POST":
                return httpx.Response(200, json={"id": "ev-1", "htmlLink": "https://calendar.google.com/event?eid=ev-1"})
            return httpx.Response(200, json={"items": [
                {"summary": "Standup", "start": {"dateTime": "2026-11-20T10:00:00+05:30"}},
            ]})
        return super().__call__(request)

    def gmail_tokens_seen(self):
        return [r.headers.get("authorization", "")
                for r in self.requests if "gmail.googleapis.com" in str(r.url)]

    def calendar_tokens_seen(self):
        return [r.headers.get("authorization", "")
                for r in self.requests if "www.googleapis.com/calendar/v3" in str(r.url)]


def _two_users(db_path):
    a, token_a = signup_user("a@example.com", "correct-horse-123", "A", db_path=db_path)
    b, token_b = signup_user("b@example.com", "correct-horse-123", "B", db_path=db_path)
    return (a, token_a), (b, token_b)


def _connect_both(mock, a_id, b_id):
    """Connect Gmail for A then B. Exchange order fixes the tokens:
    A gets tok-google-1 (a@example.com), B gets tok-google-2 (b@example.com)."""
    manager = _manager(mock)
    _connect(manager, a_id, "google")
    _connect(manager, b_id, "google")
    return manager


def _current_manager():
    """The process-wide manager as the production code sees it right
    now. Resolved fresh on every call: test_ambient_proxy_env reloads
    the oauth module (importlib.reload), which rebinds the module
    attribute to a NEW manager instance — a top-level import in this
    file would keep patching the stale one while the routes use the
    fresh one (that is exactly the full-suite failure)."""
    from app.connectors.oauth import oauth_manager as current

    return current


@contextlib.contextmanager
def _patched_global_client(mock):
    """Route the process-wide OAuthManager's HTTP client at the mock,
    so the real HTTP routes exercise the real manager."""
    manager = _current_manager()
    old = manager._client
    manager._client = httpx.Client(transport=httpx.MockTransport(mock))
    try:
        yield
    finally:
        manager._client = old


def _run_route(manager, user_id):
    route = route_instruction("check my gmail")
    assert route is not None
    return asyncio.run(run_connector_route(
        route, task_id="t1", instruction="check my gmail",
        oauth_manager=manager, user_id=user_id))


# -- the runner resolves each user's own token ------------------------


def test_runner_uses_each_users_own_gmail(identity_db):
    mock = IdentityMock()
    (a, _), (b, _) = _two_users(identity_db)
    manager = _connect_both(mock, a["id"], b["id"])

    seen_before = len(mock.gmail_tokens_seen())
    out_a = _run_route(manager, a["id"])
    assert out_a.ok is True
    assert "Budget" in out_a.message
    assert mock.gmail_tokens_seen()[seen_before:] == ["Bearer tok-google-1"] * len(
        mock.gmail_tokens_seen()[seen_before:])

    seen_before = len(mock.gmail_tokens_seen())
    out_b = _run_route(manager, b["id"])
    assert out_b.ok is True
    assert "Budget" in out_b.message
    assert mock.gmail_tokens_seen()[seen_before:] == ["Bearer tok-google-2"] * len(
        mock.gmail_tokens_seen()[seen_before:])


def test_runner_fails_closed_without_a_connection(identity_db):
    """A session user with no connection gets an honest not-connected
    error and the legacy global token is never sent. The operator
    (user_id None) still resolves the legacy path."""
    mock = IdentityMock()
    (a, _), (b, _) = _two_users(identity_db)
    manager = _connect_both(mock, a["id"], b["id"])
    manager.set_tokens("google", access_token="legacy-tok", refresh_token="legacy-ref")

    c, _ = signup_user("c@example.com", "correct-horse-123", "C", db_path=identity_db)
    out_c = _run_route(manager, c["id"])
    assert out_c.ok is False
    assert "not connected" in out_c.message.lower()
    assert "Bearer legacy-tok" not in mock.gmail_tokens_seen()

    out_op = _run_route(manager, None)
    assert out_op.ok is True
    assert "Budget" in out_op.message
    assert "Bearer legacy-tok" in mock.gmail_tokens_seen()


def _run_calendar_route(manager, user_id, instruction):
    route = route_instruction(instruction)
    assert route is not None and route.connector == "google_calendar"
    return asyncio.run(run_connector_route(
        route, task_id="t-cal", instruction=instruction,
        oauth_manager=manager, user_id=user_id))


async def _always_approve(message, action):
    return True


def test_runner_calendar_read_uses_each_users_own_token(identity_db, monkeypatch):
    """Fix 1 (read): the connector-routed calendar read must send the
    calling session user's token — never another user's, never the
    legacy globals. Before the fix the runner called
    handle_calendar_instruction without user_id, so every session
    user resolved via the legacy globals."""
    mock = IdentityMock()
    (a, _), (b, _) = _two_users(identity_db)
    manager = _connect_both(mock, a["id"], b["id"])
    # handle_calendar_instruction resolves the module-global manager,
    # not the runner's injected one: point it at the mock manager so
    # the handler's verify + list calls are observable.
    monkeypatch.setattr("app.connectors.calendar_companion.oauth_manager", manager)

    out_a = _run_calendar_route(manager, a["id"], "show my meetings")
    assert out_a.ok is True
    assert "Standup" in out_a.message
    assert mock.calendar_tokens_seen() == ["Bearer tok-google-1"]

    out_b = _run_calendar_route(manager, b["id"], "show my meetings")
    assert out_b.ok is True
    assert "Standup" in out_b.message
    assert mock.calendar_tokens_seen() == ["Bearer tok-google-1", "Bearer tok-google-2"]


def test_runner_calendar_create_uses_each_users_own_token(identity_db, monkeypatch):
    """Fix 1 (create): same identity rule for the mutating path —
    the confirmed create must book on the calling user's calendar."""
    mock = IdentityMock()
    (a, _), (b, _) = _two_users(identity_db)
    manager = _connect_both(mock, a["id"], b["id"])
    monkeypatch.setattr("app.connectors.calendar_companion.oauth_manager", manager)

    instruction = "schedule a meeting called Standup on Monday at 10am"
    route = route_instruction(instruction)
    assert route is not None and route.connector == "google_calendar" and route.operation == "create"
    out = asyncio.run(run_connector_route(
        route, task_id="t-cal-create", instruction=instruction,
        oauth_manager=manager, user_id=a["id"], confirm_fn=_always_approve))
    assert out.ok is True
    assert "Standup" in out.message
    assert mock.calendar_tokens_seen() == ["Bearer tok-google-1"]


def test_runner_calendar_fails_closed_without_a_connection(identity_db, monkeypatch):
    """Fix 1 (fail-closed): a session user with no Google connection
    gets an honest not-connected error on the calendar path and the
    legacy global token is never sent. The operator (user_id None)
    still resolves the legacy path."""
    mock = IdentityMock()
    (a, _), (b, _) = _two_users(identity_db)
    manager = _connect_both(mock, a["id"], b["id"])
    monkeypatch.setattr("app.connectors.calendar_companion.oauth_manager", manager)
    manager.set_tokens("google", access_token="legacy-tok", refresh_token="legacy-ref")

    c, _ = signup_user("c@example.com", "correct-horse-123", "C", db_path=identity_db)
    out_c = _run_calendar_route(manager, c["id"], "show my meetings")
    assert out_c.ok is False
    assert "not connected" in out_c.message.lower()
    assert "Bearer legacy-tok" not in mock.calendar_tokens_seen()

    out_op = _run_calendar_route(manager, None, "show my meetings")
    assert out_op.ok is True
    assert "Standup" in out_op.message
    assert "Bearer legacy-tok" in mock.calendar_tokens_seen()


# -- HTTP: the task loop carries the caller's identity -----------------


def _wait_task(client, headers, task_id, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/task/{task_id}", headers=headers)
        assert r.status_code == 200, r.text
        state = r.json().get("taskState")
        if state in ("completed", "failed", "cancelled"):
            return r.json()
        time.sleep(0.2)
    raise AssertionError(f"task {task_id} did not finish in {timeout}s")


def test_http_task_runs_with_the_callers_gmail(identity_db):
    mock = IdentityMock()
    (a, token_a), (b, token_b) = _two_users(identity_db)
    _connect_both(mock, a["id"], b["id"])
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}
    with _patched_global_client(mock), TestClient(app) as client:
        seen_before = len(mock.gmail_tokens_seen())
        task_a = client.post("/api/task", json={"instruction": "check my gmail"}, headers=headers_a)
        assert task_a.status_code == 200, task_a.text
        done_a = _wait_task(client, headers_a, task_a.json()["taskId"])
        assert "Budget" in done_a.get("message", "")
        assert set(mock.gmail_tokens_seen()[seen_before:]) == {"Bearer tok-google-1"}

        seen_before = len(mock.gmail_tokens_seen())
        task_b = client.post("/api/task", json={"instruction": "check my gmail"}, headers=headers_b)
        assert task_b.status_code == 200, task_b.text
        done_b = _wait_task(client, headers_b, task_b.json()["taskId"])
        assert "Budget" in done_b.get("message", "")
        assert set(mock.gmail_tokens_seen()[seen_before:]) == {"Bearer tok-google-2"}


# -- cross-user reads/control are 404 ----------------------------------


def test_cross_user_reads_and_controls_are_404(identity_db):
    mock = IdentityMock()
    (a, token_a), (b, token_b) = _two_users(identity_db)
    _connect_both(mock, a["id"], b["id"])
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}
    with _patched_global_client(mock), TestClient(app) as client:
        chat = client.post("/api/chat", json={"title": "A's chat"}, headers=headers_a)
        assert chat.status_code == 200, chat.text
        chat_id = chat.json()["chatId"]
        assert client.get(f"/api/chat/{chat_id}", headers=headers_b).status_code == 404
        assert client.delete(f"/api/chat/{chat_id}", headers=headers_b).status_code == 404
        assert client.get(f"/api/chat/{chat_id}", headers=headers_a).status_code == 200

        task = client.post("/api/task", json={"instruction": "check my gmail", "chat_id": chat_id},
                           headers=headers_a)
        assert task.status_code == 200, task.text
        task_id = task.json()["taskId"]
        assert client.get(f"/api/task/{task_id}", headers=headers_b).status_code == 404
        assert client.post(f"/api/task/{task_id}/cancel", headers=headers_b).status_code == 404
        # B cannot attach a task to A's chat either.
        assert client.post("/api/task", json={"instruction": "check my gmail", "chat_id": chat_id},
                           headers=headers_b).status_code == 404

        mission = db_module.create_email_mission(
            "a@example.com", provider="gmail", frequency_seconds=3600,
            user_id=a["id"], db_path=identity_db)
        mid = mission["id"]
        assert client.get(f"/api/email/missions/{mid}", headers=headers_b).status_code == 404
        assert client.post(f"/api/email/missions/{mid}/pause", headers=headers_b).status_code == 404
        assert client.post(f"/api/email/missions/{mid}/sync", headers=headers_b).status_code == 404
        assert client.get(f"/api/email/missions/{mid}", headers=headers_a).status_code == 200

        # A sees only their own rows; B sees only theirs.
        assert [c["id"] for c in client.get("/api/chats", headers=headers_a).json()] == [chat_id]
        assert client.get("/api/chats", headers=headers_b).json() == []
        missions_a = client.get("/api/email/missions", headers=headers_a).json()
        assert [m["id"] for m in missions_a] == [mid]
        assert client.get("/api/email/missions", headers=headers_b).json() == []


# -- missions ------------------------------------------------------------


def test_attach_does_not_rebind_another_users_mission(identity_db, monkeypatch):
    """B connecting Gmail must create B's own mission (or reuse B's),
    never rebind or pause A's mission — the pre-fix bug."""
    mock = IdentityMock()
    (a, _), (b, _) = _two_users(identity_db)
    manager = _connect_both(mock, a["id"], b["id"])
    mission_a = db_module.create_email_mission(
        "a@example.com", provider="gmail", frequency_seconds=3600,
        user_id=a["id"], db_path=identity_db)
    assert mission_a["status"] == "active"
    # A second active mission of A's: the pre-fix bug paused A's
    # other missions when B attached; both must stay active.
    mission_a2 = db_module.create_email_mission(
        "a2@example.com", provider="gmail", frequency_seconds=3600,
        user_id=a["id"], db_path=identity_db)
    assert mission_a2["status"] == "active"

    from app.main import _attach_email_mission
    monkeypatch.setattr(
        "app.email_intelligence.sync_engine.run_email_mission_tick",
        lambda mission_id, db_path=None: asyncio.sleep(0))
    # _attach is called from the OAuth callback path; it resolves the
    # connection through the process-wide manager, so that manager's
    # client must be the mock too. It spawns the tick (patched out).
    with _patched_global_client(mock):
        asyncio.run(_attach_coro(_attach_email_mission, "google", b["id"]))

    after_a = db_module.get_email_mission(mission_a["id"], db_path=identity_db)
    assert after_a["account_email"] == "a@example.com"
    assert after_a["user_id"] == a["id"]
    assert after_a["status"] == "active"
    after_a2 = db_module.get_email_mission(mission_a2["id"], db_path=identity_db)
    assert after_a2["status"] == "active"
    missions_b = db_module.list_email_missions(user_id=b["id"], db_path=identity_db)
    assert len(missions_b) == 1
    assert missions_b[0]["account_email"] == "b@example.com"
    assert missions_b[0]["user_id"] == b["id"]


async def _attach_coro(attach, provider, user_id):
    attach(provider, user_id)


def test_mission_tick_resolves_each_users_connector(identity_db):
    """The background tick lists mail through the mission owner's
    connection, not the globals and not the other user's."""
    mock = IdentityMock()
    (a, _), (b, _) = _two_users(identity_db)
    _connect_both(mock, a["id"], b["id"])
    mission_a = db_module.create_email_mission(
        "a@example.com", provider="gmail", frequency_seconds=3600,
        user_id=a["id"], connection_id=None, db_path=identity_db)
    mission_b = db_module.create_email_mission(
        "b@example.com", provider="gmail", frequency_seconds=3600,
        user_id=b["id"], connection_id=None, db_path=identity_db)

    from app.email_intelligence.sync_engine import run_email_mission_tick
    with _patched_global_client(mock):
        before = len(mock.gmail_tokens_seen())
        asyncio.run(run_email_mission_tick(mission_a["id"], db_path=identity_db))
        # A's tick sends only A's token — a per-tick snapshot, not a
        # cumulative assertion (which would pass even if the ticks
        # were swapped).
        assert set(mock.gmail_tokens_seen()[before:]) == {"Bearer tok-google-1"}
        before = len(mock.gmail_tokens_seen())
        asyncio.run(run_email_mission_tick(mission_b["id"], db_path=identity_db))
        assert set(mock.gmail_tokens_seen()[before:]) == {"Bearer tok-google-2"}
    assert "Bearer legacy-tok" not in mock.gmail_tokens_seen()


def test_disconnect_and_refresh_are_user_scoped(identity_db):
    mock = IdentityMock()
    (a, _), (b, _) = _two_users(identity_db)
    manager = _connect_both(mock, a["id"], b["id"])
    mission_b = db_module.create_email_mission(
        "b@example.com", provider="gmail", frequency_seconds=3600,
        user_id=b["id"], db_path=identity_db)

    # Disconnecting A leaves B's connection and mission untouched.
    manager.disconnect("google", user_id=a["id"])
    status = {item["provider"]: item for item in manager.status(b["id"])}
    assert status["google"]["connections"][0]["account"] == "b@example.com"
    assert db_module.get_email_mission(mission_b["id"], db_path=identity_db)["status"] == "active"
    assert all(item["provider"] != "google" or not item["connections"]
               for item in manager.status(a["id"]))

    # Refreshing A (after reconnecting) does not modify B's row — and
    # A's access token actually rotates: the next run sends the
    # refreshed token, not the old one.
    _connect(manager, a["id"], "google")
    before = manager.status(b["id"])
    manager.refresh_access_token("google", user_id=a["id"])
    assert manager.status(b["id"]) == before
    seen_before = len(mock.gmail_tokens_seen())
    out_a = _run_route(manager, a["id"])
    assert out_a.ok is True
    assert set(mock.gmail_tokens_seen()[seen_before:]) == {"Bearer tok-google-refreshed"}


# -- capabilities + MCP gates are owner-bound ----------------------------


def test_capability_gate_is_owner_bound(identity_db):
    mock = IdentityMock()
    (a, token_a), (b, token_b) = _two_users(identity_db)
    _connect_both(mock, a["id"], b["id"])
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}
    with _patched_global_client(mock), TestClient(app) as client:
        prep = client.post("/api/capabilities/prepare", json={
            "capability": "EmailCapability", "operation": "draft_email",
            "args": {"to": "x@y.com", "subject": "s", "body": "b"},
        }, headers=headers_a)
        assert prep.status_code == 200, prep.text
        gate = prep.json()["gateTaskId"]
        # B cannot spend A's gate (the attempt consumes the single-use gate).
        exec_b = client.post("/api/capabilities/execute", json={
            "capability": "EmailCapability", "operation": "draft_email",
            "args": {"to": "x@y.com", "subject": "s", "body": "b"},
            "gateTaskId": gate,
        }, headers=headers_b)
        assert exec_b.status_code == 404, exec_b.text
        # A prepares a fresh gate, confirms, then executes: the draft
        # runs against A's Gmail.
        prep2 = client.post("/api/capabilities/prepare", json={
            "capability": "EmailCapability", "operation": "draft_email",
            "args": {"to": "x@y.com", "subject": "s", "body": "b"},
        }, headers=headers_a)
        assert prep2.status_code == 200, prep2.text
        gate = prep2.json()["gateTaskId"]
        confirm = client.post(f"/api/task/{gate}/confirm", json={"decision": "approve"}, headers=headers_a)
        assert confirm.status_code == 200, confirm.text
        seen_before = len(mock.gmail_tokens_seen())
        exec_a = client.post("/api/capabilities/execute", json={
            "capability": "EmailCapability", "operation": "draft_email",
            "args": {"to": "x@y.com", "subject": "s", "body": "b"},
            "gateTaskId": gate,
        }, headers=headers_a)
        assert exec_a.status_code == 200, exec_a.text
        assert set(mock.gmail_tokens_seen()[seen_before:]) == {"Bearer tok-google-1"}


def test_mcp_gate_is_owner_bound(identity_db):
    from app.mcp.api import registry
    from app.mcp.registry import Tool

    (a, token_a), (b, token_b) = _two_users(identity_db)
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}
    calls = []
    tool = Tool(name="identity_probe_tool",
                schema={"type": "object", "properties": {"note": {"type": "string"}}},
                execute=lambda args: calls.append(args) or {"ok": True},
                mutating=True, server="local")
    registry.register(tool)
    try:
        with TestClient(app) as client:
            prep = client.post("/api/mcp/prepare", json={
                "name": "identity_probe_tool", "args": {"note": "hi"},
            }, headers=headers_a)
            assert prep.status_code == 200, prep.text
            gate = prep.json()["gateTaskId"]
            exec_b = client.post("/api/mcp/execute", json={
                "name": "identity_probe_tool", "args": {"note": "hi"},
                "gateTaskId": gate,
            }, headers=headers_b)
            assert exec_b.status_code == 404, exec_b.text
            prep2 = client.post("/api/mcp/prepare", json={
                "name": "identity_probe_tool", "args": {"note": "hi"},
            }, headers=headers_a)
            assert prep2.status_code == 200, prep2.text
            gate = prep2.json()["gateTaskId"]
            confirm = client.post(f"/api/task/{gate}/confirm", json={"decision": "approve"},
                                  headers=headers_a)
            assert confirm.status_code == 200, confirm.text
            exec_a = client.post("/api/mcp/execute", json={
                "name": "identity_probe_tool", "args": {"note": "hi"},
                "gateTaskId": gate,
            }, headers=headers_a)
            assert exec_a.status_code == 200, exec_a.text
            assert calls == [{"note": "hi"}]
    finally:
        registry.tools.pop("identity_probe_tool", None)


# -- operator compat -----------------------------------------------------


def test_operator_still_sees_everything_and_uses_legacy(identity_db):
    """The local-token operator (user_id None) lists all chats and
    missions and still resolves the legacy globals — the operator path
    is preserved by design."""
    from conftest import AUTH_HEADERS

    mock = IdentityMock()
    (a, token_a), (b, _) = _two_users(identity_db)
    manager = _connect_both(mock, a["id"], b["id"])
    manager.set_tokens("google", access_token="legacy-tok", refresh_token="legacy-ref")
    # The HTTP task loop resolves through the process-wide manager, so
    # the legacy tokens must be set there too (restored afterwards).
    # Resolve it fresh: the ambient-proxy test reloads the oauth module
    # and rebinds the singleton (see _current_manager).
    global_manager = _current_manager()
    prev_access = global_manager._tokens.get("google")
    prev_refresh = global_manager._refresh_tokens.get("google")
    global_manager.set_tokens("google", access_token="legacy-tok", refresh_token="legacy-ref")
    try:
        with _patched_global_client(mock), TestClient(app) as client:
            chat = client.post("/api/chat", json={"title": "A's chat"},
                               headers={"Authorization": f"Bearer {token_a}"})
            chat_id = chat.json()["chatId"]
            db_module.create_email_mission(
                "a@example.com", provider="gmail", frequency_seconds=3600,
                user_id=a["id"], db_path=identity_db)
            db_module.create_email_mission(
                "b@example.com", provider="gmail", frequency_seconds=3600,
                user_id=b["id"], db_path=identity_db)
    
            assert client.get(f"/api/chat/{chat_id}", headers=AUTH_HEADERS).status_code == 200
            assert len(client.get("/api/chats", headers=AUTH_HEADERS).json()) >= 1
            assert len(client.get("/api/email/missions", headers=AUTH_HEADERS).json()) == 2
    
            task = client.post("/api/task", json={"instruction": "check my gmail"},
                               headers=AUTH_HEADERS)
            assert task.status_code == 200, task.text
            done = _wait_task(client, AUTH_HEADERS, task.json()["taskId"])
            assert "Budget" in done.get("message", "")
            assert "Bearer legacy-tok" in mock.gmail_tokens_seen()
    finally:
        if prev_access is None:
            global_manager._tokens.pop("google", None)
        else:
            global_manager._tokens["google"] = prev_access
        if prev_refresh is None:
            global_manager._refresh_tokens.pop("google", None)
        else:
            global_manager._refresh_tokens["google"] = prev_refresh


# -- review follow-ups: artifacts, scheduled jobs, legacy rows, expiry --


def test_artifact_ids_are_owner_bound(identity_db):
    """B gets 404 on A's artifact (metadata + both list routes);
    A gets 200. A legacy (ownerless) artifact is operator-only."""
    from conftest import AUTH_HEADERS

    (a, token_a), (b, token_b) = _two_users(identity_db)
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}
    with TestClient(app) as client:
        chat = client.post("/api/chat", json={"title": "A's chat"}, headers=headers_a)
        assert chat.status_code == 200, chat.text
        chat_id = chat.json()["chatId"]
        art_id = "art_identity_probe_1"
        db_module.save_artifact({
            "artifact_id": art_id, "task_id": "task_identity_probe_1",
            "conversation_id": chat_id, "filename": "Summary.docx",
            "filepath": "/tmp/identity-probe/Summary.docx", "file_format": "docx",
            "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "file_size_bytes": 10, "title": "A's summary", "summary": "probe",
            "metadata": {},
        })
        assert client.get(f"/api/artifacts/{art_id}", headers=headers_b).status_code == 404
        assert client.get(f"/api/chats/{chat_id}/artifacts", headers=headers_b).status_code == 404
        assert client.get("/api/tasks/task_identity_probe_1/artifacts",
                          headers=headers_b).status_code == 404
        assert client.get(f"/api/artifacts/{art_id}", headers=headers_a).status_code == 200
        assert client.get(f"/api/chats/{chat_id}/artifacts", headers=headers_a).status_code == 200
        assert client.get("/api/tasks/task_identity_probe_1/artifacts",
                          headers=headers_a).status_code == 200

        # Legacy artifact: no chat, no checkpoint owner.
        legacy_id = "art_identity_legacy_1"
        db_module.save_artifact({
            "artifact_id": legacy_id, "task_id": "task_identity_legacy_1",
            "conversation_id": None, "filename": "Old.docx",
            "filepath": "/tmp/identity-probe/Old.docx", "file_format": "docx",
            "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "file_size_bytes": 10, "title": "legacy", "summary": "probe",
            "metadata": {},
        })
        assert client.get(f"/api/artifacts/{legacy_id}", headers=headers_b).status_code == 404
        assert client.get(f"/api/artifacts/{legacy_id}", headers=AUTH_HEADERS).status_code == 200


def test_scheduled_job_cross_user_is_404(identity_db):
    """A scheduled job is visible and controllable only by its owner;
    B's list omits it and B's delete / run-now are 404."""
    (a, token_a), (b, token_b) = _two_users(identity_db)
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}
    with TestClient(app) as client:
        created = client.post("/api/scheduled", json={
            "instruction": "check my gmail", "schedule": "every 1h",
        }, headers=headers_a)
        assert created.status_code == 201, created.text
        job_id = created.json()["id"]
        assert job_id not in [j["id"] for j in client.get("/api/scheduled", headers=headers_b).json()["jobs"]]
        assert client.delete(f"/api/scheduled/{job_id}", headers=headers_b).status_code == 404
        assert client.post(f"/api/scheduled/{job_id}/run-now", headers=headers_b).status_code == 404
        assert job_id in [j["id"] for j in client.get("/api/scheduled", headers=headers_a).json()["jobs"]]
        assert client.delete(f"/api/scheduled/{job_id}", headers=headers_a).status_code == 200


def test_legacy_rows_are_operator_only(identity_db):
    """NULL-owner (legacy) chats and missions are invisible to session
    users — 404 on direct reads, absent from lists — and readable by
    the operator."""
    from conftest import AUTH_HEADERS

    (a, _), (b, token_b) = _two_users(identity_db)
    headers_b = {"Authorization": f"Bearer {token_b}"}
    legacy_chat = db_module.create_chat(title="legacy chat", db_path=identity_db)
    legacy_mission = db_module.create_email_mission(
        "legacy@example.com", provider="gmail", frequency_seconds=3600,
        db_path=identity_db)
    assert legacy_chat["user_id"] is None
    assert legacy_mission["user_id"] is None
    with TestClient(app) as client:
        assert client.get(f"/api/chat/{legacy_chat['id']}", headers=headers_b).status_code == 404
        assert client.get(f"/api/email/missions/{legacy_mission['id']}",
                          headers=headers_b).status_code == 404
        assert legacy_chat["id"] not in [c["id"] for c in client.get("/api/chats", headers=headers_b).json()]
        assert legacy_mission["id"] not in [m["id"] for m in client.get("/api/email/missions", headers=headers_b).json()]
        assert client.get(f"/api/chat/{legacy_chat['id']}", headers=AUTH_HEADERS).status_code == 200
        assert client.get(f"/api/email/missions/{legacy_mission['id']}",
                          headers=AUTH_HEADERS).status_code == 200


def test_expired_credential_fails_closed_for_that_user_only(identity_db):
    """A's access token dies at the provider and its refresh is
    rejected: A's run fails honestly, B's token is never sent, and
    B's run still succeeds."""
    mock = IdentityMock()
    (a, _), (b, _) = _two_users(identity_db)
    manager = _connect_both(mock, a["id"], b["id"])
    mock.expired_tokens = {"tok-google-1"}
    mock.fail_refresh = True
    manager._verification_cache.clear()

    req_before = len(mock.requests)
    tok_before = len(mock.gmail_tokens_seen())
    out_a = _run_route(manager, a["id"])
    assert out_a.ok is False
    assert any(t in out_a.message.lower()
               for t in ("not connected", "401", "refresh", "failed")), out_a.message
    slice_a = mock.gmail_tokens_seen()[tok_before:]
    assert "Bearer tok-google-2" not in slice_a
    assert "Bearer legacy-tok" not in slice_a
    # The refresh path was actually exercised and rejected.
    assert any("oauth2.googleapis.com/token" in str(r.url) and b"refresh_token" in r.content
               for r in mock.requests[req_before:])

    tok_before = len(mock.gmail_tokens_seen())
    out_b = _run_route(manager, b["id"])
    assert out_b.ok is True
    assert "Budget" in out_b.message
    assert set(mock.gmail_tokens_seen()[tok_before:]) == {"Bearer tok-google-2"}


def test_setup_instruction_does_not_rebind_another_users_mission(identity_db):
    """Review must-fix 1: B connects the SAME mailbox (a@example.com)
    and issues a setup instruction. Pre-fix, the unscoped setup
    dedupe re-bound A's mission row into B's chat (A's mail then
    classified under B's control); now A keeps its mission and B
    gets a new one of their own."""
    mock = IdentityMock()
    mock.second_google_email = "a@example.com"
    (a, _), (b, _) = _two_users(identity_db)
    _connect_both(mock, a["id"], b["id"])
    mission_a = db_module.create_email_mission(
        "a@example.com", provider="gmail", frequency_seconds=3600,
        conversation_id="chat-a", user_id=a["id"], db_path=identity_db)
    assert mission_a["status"] == "active"

    from app.email_intelligence.mission_setup import handle_email_intelligence_instruction
    with _patched_global_client(mock):
        reply = asyncio.run(handle_email_intelligence_instruction(
            "Continuously scan my emails and notify me about important ones.",
            chat_id="chat-b", db_path=identity_db, user_id=b["id"]))
    assert isinstance(reply, str) and reply
    after_a = db_module.get_email_mission(mission_a["id"], db_path=identity_db)
    assert after_a["user_id"] == a["id"]
    assert after_a["conversation_id"] == "chat-a"
    assert after_a["status"] == "active"
    missions_b = db_module.list_email_missions(user_id=b["id"], db_path=identity_db)
    assert len(missions_b) == 1
    assert missions_b[0]["account_email"] == "a@example.com"
    assert missions_b[0]["conversation_id"] == "chat-b"


def test_mission_control_does_not_resume_another_users_paused_mission(identity_db):
    """Review must-fix 2: A's mission is paused. B issues 'resume
    email monitoring' with no chat (the scheduled-job shape, where
    chat_id is None) and in B's own chat. Pre-fix, the unscoped
    paused lookup resumed A's mission; now B is told there is
    nothing to resume and A's mission stays paused — while A's own
    resume still works."""
    (a, _), (b, _) = _two_users(identity_db)
    mission_a = db_module.create_email_mission(
        "a@example.com", provider="gmail", frequency_seconds=3600,
        conversation_id="chat-a", user_id=a["id"], db_path=identity_db)
    db_module.update_email_mission(mission_a["id"], {"status": "paused"}, db_path=identity_db)

    from app.email_intelligence.mission_setup import handle_email_mission_control
    reply = asyncio.run(handle_email_mission_control(
        "resume email monitoring", chat_id=None, user_id=b["id"], db_path=identity_db))
    assert "no paused" in reply.lower()
    assert db_module.get_email_mission(mission_a["id"], db_path=identity_db)["status"] == "paused"
    reply2 = asyncio.run(handle_email_mission_control(
        "resume email monitoring", chat_id="chat-b", user_id=b["id"], db_path=identity_db))
    assert "no paused" in reply2.lower()
    assert db_module.get_email_mission(mission_a["id"], db_path=identity_db)["status"] == "paused"
    reply3 = asyncio.run(handle_email_mission_control(
        "resume email monitoring", chat_id="chat-a", user_id=a["id"], db_path=identity_db))
    assert "resumed" in reply3.lower()
    assert db_module.get_email_mission(mission_a["id"], db_path=identity_db)["status"] == "active"


def test_cancel_checkpoint_keeps_the_owner(identity_db):
    """Review should-fix 5: a task cancelled before its first step
    checkpoints with its owner stamped, so the owner can still read
    it after a restart. Pre-fix the checkpoint carried no user_id
    and the ownership check 404'd the task's own owner."""
    from app.loop_checkpoints import save_cancel_checkpoint

    (a, _), (b, _) = _two_users(identity_db)
    assert save_cancel_checkpoint("task_cancel_probe", "check my gmail", 0,
                                  user_id=a["id"], conversation_id="chat-a") is True
    row = db_module.get_task_checkpoint("task_cancel_probe", db_path=identity_db)
    assert row is not None
    assert (row.get("working_memory") or {}).get("user_id") == a["id"]
    assert row.get("conversation_id") == "chat-a"
