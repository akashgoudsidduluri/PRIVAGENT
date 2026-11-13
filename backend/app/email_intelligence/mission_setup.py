"""Email monitoring mission setup and conversational onboarding."""
from __future__ import annotations

import json
import re
import time
from typing import Any, Optional

from app import db
from .companion_queries import is_email_companion_query, answer_email_companion_query
from .sync_engine import run_email_mission_tick, get_demo_mailbox

SETUP_TRIGGER = re.compile(
    r"\b(?:continuously\s+scan|monitor|moinitor|scan\s+and\s+clean|clean\s+up|cleanup|scan\s+my)\s+(?:my\s+)?emails?\b|"
    r"\b(?:identify\s+unnecessary\s+emails|remove\s+unnecessary|detect\s+important\s+emails|email\s+monitoring)\b|"
    r"\b(?:remind|notify|alert)\s+(?:me\s+)?(?:if|when|about)\s+.*?\b(?:emails?|mails?|inbox)\b|"
    r"\b(?:emails?|mails?|inbox)\b.*?\b(?:notify|alert|remind)\s+me\b|"
    r"\b(?:monitor|watch|track)\b.*?\b(?:emails?|mails?|inbox)\b",
    re.IGNORECASE,
)


def extract_custom_topics(text: str) -> list[str]:
    """Extract requested custom alert topics from the prompt (e.g. internships, jobs)."""
    m = re.search(r"\b(?:rel+ated\s+to|about|for|regarding|concerning|on)\s+(.+?)(?:\s+(?:notify|alert|remind|then|please)|$)", text, re.IGNORECASE)
    if m:
        raw = m.group(1).strip()
        parts = re.split(r"\s*(?:,|and|\bor\b)\s*", raw)
        clean = []
        for p in parts:
            p_clean = p.strip().lower()
            if len(p_clean) > 2 and p_clean not in ("mail", "email", "emails", "inbox", "important", "imp"):
                if ("inter" in p_clean and "ship" in p_clean) or "interh" in p_clean:
                    p_clean = "internships"
                clean.append(p_clean)
        return clean
    return []


def is_email_mission_setup_instruction(text: str) -> bool:
    """Return True if user is instructing PrivAgent to configure continuous email monitoring."""
    t = (text or "").strip()
    return bool(SETUP_TRIGGER.search(t))


