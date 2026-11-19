"""Local OAuth authorization state and access tokens.

Two scopes exist side by side:

* **Per-user connections** (the Connected Services experience): tokens live
  in the ``oauth_connections`` table, one row per (user, provider,
  account) — a user can hold several accounts from the same provider.
  Fernet-encrypted via ``app.secrets.encrypt_text``. OAuth state lives
  in ``oauth_states`` (DB-backed, so a restart between authorize and
  callback does not kill the flow) and binds the callback to the user
  who started it. Every token operation takes ``user_id``.
* **Legacy global tokens** (``user_id=None``): the pre-existing single
  in-memory/env/store tokens. The local-token operator and all
  background paths (email missions, connector_runner, capabilities)
  keep exactly today's behaviour through this path. Legacy globals are
  NEVER silently transferred to a user: migration is an explicit,
  ownership-verified, confirmed step (see ``migrate_legacy``), and the
  globals stay in place afterwards so background paths keep working.

Client IDs and secrets are per-application, not per-user: they resolve
from ``PRIVAGENT_{PROVIDER}_OAUTH_CLIENT_ID/SECRET`` env vars (see
``_oauth_credential``) and are never sent to the frontend.

Provider notes (verified against official docs 2026-11-19):
* PKCE S256 is used for Google, Microsoft and GitHub (GitHub OAuth
  Apps support PKCE S256 since 2025-07-14). Slack supports PKCE but
  it is deliberately NOT enabled: localhost redirects + bot scopes
  are incompatible with a PKCE-opted-in Slack app.
* Microsoft has no per-token revocation endpoint — disconnect there
  deletes locally only (never call revokeSignInSessions: it would
  sign the user out of every app).
* GitHub's ``repo`` scope is unavoidably over-broad for private-repo
  reads (no read-only private-repo OAuth scope exists); the verify
  path checks the X-OAuth-Scopes header and reports a reduced grant.
* Refresh semantics differ: Google never rotates, Microsoft issues a
  new refresh token every refresh, GitHub rotation is total, Slack's
  are single-use. The store saves a new refresh token when one is
  returned and keeps the old one when not.
"""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
import time
import uuid
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

from .. import db as db_module
from ..secrets import decrypt_text, encrypt_text
from . import GmailConnector, OutlookConnector, GoogleCalendarConnector, GoogleDriveConnector, SlackConnector, GitHubConnector

_PROVIDER = {
    # drive.readonly is required for search/extract on the user's EXISTING
    # files: drive.file only covers files this app created or opened, so
    # "find X in Drive" returned empty/403 on real accounts. drive.file
    # stays for uploads (upload_deliverable). Both are least-privilege:
    # readonly for reads, file-scoped for writes. Adding readonly needs
    # the user to re-consent on Google.
    "google": {"auth": "https://accounts.google.com/o/oauth2/v2/auth", "token": "https://oauth2.googleapis.com/token",
               "scopes": "https://www.googleapis.com/auth/gmail.modify https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/drive.readonly https://www.googleapis.com/auth/drive.file"},
    # User.Read is the documented least-privilege for GET /v1.0/me, which
    # is both the live check and the account-identity source. It needs no
    # admin consent. Mail.Send is deliberately NOT requested: the Outlook
    # connector has no send method, so Outlook is read/delete-only.
    "microsoft": {"auth": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
                  "token": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
                  "scopes": "User.Read offline_access Mail.ReadWrite Calendars.ReadWrite"},
    "slack": {"auth": "https://slack.com/oauth/v2/authorize", "token": "https://slack.com/api/oauth.v2.access",
              "scopes": "channels:read,channels:history,groups:history,chat:write"},
    "github": {"auth": "https://github.com/login/oauth/authorize", "token": "https://github.com/login/oauth/access_token",
               "scopes": "repo"},
}

# Providers whose authorize flow sends a PKCE S256 challenge. Slack is
# excluded on purpose (see the module docstring): a PKCE-opted-in Slack
# app cannot request bot scopes from a localhost redirect.
_PKCE_PROVIDERS = ("google", "microsoft", "github")

# Provider revoke endpoints. Microsoft has no user-token revocation
# endpoint: disconnect there deletes locally only (see _revoke_at_provider).
_REVOKE = {
    "google": "https://oauth2.googleapis.com/revoke",
    "slack": "https://slack.com/api/auth.revoke",
}

_CONNECTOR_CLS = {
    "gmail": GmailConnector, "outlook": OutlookConnector,
    "google_calendar": GoogleCalendarConnector, "google_drive": GoogleDriveConnector,
    "slack": SlackConnector, "github": GitHubConnector,
}


def _load_env_if_needed() -> None:
    """Ensure backend/.env is loaded into os.environ if not already loaded."""
    candidate_dirs = [
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        os.getcwd(),
        os.path.join(os.getcwd(), "backend"),
    ]
    for d in candidate_dirs:
        env_file = os.path.join(d, ".env")
        if os.path.exists(env_file):
            try:
                from dotenv import load_dotenv
                load_dotenv(dotenv_path=env_file, override=False)
                break
            except Exception:
                pass


def _store_get(env_key: str) -> str | None:
    """Read a secret that used to live in ``.env`` from the encrypted store.

    Best effort: a missing/corrupt store simply yields ``None`` and the
    caller falls back to the environment.
    """
    try:
        from .. import secrets as secrets_store
        return secrets_store.get_secret_for_env_key(env_key)
    except Exception:
        return None


