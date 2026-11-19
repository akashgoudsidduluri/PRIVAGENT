"""Per-user OAuth connections for Connected Services.

Pre-fix state: tokens are GLOBAL per provider (``OAuthManager._tokens``),
OAuth state lives only in memory, Slack/GitHub "connected" is token
presence with no live check, and disconnect never revokes at the
provider. These tests prove the per-user flow with mocked provider
endpoints: authorize → callback → token exchange → live API access,
strict isolation between users, legacy migration, real revocation, and
an unchanged local-token operator path.
"""

import time

import httpx
import pytest

from app import db as db_module
from app.auth_users import signup as signup_user
from app.connectors.oauth import OAuthManager


@pytest.fixture
def oauth_db(tmp_path, monkeypatch):
    """Isolated temporary SQLite database for OAuth tests."""
    db_file = str(tmp_path / "oauth_test.db")
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


class ProviderMock:
    """Mocked provider endpoints. Records every request for assertions."""

    def __init__(self):
        self.requests = []
        self.slack_auth_test = {"ok": True, "user": "U123", "team": "T1"}
        self.github_user_status = 200
        self.github_user_body = {"login": "octocat"}

    def __call__(self, request):
        self.requests.append(request)
        url = str(request.url)
        if "oauth2.googleapis.com/token" in url:
            return httpx.Response(200, json={"access_token": "tok-google", "refresh_token": "ref-google"})
        if "oauth2.googleapis.com/revoke" in url:
            return httpx.Response(200, json={})
        if "gmail.googleapis.com" in url and "/profile" in url:
            return httpx.Response(200, json={"emailAddress": "a@example.com"})
        if "login.microsoftonline.com" in url and "/token" in url:
            return httpx.Response(200, json={"access_token": "tok-ms", "refresh_token": "ref-ms"})
        if "graph.microsoft.com" in url:
            return httpx.Response(200, json={"mail": "a@outlook.com"})
        if "slack.com/api/oauth.v2.access" in url:
            return httpx.Response(200, json={"access_token": "xoxb-1", "ok": True})
        if "slack.com/api/auth.test" in url:
            return httpx.Response(200, json=self.slack_auth_test)
        if "slack.com/api/auth.revoke" in url:
            return httpx.Response(200, json={"ok": True})
        if "github.com/login/oauth/access_token" in url:
            return httpx.Response(200, json={"access_token": "ghp-1"})
        if "api.github.com/user" in url:
            return httpx.Response(self.github_user_status, json=self.github_user_body)
        if "api.github.com/applications" in url:
            return httpx.Response(204)
        return httpx.Response(404, json={"error": "unmocked " + url})

    def saw(self, fragment):
        return [r for r in self.requests if fragment in str(r.url)]


def _manager(mock):
    return OAuthManager(httpx.Client(transport=httpx.MockTransport(mock)))


def _user(email, db_path):
    user, _ = signup_user(email, "correct-horse-123", email.split("@")[0], db_path=db_path)
    return user


def _state_from_url(url):
    from urllib.parse import parse_qs, urlsplit

    return parse_qs(urlsplit(url).query)["state"][0]


def _connect(manager, user_id, provider="google"):
    url = manager.start(provider, user_id=user_id)
    manager.complete(provider, "auth-code", _state_from_url(url))
    return url


def test_per_user_complete_stores_encrypted_tokens(oauth_db):
    mock = ProviderMock()
    manager = _manager(mock)
    user = _user("a@example.com", oauth_db)
    url = manager.start("google", user_id=user["id"])
    from urllib.parse import parse_qs, urlsplit

    query = parse_qs(urlsplit(url).query)
    assert query["code_challenge_method"] == ["S256"]
    manager.complete("google", "auth-code", query["state"][0])
    with db_module.get_connection(oauth_db) as conn:
        row = conn.execute(
            "SELECT * FROM oauth_connections WHERE user_id = ? AND provider = ?",
            (user["id"], "google"),
        ).fetchone()
    assert row is not None
    # Stored encrypted, never plaintext.
    assert row["access_token_enc"] != "tok-google"
    assert "tok-google" not in row["access_token_enc"]
    from app.secrets import decrypt_text

    assert decrypt_text(row["access_token_enc"]) == "tok-google"
    assert decrypt_text(row["refresh_token_enc"]) == "ref-google"
    status = {item["provider"]: item for item in manager.status(user["id"])}
    assert status["google"]["connected"] is True
    assert status["google"]["account"] == "a@example.com"
    assert manager.connector("gmail", user_id=user["id"]).configured


