"""Tests for dynamic step narration, continuous monitoring companion, and natural conversational voice."""
from __future__ import annotations

import json

import pytest

from app import db
from app.step_narrator import generate_step_narrative, _extract_site_and_query
from app.continuous_monitoring_companion import (
    is_continuous_monitoring_directive,
    handle_continuous_monitoring_instruction,
    parse_below_price,
    parse_daily_time,
)
from app.email_dispatch import (
    is_email_dispatch_instruction,
    handle_email_dispatch,
)
from app.intent_router import classify_intent, TaskIntent


def test_step_narrator_navigation_ecommerce():
    # 1. Amazon navigation
    action_amz = {
        "type": "navigate",
        "params": {"url": "https://www.amazon.in/s?k=samsung+galaxy+A27"},
    }
    narrative1 = generate_step_narrative(
        step_num=1,
        action=action_amz,
        result={"ok": True},
        instruction="compare price of samsung galaxy A27 on amazon, flipkart and croma",
    )
    assert narrative1.startswith("Step 1:")
    assert "Amazon" in narrative1
    assert "samsung galaxy A27" in narrative1
    assert "https://www.amazon.in" in narrative1

    # 2. Flipkart navigation transitioning from Amazon
    action_fk = {
        "type": "navigate",
        "params": {"url": "https://www.flipkart.com/search?q=samsung+galaxy+A27"},
    }
    narrative2 = generate_step_narrative(
        step_num=2,
        action=action_fk,
        result={"ok": True},
        instruction="compare price of samsung galaxy A27 on amazon, flipkart and croma",
        previous_site="Amazon",
    )
    assert narrative2.startswith("Step 2:")
    assert "Flipkart" in narrative2
    assert "here we go" in narrative2 or "check on" in narrative2

    # 3. Croma navigation
    action_croma = {
        "type": "navigate",
        "params": {"url": "https://www.croma.com/searchB?q=samsung+galaxy+A27"},
    }
    narrative3 = generate_step_narrative(
        step_num=3,
        action=action_croma,
        result={"ok": True},
        instruction="compare price of samsung galaxy A27 on amazon, flipkart and croma",
        previous_site="Flipkart",
    )
    assert narrative3.startswith("Step 3:")
    assert "Croma" in narrative3


def test_step_narrator_click_and_type_actions():
    # Popup close attempt (not found)
    action_close = {"type": "click", "target": {"name": "close button"}}
    narrative_close_fail = generate_step_narrative(
        step_num=7,
        action=action_close,
        result={"ok": False, "error": {"message": "no matching element"}},
        instruction="compare price of samsung galaxy A27",
    )
    assert narrative_close_fail.startswith("Step 7:")
    assert "overlay" in narrative_close_fail.lower() or "popup" in narrative_close_fail.lower()

    # Product card clicked (new tab)
    narrative_open_tab = generate_step_narrative(
        step_num=4,
        action={"type": "click", "target": {"name": "Samsung Galaxy A27 5G Light Green"}},
        result={"ok": True, "evidence": "opened new tab: navigated https://www.flipkart.com/samsung-galaxy-a27 → https://www.flipkart.com/samsung-galaxy-a27-5g-light-green/p/itm123"},
        instruction="compare price of samsung galaxy A27",
    )
    assert narrative_open_tab.startswith("Step 4:")
    assert "new tab" in narrative_open_tab.lower()
    assert "specs" in narrative_open_tab.lower() or "price" in narrative_open_tab.lower()

    # Typing into search box
    narrative_type = generate_step_narrative(
        step_num=12,
        action={"type": "type", "params": {"text": "samsung galaxy A27", "submit": True}},
        result={"ok": True},
        instruction="compare price of samsung galaxy A27",
    )
    assert narrative_type.startswith("Step 12:")
    assert "samsung galaxy A27" in narrative_type
    assert "search box" in narrative_type or "typed" in narrative_type.lower()


def test_continuous_monitoring_directive_detection():
    # User's exact prompt from transcript
    p1 = "Continuously monitor this product price and notify me if it drops"
    assert is_continuous_monitoring_directive(p1) is True

    # Variations
    assert is_continuous_monitoring_directive("Monitor price of iPhone 16 on Amazon") is True
    assert is_continuous_monitoring_directive("Alert me if the price drops on this deal") is True
    assert is_continuous_monitoring_directive("Track the price of Samsung Galaxy S24") is True
    assert is_continuous_monitoring_directive("Keep an eye on the price and notify me") is True

    # Non-monitoring requests should not match
    assert is_continuous_monitoring_directive("What is the price of iPhone 16") is False
    assert is_continuous_monitoring_directive("Explain quantum computing") is False


