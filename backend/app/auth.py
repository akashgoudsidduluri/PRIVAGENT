"""Local single-operator authentication for the NAVIGATOR backend.

The backend binds to loopback, but loopback alone is not authentication:
any process on the machine (or any page served from localhost) could
otherwise create tasks, read chats, change the LLM key, and drive the
user's browser. Every ``/api/*`` route therefore requires a bearer
token, checked with :func:`hmac.compare_digest` (constant time).

Token source, in order:
1. ``PRIVAGENT_LOCAL_TOKEN`` in the environment / ``backend/.env``
   (preferred for a fixed, operator-chosen token).
2. The encrypted secret store (``app.secrets``, key
   ``"local_auth_token"``) — this is where generated tokens live since
   Task 2.
3. Legacy ``backend/data/.local_token`` (Task 1, plaintext, mode
   ``0600``). If it exists, its value is migrated into the encrypted
   store and the plaintext file is deleted — unless encryption fails,
   in which case the file is kept and still honoured (fail safe: never
   lock the operator out by deleting a token we could not store).
4. A fresh token (32 random bytes, hex-encoded) is generated and stored
   encrypted in the secret store.

The token is deliberately a single shared operator secret, not a
per-user account system: this is a single-user local tool. Do not
reuse it as a multi-user session mechanism.
"""
from __future__ import annotations

import hmac
import logging
import os
import secrets
from pathlib import Path
from typing import Optional

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

logger = logging.getLogger("navigator.auth")

ENV_VAR = "PRIVAGENT_LOCAL_TOKEN"
TOKEN_FILE = Path(__file__).resolve().parents[1] / "data" / ".local_token"
STORE_KEY = "local_auth_token"

# Bearer scheme used by the ``require_auth`` dependency. ``auto_error``
# is off so we can return a uniform 401 with a WWW-Authenticate header.
_bearer_scheme = HTTPBearer(auto_error=False)

_token_cache: Optional[str] = None
# The store-read warning below fires at most once per process: the
# token is re-read from the store on every call now (see
# get_local_token), so a corrupt store must not spam one warning per
# request.
_store_read_warned: bool = False


def _store_token() -> Optional[str]:
    """Read the operator token from the encrypted store (best effort)."""
    global _store_read_warned
    try:
        from . import secrets as secrets_store
        return secrets_store.get_secret(STORE_KEY)
    except Exception as exc:  # corrupt store / missing cryptography: fall through
        if not _store_read_warned:
            _store_read_warned = True
            logger.warning("Could not read local token from the encrypted store: %s", exc)
        return None


def _migrate_legacy_file_token() -> Optional[str]:
    """Move a Task 1 plaintext ``.local_token`` into the encrypted store.

    Returns the token if one was found (migrated or not). The plaintext
    file is deleted only after the encrypted round-trip verifies.
    """
    try:
        if not TOKEN_FILE.exists():
            return None
        stored = TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.warning("Could not read local token file %s: %s", TOKEN_FILE, exc)
        return None
    if not stored:
        return None
    try:
        from . import secrets as secrets_store
        secrets_store.set_secret(STORE_KEY, stored)
        if secrets_store.get_secret(STORE_KEY) != stored:
            raise ValueError("encrypted round-trip verification failed")
    except Exception as exc:
        logger.warning(
            "Could not migrate plaintext token file %s to the encrypted store; "
            "keeping the file for now: %s",
            TOKEN_FILE, exc,
        )
        return stored
    try:
        TOKEN_FILE.unlink()
    except OSError as exc:
        logger.warning("Migrated token but could not delete plaintext file %s: %s", TOKEN_FILE, exc)
    else:
        logger.info(
            "Migrated the local operator token from plaintext %s into the encrypted secret store.",
            TOKEN_FILE,
        )
    return stored


def get_local_token() -> str:
    """Return the operator token, generating and persisting one if needed.

    The environment variable wins on every call so tests and operators
    can rotate the token without restarting the process. A token in
    the encrypted store is re-read on every call, so rotating the
    stored token also takes effect immediately — a running process
    must not keep accepting a rotated-away token (DeepSeek review
    2026-11-17). Only a freshly generated token is cached
    in-process, and only as a fallback when the store cannot be read,
    so a corrupt store neither locks the operator out nor mints a new
    token on every request. A plaintext legacy file left behind by a
    failed delete is removed once the store copy has been read back
    successfully.
    """
    global _token_cache
    env_token = os.environ.get(ENV_VAR, "").strip()
    if env_token:
        return env_token
    stored = _store_token()
    if stored:
        _token_cache = stored
        if TOKEN_FILE.exists():
            try:
                TOKEN_FILE.unlink()
            except OSError:
                pass
        return stored
    if _token_cache:
        return _token_cache
    legacy = _migrate_legacy_file_token()
    if legacy:
        _token_cache = legacy
        return legacy
    token = secrets.token_hex(32)  # 32 random bytes, hex-encoded
    try:
        from . import secrets as secrets_store
        secrets_store.set_secret(STORE_KEY, token)
    except Exception as exc:
        # The token still works for this process lifetime; it just will
        # not survive a restart. Never log the token value itself.
        logger.warning("Could not persist local token to the encrypted store: %s", exc)
    else:
        logger.info(
            "Generated a new local operator token and stored it encrypted in the secret store. "
            "Set %s in backend/.env to use a fixed token instead.",
            ENV_VAR,
        )
    _token_cache = token
    return token


def verify_token(candidate: Optional[str]) -> bool:
    """Constant-time check of a candidate bearer token."""
    if not candidate:
        return False
    return hmac.compare_digest(candidate.strip(), get_local_token())


def reset_for_tests() -> None:
    """Clear the in-process file-token cache (tests only)."""
    global _token_cache
    _token_cache = None


async def require_auth(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> None:
    """FastAPI dependency: reject requests without a valid bearer token."""
    if (
        credentials is None
        or credentials.scheme.lower() != "bearer"
        or not verify_token(credentials.credentials)
    ):
        raise HTTPException(
            status_code=401,
            detail="missing or invalid local auth token",
            headers={"WWW-Authenticate": "Bearer"},
        )
