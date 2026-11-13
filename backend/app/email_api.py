"""FastAPI endpoints for Email Intelligence dashboard, control, and audit."""
from __future__ import annotations

import logging
import time
from typing import Any, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app import db
from .email_intelligence import (
    CleanupPolicy,
    NotificationPolicy,
    EmailMissionSetupInput,
    run_email_mission_tick,
    restore_email_from_trash,
    answer_email_companion_query,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/email", tags=["Email Intelligence"])


class PolicyAuthorizeInput(BaseModel):
    authorized: bool = True
    cleanup_policy: Optional[dict[str, Any]] = None


class CompanionQueryInput(BaseModel):
    query: str
    mission_id: Optional[str] = None


@router.get("/missions")
def list_missions():
    """List all email monitoring missions."""
    db.init_db()
    return db.list_email_missions()


@router.post("/missions", status_code=201)
async def create_mission(body: EmailMissionSetupInput):
    """Register and start an email monitoring mission."""
    db.init_db()
    mission = db.create_email_mission(
        account_email=body.account_email,
        provider=body.provider,
        frequency_seconds=body.frequency_seconds,
        cleanup_policy=body.cleanup_policy.model_dump(),
        notification_policy=body.notification_policy.model_dump(),
        conversation_id=body.conversation_id,
    )
    # Trigger first tick in background or inline
    await run_email_mission_tick(mission["id"])
    return db.get_email_mission(mission["id"])


@router.get("/missions/{mission_id}")
def get_mission(mission_id: str):
    """Get mission details."""
    db.init_db()
    m = db.get_email_mission(mission_id)
    if not m:
        raise HTTPException(404, "Email mission not found")
    return m


@router.post("/missions/{mission_id}/authorize")
async def authorize_cleanup_policy(mission_id: str, body: PolicyAuthorizeInput):
    """Explicitly authorize autonomous email cleanup within approved policy."""
    db.init_db()
    m = db.get_email_mission(mission_id)
    if not m:
        raise HTTPException(404, "Email mission not found")

    updates: dict[str, Any] = {
        "policy_authorized": 1 if body.authorized else 0,
        "dry_run": 0 if body.authorized else 1,
    }
    if body.cleanup_policy:
        import json
        updates["cleanup_policy_json"] = json.dumps(body.cleanup_policy)

    updated = db.update_email_mission(mission_id, updates)
    db.log_email_audit(
        mission_id,
        "policy_authorized" if body.authorized else "policy_deauthorized",
        {"authorized": body.authorized, "dry_run": not body.authorized},
    )
    return {"ok": True, "mission": updated}


@router.post("/missions/{mission_id}/pause")
def pause_mission(mission_id: str):
    """Pause continuous email monitoring."""
    db.init_db()
    updated = db.update_email_mission(mission_id, {"status": "paused"})
    if not updated:
        raise HTTPException(404, "Email mission not found")
    db.log_email_audit(mission_id, "monitoring_paused", {})
    return {"ok": True, "status": "paused"}


@router.post("/missions/{mission_id}/resume")
async def resume_mission(mission_id: str):
    """Resume continuous email monitoring and trigger an immediate tick."""
    db.init_db()
    now = time.time()
    updated = db.update_email_mission(mission_id, {"status": "active", "next_run_at": now})
    if not updated:
        raise HTTPException(404, "Email mission not found")
    db.log_email_audit(mission_id, "monitoring_resumed", {})
    await run_email_mission_tick(mission_id)
    return {"ok": True, "status": "active"}


@router.delete("/missions/{mission_id}")
def cancel_mission(mission_id: str):
    """Cancel / stop continuous email monitoring."""
    db.init_db()
    updated = db.update_email_mission(mission_id, {"status": "stopped"})
    if not updated:
        raise HTTPException(404, "Email mission not found")
    db.log_email_audit(mission_id, "monitoring_cancelled", {})
    return {"ok": True, "status": "stopped"}


@router.post("/missions/{mission_id}/sync")
async def force_sync_tick(mission_id: str):
    """Trigger an immediate sync tick for the email mission."""
    db.init_db()
    mission = db.get_email_mission(mission_id)
    if mission:
        from .connectors.oauth import oauth_manager
        prov = mission.get("provider", "gmail")
        conn_key = "gmail" if prov == "gmail" else "outlook"
        try:
            conn = oauth_manager.connector(conn_key)
            if getattr(conn, "configured", False):
                prof = conn.get_profile()
                email_addr = prof.get("emailAddress") or prof.get("mail")
                if email_addr and mission.get("account_email") != email_addr:
                    db.update_email_mission(mission_id, {"account_email": email_addr, "sync_cursor": ""})
        except Exception:
            pass
    result = await run_email_mission_tick(mission_id)
    return result


@router.get("/missions/{mission_id}/dashboard")
def get_dashboard_payload(mission_id: str):
    """Return aggregated stats and categorized lists for the frontend dashboard."""
    db.init_db()
    mission = db.get_email_mission(mission_id)
    if not mission:
        raise HTTPException(404, "Email mission not found")

    stats = db.get_email_dashboard_stats(mission_id)
    all_classifications = db.get_email_classifications(mission_id, limit=200)

    # 1. Needs Attention: Important + Unanswered / has deadline
    needs_attention = [
        m for m in all_classifications
        if m.get("category") == "important" and not m.get("is_replied")
    ]

    # 2. Handled: Important + Replied
    handled = [
        m for m in all_classifications
        if m.get("category") == "important" and bool(m.get("is_replied"))
    ]

    # 3. Unnecessary Detected: Promotional/newsletter (dry run or detected)
    unnecessary_detected = [
        m for m in all_classifications
        if m.get("category") == "unnecessary" and m.get("action_taken") != "moved_to_trash"
    ]

    # 4. Moved to Trash: Actually trashed with verification
    trashed = [
        m for m in all_classifications
        if m.get("action_taken") == "moved_to_trash"
    ]

    # 5. Uncertain: Review required
    uncertain = [
        m for m in all_classifications
        if m.get("category") == "uncertain"
    ]

    # 6. Audit History
    audit_logs = db.get_email_audit_logs(mission_id, limit=100)

    # 7. Active Notifications
    notifications = db.get_email_notifications(mission_id, limit=50)

    return {
        "mission": mission,
        "stats": stats,
        "sections": {
            "needs_attention": needs_attention,
            "handled": handled,
            "unnecessary_detected": unnecessary_detected,
            "moved_to_trash": trashed,
            "uncertain_review": uncertain,
            "audit_history": audit_logs,
            "notifications": notifications,
        },
    }


@router.post("/missions/{mission_id}/restore/{message_id}")
def restore_email(mission_id: str, message_id: str):
    """Restore an email back to the inbox from Trash."""
    db.init_db()
    mission = db.get_email_mission(mission_id)
    if not mission:
        raise HTTPException(404, "Email mission not found")

    from .email_intelligence.sync_engine import _get_connector_for_mission
    connector, _ = _get_connector_for_mission(mission)
    res = restore_email_from_trash(mission_id, message_id, connector)
    return res


@router.post("/notifications/{notification_id}/dismiss")
def dismiss_notification(notification_id: str):
    """Dismiss a neglected email notification."""
    db.init_db()
    ok = db.update_email_notification_status(notification_id, "dismissed")
    if not ok:
        raise HTTPException(404, "Notification not found")
    return {"ok": True}


@router.post("/companion/ask")
def ask_companion(body: CompanionQueryInput):
    """Ask the companion bot an email question based on actual SQLite mission data."""
    db.init_db()
    return answer_email_companion_query(body.query, mission_id=body.mission_id)

