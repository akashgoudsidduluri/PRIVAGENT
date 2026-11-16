"""Task 14: connector runner tests.

Reads execute directly against a fake connector (no browser, no
network). Writes confirm first and never execute on denial. An
unconfigured connector fails honestly with the missing-connector
message. Answers are scrubbed before return, and every call emits
one metadata-only audit event.
"""
from __future__ import annotations

import asyncio

import pytest

from app.connector_router import ConnectorRoute
from app.connector_runner import run_connector_route


class FakeConnector:
    def __init__(self, *, configured=True, origin="https://gmail.googleapis.com"):
        self.configured = configured
        self.origin = origin
        self.calls = []
        self.trashed = []
        self.drafts = []

    # -- Gmail ------------------------------------------------------
    def summarize_messages(self, query, limit=10):
        self.calls.append(("summarize", query))
        if query == "is:unread":
            return [{"from": "Priya <priya@example.com>", "subject": "Budget",
                     "snippet": "Here is the budget draft.", "thread_id": "t1"}]
        if query == "nothing matches this":
            return []
        return [{"from": "A <a@example.com>", "subject": "Hello",
                 "snippet": "Hi there.", "thread_id": "t1"}]

    def list_messages(self, query="", limit=10):
        self.calls.append(("list", query))
        if query == "nothing matches this":
            return {"messages": []}
        return {"messages": [{"id": "m1", "threadId": "t1"}]}

    def get_message(self, message_id, format="full"):
        self.calls.append(("get", message_id))
        return {"payload": {"headers": [
            {"name": "From", "value": "Priya <priya@example.com>"},
            {"name": "Subject", "value": "Budget"},
        ]}, "snippet": "Here is the budget draft."}

    def draft_email(self, to, subject, body):
        self.drafts.append((to, subject, body))
        return {"id": "d1"}

    def trash_message(self, message_id):
        self.trashed.append(message_id)
        return {}

    # -- Drive ------------------------------------------------------
    def search_documents(self, query, limit=50):
        self.calls.append(("drive_search", query))
        if query == "nothing matches this":
            return []
        return [{"name": "Q3 deck", "webViewLink": "https://drive.google.com/file/d/1/view"}]


class FakeOAuth:
    def __init__(self, connectors):
        self._connectors = connectors

    def connector(self, name):
        return self._connectors[name]


def _route(connector, operation, query="", params=None):
    return ConnectorRoute(connector=connector, operation=operation, query=query,
                          params=params or {})


@pytest.fixture
def audit_events(monkeypatch):
    events = []

    def _append(event_type, summary, actor=None, task_id=None, **kw):
        events.append((event_type, summary))

    monkeypatch.setattr("app.audit.append_event", _append)
    return events


def _run(route, oauth, confirm=None):
    # The repo's test pattern: drive the coroutine with asyncio.run
    # inside a sync test (no pytest-asyncio plugin in this suite).
    return asyncio.run(run_connector_route(route, task_id="t1", instruction="test",
                                           oauth_manager=oauth, confirm_fn=confirm))


def test_gmail_list_read(audit_events):
    conn = FakeConnector()
    out = _run(_route("gmail", "list"), FakeOAuth({"gmail": conn}))
    assert out.ok is True
    assert "Budget" in out.message and "Priya" in out.message
    assert out.executed is True
    assert ("list", "") in conn.calls
    assert audit_events and audit_events[0][0] == "connector_call"
    assert "Budget" not in audit_events[0][1]  # metadata only, never content


def test_gmail_search_read_and_empty_result(audit_events):
    conn = FakeConnector()
    out = _run(_route("gmail", "search", query="is:unread"), FakeOAuth({"gmail": conn}))
    assert out.ok is True and "Budget" in out.message

    out = _run(_route("gmail", "search", query="nothing matches this"),
                     FakeOAuth({"gmail": conn}))
    assert out.ok is True
    assert "No emails matched" in out.message


