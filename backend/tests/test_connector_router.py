"""Task 14: connector router tests.

The router detects Gmail / Calendar / Drive intents and routes them
to the native API connector instead of the browser UI. Negatives
matter as much as positives: an explicit UI request ("open Gmail"),
an email dispatch ("email me this"), and a monitoring setup
("continuously scan my emails") must NOT route.
"""
from __future__ import annotations

import pytest

from app.connector_router import route_instruction


@pytest.mark.parametrize(
    "instruction,connector,operation",
    [
        ("check my inbox", "gmail", "list"),
        ("Check my inbox for anything new", "gmail", "list"),
        ("show me my unread emails", "gmail", "search"),
        ("find the email from Priya", "gmail", "search"),
        ("find emails from priya@example.com", "gmail", "search"),
        ("what did Priya email me about the budget", "gmail", "search"),
        ("search my email for the invoice", "gmail", "search"),
        ("draft an email to priya@example.com about the report", "gmail", "draft"),
        ("trash the email from the newsletter", "gmail", "trash"),
        ("delete the email from the newsletter", "gmail", "trash"),
        ("what's on my calendar tomorrow", "google_calendar", "list"),
        ("what is on my calendar tomorrow", "google_calendar", "list"),
        ("do I have any meetings today", "google_calendar", "list"),
        ("show my schedule for next week", "google_calendar", "list"),
        ("schedule a meeting with Priya tomorrow at 10am", "google_calendar", "create"),
        ("create a calendar event for Friday 2pm", "google_calendar", "create"),
        ("find the Q3 deck in Drive", "google_drive", "search"),
        ("search Drive for the budget spreadsheet", "google_drive", "search"),
        ("find my resume in Google Drive", "google_drive", "search"),
        # REVIEW (Task 14 audit): recall gaps — "get" was not a read
        # verb, "the" was not an object determiner, and "about" was
        # not a preposition for a single-email read.
        ("get my emails", "gmail", "list"),
        ("get my mail", "gmail", "list"),
        ("check the mail", "gmail", "list"),
        ("read the email about the invoices", "gmail", "search"),
    ],
)
def test_routes_detected(instruction, connector, operation):
    route = route_instruction(instruction)
    assert route is not None, instruction
    assert route.connector == connector
    assert route.operation == operation
    assert route.label.startswith("via ")
    assert route.to_dict()["connector"] == connector
    assert route.to_dict()["via"] == route.label


@pytest.mark.parametrize(
    "instruction",
    [
        # Explicit UI navigation stays in the browser.
        "open Gmail",
        "open my inbox",
        "go to my inbox",
        "open Google Calendar",
        "open Drive",
        # Dispatch / monitoring are separate flows, not connector reads.
        "email me this",
        "send an email to Priya",
        "continuously scan my emails",
        "pause email monitoring",
        "monitor my inbox for invoices",
        # Not a connector task at all.
        "find the cheapest laptop",
        "what is my email address",
        "what did you delete",
        "show me important emails",
        "book a flight to Delhi",
        # REVIEW (Task 14 audit): "get" is now a read verb, but only
        # for a mailbox object — "get my email password" is not a read.
        "get my email password",
    ],
)
def test_negatives_do_not_route(instruction):
    assert route_instruction(instruction) is None, instruction


def test_draft_extracts_recipient_and_subject():
    route = route_instruction("draft an email to priya@example.com about the Q3 report")
    assert route is not None
    assert route.operation == "draft"
    assert route.params.get("to") == "priya@example.com"
    assert "Q3 report" in (route.params.get("subject") or "")


def test_search_query_is_the_thing_to_find():
    route = route_instruction("find the email from Priya about the budget")
    assert route is not None
    assert route.operation == "search"
    assert route.query
    assert "priya" in route.query.lower()


def test_mutating_flag():
    assert route_instruction("check my inbox").mutating is False
    assert route_instruction("draft an email to a@b.com").mutating is True
    assert route_instruction("trash the email from X").mutating is True
    assert route_instruction("schedule a meeting tomorrow").mutating is True
    assert route_instruction("what's on my calendar tomorrow").mutating is False


def test_empty_and_none_instructions_do_not_route():
    assert route_instruction("") is None
    assert route_instruction("   ") is None
    assert route_instruction(None) is None


def test_route_dict_carries_names_only():
    route = route_instruction("check my inbox")
    d = route.to_dict()
    assert d["connector"] == "gmail"
    assert d["operation"] == "list"
    assert d["via"] == "via Gmail connector"
    assert "reason" in d
    assert "mutating" in d