def test_state_rejected_wrong_expired_reused(oauth_db):
    mock = ProviderMock()
    manager = _manager(mock)
    user = _user("a@example.com", oauth_db)
    url = manager.start("google", user_id=user["id"])
    state = _state_from_url(url)
    with pytest.raises(PermissionError):
        manager.complete("google", "auth-code", "bogus-state")
    # Expired state row.
    with db_module.get_connection(oauth_db) as conn:
        conn.execute(
            "INSERT INTO oauth_states (state, user_id, provider, verifier, expires_at)"
            " VALUES (?, ?, ?, ?, ?)",
            ("expired-state", user["id"], "google", "v", int(time.time()) - 10),
        )
        conn.commit()
    with pytest.raises(PermissionError):
        manager.complete("google", "auth-code", "expired-state")
    # Reuse after a successful complete.
    manager.complete("google", "auth-code", state)
    with pytest.raises(PermissionError):
        manager.complete("google", "auth-code", state)


def test_user_isolation(oauth_db):
    mock = ProviderMock()
    manager = _manager(mock)
    user_a = _user("a@example.com", oauth_db)
    user_b = _user("b@example.com", oauth_db)
    _connect(manager, user_a["id"], "google")
    status_b = {item["provider"]: item for item in manager.status(user_b["id"])}
    assert status_b["google"]["connected"] is False
    assert status_b["google"]["account"] is None
    assert not manager.connector("gmail", user_id=user_b["id"]).configured
    assert manager.connector("gmail", user_id=user_a["id"]).configured
    # B's token exchange cannot touch A's row.
    with db_module.get_connection(oauth_db) as conn:
        rows = conn.execute(
            "SELECT user_id FROM oauth_connections WHERE provider = 'google'"
        ).fetchall()
    assert [r["user_id"] for r in rows] == [user_a["id"]]


def test_slack_live_verification(oauth_db):
    mock = ProviderMock()
    manager = _manager(mock)
    user = _user("a@example.com", oauth_db)
    _connect(manager, user["id"], "slack")
    status = {item["provider"]: item for item in manager.status(user["id"])}
    assert mock.saw("auth.test"), "Slack verify must call auth.test, not just check presence"
    assert status["slack"]["connected"] is True
    assert status["slack"]["account"] == "U123"
    # A dead token reports not-connected even though a token exists.
    mock.slack_auth_test = {"ok": False, "error": "invalid_auth"}
    status = {item["provider"]: item for item in manager.status(user["id"], force_check=True)}
    assert status["slack"]["connected"] is False


def test_github_live_verification(oauth_db):
    mock = ProviderMock()
    manager = _manager(mock)
    user = _user("a@example.com", oauth_db)
    _connect(manager, user["id"], "github")
    status = {item["provider"]: item for item in manager.status(user["id"])}
    assert mock.saw("api.github.com/user"), "GitHub verify must call /user, not just check presence"
    assert status["github"]["connected"] is True
    assert status["github"]["account"] == "octocat"
    mock.github_user_status = 401
    mock.github_user_body = {"message": "Bad credentials"}
    status = {item["provider"]: item for item in manager.status(user["id"], force_check=True)}
    assert status["github"]["connected"] is False


def test_disconnect_revokes_at_provider(oauth_db):
    mock = ProviderMock()
    manager = _manager(mock)
    user = _user("a@example.com", oauth_db)
    _connect(manager, user["id"], "google")
    result = manager.disconnect("google", user_id=user["id"])
    assert mock.saw("oauth2.googleapis.com/revoke"), "Google disconnect must revoke at the provider"
    assert result.get("revoked") is True
    status = {item["provider"]: item for item in manager.status(user["id"])}
    assert status["google"]["connected"] is False
    with db_module.get_connection(oauth_db) as conn:
        row = conn.execute(
            "SELECT * FROM oauth_connections WHERE user_id = ? AND provider = ?",
            (user["id"], "google"),
        ).fetchone()
    assert row is None

    _connect(manager, user["id"], "github")
    manager.disconnect("github", user_id=user["id"])
    assert mock.saw("api.github.com/applications"), "GitHub disconnect must delete the token at the provider"

    _connect(manager, user["id"], "slack")
    manager.disconnect("slack", user_id=user["id"])
    assert mock.saw("slack.com/api/auth.revoke"), "Slack disconnect must revoke at the provider"


