"""Regression: two silent-drop / misread bugs found by the live API probe.

1. Provider misread: "scan my email for the Microsoft interview" was
   read as a request for the OUTLOOK provider (the provider regex matched
   bare "microsoft"), so the handler reported "Outlook not connected"
   and never scanned Gmail — even though "Microsoft interview" is a
   TOPIC, not a mailbox. Only a mailbox-anchored "microsoft"
   ("microsoft account/inbox/mail/365") names the provider.

2. Calendar half dropped: "add it to my calendar" was not recognised
   as a calendar ask anywhere (is_calendar_query, the clause extractor,
   or the router), so the calendar half of a compound instruction
   vanished silently — no event, no message, nothing.
"""

from app.connectors.calendar_companion import (
    extract_calendar_create_clause,
    handle_compound_calendar,
    is_calendar_query,
    resolve_calendar_event_pronoun,
)
from app.email_intelligence.mission_setup import named_email_providers
from app.intent_router import TaskIntent, classify_intent

INS = "scan my email for the Microsoft interview and add it to my calendar with a reminder 3 days before"


def test_microsoft_interview_is_not_an_outlook_request():
    assert named_email_providers("scan my email for the Microsoft interview") == set()
    assert named_email_providers("find the Microsoft interview invitation") == set()
    assert named_email_providers("Microsoft interview offer") == set()
    # Mailbox-anchored "microsoft" still names Outlook.
    assert named_email_providers("monitor my Outlook inbox") == {"outlook"}
    assert named_email_providers("monitor my microsoft account") == {"outlook"}
    assert named_email_providers("check my Microsoft 365 mail") == {"outlook"}
    assert named_email_providers("monitor my gmail") == {"gmail"}
    assert named_email_providers("monitor my gmail and outlook") == {"gmail", "outlook"}


def test_add_it_to_my_calendar_is_a_calendar_ask():
    assert is_calendar_query("add it to my calendar") is True
    assert is_calendar_query("add this to my calendar") is True
    assert is_calendar_query("put it on my calendar") is True
    assert is_calendar_query(INS) is True
    # And the router sends a standalone one to the calendar handler,
    # not to the browser.
    got = classify_intent("add the Microsoft interview to my calendar")
    assert got.intent == TaskIntent.NON_BROWSER


def test_compound_calendar_clause_survives_add_it_phrasing():
    clause = extract_calendar_create_clause(INS)
    assert clause is not None
    assert clause.startswith("add it to my calendar")
    resolved = resolve_calendar_event_pronoun(clause, INS)
    assert "Microsoft interview" in resolved
    assert resolved.strip().lower().startswith("add microsoft interview to my calendar")


async def _compound_result():
    return await handle_compound_calendar(INS)


def test_compound_calendar_half_is_not_silently_dropped():
    import asyncio

    result = asyncio.run(_compound_result())
    # The clause has no time (it lives in the email), so the honest
    # outcome is the "couldn't parse a time" note — never silence.
    assert result is not None
    assert "Calendar" in result
    assert "couldn't work out the calendar event" in result
