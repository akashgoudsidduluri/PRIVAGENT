"""Regression tests for the 2026-11-20 live-test fixes.

Fix 1 (frontend, see tests/timeline_clear.test.mjs): + New Mission
clears the TASK TIMELINE panel.
Fix 2: /api/connectors status is fast (unconfigured providers skip
live verification; configured providers verify concurrently with a
4s cap) — see test_oauth_status_* below.
Fix 3: calendar routing / status checks / multi-turn follow-ups /
multi-event reminders.
Fix 4: <function=...> tags stripped from conversational output.

Every test here is written against the production failure first:
the live reports were "calender" typo not routing, "name the events
as OA remainder 1 and 2" creating nothing, "check if calendar
connected" falling through to the LLM, "create it" listing events
("No upcoming events found"), and <function=calltool> leaking into
chat replies.
"""
import asyncio
import time
from unittest import mock

import httpx
import pytest

from app.connectors.calendar_companion import (
    handle_calendar_instruction,
    is_calendar_query,
    merge_calendar_followup,
    parse_calendar_creates,
)
from app.connector_router import (
    connector_status_reply,
    is_connector_status_query,
)
from app.connectors.oauth import OAuthManager
from app.loop_helpers import strip_tool_call_blocks


# ---------------------------------------------------------------------------
# Fake Google stack for the calendar handler
# ---------------------------------------------------------------------------

class _FakeCalendarConnector:
    def __init__(self):
        self.created = []  # list of kwargs passed to create_invite
        self.list_calls = 0

    def create_invite(self, **kwargs):
        self.created.append(kwargs)
        return {"htmlLink": "https://calendar.google.com/event?eid=x"}

    def list_events(self, **kwargs):
        self.list_calls += 1
        return []


class _FakeOAuthManager:
    def __init__(self, connector):
        self._connector = connector

    def verify_connection(self, provider, user_id=None, connection_id=None):
        return {"configured": True, "connected": True, "account": "user@test.org", "error": None}

    def connector(self, name, user_id=None, connection_id=None):
        return self._connector


def _run(coro):
    return asyncio.run(coro)


_MULTI_INSTRUCTION = (
    "on my calender remind me at 5 30 pm and 6 10 pm on 21st nov 2026. "
    "name the events as OA remainder 1 and 2"
)


# ---------------------------------------------------------------------------
# Fix 3.2 — "calender" typo + named multi-events
# ---------------------------------------------------------------------------

def test_calender_typo_is_a_calendar_query():
    assert is_calendar_query("on my calender remind me at 5 30 pm on 21st nov") is True
    assert is_calendar_query("check my calender for tomorrow") is True
    assert is_calendar_query("remind me on 21st nov 2026 at 5 30 pm") is True
    assert is_calendar_query("keep events for OA1 and OA2") is True


def test_multi_named_reminder_events_parse():
    events = parse_calendar_creates(_MULTI_INSTRUCTION)
    assert events is not None
    assert len(events) == 2
    assert events[0]["title"] == "OA remainder 1"
    assert events[1]["title"] == "OA remainder 2"
    assert (events[0]["start"].hour, events[0]["start"].minute) == (17, 30)
    assert (events[1]["start"].hour, events[1]["start"].minute) == (18, 10)
    assert (events[0]["start"].day, events[0]["start"].month, events[0]["start"].year) == (21, 11, 2026)
    assert (events[1]["start"].day, events[1]["start"].month, events[1]["start"].year) == (21, 11, 2026)


def test_multi_named_reminder_events_created_on_google_calendar():
    fake = _FakeCalendarConnector()
    with mock.patch(
        "app.connectors.calendar_companion.oauth_manager",
        _FakeOAuthManager(fake),
    ):
        answer = _run(handle_calendar_instruction(_MULTI_INSTRUCTION))
    assert len(fake.created) == 2
    assert fake.created[0]["title"] == "OA remainder 1"
    assert fake.created[1]["title"] == "OA remainder 2"
    assert fake.list_calls == 0
    assert "OA remainder 1" in answer and "OA remainder 2" in answer


