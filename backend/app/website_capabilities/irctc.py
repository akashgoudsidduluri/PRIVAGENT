"""IRCTC Website Capability.

Encapsulates IRCTC train searching, authentication requirements,
booking confirmation gates, and the Zero-Agent Payment policy.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from .base import ActionRiskLevel, BaseWebsiteCapability


class IrctcCapability(BaseWebsiteCapability):
    service_id = "irctc"
    display_name = "IRCTC (Indian Railways)"
    primary_domain = "irctc.co.in"
    allowed_domains = ["irctc.co.in", "www.irctc.co.in"]
    login_url = "https://www.irctc.co.in/nget/train-search"
    auth_type = "browser_cookie"

    def check_requires_auth(
        self,
        action_name: str,
        url: str,
        task_instruction: str = "",
    ) -> bool:
        """Searching trains and checking availability is public.
        
        Entering passenger details and proceeding to reservation requires authentication.
        """
        lower_task = (task_instruction or "").lower()
        lower_url = (url or "").lower()

        # Pure search/inquiry does not need login upfront
        if any(term in lower_task for term in ("check availability", "find trains", "train schedule", "search trains")):
            if not any(b in lower_task for b in ("book", "reserve", "ticket")):
                return False

        # Explicit booking directives require authentication
        if any(term in lower_task for term in ("book", "booking", "reserve", "reservation", "login to irctc")):
            return True

        if any(path in lower_url for path in ("/booking", "/passenger", "/payment")):
            return True

        return False

    def verify_authentication(
        self,
        observation: Any,
    ) -> tuple[bool, Optional[str], str]:
        """Verify whether an active IRCTC session exists in the browser."""
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

        # Negative checks: active login dialog or captcha
        has_password = False
        has_login_modal = False
        for el in interactive:
            itype = (el.get("inputType") or "").lower() if isinstance(el, dict) else (getattr(el, "input_type", None) or "").lower()
            name = (el.get("name") or "").lower() if isinstance(el, dict) else (getattr(el, "name", None) or "").lower()
            if itype == "password":
                has_password = True
            if "captcha" in name:
                has_login_modal = True

        if has_password and has_login_modal:
            return False, None, "IRCTC login dialog is active; user credentials and captcha required"

        # Positive checks: Welcome header or Logout button
        username = None
        has_logout = False

        # Look for "Welcome <username>" or "Logout"
        welcome_match = re.search(r"welcome\s+([a-zA-Z0-9_\-]+)", page_text, re.IGNORECASE)
        if welcome_match:
            username = welcome_match.group(1)

        for el in interactive:
            name = (el.get("name") or "").lower() if isinstance(el, dict) else (getattr(el, "name", None) or "").lower()
            if any(term in name for term in ("logout", "log out", "sign out")):
                has_logout = True
            if "my account" in name or "user profile" in name:
                has_logout = True

        if has_logout or username:
            verified_user = username or "irctc_user"
            return True, verified_user, f"IRCTC authenticated: session verified (account: {verified_user})"

        # If on train search without login popup
        if "irctc.co.in" in url and not has_password:
            # Check if user clicked login or is still guest
            if any(term in page_text.lower() for term in ("login", "sign in")):
                return False, None, "IRCTC guest mode: Login button visible, user not signed in"
            return True, "irctc_guest", "IRCTC accessible in guest search mode"

        return False, None, "IRCTC authentication inconclusive"

    def check_action_risk(
        self,
        action_name: str,
        action_params: dict[str, Any],
        url: str,
    ) -> tuple[str, str]:
        """Enforce strict Zero-Agent Payment policy and confirmation gates for IRCTC."""
        target = str(action_params.get("targetId") or action_params.get("text") or "").lower()
        param_str = str(action_params).lower()

        # Strict Zero-Agent Payment rule
        payment_triggers = ("pay & book", "proceed to pay", "make payment", "pay now", "upi", "credit card", "net banking", "debit card", "cvv", "otp")
        if any(pt in target or pt in param_str for pt in payment_triggers) or "payment" in url.lower():
            return (
                ActionRiskLevel.PROHIBITED,
                "IRCTC Payment Handoff: PrivAgent policy strictly prohibits executing financial transactions or entering payment credentials. Please complete payment securely in the browser window."
            )

        # Passenger booking confirmation gate
        if any(b in target for b in ("book now", "continue to passenger", "submit passenger")):
            return (
                ActionRiskLevel.CONFIRMATION_REQUIRED,
                "Booking ticket reservation on IRCTC: Please confirm the selected train, date, class, and fare before proceeding."
            )

        # Autonomous train search and filters
        return ActionRiskLevel.AUTONOMOUS, "Public train search and schedule lookup"