def test_intent_router_classifies_monitoring():
    res = classify_intent("Continuously monitor this product price and notify me if it drops")
    assert res.intent == TaskIntent.NON_BROWSER
    assert "monitoring" in res.reason.lower()


@pytest.mark.asyncio
async def test_continuous_monitoring_execution(tmp_path):
    test_db = str(tmp_path / "test_monitor.db")
    resp = await handle_continuous_monitoring_instruction(
        instruction="Continuously monitor this product price and notify me if it drops",
        chat_id="chat-123",
        history=[{"role": "user", "content": "compare price of samsung galaxy A27 on amazon, flipkart and croma"}],
        db_path=test_db,
    )
    assert "Continuous Price Monitoring Activated" in resp
    assert "5173" in resp  # the Control Center dashboard link stays
    assert "buddy" in resp.lower()
    assert "see you later" in resp.lower() or "catch you later" in resp.lower()
    # Fix 4 (live failure 2026-11-18 02:07 IST): the agent offered to
    # "keep a watch running on port 5173 and ping you". Port 5173 is
    # only the workspace web UI's address, never the monitoring
    # channel, and there is no phone ping — alerts land in the
    # workspace conversation and the monitoring view.
    assert "port 5173" not in resp.lower()
    assert "ping you" not in resp.lower()
    assert "right here in this conversation" in resp


def test_email_dispatch_detection():
    p1 = "Email this comparison summary to operator@example.com"
    is_d, recip = is_email_dispatch_instruction(p1)
    assert is_d is True
    assert recip == "operator@example.com"

    # Non-email dispatch
    is_d2, _ = is_email_dispatch_instruction("What is on my calendar today?")
    assert is_d2 is False


@pytest.mark.asyncio
async def test_email_dispatch_execution_when_connected(monkeypatch):
    # Mock oauth_manager to simulate connected account
    from app.connectors.oauth import oauth_manager
    monkeypatch.setattr(oauth_manager, "verify_connection", lambda p: {"connected": True, "account": "operator@example.com", "access_token": "mock_token"})

    resp = await handle_email_dispatch(
        instruction="Email this comparison summary to operator@example.com",
        recipient="operator@example.com",
        chat_id="chat-123",
        history=[],
    )
    assert "operator@example.com" in resp
    assert "Connected Account" in resp


def test_restore_companion_query_execution_and_capability(monkeypatch):
    from app.email_intelligence.companion_queries import is_email_companion_query, answer_email_companion_query
    from app import db

    # 1. Capability check
    cap_query = "are you capable of restoring last deleted mail?"
    assert is_email_companion_query(cap_query) is True
    ans = answer_email_companion_query(cap_query)
    assert ans["answered"] is True
    assert "Reversible Trash" in ans["text"]

    # 2. Execution check when mock connected
    from app.connectors.oauth import oauth_manager
    monkeypatch.setattr(oauth_manager, "verify_connection", lambda p: {"connected": True, "account": "user@test.org"})

    class MockGmailConn:
        def list_messages(self, query="", limit=10):
            return {"messages": [{"id": "msg_trash_999"}]}
        def get_message(self, mid):
            return {
                "id": mid,
                "payload": {"headers": [{"name": "Subject", "value": "Meeting agenda"}, {"name": "From", "value": "boss@test.org"}]},
            }
        def untrash_message(self, mid):
            return {"id": mid, "labelIds": ["INBOX"]}

    monkeypatch.setattr(oauth_manager, "connector", lambda p: MockGmailConn())

    exec_query = "then restore lastly deleted mail"
    assert is_email_companion_query(exec_query) is True
    res = answer_email_companion_query(exec_query)
    assert res["answered"] is True
    assert "Successfully Restored Email" in res["text"] or "Restored Email from Trash to Inbox" in res["text"]
    assert "Meeting agenda" in res["text"]
    assert "boss@test.org" in res["text"]
    assert "msg_trash_999" in res["text"]


