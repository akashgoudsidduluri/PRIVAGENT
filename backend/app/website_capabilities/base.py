"""Base Website Capability Specification.

Defines the contract for service-specific domain intelligence, authentication requirements,
positive evidence verification, risk classification, and zero-agent safety boundaries.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional


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
        # Default policy: financial payments are strictly prohibited
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
