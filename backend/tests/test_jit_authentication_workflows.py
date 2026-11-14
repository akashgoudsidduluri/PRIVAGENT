"""End-to-End Demonstration and Verification of Just-in-Time Authentication Workflows.

Tests:
- Scenario A: First-time LeetCode authentication, task resumption, and recurring scheduling.
- Scenario B: Returning user with active session reuse vs. expired re-authentication.
- Scenario C: IRCTC ticket booking search, confirmation gate, and strict Zero-Agent Payment handoff.
"""
import asyncio
import time
import pytest
from cryptography.fernet import Fernet

from app.auth_manager import AuthenticationManager
from app.website_capabilities.base import ActionRiskLevel
from app.website_capabilities.irctc import IrctcCapability
from app.website_capabilities.leetcode import LeetCodeCapability
from app.website_capabilities.registry import resolve_capability, get_capability_for_domain
from app.completion_verifier import verify_task_completion
from app.continuous_monitoring_companion import is_continuous_monitoring_directive
from app.db import init_db
from app.intent_router import classify_intent, TaskIntent
from app.secure_session_manager import SecureSessionManager


@pytest.fixture
def temp_db(tmp_path):
    db_file = tmp_path / "test_jit_scenarios.db"
    init_db(str(db_file))
    return str(db_file)


@pytest.fixture
def auth_system(temp_db):
    test_key = Fernet.generate_key()
    session_mgr = SecureSessionManager(key=test_key, db_path=temp_db)
    auth_mgr = AuthenticationManager(session_manager=session_mgr)
    return auth_mgr, session_mgr


def test_scenario_a_first_time_leetcode_workflow(auth_system):
    auth_mgr, session_mgr = auth_system
    prompt = "Submit Two Sum on LeetCode if I haven't submitted anything by 3 AM"

    # Step 1: Intent Routing
    intent_res = classify_intent(prompt)
    assert intent_res.intent == TaskIntent.NEW_BROWSER_TASK

    # Step 2: Capability Resolution
    cap = resolve_capability(prompt)
    assert isinstance(cap, LeetCodeCapability)
    assert cap.service_id == "leetcode"
    assert cap.login_url == "https://leetcode.com/accounts/login/"

    # Step 3: Check Authentication Requirement (First time: no session)
    needs_auth, target_cap, guidance_msg = auth_mgr.check_auth_needed(prompt)
    assert needs_auth is True
    assert target_cap.service_id == "leetcode"
    assert "log in to LeetCode first" in guidance_msg

    # Step 4: User completes sign-in in browser tab
    simulated_logged_in_obs = {
        "url": "https://leetcode.com/problemset/all/",
        "interactive": [
            {"role": "link", "name": "Profile", "href": "/u/akash_engineer/", "targetId": "avatar_btn"},
            {"role": "button", "name": "Sign Out", "targetId": "btn_logout"},
        ],
        "pageText": "Welcome back akash_engineer! Daily Challenge: Two Sum",
    }

    # Step 5: Verification & Encrypted Session Storage
    is_verified, account_id, reason = auth_mgr.verify_and_save_session(target_cap, simulated_logged_in_obs)
    assert is_verified is True
    assert account_id == "akash_engineer"

    # Session is securely persisted and active
    assert session_mgr.is_session_valid("leetcode") is True
    saved_session = session_mgr.get_session("leetcode")
    assert saved_session["account_identifier"] == "akash_engineer"
    assert saved_session["status"] == "active"

    # Step 6: Task Resumption - Now checking auth needed on same prompt returns False
    needs_auth_resumed, _, _ = auth_mgr.check_auth_needed(prompt)
    assert needs_auth_resumed is False  # Proceed immediately without interrupting user!


def test_scenario_b_returning_user_session_lifecycle(auth_system):
    auth_mgr, session_mgr = auth_system
    prompt = "Check my LeetCode submissions."

    # 1. Pre-populate active authenticated session
    session_mgr.save_session(
        service_id="leetcode",
        domain="leetcode.com",
        account_identifier="experienced_dev",
        auth_type="browser_cookie",
        credentials={"cookie": "valid_session_token_xyz"},
    )

    # Returning user: should immediately continue without login prompt
    needs_auth, cap, msg = auth_mgr.check_auth_needed(prompt)
    assert needs_auth is False
    assert cap.service_id == "leetcode"
    assert msg is None

    # 2. Simulate session expiration
    session_mgr.invalidate_session("leetcode", reason="expired")

    # Now when user checks submissions, re-authentication is cleanly requested
    needs_reauth, _, reauth_msg = auth_mgr.check_auth_needed(prompt)
    assert needs_reauth is True
    assert "log in to LeetCode first" in reauth_msg


def test_scenario_c_irctc_workflow_and_zero_agent_payment(auth_system):
    auth_mgr, session_mgr = auth_system
    prompt = "login to irctc and book a ticket from sec to tir on 15/nov/26 if available"

    # Step 1: Intent Routing matches browser task directly
    intent_res = classify_intent(prompt)
    assert intent_res.intent == TaskIntent.NEW_BROWSER_TASK

    # Step 2: Capability Resolution
    cap = resolve_capability(prompt)
    assert isinstance(cap, IrctcCapability)
    assert cap.service_id == "irctc"

    # Step 3: Action Risk Levels
    # A) Train search & schedule lookup is autonomous
    risk_search, _ = cap.check_action_risk("click", {"targetId": "search trains"}, "https://www.irctc.co.in/nget/train-search")
    assert risk_search == ActionRiskLevel.AUTONOMOUS

    # B) Selecting passenger details & initiating booking requires confirmation
    risk_book, book_reason = cap.check_action_risk("click", {"targetId": "book now button"}, "https://www.irctc.co.in/nget/booking")
    assert risk_book == ActionRiskLevel.CONFIRMATION_REQUIRED
    assert "confirm the selected train" in book_reason

    # C) Payment execution is strictly PROHIBITED (Zero-Agent Payment policy)
    risk_pay, pay_reason = cap.check_action_risk("click", {"targetId": "proceed to pay button", "text": "Pay & Book"}, "https://www.irctc.co.in/nget/payment")
    assert risk_pay == ActionRiskLevel.PROHIBITED
    assert "strictly prohibits executing financial transactions" in pay_reason


def test_continuous_price_monitoring_verifier():
    """Verify that continuous price monitoring is not rejected as 'Research incomplete'."""
    inst = "Continuously monitor this product price and notify me if it drops"
    assert is_continuous_monitoring_directive(inst) is True

    result = verify_task_completion(
        instruction=inst,
        candidate_state="completed",
        message="I've configured continuous background price tracking for Samsung Galaxy A27.",
        history=[{"action": {"type": "navigate"}, "result_ok": True}],
        working_memory={"continuous_monitoring": True},
    )
    assert result.is_verified is True
    assert result.state == "completed"
    assert "Research incomplete" not in result.message
