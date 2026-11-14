"""Website Capabilities and Safety Boundaries for Just-in-Time Authentication.

Encapsulates service-specific domain intelligence, authentication requirements,
positive DOM evidence verification, risk classification, and zero-agent safety boundaries.
"""
from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from typing import Any, Optional
from urllib.parse import urlsplit

from .auth_validator import verify_authentication_state

logger = logging.getLogger("privagent.website_capabilities")


class ActionRiskLevel:
    AUTONOMOUS = "autonomous"            # Public reads, navigation, schedule lookups
    TASK_AUTHORIZED = "task_authorized"  # Actions commanded directly in prompt (typing query, clicking filter)
    CONFIRMATION_REQUIRED = "confirmation_required"  # Consequential actions (initiating booking, destructive edits)
    PROHIBITED = "prohibited"            # Strictly prohibited (entering credit card, OTP, CVV, clicking Pay)


class BaseWebsiteCapability(ABC):
    """Abstract base class for domain capabilities."""

    service_id: str
    display_name: str
    primary_domain: str
    allowed_domains: list[str]
    login_url: str
    auth_type: str = "browser_cookie"

    @abstractmethod
    def check_requires_auth(
        self,
        action_name: str,
        url: str,
        task_instruction: str = "",
    ) -> bool:
        """Return True if this operation or destination requires prior authentication."""
        pass

    @abstractmethod
    def verify_authentication(
        self,
        observation: Any,
    ) -> tuple[bool, Optional[str], str]:
        """Verify if observation proves active authenticated state.

        Returns:
            (is_authenticated: bool, account_identifier: Optional[str], reason: str)
        """
        pass

    def check_action_risk(
        self,
        action_name: str,
        action_params: dict[str, Any],
        url: str,
    ) -> tuple[str, str]:
        """Determine safety risk level of an action on this service.

        Returns:
            (risk_level: str, reason: str)
        """
        param_str = str(action_params).lower()
        if any(term in param_str for term in ("cvv", "otp", "card_number", "card number", "pay now", "make payment", "proceed to pay")):
            return ActionRiskLevel.PROHIBITED, "Payment and financial transactions must be completed directly by the user."
        return ActionRiskLevel.TASK_AUTHORIZED, "Standard interaction"

    def get_auth_prompt_message(self) -> str:
        """User-friendly message explaining the login requirement."""
        return (
            f"I need you to log in to {self.display_name} first. "
            f"I've opened the login page in your browser. "
            f"Once you've completed sign-in, click 'I Have Completed This' to continue your task."
        )


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
        target = str(action_params.get("targetId") or action_params.get("text") or "").lower()
        if "submit" in target:
            return ActionRiskLevel.TASK_AUTHORIZED, "Code submission authorized by user directive"
        return ActionRiskLevel.TASK_AUTHORIZED, "Standard LeetCode interaction"


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


class GenericWebsiteCapability(BaseWebsiteCapability):
    def __init__(self, domain: str = "generic"):
        self.service_id = domain.split(".")[0].lower()
        self.display_name = domain
        self.primary_domain = domain
        self.allowed_domains = [domain]
        self.login_url = f"https://{domain}/login"
        self.auth_type = "browser_cookie"

    def check_requires_auth(
        self,
        action_name: str,
        url: str,
        task_instruction: str = "",
    ) -> bool:
        """Generic heuristic: check if action target or URL is on an auth path."""
        lower_task = (task_instruction or "").lower()
        lower_url = (url or "").lower()

        if any(term in lower_task for term in ("login", "sign in", "log in", "authenticate", "my account", "my orders")):
            return True

        parsed = urlsplit(lower_url)
        path = parsed.path
        if any(seg in path for seg in ("/login", "/signin", "/auth", "/session", "/account/login")):
            return True

        return False

    def verify_authentication(
        self,
        observation: Any,
    ) -> tuple[bool, Optional[str], str]:
        """Verify using the core auth_validator module."""
        is_auth, reason = verify_authentication_state(observation, expected_site=self.service_id)
        return is_auth, "user" if is_auth else None, reason

    def check_action_risk(
        self,
        action_name: str,
        action_params: dict[str, Any],
        url: str,
    ) -> tuple[str, str]:
        param_str = str(action_params).lower()
        if any(k in param_str for k in ("pay now", "checkout", "cvv", "card number", "place order")):
            return ActionRiskLevel.CONFIRMATION_REQUIRED, "Transactional action requires user confirmation."
        return ActionRiskLevel.TASK_AUTHORIZED, "Standard action"


_CAPABILITIES: dict[str, BaseWebsiteCapability] = {
    "leetcode": LeetCodeCapability(),
    "irctc": IrctcCapability(),
}

_DOMAIN_MAPPINGS: dict[str, str] = {
    "leetcode.com": "leetcode",
    "irctc.co.in": "irctc",
    "www.irctc.co.in": "irctc",
}


def get_capability_for_service(service_id: str) -> Optional[BaseWebsiteCapability]:
    """Retrieve capability by service id."""
    return _CAPABILITIES.get(service_id.strip().lower())


def get_capability_for_domain(domain_or_url: str) -> BaseWebsiteCapability:
    """Retrieve capability by domain or URL, defaulting to GenericWebsiteCapability."""
    raw = domain_or_url.strip().lower()
    if "://" in raw:
        try:
            domain = urlsplit(raw).netloc.split(":")[0]
        except Exception:
            domain = raw
    else:
        domain = raw.split("/")[0].split(":")[0]

    for d_key, s_key in _DOMAIN_MAPPINGS.items():
        if d_key in domain or domain in d_key:
            return _CAPABILITIES[s_key]

    for s_key, cap in _CAPABILITIES.items():
        if s_key in domain or domain in cap.primary_domain:
            return cap

    return GenericWebsiteCapability(domain=domain or "generic")


def resolve_capability(query_or_instruction: str) -> Optional[BaseWebsiteCapability]:
    """Infer the primary capability from a natural language directive or URL."""
    text = (query_or_instruction or "").lower()

    if any(k in text for k in ("leetcode", "two sum", "hackerrank")):
        return _CAPABILITIES["leetcode"]

    if any(k in text for k in ("irctc", "train ticket", "trains from", "trains to", "railway ticket")):
        return _CAPABILITIES["irctc"]

    for d_key, s_key in _DOMAIN_MAPPINGS.items():
        if d_key in text:
            return _CAPABILITIES[s_key]

    return None


__all__ = [
    "ActionRiskLevel",
    "BaseWebsiteCapability",
    "GenericWebsiteCapability",
    "IrctcCapability",
    "LeetCodeCapability",
    "get_capability_for_domain",
    "get_capability_for_service",
    "resolve_capability",
]