def _store_set(env_key: str, value: str) -> None:
    """Write a secret to the encrypted store (never to ``.env``)."""
    from .. import secrets as secrets_store
    secrets_store.set_secret(secrets_store.ENV_TO_STORE_KEY[env_key], value)


def _store_delete(env_key: str) -> None:
    try:
        from .. import secrets as secrets_store
        secrets_store.delete_secret(secrets_store.ENV_TO_STORE_KEY[env_key])
    except Exception:
        pass


def _oauth_credential(provider: str, kind: str) -> str:
    """Resolve an OAuth client credential: environment first, store second.

    ``kind`` is ``"CLIENT_ID"`` or ``"CLIENT_SECRET"``. Client IDs are not
    secrets and still normally come from ``.env``; client secrets moved to
    the encrypted store in Task 2 (with ``.env`` migration on startup).
    """
    prefix = f"PRIVAGENT_{provider.upper()}_OAUTH_"
    env_value = os.environ.get(prefix + kind, "")
    if env_value:
        return env_value
    if kind == "CLIENT_SECRET":
        return _store_get(prefix + kind) or ""
    return ""


class OAuthManager:
    def __init__(self, client: httpx.Client | None = None, auto_hydrate: bool | None = None):
        self._client = client or httpx.Client(timeout=12, follow_redirects=False, trust_env=False)
        # Legacy in-memory states for the local-operator path only (no user).
        self._pending: dict[str, tuple[float, str, str]] = {}
        self._tokens: dict[str, str] = {}
        self._refresh_tokens: dict[str, str] = {}
        # Keyed by (user_id, provider, connection_id); user_id None is the
        # legacy scope and connection_id None is the primary connection.
        self._verification_cache: dict[tuple[Optional[str], str, Optional[str]], tuple[float, dict[str, Any]]] = {}
        self._is_test_client = client is not None
        # Hydrate any previously stored access tokens and refresh tokens from env
        # Only auto-hydrate for live default client, not in isolated test clients
        should_hydrate = (client is None) if auto_hydrate is None else auto_hydrate
        if should_hydrate:
            _load_env_if_needed()
            for name in _PROVIDER:
                env_tok = os.environ.get(f"PRIVAGENT_{name.upper()}_ACCESS_TOKEN")
                if not env_tok:
                    env_tok = _store_get(f"PRIVAGENT_{name.upper()}_ACCESS_TOKEN")
                if env_tok:
                    self._tokens[name] = env_tok.strip()
                env_ref = os.environ.get(f"PRIVAGENT_{name.upper()}_REFRESH_TOKEN")
                if not env_ref:
                    env_ref = _store_get(f"PRIVAGENT_{name.upper()}_REFRESH_TOKEN")
                if env_ref:
                    self._refresh_tokens[name] = env_ref.strip()

    def _invalidate_cache(self, user_id: Optional[str], provider: str) -> None:
        """Drop every cached verification for one (user, provider)."""
        self._verification_cache = {
            key: value for key, value in self._verification_cache.items()
            if not (key[0] == user_id and key[1] == provider)
        }

    # ------------------------------------------------------- per-user store
    def _connection_row(self, user_id: str, provider: str, connection_id: Optional[str] = None) -> Optional[dict]:
        """Resolve a user's connection row: the named one, the primary, else the oldest."""
        with db_module.get_connection() as conn:
            if connection_id is not None:
                row = conn.execute(
                    "SELECT * FROM oauth_connections WHERE id = ? AND user_id = ? AND provider = ?",
                    (connection_id, user_id, provider),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM oauth_connections WHERE user_id = ? AND provider = ?"
                    " ORDER BY is_primary DESC, created_at ASC LIMIT 1",
                    (user_id, provider),
                ).fetchone()
        return dict(row) if row is not None else None

    def find_connection_id(self, user_id: str, provider: str, account_email: str) -> Optional[str]:
        """Connection id for a user's account (case-insensitive match),
        or the user's primary connection for the provider when the
        account is not found. Used to stamp the exact connection on a
        mission so its ticks resolve that connection, not whichever row
        happens to be primary later."""
        with db_module.get_connection() as conn:
            row = conn.execute(
                "SELECT id FROM oauth_connections WHERE user_id = ? AND provider = ?"
                " AND LOWER(account_email) = LOWER(?)",
                (user_id, provider, account_email),
            ).fetchone()
            if row is not None:
                return row["id"]
        row = self._connection_row(user_id, provider)
        return row["id"] if row is not None else None

    def _user_tokens(self, user_id: str, provider: str, connection_id: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
        """Return (access_token, refresh_token) for a user's connection, decrypted."""
        row = self._connection_row(user_id, provider, connection_id)
        if row is None:
            return None, None
        return decrypt_text(row["access_token_enc"]), decrypt_text(row["refresh_token_enc"])

    def _save_user_tokens(
        self, user_id: str, provider: str, access_token: str,
        refresh_token: Optional[str] = None, account_email: Optional[str] = None,
        connection_id: Optional[str] = None, migrated_from_legacy: int = 0,
    ) -> str:
        """Upsert a user's connection row. Returns the connection id.

        Upsert key is (user_id, provider, account_email) — or the named
        connection id. The first row for a (user, provider) becomes the
        primary. A refresh without a new refresh token keeps the old one
        (Google never rotates; Microsoft/GitHub/Slack do — see the
        module docstring).
        """
        now = int(time.time())
        with db_module.get_connection() as conn:
            if connection_id is not None:
                row = conn.execute(
                    "SELECT id, refresh_token_enc FROM oauth_connections WHERE id = ? AND user_id = ? AND provider = ?",
                    (connection_id, user_id, provider),
                ).fetchone()
                if row is None:
                    raise PermissionError("connection not found")
            elif account_email is None:
                row = conn.execute(
                    "SELECT id, refresh_token_enc FROM oauth_connections"
                    " WHERE user_id = ? AND provider = ? AND account_email IS NULL",
                    (user_id, provider),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT id, refresh_token_enc FROM oauth_connections"
                    " WHERE user_id = ? AND provider = ? AND account_email = ?",
                    (user_id, provider, account_email),
                ).fetchone()
            if refresh_token is None and row is not None:
                refresh_enc = row["refresh_token_enc"]
            else:
                refresh_enc = encrypt_text(refresh_token)
            if row is not None:
                conn_id = row["id"]
                conn.execute(
                    "UPDATE oauth_connections SET access_token_enc = ?, refresh_token_enc = ?,"
                    " account_email = COALESCE(?, account_email), migrated_from_legacy = ?,"
                    " updated_at = ? WHERE id = ?",
                    (encrypt_text(access_token), refresh_enc, account_email,
                     migrated_from_legacy, now, conn_id),
                )
            else:
                conn_id = uuid.uuid4().hex
                existing = conn.execute(
                    "SELECT COUNT(*) AS n FROM oauth_connections WHERE user_id = ? AND provider = ?",
                    (user_id, provider),
                ).fetchone()["n"]
                is_primary = 1 if existing == 0 else 0
                try:
                    conn.execute(
                        "INSERT INTO oauth_connections"
                        " (id, user_id, provider, account_email, access_token_enc,"
                        "  refresh_token_enc, scopes, is_primary,"
                        "  migrated_from_legacy, created_at, updated_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (conn_id, user_id, provider, account_email, encrypt_text(access_token),
                         refresh_enc, _PROVIDER[provider]["scopes"],
                         is_primary, migrated_from_legacy, now, now),
                    )
                except Exception:
                    # A concurrent complete() for the same account raced
                    # this insert into the UNIQUE constraint: fall back
                    # to updating the row that won.
                    conn.execute(
                        "UPDATE oauth_connections SET access_token_enc = ?, refresh_token_enc = ?,"
                        " migrated_from_legacy = ?, updated_at = ?"
                        " WHERE user_id = ? AND provider = ? AND account_email IS ?",
                        (encrypt_text(access_token), refresh_enc,
                         migrated_from_legacy, now, user_id, provider, account_email),
                    )
                    conn_id = conn.execute(
                        "SELECT id FROM oauth_connections WHERE user_id = ? AND provider = ?"
                        " AND account_email IS ?",
                        (user_id, provider, account_email),
                    ).fetchone()["id"]
                if is_primary:
                    # A concurrent complete() could have made two rows
                    # primary; exactly one primary must survive.
                    conn.execute(
                        "UPDATE oauth_connections SET is_primary = 0"
                        " WHERE user_id = ? AND provider = ? AND id != ?",
                        (user_id, provider, conn_id),
                    )
            conn.commit()
        self._invalidate_cache(user_id, provider)
        return conn_id

    def set_primary(self, user_id: str, provider: str, connection_id: str) -> None:
        """Make one connection the primary for its provider (ownership-checked)."""
        row = self._connection_row(user_id, provider, connection_id)
        if row is None:
            raise PermissionError("connection not found")
        with db_module.get_connection() as conn:
            conn.execute(
                "UPDATE oauth_connections SET is_primary = 0 WHERE user_id = ? AND provider = ?",
                (user_id, provider),
            )
            conn.execute(
                "UPDATE oauth_connections SET is_primary = 1, updated_at = ? WHERE id = ?",
                (int(time.time()), connection_id),
            )
            conn.commit()
        self._invalidate_cache(user_id, provider)

    def _delete_user_tokens(self, user_id: str, provider: str, connection_id: Optional[str] = None) -> Optional[dict]:
        """Delete a user's connection row; returns the deleted row (for its flags)."""
        row = self._connection_row(user_id, provider, connection_id)
        if row is None:
            return None
        with db_module.get_connection() as conn:
            conn.execute("DELETE FROM oauth_connections WHERE id = ?", (row["id"],))
            # If the primary is gone, promote the oldest remaining row.
            if row["is_primary"]:
                nxt = conn.execute(
                    "SELECT id FROM oauth_connections WHERE user_id = ? AND provider = ?"
                    " ORDER BY created_at ASC LIMIT 1",
                    (user_id, provider),
                ).fetchone()
                if nxt is not None:
                    conn.execute(
                        "UPDATE oauth_connections SET is_primary = 1 WHERE id = ?",
                        (nxt["id"],),
                    )
            conn.commit()
        self._invalidate_cache(user_id, provider)
        return row

    def _token_for(self, user_id: Optional[str], provider: str, connection_id: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
        if user_id is None:
            return self._tokens.get(provider), self._refresh_tokens.get(provider)
        return self._user_tokens(user_id, provider, connection_id)

    # ------------------------------------------------------------ OAuth state
    def _save_state(self, state: str, user_id: str, provider: str, verifier: str) -> None:
        with db_module.get_connection() as conn:
            conn.execute(
                "INSERT INTO oauth_states (state, user_id, provider, verifier, expires_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (state, user_id, provider, verifier, int(time.time()) + 600),
            )
            conn.commit()

    def _load_state(self, state: str) -> Optional[tuple[str, str, str]]:
        """Consume a DB-backed state (single-use). Returns (user_id, provider, verifier)."""
        with db_module.get_connection() as conn:
            row = conn.execute(
                "SELECT user_id, provider, verifier, expires_at FROM oauth_states WHERE state = ?",
                (state,),
            ).fetchone()
            if row is not None:
                conn.execute("DELETE FROM oauth_states WHERE state = ?", (state,))
                conn.commit()
        if row is None or row["expires_at"] <= int(time.time()):
            return None
        return row["user_id"], row["provider"], row["verifier"]

    # ------------------------------------------------------------- credentials
    def configured(self, provider: str) -> bool:
        _load_env_if_needed()
        has_creds = bool(_oauth_credential(provider, "CLIENT_ID") and _oauth_credential(provider, "CLIENT_SECRET"))
        return provider in _PROVIDER and (has_creds or bool(self._tokens.get(provider)) or bool(self._refresh_tokens.get(provider)))

    def set_credentials(self, provider: str, client_id: str, client_secret: str) -> None:
        """Set OAuth client credentials.

        The client ID is not a secret and lives in process memory (operators
        may still keep it in ``.env``). The client secret is a secret: it is
        stored encrypted (Task 2) and never written to ``.env`` again.
        """
        if provider not in _PROVIDER:
            raise ValueError(f"Unknown provider '{provider}'")
        self._verification_cache = {
            key: value for key, value in self._verification_cache.items() if key[1] != provider
        }
        prefix = f"PRIVAGENT_{provider.upper()}_OAUTH_"
        cid = (client_id or "").strip()
        csec = (client_secret or "").strip()
        os.environ[prefix + "CLIENT_ID"] = cid
        os.environ[prefix + "CLIENT_SECRET"] = csec
        if not self._is_test_client and csec:
            _store_set(prefix + "CLIENT_SECRET", csec)

    def set_tokens(self, provider: str, access_token: str | None = None, refresh_token: str | None = None) -> None:
        """Set legacy global access/refresh tokens, stored encrypted (Task 2).

        Tokens are also mirrored into ``os.environ`` for this process only,
        so existing connector code that reads the environment keeps working;
        they are never written to ``.env`` again.
        """
        if provider not in _PROVIDER:
            raise ValueError(f"Unknown provider '{provider}'")
        self._verification_cache = {
            key: value for key, value in self._verification_cache.items() if key[1] != provider
        }
        if access_token is not None:
            clean_tok = access_token.strip()
            self._tokens[provider] = clean_tok
            prefix = f"PRIVAGENT_{provider.upper()}_ACCESS_TOKEN"
            os.environ[prefix] = clean_tok
            if not self._is_test_client:
                _store_set(prefix, clean_tok)
        if refresh_token is not None:
            clean_ref = refresh_token.strip()
            self._refresh_tokens[provider] = clean_ref
            prefix_ref = f"PRIVAGENT_{provider.upper()}_REFRESH_TOKEN"
            os.environ[prefix_ref] = clean_ref
            if not self._is_test_client:
                _store_set(prefix_ref, clean_ref)

    def set_token(self, provider: str, token: str) -> None:
        self.set_tokens(provider, access_token=token)

    def refresh_access_token(self, provider: str, user_id: Optional[str] = None, connection_id: Optional[str] = None) -> str | None:
        """Exchange refresh_token for a fresh access_token via OAuth token endpoint."""
        if provider not in _PROVIDER:
            return None
        _, refresh_tok = self._token_for(user_id, provider, connection_id)
        if not refresh_tok:
            return None
        # A refresh belongs to the connection being refreshed: resolve it
        # now so the save updates that row (not a new NULL-account row).
        row_id = None
        if user_id is not None:
            row = self._connection_row(user_id, provider, connection_id)
            if row is None:
                return None
            row_id = row["id"]

        prefix = f"PRIVAGENT_{provider.upper()}_OAUTH_"
        client_id = _oauth_credential(provider, "CLIENT_ID")
        client_secret = _oauth_credential(provider, "CLIENT_SECRET")

        token_url = _PROVIDER[provider]["token"]
        data: dict[str, str] = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_tok,
        }
        if client_id:
            data["client_id"] = client_id
        if client_secret:
            data["client_secret"] = client_secret

        try:
            resp = self._client.post(token_url, data=data, headers={"Accept": "application/json"})
            if resp.status_code == 200:
                payload = resp.json()
                new_token = payload.get("access_token")
                if new_token:
                    if user_id is None:
                        # Save a rotated refresh token too (GitHub
                        # rotation is total; Microsoft issues a new one
                        # every refresh). set_tokens no-ops on None.
                        self.set_tokens(provider, access_token=new_token,
                                        refresh_token=payload.get("refresh_token"))
                    else:
                        self._save_user_tokens(
                            user_id, provider, new_token,
                            refresh_token=payload.get("refresh_token"),
                            connection_id=row_id,
                        )
                    return new_token
        except Exception:
            pass
        return None

    def verify_connection(self, provider: str, force_check: bool = False, user_id: Optional[str] = None, connection_id: Optional[str] = None) -> dict[str, Any]:
        """Test the real API connection and return account info or failure reason with caching."""
        now = time.time()
        cache_key = (user_id, provider, connection_id)
        if not force_check and cache_key in self._verification_cache:
            cached_at, cached_res = self._verification_cache[cache_key]
            if now - cached_at < 15.0:
                return dict(cached_res)

        res = self._compute_verify_connection(provider, user_id=user_id, connection_id=connection_id)
        self._verification_cache[cache_key] = (now, res)
        return dict(res)

    def _compute_verify_connection(self, provider: str, user_id: Optional[str] = None, connection_id: Optional[str] = None) -> dict[str, Any]:
        access_token, refresh_token = self._token_for(user_id, provider, connection_id)
        has_creds = bool(_oauth_credential(provider, "CLIENT_ID") and _oauth_credential(provider, "CLIENT_SECRET"))
        is_cfg = provider in _PROVIDER and (has_creds or access_token is not None or refresh_token is not None)
        if provider == "google":
            conn = self.connector("gmail", user_id=user_id, connection_id=connection_id)
            if not conn.configured:
                # Try refresh if we have a refresh token
                new_tok = self.refresh_access_token(provider, user_id=user_id, connection_id=connection_id)
                if not new_tok:
                    return {"configured": is_cfg, "connected": False, "account": None, "error": "Google is not connected"}
                conn = self.connector("gmail", user_id=user_id, connection_id=connection_id)
            try:
                prof = conn.get_profile()
                email = prof.get("emailAddress")
                self._remember_account(user_id, provider, email, connection_id)
                return {"configured": True, "connected": True, "account": email, "error": None}
            except Exception as exc:
                err_str = str(exc)
                if "401" in err_str:
                    err_msg = "Access token expired. Please provide a fresh token or refresh token."
                else:
                    err_msg = err_str
                return {"configured": True, "connected": False, "account": None, "error": err_msg}
        elif provider == "microsoft":
            conn = self.connector("outlook", user_id=user_id, connection_id=connection_id)
            if not conn.configured:
                new_tok = self.refresh_access_token(provider, user_id=user_id, connection_id=connection_id)
                if not new_tok:
                    return {"configured": is_cfg, "connected": False, "account": None, "error": "Outlook is not connected"}
                conn = self.connector("outlook", user_id=user_id, connection_id=connection_id)
            try:
                prof = conn.get_profile()
                email = prof.get("mail") or prof.get("userPrincipalName")
                self._remember_account(user_id, provider, email, connection_id)
                return {"configured": True, "connected": True, "account": email, "error": None}
            except Exception as exc:
                return {"configured": True, "connected": False, "account": None, "error": str(exc)}
        elif provider == "slack":
            if access_token is None:
                new_tok = self.refresh_access_token(provider, user_id=user_id, connection_id=connection_id)
                if not new_tok:
                    return {"configured": is_cfg, "connected": False, "account": None, "error": "Slack is not connected"}
                access_token = new_tok
            for _attempt in range(2):
                try:
                    resp = self._client.post(
                        "https://slack.com/api/auth.test", headers={"Authorization": f"Bearer {access_token}"}
                    )
                    payload = resp.json() if resp.status_code == 200 else {}
                    if payload.get("ok"):
                        account = payload.get("user") or payload.get("team")
                        self._remember_account(user_id, provider, account, connection_id)
                        return {"configured": True, "connected": True, "account": account, "error": None}
                    err = payload.get("error") or "Slack token rejected"
                except Exception as exc:
                    err = str(exc)
                # A dead token with a valid refresh token self-heals: one
                # refresh, then one retry. Without rotation there is no
                # refresh token and this is a no-op.
                if _attempt == 0 and refresh_token is not None:
                    new_tok = self.refresh_access_token(provider, user_id=user_id, connection_id=connection_id)
                    if new_tok:
                        access_token = new_tok
                        continue
                return {"configured": True, "connected": False, "account": None, "error": err}
        elif provider == "github":
            if access_token is None:
                return {"configured": is_cfg, "connected": False, "account": None, "error": "GitHub is not connected"}
            for _attempt in range(2):
                try:
                    resp = self._client.get(
                        "https://api.github.com/user", headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
                    )
                    if resp.status_code == 200:
                        # Users can edit granted scopes down after authorizing:
                        # the X-OAuth-Scopes header shows what this token
                        # actually has. No `repo` means private-repo reads
                        # will fail — say so honestly instead of "Connected".
                        granted = resp.headers.get("X-OAuth-Scopes", "")
                        if granted and "repo" not in [s.strip() for s in granted.split(",")]:
                            return {"configured": True, "connected": False, "account": None,
                                    "error": f"GitHub authorization is missing the repo scope (granted: {granted})"}
                        login = resp.json().get("login")
                        self._remember_account(user_id, provider, login, connection_id)
                        return {"configured": True, "connected": True, "account": login, "error": None}
                    err = "GitHub token rejected" if resp.status_code == 401 else f"GitHub API returned HTTP {resp.status_code}"
                except Exception as exc:
                    err = str(exc)
                    resp = None
                # An expiring GitHub token (8h) with a valid refresh token
                # self-heals: one refresh, then one retry.
                if _attempt == 0 and resp is not None and resp.status_code == 401 and refresh_token is not None:
                    new_tok = self.refresh_access_token(provider, user_id=user_id, connection_id=connection_id)
                    if new_tok:
                        access_token = new_tok
                        continue
                return {"configured": True, "connected": False, "account": None, "error": err}
        else:
            is_connected = access_token is not None
            return {
                "configured": is_cfg or is_connected,
                "connected": is_connected,
                "account": None,
                "error": None if is_connected else f"{provider.capitalize()} is not connected",
            }

    def _remember_account(self, user_id: Optional[str], provider: str, account: Optional[str], connection_id: Optional[str] = None) -> None:
        """Record the verified account on the user's connection row (best effort).

        Only fills in a row whose account is still unknown. If filling it
        would collide with another row for the same account (a re-connect
        whose profile fetch failed at complete() time), the newer row
        wins: the stale duplicate is deleted and primary is transferred.
        """
        if user_id is None or not account:
            return
        try:
            row = self._connection_row(user_id, provider, connection_id)
            if row is None or row["account_email"]:
                return
            with db_module.get_connection() as conn:
                dup = conn.execute(
                    "SELECT id, is_primary FROM oauth_connections"
                    " WHERE user_id = ? AND provider = ? AND account_email = ?",
                    (user_id, provider, account),
                ).fetchone()
                if dup is not None and dup["id"] != row["id"]:
                    if dup["is_primary"]:
                        conn.execute(
                            "UPDATE oauth_connections SET is_primary = 1 WHERE id = ?",
                            (row["id"],),
                        )
                    conn.execute("DELETE FROM oauth_connections WHERE id = ?", (dup["id"],))
                conn.execute(
                    "UPDATE oauth_connections SET account_email = ?, updated_at = ? WHERE id = ?",
                    (account, int(time.time()), row["id"]),
                )
                conn.commit()
        except Exception:
            pass

    def _fetch_account(self, provider: str, access_token: str) -> Optional[str]:
        """Best-effort live account identity for a just-exchanged token."""
        try:
            if provider == "google":
                prof = GmailConnector(access_token, client=self._client).get_profile()
                return prof.get("emailAddress")
            if provider == "microsoft":
                prof = OutlookConnector(access_token, client=self._client).get_profile()
                return prof.get("mail") or prof.get("userPrincipalName")
            if provider == "slack":
                resp = self._client.post(
                    "https://slack.com/api/auth.test", headers={"Authorization": f"Bearer {access_token}"}
                )
                payload = resp.json() if resp.status_code == 200 else {}
                return payload.get("user") or payload.get("team") if payload.get("ok") else None
            if provider == "github":
                resp = self._client.get(
                    "https://api.github.com/user", headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
                )
                return resp.json().get("login") if resp.status_code == 200 else None
        except Exception:
            return None
        return None

    def status(self, user_id: Optional[str] = None, force_check: bool = False) -> list[dict]:
        results = []
        for name in _PROVIDER:
            if user_id is None:
                info = self.verify_connection(name, force_check=force_check, user_id=None)
                results.append({
                    "provider": name,
                    "configured": info["configured"],
                    "connected": info["connected"],
                    "account": info["account"],
                    "error": info["error"],
                    "connections": [],
                })
                continue
            with db_module.get_connection() as conn:
                rows = conn.execute(
                    "SELECT id, account_email, scopes, is_primary FROM oauth_connections"
                    " WHERE user_id = ? AND provider = ? ORDER BY is_primary DESC, created_at ASC",
                    (user_id, name),
                ).fetchall()
            connections = []
            for row in rows:
                info = self.verify_connection(
                    name, force_check=force_check, user_id=user_id, connection_id=row["id"]
                )
                connections.append({
                    "id": row["id"],
                    "account": info["account"] or row["account_email"],
                    "connected": info["connected"],
                    "error": info["error"],
                    "is_primary": bool(row["is_primary"]),
                    "scopes": row["scopes"],
                })
            primary = next((c for c in connections if c["is_primary"]), connections[0] if connections else None)
            has_creds = bool(_oauth_credential(name, "CLIENT_ID") and _oauth_credential(name, "CLIENT_SECRET"))
            results.append({
                "provider": name,
                "configured": has_creds or bool(connections),
                "connected": any(c["connected"] for c in connections),
                "account": primary["account"] if primary else None,
                "error": primary["error"] if primary else None,
                "connections": connections,
                # Whether operator-global (legacy) tokens exist for this
                # provider — the UI offers to claim them. Existence only;
                # the account is never revealed.
                "legacy_available": name in self._tokens or name in self._refresh_tokens,
            })
        return results

    def _redirect(self, provider: str) -> str:
        origin = os.environ.get("PRIVAGENT_OAUTH_REDIRECT_ORIGIN", "http://localhost:8000").rstrip("/")
        if origin not in ("http://localhost:8000", "http://127.0.0.1:8000"):
            raise ValueError("OAuth redirect origin must be the local backend")
        return f"{origin}/api/connectors/{provider}/callback"

    def start(self, provider: str, user_id: Optional[str] = None) -> str:
        if provider not in _PROVIDER:
            raise ValueError(f"Unknown provider '{provider}'")
        # An authorize URL with an empty client_id is broken: fail honestly
        # instead of sending the user to the provider to be rejected there.
        if not (_oauth_credential(provider, "CLIENT_ID") and _oauth_credential(provider, "CLIENT_SECRET")):
            raise ValueError("OAuth client is not configured")
        spec = _PROVIDER[provider]
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)
        if user_id is None:
            self._pending[state] = (time.monotonic() + 600, provider, verifier)
            self._pending = {key: value for key, value in self._pending.items() if value[0] > time.monotonic()}
        else:
            # Prune abandoned states so the table does not grow unboundedly.
            with db_module.get_connection() as conn:
                conn.execute("DELETE FROM oauth_states WHERE expires_at <= ?", (int(time.time()),))
                conn.commit()
            self._save_state(state, user_id, provider, verifier)
        params = {"client_id": _oauth_credential(provider, "CLIENT_ID"),
                  "redirect_uri": self._redirect(provider), "response_type": "code", "state": state,
                  "scope": spec["scopes"]}
        if provider in _PKCE_PROVIDERS:
            params["code_challenge"] = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            params["code_challenge_method"] = "S256"
        if provider == "google":
            params["access_type"] = "offline"
        return spec["auth"] + "?" + urlencode(params)

    def complete(self, provider: str, code: str, state: str) -> Optional[str]:
        """Exchange the code for tokens. Returns the owning user id (None for the legacy path)."""
        resolved = self._load_state(state)
        if resolved is not None:
            user_id, state_provider, verifier = resolved
            if state_provider != provider or not code:
                raise PermissionError("OAuth state is invalid or expired")
            data = {"client_id": _oauth_credential(provider, "CLIENT_ID"),
                    "client_secret": _oauth_credential(provider, "CLIENT_SECRET"),
                    "code": code, "redirect_uri": self._redirect(provider)}
            if provider in _PKCE_PROVIDERS:
                data.update({"grant_type": "authorization_code", "code_verifier": verifier})
            response = self._client.post(_PROVIDER[provider]["token"], data=data, headers={"Accept": "application/json"})
            if response.status_code != 200:
                raise RuntimeError(f"OAuth token exchange failed with HTTP {response.status_code}")
            payload = response.json()
            token = payload.get("access_token")
            refresh_token = payload.get("refresh_token")
            if not token:
                raise RuntimeError("OAuth provider did not return an access token")
            # Learn the account before saving so a re-connect upserts the
            # right row instead of creating a duplicate unknown one.
            account = self._fetch_account(provider, token)
            self._save_user_tokens(user_id, provider, token, refresh_token, account_email=account)
            self._invalidate_cache(user_id, provider)
            return user_id

        pending = self._pending.pop(state, None)
        if not pending or pending[0] < time.monotonic() or pending[1] != provider or not code:
            raise PermissionError("OAuth state is invalid or expired")
        data = {"client_id": _oauth_credential(provider, "CLIENT_ID"),
                "client_secret": _oauth_credential(provider, "CLIENT_SECRET"),
                "code": code, "redirect_uri": self._redirect(provider)}
        if provider in _PKCE_PROVIDERS:
            data.update({"grant_type": "authorization_code", "code_verifier": pending[2]})
        response = self._client.post(_PROVIDER[provider]["token"], data=data, headers={"Accept": "application/json"})
        if response.status_code != 200:
            raise RuntimeError(f"OAuth token exchange failed with HTTP {response.status_code}")
        token = response.json().get("access_token")
        refresh_token = response.json().get("refresh_token")
        if not token:
            raise RuntimeError("OAuth provider did not return an access token")
        self.set_tokens(provider, access_token=token, refresh_token=refresh_token)
        return None

    def _revoke_at_provider(self, provider: str, access_token: Optional[str]) -> bool:
        """Revoke the token at the provider. Best-effort: False on any failure.

        Microsoft has no user-token revocation endpoint, so it always
        reports False — disconnect still deletes locally.
        """
        if not access_token:
            return False
        try:
            if provider == "google":
                resp = self._client.post(_REVOKE["google"], data={"token": access_token})
                return resp.status_code == 200
            if provider == "slack":
                # Documented method is GET auth.revoke.
                resp = self._client.get(
                    _REVOKE["slack"], headers={"Authorization": f"Bearer {access_token}"}
                )
                return resp.status_code == 200 and bool(resp.json().get("ok"))
            if provider == "github":
                client_id = _oauth_credential(provider, "CLIENT_ID")
                client_secret = _oauth_credential(provider, "CLIENT_SECRET")
                if not client_id or not client_secret:
                    return False
                resp = self._client.request(
                    "DELETE",
                    f"https://api.github.com/applications/{client_id}/token",
                    auth=(client_id, client_secret),
                    json={"access_token": access_token},
                    headers={"Accept": "application/json"},
                )
                return resp.status_code in (204, 404)
            return False
        except Exception:
            return False

    def disconnect(self, provider: str, user_id: Optional[str] = None, connection_id: Optional[str] = None) -> dict[str, bool]:
        """Revoke at the provider, then delete locally. Always deletes locally."""
        if user_id is None and connection_id is not None:
            # The operator has no connection rows: a named connection
            # here is always a mistake, not a silent no-op success.
            raise PermissionError("connection not found")
        if user_id is not None and self._connection_row(user_id, provider, connection_id) is None:
            raise PermissionError("connection not found")
        access_token, _ = self._token_for(user_id, provider, connection_id)
        revoked = self._revoke_at_provider(provider, access_token)
        if user_id is None:
            self._tokens.pop(provider, None)
            self._refresh_tokens.pop(provider, None)
            prefix = f"PRIVAGENT_{provider.upper()}_ACCESS_TOKEN"
            prefix_ref = f"PRIVAGENT_{provider.upper()}_REFRESH_TOKEN"
            os.environ.pop(prefix, None)
            os.environ.pop(prefix_ref, None)
            if not self._is_test_client:
                _store_delete(prefix)
                _store_delete(prefix_ref)
        else:
            deleted = self._delete_user_tokens(user_id, provider, connection_id)
            # A connection claimed from the legacy globals holds the same
            # token the operator path uses — it was just revoked, so the
            # globals now point at a dead token and must be cleared.
            # Token equality (not the migrated_from_legacy flag) is the
            # sound check: the flag reflects provenance at insert time
            # and goes stale if the operator re-auths afterwards.
            if deleted is not None:
                row_token = decrypt_text(deleted["access_token_enc"])
                if row_token is not None and row_token == self._tokens.get(provider):
                    self._tokens.pop(provider, None)
                    self._refresh_tokens.pop(provider, None)
                    prefix = f"PRIVAGENT_{provider.upper()}_ACCESS_TOKEN"
                    prefix_ref = f"PRIVAGENT_{provider.upper()}_REFRESH_TOKEN"
                    os.environ.pop(prefix, None)
                    os.environ.pop(prefix_ref, None)
                    if not self._is_test_client:
                        _store_delete(prefix)
                        _store_delete(prefix_ref)
        self._invalidate_cache(user_id, provider)
        return {"ok": True, "revoked": revoked}

    def migrate_legacy(self, provider: str, user_id: str, account_email: str) -> dict[str, Any]:
        """Claim the operator-global tokens into a user's connection.

        Explicit and ownership-verified, never silent: the caller must
        supply the account email, which is checked against the legacy
        token's live profile. The legacy globals are KEPT afterwards —
        background paths (email missions, connector_runner) have no
        user context and keep working. A provider already claimed by
        another user cannot be claimed again (the route answers 403).
        """
        if provider not in _PROVIDER:
            raise ValueError(f"Unknown provider '{provider}'")
        if not account_email or not account_email.strip():
            raise ValueError("account email is required")
        with db_module.get_connection() as conn:
            claim = conn.execute(
                "SELECT user_id FROM oauth_connections"
                " WHERE provider = ? AND migrated_from_legacy = 1",
                (provider,),
            ).fetchone()
        if claim is not None and claim["user_id"] != user_id:
            raise PermissionError("this provider's legacy connection was already claimed by another account")
        access_token, refresh_token = self._tokens.get(provider), self._refresh_tokens.get(provider)
        if access_token is None and refresh_token is None:
            raise ValueError("no legacy connection exists for this provider")
        if access_token is None:
            # Refresh-only legacy token: refresh first (never destroy
            # the only credential by migrating a bare refresh token).
            access_token = self.refresh_access_token(provider)
            if access_token is None:
                raise RuntimeError("legacy refresh token could not be exchanged")
            _, refresh_token = self._tokens.get(provider), self._refresh_tokens.get(provider)
        account = self._fetch_account(provider, access_token)
        if account is None and refresh_token is not None:
            # An expired-but-refreshable legacy access token: one
            # refresh, then retry the profile fetch.
            new_access = self.refresh_access_token(provider)
            if new_access is not None:
                access_token = new_access
                _, refresh_token = self._tokens.get(provider), self._refresh_tokens.get(provider)
                account = self._fetch_account(provider, access_token)
        if account is None:
            raise RuntimeError("legacy token could not be verified")
        if account.lower() != account_email.strip().lower():
            raise PermissionError("account email does not match the legacy connection")
        conn_id = self._save_user_tokens(
            user_id, provider, access_token, refresh_token,
            account_email=account, migrated_from_legacy=1,
        )
        return {"ok": True, "account": account, "connection_id": conn_id}

    def connector(self, name: str, user_id: Optional[str] = None, connection_id: Optional[str] = None):
        mapping = {"gmail": ("google", GmailConnector), "outlook": ("microsoft", OutlookConnector),
                   "google_calendar": ("google", GoogleCalendarConnector), "google_drive": ("google", GoogleDriveConnector),
                   "slack": ("slack", SlackConnector), "github": ("github", GitHubConnector)}
        if name not in mapping:
            raise ValueError("unknown connector")
        provider, cls = mapping[name]
        access_token, _ = self._token_for(user_id, provider, connection_id)
        return cls(access_token, client=self._client,
                   refresh_hook=lambda: self.refresh_access_token(provider, user_id=user_id, connection_id=connection_id))


oauth_manager = OAuthManager()
