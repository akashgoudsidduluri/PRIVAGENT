"""Track-new-items monitoring (2026-11-18): the internship hunt failed
because (a) the directive pattern had no "track/watch/alert ... new
(listing/job/internship)" branch, so the ask fell through to the browser
task and the planner invented a "no tracking capability" blocker, and
(b) the change detector had no way to notice a NEW item on a listing
page. These tests pin the fix: pure tracking asks route to the handler,
compound find-now + track asks do NOT (the browser task searches first
and offers the watch), and a new_listings watch fires only on genuinely
new items, never on the first run or an empty baseline.
"""
import asyncio
import json
import time
import uuid
from pathlib import Path

import pytest

from app import db
from app.change_detector import _triggered, evaluate_observation
from app.continuous_monitoring_companion import (
    _extract_tracked_listing,
    _is_compound_find_and_track,
    handle_continuous_monitoring_instruction,
    is_continuous_monitoring_directive,
)

COMPOUND = (
    "Find SDE internships in Hyderabad/Bangalore, stipend above 15000, "
    "deadline within 2 weeks across Internshala, LinkedIn, and Unstop. "
    "Track them and alert me when a new one matching my profile appears."
)
PURE_TRACK = (
    "Track SDE internships in Hyderabad/Bangalore and alert me when a "
    "new one matching my profile appears."
)
HISTORY_WITH_URL = [
    {"role": "user", "content": "Find SDE internships in Hyderabad"},
    {"role": "assistant",
     "content": "See https://internshala.com/internships/software-development-internship/ for results"},
]


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = str(tmp_path / "track-new.sqlite3")
    monkeypatch.setenv("PRIVAGENT_DB_PATH", path)
    db.init_db()
    return path


def _insert_mission(database, condition_type="new_listings", params=None):
    mission_id = str(uuid.uuid4())
    with db.get_connection(database) as conn:
        conn.execute(
            """INSERT INTO monitoring_missions
            (id, conversation_id, title, target_url, trigger_type, schedule_cron,
             interval_seconds, condition_type, condition_params, status, created_at)
            VALUES (?, 'chat-1', 'New listings: SDE internships',
                    'https://internshala.com/internships/software-development-internship/',
                    'interval', NULL, 3600, ?, ?, 'active', ?)""",
            (mission_id, condition_type, json.dumps(params or {"target": "SDE internships"}), time.time()),
        )
        conn.commit()
    return mission_id


def _missions(database):
    with db.get_connection(database) as conn:
        return conn.execute("SELECT * FROM monitoring_missions").fetchall()


# --- Directive routing -------------------------------------------------

def test_compound_find_and_track_is_not_a_directive():
    # The user's literal failed instruction: it asks to FIND now AND
    # track. Routing it to the handler would create a watch without
    # ever showing results, so it must go to the browser task.
    assert _is_compound_find_and_track(COMPOUND) is True
    assert is_continuous_monitoring_directive(COMPOUND) is False


def test_pure_track_new_is_a_directive():
    assert _is_compound_find_and_track(PURE_TRACK) is False
    assert is_continuous_monitoring_directive(PURE_TRACK) is True


def test_alert_when_you_find_is_a_tracking_ask_not_a_search():
    # "when you find" is a future-discovery clause, not a search-now
    # ask — the find verb sits after the alert verb.
    text = "alert me when you find a new internship"
    assert _is_compound_find_and_track(text) is False
    assert is_continuous_monitoring_directive(text) is True


def test_price_watches_still_route():
    # Regression: the new branches must not swallow the price paths.
    assert is_continuous_monitoring_directive(
        "watch the Samsung Galaxy Fit3 and alert me if it drops below 4000") is True
    assert is_continuous_monitoring_directive(
        "every day at 9am tell me the price of the Amazfit Helio Strap") is True
    assert is_continuous_monitoring_directive("alert me when a new iPhone 16 drops on Amazon") is True


