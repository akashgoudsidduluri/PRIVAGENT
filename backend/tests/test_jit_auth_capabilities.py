"""Unit tests for Website Capability Layer and Just-in-Time Authentication Manager."""
import pytest
from cryptography.fernet import Fernet

from app.auth_manager import AuthenticationManager
from app.website_capabilities.base import ActionRiskLevel
from app.website_capabilities.irctc import IrctcCapability
from app.website_capabilities.leetcode import LeetCodeCapability
from app.website_capabilities.registry import (
    get_capability_for_domain,
    get_capability_for_service,
    resolve_capability,
)
from app.db import init_db
from app.secure_session_manager import SecureSessionManager


@pytest.fixture
def temp_db(tmp_path):
    db_file = tmp_path / "test_jit_auth.db"
    init_db(str(db_file))
    return str(db_file)


@pytest.fixture
def auth_manager(temp_db):
    test_key = Fernet.generate_key()
    session_mgr = SecureSessionManager(key=test_key, db_path=temp_db)
    return AuthenticationManager(session_manager=session_mgr)


def test_capability_resolution():
    cap_lc = resolve_capability("Submit Two Sum on LeetCode if I haven't submitted anything")
    assert cap_lc is not None
    assert cap_lc.service_id == "leetcode"

    cap_irctc = resolve_capability("login to irctc and book a ticket from sec to tir")
    assert cap_irctc is not None
    assert cap_irctc.service_id == "irctc"

    cap_by_domain = get_capability_for_domain("https://leetcode.com/problemset/all/")
    assert cap_by_domain.service_id == "leetcode"


def test_leetcode_capability_auth_and_verification():
    lc = LeetCodeCapability()

    # Submission requires auth
    assert lc.check_requires_auth("click", "https://leetcode.com", "Submit Two Sum on LeetCode") is True
    # Pure generic browsing does not
    assert lc.check_requires_auth("navigate", "https://leetcode.com/problems/two-sum", "read the two sum problem description") is False

    # Negative observation (on login page with password field)
    login_obs = {
        "url": "https://leetcode.com/accounts/login/",
        "interactive": [{"role": "input", "inputType": "password", "targetId": "password_box"}],
        "pageText": "Sign in with your LeetCode account",
    }
    is_auth, user, reason = lc.verify_authentication(login_obs)
    assert is_auth is False
    assert user is None

    # Positive observation (authenticated with avatar and profile URL)
    auth_obs = {
        "url": "https://leetcode.com/problemset/all/",
        "interactive": [
            {"role": "link", "name": "Profile", "href": "/u/algo_master/", "targetId": "nav_avatar"},
            {"role": "button", "name": "Sign Out", "targetId": "logout_btn"},
        ],
        "pageText": "Welcome back algo_master! Daily Challenge: Two Sum",
    }
    is_auth, user, reason = lc.verify_authentication(auth_obs)
    assert is_auth is True
    assert user == "algo_master"


def test_irctc_capability_and_payment_boundary():
    irctc = IrctcCapability()

    # Searching trains does not require auth
    assert irctc.check_requires_auth("navigate", "https://www.irctc.co.in", "check availability of trains from sec to tir") is False
    # Booking requires auth
    assert irctc.check_requires_auth("click", "https://www.irctc.co.in", "book a ticket from sec to tir") is True

    # Search trains is autonomous
    risk, _ = irctc.check_action_risk("click", {"targetId": "search_button"}, "https://www.irctc.co.in/nget/train-search")
    assert risk == ActionRiskLevel.AUTONOMOUS

    # Booking confirmation gate
    risk, _ = irctc.check_action_risk("click", {"targetId": "book now button"}, "https://www.irctc.co.in/nget/booking")
    assert risk == ActionRiskLevel.CONFIRMATION_REQUIRED

    # Payment strictly prohibited (Zero-Agent Payment rule)
    risk, reason = irctc.check_action_risk("click", {"targetId": "proceed to pay button"}, "https://www.irctc.co.in/nget/payment")
    assert risk == ActionRiskLevel.PROHIBITED
    assert "prohibits executing financial transactions" in reason


def test_jit_auth_manager_lifecycle(auth_manager):
    task = "Submit Two Sum on LeetCode if I haven't submitted anything by 3 AM"

    # 1. First-time user: No session exists -> Auth Needed
    needs_auth, cap, msg = auth_manager.check_auth_needed(task)
    assert needs_auth is True
    assert cap is not None
    assert cap.service_id == "leetcode"
    assert "log in to LeetCode first" in msg

    # 2. User completes login -> Positive observation verified & saved
    obs = {
        "url": "https://leetcode.com/problemset/",
        "interactive": [{"role": "link", "name": "avatar", "href": "/u/code_ninja/"}],
        "pageText": "code_ninja",
    }
    success, username, _ = auth_manager.verify_and_save_session(cap, obs)
    assert success is True
    assert username == "code_ninja"

    # 3. Returning user: Active session exists -> No Auth Needed!
    needs_auth_returning, cap_ret, _ = auth_manager.check_auth_needed(task)
    assert needs_auth_returning is False
    assert cap_ret.service_id == "leetcode"

    # 4. Invalidate session -> Requires Auth again
    auth_manager.invalidate_session("leetcode", reason="expired")
    needs_auth_after_exp, _, _ = auth_manager.check_auth_needed(task)
    assert needs_auth_after_exp is True
