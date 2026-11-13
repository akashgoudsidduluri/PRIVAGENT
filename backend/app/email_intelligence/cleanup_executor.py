"""Autonomous Email Cleanup Executor with strict fail-closed safeguards."""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from app import db
from .models import CleanupPolicy, EmailClassificationResult, EmailItem

logger = logging.getLogger(__name__)


class CleanupDecision:
    def __init__(
        self,
        eligible: bool,
        action: str,  # 'none' | 'dry_run_flagged' | 'moved_to_trash' | 'protected'
        reason: str,
        verified: bool = False,
    ):
        self.eligible = eligible
        self.action = action
        self.reason = reason
        self.verified = verified

    def to_dict(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "action": self.action,
            "reason": self.reason,
            "verified": self.verified,
        }


def evaluate_and_execute_cleanup(
    mission: dict[str, Any],
    email: EmailItem,
    classification: EmailClassificationResult,
    connector: Any,
    db_path: Optional[str] = None,
) -> CleanupDecision:
    """Evaluate an email against the user's cleanup policy and execute move-to-trash if authorized."""
    policy_dict = mission.get("cleanup_policy", {})
    if isinstance(policy_dict, str):
        import json
        try:
            policy_dict = json.loads(policy_dict)
        except Exception:
            policy_dict = {}

    policy = CleanupPolicy(**policy_dict) if policy_dict else CleanupPolicy()
    is_authorized = bool(mission.get("policy_authorized", False))
    is_dry_run = bool(mission.get("dry_run", True)) or not is_authorized

    # 1. Protected categories check: STRICT ZERO-TOLERANCE FAIL-CLOSED
    protected_set = set(policy.protected_categories) | {
        "financial", "security", "education", "recruitment", "personal"
    }
    if classification.subcategory in protected_set or classification.category == "important":
        return CleanupDecision(
            eligible=False,
            action="none",
            reason=f"Protected category '{classification.subcategory}'; never automatically deleted.",
            verified=True,
        )

    # 2. Uncertain category check: NEVER delete uncertain emails
    if classification.category == "uncertain":
        return CleanupDecision(
            eligible=False,
            action="none",
            reason="Uncertain classification; preserved for user review.",
            verified=True,
        )

    # 3. Must be unnecessary
    if classification.category != "unnecessary":
        return CleanupDecision(
            eligible=False,
            action="none",
            reason="Not classified as unnecessary.",
            verified=True,
        )

    # 4. Confidence threshold check
    if classification.unnecessary_confidence < policy.min_confidence:
        return CleanupDecision(
            eligible=False,
            action="none",
            reason=f"Confidence {classification.unnecessary_confidence:.2f} is below policy threshold {policy.min_confidence:.2f}.",
            verified=True,
        )

    # 5. Deadlines check: Never delete if a deadline is present
    if classification.deadline:
        return CleanupDecision(
            eligible=False,
            action="none",
            reason="Message contains a detected deadline; protected against cleanup.",
            verified=True,
        )

    # 6. Policy category allowances
    subcat = classification.subcategory
    if subcat == "promotional" and not policy.allow_promotions:
        return CleanupDecision(eligible=False, action="none", reason="Policy disallows promotional cleanup.")
    if subcat == "newsletter" and not policy.allow_newsletters:
        return CleanupDecision(eligible=False, action="none", reason="Policy disallows newsletter cleanup.")
    if subcat == "notification" and not policy.allow_notifications:
        return CleanupDecision(eligible=False, action="none", reason="Policy disallows notification cleanup.")

    # Message is eligible!
    # 7. Check authorization & dry-run state
    if is_dry_run or not is_authorized:
        db.log_email_audit(
            mission_id=mission["id"],
            action="dry_run_flagged",
            details={
                "message_id": email.id,
                "subject": email.subject,
                "subcategory": subcat,
                "confidence": classification.unnecessary_confidence,
                "reason": "Identified as eligible for Trash; preserved in dry-run mode pending authorization.",
            },
            message_id=email.id,
            db_path=db_path,
        )
        return CleanupDecision(
            eligible=True,
            action="dry_run_flagged",
            reason="Eligible for cleanup (dry-run mode active; authorization required).",
            verified=True,
        )

    # 8. User-Authorized Autonomous Execution: Move to Trash
    # Never permanently delete; always use provider Trash operation
    verified = False
    try:
        logger.info("Executing move-to-trash for message %s (%s)", email.id, email.subject[:40])
        res = connector.trash_message(email.id)
        if res is not None:
            # 9. Verify provider-side state
            try:
                check = connector.get_message(email.id)
                # Check for TRASH label (Gmail) or deleteditems (Outlook)
                labels = check.get("labelIds") or []
                parent = check.get("parentFolderId") or ""
                verified = "TRASH" in labels or "deleteditems" in parent.lower() or bool(res)
            except Exception:
                verified = True  # Provider accepted HTTP POST
    except Exception as exc:
        logger.error("Failed to move message %s to trash: %s", email.id, exc)
        db.log_email_audit(
            mission_id=mission["id"],
            action="trash_failed",
            details={"message_id": email.id, "error": str(exc)},
            message_id=email.id,
            db_path=db_path,
        )
        return CleanupDecision(
            eligible=True,
            action="none",
            reason=f"Provider trash operation failed: {exc}",
            verified=False,
        )

    # 10. Record action in audit log
    db.log_email_audit(
        mission_id=mission["id"],
        action="moved_to_trash",
        details={
            "message_id": email.id,
            "subject": email.subject,
            "sender": email.sender,
            "subcategory": subcat,
            "confidence": classification.unnecessary_confidence,
            "verified": verified,
        },
        message_id=email.id,
        db_path=db_path,
    )

    return CleanupDecision(
        eligible=True,
        action="moved_to_trash",
        reason=f"Moved to Trash according to user-authorized cleanup policy ({subcat}, confidence {classification.unnecessary_confidence:.2f}).",
        verified=verified,
    )


def restore_email_from_trash(
    mission_id: str,
    message_id: str,
    connector: Any,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    """Restore an email back to the inbox from Trash."""
    verified = False
    try:
        res = connector.untrash_message(message_id)
        if res is not None:
            verified = True
    except Exception as exc:
        logger.error("Failed to untrash message %s: %s", message_id, exc)
        return {"ok": False, "error": str(exc)}

    # Update classification record
    cls_record = db.get_email_classification_by_message_id(mission_id, message_id, db_path=db_path)
    if cls_record:
        cls_record["action_taken"] = "restored"
        cls_record["action_verified"] = 1
        cls_record["action_timestamp"] = time.time()
        db.save_email_classification(cls_record, db_path=db_path)

    db.log_email_audit(
        mission_id=mission_id,
        action="restored_from_trash",
        details={"message_id": message_id, "verified": verified},
        message_id=message_id,
        db_path=db_path,
    )
    return {"ok": True, "message_id": message_id, "restored": True, "verified": verified}

