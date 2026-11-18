"""Fixed-origin native API transport. Credentials are memory-only and never logged."""
from __future__ import annotations

import httpx
from typing import Any
from urllib.parse import urlparse


class ConnectorError(RuntimeError):
    pass


# Hosts a 3xx redirect may be followed to. Drive export/alt=media 302 to
# a signed googleusercontent.com download URL; the redirect URL itself
# carries the authorisation, so no Bearer token is ever forwarded to it.
_REDIRECT_HOSTS = (".googleusercontent.com",)


class NativeConnector:
    origin = ""
    service = ""

    def __init__(self, token: str | None = None, *, client: httpx.Client | None = None, refresh_hook: Any = None):
        self._token = token
        self._client = client or httpx.Client(timeout=12, follow_redirects=False)
        self._refresh_hook = refresh_hook

    @property
    def configured(self) -> bool:
        return bool(self._token)

    def _headers(self, headers: dict | None = None) -> dict:
        # Caller headers first: the Authorization/Accept pair always wins,
        # so a caller can never (accidentally or otherwise) swap the token.
        return {**(headers or {}), "Authorization": f"Bearer {self._token}",
                "Accept": "application/json"}

    def _request(self, method: str, path: str, *, params: dict | None = None,
                 json: Any = None, content: bytes | None = None, headers: dict | None = None) -> Any:
        if not self._token:
            raise ConnectorError(f"{self.service} is not connected")
        if not path.startswith("/") or path.startswith("//") or ".." in path or "?" in path or "#" in path:
            raise ConnectorError("unsafe API path")
        response = self._client.request(method, self.origin + path, params=params, json=json, content=content,
                                        headers=self._headers(headers))
        if response.status_code == 401 and self._refresh_hook:
            new_token = self._refresh_hook()
            if not new_token or new_token == self._token:
                raise ConnectorError(f"{self.service} authentication failed: token refresh did not return a new token")
            self._token = new_token
            response = self._client.request(method, self.origin + path, params=params, json=json, content=content,
                                            headers=self._headers(headers))
            if response.status_code == 401:
                raise ConnectorError(f"{self.service} API returned HTTP 401 after token refresh")
        if response.status_code in (301, 302, 303, 307, 308):
            response = self._follow_redirect(method, response)
        if response.status_code < 200 or response.status_code >= 300:
            # Provider error bodies can echo account data or tokens.
            raise ConnectorError(f"{self.service} API returned HTTP {response.status_code}")
        if not response.content:
            return {}
        if response.headers.get("content-type", "").startswith("application/json"):
            return response.json()
        return response.content

    def _follow_redirect(self, method: str, response: httpx.Response) -> httpx.Response:
        """Follow ONE redirect to a trusted download host, without the token.

        Drive export/alt=media 302 to a signed googleusercontent.com URL.
        Anything else (another host, no Location, non-GET, a second
        redirect) fails honestly instead of silently returning nothing.
        """
        if method != "GET":
            raise ConnectorError(f"{self.service} redirect refused for {method} request")
        location = response.headers.get("location", "")
        if not location:
            raise ConnectorError(f"{self.service} redirect without a Location header")
        parsed = urlparse(location)
        if parsed.scheme != "https":
            raise ConnectorError(f"{self.service} redirect to unsupported scheme")
        host = (parsed.hostname or "").lower()
        origin_host = (urlparse(self.origin).hostname or "").lower()
        if host != origin_host and not host.endswith(_REDIRECT_HOSTS):
            raise ConnectorError(f"{self.service} redirect to untrusted host")
        # No Authorization header: the signed URL carries its own auth,
        # and the Bearer token must never leave the connector's origin.
        followed = self._client.request("GET", location)
        if followed.status_code in (301, 302, 303, 307, 308):
            raise ConnectorError(f"{self.service} refused second redirect")
        return followed

    def close(self) -> None:
        self._client.close()
