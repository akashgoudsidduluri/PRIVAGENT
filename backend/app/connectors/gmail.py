"""Gmail read, draft, and label operations (the send endpoint is intentionally separate)."""
from __future__ import annotations
import base64
from email.message import EmailMessage
from urllib.parse import quote
from .base import NativeConnector


class GmailConnector(NativeConnector):
    origin = "https://gmail.googleapis.com"
    service = "Gmail"

    def search_threads(self, query: str, limit: int = 20) -> list[dict]:
        data = self._request("GET", "/gmail/v1/users/me/threads", params={"q": query[:300], "maxResults": min(max(limit, 1), 100)})
        return data.get("threads", [])

    def get_thread(self, thread_id: str) -> dict:
        return self._request("GET", f"/gmail/v1/users/me/threads/{quote(thread_id, safe='')}", params={"format": "metadata"})

    def summarize_messages(self, query: str, limit: int = 10) -> list[dict]:
        summaries = []
        for item in self.search_threads(query, limit):
            thread = self.get_thread(item["id"])
            for message in thread.get("messages", [])[:limit]:
                headers = {h.get("name", "").lower(): h.get("value", "") for h in message.get("payload", {}).get("headers", [])}
                summaries.append({"thread_id": item["id"], "from": headers.get("from", ""),
                                  "subject": headers.get("subject", ""), "snippet": message.get("snippet", "")})
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
