"""Multi-account OAuth connections + confirmed legacy migration (v2).

v1 stored one account per (user, provider) and silently migrated the
legacy global tokens into the first calling user. v2 stores one row per
(user, provider, account) with a primary, and migration is explicit:
the user names the account and confirms, the server verifies ownership
against the legacy token's live profile, and the legacy globals are
KEPT so background paths keep working.
"""

import time
from urllib.parse import parse_qs

import httpx
import pytest

from app import db as db_module
from app.auth_users import signup as signup_user
from app.connectors.oauth import OAuthManager


@pytest.fixture
def oauth_db(tmp_path, monkeypatch):
    """Isolated temporary SQLite database for OAuth tests."""
    db_file = str(tmp_path / "oauth_v2_test.db")
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
    """Mocked provider endpoints. Distinct tokens per exchange so two
    accounts from one provider can be told apart; the gmail profile
    maps a token to its account email."""

    def __init__(self):
        self.requests = []
        self.exchange_count = 0
        self.fail_exchange = False
        self.fail_profile = False
        self.second_google_email = None
        self.github_refresh = None
        self.slack_refresh = None
        self.slack_auth_test = {"ok": True, "user": "U123", "team": "T1"}
        self.github_user_status = 200
        self.github_user_body = {"login": "octocat"}
        self.github_user_headers = {}

    def __call__(self, request):
        self.requests.append(request)
        url = str(request.url)
        body = parse_qs(request.content.decode(errors="ignore"))
        grant = body.get("grant_type", [None])[0]
        if "oauth2.googleapis.com/token" in url:
            if self.fail_exchange:
                return httpx.Response(500, json={"error": "boom"})
            if grant == "refresh_token":
                # Google never rotates: a new access token, no refresh token.
                return httpx.Response(200, json={"access_token": "tok-google-refreshed"})
            self.exchange_count += 1
            return httpx.Response(
                200,
                json={"access_token": f"tok-google-{self.exchange_count}",
                      "refresh_token": "ref-google"},
            )
        if "oauth2.googleapis.com/revoke" in url:
            return httpx.Response(200, json={})
        if "gmail.googleapis.com" in url and "/profile" in url:
            if self.fail_profile:
                return httpx.Response(500, json={"error": "boom"})
            token = request.headers.get("authorization", "").replace("Bearer ", "")
            email = {
                "tok-google": "a@example.com",
                "tok-google-1": "a@example.com",
                "tok-google-2": self.second_google_email or "b@example.com",
                "tok-google-refreshed": "a@example.com",
                "legacy-tok": "a@example.com",
                "legacy-refreshed": "a@example.com",
                "dead-token": "a@example.com",
            }.get(token, "a@example.com")
            return httpx.Response(200, json={"emailAddress": email})
        if "login.microsoftonline.com" in url and "/token" in url:
            if grant == "refresh_token":
                # Microsoft issues a NEW refresh token on every refresh.
                return httpx.Response(
                    200,
                    json={"access_token": "tok-ms-refreshed", "refresh_token": "ref-ms-2"},
                )
            return httpx.Response(200, json={"access_token": "tok-ms", "refresh_token": "ref-ms"})
        if "graph.microsoft.com" in url:
            return httpx.Response(200, json={"mail": "a@outlook.com"})
        if "slack.com/api/oauth.v2.access" in url:
            if grant == "refresh_token":
                # Slack rotation: single-use refresh token, new pair each time.
                return httpx.Response(
                    200, json={"access_token": "xoxb-refreshed", "refresh_token": "xoxe-1-ref2", "ok": True}
                )
            payload = {"access_token": "xoxb-1", "ok": True}
            if self.slack_refresh is not None:
                payload["refresh_token"] = self.slack_refresh
            return httpx.Response(200, json=payload)
        if "slack.com/api/auth.test" in url:
            return httpx.Response(200, json=self.slack_auth_test)
        if "slack.com/api/auth.revoke" in url:
            return httpx.Response(200, json={"ok": True})
        if "github.com/login/oauth/access_token" in url:
            if grant == "refresh_token":
                # GitHub rotation is total: the old pair dies, a new one comes back.
                return httpx.Response(
                    200, json={"access_token": "ghp-refreshed", "refresh_token": "ghp-ref-2"}
                )
            self.exchange_count += 1
            payload = {"access_token": f"ghp-{self.exchange_count}"}
            if self.github_refresh is not None:
                payload["refresh_token"] = self.github_refresh
            return httpx.Response(200, json=payload)
        if "api.github.com/user" in url:
            token = request.headers.get("authorization", "").replace("Bearer ", "")
            if token == "dead-token":
                return httpx.Response(401, json={"message": "Bad credentials"})
            return httpx.Response(
                self.github_user_status, json=self.github_user_body,
                headers=self.github_user_headers,
            )
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


