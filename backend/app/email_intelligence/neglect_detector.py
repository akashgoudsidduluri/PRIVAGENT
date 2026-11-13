"""Detection of important emails that the user may be neglecting."""
from __future__ import annotations

import time
from typing import Optional
from .models import EmailClassificationResult, EmailItem, NotificationPolicy


def evaluate_email_neglect(
    email: EmailItem,
    classification: EmailClassificationResult,
    policy: Optional[NotificationPolicy] = None,
    now: Optional[float] = None,
) -> Optional[dict[str, str | float]]:
    """Determine whether an important email is being neglected.

    Returns notification details dict if neglected, or None if handled / not neglected.
    """
    if policy is None:
        policy = NotificationPolicy()
    current_time = now if now is not None else time.time()

    # 2. Already replied emails are HANDLED; never flag as neglected
    if email.is_replied:
        return None

    # 3. Check for custom user-requested keywords/topics (e.g. internships, jobs)
    matched_keyword = None
    if getattr(policy, "custom_keywords", None):
        full_text = f"{email.subject} {email.snippet} {email.sender}".lower()
        for kw in policy.custom_keywords:
            kw_clean = kw.lower().rstrip("s")
            if len(kw_clean) >= 3 and (kw_clean in full_text or kw.lower() in full_text):
                matched_keyword = kw
                break

    if matched_keyword:
        return {
            "message_id": email.id,
            "sender": email.sender,
            "subject": email.subject,
            "importance_reason": f"Matches your monitored topic '{matched_keyword}'.",
            "neglect_reason": f"New email related to '{matched_keyword}' requires attention.",
            "received_at": email.received_at,
            "deadline": classification.deadline or "Action requested",
            "recommended_action": f"Review email regarding {matched_keyword}",
        }

    # 4. For standard checks, non-important emails are not flagged as neglected
    if classification.category != "important":
        return None

    elapsed_seconds = max(0.0, current_time - email.received_at)
    threshold = float(policy.neglect_threshold_seconds)

    has_deadline = bool(classification.deadline)
    requires_response = bool(classification.required_action) or classification.subcategory in ("interview", "recruitment", "personal")
    is_unread = email.is_unread

    # Check neglect criteria:
    # A) Interview invitation requiring confirmation (Scenario 2)
    # B) Approaching deadline or explicit required action (Scenarios 1 & 3)
    # C) Unreplied required action / personal inquiry after threshold (Scenario 4)
    # D) Unread important message after threshold
    is_neglected = False
    neglect_reason = ""

    is_interview = "interview" in (email.subject + " " + email.snippet).lower() or classification.subcategory == "interview"

    if is_interview and (requires_response or is_unread or elapsed_seconds >= threshold):
        is_neglected = True
        neglect_reason = "Interview invitation requires confirmation, but no reply has been detected."
        if has_deadline:
            neglect_reason += f" (Scheduled: {classification.deadline})"
    elif has_deadline:
        is_neglected = True
        neglect_reason = f"Email contains a deadline ({classification.deadline}) and remains unattended."
    elif requires_response and (elapsed_seconds >= threshold or is_unread):
        is_neglected = True
        if classification.subcategory == "recruitment":
            neglect_reason = "Job or recruitment inquiry awaiting your response."
        else:
            neglect_reason = "Message appears to require a response, but no user reply has been detected."
    elif is_unread and elapsed_seconds >= threshold:
        is_neglected = True
        neglect_reason = f"Important {classification.subcategory} message has remained unread for over {int(elapsed_seconds / 3600)} hours."

    if not is_neglected:
        return None

    importance_reason = classification.reasoning or f"Classified as important ({classification.subcategory})."
    recommended_action = classification.recommended_action or (
        "Review and reply" if requires_response else "Review email"
    )

    return {
        "message_id": email.id,
        "sender": email.sender,
        "subject": email.subject,
        "importance_reason": importance_reason,
        "neglect_reason": neglect_reason,
        "received_at": email.received_at,
        "deadline": classification.deadline or "",
        "recommended_action": recommended_action,
    }
