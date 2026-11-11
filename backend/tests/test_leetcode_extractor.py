"""Unit tests for LeetCode profile extraction and authentication verification."""
import pytest

from app.auth_validator import verify_authentication_state
from app.extractors.leetcode import extract_leetcode_profile, format_leetcode_report


def test_auth_validator_detects_login_page():
    obs_login = {
        "url": "https://leetcode.com/accounts/login/",
        "interactive": [
            {"targetId": "login_btn", "role": "button", "name": "Sign In"},
            {"targetId": "password_inp", "role": "textbox", "name": "Password", "inputType": "password"},
        ],
        "pageText": "Sign in to LeetCode\nUsername or Email\nPassword\nForgot Password?",
    }
    is_auth, reason = verify_authentication_state(obs_login, expected_site="leetcode")
    assert is_auth is False
    assert "login page" in reason.lower() or "authentication" in reason.lower()


def test_auth_validator_detects_authenticated_state():
    obs_auth = {
        "url": "https://leetcode.com/problemset/",
        "interactive": [
            {"targetId": "user_avatar", "role": "button", "name": "User Avatar"},
            {"targetId": "signout_btn", "role": "link", "name": "Sign Out"},
        ],
        "pageText": "Problems\nStudy Plan\nDiscuss\nInterview",
    }
    is_auth, reason = verify_authentication_state(obs_auth, expected_site="leetcode")
    assert is_auth is True
    assert "authenticated" in reason.lower()


def test_extract_active_competitor_profile():
    sample_text = """
    ### akash_coder
    India | LeetCode User
    
    Solved 584 / 3240
    Easy 240 / 820
    Medium 284 / 1700
    Hard 60 / 720
    
    Contest Rating: 1,845.6
    Global Ranking: 32,150 / 450,000
    
    Badges 4
    50 Days Badge 2026
    Guardian Badge
    
    Recent Submissions
    Two Sum | Accepted | 2 hours ago
    LRU Cache | Accepted | 1 day ago
    """

    obs = {
        "url": "https://leetcode.com/u/akash_coder/",
        "title": "akash_coder - LeetCode Profile",
        "pageText": sample_text,
        "interactive": [],
    }

    profile = extract_leetcode_profile(obs)
    assert profile["username"] == "akash_coder"
    assert profile["total_solved"] == 584
    assert profile["easy_solved"] == 240
    assert profile["medium_solved"] == 284
    assert profile["hard_solved"] == 60
    assert profile["contest_rating"] == 1845.6
    assert profile["global_ranking"] == 32150
    assert "50 Days Badge 2026" in profile["badges"]

    report = format_leetcode_report(profile)
    assert "akash_coder" in report
    assert "584" in report
    assert "1845.6" in report
    assert "https://leetcode.com/u/akash_coder/" in report


def test_extract_unrated_user_profile():
    sample_text = """
    ### new_dev_2026
    
    Solved 15 / 3240
    Easy 12 / 820
    Medium 3 / 1700
    Hard 0 / 720
    
    Contest Rating: Unrated
    Global Ranking: Unavailable
    
    Badges 0
    """

    obs = {
        "url": "https://leetcode.com/u/new_dev_2026/",
        "title": "new_dev_2026 - LeetCode Profile",
        "pageText": sample_text,
        "interactive": [],
    }

    profile = extract_leetcode_profile(obs)
    assert profile["username"] == "new_dev_2026"
    assert profile["total_solved"] == 15
    assert profile["easy_solved"] == 12
    assert profile["medium_solved"] == 3
    assert profile["hard_solved"] == 0
    # Strict requirement: Distinguish unavailable / unrated from 0!
    assert profile["contest_rating"] != 0
    assert "unrated" in str(profile["contest_rating"]).lower()
    assert profile["global_ranking"] != 0

    report = format_leetcode_report(profile)
    assert "Unrated" in report
    assert "Unavailable" in report

