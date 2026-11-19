"""FastAPI endpoints for Email Intelligence dashboard, control, and audit."""
from __future__ import annotations

import logging
import time
from typing import Any, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from fastapi import Request

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


def _caller_user_id(request: Request) -> Optional[str]:
    """The authenticated user id, or None for the local operator."""
    return getattr(request.state, "user_id", None)


def _require_mission_owner(request: Request, mission: Optional[dict]) -> dict:
    """404 (never confirm existence) when the caller may not access
    this mission. The operator may access any mission; a session user
    only their own. Legacy missions with no owner are operator-owned."""
    if not mission:
        raise HTTPException(404, "Email mission not found")
    uid = _caller_user_id(request)
    if uid is not None and mission.get("user_id") != uid:
        raise HTTPException(404, "Email mission not found")
    return mission


class PolicyAuthorizeInput(BaseModel):
    authorized: bool = True
    cleanup_policy: Optional[dict[str, Any]] = None


class CompanionQueryInput(BaseModel):
    query: str
    mission_id: Optional[str] = None


@router.get("/missions")
def list_missions(request: Request):
    """List email monitoring missions owned by the caller (all, for
    the operator)."""
    db.init_db()
    return db.list_email_missions(user_id=_caller_user_id(request))


@router.post("/missions", status_code=201)
async def create_mission(request: Request, body: EmailMissionSetupInput):
    """Register and start an email monitoring mission owned by the caller."""
    db.init_db()
    user_id = _caller_user_id(request)
    existing_missions = [m for m in db.list_email_missions(user_id=user_id) if m.get("account_email") == body.account_email and m.get("status") == "active"]
    if existing_missions:
        mission = existing_missions[0]
        await run_email_mission_tick(mission["id"])
        return mission

    connection_id = None
    if user_id:
        from .connectors.oauth import oauth_manager
        connection_id = oauth_manager.find_connection_id(user_id, body.provider, body.account_email)
    mission = db.create_email_mission(
        account_email=body.account_email,
        provider=body.provider,
        frequency_seconds=body.frequency_seconds,
        cleanup_policy=body.cleanup_policy.model_dump(),
        notification_policy=body.notification_policy.model_dump(),
        conversation_id=body.conversation_id,
        user_id=user_id,
        connection_id=connection_id,
    )
    # Trigger first tick in background or inline
    await run_email_mission_tick(mission["id"])
    return db.get_email_mission(mission["id"])


@router.get("/missions/{mission_id}")
def get_mission(request: Request, mission_id: str):
    """Get mission details."""
    db.init_db()
    m = _require_mission_owner(request, db.get_email_mission(mission_id))
    return m


@router.post("/missions/{mission_id}/authorize")
async def authorize_cleanup_policy(request: Request, mission_id: str, body: PolicyAuthorizeInput):
    """Explicitly authorize autonomous email cleanup within approved policy."""
    db.init_db()
    m = _require_mission_owner(request, db.get_email_mission(mission_id))

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
def pause_mission(request: Request, mission_id: str):
    """Pause continuous email monitoring."""
    db.init_db()
    _require_mission_owner(request, db.get_email_mission(mission_id))
    updated = db.update_email_mission(mission_id, {"status": "paused"})
    if not updated:
        raise HTTPException(404, "Email mission not found")
    db.log_email_audit(mission_id, "monitoring_paused", {})
    return {"ok": True, "status": "paused"}


@router.post("/missions/{mission_id}/resume")
async def resume_mission(request: Request, mission_id: str):
    """Resume continuous email monitoring and trigger an immediate tick."""
    db.init_db()
    _require_mission_owner(request, db.get_email_mission(mission_id))
    now = time.time()
    updated = db.update_email_mission(mission_id, {"status": "active", "next_run_at": now})
    if not updated:
        raise HTTPException(404, "Email mission not found")
    db.log_email_audit(mission_id, "monitoring_resumed", {})
    await run_email_mission_tick(mission_id)
    return {"ok": True, "status": "active"}


@router.delete("/missions/{mission_id}")
def cancel_mission(request: Request, mission_id: str):
    """Cancel / stop continuous email monitoring."""
    db.init_db()
    _require_mission_owner(request, db.get_email_mission(mission_id))
    updated = db.update_email_mission(mission_id, {"status": "stopped"})
    if not updated:
        raise HTTPException(404, "Email mission not found")
    db.log_email_audit(mission_id, "monitoring_cancelled", {})
    return {"ok": True, "status": "stopped"}


@router.post("/missions/{mission_id}/sync")
async def force_sync_tick(request: Request, mission_id: str):
    """Trigger an immediate sync tick for the email mission. The tick
    resolves the mission owner's own connection; this route never
    rebinds the mission's account to whatever profile the global
    token returns (that let any holder of the globals re-own a
    mission by syncing it)."""
    db.init_db()
    _require_mission_owner(request, db.get_email_mission(mission_id))
    result = await run_email_mission_tick(mission_id)
    return result


@router.get("/missions/{mission_id}/dashboard")
def get_dashboard_payload(request: Request, mission_id: str):
    """Return aggregated stats and categorized lists for the frontend dashboard."""
    db.init_db()
    mission = _require_mission_owner(request, db.get_email_mission(mission_id))

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
def restore_email(request: Request, mission_id: str, message_id: str):
    """Restore an email back to the inbox from Trash."""
    db.init_db()
    mission = _require_mission_owner(request, db.get_email_mission(mission_id))

    from .email_intelligence.sync_engine import _get_connector_for_mission
    try:
        connector, _ = _get_connector_for_mission(mission)
    except RuntimeError as exc:
        # A real account with no live OAuth must not silently restore
        # from the demo mailbox.
        raise HTTPException(503, str(exc))
    res = restore_email_from_trash(mission_id, message_id, connector)
    return res


@router.post("/notifications/{notification_id}/dismiss")
def dismiss_notification(request: Request, notification_id: str):
    """Dismiss a neglected email notification (only the mission owner's)."""
    db.init_db()
    notif = db.get_email_notification(notification_id)
    if not notif:
        raise HTTPException(404, "Notification not found")
    _require_mission_owner(request, db.get_email_mission(notif.get("mission_id") or ""))
    ok = db.update_email_notification_status(notification_id, "dismissed")
    if not ok:
        raise HTTPException(404, "Notification not found")
    return {"ok": True}


@router.post("/companion/ask")
def ask_companion(request: Request, body: CompanionQueryInput):
    """Ask the companion bot an email question based on actual SQLite mission data."""
    db.init_db()
    return answer_email_companion_query(body.query, mission_id=body.mission_id, user_id=_caller_user_id(request))

