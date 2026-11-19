"""User auth + profile for the PrivAgent website (email + password).

Pre-fix state: there are no user accounts at all — the only auth is the
single shared local token, POST /api/auth/signup is unreachable (401 from
the guard), and GET /api/profile does not exist. These tests prove the
new flow: signup/login/logout, session tokens, profile read/update, and
that the local-token flow keeps working untouched.

No mailer exists in the backend, so there are deliberately no
email-verification / password-reset tests here — that is a documented gap.
"""

import time

import pytest

from app import db as db_module
from app.auth_users import (
    create_session,
    hash_password,
    verify_password,
    verify_session,
)


@pytest.fixture
def auth_db(tmp_path, monkeypatch):
    """Isolated temporary SQLite database for auth tests."""
    db_file = str(tmp_path / "auth_test.db")
    monkeypatch.setenv("PRIVAGENT_DB_PATH", db_file)
    db_module.init_db(db_file)
    return db_file


def _signup(client, email="akash@example.com", password="correct-horse-123", display_name="Akash"):
    return client.post(
        "/api/auth/signup",
        json={"email": email, "password": password, "display_name": display_name},
    )


def _login(client, email="akash@example.com", password="correct-horse-123"):
    return client.post("/api/auth/login", json={"email": email, "password": password})


def test_signup_creates_user_and_session(auth_db, disable_auto_auth):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        resp = _signup(client)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["user"]["email"] == "akash@example.com"
        assert body["user"]["display_name"] == "Akash"
        assert "password_hash" not in body["user"]
        assert "password" not in body["user"]
        token = body["session_token"]
        assert token

        me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200, me.text
        assert me.json()["email"] == "akash@example.com"


def test_signup_validation(auth_db, disable_auto_auth):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        assert _signup(client, email="not-an-email").status_code in (400, 422)
        assert _signup(client, email="a@b.co", password="short").status_code in (400, 422)
        # Display name is optional — defaults to the email prefix.
        resp = _signup(client, email="naman@example.com", password="correct-horse-123", display_name=None)
        assert resp.status_code == 201, resp.text
        assert resp.json()["user"]["display_name"]


def test_signup_duplicate_email(auth_db, disable_auto_auth):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        assert _signup(client).status_code == 201
        assert _signup(client).status_code == 409


def test_login_ok_and_wrong_password_same_message(auth_db, disable_auto_auth):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        assert _signup(client).status_code == 201
        ok = _login(client)
        assert ok.status_code == 200, ok.text
        assert ok.json()["session_token"]

        wrong = _login(client, password="definitely-wrong-1")
        assert wrong.status_code == 401
        unknown = _login(client, email="nobody@example.com", password="definitely-wrong-1")
        assert unknown.status_code == 401
        # Never reveal whether the email exists.
        assert wrong.json()["detail"] == unknown.json()["detail"]


def test_profile_requires_user_session(auth_db, authed_client, disable_auto_auth):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        token = _signup(client).json()["session_token"]
        # Local token must NOT return a profile.
        assert authed_client.get("/api/profile").status_code == 401
        # No token at all -> 401.
        assert client.get("/api/profile").status_code == 401
        # Session token -> profile with connected providers.
        prof = client.get("/api/profile", headers={"Authorization": f"Bearer {token}"})
        assert prof.status_code == 200, prof.text
        body = prof.json()
        assert body["email"] == "akash@example.com"
        assert body["avatar_initials"]
        assert body["created_at"]
        assert isinstance(body["connected_providers"], list)