def test_two_google_accounts_primary_switch(oauth_db):
    mock = ProviderMock()
    manager = _manager(mock)
    user = _user("a@example.com", oauth_db)
    _connect(manager, user["id"], "google")
    _connect(manager, user["id"], "google")
    status = {item["provider"]: item for item in manager.status(user["id"])}
    conns = status["google"]["connections"]
    assert [c["account"] for c in conns] == ["a@example.com", "b@example.com"]
    assert [c["is_primary"] for c in conns] == [True, False]
    # The agent's default connector uses the primary account.
    assert manager.connector("gmail", user_id=user["id"]).get_profile()["emailAddress"] == "a@example.com"
    second_id = conns[1]["id"]
    manager.set_primary(user["id"], "google", second_id)
    assert manager.connector("gmail", user_id=user["id"]).get_profile()["emailAddress"] == "b@example.com"
    # Disconnecting the primary leaves the other one, promoted to primary.
    manager.disconnect("google", user_id=user["id"], connection_id=second_id)
    status = {item["provider"]: item for item in manager.status(user["id"])}
    conns = status["google"]["connections"]
    assert len(conns) == 1 and conns[0]["account"] == "a@example.com"
    assert conns[0]["is_primary"] is True


def test_unauthorized_access(oauth_db, disable_auto_auth):
    from fastapi.testclient import TestClient

    from app.main import app

    mock = ProviderMock()
    from app.connectors.oauth import oauth_manager

    old_client = oauth_manager._client
    oauth_manager._client = httpx.Client(transport=httpx.MockTransport(mock))
    try:
        with TestClient(app) as client:
            user_a, token_a = signup_user("a@example.com", "correct-horse-123", "A", db_path=oauth_db)
            user_b, token_b = signup_user("b@example.com", "correct-horse-123", "B", db_path=oauth_db)
            headers_a = {"Authorization": f"Bearer {token_a}"}
            headers_b = {"Authorization": f"Bearer {token_b}"}
            start = client.post("/api/connectors/google/start", headers=headers_a)
            state = _state_from_url(start.json()["authorizationUrl"])
            cb = client.get(
                f"/api/connectors/google/callback?code=auth-code&state={state}",
                follow_redirects=False,
            )
            assert cb.status_code == 303
            status_a = client.get("/api/connectors", headers=headers_a).json()
            conn_id = [i for i in status_a if i["provider"] == "google"][0]["connections"][0]["id"]
            # B cannot disconnect, re-primary, or even see A's connection.
            assert client.delete(
                f"/api/connectors/google?connection_id={conn_id}", headers=headers_b
            ).status_code == 404
            assert client.post(
                f"/api/connectors/google/connections/{conn_id}/primary", headers=headers_b
            ).status_code == 404
            status_b = client.get("/api/connectors", headers=headers_b).json()
            google_b = [i for i in status_b if i["provider"] == "google"][0]
            assert google_b["connections"] == [] and google_b["connected"] is False
            # A's connection survived B's attempts.
            status_a = client.get("/api/connectors", headers=headers_a).json()
            assert [i for i in status_a if i["provider"] == "google"][0]["connections"]
    finally:
        oauth_manager._client = old_client


