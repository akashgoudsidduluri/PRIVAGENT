"""Just-in-Time Authentication Manager.

Orchestrates proactive credential checks, user login guidance, positive DOM verification,
and automatic session encryption without requiring pre-configuration in integration settings.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from .website_capabilities import (
    BaseWebsiteCapability,
    get_capability_for_domain,
    get_capability_for_service,
    resolve_capability,
)
from .secure_session_manager import SecureSessionManager, get_secure_session_manager

logger = logging.getLogger("privagent.auth_manager")


class AuthenticationManager:
    """Coordinates JIT authentication checks and session lifecycle."""

    def __init__(self, session_manager: Optional[SecureSessionManager] = None):
        self._session_mgr = session_manager or get_secure_session_manager()

    @property
    def session_manager(self) -> SecureSessionManager:
        return self._session_mgr

    def check_auth_needed(
        self,
        task_instruction: str,
        target_url: Optional[str] = None,
    ) -> tuple[bool, Optional[BaseWebsiteCapability], Optional[str]]:
        """Evaluate if a task requires just-in-time authentication before proceeding.

        Returns:
            (needs_auth: bool, capability: Optional[BaseWebsiteCapability], guidance_msg: Optional[str])
        """
        cap: Optional[BaseWebsiteCapability] = None

        if target_url:
            cap = get_capability_for_domain(target_url)

        if not cap or cap.service_id == "generic":
            resolved = resolve_capability(task_instruction)
            if resolved:
                cap = resolved

        if not cap:
            return False, None, None

        # Check if the specific action requires authentication
        requires_auth = cap.check_requires_auth(
            action_name="init",
            url=target_url or cap.login_url,
            task_instruction=task_instruction,
        )

        if not requires_auth:
            return False, cap, None

        # Check if we already have an active, non-expired session
        if self._session_mgr.is_session_valid(cap.service_id):
            sess = self._session_mgr.get_session(cap.service_id)
            account = sess.get("account_identifier") if sess else None
            logger.info("Found existing active session for %s (account=%s)", cap.service_id, account)
            return False, cap, None

        # Authentication is required and no active session exists
        prompt_msg = cap.get_auth_prompt_message()
        return True, cap, prompt_msg

    def verify_and_save_session(
        self,
        capability: BaseWebsiteCapability,
        observation: Any,
    ) -> tuple[bool, Optional[str], str]:
        """Inspect live browser observation, verify login, and persist encrypted session."""
        is_auth, account_id, reason = capability.verify_authentication(observation)

        if is_auth:
            self._session_mgr.save_session(
                service_id=capability.service_id,
                domain=capability.primary_domain,
                account_identifier=account_id or "verified_account",
                auth_type=capability.auth_type,
            )
            logger.info("Successfully verified and saved session for %s (%s)", capability.service_id, account_id)
            return True, account_id, reason

        return False, None, reason

    def get_session(self, service_id_or_domain: str) -> Optional[dict[str, Any]]:
        """Retrieve sanitized session metadata."""
        return self._session_mgr.get_session(service_id_or_domain)

    def invalidate_session(self, service_id_or_domain: str, reason: str = "invalidated") -> bool:
        """Invalidate session upon explicit logout or expiration."""
        return self._session_mgr.invalidate_session(service_id_or_domain, reason=reason)


# Global singleton
_default_auth_manager: Optional[AuthenticationManager] = None


def get_auth_manager() -> AuthenticationManager:
    """Return the global AuthenticationManager singleton."""
    global _default_auth_manager
    if _default_auth_manager is None:
        _default_auth_manager = AuthenticationManager()
    return _default_auth_manager
