"""Slack conversation reads and explicit post operations."""
from __future__ import annotations
from .base import NativeConnector, ConnectorError


class SlackConnector(NativeConnector):
    origin = "https://slack.com"
    service = "Slack"

    def _slack(self, method: str, path: str, **kwargs) -> dict:
        data = self._request(method, path, **kwargs)
        if not data.get("ok"):
            raise ConnectorError(f"Slack API rejected {path.rsplit('/', 1)[-1]}")
        return data

    def query_channels(self, limit: int = 100) -> list[dict]:
        return self._slack("GET", "/api/conversations.list", params={"limit": min(max(limit, 1), 200)}).get("channels", [])

    def summarize_discussions(self, channel: str, limit: int = 30) -> list[dict]:
        messages = self._slack("GET", "/api/conversations.history",
                               params={"channel": channel, "limit": min(max(limit, 1), 100)}).get("messages", [])
        return [{"user": m.get("user"), "text": m.get("text", ""), "ts": m.get("ts")} for m in messages]

    def post_notification(self, channel: str, text: str) -> dict:
        return self._slack("POST", "/api/chat.postMessage", json={"channel": channel, "text": text})