async def handle_email_intelligence_instruction(
    instruction: str,
    chat_id: Optional[str] = None,
    db_path: Optional[str] = None,
) -> str:
    """Handle either mission setup or companion query for email intelligence."""
    db.init_db(db_path)

    # 1. If it's a specific companion question (e.g. "What did you delete today?")
    if is_email_companion_query(instruction) and not is_email_mission_setup_instruction(instruction):
        ans = answer_email_companion_query(instruction, db_path=db_path)
        return ans.get("text", "Processed your email query.")

    # 2. Mission Setup Instruction
    # Determine connected account / provider
    from app.email_intelligence import sync_engine
    injected_demo = getattr(sync_engine, "_demo_mailbox", None)
    is_demo_requested = bool(re.search(r"\b(?:demo|sandbox|mock|test\s+mailbox)\b", instruction, re.IGNORECASE))

    account_email = None
    provider = "gmail"
    mode_label = ""
    error_reason = ""

    if injected_demo is not None:
        account_email = getattr(injected_demo, "account_email", "demo.privagent@gmail.com")
        provider = "gmail"
        mode_label = "[Controlled Test Mailbox (Sandbox)]"
    else:
        from app.connectors.oauth import oauth_manager
        google_status = oauth_manager.verify_connection("google")
        ms_status = oauth_manager.verify_connection("microsoft")

        if google_status.get("connected") and google_status.get("account"):
            account_email = google_status["account"]
            provider = "gmail"
            mode_label = f"[Live Connected Gmail: {account_email}]"
        elif ms_status.get("connected") and ms_status.get("account"):
            account_email = ms_status["account"]
            provider = "outlook"
            mode_label = f"[Live Connected Outlook: {account_email}]"
        elif is_demo_requested:
            demo_box = sync_engine.get_demo_mailbox()
            account_email = demo_box.account_email
            provider = "gmail"
            mode_label = "[Controlled Test Mailbox (Sandbox)]"
        else:
            error_reason = google_status.get("error") or ms_status.get("error") or ""

    if not account_email:
        expired_note = "Your access token has expired (HTTP 401)." if "401" in error_reason else "No active mail account was found."
        return (
            f"### ⚠️ Mail Account Not Connected\n\n"
            f"{expired_note}\n\n"
            f"To enable continuous email monitoring on your real inbox:\n"
            f"1. Open the **Connectors** tab in the Control Center.\n"
            f"2. Enter your OAuth credentials or paste a valid **Access Token** / **Refresh Token**.\n"
            f"3. Return here and prompt me again (e.g. *'Monitor my email and delete unnecessary emails'*).\n\n"
            f"*(Tip: To test email automation right now in sandbox mode, say: 'Monitor my email using demo sandbox'.)*"
        )

    if not account_email and is_demo_requested:
        account_email = "demo.privagent@gmail.com"
        provider = "gmail"
        mode_label = "[Controlled Test Mailbox (Live-Protocol Sandbox)]"

    # Extract custom alert topics from the user instruction (e.g. internships, jobs)
    custom_topics = extract_custom_topics(instruction)
    base_notif = {
        "notify_deadlines": True,
        "notify_interviews": True,
        "notify_unanswered_important": True,
        "neglect_threshold_seconds": 3600,
        "custom_keywords": custom_topics,
    }

    # Check for existing mission to re-bind or update
    existing_missions = db.list_email_missions(db_path=db_path)
    existing_mission = next((m for m in existing_missions if m.get("account_email") == account_email), None)

    if existing_mission:
        mission_id = existing_mission["id"]
        prev_pol = existing_mission.get("notification_policy") or {}
        prev_kws = prev_pol.get("custom_keywords") or []
        combined_kws = list(dict.fromkeys(prev_kws + custom_topics))
        base_notif["custom_keywords"] = combined_kws

        db.update_email_mission(
            mission_id,
            {
                "status": "active",
                "conversation_id": chat_id or existing_mission.get("conversation_id"),
                "notification_policy_json": json.dumps(base_notif),
                "next_run_at": time.time(),
            },
            db_path=db_path,
        )
        mission = db.get_email_mission(mission_id, db_path=db_path)
    else:
        mission = db.create_email_mission(
            account_email=account_email,
            provider=provider,
            frequency_seconds=300,
            conversation_id=chat_id,
            cleanup_policy={
                "allow_promotions": True,
                "allow_newsletters": True,
                "allow_notifications": True,
                "min_confidence": 0.80,
                "protected_categories": ["financial", "security", "education", "recruitment", "personal"],
            },
            notification_policy=base_notif,
            db_path=db_path,
        )
        mission_id = mission["id"]

    # Execute immediate initial synchronization and classification tick
    tick_result = await run_email_mission_tick(mission_id, db_path=db_path)
    stats = db.get_email_dashboard_stats(mission_id, db_path=db_path)

    custom_topics_text = ""
    if base_notif.get("custom_keywords"):
        kws_str = ", ".join(f"`{k}`" for k in base_notif["custom_keywords"])
        custom_topics_text = (
            f"\n• **Monitored Custom Topics:** {kws_str}\n"
            f"  - *Incoming emails matching these topics trigger immediate priority notifications in this chat thread.*"
        )

    channel_text = f"\n• **Dedicated Chat Channel:** This chat thread is now dedicated to continuous email monitoring. Live alerts will stream directly here." if chat_id else ""

    summary_markdown = f"""### 📬 Autonomous Email Intelligence Mission Configured

I have configured continuous email monitoring for your account. Here is the operational summary:

• **Connected Account:** `{account_email}` ({provider.upper()}) {mode_label}  
• **Monitoring Frequency:** Every 5 minutes (300 seconds)  {channel_text}{custom_topics_text}
• **Email Categories Monitored:**
  - **Unnecessary:** Promotional campaigns, newsletters, repeated automated notifications, bulk marketing.
  - **Important:** University/academic communication, recruitment/job opportunities, interview invitations, assessment deadlines, banking & financial statements, security alerts, personal communication.
  - **Uncertain:** Unknown senders or ambiguous context *(strictly preserved for human review)*.

---

### 🛡️ Autonomous Deletion Policy & Explicit Authorization Gate

> [!IMPORTANT]
> **Safety Guarantee: Zero Permanent Deletions.**
> Deletions move messages to **Trash** only. You can restore any message at any time.

• **Current Mode:** **DRY-RUN REVIEW**  
• **Deletion Eligibility Rules:**
  - Eligible messages must be classified as *unnecessary* with confidence >= 80%.
  - Protected categories (**Banking, Financial, Security, Academic, Recruitment, Personal, and Deadlines**) are **strictly locked and never automatically deleted**.
  - No messages will be moved to Trash until you **explicitly authorize** autonomous cleanup.

---

### 🔔 Important-Email Notification Policy

• **Proactive Alert Triggers:**
  - Unattended deadlines (academic registrations, internship submissions).
  - Interview invitations requiring your confirmation where no reply has been detected.
  - Important unanswered emails older than 1 hour.
• **Deduplication:** You will receive only one alert per unresolved email. Alerts automatically resolve when you reply.

---

### 📊 Initial Scan Results

• **Total Processed:** {stats.get('total_processed', 0)} messages  
• **Unnecessary Identified (Dry-Run Flagged):** {stats.get('dry_run_flagged', 0)}  
• **Important Emails Detected:** {stats.get('important_detected', 0)}  
• **Important Emails Awaiting Attention:** {stats.get('neglected_awaiting_attention', 0)}  
• **Monitoring Status:** **ACTIVE (Ticking in background)**

You can view details, inspect the audit history, or authorize autonomous cleanup at any time in the **Email Intelligence Dashboard** under the Control Center ([http://localhost:5173](http://localhost:5173)).

*Continuous email intelligence is now active on your dashboard (http://localhost:5173). I'm heading into the background to keep watch on your inbox, catch you later buddy!* 👋
"""
    return summary_markdown