def test_i_connected_everything_companion_query(monkeypatch):
    from app.email_intelligence.companion_queries import is_email_companion_query, answer_email_companion_query
    from app.connectors.oauth import oauth_manager

    monkeypatch.setattr(oauth_manager, "verify_connection", lambda p: {"connected": True, "account": "user@test.org"})
    q = "i connected evrything"
    assert is_email_companion_query(q) is True
    ans = answer_email_companion_query(q)
    assert ans["answered"] is True
    assert "Mail Account Connected & Verified" in ans["text"]


def test_promotional_and_safety_queries(monkeypatch):
    from app.email_intelligence.companion_queries import is_email_companion_query, answer_email_companion_query
    from app.connectors.oauth import oauth_manager

    monkeypatch.setattr(oauth_manager, "verify_connection", lambda p: {"connected": True, "account": "user@test.org"})

    # 1. "can you automatically delete promotional emails"
    q_promo = "can you automatically delete promotional emails"
    assert is_email_companion_query(q_promo) is True
    ans_promo = answer_email_companion_query(q_promo)
    assert ans_promo["answered"] is True
    assert "Autonomous Promotional Email Cleanup Engine" in ans_promo["text"]
    assert "Reversible Trash Only" in ans_promo["text"]

    # 2. "ensure they are not important"
    q_safe = "ensure they are not important"
    assert is_email_companion_query(q_safe) is True
    ans_safe = answer_email_companion_query(q_safe)
    assert ans_safe["answered"] is True
    assert "Fail-Closed Safeguard" in ans_safe["text"]
    assert "Banking" in ans_safe["text"]
    assert "Security" in ans_safe["text"]

    # 3. "it is connected, lets start"
    q_start = "it is connected, lets start"
    assert is_email_companion_query(q_start) is True
    ans_start = answer_email_companion_query(q_start)
    assert ans_start["answered"] is True
    assert "Autonomous Email Cleanup Started" in ans_start["text"]





def test_daily_9am_price_digest_directive():
    # The user's exact follow-up after the fitness-band suggestion.
    follow_up = "yes every day in the mrng at 9am just notify me the prizes"
    assert is_continuous_monitoring_directive(follow_up) is True
    # 9:00 AM IST is 03:30 UTC (IST is UTC+5:30, no daylight saving).
    assert parse_daily_time(follow_up) == ("30 3 * * *", "9:00 AM IST")
    assert parse_daily_time("every day at 9:30am tell me the price") == ("0 4 * * *", "9:30 AM IST")
    assert parse_daily_time("every morning at 8 tell me the prices") == ("30 2 * * *", "8:00 AM IST")
    # A price-drop ask with no daily time is not a daily schedule.
    assert parse_daily_time("notify me if the price drops") is None
    # A plain price question is still not a monitoring directive.
    assert is_continuous_monitoring_directive("What is the price of iPhone 16") is False


@pytest.mark.asyncio
async def test_daily_9am_digest_execution(tmp_path):
    """The user's exact flow: after the Fit3 suggestion, "yes every
    day at 9am notify me the prices" creates a CRON mission (not a
    5-minute interval one) for the Fit3 — never the old hardcoded
    phone fallback."""
    test_db = str(tmp_path / "test_monitor.db")
    history = [
        {"role": "assistant", "content": "1. **Samsung Galaxy Fit3** — ₹3,899 — https://www.flipkart.com/samsung-galaxy-fit3/p/itm123 — best overall"},
        {"role": "user", "content": "I want like this"},
    ]
    resp = await handle_continuous_monitoring_instruction(
        instruction="yes every day in the mrng at 9am just notify me the prizes",
        chat_id="chat-123",
        history=history,
        db_path=test_db,
    )
    assert "9:00 AM IST" in resp
    assert "Fit3" in resp
    assert "A27" not in resp
    assert "Every 5 minutes" not in resp
    with db.get_connection(test_db) as conn:
        row = conn.execute(
            "SELECT trigger_type, schedule_cron, condition_type, title, target_url FROM monitoring_missions"
        ).fetchone()
    assert row["trigger_type"] == "cron"
    assert row["schedule_cron"] == "30 3 * * *"
    assert row["condition_type"] == "daily_price"
    assert "Fit3" in row["title"]
    assert "flipkart" in row["target_url"]