class _FailingSecondCalendarConnector(_FakeCalendarConnector):
    """Fails on the second create — the reply must still name the
    event that was already created, not report total failure."""

    def create_invite(self, **kwargs):
        if len(self.created) == 1:
            raise RuntimeError("calendar API 500")
        return super().create_invite(**kwargs)


def test_multi_event_partial_failure_names_created_events():
    fake = _FailingSecondCalendarConnector()
    with mock.patch(
        "app.connectors.calendar_companion.oauth_manager",
        _FakeOAuthManager(fake),
    ):
        answer = _run(handle_calendar_instruction(_MULTI_INSTRUCTION))
    assert len(fake.created) == 1
    assert fake.created[0]["title"] == "OA remainder 1"
    assert "Failed to Create" in answer
    assert "OA remainder 1" in answer
    assert "Created 1 before the failure" in answer


def test_hourly_block_creates_bounded_events():
    events = parse_calendar_creates(
        "from 9 am to 6 pm every 1 hour on 21st nov 2026 named OA1, OA2, OA3, OA4, OA5, OA6, OA7, OA8, OA9, OA10"
    )
    assert events is not None
    assert len(events) == 10
    assert events[0]["title"] == "OA1"
    assert (events[0]["start"].hour, events[0]["start"].minute) == (9, 0)
    assert (events[9]["start"].hour, events[9]["start"].minute) == (18, 0)
    # Cap: a 24h block at 1-hour steps must never explode past 12.
    events = parse_calendar_creates(
        "from 12 am to 11 pm every 1 hour on 21st nov 2026 named X1, X2, X3, X4, X5, X6, X7, X8, X9, X10, X11, X12, X13, X14"
    )
    assert events is not None
    assert len(events) == 12


# ---------------------------------------------------------------------------
# Fix 3.1 — connector status questions
# ---------------------------------------------------------------------------

def test_check_if_calendar_connected_is_a_status_query():
    assert is_connector_status_query("check if calendar connected") == "google_calendar"
    assert is_connector_status_query("is calendar connected") == "google_calendar"
    assert is_connector_status_query("is my calender connected") == "google_calendar"
    assert is_connector_status_query("check if gmail connected") == "gmail"
    assert is_connector_status_query("is drive connected") == "google_drive"
    # Bare "is it connected" resolves from the recent calendar turn.
    history = [
        {"role": "user", "content": "what is on my calendar tomorrow"},
        {"role": "agent", "content": "You have no events tomorrow."},
    ]
    assert is_connector_status_query("is it connected", history=history) == "google_calendar"
    assert is_connector_status_query("is it connected", history=[]) is None
    # A plain calendar read is NOT a status question.
    assert is_connector_status_query("what is on my calendar tomorrow") is None


def test_calendar_status_reply_reports_real_connection():
    # Main's real status shape: provider/configured/connected/account/
    # error only — there is NO "connections" list on this branch, so
    # the reply must not claim a scope problem for a connected
    # account (regression: it used to answer "Calendar Scope Missing"
    # for every connected account).
    status_payload = [
        {
            "provider": "google",
            "configured": True,
            "connected": True,
            "account": "user@test.org",
            "error": None,
        },
        {"provider": "microsoft", "configured": False, "connected": False, "account": None, "error": None},
        {"provider": "slack", "configured": False, "connected": False, "account": None, "error": None},
        {"provider": "github", "configured": False, "connected": False, "account": None, "error": None},
    ]
    with mock.patch("app.connectors.oauth.oauth_manager") as mgr:
        mgr.status.return_value = status_payload
        reply = connector_status_reply("google_calendar")
    assert "Connected" in reply
    assert "user@test.org" in reply
    assert "Calendar" in reply
    assert "Scope Missing" not in reply

    with mock.patch("app.connectors.oauth.oauth_manager") as mgr:
        mgr.status.return_value = [
            {**p, "connected": False, "account": None} for p in status_payload
        ]
        reply = connector_status_reply("google_calendar")
    assert "Not Connected" in reply
    assert "Connectors" in reply


