"""Local operator auth (Task 1): every /api/* route requires a bearer
token, a missing Origin no longer bypasses anything, and the
unauthenticated test backdoor is gone.

Most tests in this file use ``disable_auto_auth`` (see conftest.py):
the suite otherwise injects the test token automatically, so "no
token" would not really mean no token.
"""
from __future__ import annotations

import os
import stat

from fastapi.testclient import TestClient

from app import auth
from app.main import _origin_allowed, app
from conftest import AUTH_HEADERS, TEST_TOKEN


def test_no_token_returns_401(disable_auto_auth):
    with TestClient(app) as client:
        r = client.get("/api/chats")
        assert r.status_code == 401
        assert "token" in r.json()["detail"].lower()
        assert r.headers.get("www-authenticate") == "Bearer"


def test_wrong_token_returns_401(disable_auto_auth):
    with TestClient(app) as client:
        r = client.get(
            "/api/chats", headers={"Authorization": "Bearer wrong-token"}
        )
        assert r.status_code == 401


def test_malformed_authorization_returns_401(disable_auto_auth):
    with TestClient(app) as client:
        assert client.get(
            "/api/chats", headers={"Authorization": "Basic abc123"}
        ).status_code == 401
        assert client.get(
            "/api/chats", headers={"Authorization": "Bearer"}
        ).status_code == 401


def test_correct_token_returns_200():
    with TestClient(app) as client:
        r = client.get("/api/chats", headers=AUTH_HEADERS)
        assert r.status_code == 200
        assert isinstance(r.json(), list)


def test_missing_origin_does_not_bypass_auth(disable_auto_auth):
    # The old origin guard treated "no Origin" as "curl / tests, allow".
    # A missing Origin must now still present a valid token.
    with TestClient(app) as client:
        r = client.post("/api/task", json={"instruction": "exfiltrate"})
        assert r.status_code == 401
        # An allow-listed Origin without a token does not help either.
        r = client.post(
            "/api/task",
            json={"instruction": "exfiltrate"},
            headers={"Origin": "http://localhost:5173"},
        )
        assert r.status_code == 401


def test_llm_config_requires_auth(disable_auto_auth):
    with TestClient(app) as client:
        assert client.get("/api/llm/config").status_code == 401
        assert client.post(
            "/api/llm/config", json={"provider": "mock"}
        ).status_code == 401
    with TestClient(app) as client:
        assert client.get("/api/llm/config", headers=AUTH_HEADERS).status_code == 200


def test_test_execute_action_endpoint_is_gone():
    # Removed in Task 1: it was an unauthenticated backdoor that drove
    # the extension directly, bypassing planner, redaction, and gate.
    with TestClient(app) as client:
        r = client.post(
            "/api/test/execute_action",
            json={"action": {"type": "navigate", "url": "https://example.org"}},
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 404


def test_health_is_exempt_from_auth(disable_auto_auth):
    with TestClient(app) as client:
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


def test_evil_origin_rejected_even_with_valid_token():
    # Defence in depth: a disallowed Origin is rejected before auth is
    # even considered, so a stolen token cannot be spent cross-site.
    with TestClient(app) as client:
        r = client.post(
            "/api/task",
            json={"instruction": "exfiltrate"},
            headers={**AUTH_HEADERS, "Origin": "https://evil.example"},
        )
        assert r.status_code == 403


def test_origin_helper_missing_origin_is_not_allowed():
    assert _origin_allowed(None) is False
    assert _origin_allowed("http://localhost:5173") is True
    assert _origin_allowed("chrome-extension://abcdefghijklmnop") is True
    assert _origin_allowed("https://evil.example") is False


def test_query_token_accepted_for_get_only(disable_auto_auth):
    # Anchor navigations (<a href> preview/download) cannot send a
    # header, so GET requests may carry ?token=. It must not work for
    # state-changing requests.
    with TestClient(app) as client:
        ok = client.get(f"/api/artifacts/nope/download?token={TEST_TOKEN}")
        assert ok.status_code == 404  # 404 proves auth passed (no such artifact)
        assert client.get("/api/artifacts/nope/download").status_code == 401
        assert client.post(
            "/api/task?token=" + TEST_TOKEN, json={"instruction": "x"}
        ).status_code == 401


def test_generated_token_is_persisted_encrypted_with_mode_0600(tmp_path, monkeypatch):
    # Task 2: a generated token no longer lives in a plaintext .local_token
    # file. It is stored encrypted in the secret store (mode 0600); the
    # legacy plaintext file must not exist afterwards.
    from app import secrets as secrets_store

    store_dir = tmp_path / "secrets"
    monkeypatch.setenv(secrets_store.SECRETS_DIR_ENV, str(store_dir))
    monkeypatch.setenv(secrets_store.MASTER_KEY_ENV, "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    secrets_store.reset_for_tests()
    token_file = tmp_path / ".local_token"
    monkeypatch.setattr(auth, "TOKEN_FILE", token_file)
    monkeypatch.setenv(auth.ENV_VAR, "")
    auth.reset_for_tests()
    try:
        token = auth.get_local_token()
        assert len(token) == 64  # 32 random bytes, hex-encoded
        assert int(token, 16) >= 0
        assert not token_file.exists()
        store_file = store_dir / "secrets.enc.json"
        assert store_file.exists()
        assert stat.S_IMODE(store_file.stat().st_mode) == 0o600
        assert token not in store_file.read_text(encoding="utf-8")
        # Second call reuses the stored token; it is not regenerated.
        auth.reset_for_tests()
        assert auth.get_local_token() == token
        assert auth.verify_token(token) is True
        assert auth.verify_token("not-the-token") is False
        assert auth.verify_token(None) is False
    finally:
        auth.reset_for_tests()
        secrets_store.reset_for_tests()
        monkeypatch.setenv(auth.ENV_VAR, TEST_TOKEN)


def test_env_token_wins_over_file_token(tmp_path, monkeypatch):
    token_file = tmp_path / ".local_token"
    token_file.write_text("file-token-value\n", encoding="utf-8")
    monkeypatch.setattr(auth, "TOKEN_FILE", token_file)
    monkeypatch.setenv(auth.ENV_VAR, "env-token-value")
    auth.reset_for_tests()
    try:
        assert auth.get_local_token() == "env-token-value"
    finally:
        auth.reset_for_tests()
        monkeypatch.setenv(auth.ENV_VAR, TEST_TOKEN)