def test_plain_find_is_neither_directive_nor_compound():
    # A pure search with no tracking clause goes to the browser task
    # as a search — it is not a monitoring directive.
    assert is_continuous_monitoring_directive("Find SDE internships in Hyderabad") is False
    assert _is_compound_find_and_track("Find SDE internships in Hyderabad") is False


# --- new_listings detector ---------------------------------------------

def test_new_listings_fires_naming_the_new_item():
    before = {"text": "a b", "elements": ["Alpha Intern", "Beta Intern"], "price": None}
    after = {"text": "a b c", "elements": ["Gamma Intern", "Alpha Intern", "Beta Intern"], "price": None}
    fired, summary = _triggered("new_listings", {}, before, after)
    assert fired is True
    assert "Gamma Intern" in summary


def test_new_listings_silent_when_unchanged():
    before = {"text": "a b", "elements": ["Alpha Intern", "Beta Intern"], "price": None}
    assert _triggered("new_listings", {}, before, before) == (False, "No new listings")


def test_new_listings_empty_before_baselines_instead_of_alerting():
    # The previous snapshot had no items (login wall, empty results):
    # the first non-empty snapshot is the new baseline, not "every
    # existing listing is new".
    before = {"text": "", "elements": [], "price": None}
    after = {"text": "x", "elements": ["Alpha Intern", "Beta Intern"], "price": None}
    fired, summary = _triggered("new_listings", {}, before, after)
    assert fired is False
    assert "Baseline" in summary


def test_new_listings_first_run_baselines_then_second_run_alerts(database):
    mission_id = _insert_mission(database)
    # First run: baseline only, no event.
    assert evaluate_observation(
        mission_id, {"text": "list", "elements": ["Alpha Intern", "Beta Intern"]},
        observed_at=100, db_path=database) is None
    # Second run, unchanged: silent.
    assert evaluate_observation(
        mission_id, {"text": "list", "elements": ["Alpha Intern", "Beta Intern"]},
        observed_at=200, db_path=database) is None
    # Third run, one new listing: fires and names it.
    event = evaluate_observation(
        mission_id, {"text": "list+", "elements": ["Gamma Intern", "Alpha Intern", "Beta Intern"]},
        observed_at=300, db_path=database)
    assert event is not None
    assert "Gamma Intern" in event["summary"]
    # Fourth run, unchanged since the alert: silent (deduplicated).
    assert evaluate_observation(
        mission_id, {"text": "list+", "elements": ["Gamma Intern", "Alpha Intern", "Beta Intern"]},
        observed_at=400, db_path=database) is None


# --- Handler -----------------------------------------------------------

def test_handler_creates_new_listings_watch(database, monkeypatch):
    from app import config as _config
    monkeypatch.setattr(_config, "ALLOWED_DOMAINS", ["internshala.com"])
    answer = asyncio.run(handle_continuous_monitoring_instruction(
        PURE_TRACK, chat_id="chat-1", history=HISTORY_WITH_URL, db_path=database))
    rows = _missions(database)
    assert len(rows) == 1
    row = rows[0]
    assert row["condition_type"] == "new_listings"
    assert row["interval_seconds"] == 3600
    assert row["status"] == "active"
    assert "SDE internships in Hyderabad/Bangalore" in row["title"]
    assert row["target_url"] == "https://internshala.com/internships/software-development-internship/"
    assert "<!-- privagent-watch:" in answer
    assert "Price Watch" not in answer
    assert "every hour" in answer


def test_handler_asks_when_no_search_page_url(database):
    answer = asyncio.run(handle_continuous_monitoring_instruction(
        "alert me when a new SDE internship appears", chat_id="chat-1",
        history=[], db_path=database))
    assert _missions(database) == []
    assert "search page" in answer
    assert "privagent-watch" not in answer