@pytest.mark.asyncio
async def test_no_hardcoded_phone_fallback(tmp_path):
    """A product the extractor has never seen is watched as itself —
    the old 'Samsung Galaxy A27' fallback is gone."""
    test_db = str(tmp_path / "test_monitor2.db")
    resp = await handle_continuous_monitoring_instruction(
        instruction="Monitor price of iPhone 16 on Amazon",
        chat_id="chat-123",
        history=[],
        db_path=test_db,
    )
    assert "A27" not in resp
    assert "Iphone 16" in resp


# ---------------------------------------------------------------------------
# Fix 5 (live failure 2026-11-18 02:07 IST): after the Fit3 suggestion
# the agent gave a verdict but never asked "do you want me to alert
# you?", so the user never reached the daily digest. The suggestion
# must offer the alert, and a bare "yes" to that offer must create a
# real watch — not a conversational promise.
# ---------------------------------------------------------------------------

_OFFER_HISTORY = [
    {"role": "user", "content": "Suggest the best budget friendly fitness band in India"},
    {
        "role": "assistant",
        "content": (
            "1. **Samsung Galaxy Fit3** — ₹3,899 — "
            "https://www.flipkart.com/samsung-galaxy-fit3/p/itm123 — best overall\n\n"
            "Note: I didn't check the stores for this — these prices come from my general "
            "knowledge, not verified live listings.\n\n"
            "Want me to keep an eye on it? I can alert you right here in this conversation "
            "if the price drops."
        ),
    },
]


def test_alert_offer_acceptance_bare_yes_is_monitoring_directive():
    from app.continuous_monitoring_companion import is_continuous_monitoring_directive

    assert is_continuous_monitoring_directive("yes", history=_OFFER_HISTORY) is True
    assert is_continuous_monitoring_directive("yeah please", history=_OFFER_HISTORY) is True
    # No offer in the history: a bare "yes" stays conversational.
    assert is_continuous_monitoring_directive("yes", history=[]) is False
    assert is_continuous_monitoring_directive(
        "yes",
        history=[{"role": "assistant", "content": "Happy to help!"}],
    ) is False
    # Offer present but no product named anywhere: no junk watch.
    assert is_continuous_monitoring_directive(
        "yes",
        history=[{"role": "assistant", "content": "Want me to keep an eye on it?"}],
    ) is False
    # A real question is not an acceptance.
    assert is_continuous_monitoring_directive("why did it fail?", history=_OFFER_HISTORY) is False


@pytest.mark.asyncio
async def test_alert_offer_acceptance_creates_fit3_watch(tmp_path):
    test_db = str(tmp_path / "test_monitor3.db")
    resp = await handle_continuous_monitoring_instruction(
        instruction="yes",
        chat_id="chat-123",
        history=_OFFER_HISTORY,
        db_path=test_db,
    )
    assert "Fit3" in resp
    with db.get_connection(test_db) as conn:
        row = conn.execute(
            "SELECT trigger_type, condition_type, title, target_url FROM monitoring_missions"
        ).fetchone()
    assert row["trigger_type"] == "interval"
    assert row["condition_type"] == "price_drop"
    assert "Fit3" in row["title"]
    assert "flipkart" in row["target_url"]


def test_unverified_suggestion_note_detection():
    from app.continuous_monitoring_companion import needs_unverified_suggestion_note

    fit3_answer = (
        "1. **Samsung Galaxy Fit3** — ₹3,899 — "
        "https://www.flipkart.com/samsung-galaxy-fit3/p/itm123 — best overall"
    )
    assert needs_unverified_suggestion_note(
        "Suggest the best budget friendly fitness band in India", fit3_answer
    ) is True
    # Brand follow-up with a price in the answer.
    assert needs_unverified_suggestion_note(
        "want like the Samsung Galaxy Fit3", "The Fit3 costs ₹3,899."
    ) is True
    # A plain price question answered with a price.
    assert needs_unverified_suggestion_note(
        "What is the price of iPhone 16", "The iPhone 16 is $799."
    ) is True
    # Non-product questions never trigger it.
    assert needs_unverified_suggestion_note(
        "Explain quantum computing", "A qubit is a two-state system."
    ) is False
    # A product question answered without any price or store link.
    assert needs_unverified_suggestion_note(
        "Suggest a fitness band", "Any band with a heart-rate sensor will do."
    ) is False


