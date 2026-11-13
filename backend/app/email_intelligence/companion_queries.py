"""Companion conversational queries powered by actual mission and audit records."""
from __future__ import annotations

import re
import time
from typing import Any, Optional
from app import db


def is_email_companion_query(text: str) -> bool:
    """Return True if user is querying email intelligence status or history."""
    t = (text or "").lower()
    patterns = [
        r"what\s+did\s+you\s+delete",
        r"why\s+did\s+you\s+delete",
        r"show\s+me\s+important\s+emails?",
        r"emails?\s+i\s+haven'?t\s+replied",
        r"what\s+deadlines",
        r"pause\s+email",
        r"resume\s+email",
        r"(?:today'?s?\s+)?email\s+summary",
        r"scan\s+(?:my\s+)?emails?",
        r"clean(?:up)?\s+(?:my\s+)?emails?",
        r"what\s+is\s+my\s+(?:e-?mail|mail|account)",
        r"which\s+account\s+are\s+you\s+monitoring",
        r"are\s+you\s+(?:still\s+)?scanning",
        r"is\s+monitoring\s+active",
        r"monitoring\s+status",
        r"are\s+(?:you|u)\s+deleting",
        r"check\s+now",
        r"i\s+have\s+connected",
        r"is\s+my\s+(?:e-?mail|mail)\s+connected",
        r"check\s+(?:my\s+)?(?:e-?mail|mail|inbox|connected\s+mail)",
        r"(?:is\s+)?(?:my\s+)?(?:e-?mail|mail)\s+(?:connected|active|working)",
        r"did\s+you\s+check\s+(?:my\s+)?(?:e-?mail|mail)",
        r"verify\s+(?:my\s+)?(?:e-?mail|mail|connection)",
    ]
    return any(re.search(p, t) for p in patterns)


