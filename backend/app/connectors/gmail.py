"""Gmail read, draft, and label operations (the send endpoint is intentionally separate)."""
from __future__ import annotations
import base64
from email.message import EmailMessage
from urllib.parse import quote
from .base import NativeConnector


class GmailConnector(NativeConnector):
    origin = "https://gmail.googleapis.com"
    service = "Gmail"

    def get_profile(self) -> dict:
        return self._request("GET", "/gmail/v1/users/me/profile")

    def search_threads(self, query: str, limit: int = 20) -> list[dict]:
        data = self._request("GET", "/gmail/v1/users/me/threads", params={"q": (query or "")[:300], "maxResults": min(max(limit, 1), 100)})
        return data.get("threads", [])

    def get_thread(self, thread_id: str, format: str = "metadata") -> dict:
        # Callers that need bodies/snippets use format="full": the metadata
        # format returns headers only, and snippets may come back empty.
        return self._request("GET", f"/gmail/v1/users/me/threads/{quote(thread_id, safe='')}", params={"format": format})

    def list_messages(self, query: str = "", limit: int = 50, page_token: str | None = None) -> dict:
        params: dict = {"maxResults": min(max(limit, 1), 100)}
        if query:
            params["q"] = (query or "")[:300]
        if page_token:
            params["pageToken"] = page_token
        return self._request("GET", "/gmail/v1/users/me/messages", params=params)

    def get_message(self, message_id: str, format: str = "full") -> dict:
        return self._request("GET", f"/gmail/v1/users/me/messages/{quote(message_id, safe='')}", params={"format": format})

    def list_history(self, start_history_id: str, max_results: int = 100) -> dict:
        return self._request("GET", "/gmail/v1/users/me/history", params={"startHistoryId": start_history_id, "maxResults": min(max(max_results, 1), 500)})

    def trash_message(self, message_id: str) -> dict:
        return self._request("POST", f"/gmail/v1/users/me/messages/{quote(message_id, safe='')}/trash")

    def untrash_message(self, message_id: str) -> dict:
        return self._request("POST", f"/gmail/v1/users/me/messages/{quote(message_id, safe='')}/untrash")

    def summarize_messages(self, query: str, limit: int = 10) -> list[dict]:
        # One list call (its items already carry the snippet) plus one
        # metadata fetch per message for From/Subject — not one thread
        # fetch per thread. The old search_threads + get_thread path was
        # N+1 on threads, returned whole threads, and silently stopped at
        # the first page (nextPageToken discarded).
        summaries = []
        listing = self.list_messages(query, limit)
        for item in listing.get("messages", [])[:limit]:
            msg = self.get_message(item["id"], format="metadata")
            headers = {h.get("name", "").lower(): h.get("value", "") for h in msg.get("payload", {}).get("headers", [])}
            summaries.append({"thread_id": item.get("threadId", ""),
                              "from": headers.get("from", ""),
                              "subject": headers.get("subject", ""),
                              "snippet": item.get("snippet", "")})
        return summaries[:limit]

    def draft_email(self, to: str, subject: str, body: str) -> dict:
        mail = EmailMessage()
        mail["To"] = to
        mail["Subject"] = subject
        mail.set_content(body)
        raw = base64.urlsafe_b64encode(mail.as_bytes()).decode().rstrip("=")
        return self._request("POST", "/gmail/v1/users/me/drafts", json={"message": {"raw": raw}})

    def manage_labels(self, message_id: str, add: list[str], remove: list[str]) -> dict:
        return self._request("POST", f"/gmail/v1/users/me/messages/{quote(message_id, safe='')}/modify",
                             json={"addLabelIds": add, "removeLabelIds": remove})