def test_migration_requires_confirmation_and_keeps_legacy(oauth_db, disable_auto_auth):
    from fastapi.testclient import TestClient

    from app.connectors.oauth import oauth_manager
    from app.main import app

    mock = ProviderMock()
    old_client = oauth_manager._client
    oauth_manager._client = httpx.Client(transport=httpx.MockTransport(mock))
    try:
        with TestClient(app) as client:
            user_a, token_a = signup_user("a@example.com", "correct-horse-123", "A", db_path=oauth_db)
            user_b, token_b = signup_user("b@example.com", "correct-horse-123", "B", db_path=oauth_db)
            headers_a = {"Authorization": f"Bearer {token_a}"}
            headers_b = {"Authorization": f"Bearer {token_b}"}
            # Legacy globals exist, but no user has them yet.
            oauth_manager._tokens["google"] = "legacy-tok"
            oauth_manager._refresh_tokens["google"] = "legacy-ref"
            oauth_manager._invalidate_cache(None, "google")
            status_a = client.get("/api/connectors", headers=headers_a).json()
            assert [i for i in status_a if i["provider"] == "google"][0]["connections"] == []
            # No confirm -> 400. Wrong account -> 403.
            assert client.post(
                "/api/connectors/google/migrate",
                json={"accountEmail": "a@example.com"}, headers=headers_a,
            ).status_code == 400
            assert client.post(
                "/api/connectors/google/migrate",
                json={"accountEmail": "wrong@example.com", "confirm": True}, headers=headers_a,
            ).status_code == 403
            # Right account + confirm -> claimed.
            ok = client.post(
                "/api/connectors/google/migrate",
                json={"accountEmail": "a@example.com", "confirm": True}, headers=headers_a,
            )
            assert ok.status_code == 200, ok.text
            assert ok.json()["account"] == "a@example.com"
            with db_module.get_connection(oauth_db) as conn:
                row = conn.execute(
                    "SELECT * FROM oauth_connections WHERE user_id = ? AND provider = ?",
                    (user_a["id"], "google"),
                ).fetchone()
            assert row is not None and row["migrated_from_legacy"] == 1
            from app.secrets import decrypt_text

            assert decrypt_text(row["access_token_enc"]) == "legacy-tok"
            # The legacy globals are KEPT: background paths keep working.
            assert oauth_manager._tokens.get("google") == "legacy-tok"
            # Another user cannot claim the same legacy connection.
            assert client.post(
                "/api/connectors/google/migrate",
                json={"accountEmail": "a@example.com", "confirm": True}, headers=headers_b,
            ).status_code == 403
    finally:
        oauth_manager._client = old_client
        oauth_manager._tokens.pop("google", None)
        oauth_manager._refresh_tokens.pop("google", None)
        oauth_manager._invalidate_cache(None, "google")


def test_migration_refresh_only_legacy(oauth_db):
    # Only a refresh token exists (the old form accepted refresh-only
    # paste). migrate_legacy itself must refresh first — this test calls
    # it directly, so the refresh-first branch inside migrate_legacy is
    # genuinely exercised (calling refresh_access_token beforehand
    # would leave that branch untested).
    mock = ProviderMock()
    manager = _manager(mock)
    user = _user("a@example.com", oauth_db)
    manager._tokens.pop("google", None)
    manager._refresh_tokens["google"] = "legacy-ref"
    # The mock's refresh returns tok-google-refreshed, whose profile is
    # a@example.com, so the direct migrate call works end to end.
    result = manager.migrate_legacy("google", user["id"], "a@example.com")
    assert result["account"] == "a@example.com"
    with db_module.get_connection(oauth_db) as conn:
        row = conn.execute(
            "SELECT * FROM oauth_connections WHERE user_id = ? AND provider = ?",
            (user["id"], "google"),
        ).fetchone()
    from app.secrets import decrypt_text

    assert decrypt_text(row["access_token_enc"]) == "tok-google-refreshed"
    # The refresh token was not destroyed: it survives in the user's row.
    assert decrypt_text(row["refresh_token_enc"]) == "legacy-ref"


def test_github_pkce(oauth_db):
    mock = ProviderMock()
    manager = _manager(mock)
    user = _user("a@example.com", oauth_db)
    url = manager.start("github", user_id=user["id"])
    from urllib.parse import parse_qs, urlsplit

    query = parse_qs(urlsplit(url).query)
    assert query["code_challenge_method"] == ["S256"]
    assert len(query["code_challenge"][0]) == 43
    manager.complete("github", "auth-code", query["state"][0])
    token_req = [r for r in mock.requests if "github.com/login/oauth/access_token" in str(r.url)]
    assert token_req and "code_verifier=" in token_req[0].content.decode()
    # Slack must NOT get a PKCE challenge (localhost redirect + bot scopes).
    slack_url = manager.start("slack", user_id=user["id"])
    slack_query = parse_qs(urlsplit(slack_url).query)
    assert "code_challenge" not in slack_query


