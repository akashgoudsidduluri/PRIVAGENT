"""Local OAuth authorization state and access tokens; never persisted to SQLite."""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from . import GmailConnector, OutlookConnector, GoogleCalendarConnector, GoogleDriveConnector, SlackConnector, GitHubConnector

_PROVIDER = {
    "google": {"auth": "https://accounts.google.com/o/oauth2/v2/auth", "token": "https://oauth2.googleapis.com/token",
               "scopes": "https://www.googleapis.com/auth/gmail.modify https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/drive.file"},
    "microsoft": {"auth": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
                  "token": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
                  "scopes": "offline_access Mail.ReadWrite Calendars.ReadWrite"},
    "slack": {"auth": "https://slack.com/oauth/v2/authorize", "token": "https://slack.com/api/oauth.v2.access",
              "scopes": "channels:read,channels:history,groups:history,chat:write"},
    "github": {"auth": "https://github.com/login/oauth/authorize", "token": "https://github.com/login/oauth/access_token",
               "scopes": "repo"},
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


def _update_env_file(key: str, value: str) -> None:
    """Persist key=value into backend/.env so it survives restarts, skipping during test runs."""
    import sys
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("PYTEST_VERSION") or "pytest" in sys.modules:
        return
    candidate_dirs = [
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        os.getcwd(),
        os.path.join(os.getcwd(), "backend"),
    ]
    for d in candidate_dirs:
        env_file = os.path.join(d, ".env")
        if os.path.exists(env_file):
            try:
                lines: list[str] = []
                found = False
                with open(env_file, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.startswith(f"{key}="):
                            lines.append(f"{key}={value}\n")
                            found = True
                        else:
                            lines.append(line)
                if not found:
                    if lines and not lines[-1].endswith("\n"):
                        lines[-1] += "\n"
                    lines.append(f"{key}={value}\n")
                with open(env_file, "w", encoding="utf-8") as f:
                    f.writelines(lines)
            except Exception:
                pass


class OAuthManager:
    def __init__(self, client: httpx.Client | None = None, auto_hydrate: bool | None = None):
        self._client = client or httpx.Client(timeout=12, follow_redirects=False)
        self._pending: dict[str, tuple[float, str, str]] = {}
        self._tokens: dict[str, str] = {}
        self._refresh_tokens: dict[str, str] = {}
        self._verification_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._is_test_client = client is not None
        # Hydrate any previously stored access tokens and refresh tokens from env
        # Only auto-hydrate for live default client, not in isolated test clients
        should_hydrate = (client is None) if auto_hydrate is None else auto_hydrate
        if should_hydrate:
            _load_env_if_needed()
            for name in _PROVIDER:
                env_tok = os.environ.get(f"PRIVAGENT_{name.upper()}_ACCESS_TOKEN")
                if env_tok:
                    self._tokens[name] = env_tok.strip()
                env_ref = os.environ.get(f"PRIVAGENT_{name.upper()}_REFRESH_TOKEN")
                if env_ref:
                    self._refresh_tokens[name] = env_ref.strip()

    def configured(self, provider: str) -> bool:
        _load_env_if_needed()
        prefix = f"PRIVAGENT_{provider.upper()}_OAUTH_"
        has_creds = bool(os.environ.get(prefix + "CLIENT_ID") and os.environ.get(prefix + "CLIENT_SECRET"))
        return provider in _PROVIDER and (has_creds or bool(self._tokens.get(provider)) or bool(self._refresh_tokens.get(provider)))

    def set_credentials(self, provider: str, client_id: str, client_secret: str) -> None:
        """Set OAuth client credentials in process memory and persist to .env."""
        if provider not in _PROVIDER:
            raise ValueError(f"Unknown provider '{provider}'")
        self._verification_cache.pop(provider, None)
        prefix = f"PRIVAGENT_{provider.upper()}_OAUTH_"
        cid = (client_id or "").strip()
        csec = (client_secret or "").strip()
        os.environ[prefix + "CLIENT_ID"] = cid
        os.environ[prefix + "CLIENT_SECRET"] = csec
        if not self._is_test_client:
            _update_env_file(prefix + "CLIENT_ID", cid)
            _update_env_file(prefix + "CLIENT_SECRET", csec)

    def set_tokens(self, provider: str, access_token: str | None = None, refresh_token: str | None = None) -> None:
        """Set access token and/or refresh token and persist so restarts don't lose them."""
        if provider not in _PROVIDER:
            raise ValueError(f"Unknown provider '{provider}'")
        self._verification_cache.pop(provider, None)
        if access_token is not None:
            clean_tok = access_token.strip()
            self._tokens[provider] = clean_tok
            prefix = f"PRIVAGENT_{provider.upper()}_ACCESS_TOKEN"
            os.environ[prefix] = clean_tok
            if not self._is_test_client:
                _update_env_file(prefix, clean_tok)
        if refresh_token is not None:
            clean_ref = refresh_token.strip()
            self._refresh_tokens[provider] = clean_ref
            prefix_ref = f"PRIVAGENT_{provider.upper()}_REFRESH_TOKEN"
            os.environ[prefix_ref] = clean_ref
            if not self._is_test_client:
                _update_env_file(prefix_ref, clean_ref)

    def set_token(self, provider: str, token: str) -> None:
        self.set_tokens(provider, access_token=token)

    def refresh_access_token(self, provider: str) -> str | None:
        """Exchange refresh_token for a fresh access_token via OAuth token endpoint."""
        if provider not in _PROVIDER:
            return None
        refresh_tok = self._refresh_tokens.get(provider)
        if not refresh_tok:
            return None

        prefix = f"PRIVAGENT_{provider.upper()}_OAUTH_"
        client_id = os.environ.get(prefix + "CLIENT_ID", "")
        client_secret = os.environ.get(prefix + "CLIENT_SECRET", "")

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
                    self.set_tokens(provider, access_token=new_token)
                    return new_token
        except Exception:
            pass
        return None

    def verify_connection(self, provider: str, force_check: bool = False) -> dict[str, Any]:
        """Test the real API connection and return account info or failure reason with caching."""
        now = time.time()
        if not force_check and provider in self._verification_cache:
            cached_at, cached_res = self._verification_cache[provider]
            if now - cached_at < 15.0:
                return dict(cached_res)

        res = self._compute_verify_connection(provider)
        self._verification_cache[provider] = (now, res)
        return dict(res)

    def _compute_verify_connection(self, provider: str) -> dict[str, Any]:
        is_cfg = self.configured(provider)
        if provider == "google":
            conn = self.connector("gmail")
            if not conn.configured:
                # Try refresh if we have a refresh token
                new_tok = self.refresh_access_token(provider)
                if not new_tok:
                    return {"configured": is_cfg, "connected": False, "account": None, "error": "Google is not connected"}
                conn = self.connector("gmail")
            try:
                prof = conn.get_profile()
                email = prof.get("emailAddress")
                return {"configured": True, "connected": True, "account": email, "error": None}
            except Exception as exc:
                err_str = str(exc)
                if "401" in err_str:
                    err_msg = "Access token expired. Please provide a fresh token or refresh token."
                else:
                    err_msg = err_str
                return {"configured": True, "connected": False, "account": None, "error": err_msg}
        elif provider == "microsoft":
            conn = self.connector("outlook")
            if not conn.configured:
                new_tok = self.refresh_access_token(provider)
                if not new_tok:
                    return {"configured": is_cfg, "connected": False, "account": None, "error": "Outlook is not connected"}
                conn = self.connector("outlook")
            try:
                prof = conn.get_profile()
                email = prof.get("mail") or prof.get("userPrincipalName")
                return {"configured": True, "connected": True, "account": email, "error": None}
            except Exception as exc:
                return {"configured": True, "connected": False, "account": None, "error": str(exc)}
        else:
            is_connected = bool(self._tokens.get(provider))
            return {
                "configured": is_cfg or is_connected,
                "connected": is_connected,
                "account": None,
                "error": None if is_connected else f"{provider.capitalize()} is not connected",
            }

    def status(self) -> list[dict]:
        results = []
        for name in _PROVIDER:
            info = self.verify_connection(name)
            results.append({
                "provider": name,
                "configured": info["configured"],
                "connected": info["connected"],
                "account": info["account"],
                "error": info["error"],
            })
        return results

    def _redirect(self, provider: str) -> str:
        origin = os.environ.get("PRIVAGENT_OAUTH_REDIRECT_ORIGIN", "http://localhost:8000").rstrip("/")
        if origin not in ("http://localhost:8000", "http://127.0.0.1:8000"):
            raise ValueError("OAuth redirect origin must be the local backend")
        return f"{origin}/api/connectors/{provider}/callback"

    def start(self, provider: str) -> str:
        if not self.configured(provider):
            raise ValueError("OAuth client is not configured")
        spec = _PROVIDER[provider]
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)
        self._pending[state] = (time.monotonic() + 600, provider, verifier)
        self._pending = {key: value for key, value in self._pending.items() if value[0] > time.monotonic()}
        params = {"client_id": os.environ[f"PRIVAGENT_{provider.upper()}_OAUTH_CLIENT_ID"],
                  "redirect_uri": self._redirect(provider), "response_type": "code", "state": state,
                  "scope": spec["scopes"]}
        if provider in ("google", "microsoft"):
            params["code_challenge"] = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            params["code_challenge_method"] = "S256"
        if provider == "google":
            params["access_type"] = "offline"
        return spec["auth"] + "?" + urlencode(params)

    def complete(self, provider: str, code: str, state: str) -> None:
        pending = self._pending.pop(state, None)
        if not pending or pending[0] < time.monotonic() or pending[1] != provider or not code:
            raise PermissionError("OAuth state is invalid or expired")
        data = {"client_id": os.environ[f"PRIVAGENT_{provider.upper()}_OAUTH_CLIENT_ID"],
                "client_secret": os.environ[f"PRIVAGENT_{provider.upper()}_OAUTH_CLIENT_SECRET"],
                "code": code, "redirect_uri": self._redirect(provider)}
        if provider in ("google", "microsoft"):
            data.update({"grant_type": "authorization_code", "code_verifier": pending[2]})
        response = self._client.post(_PROVIDER[provider]["token"], data=data, headers={"Accept": "application/json"})
        if response.status_code != 200:
            raise RuntimeError(f"OAuth token exchange failed with HTTP {response.status_code}")
        token = response.json().get("access_token")
        refresh_token = response.json().get("refresh_token")
        if not token:
            raise RuntimeError("OAuth provider did not return an access token")
        self.set_tokens(provider, access_token=token, refresh_token=refresh_token)

    def disconnect(self, provider: str) -> None:
        self._tokens.pop(provider, None)
        self._refresh_tokens.pop(provider, None)
        self._verification_cache.pop(provider, None)
        prefix = f"PRIVAGENT_{provider.upper()}_ACCESS_TOKEN"
        prefix_ref = f"PRIVAGENT_{provider.upper()}_REFRESH_TOKEN"
        os.environ.pop(prefix, None)
        os.environ.pop(prefix_ref, None)
        if not self._is_test_client:
            _update_env_file(prefix, "")
            _update_env_file(prefix_ref, "")

    def connector(self, name: str):
        mapping = {"gmail": ("google", GmailConnector), "outlook": ("microsoft", OutlookConnector),
                   "google_calendar": ("google", GoogleCalendarConnector), "google_drive": ("google", GoogleDriveConnector),
                   "slack": ("slack", SlackConnector), "github": ("github", GitHubConnector)}
        if name not in mapping:
            raise ValueError("unknown connector")
        provider, cls = mapping[name]
        return cls(self._tokens.get(provider), client=self._client, refresh_hook=lambda: self.refresh_access_token(provider))


oauth_manager = OAuthManager()
