"""Local OAuth authorization state and access tokens; never persisted to SQLite."""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
import time
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


class OAuthManager:
    def __init__(self, client: httpx.Client | None = None):
        self._client = client or httpx.Client(timeout=12, follow_redirects=False)
        self._pending: dict[str, tuple[float, str, str]] = {}
        self._tokens: dict[str, str] = {}

    def configured(self, provider: str) -> bool:
        prefix = f"PRIVAGENT_{provider.upper()}_OAUTH_"
        return provider in _PROVIDER and bool(os.environ.get(prefix + "CLIENT_ID") and os.environ.get(prefix + "CLIENT_SECRET"))

    def status(self) -> list[dict]:
        return [{"provider": name, "configured": self.configured(name), "connected": name in self._tokens}
                for name in _PROVIDER]

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
        if not token:
            raise RuntimeError("OAuth provider did not return an access token")
        self._tokens[provider] = token

    def disconnect(self, provider: str) -> None:
        self._tokens.pop(provider, None)

    def connector(self, name: str):
        mapping = {"gmail": ("google", GmailConnector), "outlook": ("microsoft", OutlookConnector),
                   "google_calendar": ("google", GoogleCalendarConnector), "google_drive": ("google", GoogleDriveConnector),
                   "slack": ("slack", SlackConnector), "github": ("github", GitHubConnector)}
        if name not in mapping:
            raise ValueError("unknown connector")
        provider, cls = mapping[name]
        return cls(self._tokens.get(provider))


oauth_manager = OAuthManager()