def test_microsoft_scopes_include_user_read(oauth_db):
    mock = ProviderMock()
    manager = _manager(mock)
    user = _user("a@example.com", oauth_db)
    url = manager.start("microsoft", user_id=user["id"])
    from urllib.parse import parse_qs, urlsplit

    scopes = parse_qs(urlsplit(url).query)["scope"][0].split()
    assert "User.Read" in scopes
    assert "offline_access" in scopes and "Mail.ReadWrite" in scopes


def test_per_user_refresh(oauth_db):
    mock = ProviderMock()
    manager = _manager(mock)
    user = _user("a@example.com", oauth_db)
    _connect(manager, user["id"], "google")
    new_tok = manager.refresh_access_token("google", user_id=user["id"])
    assert new_tok == "tok-google-refreshed"
    with db_module.get_connection(oauth_db) as conn:
        row = conn.execute(
            "SELECT * FROM oauth_connections WHERE user_id = ? AND provider = ?",
            (user["id"], "google"),
        ).fetchone()
    from app.secrets import decrypt_text

    assert decrypt_text(row["access_token_enc"]) == "tok-google-refreshed"
    # Google never rotates: the old refresh token is kept.
    assert decrypt_text(row["refresh_token_enc"]) == "ref-google"
    # Microsoft issues a new refresh token on every refresh: it is saved.
    _connect(manager, user["id"], "microsoft")
    assert manager.refresh_access_token("microsoft", user_id=user["id"]) == "tok-ms-refreshed"
    with db_module.get_connection(oauth_db) as conn:
        row = conn.execute(
            "SELECT * FROM oauth_connections WHERE user_id = ? AND provider = ?",
            (user["id"], "microsoft"),
        ).fetchone()
    assert decrypt_text(row["refresh_token_enc"]) == "ref-ms-2"


def test_profile_routes_show_user_connections(oauth_db, disable_auto_auth):
    from fastapi.testclient import TestClient

    from app.connectors.oauth import oauth_manager
    from app.main import app

    mock = ProviderMock()
    old_client = oauth_manager._client
    oauth_manager._client = httpx.Client(transport=httpx.MockTransport(mock))
    try:
        with TestClient(app) as client:
            user, token = signup_user("a@example.com", "correct-horse-123", "A", db_path=oauth_db)
            headers = {"Authorization": f"Bearer {token}"}
            start = client.post("/api/connectors/google/start", headers=headers)
            state = _state_from_url(start.json()["authorizationUrl"])
            client.get(
                f"/api/connectors/google/callback?code=auth-code&state={state}",
                follow_redirects=False,
            )
            # A legacy token also exists — the profile must show the
            # USER's connection, not the operator's globals.
            oauth_manager._tokens["google"] = "legacy-tok"
            oauth_manager._invalidate_cache(None, "google")
            profile = client.get("/api/profile", headers=headers)
            assert profile.status_code == 200
            google = [i for i in profile.json()["connected_providers"] if i["provider"] == "google"][0]
            assert google["connected"] is True
            assert google["account"] == "a@example.com"
            assert len(google["connections"]) == 1
    finally:
        oauth_manager._client = old_client
        oauth_manager._tokens.pop("google", None)
        oauth_manager._invalidate_cache(None, "google")


