"""Synchronization and Execution Engine for Continuous Email Monitoring."""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from app import db
from app.hub import hub
from .classifier import classify_email
from .cleanup_executor import evaluate_and_execute_cleanup
from .models import EmailItem, NotificationPolicy
from .neglect_detector import evaluate_email_neglect
from .controlled_mailbox import ControlledTestMailbox

logger = logging.getLogger(__name__)

# Global test mailbox instance for sandbox demo/testing (None in production; set by tests or on demand)
_demo_mailbox: Optional[ControlledTestMailbox] = None


def get_demo_mailbox() -> ControlledTestMailbox:
    global _demo_mailbox
    if _demo_mailbox is None:
        _demo_mailbox = ControlledTestMailbox()
    return _demo_mailbox


def _get_connector_for_mission(mission: dict[str, Any]):
    """Return a live OAuth connector if configured, or a controlled test mailbox connector if demo."""
    from app.connectors.oauth import oauth_manager
    provider_name = mission.get("provider", "gmail").lower()
    connector_key = "gmail" if provider_name == "gmail" else "outlook"
    account_email = str(mission.get("account_email", "")).lower()

    # Test/demo accounts always use the controlled test mailbox
    is_test_account = (
        account_email in ("tester.privagent@gmail.com", "demo.privagent@gmail.com", "api.tester@gmail.com")
        or "privagent" in account_email
        or "tester" in account_email
        or provider_name in ("demo", "sandbox")
    )
    if not is_test_account:
        conn = oauth_manager.connector(connector_key)
        if getattr(conn, "configured", False):
            return conn, "live"

    # Fallback to controlled protocol test mailbox if live OAuth is not configured
    box = _demo_mailbox or get_demo_mailbox()
    client = box.make_mock_client()
    if provider_name == "gmail":
        from app.connectors import GmailConnector
        return GmailConnector("sandbox-demo-token", client=client), "sandbox"
    else:
        from app.connectors import OutlookConnector
        return OutlookConnector("sandbox-demo-token", client=client), "sandbox"