def test_profile_update(auth_db, disable_auto_auth):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        token = _signup(client).json()["session_token"]
        headers = {"Authorization": f"Bearer {token}"}
        resp = client.patch("/api/profile", json={"display_name": "Akash Goud"}, headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["display_name"] == "Akash Goud"
        assert resp.json()["email"] == "akash@example.com"  # email is not changeable
        # Avatar initials follow the new name.
        assert resp.json()["avatar_initials"] == "AG"


def test_logout_revokes_session(auth_db, disable_auto_auth):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        token = _signup(client).json()["session_token"]
        headers = {"Authorization": f"Bearer {token}"}
        assert client.get("/api/auth/me", headers=headers).status_code == 200
        assert client.post("/api/auth/logout", headers=headers).status_code == 200
        assert client.get("/api/auth/me", headers=headers).status_code == 401


def test_session_token_not_accepted_via_query(auth_db, disable_auto_auth):
    """Session tokens are header-only: they must never travel in URLs."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        token = _signup(client).json()["session_token"]
        assert client.get(f"/api/profile?token={token}").status_code == 401


def test_local_token_still_works(auth_db, authed_client):
    """The additive auth must not change the existing local-token flow."""
    resp = authed_client.get("/api/connectors")
    assert resp.status_code == 200, resp.text


def test_login_brute_force_guard(auth_db, disable_auto_auth):
    from fastapi.testclient import TestClient

    from app.main import app

    # Own email: the guard is per-email and in-memory, so no other test
    # may share this address or the count would leak across tests.
    with TestClient(app) as client:
        assert _signup(client, email="brute@example.com").status_code == 201
        for _ in range(5):
            assert _login(client, email="brute@example.com", password="wrong-password-1").status_code == 401
        assert _login(client, email="brute@example.com", password="wrong-password-1").status_code == 429


def test_password_hash_roundtrip():
    hashed, salt = hash_password("correct-horse-123")
    assert verify_password("correct-horse-123", hashed, salt) is True
    assert verify_password("wrong-password-1", hashed, salt) is False
    # Same password, different salt -> different hash.
    hashed2, salt2 = hash_password("correct-horse-123")
    assert salt2 != salt
    assert hashed2 != hashed


def test_expired_session_rejected(auth_db):
    from app.auth_users import signup

    user, _ = signup("expire@example.com", "correct-horse-123", "Expire", db_path=auth_db)
    token = create_session(user["id"], db_path=auth_db, expires_in_seconds=-1)
    assert token
    assert verify_session(token, db_path=auth_db) is None
    # A live session verifies, and a bogus token does not.
    live = create_session(user["id"], db_path=auth_db)
    assert isinstance(live, str) and live
    assert verify_session(live, db_path=auth_db) == user["id"]
    assert verify_session("bogus-token", db_path=auth_db) is None


# --- Independent review fixes (2026-11-19) ---------------------------------

def test_login_unknown_email_still_costs_a_password_check(auth_db, monkeypatch):
    """M1: login must not reveal by timing whether an email exists.

    An unknown email used to skip the scrypt call entirely (the ``or``
    short-circuited), while a wrong password for a real account paid
    ~50-100 ms. The fix runs a dummy verify against a fixed hash/salt
    and discards the result.
    """
    import app.auth_users as auth_users

    calls = []
    real_verify = auth_users.verify_password

    def counting_verify(password, password_hash, salt):
        calls.append((password, password_hash))
        return real_verify(password, password_hash, salt)

    monkeypatch.setattr(auth_users, "verify_password", counting_verify)
    with pytest.raises(auth_users.AuthError) as excinfo:
        auth_users.login("nobody@example.com", "definitely-wrong-1", db_path=auth_db)
    assert excinfo.value.status == 401
    assert len(calls) == 1


def test_signup_race_returns_409_not_500(auth_db, monkeypatch):
    """S3: two simultaneous signups for the same email — the loser gets 409.

    The SELECT-then-INSERT check can be beaten by a concurrent signup;
    without the IntegrityError catch the UNIQUE violation escapes as a
    500. The blinded connection simulates the race deterministically:
    the check sees no row, but the real INSERT still hits the existing
    account.
    """
    import app.auth_users as auth_users

    auth_users.signup("race@example.com", "correct-horse-123", "Race", db_path=auth_db)
    real_get_connection = db_module.get_connection

    class BlindConnection:
        """A connection blind to the existing row on the email check."""

        def __init__(self, conn):
            self._conn = conn

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self._conn.__exit__(*exc)

        def execute(self, sql, params=()):
            if sql.strip().upper().startswith("SELECT ID FROM USERS WHERE EMAIL"):
                class Empty:
                    def fetchone(self):
                        return None

                return Empty()
            return self._conn.execute(sql, params)

        def commit(self):
            return self._conn.commit()

    def blind_get_connection(db_path=None):
        return BlindConnection(real_get_connection(db_path))

    monkeypatch.setattr(db_module, "get_connection", blind_get_connection)
    with pytest.raises(auth_users.AuthError) as excinfo:
        auth_users.signup("race@example.com", "correct-horse-123", "Race", db_path=auth_db)
    assert excinfo.value.status == 409


def test_profile_patch_empty_body_is_noop(auth_db, disable_auto_auth):
    """S4: PATCH /api/profile with {} returns the current profile.

    display_name is optional — a missing value is a no-op. Only an
    explicitly empty name is a 400.
    """
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        token = _signup(client).json()["session_token"]
        headers = {"Authorization": f"Bearer {token}"}
        resp = client.patch("/api/profile", json={}, headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["display_name"] == "Akash"
        assert resp.json()["email"] == "akash@example.com"
        assert client.patch("/api/profile", json={"display_name": ""}, headers=headers).status_code == 400


def test_expired_session_row_is_deleted(auth_db):
    """S5: presenting an expired session deletes its row."""
    import hashlib

    from app.auth_users import signup

    user, _ = signup("expire-del@example.com", "correct-horse-123", "ExpireDel", db_path=auth_db)
    token = create_session(user["id"], db_path=auth_db, expires_in_seconds=-1)
    assert verify_session(token, db_path=auth_db) is None
    # Signup itself minted a live session for this user, so check the
    # expired token's row specifically, not just "any row for the user".
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with db_module.get_connection(auth_db) as conn:
        row = conn.execute(
            "SELECT token_hash FROM sessions WHERE token_hash = ?", (token_hash,)
        ).fetchone()
    assert row is None