def test_callback_error_split_and_sanitization(oauth_db, disable_auto_auth):
    from fastapi.testclient import TestClient

    from app.connectors.oauth import oauth_manager
    from app.main import app

    mock = ProviderMock()
    old_client = oauth_manager._client
    oauth_manager._client = httpx.Client(transport=httpx.MockTransport(mock))
    try:
        with TestClient(app) as client:
            user, token = signup_user("a@example.com", "correct-horse-123", "A", db_path=oauth_db)
            headers = {"Authorization": f"Bearer {token}"}
            # Exchange failure -> exchange_failed, not invalid_state.
            mock.fail_exchange = True
            start = client.post("/api/connectors/google/start", headers=headers)
            state = _state_from_url(start.json()["authorizationUrl"])
            cb = client.get(
                f"/api/connectors/google/callback?code=auth-code&state={state}",
                follow_redirects=False,
            )
            assert cb.status_code == 303
            assert "oauth_error=exchange_failed" in cb.headers["location"]
            # Bad state -> invalid_state.
            bad = client.get(
                "/api/connectors/google/callback?code=c&state=bogus",
                follow_redirects=False,
            )
            assert "oauth_error=invalid_state" in bad.headers["location"]
            # A crafted provider error value is sanitized, not reflected.
            crafted = client.get(
                "/api/connectors/google/callback?error=<script>alert(1)",
                follow_redirects=False,
            )
            assert "oauth_error=failed" in crafted.headers["location"]
            assert "<script>" not in crafted.headers["location"]
    finally:
        oauth_manager._client = old_client


def test_expired_state_pruned_on_start(oauth_db):
    mock = ProviderMock()
    manager = _manager(mock)
    user = _user("a@example.com", oauth_db)
    with db_module.get_connection(oauth_db) as conn:
        conn.execute(
            "INSERT INTO oauth_states (state, user_id, provider, verifier, expires_at)"
            " VALUES (?, ?, ?, ?, ?)",
            ("stale-state", user["id"], "google", "v", int(time.time()) - 10),
        )
        conn.commit()
    manager.start("google", user_id=user["id"])
    with db_module.get_connection(oauth_db) as conn:
        row = conn.execute(
            "SELECT * FROM oauth_states WHERE state = 'stale-state'"
        ).fetchone()
    assert row is None


def test_github_reduced_scopes_reported(oauth_db):
    mock = ProviderMock()
    manager = _manager(mock)
    user = _user("a@example.com", oauth_db)
    _connect(manager, user["id"], "github")
    mock.github_user_headers = {"X-OAuth-Scopes": "public_repo"}
    status = {item["provider"]: item for item in manager.status(user["id"], force_check=True)}
    assert status["github"]["connected"] is False
    assert "repo scope" in status["github"]["error"]


def test_slack_revoke_uses_get(oauth_db):
    mock = ProviderMock()
    manager = _manager(mock)
    user = _user("a@example.com", oauth_db)
    _connect(manager, user["id"], "slack")
    manager.disconnect("slack", user_id=user["id"])
    revoke_reqs = [r for r in mock.requests if "slack.com/api/auth.revoke" in str(r.url)]
    assert revoke_reqs and revoke_reqs[0].method == "GET"


def test_migrate_disconnect_sequence_a_globals_survive(oauth_db):
    """M1-A: migrate (globals=T1) -> operator re-auths (globals=T2) ->
    user disconnects -> the operator's valid T2 globals survive."""
    mock = ProviderMock()
    manager = _manager(mock)
    user = _user("a@example.com", oauth_db)
    manager._tokens["google"] = "legacy-tok"
    manager._refresh_tokens["google"] = "legacy-ref"
    manager.migrate_legacy("google", user["id"], "a@example.com")
    # The operator re-authenticates the legacy path with a new token.
    manager.set_tokens("google", access_token="legacy-tok-2", refresh_token="legacy-ref-2")
    manager.disconnect("google", user_id=user["id"])
    assert manager._tokens.get("google") == "legacy-tok-2"
    assert manager._refresh_tokens.get("google") == "legacy-ref-2"


def test_migrate_disconnect_sequence_b_globals_cleared(oauth_db):
    """M1-B: user OAuth-connects, then migrates the same account (the
    row's tokens become the legacy token), then disconnects -> the
    globals point at the revoked token and are cleared."""
    mock = ProviderMock()
    manager = _manager(mock)
    user = _user("a@example.com", oauth_db)
    _connect(manager, user["id"], "google")
    manager._tokens["google"] = "legacy-tok"
    manager._refresh_tokens["google"] = "legacy-ref"
    manager.migrate_legacy("google", user["id"], "a@example.com")
    with db_module.get_connection(oauth_db) as conn:
        rows = conn.execute(
            "SELECT * FROM oauth_connections WHERE user_id = ? AND provider = ?",
            (user["id"], "google"),
        ).fetchall()
    assert len(rows) == 1 and rows[0]["migrated_from_legacy"] == 1
    manager.disconnect("google", user_id=user["id"])
    assert "google" not in manager._tokens
    assert "google" not in manager._refresh_tokens


