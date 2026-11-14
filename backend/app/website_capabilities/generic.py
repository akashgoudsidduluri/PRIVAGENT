"""Generic Website Capability.

Fallback capability for domains without specialized rules.
Uses heuristic DOM analysis, password field detection, and general payment protection.
"""
from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlsplit

from ..auth_validator import verify_authentication_state
from .base import ActionRiskLevel, BaseWebsiteCapability


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