# ---------------------------------------------------------------------------
# Fix 6: absolute below-price trigger (user: Halio strap alert below
# ₹5000). Generic threshold — no watch is created for any product here.
# ---------------------------------------------------------------------------

def test_below_price_directive_and_parsing():
    assert is_continuous_monitoring_directive("Alert me when the price falls below ₹5000") is True
    assert is_continuous_monitoring_directive("Notify me if the Samsung Galaxy Fit3 drops under 3000") is True
    assert is_continuous_monitoring_directive("Monitor the price and alert me if it goes below ₹4,500") is True
    assert is_continuous_monitoring_directive("Let me know when it drops below ₹5000") is True
    # A plain statement is not a directive.
    assert is_continuous_monitoring_directive("The price is below 5000") is False
    assert is_continuous_monitoring_directive("What is the price of iPhone 16") is False
    assert parse_below_price("Alert me when the price falls below ₹5000") == 5000.0
    assert parse_below_price("notify me if it drops under ₹4,500") == 4500.0
    assert parse_below_price("alert me if the price drops") is None
    assert parse_below_price("every day at 9am tell me the price") is None


@pytest.mark.asyncio
async def test_below_price_watch_creation(tmp_path):
    test_db = str(tmp_path / "test_monitor_below.db")
    resp = await handle_continuous_monitoring_instruction(
        instruction="Alert me if the Samsung Galaxy Fit3 price falls below ₹3000",
        chat_id="chat-123",
        history=[],
        db_path=test_db,
    )
    assert "below ₹3000" in resp
    assert "first check" in resp
    with db.get_connection(test_db) as conn:
        row = conn.execute(
            "SELECT trigger_type, condition_type, condition_params, title FROM monitoring_missions"
        ).fetchone()
    assert row["trigger_type"] == "interval"
    assert row["condition_type"] == "below_price"
    assert json.loads(row["condition_params"])["threshold"] == 3000.0
    assert "Fit3" in row["title"]


# --- Backend flaw-hunt finding 7: junk watch + hour-first daily time. ---

def test_daily_time_hour_first_order():
    """The daily phrase may come AFTER the hour ("notify me the price
    at 9am every day") — the old regex required it before the hour,
    so this became a 5-minute price_drop watch instead of the 9am
    digest."""
    assert parse_daily_time("notify me the price at 9am every day") == ("30 3 * * *", "9:00 AM IST")
    assert parse_daily_time("tell me the price at 9:30am every morning") == ("0 4 * * *", "9:30 AM IST")
    assert parse_daily_time("notify me at 9 every day") == ("30 3 * * *", "9:00 AM IST")
    # A price threshold is not a time, even with "every day" after it.
    assert parse_daily_time("alert me if it drops below ₹5000 every day") is None
    assert parse_daily_time("notify me if the price drops") is None


@pytest.mark.asyncio
async def test_no_product_no_url_refuses_watch(tmp_path):
    """\"Monitor this\" with no product named and no store link
    anywhere must NOT create a watch for 'your last suggestion' on
    the Flipkart homepage. The handler asks which product instead."""
    test_db = str(tmp_path / "test_monitor_junk.db")
    resp = await handle_continuous_monitoring_instruction(
        instruction="monitor this",
        chat_id="chat-123",
        history=[],
        db_path=test_db,
    )
    assert "don't have a product to watch" in resp
    with db.get_connection(test_db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM monitoring_missions").fetchone()[0]
    assert count == 0


@pytest.mark.asyncio
async def test_product_without_url_watches_search_page(tmp_path):
    """A named product with no store URL is watched via its search
    page — never the store homepage, whose first price is a banner,
    not the product's."""
    test_db = str(tmp_path / "test_monitor_search.db")
    resp = await handle_continuous_monitoring_instruction(
        instruction="Monitor price of iPhone 16 on Amazon",
        chat_id="chat-123",
        history=[],
        db_path=test_db,
    )
    assert "Continuous Price Monitoring Activated" in resp
    with db.get_connection(test_db) as conn:
        row = conn.execute("SELECT title, target_url FROM monitoring_missions").fetchone()
    assert "Iphone 16" in row["title"]
    assert "search?q=" in row["target_url"]
    assert row["target_url"] != "https://www.flipkart.com"
