"""Controlled live-protocol test mailbox adhering strictly to Gmail and MS Graph API schemas."""
from __future__ import annotations

import base64
import copy
import json
import time
from email.message import EmailMessage
from typing import Any, Optional
from urllib.parse import parse_qs, quote, urlsplit

import httpx

from .models import EmailItem

# Representative 10 Acceptance Test Scenarios
TEST_DATASET: list[dict[str, Any]] = [
    {
        "id": "msg_01_promo",
        "thread_id": "thread_01_promo",
        "sender": "marketing@store.amazon.in",
        "recipient": "user@example.com",
        "subject": "Mega Electronics Sale: Up to 50% Off on Laptops & Smart TVs",
        "snippet": "Don't miss the biggest sale of the season. Special discounts on Apple, Samsung, and Sony. Shop now before stock ends!",
        "body_text": "Exclusive festive discounts! Unsubscribe here to stop receiving marketing emails. Terms and conditions apply.",
        "received_offset_hours": 3.0,
        "is_unread": True,
        "is_replied": False,
        "expected_category": "unnecessary",
        "expected_subcategory": "promotional",
        "eligible_for_cleanup": True,
        "protected": False,
    },
    {
        "id": "msg_02_newsletter",
        "thread_id": "thread_02_newsletter",
        "sender": "newsletter@technewsweekly.substack.com",
        "recipient": "user@example.com",
        "subject": "Tech Weekly Digest #142: Emerging Trends in AI Systems",
        "snippet": "In this issue: Exploring model architectures, runtime schedulers, and edge device efficiency. Click to view the full edition online.",
        "body_text": "Weekly curated newsletter. You are receiving this because you subscribed. To unsubscribe, click manage preferences.",
        "received_offset_hours": 6.0,
        "is_unread": True,
        "is_replied": False,
        "expected_category": "unnecessary",
        "expected_subcategory": "newsletter",
        "eligible_for_cleanup": True,
        "protected": False,
    },
    {
        "id": "msg_03_notification",
        "thread_id": "thread_03_notification",
        "sender": "gitlab-notifications@gitlab.com",
        "recipient": "user@example.com",
        "subject": "Pipeline #89423 passed for branch feat/auth-gate",
        "snippet": "GitLab CI/CD Pipeline #89423 passed successfully in 3 minutes 42 seconds for commit a4b8c9d by dev-bot.",
        "body_text": "Automated build notification. All 42 tests passed. No action required.",
        "received_offset_hours": 12.0,
        "is_unread": True,
        "is_replied": False,
        "expected_category": "unnecessary",
        "expected_subcategory": "notification",
        "eligible_for_cleanup": True,
        "protected": False,
    },
    {
        "id": "msg_04_internship",
        "thread_id": "thread_04_internship",
        "sender": "university-recruiting@google.com",
        "recipient": "user@example.com",
        "subject": "Google SWE Summer Internship 2027 — Application Deadline Nov 20",
        "snippet": "Dear Applicant, Thank you for your interest in the Google Software Engineering Internship. Please submit your project portfolio before the deadline: Friday, Nov 20 at 5 PM.",
        "body_text": "Submit your academic transcript and updated resume before the portal closes. Contact the recruitment team for questions.",
        "received_offset_hours": 48.0,
        "is_unread": True,
        "is_replied": False,
        "expected_category": "important",
        "expected_subcategory": "recruitment",
        "eligible_for_cleanup": False,
        "protected": True,
        "expected_neglected": True,
    },
    {
        "id": "msg_05_interview",
        "thread_id": "thread_05_interview",
        "sender": "talent@microsoft.com",
        "recipient": "user@example.com",
        "subject": "Microsoft Technical Interview Invitation — Please Confirm Availability",
        "snippet": "We were impressed with your technical assessment. We would like to schedule your virtual interview round. Please reply to confirm your preferred slot for tomorrow, 2:00 PM.",
        "body_text": "Reply to this message with your availability. Your interview link will be issued upon confirmation.",
        "received_offset_hours": 24.0,
        "is_unread": True,
        "is_replied": False,
        "expected_category": "important",
        "expected_subcategory": "interview",
        "eligible_for_cleanup": False,
        "protected": True,
        "expected_neglected": True,
    },
    {
        "id": "msg_06_university",
        "thread_id": "thread_06_university",
        "sender": "registrar@university.edu",
        "recipient": "user@example.com",
        "subject": "Dean's Office: End-Semester Examination Registration Closes This Friday",
        "snippet": "Notice to all students: The portal for final examination seat allocation will close promptly on Friday, 5:00 PM. Failure to register will result in late fees.",
        "body_text": "Official academic circular from the Academic Registrar. Please verify your enrolled courses on the student portal before Friday.",
        "received_offset_hours": 18.0,
        "is_unread": True,
        "is_replied": False,
        "expected_category": "important",
        "expected_subcategory": "education",
        "eligible_for_cleanup": False,
        "protected": True,
        "expected_neglected": True,
    },
    {
        "id": "msg_07_personal",
        "thread_id": "thread_07_personal",
        "sender": "dr.mentor@research-institute.org",
        "recipient": "user@example.com",
        "subject": "Urgent: Project handover notes and review needed by tomorrow",
        "snippet": "Hi, I have reviewed your draft on the telemetry layer. Could you please check section 4 and reply with your updated notes by tomorrow morning?",
        "body_text": "Let me know your thoughts on the architecture decisions before our meeting.",
        "received_offset_hours": 14.0,
        "is_unread": True,
        "is_replied": False,
        "expected_category": "important",
        "expected_subcategory": "personal",
        "eligible_for_cleanup": False,
        "protected": True,
        "expected_neglected": True,
    },
    {
        "id": "msg_08_banking",
        "thread_id": "thread_08_banking",
        "sender": "alerts@hdfcbank.net",
        "recipient": "user@example.com",
        "subject": "HDFC Bank Alert: OTP for Online Transaction of INR 4,500",
        "snippet": "Your One-Time Password for transaction on Card ending 4891 is 894210. Valid for 10 minutes. Do not share with anyone.",
        "body_text": "Bank alert for online debit. If this was not authorized by you, call customer care immediately.",
        "received_offset_hours": 0.5,
        "is_unread": False,
        "is_replied": False,
        "expected_category": "important",
        "expected_subcategory": "security",
        "eligible_for_cleanup": False,
        "protected": True,
        "expected_neglected": False,
    },
    {
        "id": "msg_09_uncertain",
        "thread_id": "thread_09_uncertain",
        "sender": "alex.morgan.consulting@consult-advisors-group.org",
        "recipient": "user@example.com",
        "subject": "Introductory inquiry regarding recent advisory notes",
        "snippet": "Following up on general market trends discussed previously. Hope all is well.",
        "body_text": "Brief note regarding advisory materials. No immediate action required.",
        "received_offset_hours": 5.0,
        "is_unread": True,
        "is_replied": False,
        "expected_category": "uncertain",
        "expected_subcategory": "unknown",
        "eligible_for_cleanup": False,
        "protected": True,
        "expected_neglected": False,
    },
    {
        "id": "msg_10_answered",
        "thread_id": "thread_10_answered",
        "sender": "recruiter-liaison@startup.io",
        "recipient": "user@example.com",
        "subject": "Follow-up regarding technical interview preparation",
        "snippet": "Thank you for sending your availability. We look forward to speaking with you next Tuesday.",
        "body_text": "Your interview schedule is confirmed. User replied with availability on Tuesday.",
        "received_offset_hours": 36.0,
        "is_unread": False,
        "is_replied": True,  # User ALREADY replied!
        "expected_category": "important",
        "expected_subcategory": "recruitment",
        "eligible_for_cleanup": False,
        "protected": True,
        "expected_neglected": False,  # MUST NOT be neglected because already answered!
    },
]


