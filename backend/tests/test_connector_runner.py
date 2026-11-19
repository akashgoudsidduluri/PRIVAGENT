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

    def connector(self, name, user_id=None, connection_id=None):
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
        self.created_reminders = []
        self.listed = 0

    def create_invite(self, calendar_id, title, start, end, attendees, reminders=None):
        self.created.append((title, start, end, attendees))
        self.created_reminders.append(reminders)
        return {"htmlLink": "https://calendar.google.com/event?id=x"}

    def list_events(self, calendar_id, start, end, limit):
        self.listed += 1
        return []


class FakeCalendarOAuth:
    def __init__(self, connector):
        self._connector = connector

    def verify_connection(self, provider, user_id=None, connection_id=None):
        return {"connected": True, "account": "op@example.com"}

    def connector(self, name, user_id=None, connection_id=None):
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


# -- Calendar time_text interpretation (fix 7) ----------------------
#
# "SIH review tomorrow 10:00 AM IST" used to book tomorrow 10:00 UTC
# (15:30 IST): the named time was captured but never interpreted.


def test_parse_calendar_create_time_text():
    import datetime as _dt

    from app.connectors.calendar_companion import parse_calendar_create

    ist = _dt.timezone(_dt.timedelta(hours=5, minutes=30), "IST")
    tomorrow_ist = (_dt.datetime.now(_dt.timezone.utc).astimezone(ist)
                    + _dt.timedelta(days=1)).date()

    # The demo case: tomorrow 10:00 AM IST books 10:00 IST, not 10:00 UTC.
    parsed = parse_calendar_create("schedule a meeting called SIH review tomorrow at 10:00 AM IST")
    assert parsed is not None
    assert (parsed["start"].hour, parsed["start"].minute) == (10, 0)
    assert parsed["start"].utcoffset() == _dt.timedelta(hours=5, minutes=30)
    assert parsed["start"].tzname() == "IST"
    assert parsed["start"].date() == tomorrow_ist
    assert (parsed["end"] - parsed["start"]) == _dt.timedelta(hours=1)

    # Explicit UTC stays UTC.
    parsed_utc = parse_calendar_create("schedule a meeting called Sync tomorrow at 10:00 AM UTC")
    assert parsed_utc["start"].utcoffset() == _dt.timedelta(0)
    assert parsed_utc["start"].tzname() == "UTC"

    # An unmarked time is the user's local time (IST), not UTC.
    parsed_plain = parse_calendar_create("schedule a meeting called Standup tomorrow at 10am")
    assert parsed_plain["start"].utcoffset() == _dt.timedelta(hours=5, minutes=30)
    assert (parsed_plain["start"].hour, parsed_plain["start"].minute) == (10, 0)

    # 24-hour time and an explicit date.
    parsed_24 = parse_calendar_create("schedule a meeting called Review today at 14:30")
    assert (parsed_24["start"].hour, parsed_24["start"].minute) == (14, 30)
    parsed_date = parse_calendar_create("schedule a meeting called Demo on 2026-11-19 at 9am")
    assert (parsed_date["start"].year, parsed_date["start"].month, parsed_date["start"].day) == (2026, 11, 19)
    assert parsed_date["start"].hour == 9

    # A weekday name books that weekday.
    parsed_mon = parse_calendar_create("schedule a meeting called Standup on Monday at 10am")
    assert parsed_mon["start"].weekday() == 0

    # No usable time: tomorrow 10:00 IST, never silent UTC.
    parsed_fallback = parse_calendar_create("schedule a meeting called Chat on whenever")
    assert parsed_fallback is not None
    assert (parsed_fallback["start"].hour, parsed_fallback["start"].minute) == (10, 0)
    assert parsed_fallback["start"].utcoffset() == _dt.timedelta(hours=5, minutes=30)


def test_calendar_create_ist_time_books_ist(monkeypatch, audit_events):
    """End to end: the confirmation and the created invite both carry
    10:00 IST for 'tomorrow 10:00 AM IST' — never 10:00 UTC."""
    cal = FakeCalendarConnector()
    oauth = FakeOAuth({"google_calendar": cal})
    seen = []

    async def _confirm(message, action):
        seen.append(message)
        return True

    out = _run_calendar(
        _route("google_calendar", "create"),
        "schedule a meeting called SIH review tomorrow at 10:00 AM IST",
        oauth, FakeCalendarOAuth(cal), confirm=_confirm, monkeypatch=monkeypatch,
    )
    assert out.ok is True
    assert len(seen) == 1
    assert "10:00 AM IST" in seen[0]
    assert "UTC" not in seen[0]
    assert "15:30" not in seen[0]
    assert len(cal.created) == 1
    title, start, end, _attendees = cal.created[0]
    assert "SIH review" in title
    assert "T10:00:00+05:30" in start
    assert "T11:00:00+05:30" in end