# ---------------------------------------------------------------------------
# Fix 3.3 — multi-turn follow-ups never fall back to list_events
# ---------------------------------------------------------------------------

_HISTORY = [
    {"role": "user", "content": "remind me at 5 30 pm on 21st nov 2026, name the event as OA remainder 1"},
    {"role": "agent", "content": "Should I create OA remainder 1 at 5:30 PM on Nov 21, 2026?"},
]


def test_create_it_followup_merges_history():
    merged = merge_calendar_followup("create it", _HISTORY)
    assert "OA remainder 1" in merged
    assert "21st nov 2026" in merged


def test_create_it_followup_creates_event_not_listing():
    fake = _FakeCalendarConnector()
    merged = merge_calendar_followup("create it", _HISTORY)
    with mock.patch(
        "app.connectors.calendar_companion.oauth_manager",
        _FakeOAuthManager(fake),
    ):
        answer = _run(handle_calendar_instruction(merged))
    assert len(fake.created) == 1
    assert fake.created[0]["title"] == "OA remainder 1"
    assert fake.list_calls == 0
    assert "No upcoming events found" not in answer


def test_go_ahead_and_partial_reference_followups():
    assert "OA remainder 1" in merge_calendar_followup("go ahead", _HISTORY)
    assert "OA remainder 1" in merge_calendar_followup("you create it", _HISTORY)
    merged = merge_calendar_followup(
        "create a google calendar event as said on 21th", _HISTORY
    )
    assert "OA remainder 1" in merged


def test_create_intent_without_details_asks_instead_of_listing():
    fake = _FakeCalendarConnector()
    with mock.patch(
        "app.connectors.calendar_companion.oauth_manager",
        _FakeOAuthManager(fake),
    ):
        answer = _run(handle_calendar_instruction("create a google calendar event"))
    assert fake.list_calls == 0
    assert fake.created == []
    assert "No upcoming events found" not in answer
    # A crisp clarification question, not a listing and not silence.
    assert "?" in answer
    assert "when" in answer.lower() or "title" in answer.lower()


# ---------------------------------------------------------------------------
# Fix 4 — <function=...> tags stripped
# ---------------------------------------------------------------------------

def test_function_tags_stripped_from_conversational_output():
    answer, names = strip_tool_call_blocks(
        'Here is the plan <function=calltool>{"name": "browse"}</function> done.'
    )
    assert "<function" not in answer
    assert "calltool" in names or "browse" in names
    assert "Here is the plan" in answer and "done." in answer

    answer, _ = strip_tool_call_blocks("Quick check <function=search/> then answer.")
    assert "<function" not in answer
    assert "Quick check" in answer and "then answer." in answer

    # Truncated (unclosed) block is cut, like <tool_call> is.
    answer, _ = strip_tool_call_blocks("Answer starts here <function=browse>{\"name\": \"browse\"}")
    assert "<function" not in answer
    assert answer.strip() == "Answer starts here"

    # Plain text without tags is untouched.
    answer, names = strip_tool_call_blocks("Just a normal answer.")
    assert answer == "Just a normal answer."
    assert names == []


# ---------------------------------------------------------------------------
# Fix 2 — /api/connectors status is fast
# ---------------------------------------------------------------------------

def test_oauth_status_skips_unconfigured_providers_fast():
    def _no_network(request):
        raise AssertionError(f"unexpected live call to {request.url}")

    manager = OAuthManager(client=httpx.Client(transport=httpx.MockTransport(_no_network)))
    started = time.monotonic()
    results = manager.status()
    elapsed = time.monotonic() - started
    assert elapsed < 2.0
    assert len(results) == 4
    assert all(r["connected"] is False for r in results)
    assert all(r["configured"] is False for r in results)

