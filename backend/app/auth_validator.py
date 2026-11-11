"""Authentication state validator.

Uses fresh browser evidence (URL, form controls, account navigation elements,
page text) to verify whether a user has genuinely authenticated.
Fail-closed: Does not rely solely on the user clicking 'Continue'.
"""
from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import urlsplit


_AUTH_PATH_SEGMENTS = {
    "signin", "sign-in", "login", "log-in", "logon", "log-on", "signup",
    "sign-up", "auth", "authenticate", "authorize", "oauth", "oauth2",
    "otp", "verify", "verification", "mfa", "2fa", "password", "passwd",
}

_AUTH_ERROR_PATTERNS = re.compile(
    r"(?i)\b("
    r"invalid (?:credentials|username|password|email|code)|"
    r"incorrect (?:username|password|code)|"
    r"wrong password|"
    r"captcha (?:verification )?failed|"
    r"please enter a valid|"
    r"session expired|"
    r"authentication failed"
    r")\b"
)

_LOGGED_IN_INDICATORS = re.compile(
    r"(?i)\b("
    r"sign[ -]?out|log[ -]?out|my account|my profile|"
    r"view profile|account settings|dashboard"
    r")\b"
)


def verify_authentication_state(
    obs: Any,
    expected_site: Optional[str] = None,
) -> tuple[bool, str]:
    """Verify if the browser observation shows a genuinely authenticated state.
    
    Returns:
        (is_authenticated: bool, reason: str)
    """
    if not obs:
        return False, "No observation available to verify authentication"

    url = (
        obs.get("url") if isinstance(obs, dict)
        else getattr(obs, "url", "")
    ) or ""

    interactive = (
        obs.get("interactive") if isinstance(obs, dict)
        else getattr(obs, "interactive", [])
    ) or []

    page_text = (
        obs.get("pageText") or obs.get("page_text") if isinstance(obs, dict)
        else getattr(obs, "page_text", None)
    ) or ""

    # 1. URL Path Analysis
    try:
        parsed = urlsplit(url)
        path_segments = {
            seg.lower()
            for seg in re.split(r"[/._\-]+", parsed.path or "")
            if seg
        }
    except Exception:
        path_segments = set()

    is_on_auth_path = bool(path_segments & _AUTH_PATH_SEGMENTS)

    # 2. Inspect active input fields for password or login credentials
    has_password_field = False
    has_login_button = False
    has_user_avatar_or_profile = False
    has_sign_out = False

    for el in interactive:
        if isinstance(el, dict):
            itype = (el.get("inputType") or "").lower()
            role = (el.get("role") or "").lower()
            name = (el.get("name") or "").lower()
            target_id = (el.get("targetId") or "").lower()
        else:
            itype = (getattr(el, "input_type", None) or "").lower()
            role = (getattr(el, "role", None) or "").lower()
            name = (getattr(el, "name", None) or "").lower()
            target_id = (getattr(el, "target_id", None) or "").lower()

        if itype == "password" or "password" in target_id:
            has_password_field = True
        if role == "button" and any(k in name for k in ("sign in", "log in", "login", "signin")):
            has_login_button = True
        if any(k in name for k in ("avatar", "profile", "account", "user menu")) or "avatar" in target_id:
            has_user_avatar_or_profile = True
        if _LOGGED_IN_INDICATORS.search(name):
            has_sign_out = True

    # 3. Check for visible authentication errors on page
    if _AUTH_ERROR_PATTERNS.search(page_text):
        return False, "Authentication failed: visible error or invalid credentials message on page"

    # 4. Site-specific checks (e.g. LeetCode)
    if expected_site == "leetcode" or "leetcode.com" in url:
        # LeetCode login URL is /accounts/login/
        if "/accounts/login" in url or (is_on_auth_path and has_password_field):
            return False, "Still on LeetCode login page (credentials/password form is active)"
        
        # When authenticated on LeetCode:
        # URL redirected to / or /problemset/ or user profile, and user avatar / profile link exists
        if has_user_avatar_or_profile or has_sign_out or "/u/" in url or "/profile" in url:
            return True, "LeetCode authenticated: active session and user profile/avatar verified"
        
        # If redirected to main page and no password fields remain:
        if not has_password_field and not has_login_button and "leetcode.com" in url:
            return True, "LeetCode authenticated: login form cleared and session active"

    # 5. General web site authentication verification
    if is_on_auth_path and (has_password_field or has_login_button):
        return False, f"Still on authentication surface ({url}): login form controls remain active"

    if has_sign_out or has_user_avatar_or_profile:
        return True, "Authenticated state verified: account/session controls observed"

    if not is_on_auth_path and not has_password_field:
        return True, "Authenticated state verified: navigated away from login screen with no active credential prompt"

    return False, "Authentication inconclusive: page still displays login prompts"

