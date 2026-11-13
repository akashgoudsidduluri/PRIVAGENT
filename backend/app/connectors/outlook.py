"""Microsoft Graph mail adapter with the same email capability operations."""
from __future__ import annotations
from urllib.parse import quote
from .base import NativeConnector


class OutlookConnector(NativeConnector):
    origin = "https://graph.microsoft.com"
    service = "Outlook"

    def get_profile(self) -> dict:
        return self._request("GET", "/v1.0/me")

    def search_threads(self, query: str, limit: int = 20) -> list[dict]:
        data = self._request("GET", "/v1.0/me/messages", params={"$search": f'"{query[:200]}"', "$top": min(max(limit, 1), 50)})
        return data.get("value", [])

    def list_messages(self, query: str = "", limit: int = 50, skip_token: str | None = None) -> list[dict]:
        params: dict = {"$top": min(max(limit, 1), 50), "$orderby": "receivedDateTime desc"}
        if query:
            params["$search"] = f'"{query[:200]}"'
        if skip_token:
            params["$skiptoken"] = skip_token
        data = self._request("GET", "/v1.0/me/mailFolders/inbox/messages", params=params)
        return data.get("value", [])

    def get_message(self, message_id: str) -> dict:
        return self._request("GET", f"/v1.0/me/messages/{quote(message_id, safe='')}")

    def get_delta(self, delta_link: str | None = None) -> dict:
        if delta_link:
            path = delta_link
            if path.startswith(self.origin):
                path = path[len(self.origin):]
            return self._request("GET", path)
        return self._request("GET", "/v1.0/me/mailFolders/inbox/messages/delta")

    def trash_message(self, message_id: str) -> dict:
        return self._request("POST", f"/v1.0/me/messages/{quote(message_id, safe='')}/move", json={"destinationId": "deleteditems"})

    def untrash_message(self, message_id: str) -> dict:
        return self._request("POST", f"/v1.0/me/messages/{quote(message_id, safe='')}/move", json={"destinationId": "inbox"})

    def summarize_messages(self, query: str, limit: int = 10) -> list[dict]:
        return [{"id": m.get("id"), "from": m.get("from", {}).get("emailAddress", {}).get("address"),
                 "subject": m.get("subject"), "snippet": m.get("bodyPreview", "")}
                for m in self.search_threads(query, limit)]

    def draft_email(self, to: str, subject: str, body: str) -> dict:
        return self._request("POST", "/v1.0/me/messages", json={"subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": [{"emailAddress": {"address": to}}]})

    def manage_labels(self, message_id: str, add: list[str], remove: list[str]) -> dict:
        message = self._request("GET", f"/v1.0/me/messages/{quote(message_id, safe='')}", params={"$select": "categories"})
        categories = set(message.get("categories") or [])
        categories.update(add)
        categories.difference_update(remove)
        return self._request("PATCH", f"/v1.0/me/messages/{quote(message_id, safe='')}", json={"categories": sorted(categories)})