def test_parse_calendar_create_time_first_form():
    """The most natural phrasing puts the time before the title:
    'Create a calendar event tomorrow at 10am for SIH review.' The
    old title-first regex captured the day word into the title
    ('SIH review tomorrow') or missed the 'calendar' noun entirely,
    so the event was created with the wrong name. The parse is now
    time-aware: the title is the instruction minus the verb phrase
    and the time span."""
    import datetime as _dt

    from app.connectors.calendar_companion import parse_calendar_create

    ist = _dt.timezone(_dt.timedelta(hours=5, minutes=30), "IST")
    tomorrow_ist = (_dt.datetime.now(_dt.timezone.utc).astimezone(ist)
                    + _dt.timedelta(days=1)).date()

    parsed = parse_calendar_create("Create a calendar event tomorrow at 10am for SIH review.")
    assert parsed is not None
    assert parsed["title"] == "SIH review"
    assert parsed["start"].date() == tomorrow_ist
    assert (parsed["start"].hour, parsed["start"].minute) == (10, 0)
    assert parsed["start"].utcoffset() == _dt.timedelta(hours=5, minutes=30)

    # Title-first form keeps working, and the day word stays in the time.
    parsed2 = parse_calendar_create("Create an event SIH review tomorrow at 10am")
    assert parsed2 is not None and parsed2["title"] == "SIH review"

    # Attendee emails are attendees, not part of the title.
    parsed3 = parse_calendar_create(
        "Create a calendar event tomorrow at 10am for SIH review with priya@example.com"
    )
    assert parsed3 is not None and parsed3["title"] == "SIH review"
    assert "priya@example.com" in parsed3["attendees"]

    # No clock time and no 'on/at/for' separator: not a create.
    assert parse_calendar_create("schedule a meeting with Priya tomorrow") is None
    assert parse_calendar_create("Create a calendar event for SIH review") is None


def test_parse_calendar_create_next_weekday_and_modifier_noun():
    """Focused re-check found two gaps in the time-first parse:

    - "next Monday" leaked into the title: "Add a calendar event
      next Monday at 9:30am called Chat with Priya" titled the event
      "next   called Chat with Priya" because _time_span only knew
      the bare weekday, not the "next"/"this" qualifier.
    - A modifier before the event noun broke the verb phrase entirely:
      "Book a dentist appointment tomorrow at 4pm" returned None,
      because the verb regex required the event noun right after the
      article. The modifier + noun are the title ("dentist
      appointment"), not filler.
    """
    import datetime as _dt

    from app.connectors.calendar_companion import parse_calendar_create

    ist = _dt.timezone(_dt.timedelta(hours=5, minutes=30), "IST")
    now_ist = _dt.datetime.now(_dt.timezone.utc).astimezone(ist)

    parsed = parse_calendar_create(
        "Add a calendar event next Monday at 9:30am called Chat with Priya."
    )
    assert parsed is not None
    assert parsed["title"] == "Chat with Priya"
    assert (parsed["start"].hour, parsed["start"].minute) == (9, 30)
    assert parsed["start"].weekday() == 0  # Monday
    assert parsed["start"].date() > now_ist.date()

    parsed2 = parse_calendar_create("Book a dentist appointment tomorrow at 4pm.")
    assert parsed2 is not None
    assert parsed2["title"] == "dentist appointment"
    assert (parsed2["start"].hour, parsed2["start"].minute) == (16, 0)

    parsed3 = parse_calendar_create("Create a team meeting tomorrow at 10am")
    assert parsed3 is not None and parsed3["title"] == "team meeting"