def test_drive_search_read():
    conn = FakeConnector()
    out = _run(_route("google_drive", "search", query="Q3 deck"),
                     FakeOAuth({"google_drive": conn}))
    assert out.ok is True and "Q3 deck" in out.message


def test_unconfigured_connector_fails_honestly(audit_events):
    conn = FakeConnector(configured=False)
    out = _run(_route("gmail", "list"), FakeOAuth({"gmail": conn}))
    assert out.ok is False
    assert "not connected" in out.message
    assert "Connectors" in out.message
    assert "open Gmail" in out.message  # the browser escape hatch is named
    assert conn.calls == []  # no network call was attempted
    assert audit_events and "not_connected" in audit_events[0][1]


def test_unknown_connector_fails_honestly():
    out = _run(_route("gmail", "list"), FakeOAuth({}))
    assert out.ok is False
    assert "not connected" in out.message


def test_draft_confirms_first_and_executes_on_approval(audit_events):
    conn = FakeConnector()
    seen = {}

    async def confirm(message, action):
        seen["message"] = message
        seen["action"] = action
        return True

    out = _run(
        _route("gmail", "draft", params={"to": "priya@example.com", "subject": "Hi"}),
        FakeOAuth({"gmail": conn}), confirm=confirm,
    )
    assert out.ok is True and "Draft created" in out.message
    assert conn.drafts == [("priya@example.com", "Hi", "")]
    assert "priya@example.com" in seen["message"]
    assert out.executed is True


def test_draft_denied_executes_nothing():
    conn = FakeConnector()

    async def confirm(message, action):
        return False

    out = _run(
        _route("gmail", "draft", params={"to": "priya@example.com"}),
        FakeOAuth({"gmail": conn}), confirm=confirm,
    )
    assert out.ok is False
    assert "declined" in out.message
    assert conn.drafts == []


def test_draft_without_recipient_fails_before_confirm():
    called = []

    async def confirm(message, action):
        called.append(message)
        return True

    conn = FakeConnector()
    out = _run(_route("gmail", "draft", params={}),
                     FakeOAuth({"gmail": conn}), confirm=confirm)
    assert out.ok is False
    assert "who the draft is for" in out.message
    assert called == [] and conn.drafts == []


def test_trash_confirms_naming_the_concrete_email():
    conn = FakeConnector()
    seen = {}

    async def confirm(message, action):
        seen["message"] = message
        return True

    out = _run(_route("gmail", "trash", query="Budget"),
                     FakeOAuth({"gmail": conn}), confirm=confirm)
    assert out.ok is True and "Moved to Trash" in out.message
    assert conn.trashed == ["m1"]
    assert "Budget" in seen["message"] and "Priya" in seen["message"]


def test_trash_nothing_found_trashes_nothing():
    conn = FakeConnector()

    async def confirm(message, action):
        raise AssertionError("must not ask to confirm when nothing matched")

    out = _run(_route("gmail", "trash", query="nothing matches this"),
                     FakeOAuth({"gmail": conn}), confirm=confirm)
    assert out.ok is False
    assert "nothing was trashed" in out.message
    assert conn.trashed == []


def test_answer_is_scrubbed():
    """A credential inside an email snippet must not reach the operator
    verbatim — the runner scrubs answers before returning them."""
    conn = FakeConnector()

    def _leaky(query, limit=10):
        return [{"from": "X <x@example.com>", "subject": "Keys",
                 "snippet": "my password is hunter2 for the account", "thread_id": "t1"}]

    conn.summarize_messages = _leaky
    out = _run(_route("gmail", "search", query="Keys"), FakeOAuth({"gmail": conn}))
    assert out.ok is True
    assert "hunter2" not in out.message


def test_connector_api_failure_is_honest():
    conn = FakeConnector()

    def _boom(query, limit=10):
        raise RuntimeError("quota exceeded")

    conn.summarize_messages = _boom
    out = _run(_route("gmail", "search", query="x"), FakeOAuth({"gmail": conn}))
    assert out.ok is False
    assert "Gmail API call failed" in out.message
    assert "quota exceeded" in out.message