def test_migrate_dead_access_token_refreshes(oauth_db):
    """S3: a legacy access token that no longer verifies, with a valid
    refresh token, migrates after one refresh instead of failing 502."""
    mock = ProviderMock()
    manager = _manager(mock)
    user = _user("a@example.com", oauth_db)
    manager._tokens["google"] = "dead-token"
    manager._refresh_tokens["google"] = "legacy-ref"
    # Make the first profile fetch fail (the dead access token), then
    # let the real implementation run after the refresh.
    orig_fetch = manager._fetch_account
    calls = {"n": 0}

    def _fetch_once(provider, token):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return orig_fetch(provider, token)

    manager._fetch_account = _fetch_once
    result = manager.migrate_legacy("google", user["id"], "a@example.com")
    assert result["account"] == "a@example.com"
    from app.secrets import decrypt_text

    with db_module.get_connection(oauth_db) as conn:
        row = conn.execute(
            "SELECT * FROM oauth_connections WHERE user_id = ? AND provider = ?",
            (user["id"], "google"),
        ).fetchone()
    assert decrypt_text(row["access_token_enc"]) == "tok-google-refreshed"


def test_operator_delete_bogus_connection(oauth_db, authed_client):
    """S4: the operator path has no connection rows, so a named
    connection_id there is an error, not a silent ok."""
    resp = authed_client.delete("/api/connectors/google?connection_id=bogus")
    assert resp.status_code == 404, resp.text


def test_same_account_reconnect_upserts(oauth_db):
    """S6: connecting the same account twice updates the one row
    instead of creating a duplicate connection."""
    mock = ProviderMock()
    mock.second_google_email = "a@example.com"
    manager = _manager(mock)
    user = _user("a@example.com", oauth_db)
    _connect(manager, user["id"], "google")
    _connect(manager, user["id"], "google")
    status = {item["provider"]: item for item in manager.status(user["id"])}
    conns = status["google"]["connections"]
    assert len(conns) == 1 and conns[0]["account"] == "a@example.com"
    from app.secrets import decrypt_text

    with db_module.get_connection(oauth_db) as conn:
        row = conn.execute(
            "SELECT * FROM oauth_connections WHERE user_id = ? AND provider = ?",
            (user["id"], "google"),
        ).fetchone()
    assert decrypt_text(row["access_token_enc"]) == "tok-google-2"


def test_github_and_slack_refresh(oauth_db):
    """S6: GitHub total rotation and Slack single-use refresh both save
    the new pair (not just the access token)."""
    mock = ProviderMock()
    mock.github_refresh = "ghp-ref-1"
    mock.slack_refresh = "xoxe-1-ref"
    manager = _manager(mock)
    user = _user("a@example.com", oauth_db)
    _connect(manager, user["id"], "github")
    assert manager.refresh_access_token("github", user_id=user["id"]) == "ghp-refreshed"
    from app.secrets import decrypt_text

    with db_module.get_connection(oauth_db) as conn:
        row = conn.execute(
            "SELECT * FROM oauth_connections WHERE user_id = ? AND provider = ?",
            (user["id"], "github"),
        ).fetchone()
    assert decrypt_text(row["access_token_enc"]) == "ghp-refreshed"
    assert decrypt_text(row["refresh_token_enc"]) == "ghp-ref-2"
    _connect(manager, user["id"], "slack")
    assert manager.refresh_access_token("slack", user_id=user["id"]) == "xoxb-refreshed"
    with db_module.get_connection(oauth_db) as conn:
        row = conn.execute(
            "SELECT * FROM oauth_connections WHERE user_id = ? AND provider = ?",
            (user["id"], "slack"),
        ).fetchone()
    assert decrypt_text(row["access_token_enc"]) == "xoxb-refreshed"
    assert decrypt_text(row["refresh_token_enc"]) == "xoxe-1-ref2"