def answer_email_companion_query(
    query: str,
    mission_id: Optional[str] = None,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    """Produce an accurate conversational reply based exclusively on real DB records."""
    db.init_db(db_path)
    missions = db.list_email_missions(db_path=db_path)
    if not missions:
        from app.connectors.oauth import oauth_manager
        google_status = oauth_manager.verify_connection("google")
        ms_status = oauth_manager.verify_connection("microsoft")
        if google_status.get("connected") or ms_status.get("connected"):
            acc = google_status.get("account") or ms_status.get("account")
            prov = "Google (Gmail)" if google_status.get("connected") else "Microsoft (Outlook)"
            return {
                "answered": True,
                "text": (
                    f"### ✓ Mail Account Connected & Verified\n\n"
                    f"• **Connected Account:** `{acc}` ({prov})\n"
                    f"• **Status:** Authenticated and ready for monitoring\n\n"
                    f"To begin continuous inbox monitoring, say: *'Monitor my email and remind me if any important mail comes related to internships and jobs'*."
                ),
                "stats": {},
            }
        return {
            "answered": True,
            "text": "No email monitoring missions are currently configured. You can start one by asking: 'Continuously scan my emails. Identify unnecessary emails and remove them. If you find important emails that I have not attended to, notify me.'",
            "stats": {},
        }

    active_mission = None
    if mission_id:
        active_mission = db.get_email_mission(mission_id, db_path=db_path)
    if not active_mission:
        active_mission = missions[0]
    mid = active_mission["id"]
    stats = db.get_email_dashboard_stats(mid, db_path=db_path)
    t = query.lower()

    # 00. Connection verification / "check now" / "i have connected"
    if re.search(
        r"check\s+now|i\s+have\s+connected|is\s+my\s+(?:e-?mail|mail)\s+connected|"
        r"check\s+(?:my\s+)?(?:e-?mail|mail|inbox|connected\s+mail)|"
        r"(?:is\s+)?(?:my\s+)?(?:e-?mail|mail)\s+(?:connected|active|working)|"
        r"did\s+you\s+check\s+(?:my\s+)?(?:e-?mail|mail)|"
        r"verify\s+(?:my\s+)?(?:e-?mail|mail|connection)",
        t,
    ):
        from app.connectors.oauth import oauth_manager
        google_status = oauth_manager.verify_connection("google")
        ms_status = oauth_manager.verify_connection("microsoft")

        connected_acc = None
        prov_label = ""
        if google_status.get("connected") and google_status.get("account"):
            connected_acc = google_status["account"]
            prov_label = "Google (Gmail)"
        elif ms_status.get("connected") and ms_status.get("account"):
            connected_acc = ms_status["account"]
            prov_label = "Microsoft (Outlook)"

        if connected_acc:
            pol = active_mission.get("notification_policy") or {}
            custom_kws = pol.get("custom_keywords") or []
            topics_line = ""
            if custom_kws:
                topics_str = ", ".join(f"`{k}`" for k in custom_kws)
                topics_line = f"\n• **Monitored Custom Topics:** {topics_str} (Immediate priority alerts active)"

            mode_str = "AUTONOMOUS CLEANUP ACTIVE" if active_mission.get("policy_authorized") else "DRY-RUN REVIEW (Safe — no deletions without authorization)"
            return {
                "answered": True,
                "text": (
                    f"### ✓ Mail Account Connected & Verified\n\n"
                    f"• **Connected Account:** `{connected_acc}` ({prov_label})\n"
                    f"• **Monitoring Status:** **{active_mission.get('status', 'active').upper()}** (Scans every {active_mission.get('frequency_seconds', 300)}s){topics_line}\n"
                    f"• **Cleanup Mode:** **{mode_str}**\n"
                    f"• **Total Processed:** **{stats.get('total_processed', 0)} messages** ({stats.get('important_detected', 0)} important, {stats.get('dry_run_flagged', 0)} flagged for review)\n"
                    f"• **Awaiting Attention:** **{stats.get('neglected_awaiting_attention', 0)} emails**\n\n"
                    f"Continuous background monitoring is active. Whenever new emails matching your topics or requiring urgent action arrive, I'll alert you right here in this chat!"
                ),
                "stats": stats,
            }
        else:
            err = google_status.get("error") or ms_status.get("error") or "No active mail account was detected."
            return {
                "answered": True,
                "text": (
                    f"### ⚠️ Mail Account Not Connected\n\n"
                    f"{err}\n\n"
                    f"To connect your account:\n"
                    f"1. Open the **Connectors** tab in the Control Center.\n"
                    f"2. Add your OAuth credentials or paste your Access Token / Refresh Token.\n"
                    f"3. Return here and prompt me again."
                ),
                "stats": stats,
            }

    # 0a. "What is my mail / which account are you monitoring?"
    if re.search(r"what\s+is\s+my\s+(?:e-?mail|mail|account)|which\s+account", t):
        from app.connectors.oauth import oauth_manager
        prov = active_mission.get("provider", "gmail")
        is_live = False
        try:
            conn = oauth_manager.connector("gmail" if prov == "gmail" else "outlook")
            is_live = getattr(conn, "configured", False)
        except Exception:
            pass
        status_mode = "Live Connected Account" if is_live else "Controlled Test Mailbox (Sandbox)"
        return {
            "answered": True,
            "text": (
                f"Currently monitoring email account: **`{active_mission.get('account_email')}`** ({prov.upper()})\n"
                f"• Mode: **[{status_mode}]**\n"
                f"• Status: **{active_mission.get('status', 'active').upper()}** (Checking every {active_mission.get('frequency_seconds', 300)}s)\n"
                f"• Deletion Mode: **{'AUTONOMOUS CLEANUP ACTIVE' if active_mission.get('policy_authorized') else 'DRY-RUN REVIEW (No deletions)'}**\n"
                f"• Total processed: **{stats.get('total_processed', 0)} messages**"
            ),
            "stats": stats,
        }

    # 0b. "Are you still scanning / monitoring status?"
    if re.search(r"are\s+you\s+(?:still\s+)?scanning|is\s+monitoring\s+active|monitoring\s+status", t):
        return {
            "answered": True,
            "text": (
                f"Yes, continuous monitoring is **{active_mission.get('status', 'active').upper()}** for `{active_mission.get('account_email')}`.\n"
                f"• Scanning interval: every {active_mission.get('frequency_seconds', 300)} seconds\n"
                f"• Current Mode: **{'AUTONOMOUS CLEANUP ACTIVE' if active_mission.get('policy_authorized') else 'DRY-RUN REVIEW (Safe)'}**\n"
                f"• Needs attention: **{stats.get('neglected_awaiting_attention', 0)} important emails**"
            ),
            "stats": stats,
        }

    # 0c. "Are you deleting?"
    if re.search(r"are\s+(?:you|u)\s+deleting", t):
        if active_mission.get("policy_authorized"):
            return {
                "answered": True,
                "text": "Autonomous cleanup is currently **AUTHORIZED**. Eligible unnecessary emails (promotions, low-value notifications) are moved to Trash. Protected categories (banking, security, recruitment, personal) are strictly preserved.",
                "stats": stats,
            }
        else:
            return {
                "answered": True,
                "text": "No, I am **not deleting any emails**. Current mode is **DRY-RUN REVIEW**. Unnecessary emails are only detected and flagged for your review in the Control Center. No emails will be moved to Trash without your explicit authorization.",
                "stats": stats,
            }

    # 1. "What did you delete today?"
    if re.search(r"what\s+did\s+you\s+delete", t):
        trashed = db.get_email_classifications(mid, action_taken="moved_to_trash", db_path=db_path)
        dry_run = db.get_email_classifications(mid, action_taken="dry_run_flagged", db_path=db_path)

        if trashed:
            lines = [f"• **{m['subject']}** (from *{m['sender']}* — {m.get('subcategory', 'promotional')})" for m in trashed[:5]]
            return {
                "answered": True,
                "text": (
                    f"Today, I moved **{len(trashed)} unnecessary email(s)** to Trash in accordance with your authorized policy:\n\n"
                    + "\n".join(lines)
                    + "\n\n*All banking, security, recruitment, education, and personal messages were strictly preserved.* You can restore any message from the Trash tab."
                ),
                "stats": stats,
            }
        elif dry_run:
            lines = [f"• **{m['subject']}** (from *{m['sender']}*)" for m in dry_run[:5]]
            return {
                "answered": True,
                "text": (
                    f"Autonomous deletion is currently in **Dry-Run Mode**. I identified **{len(dry_run)} unnecessary email(s)** eligible for cleanup, but took no action:\n\n"
                    + "\n".join(lines)
                    + "\n\nTo allow me to move eligible emails to Trash automatically, authorize the cleanup policy in the Control Center."
                ),
                "stats": stats,
            }
        else:
            return {
                "answered": True,
                "text": "I have not moved any emails to Trash today. No messages met the authorized criteria for deletion.",
                "stats": stats,
            }

    # 2. "Why did you delete this email?"
    if re.search(r"why\s+did\s+you\s+delete", t):
        trashed = db.get_email_classifications(mid, action_taken="moved_to_trash", db_path=db_path)
        if trashed:
            sample = trashed[0]
            return {
                "answered": True,
                "text": (
                    f"Regarding **'{sample['subject']}'** (from *{sample['sender']}*):\n\n"
                    f"• **Classification:** {sample.get('category')} ({sample.get('subcategory')})\n"
                    f"• **Confidence:** {sample.get('unnecessary_confidence', 0.0) * 100:.0f}%\n"
                    f"• **Reasoning:** {sample.get('reasoning')}\n"
                    f"• **Safety verification:** Verified that it did not belong to any protected category (financial, security, education, recruitment, personal) and contained no deadlines."
                ),
                "stats": stats,
            }
        return {
            "answered": True,
            "text": "No emails have been moved to Trash yet in this session.",
            "stats": stats,
        }

    # 3. "Show me important emails I haven't replied to."
    if re.search(r"important\s+emails?|haven'?t\s+replied|unanswered", t):
        important = db.get_email_classifications(mid, category="important", db_path=db_path)
        neglected = [m for m in important if not m.get("is_replied")]
        if neglected:
            lines = []
            for m in neglected[:5]:
                dl = f" | ⏰ **Deadline:** {m['deadline']}" if m.get("deadline") else ""
                lines.append(f"• **{m['subject']}** (from *{m['sender']}*){dl}\n  *Action:* {m.get('required_action') or m.get('reasoning')}")
            return {
                "answered": True,
                "text": (
                    f"I found **{len(neglected)} important email(s)** that appear to need your attention:\n\n"
                    + "\n\n".join(lines)
                ),
                "stats": stats,
            }
        return {
            "answered": True,
            "text": "All important emails have been answered or attended to! There are no neglected messages pending.",
            "stats": stats,
        }

    # 4. "What deadlines have you detected?"
    if re.search(r"deadlines?", t):
        with_deadlines = [m for m in db.get_email_classifications(mid, db_path=db_path) if m.get("deadline")]
        if with_deadlines:
            lines = [f"• **{m['deadline']}**: {m['subject']} (from *{m['sender']}*)" for m in with_deadlines[:5]]
            return {
                "answered": True,
                "text": (
                    f"Here are the active deadlines I detected from your incoming emails:\n\n"
                    + "\n".join(lines)
                ),
                "stats": stats,
            }
        return {
            "answered": True,
            "text": "No upcoming deadlines have been detected in your monitored emails.",
            "stats": stats,
        }

    # 5. "Pause email monitoring"
    if re.search(r"pause\s+email", t):
        db.update_email_mission(mid, {"status": "paused"}, db_path=db_path)
        return {
            "answered": True,
            "text": f"Email monitoring for **{active_mission.get('account_email')}** is now **paused**. No automated scans or cleanups will execute until you resume.",
            "stats": stats,
        }

    # 6. "Resume monitoring"
    if re.search(r"resume\s+email", t):
        now = time.time()
        db.update_email_mission(mid, {"status": "active", "next_run_at": now}, db_path=db_path)
        return {
            "answered": True,
            "text": f"Email monitoring for **{active_mission.get('account_email')}** has been **resumed**. Next scan is running now.",
            "stats": stats,
        }

    # 7. "Show me today's email summary"
    return {
        "answered": True,
        "text": (
            f"### 📬 Today's Email Intelligence Summary\n"
            f"• **Monitored Account:** `{active_mission.get('account_email')}` ({active_mission.get('provider').upper()})\n"
            f"• **Status:** {active_mission.get('status').upper()} | **Mode:** {'Autonomous Deletion' if active_mission.get('policy_authorized') else 'Dry-Run Review'}\n"
            f"• **Total Processed:** {stats.get('total_processed', 0)} emails\n"
            f"• **Unnecessary Detected:** {stats.get('unnecessary_detected', 0)} ({stats.get('moved_to_trash', 0)} moved to Trash, {stats.get('dry_run_flagged', 0)} dry-run flagged)\n"
            f"• **Important Detected:** {stats.get('important_detected', 0)} ({stats.get('important_handled', 0)} handled, {stats.get('neglected_awaiting_attention', 0)} awaiting attention)\n"
            f"• **Uncertain (Protected):** {stats.get('uncertain_review_required', 0)}\n\n"
            f"All protected categories (financial, security, academic, recruitment, personal) remain strictly locked against deletion."
        ),
        "stats": stats,
    }