def test_handler_refuses_blocked_domain_by_name(database, monkeypatch):
    from app import config as _config
    monkeypatch.setattr(_config, "ALLOWED_DOMAINS", ["internshala.com"])
    history = [{"role": "assistant",
                "content": "See https://www.linkedin.com/jobs/search/?keywords=SDE for results"}]
    answer = asyncio.run(handle_continuous_monitoring_instruction(
        PURE_TRACK, chat_id="chat-1", history=history, db_path=database))
    assert _missions(database) == []
    assert "linkedin.com" in answer
    assert "PRIVAGENT_ALLOWED_DOMAINS" in answer


def test_price_signal_wins_over_track_new(database, monkeypatch):
    from app import config as _config
    monkeypatch.setattr(_config, "ALLOWED_DOMAINS", ["flipkart.com"])
    answer = asyncio.run(handle_continuous_monitoring_instruction(
        "watch the Samsung Galaxy Fit3 and alert me when a new listing appears "
        "and the price drops below 4000",
        chat_id="chat-1", history=[], db_path=database))
    rows = _missions(database)
    assert len(rows) == 1
    assert rows[0]["condition_type"] == "below_price"
    assert json.loads(rows[0]["condition_params"])["threshold"] == 4000.0


def test_extractor_prefers_full_search_url_not_domain():
    title, url = _extract_tracked_listing(PURE_TRACK, history=HISTORY_WITH_URL)
    assert title == "SDE internships in Hyderabad/Bangalore"
    assert url == "https://internshala.com/internships/software-development-internship/"


def test_extractor_strips_url_from_title():
    # A URL pasted in the instruction must not become the title.
    title, url = _extract_tracked_listing(
        "Track new SDE internships on https://internshala.com/internships/software-development-internship/ "
        "and alert me when a new one appears", history=[])
    assert url == "https://internshala.com/internships/software-development-internship/"
    assert "http" not in title
    assert "internshala.com" not in title
    assert title == "SDE internships"


# --- Planner prompt ----------------------------------------------------

def test_planner_prompt_has_monitoring_and_defaults_sections():
    src = Path(__file__).resolve().parent.parent / "app" / "planner.py"
    text = src.read_text()
    assert "15. MISSING DETAILS" in text
    assert "NEVER conclude a mission with only questions" in text
    assert "16. BACKGROUND MONITORING" in text
    assert "NEVER claim you cannot monitor" in text
    assert "COMPOUND INSTRUCTION" in text


def test_trip_plan_routes_to_browser_task():
    # The live trip stall: "Plan Hyderabad to Delhi next month..."
    # fell through to NON_BROWSER ("general knowledge/conversational
    # request without browsing directives") because TRAVEL_SEARCH_TRIGGER
    # required the verb to come first (find/search/check/book + flights).
    # "Plan" + travel nouns later in the instruction must route to the
    # browser task, where the planner's new defaults rule applies.
    from app.intent_router import classify_intent
    trip = ("Plan Hyderabad to Delhi next month: cheapest flights, "
            "hotels under 3000, and give me the itinerary doc.")
    r = classify_intent(trip)
    assert r.intent.value == "NEW_BROWSER_TASK", r.reason
    assert classify_intent("Plan a trip to Delhi next month").intent.value == "NEW_BROWSER_TASK"
    assert classify_intent("Book the cheapest flight from Hyderabad to Delhi next month.").intent.value == "NEW_BROWSER_TASK"
    # Sanity: a genuine general-knowledge question must stay NON_BROWSER.
    assert classify_intent("What is the capital of France?").intent.value == "NON_BROWSER"


# --- Bare-"yes" alert acceptance after a listing search ----------------

LISTING_HISTORY = [
    {"role": "user", "content": COMPOUND},
    {"role": "assistant", "content": (
        "Found 3 internships on Internshala: "
        "https://internshala.com/internships/software-development-internship/ "
        "— SDE Intern at Acme (stipend 20000), SDE Intern at Beta (stipend 18000). "
        "Want me to keep an eye on it?"
    )},
]