def test_github_401_verify_refreshes(oauth_db):
    """S6: a dead GitHub access token with a valid refresh token
    self-heals on verify instead of reporting 'token rejected'."""
    mock = ProviderMock()
    mock.github_refresh = "ghp-ref-1"
    manager = _manager(mock)
    user = _user("a@example.com", oauth_db)
    _connect(manager, user["id"], "github")
    # Kill the access token in the row, keep the refresh token.
    manager._save_user_tokens(user["id"], "github", "dead-token", refresh_token="ghp-ref-1", account_email="octocat")
    status = {item["provider"]: item for item in manager.status(user["id"], force_check=True)}
    assert status["github"]["connected"] is True
    assert status["github"]["account"] == "octocat"


def test_patch_profile_shows_user_connections(oauth_db, disable_auto_auth):
    """S6: PATCH /api/profile returns the user's own connections (M3
    covers GET too; the PATCH route had the same bug)."""
    from fastapi.testclient import TestClient

    from app.connectors.oauth import oauth_manager
    from app.main import app

    mock = ProviderMock()
    old_client = oauth_manager._client
    oauth_manager._client = httpx.Client(transport=httpx.MockTransport(mock))
    try:
        with TestClient(app) as client:
            user, token = signup_user("a@example.com", "correct-horse-123", "A", db_path=oauth_db)
            headers = {"Authorization": f"Bearer {token}"}
            start = client.post("/api/connectors/google/start", headers=headers)
            state = _state_from_url(start.json()["authorizationUrl"])
            client.get(
                f"/api/connectors/google/callback?code=auth-code&state={state}",
                follow_redirects=False,
            )
            oauth_manager._tokens["google"] = "legacy-tok"
            oauth_manager._invalidate_cache(None, "google")
            resp = client.patch("/api/profile", json={"displayName": "A2"}, headers=headers)
            assert resp.status_code == 200
            google = [i for i in resp.json()["connected_providers"] if i["provider"] == "google"][0]
            assert google["connected"] is True and google["account"] == "a@example.com"
    finally:
        oauth_manager._client = old_client
        oauth_manager._tokens.pop("google", None)
        oauth_manager._invalidate_cache(None, "google")


def test_remember_account_fills_unknown(oauth_db):
    """S6: a connection saved with an unknown account (profile fetch
    failed at complete time) gets its account filled on verify."""
    mock = ProviderMock()
    mock.fail_profile = True
    manager = _manager(mock)
    user = _user("a@example.com", oauth_db)
    _connect(manager, user["id"], "google")
    with db_module.get_connection(oauth_db) as conn:
        row = conn.execute(
            "SELECT * FROM oauth_connections WHERE user_id = ? AND provider = ?",
            (user["id"], "google"),
        ).fetchone()
    assert row["account_email"] is None
    mock.fail_profile = False
    status = {item["provider"]: item for item in manager.status(user["id"], force_check=True)}
    assert status["google"]["account"] == "a@example.com"
    with db_module.get_connection(oauth_db) as conn:
        row = conn.execute(
            "SELECT * FROM oauth_connections WHERE user_id = ? AND provider = ?",
            (user["id"], "google"),
        ).fetchone()
    assert row["account_email"] == "a@example.com"


def test_v1_table_migrates_to_v2(oauth_db):
    """A v1 oauth_connections table (no id column) is rebuilt, rows kept."""
    import sqlite3

    db_file = oauth_db
    user = _user("v1@example.com", db_file)
    conn = sqlite3.connect(db_file)
    conn.execute("DROP TABLE oauth_connections")
    conn.execute(
        """CREATE TABLE oauth_connections (
            user_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            access_token_enc TEXT NOT NULL,
            refresh_token_enc TEXT,
            account_email TEXT,
            scopes TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (user_id, provider)
        )"""
    )
    conn.execute(
        "INSERT INTO oauth_connections VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (user["id"], "google", "enc-a", "enc-r", "a@example.com", "s", 100, 100),
    )
    conn.commit()
    conn.close()
    db_module.init_db(db_file)
    with db_module.get_connection(db_file) as conn:
        row = conn.execute("SELECT * FROM oauth_connections").fetchone()
    assert row is not None
    assert row["id"] and row["is_primary"] == 1 and row["migrated_from_legacy"] == 0
    assert row["account_email"] == "a@example.com"
