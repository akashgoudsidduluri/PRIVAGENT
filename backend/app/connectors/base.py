"""Fixed-origin native API transport. Credentials are memory-only and never logged."""
from __future__ import annotations

import httpx
from typing import Any


class ConnectorError(RuntimeError):
    pass


class NativeConnector:
    origin = ""
    service = ""

    def __init__(self, token: str | None = None, *, client: httpx.Client | None = None):
        self._token = token
        self._client = client or httpx.Client(timeout=12, follow_redirects=False)

    @property
    def configured(self) -> bool:
        return bool(self._token)

    def _request(self, method: str, path: str, *, params: dict | None = None,
                 json: Any = None, content: bytes | None = None, headers: dict | None = None) -> Any:
        if not self._token:
            raise ConnectorError(f"{self.service} is not connected")
        if not path.startswith("/") or path.startswith("//") or ".." in path:
            raise ConnectorError("unsafe API path")
        response = self._client.request(method, self.origin + path, params=params, json=json, content=content,
                                        headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json",
                                                 **(headers or {})})
        if response.status_code < 200 or response.status_code >= 300:
            # Provider error bodies can echo account data or tokens.
            raise ConnectorError(f"{self.service} API returned HTTP {response.status_code}")
        if not response.content:
            return {}
        if response.headers.get("content-type", "").startswith("application/json"):
            return response.json()
        return response.content

    def close(self) -> None:
        self._client.close()