async def run_email_mission_tick(mission_id: str, db_path: Optional[str] = None) -> dict[str, Any]:
    """Execute one synchronization, classification, cleanup, and neglect-detection tick."""
    db.init_db(db_path)
    mission = db.get_email_mission(mission_id, db_path=db_path)
    if not mission or mission.get("status") != "active":
        return {"status": "skipped", "reason": "mission not found or not active"}

    account_email = mission.get("account_email", "user@example.com")
    task_record = hub.new_task(
        f"Autonomous email scan for {account_email}",
        chat_id=mission.get("conversation_id"),
    )
    task_record.state = "running"
    task_record.message = f"Connecting to email ({account_email})..."
    await hub.broadcast_progress(task_record)

    connector, mode = _get_connector_for_mission(mission)
    logger.info("Email tick for mission %s in mode: %s", mission_id, mode)

    # 1. Mailbox Sync
    task_record.message = "Synchronizing mailbox..."
    await hub.broadcast_progress(task_record)

    email_items: list[EmailItem] = []
    new_cursor = mission.get("sync_cursor") or ""

    if mode == "sandbox":
        box = _demo_mailbox or get_demo_mailbox()
        email_items = box.to_email_items()
        new_cursor = str(box.history_id)
    else:
        # Live connector incremental sync
        try:
            if mission.get("provider") == "gmail":
                resp = connector.list_messages(limit=50)
                msgs = resp.get("messages", [])
                for m in msgs:
                    full = connector.get_message(m["id"])
                    headers = {h["name"].lower(): h["value"] for h in full.get("payload", {}).get("headers", [])}
                    email_items.append(
                        EmailItem(
                            id=full["id"],
                            thread_id=full.get("threadId", full["id"]),
                            sender=headers.get("from", ""),
                            recipient=headers.get("to", ""),
                            subject=headers.get("subject", ""),
                            snippet=full.get("snippet", ""),
                            body_text=full.get("snippet", ""),
                            received_at=float(full.get("internalDate", time.time() * 1000)) / 1000.0,
                            is_unread="UNREAD" in full.get("labelIds", []),
                            is_replied=False,
                            labels=full.get("labelIds", []),
                            raw_headers=headers,
                        )
                    )
                new_cursor = str(int(time.time()))
            else:
                # Outlook MS Graph
                vals = connector.list_messages(limit=50)
                for v in vals:
                    email_items.append(
                        EmailItem(
                            id=v.get("id"),
                            thread_id=v.get("id"),
                            sender=v.get("from", {}).get("emailAddress", {}).get("address", ""),
                            subject=v.get("subject", ""),
                            snippet=v.get("bodyPreview", ""),
                            received_at=time.time(),
                            is_unread=v.get("isRead") is False,
                            is_replied=False,
                        )
                    )
                new_cursor = str(int(time.time()))
        except Exception as exc:
            logger.error("Live mailbox sync failed: %s", exc)
            task_record.state = "failed"
            task_record.message = f"Mailbox sync failed: {exc}"
            await hub.broadcast_progress(task_record)
            db.log_email_audit(mission_id, "sync_failed", {"error": str(exc)}, db_path=db_path)
            return {"status": "failed", "error": str(exc)}

    # 2. Classification & Processing
    task_record.message = "Analyzing incoming messages..."
    await hub.broadcast_progress(task_record)

    total_synced = len(email_items)
    new_unnecessary = 0
    new_important = 0
    new_trashed = 0
    new_dry_run = 0
    new_neglected = 0

    notif_policy = NotificationPolicy(**mission.get("notification_policy", {})) if mission.get("notification_policy") else NotificationPolicy()

    for email in email_items:
        # Check existing classification
        existing = db.get_email_classification_by_message_id(mission_id, email.id, db_path=db_path)

        if existing:
            # Check for state changes (e.g. user replied or marked read)
            if existing.get("is_replied") != int(email.is_replied) or existing.get("is_unread") != int(email.is_unread):
                existing["is_replied"] = int(email.is_replied)
                existing["is_unread"] = int(email.is_unread)
                db.save_email_classification(existing, db_path=db_path)
                if email.is_replied:
                    # User handled it! Resolve any pending notification
                    notifs = db.get_email_notifications(mission_id, status="sent", db_path=db_path)
                    for n in notifs:
                        if n.get("message_id") == email.id:
                            db.update_email_notification_status(n["id"], "resolved", db_path=db_path)
            cls_result_dict = existing
            category = existing.get("category", "uncertain")
        else:
            task_record.message = f"Classifying: {email.subject[:35]}..."
            await hub.broadcast_progress(task_record)

            cls = classify_email(email)
            category = cls.category
            cls_record = {
                "mission_id": mission_id,
                "message_id": email.id,
                "thread_id": email.thread_id,
                "sender": email.sender,
                "recipient": email.recipient,
                "subject": email.subject,
                "snippet": email.snippet,
                "received_at": email.received_at,
                "category": cls.category,
                "subcategory": cls.subcategory,
                "importance_score": cls.importance_score,
                "unnecessary_confidence": cls.unnecessary_confidence,
                "reasoning": cls.reasoning,
                "deadline": cls.deadline,
                "required_action": cls.required_action,
                "is_unread": email.is_unread,
                "is_replied": email.is_replied,
                "action_taken": "none",
                "raw_headers": email.raw_headers,
            }
            cls_result_dict = db.save_email_classification(cls_record, db_path=db_path)
            db.log_email_audit(
                mission_id,
                "classified",
                {
                    "message_id": email.id,
                    "subject": email.subject,
                    "category": cls.category,
                    "subcategory": cls.subcategory,
                    "confidence": cls.unnecessary_confidence,
                    "reasoning": cls.reasoning,
                },
                message_id=email.id,
                db_path=db_path,
            )

        if category == "unnecessary":
            new_unnecessary += 1
            # 3. Cleanup Evaluation
            if cls_result_dict.get("action_taken") not in ("moved_to_trash", "restored"):
                task_record.message = "Reviewing deletion eligibility..."
                await hub.broadcast_progress(task_record)

                from .models import EmailClassificationResult
                c_obj = EmailClassificationResult(
                    category=cls_result_dict["category"],
                    subcategory=cls_result_dict.get("subcategory", "unknown"),
                    importance_score=cls_result_dict.get("importance_score", 0.0),
                    unnecessary_confidence=cls_result_dict.get("unnecessary_confidence", 0.0),
                    reasoning=cls_result_dict.get("reasoning", ""),
                    deadline=cls_result_dict.get("deadline"),
                    required_action=cls_result_dict.get("required_action"),
                )
                decision = evaluate_and_execute_cleanup(mission, email, c_obj, connector, db_path=db_path)
                if decision.action == "moved_to_trash":
                    new_trashed += 1
                    cls_result_dict["action_taken"] = "moved_to_trash"
                    cls_result_dict["action_verified"] = 1 if decision.verified else 0
                    cls_result_dict["action_timestamp"] = time.time()
                    db.save_email_classification(cls_result_dict, db_path=db_path)
                    task_record.message = f"Moved to Trash: {email.subject[:30]}..."
                    await hub.broadcast_progress(task_record)
                elif decision.action == "dry_run_flagged":
                    new_dry_run += 1
                    cls_result_dict["action_taken"] = "dry_run_flagged"
                    cls_result_dict["action_verified"] = 1
                    cls_result_dict["action_timestamp"] = time.time()
                    db.save_email_classification(cls_result_dict, db_path=db_path)

        elif category == "important":
            new_important += 1
            # 4. Neglect Detection
            task_record.message = "Detecting important emails..."
            await hub.broadcast_progress(task_record)

            from .models import EmailClassificationResult
            c_obj = EmailClassificationResult(
                category=cls_result_dict["category"],
                subcategory=cls_result_dict.get("subcategory", "unknown"),
                importance_score=cls_result_dict.get("importance_score", 0.0),
                unnecessary_confidence=cls_result_dict.get("unnecessary_confidence", 0.0),
                reasoning=cls_result_dict.get("reasoning", ""),
                deadline=cls_result_dict.get("deadline"),
                required_action=cls_result_dict.get("required_action"),
            )
            neglect = evaluate_email_neglect(email, c_obj, notif_policy)
            if neglect:
                new_neglected += 1
                # Deduplication: Check if notification already sent
                existing_notifs = [
                    n for n in db.get_email_notifications(mission_id, db_path=db_path)
                    if n.get("message_id") == email.id
                ]
                if not existing_notifs:
                    task_record.message = f"Preparing notification: {email.subject[:30]}..."
                    await hub.broadcast_progress(task_record)

                    db.save_email_notification({
                        "mission_id": mission_id,
                        "message_id": email.id,
                        "sender": neglect["sender"],
                        "subject": neglect["subject"],
                        "importance_reason": neglect["importance_reason"],
                        "neglect_reason": neglect["neglect_reason"],
                        "received_at": neglect["received_at"],
                        "deadline": neglect["deadline"],
                        "recommended_action": neglect["recommended_action"],
                    }, db_path=db_path)
                    cls_result_dict["notified"] = 1
                    db.save_email_classification(cls_result_dict, db_path=db_path)
                    db.log_email_audit(
                        mission_id,
                        "neglect_notified",
                        {
                            "message_id": email.id,
                            "subject": email.subject,
                            "deadline": neglect["deadline"],
                            "reason": neglect["neglect_reason"],
                        },
                        message_id=email.id,
                        db_path=db_path,
                    )

                    conv_id = mission.get("conversation_id")
                    if conv_id:
                        deadline_str = f"\n• **Deadline / Target:** {neglect['deadline']}" if neglect.get("deadline") else ""
                        notif_text = (
                            f"🔔 **Important Email Alert**\n\n"
                            f"• **From:** {neglect['sender']}\n"
                            f"• **Subject:** {neglect['subject']}\n"
                            f"• **Alert:** {neglect['neglect_reason']}{deadline_str}\n"
                            f"• **Recommended Action:** {neglect['recommended_action']}"
                        )
                        db.add_message(conv_id, "agent", notif_text, evidence="email_notification", task_id=task_record.task_id, db_path=db_path)
                        db.touch_chat(conv_id, db_path=db_path)

    # 5. Finalize Mission State & Next Run
    now = time.time()
    freq = mission.get("frequency_seconds", 300)
    db.update_email_mission(
        mission_id,
        {
            "last_sync_at": now,
            "next_run_at": now + freq,
            "sync_cursor": new_cursor,
        },
        db_path=db_path,
    )

    stats = db.get_email_dashboard_stats(mission_id, db_path=db_path)

    task_record.state = "completed"
    task_record.steps_done = 1
    task_record.message = (
        f"Email scan completed ({mode}): {stats.get('total_processed', 0)} messages, "
        f"{stats.get('moved_to_trash', 0)} in Trash, "
        f"{stats.get('dry_run_flagged', 0)} dry-run flagged, "
        f"{stats.get('neglected_awaiting_attention', 0)} needing attention."
    )
    await hub.broadcast_progress(task_record)

    db.log_email_audit(
        mission_id,
        "sync_completed",
        {
            "mode": mode,
            "total_synced": total_synced,
            "stats": stats,
        },
        db_path=db_path,
    )

    return {
        "status": "success",
        "mission_id": mission_id,
        "mode": mode,
        "stats": stats,
    }

