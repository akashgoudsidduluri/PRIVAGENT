"""LeetCode Website Capability.

Encapsulates LeetCode authentication logic, profile identification,
submission safety policies, and URL route intelligence.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from .base import ActionRiskLevel, BaseWebsiteCapability


class LeetCodeCapability(BaseWebsiteCapability):
    service_id = "leetcode"
    display_name = "LeetCode"
    primary_domain = "leetcode.com"
    allowed_domains = ["leetcode.com"]
    login_url = "https://leetcode.com/accounts/login/"
    auth_type = "browser_cookie"

    def check_requires_auth(
        self,
        action_name: str,
        url: str,
        task_instruction: str = "",
    ) -> bool:
        """LeetCode requires authentication for checking user submissions, submitting code, or viewing personal stats."""
        lower_task = (task_instruction or "").lower()
        lower_url = (url or "").lower()

        # Pure reading/viewing problems does not require auth
        if any(term in lower_task for term in ("read", "view", "open", "show", "describe", "look at")) and not any(
            sub in lower_task for sub in ("submit", "submission", "my ", "haven't")
        ):
            return False

        # Explicit submission or personal tracking
        if any(term in lower_task for term in (
            "submit", "submission", "submissions", "haven't made a submission",
            "haven't submitted", "my progress", "my leetcode", "check my",
            "daily challenge", "streak", "login"
        )):
            return True

        # Auth or user specific paths
        if any(path in lower_url for path in (
            "/accounts/login", "/submissions", "/profile", "/progress"
        )):
            return True

        return False

    def verify_authentication(
        self,
        observation: Any,
    ) -> tuple[bool, Optional[str], str]:
        """Verify whether the browser observation confirms an active LeetCode session."""
        if not observation:
            return False, None, "No observation provided"

        url = (
            observation.get("url") if isinstance(observation, dict)
            else getattr(observation, "url", "")
        ) or ""

        interactive = (
            observation.get("interactive") if isinstance(observation, dict)
            else getattr(observation, "interactive", [])
        ) or []

        page_text = (
            observation.get("pageText") or observation.get("page_text") if isinstance(observation, dict)
            else getattr(observation, "page_text", None)
        ) or ""

        # 1. Negative checks: still on login page or login error
        if "/accounts/login" in url.lower():
            if any("invalid" in page_text.lower() or "incorrect" in page_text.lower() for _ in [1]):
                return False, None, "LeetCode login failed: incorrect credentials or captcha required"
            return False, None, "Still on LeetCode login page"

        # Check for active password inputs
        for el in interactive:
            itype = (el.get("inputType") or "").lower() if isinstance(el, dict) else (getattr(el, "input_type", None) or "").lower()
            if itype == "password":
                return False, None, "Still displaying active password input field"

        # 2. Positive checks: User avatar, account menu, or /u/<username> link
        username = None
        has_avatar = False
        has_sign_out = False

        # Look in URL for user profile: leetcode.com/u/<username>
        u_match = re.search(r"leetcode\.com/u/([a-zA-Z0-9_\-]+)", url)
        if u_match:
            username = u_match.group(1)

        for el in interactive:
            name = (el.get("name") or "").lower() if isinstance(el, dict) else (getattr(el, "name", None) or "").lower()
            target_id = (el.get("targetId") or "").lower() if isinstance(el, dict) else (getattr(el, "target_id", None) or "").lower()
            href = (el.get("href") or "").lower() if isinstance(el, dict) else (getattr(el, "href", None) or "").lower()

            if "/u/" in href:
                href_match = re.search(r"/u/([a-zA-Z0-9_\-]+)", href)
                if href_match and not username:
                    username = href_match.group(1)
                has_avatar = True

            if any(term in name or term in target_id for term in ("avatar", "profile", "account-menu", "nav-user")):
                has_avatar = True

            if any(term in name for term in ("sign out", "signout", "log out", "logout")):
                has_sign_out = True

        # Check page text for user handle or profile markers
        if has_avatar or has_sign_out or username:
            verified_user = username or "active_user"
            return True, verified_user, f"LeetCode authenticated: active session verified (account: {verified_user})"

        # Fallback: navigated past login on leetcode.com without login prompts
        if "leetcode.com" in url.lower() and not any(k in page_text.lower() for k in ("sign in", "log in", "create account")):
            return True, username or "leetcode_user", "LeetCode authenticated: navigated to portal with active session"

        return False, None, "LeetCode authentication inconclusive: profile or session token not found"

    def check_action_risk(
        self,
        action_name: str,
        action_params: dict[str, Any],
        url: str,
    ) -> tuple[str, str]:
        """Check safety risk of action on LeetCode."""
        # Code submissions are consequential, but authorized if part of directive
        target = str(action_params.get("targetId") or action_params.get("text") or "").lower()
        if "submit" in target:
            return ActionRiskLevel.TASK_AUTHORIZED, "Code submission authorized by user directive"
        return ActionRiskLevel.TASK_AUTHORIZED, "Standard LeetCode interaction"