def test_bare_yes_after_listing_offer_is_a_directive():
    # The known gap: _history_has_monitored_product only knew retail
    # hosts (amazon/flipkart/croma), so a bare "yes" after an
    # internship search stayed conversational and no watch was created.
    assert is_continuous_monitoring_directive("yes", history=LISTING_HISTORY)


def test_bare_yes_after_listing_offer_creates_new_listings_watch(tmp_path, monkeypatch):
    from app import config
    from app.continuous_monitoring_companion import handle_continuous_monitoring_instruction
    import asyncio

    monkeypatch.setattr(config, "ALLOWED_DOMAINS", ["internshala.com"])
    db_path = str(tmp_path / "bare-yes.sqlite3")
    answer = asyncio.run(handle_continuous_monitoring_instruction(
        "yes", chat_id="c1", history=LISTING_HISTORY, db_path=db_path))
    assert "New-listings watch active" in answer
    assert "software development internship" in answer
    assert "privagent-watch:" in answer
    assert "Price Watch" not in answer
    rows = _missions(db_path)
    assert len(rows) == 1
    assert rows[0]["condition_type"] == "new_listings"
    assert rows[0]["interval_seconds"] == 3600
    assert rows[0]["target_url"] == "https://internshala.com/internships/software-development-internship/"


def test_bare_yes_without_offer_or_listing_is_not_a_directive():
    # No offer marker and no listing in history: a bare "yes" stays
    # conversational (it might be answering anything).
    assert not is_continuous_monitoring_directive("yes", history=[
        {"role": "assistant", "content": "Shall I go ahead?"},
    ])
    assert not is_continuous_monitoring_directive("yes", history=[])


def test_pronoun_title_is_derived_from_url():
    # "Track them and alert me" must not title the watch "them".
    title, url = _extract_tracked_listing("yes", history=LISTING_HISTORY)
    assert title == "software development internship"
    assert url == "https://internshala.com/internships/software-development-internship/"


def test_explicit_navigation_to_google_calendar_routes_to_browser():
    # "Open Google Calendar" was swallowed by CALENDAR_DIRECTIVE_PATTERN
    # (it contains "google calendar") and never reached the browser,
    # while "Open Gmail" and "Open Google Drive" correctly routed
    # there. An explicit "open/go to/visit" means show the site.
    from app.intent_router import classify_intent

    for text in (
        "Open Google Calendar",
        "open google calendar",
        "go to google calendar",
        "Show me Google Calendar",
        "Open Gmail",
        "Open Google Drive",
    ):
        assert classify_intent(text).intent.value == "NEW_BROWSER_TASK", text
    # But asking about the calendar still goes to the API handler.
    assert classify_intent("What meetings do I have this week?").intent.value == "NON_BROWSER"
    assert classify_intent("Create a calendar event tomorrow at 10am for SIH review.").intent.value == "NON_BROWSER"


def test_how_to_questions_are_advice_not_browser_tasks():
    # "How do I plan a trip to Delhi?" asks for the method, it does not
    # ask the agent to plan the trip. The travel trigger ("plan" + a
    # travel noun) fired on the advice question and the agent started
    # browsing instead of answering. The same for "How do I check my
    # email?" (the email trigger fired before the guard).
    from app.intent_router import classify_intent

    for text in (
        "How do I plan a trip to Delhi?",
        "How to plan a trip?",
        "How do I find cheap flights?",
        "How do I book a flight?",
        "How do I check my email?",
        "How do I set up email monitoring?",
    ):
        assert classify_intent(text).intent.value == "NON_BROWSER", text
    # The matching task phrasings still route to the browser.
    assert classify_intent(
        "Plan Hyderabad to Delhi next month: cheapest flights, hotels under 3000."
    ).intent.value == "NEW_BROWSER_TASK"
    assert classify_intent("Check my email for internship offers.").intent.value == "NEW_BROWSER_TASK"
    assert classify_intent("Find cheapest flights from Hyderabad to Delhi next month.").intent.value == "NEW_BROWSER_TASK"
