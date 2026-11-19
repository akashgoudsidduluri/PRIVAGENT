"""Email + password user auth for the PrivAgent website.

Additive by design: the single shared local token (``PRIVAGENT_LOCAL_TOKEN``,
checked by ``app.auth.verify_token``) keeps working untouched. A user
session is a second credential the security guard accepts alongside it.

No mailer exists in the backend, so there is deliberately NO email
verification or password reset in this module — a later mailer step adds
them. Passwords hash with stdlib ``hashlib.scrypt`` (no new dependencies:
bcrypt/passlib/argon2 are not installed). Session tokens are stored as
SHA-256 hashes only, never in plain text, and never logged.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import time
import uuid
from typing import Optional

from . import db as db_module

SESSION_TTL_SECONDS = 30 * 24 * 3600  # 30 days
MAX_LOGIN_FAILURES = 5
LOGIN_FAILURE_WINDOW_SECONDS = 15 * 60

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# email -> (fail_count, window_start_epoch). In-memory on purpose: a
# restart clears lockouts, and no failure history is persisted. Guarded
# by a lock: sync endpoints run in a threadpool, so two concurrent
# failed logins for the same email must not race the counter.
_login_failures: dict[str, tuple[int, float]] = {}
_login_lock = threading.Lock()


class AuthError(ValueError):
    """An auth failure with the HTTP status the route should return."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _now() -> int:
    return int(time.time())


def hash_password(password: str) -> tuple[str, str]:
    """Return (key_hex, salt_hex) for a password. Fresh salt every call."""
    salt = secrets.token_bytes(32)
    key = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=64)
    return key.hex(), salt.hex()


def verify_password(password: str, password_hash: str, salt: str) -> bool:
    try:
        key = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1, dklen=64
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(key.hex(), password_hash)


def avatar_initials(display_name: Optional[str], email: str) -> str:
    name = (display_name or "").strip()
    if name:
        return "".join(part[0] for part in name.split()[:2]).upper()
    return email.split("@")[0][:2].upper()


def _public_user(row) -> dict:
    return {
        "id": row["id"],
        "email": row["email"],
        "display_name": row["display_name"],
        "created_at": row["created_at"],
        "last_login_at": row["last_login_at"],
    }


# Dummy credentials for the unknown-email path in login(): an unknown
# email must cost the same scrypt work as a wrong password, or timing
# reveals whether the account exists. Computed once at import.
_DUMMY_HASH, _DUMMY_SALT = hash_password("dummy-password-never-used")


def _login_locked(email: str) -> bool:
    with _login_lock:
        count, start = _login_failures.get(email, (0, 0.0))
        now = time.time()
        if count >= MAX_LOGIN_FAILURES and now - start < LOGIN_FAILURE_WINDOW_SECONDS:
            return True
        if now - start >= LOGIN_FAILURE_WINDOW_SECONDS:
            _login_failures.pop(email, None)
        return False


def _record_login_failure(email: str) -> None:
    now = time.time()
    with _login_lock:
        count, start = _login_failures.get(email, (0, now))
        if now - start >= LOGIN_FAILURE_WINDOW_SECONDS:
            count, start = 0, now
        _login_failures[email] = (count + 1, start)


def _clear_login_failures(email: str) -> None:
    with _login_lock:
        _login_failures.pop(email, None)


def signup(
    email: str, password: str, display_name: Optional[str] = None, *, db_path: Optional[str] = None
) -> tuple[dict, str]:
    """Create a user and a session. Returns (public_user, session_token)."""
    email = (email or "").strip().lower()
    if not _EMAIL_RE.match(email):
        raise AuthError(400, "enter a valid email address")
    if not password or len(password) < 8:
        raise AuthError(400, "password must be at least 8 characters")
    name = (display_name or "").strip() or email.split("@")[0]
    password_hash, salt = hash_password(password)
    user_id = uuid.uuid4().hex
    now = _now()
    with db_module.get_connection(db_path) as conn:
        if conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone():
            raise AuthError(409, "an account with this email already exists")
        try:
            conn.execute(
                "INSERT INTO users (id, email, password_hash, salt, display_name,"
                " created_at, last_login_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, email, password_hash, salt, name, now, now),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            # Two simultaneous signups for the same email can both pass
            # the check above; the UNIQUE constraint decides, and the
            # loser gets a 409, not a 500.
            raise AuthError(409, "an account with this email already exists") from None
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    token = create_session(user_id, db_path=db_path)
    return _public_user(row), token


def login(email: str, password: str, *, db_path: Optional[str] = None) -> tuple[dict, str]:
    """Check credentials and open a session. Returns (public_user, session_token)."""
    email = (email or "").strip().lower()
    if _login_locked(email):
        raise AuthError(429, "too many failed attempts — try again in a few minutes")
    with db_module.get_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if row is None:
        # Same scrypt work as a real check, result discarded: timing
        # must not reveal whether the email exists.
        verify_password(password or "", _DUMMY_HASH, _DUMMY_SALT)
        _record_login_failure(email)
        raise AuthError(401, "email or password is incorrect")
    # Same message whether the email is unknown or the password is wrong:
    # the response must never reveal whether an account exists.
    if not verify_password(password or "", row["password_hash"], row["salt"]):
        _record_login_failure(email)
        raise AuthError(401, "email or password is incorrect")
    _clear_login_failures(email)
    now = _now()
    with db_module.get_connection(db_path) as conn:
        conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now, row["id"]))
        conn.commit()
    user = _public_user(row)
    user["last_login_at"] = now
    token = create_session(row["id"], db_path=db_path)
    return user, token


def create_session(
    user_id: str, *, db_path: Optional[str] = None, expires_in_seconds: int = SESSION_TTL_SECONDS
) -> str:
    """Open a session and return the raw token (shown to the user once)."""
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    now = _now()
    with db_module.get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, expires_at, created_at)"
            " VALUES (?, ?, ?, ?)",
            (token_hash, user_id, now + expires_in_seconds, now),
        )
        conn.commit()
    return token


def verify_session(token: Optional[str], *, db_path: Optional[str] = None) -> Optional[str]:
    """Return the user id for a live session token, else None."""
    if not token:
        return None
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with db_module.get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT user_id, expires_at FROM sessions WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
    if row is None:
        return None
    if row["expires_at"] <= _now():
        # Expired sessions are deleted when presented, so the table
        # does not grow one row per login forever.
        with db_module.get_connection(db_path) as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
            conn.commit()
        return None
    return row["user_id"]


def revoke_session(token: Optional[str], *, db_path: Optional[str] = None) -> None:
    if not token:
        return
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with db_module.get_connection(db_path) as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
        conn.commit()


def get_profile(user_id: str, *, db_path: Optional[str] = None) -> Optional[dict]:
    with db_module.get_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        return None
    profile = _public_user(row)
    profile["avatar_initials"] = avatar_initials(row["display_name"], row["email"])
    return profile


def update_profile(
    user_id: str, *, display_name: Optional[str] = None, db_path: Optional[str] = None
) -> Optional[dict]:
    if display_name is None:
        # PATCH {} is a no-op returning the current profile; only an
        # explicitly empty name is an error.
        return get_profile(user_id, db_path=db_path)
    name = (display_name or "").strip()
    if not name:
        raise AuthError(400, "display name must not be empty")
    with db_module.get_connection(db_path) as conn:
        cur = conn.execute(
            "UPDATE users SET display_name = ? WHERE id = ?", (name, user_id)
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    profile = _public_user(row)
    profile["avatar_initials"] = avatar_initials(row["display_name"], row["email"])
    return profile