def test_callback_routes(oauth_db, disable_auto_auth):
    from fastapi.testclient import TestClient

    from app.connectors.oauth import oauth_manager
    from app.main import app

    mock = ProviderMock()
    old_client = oauth_manager._client
    oauth_manager._client = httpx.Client(transport=httpx.MockTransport(mock))
    try:
        with TestClient(app) as client:
            user, token = signup_user(
                "a@example.com", "correct-horse-123", "A", db_path=oauth_db
            )
            headers = {"Authorization": f"Bearer {token}"}
            start = client.post("/api/connectors/google/start", headers=headers)
            assert start.status_code == 200, start.text
            state = _state_from_url(start.json()["authorizationUrl"])
            cb = client.get(
                f"/api/connectors/google/callback?code=auth-code&state={state}",
                follow_redirects=False,
            )
            assert cb.status_code == 303, cb.text
            assert cb.headers["location"] == "http://localhost:5173/agent.html#connectors"
            status = client.get("/api/connectors", headers=headers)
            assert status.status_code == 200
            google = [i for i in status.json() if i["provider"] == "google"][0]
            assert google["connected"] is True
            assert google["account"] == "a@example.com"
            # Denial lands back in the workspace with an error, not a bare 400.
            denied = client.get(
                "/api/connectors/google/callback?error=access_denied&state=x",
                follow_redirects=False,
            )
            assert denied.status_code == 303
            assert "oauth_error=denied" in denied.headers["location"]
            # Bad state likewise redirects with an error.
            bad = client.get(
                "/api/connectors/google/callback?code=c&state=bogus",
                follow_redirects=False,
            )
            assert bad.status_code == 303
            assert "oauth_error=" in bad.headers["location"]
    finally:
        oauth_manager._client = old_client


def test_local_token_operator_path_unchanged(oauth_db, authed_client):
    from app.connectors.oauth import oauth_manager

    mock = ProviderMock()
    old_client = oauth_manager._client
    oauth_manager._client = httpx.Client(transport=httpx.MockTransport(mock))
    try:
        # Legacy global token, no user session: exactly today's behaviour.
        # (Set via the manager's dict directly — a test-only shortcut — so
        # clear the verification cache the way set_tokens() would, or a
        # cached "not connected" from an earlier test in this process
        # would be served instead of the token just placed.)
        oauth_manager._tokens["google"] = "legacy-tok"
        oauth_manager._invalidate_cache(None, "google")
        resp = authed_client.get("/api/connectors")
        assert resp.status_code == 200
        google = [i for i in resp.json() if i["provider"] == "google"][0]
        assert google["connected"] is True
        assert google["account"] == "a@example.com"
        # Legacy in-memory state flow still works for the operator.
        manager = OAuthManager(httpx.Client(transport=httpx.MockTransport(mock)))
        url = manager.start("google")
        manager.complete("google", "auth-code", _state_from_url(url))
        assert manager.connector("gmail").configured
    finally:
        oauth_manager._client = old_client
        oauth_manager._tokens.pop("google", None)
        oauth_manager._refresh_tokens.pop("google", None)
        oauth_manager._invalidate_cache(None, "google")


def test_configure_rejects_token_paste(oauth_db, disable_auto_auth):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        user, token = signup_user(
            "a@example.com", "correct-horse-123", "A", db_path=oauth_db
        )
        headers = {"Authorization": f"Bearer {token}"}
        resp = client.post(
            "/api/connectors/google/configure",
            json={"accessToken": "ya29.pasted", "refreshToken": "1//pasted"},
            headers=headers,
        )
        assert resp.status_code == 400, resp.text