def test_compound_email_calendar_clause_parses_and_surfaces():
    """The combined email+calendar test failed silently: "Monitor my
    email using demo sandbox, find the Microsoft interview invitation,
    and create a Google Calendar event for it tomorrow at 10am with a
    reminder 3 days before." The email mission handler consumed the
    whole instruction and the calendar half vanished — no event, no
    message, nothing. Three more gaps in the same clause: "create a
    Google Calendar event" never parsed at all ("Google" had no slot
    in the verb regex), the reminder clause would have polluted the
    title, and the pronoun "it" would have titled the event "it".

    The clause must extract, "it" must resolve to the Microsoft
    interview named in the email half, the reminder must parse to
    4320 minutes (3 days) and stay out of the title, and the compound
    handler must return the calendar result (honest not-connected in
    this environment) instead of None.
    """
    import asyncio

    from app.connectors.calendar_companion import (
        extract_calendar_create_clause,
        handle_compound_calendar,
        parse_calendar_create,
        resolve_calendar_event_pronoun,
    )

    instr = (
        "Monitor my email using demo sandbox, find the Microsoft interview "
        "invitation, and create a Google Calendar event for it tomorrow at "
        "10am with a reminder 3 days before."
    )
    clause = extract_calendar_create_clause(instr)
    assert clause == (
        "create a Google Calendar event for it tomorrow at 10am "
        "with a reminder 3 days before"
    )
    resolved = resolve_calendar_event_pronoun(clause, instr)
    assert resolved == (
        "create a Google Calendar event for Microsoft interview tomorrow "
        "at 10am with a reminder 3 days before"
    )
    parsed = parse_calendar_create(resolved)
    assert parsed is not None
    assert parsed["title"] == "Microsoft interview"
    assert (parsed["start"].hour, parsed["start"].minute) == (10, 0)
    assert parsed["reminder_minutes"] == 4320

    # The compound handler surfaces the calendar half instead of
    # dropping it: in this environment the calendar is not connected,
    # so the honest not-connected message is the correct result.
    section = asyncio.run(handle_compound_calendar(instr))
    assert section is not None, "calendar half was silently dropped"
    assert "Google Calendar Not Connected" in section or "Google Calendar Invite Created" in section
    # A pure email instruction has no calendar clause: nothing to append.
    assert asyncio.run(handle_compound_calendar(
        "Monitor my email using demo sandbox, scan every 60 minutes."
    )) is None
    # A bare pronoun with no referent is an honest ask, not a nonsense event.
    no_ref = asyncio.run(handle_compound_calendar(
        "Monitor my email and create a calendar event for it tomorrow at 10am."
    ))
    assert no_ref is not None and "couldn't tell which event" in no_ref


def test_parse_calendar_create_google_calendar_and_reminder():
    """parse_calendar_create must accept "Google Calendar" in the verb
    phrase and must understand a reminder clause: the reminder is
    minutes-before, never part of the title."""
    from app.connectors.calendar_companion import parse_calendar_create

    p = parse_calendar_create(
        "Create a Google Calendar event tomorrow at 10am for SIH review "
        "with a reminder 1 day before."
    )
    assert p is not None
    assert p["title"] == "SIH review"
    assert p["reminder_minutes"] == 1440

    p2 = parse_calendar_create(
        "Book a dentist appointment tomorrow at 4pm, remind me 2 hours before."
    )
    assert p2 is not None
    assert p2["title"] == "dentist appointment"
    assert p2["reminder_minutes"] == 120

    p3 = parse_calendar_create("Create a calendar event tomorrow at 10am for SIH review.")
    assert p3 is not None
    assert p3["title"] == "SIH review"
    assert p3["reminder_minutes"] is None


def test_calendar_create_reminder_reaches_connector(monkeypatch, audit_events):
    """End to end: a reminder in the instruction must reach create_invite
    as a popup override — not vanish after confirmation. (The fake
    connector must mirror the real one's reminders parameter, or the
    create raises TypeError inside the handler and the route reports
    ok with nothing created.)"""
    cal = FakeCalendarConnector()
    oauth = FakeOAuth({"google_calendar": cal})
    seen = []

    async def _confirm(message, action):
        seen.append(message)
        return True

    out = _run_calendar(
        _route("google_calendar", "create"),
        "schedule a meeting called Standup on Monday at 10am with a reminder 3 days before",
        oauth, FakeCalendarOAuth(cal), confirm=_confirm, monkeypatch=monkeypatch,
    )
    assert out.ok is True
    assert len(cal.created) == 1
    assert cal.created[0][0] == "Standup"
    assert cal.created_reminders[0] == [{"method": "popup", "minutes": 4320}]
    # No reminder: nothing is passed.
    cal2 = FakeCalendarConnector()
    out2 = _run_calendar(
        _route("google_calendar", "create"),
        "schedule a meeting called Standup on Monday at 10am",
        FakeOAuth({"google_calendar": cal2}), FakeCalendarOAuth(cal2),
        confirm=_confirm, monkeypatch=monkeypatch,
    )
    assert out2.ok is True
    assert len(cal2.created) == 1
    assert cal2.created_reminders[0] is None
