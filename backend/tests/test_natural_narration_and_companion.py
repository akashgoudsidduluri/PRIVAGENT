"""Tests for dynamic step narration, continuous monitoring companion, and natural conversational voice."""
from __future__ import annotations

import pytest

from app.step_narrator import generate_step_narrative, _extract_site_and_query
from app.continuous_monitoring_companion import (
    is_continuous_monitoring_directive,
    handle_continuous_monitoring_instruction,
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
    assert "5173" in resp
    assert "buddy" in resp.lower()
    assert "see you later" in resp.lower() or "catch you later" in resp.lower()


def test_email_dispatch_detection():
    p1 = "Email this comparison summary to b21650406@gmail.com"
    is_d, recip = is_email_dispatch_instruction(p1)
    assert is_d is True
    assert recip == "b21650406@gmail.com"

    # Non-email dispatch
    is_d2, _ = is_email_dispatch_instruction("What is on my calendar today?")
    assert is_d2 is False


@pytest.mark.asyncio
async def test_email_dispatch_execution_when_connected(monkeypatch):
    # Mock oauth_manager to simulate connected account
    from app.connectors.oauth import oauth_manager
    monkeypatch.setattr(oauth_manager, "verify_connection", lambda p: {"connected": True, "account": "b21650406@gmail.com", "access_token": "mock_token"})

    resp = await handle_email_dispatch(
        instruction="Email this comparison summary to b21650406@gmail.com",
        recipient="b21650406@gmail.com",
        chat_id="chat-123",
        history=[],
    )
    assert "b21650406@gmail.com" in resp
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