class ControlledTestMailbox:
    """In-memory mailbox adhering to the Gmail and MS Graph REST APIs."""

    def __init__(self, account_email: str = "demo.privagent@gmail.com"):
        self.account_email = account_email
        self.history_id = 1000
        self._messages: dict[str, dict[str, Any]] = {}
        self._history_events: list[dict[str, Any]] = []
        self._trashed_ids: set[str] = set()
        self.reset()

    def reset(self) -> None:
        """Reset mailbox to baseline 10 scenarios."""
        now = time.time()
        self.history_id = 1000
        self._messages = {}
        self._history_events = []
        self._trashed_ids = set()

        for item in TEST_DATASET:
            received_ts = now - (item["received_offset_hours"] * 3600.0)
            msg_obj = {
                "id": item["id"],
                "threadId": item["thread_id"],
                "labelIds": ["INBOX"] if not item.get("is_trashed") else ["TRASH"],
                "snippet": item["snippet"],
                "internalDate": str(int(received_ts * 1000)),
                "payload": {
                    "headers": [
                        {"name": "From", "value": item["sender"]},
                        {"name": "To", "value": item["recipient"]},
                        {"name": "Subject", "value": item["subject"]},
                        {"name": "Date", "value": time.ctime(received_ts)},
                    ],
                    "body": {"data": base64.b64encode(item["body_text"].encode()).decode()},
                },
                "_received_at": received_ts,
                "_is_unread": item["is_unread"],
                "_is_replied": item["is_replied"],
            }
            if item["is_unread"]:
                msg_obj["labelIds"].append("UNREAD")
            if item.get("is_replied"):
                msg_obj["labelIds"].append("REPLIED")
            self._messages[item["id"]] = msg_obj

    def to_email_items(self) -> list[EmailItem]:
        """Convert current mailbox messages to EmailItem models."""
        items: list[EmailItem] = []
        for msg in self._messages.values():
            if "TRASH" in msg.get("labelIds", []):
                continue
            headers = {
                h["name"].lower(): h["value"]
                for h in msg.get("payload", {}).get("headers", [])
            }
            raw_body = ""
            body_data = msg.get("payload", {}).get("body", {}).get("data", "")
            if body_data:
                try:
                    raw_body = base64.b64decode(body_data).decode("utf-8", errors="replace")
                except Exception:
                    raw_body = ""
            items.append(
                EmailItem(
                    id=msg["id"],
                    thread_id=msg.get("threadId", msg["id"]),
                    sender=headers.get("from", ""),
                    recipient=headers.get("to", ""),
                    subject=headers.get("subject", ""),
                    snippet=msg.get("snippet", ""),
                    body_text=raw_body,
                    received_at=msg["_received_at"],
                    is_unread="UNREAD" in msg.get("labelIds", []),
                    is_replied=bool(msg.get("_is_replied")),
                    labels=msg.get("labelIds", []),
                    raw_headers=headers,
                )
            )
        items.sort(key=lambda m: m.received_at, reverse=True)
        return items

    def trash(self, message_id: str) -> bool:
        """Move message to Trash."""
        if message_id in self._messages:
            msg = self._messages[message_id]
            labels = set(msg.get("labelIds", []))
            labels.discard("INBOX")
            labels.add("TRASH")
            msg["labelIds"] = list(labels)
            self._trashed_ids.add(message_id)
            self.history_id += 1
            self._history_events.append({"id": str(self.history_id), "messagesDeleted": [{"message": {"id": message_id}}]})
            return True
        return False

    def untrash(self, message_id: str) -> bool:
        """Restore message from Trash back to Inbox."""
        if message_id in self._messages:
            msg = self._messages[message_id]
            labels = set(msg.get("labelIds", []))
            labels.discard("TRASH")
            labels.add("INBOX")
            msg["labelIds"] = list(labels)
            self._trashed_ids.discard(message_id)
            self.history_id += 1
            self._history_events.append({"id": str(self.history_id), "messagesAdded": [{"message": {"id": message_id}}]})
            return True
        return False

    def mark_replied(self, message_id: str) -> bool:
        """Simulate user manually replying to a message."""
        if message_id in self._messages:
            msg = self._messages[message_id]
            msg["_is_replied"] = True
            labels = set(msg.get("labelIds", []))
            labels.discard("UNREAD")
            labels.add("REPLIED")
            msg["labelIds"] = list(labels)
            self.history_id += 1
            return True
        return False

    def add_incoming_message(self, item: dict[str, Any]) -> str:
        """Simulate a new incoming email after monitoring has begun."""
        now = time.time()
        ident = item.get("id") or f"msg_new_{int(now)}"
        msg_obj = {
            "id": ident,
            "threadId": item.get("thread_id") or f"thread_{ident}",
            "labelIds": ["INBOX", "UNREAD"],
            "snippet": item.get("snippet", ""),
            "internalDate": str(int(now * 1000)),
            "payload": {
                "headers": [
                    {"name": "From", "value": item.get("sender", "service@example.com")},
                    {"name": "To", "value": self.account_email},
                    {"name": "Subject", "value": item.get("subject", "Notification")},
                    {"name": "Date", "value": time.ctime(now)},
                ],
                "body": {"data": base64.b64encode(item.get("body_text", "").encode()).decode()},
            },
            "_received_at": now,
            "_is_unread": True,
            "_is_replied": False,
        }
        self._messages[ident] = msg_obj
        self.history_id += 1
        self._history_events.append({"id": str(self.history_id), "messagesAdded": [{"message": {"id": ident}}]})
        return ident

    def make_mock_client(self) -> httpx.Client:
        """Create an httpx.Client with MockTransport handling Gmail / MS Graph endpoints."""
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            parsed = urlsplit(url)
            path = parsed.path
            method = request.method.upper()

            # Verify Bearer token presence (fail-closed security rule)
            auth_header = request.headers.get("authorization", "")
            if not auth_header.startswith("Bearer "):
                return httpx.Response(401, json={"error": {"code": 401, "message": "Unauthorized"}})

            # Gmail Endpoints
            if "gmail.googleapis.com" in url or path.startswith("/gmail/"):
                if path.endswith("/profile"):
                    return httpx.Response(200, json={"emailAddress": self.account_email, "historyId": str(self.history_id)})
                if "/messages" in path and path.endswith("/trash"):
                    msg_id = path.split("/messages/")[1].split("/trash")[0]
                    ok = self.trash(msg_id)
                    return httpx.Response(200 if ok else 404, json={"id": msg_id, "labelIds": ["TRASH"]})
                if "/messages" in path and path.endswith("/untrash"):
                    msg_id = path.split("/messages/")[1].split("/untrash")[0]
                    ok = self.untrash(msg_id)
                    return httpx.Response(200 if ok else 404, json={"id": msg_id, "labelIds": ["INBOX"]})
                if "/messages/" in path:
                    msg_id = path.split("/messages/")[1].split("?")[0]
                    msg = self._messages.get(msg_id)
                    if msg:
                        return httpx.Response(200, json=copy.deepcopy(msg))
                    return httpx.Response(404, json={"error": {"code": 404, "message": "Not found"}})
                if path.endswith("/history"):
                    return httpx.Response(200, json={"historyId": str(self.history_id), "history": copy.deepcopy(self._history_events)})
                if path.endswith("/messages"):
                    qs = parse_qs(parsed.query)
                    msgs = [
                        {"id": m["id"], "threadId": m["threadId"]}
                        for m in self._messages.values()
                        if "TRASH" not in m.get("labelIds", [])
                    ]
                    return httpx.Response(200, json={"messages": msgs, "resultSizeEstimate": len(msgs)})

            # Microsoft Graph Endpoints
            if "graph.microsoft.com" in url or path.startswith("/v1.0/"):
                if path == "/v1.0/me":
                    return httpx.Response(200, json={"mail": self.account_email, "displayName": "Demo User"})
                if "/messages/" in path and path.endswith("/move"):
                    msg_id = path.split("/messages/")[1].split("/move")[0]
                    body = json.loads(request.content.decode() or "{}")
                    dest = body.get("destinationId", "")
                    if dest == "deleteditems":
                        self.trash(msg_id)
                    else:
                        self.untrash(msg_id)
                    return httpx.Response(200, json={"id": msg_id, "parentFolderId": dest})
                if "/messages/" in path:
                    msg_id = path.split("/messages/")[1].split("?")[0]
                    msg = self._messages.get(msg_id)
                    if msg:
                        return httpx.Response(200, json={
                            "id": msg["id"],
                            "subject": msg["payload"]["headers"][2]["value"],
                            "parentFolderId": "deleteditems" if "TRASH" in msg.get("labelIds", []) else "inbox",
                        })
                    return httpx.Response(404, json={"error": "Not found"})
                if path.endswith("/delta") or "messages" in path:
                    values = [
                        {
                            "id": m["id"],
                            "subject": m["payload"]["headers"][2]["value"],
                            "from": {"emailAddress": {"address": m["payload"]["headers"][0]["value"]}},
                            "bodyPreview": m["snippet"],
                            "parentFolderId": "deleteditems" if "TRASH" in m.get("labelIds", []) else "inbox",
                        }
                        for m in self._messages.values()
                        if "TRASH" not in m.get("labelIds", [])
                    ]
                    return httpx.Response(200, json={"value": values, "@odata.deltaLink": f"delta_{self.history_id}"})

            return httpx.Response(404, json={"error": "Unknown path"})

        return httpx.Client(transport=httpx.MockTransport(handler))

