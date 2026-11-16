"""Conversational Google Calendar integration and schedule queries."""
from __future__ import annotations

import datetime
import re
from typing import Any, Optional

from app.connectors.oauth import oauth_manager


def is_calendar_query(text: str) -> bool:
    """Return True if the instruction is asking to inspect or manage the user's calendar."""
    t = (text or "").lower()
    patterns = [
        r"\b(?:check|show|view|list|get|what\s+is\s+on|what'?s\s+on)\s+(?:my\s+)?(?:calendar|schedule|agenda|meetings?|events?)\b",
        r"\b(?:do\s+i\s+have|are\s+there)\s+any\s+(?:meetings?|events?|appointments?|calls?)\b",
        r"\bcalendar\s+(?:status|events?|schedule)\b",
        r"\b(?:upcoming|next)\s+(?:meetings?|events?|appointments?|calls?)\b",
        r"\b(?:schedule|create|add|book)\s+(?:an?\s+)?(?:event|meeting|invite|appointment|call)\b",
        r"\bgoogle\s+calendar\b",
    ]
    return any(re.search(p, t) for p in patterns)


_CREATE_RE = re.compile(
    r"\b(?:schedule|create|add|book)\s+(?:an?\s+)?(?:event|meeting|appointment|call)\s+(?:called|titled|for)?\s*[\"']?([^\"'\n]+?)[\"']?\s+(?:on|at|for)\s+(.+)",
    re.IGNORECASE,
)


def parse_calendar_create(instruction: str, account_email: str | None = None) -> dict[str, Any] | None:
    """Parse a calendar-create instruction into its parts, or None.

    REVIEW (Task 14 audit): the connector runner confirms a calendar
    create BEFORE this module runs, so it must be able to tell — with
    no side effects — whether the instruction actually parses as a
    create and what will be created. Before this helper, an
    unparseable create ("schedule a meeting with Priya tomorrow")
    sailed through confirmation and silently returned a listing of
    existing events instead of creating anything, and the operator
    approved blind: the title/time were only computed after approval.

    The parse is deliberately the same regex and the same default the
    handler has always used (tomorrow 10:00 AM UTC, 1 hour): the
    named time text is captured but NOT interpreted — there is no date
    parser in this codebase — so the confirmation shows both the time
    as written and the time that will actually be booked, and the
    operator can decline when they differ.
    """
    m = _CREATE_RE.search(instruction or "")
    if not m:
        return None
    title = m.group(1).strip()
    time_text = m.group(2).strip()
    if not title or not time_text:
        return None
    attendees = [
        a for a in re.findall(r"[\w\.-]+@[\w\.-]+\.\w+", instruction or "")
        if a != account_email
    ]
    now = datetime.datetime.now(datetime.timezone.utc)
    start_dt = now + datetime.timedelta(days=1)
    start_dt = start_dt.replace(hour=10, minute=0, second=0, microsecond=0)
    end_dt = start_dt + datetime.timedelta(hours=1)
    return {
        "title": title,
        "time_text": time_text,
        "attendees": attendees,
        "start": start_dt,
        "end": end_dt,
    }


async def handle_calendar_instruction(instruction: str, chat_id: Optional[str] = None) -> str:
    """Handle calendar listing or event creation requests via connected Google OAuth."""
    t = instruction.lower()

    # 1. Verify Google Connection
    google_status = oauth_manager.verify_connection("google")
    if not google_status.get("connected") or not google_status.get("account"):
        err = google_status.get("error") or "No active Google connection detected."
        return (
            "### ⚠️ Google Calendar Not Connected\n\n"
            f"{err}\n\n"
            "To connect your Google Calendar:\n"
            "1. Open the **Connectors** tab in the Control Center.\n"
            "2. Sign in or enter your Google OAuth credentials with `calendar.events` scope.\n"
            "3. Return here and prompt me again (e.g. *'What meetings do I have this week?'*)."
        )

    account_email = google_status["account"]
    connector = oauth_manager.connector("google_calendar")

    # 2. Check for Event Creation
    parsed = parse_calendar_create(instruction, account_email)
    if parsed is not None:
        title = parsed["title"]
        start_dt = parsed["start"]
        end_dt = parsed["end"]
        attendees = parsed["attendees"]

        try:
            invite = connector.create_invite(
                calendar_id="primary",
                title=title or "New Meeting",
                start=start_dt.isoformat(),
                end=end_dt.isoformat(),
                attendees=attendees,
            )
            html_link = invite.get("htmlLink", "")
            link_md = f"\n• **Calendar Link:** [Open in Google Calendar]({html_link})" if html_link else ""
            att_text = f"\n• **Attendees:** {', '.join(attendees)}" if attendees else ""

            return (
                f"### ✓ Google Calendar Invite Created\n\n"
                f"• **Event:** **{title}**\n"
                f"• **When:** {start_dt.strftime('%A, %B %d, %Y at %I:%M %p UTC')}\n"
                f"• **Calendar:** `{account_email}`{att_text}{link_md}\n\n"
                f"The event has been placed on your calendar and invites dispatched."
            )
        except Exception as exc:
            return f"### ⚠️ Failed to Create Calendar Event\n\nAn error occurred while contacting the Google Calendar API: `{exc}`"

    # 3. Schedule / Events Listing
    now = datetime.datetime.now(datetime.timezone.utc)
    is_today = "today" in t
    if is_today:
        start_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_dt = start_dt + datetime.timedelta(days=1)
        window_label = "today"
    else:
        start_dt = now
        end_dt = now + datetime.timedelta(days=7)
        window_label = "the next 7 days"

    try:
        events = connector.list_events(
            calendar_id="primary",
            start=start_dt.isoformat(),
            end=end_dt.isoformat(),
            limit=25,
        )
    except Exception as exc:
        return f"### ⚠️ Google Calendar Query Failed\n\nCould not fetch events from Google Calendar: `{exc}`"

    if not events:
        return (
            f"### 📅 Google Calendar: `{account_email}`\n\n"
            f"**No upcoming events found** for {window_label}.\n"
            f"Your schedule is completely clear! ✨"
        )

    lines = []
    for ev in events[:10]:
        summary = ev.get("summary", "(No Title)")
        start_raw = ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date") or ""
        hangout = ev.get("hangoutLink", "")
        link_str = f" — [Join Video Call]({hangout})" if hangout else ""
        lines.append(f"• **{start_raw}**: **{summary}**{link_str}")

    return (
        f"### 📅 Google Calendar Schedule: `{account_email}`\n\n"
        f"Here are your upcoming events for {window_label}:\n\n"
        + "\n".join(lines)
        + f"\n\n*(Total {len(events)} events found)*"
    )