# -- Calendar creates (REVIEW, Task 14 audit) --------------------------
#
# The runner confirms a calendar create before the handler runs, so
# the confirmation must name the actual event — and an instruction
# that does not parse as a create must fail before confirmation.
# Before this fix the confirmation was a generic "Create this
# calendar event?" (no title/time) and an unparseable create sailed
# through it, then silently returned a listing of existing events.


class FakeCalendarConnector:
    def __init__(self):
        self.configured = True
        self.origin = "https://www.googleapis.com"
        self.created = []
        self.listed = 0

    def create_invite(self, calendar_id, title, start, end, attendees):
        self.created.append((title, start, end, attendees))
        return {"htmlLink": "https://calendar.google.com/event?id=x"}

    def list_events(self, calendar_id, start, end, limit):
        self.listed += 1
        return []


class FakeCalendarOAuth:
    def __init__(self, connector):
        self._connector = connector

    def verify_connection(self, provider):
        return {"connected": True, "account": "op@example.com"}

    def connector(self, name):
        return self._connector


def _run_calendar(route, instruction, oauth, companion_oauth, confirm=None, monkeypatch=None):
    """Drive a calendar route with the companion's global OAuth manager stubbed."""
    import app.connectors.calendar_companion as companion

    monkeypatch.setattr(companion, "oauth_manager", companion_oauth)
    return asyncio.run(run_connector_route(route, task_id="t1", instruction=instruction,
                                           oauth_manager=oauth, confirm_fn=confirm))


def test_calendar_create_unparseable_fails_before_confirmation(monkeypatch, audit_events):
    """'schedule a meeting with Priya tomorrow' routes as a create but
    does not parse as one — it must fail honestly with no confirmation
    prompt and no API call, not confirm a creation and list events."""
    cal = FakeCalendarConnector()
    oauth = FakeOAuth({"google_calendar": cal})
    confirmed = []

    async def _confirm(message, action):
        confirmed.append(message)
        return True

    out = _run_calendar(
        _route("google_calendar", "create"),
        "schedule a meeting with Priya tomorrow",
        oauth, FakeCalendarOAuth(cal), confirm=_confirm, monkeypatch=monkeypatch,
    )
    assert out.ok is False
    assert "couldn't tell what event to create" in out.message
    assert confirmed == []  # never asked: there is nothing to approve
    assert cal.created == []
    assert cal.listed == 0


def test_calendar_create_confirmation_names_title_and_time(monkeypatch, audit_events):
    cal = FakeCalendarConnector()
    oauth = FakeOAuth({"google_calendar": cal})
    seen = []

    async def _confirm(message, action):
        seen.append(message)
        return True

    out = _run_calendar(
        _route("google_calendar", "create"),
        "schedule a meeting called Standup on Monday at 10am",
        oauth, FakeCalendarOAuth(cal), confirm=_confirm, monkeypatch=monkeypatch,
    )
    assert out.ok is True
    assert len(seen) == 1
    # The operator approves the actual event, not a generic prompt.
    assert "Standup" in seen[0]
    assert "10:00 AM" in seen[0]  # the time that will actually be booked
    assert "Monday at 10am" in seen[0]  # the time as written
    assert len(cal.created) == 1
    assert cal.created[0][0] == "Standup"


def test_calendar_create_denied_creates_nothing(monkeypatch, audit_events):
    cal = FakeCalendarConnector()
    oauth = FakeOAuth({"google_calendar": cal})

    async def _deny(message, action):
        return False

    out = _run_calendar(
        _route("google_calendar", "create"),
        "schedule a meeting called Standup on Monday at 10am",
        oauth, FakeCalendarOAuth(cal), confirm=_deny, monkeypatch=monkeypatch,
    )
    assert out.ok is False
    assert "declined" in out.message
    assert cal.created == []
