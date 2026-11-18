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
import subprocess
import sys
from pathlib import Path

import pytest
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


def test_cors_preflight_without_token_succeeds(disable_auto_auth):
    """A real browser preflight carries no Authorization header, so the
    auth gate must not 401 it — CORSMiddleware has to answer. Without
    this the workspace at localhost:5173 is blocked by CORS on every
    authenticated call (the preflight fails, so the real request never
    fires), even though the token itself is fine. The existing
    preflight test passes only because the suite's TestClient injects
    the token automatically — a real browser never does."""
    with TestClient(app) as client:
        r = client.options(
            "/api/chats",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert r.status_code == 200
        assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"


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
        if os.name != "nt":  # Windows has no Unix mode bits (stat reports 0o666)
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


def test_print_local_token_script_prints_env_token():
    """The operator's supported way to read the token back is
    backend/scripts/print_local_token.py. With PRIVAGENT_LOCAL_TOKEN
    set it must print exactly that value — the token the running
    backend accepts — without touching the encrypted store."""
    backend_dir = Path(__file__).resolve().parents[1]
    env_file = backend_dir / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(f"{auth.ENV_VAR}="):
                pytest.skip(
                    "repo backend/.env sets PRIVAGENT_LOCAL_TOKEN, "
                    "which the script correctly prefers over the env var"
                )
    sentinel = "print-script-test-token-0123456789abcdef"
    env = dict(os.environ)
    env[auth.ENV_VAR] = sentinel
    result = subprocess.run(
        [sys.executable, str(backend_dir / "scripts" / "print_local_token.py")],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == sentinel


def test_print_local_token_script_loads_dotenv():
    """Guard: the script must load backend/.env before reading the
    token, mirroring app.config. Without it, it prints the encrypted
    store token while the backend honours PRIVAGENT_LOCAL_TOKEN (or
    the master key) from .env — a silent mismatch that hands the
    operator a token the backend rejects."""
    backend_dir = Path(__file__).resolve().parents[1]
    text = (backend_dir / "scripts" / "print_local_token.py").read_text(
        encoding="utf-8"
    )
    assert "load_dotenv" in text
    assert text.index("load_dotenv") < text.index("print(get_local_token())")


def test_stored_token_rotation_takes_effect_without_restart(monkeypatch):
    """DeepSeek review 2026-11-17: get_local_token cached the stored
    token after the first read, so rotating the secret in the store
    left the old token valid in a running process until restart. The
    store must be re-read on every call."""
    from app import secrets as secrets_store

    monkeypatch.delenv(auth.ENV_VAR, raising=False)
    auth.reset_for_tests()
    secrets_store.set_secret(auth.STORE_KEY, "first-token-0123456789abcdef")
    assert auth.get_local_token() == "first-token-0123456789abcdef"
    assert auth.verify_token("first-token-0123456789abcdef") is True
    secrets_store.set_secret(auth.STORE_KEY, "second-token-0123456789abcdef")
    assert auth.get_local_token() == "second-token-0123456789abcdef"
    assert auth.verify_token("first-token-0123456789abcdef") is False
    assert auth.verify_token("second-token-0123456789abcdef") is True
    auth.reset_for_tests()


def test_cached_token_survives_unreadable_store(monkeypatch):
    """Fail safe: if the store becomes unreadable after a token was
    cached, the running process keeps accepting the cached token
    instead of minting a new one on every call (which would 401 every
    request)."""
    from app import secrets as secrets_store

    monkeypatch.delenv(auth.ENV_VAR, raising=False)
    auth.reset_for_tests()
    secrets_store.set_secret(auth.STORE_KEY, "cached-token-0123456789abcdef")
    assert auth.get_local_token() == "cached-token-0123456789abcdef"
    monkeypatch.setattr(auth, "_store_token", lambda: None)
    assert auth.get_local_token() == "cached-token-0123456789abcdef"
    assert auth.verify_token("cached-token-0123456789abcdef") is True
    auth.reset_for_tests()
