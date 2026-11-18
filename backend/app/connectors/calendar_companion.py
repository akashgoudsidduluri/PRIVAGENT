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


# India Standard Time is UTC+5:30 year-round (no daylight saving), so
# a fixed offset is exact. The user is in IST: an unmarked time is
# their local time, and "IST" in the instruction selects it
# explicitly. "UTC"/"GMT" selects UTC.
_IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30), "IST")

_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_NAMES = "|".join(sorted(_MONTHS, key=len, reverse=True))
_TIME_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", re.IGNORECASE)
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_DMY_DATE_RE = re.compile(rf"\b(\d{{1,2}})\s+({_MONTH_NAMES})\b(?:\s+(\d{{4}}))?", re.IGNORECASE)
_MDY_DATE_RE = re.compile(rf"\b({_MONTH_NAMES})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:\s*,?\s*(\d{{4}}))?", re.IGNORECASE)


def _parse_time_of_day(text: str) -> tuple[int, int] | None:
    """Parse the first clock time in the text ("10:00 AM", "10am",
    "14:00", "at 10"). A time with minutes or an am/pm marker wins
    over a bare number, so "for 2 people at 10am" reads 10am, not 2."""
    matches = list(_TIME_RE.finditer(text))
    for m in matches:
        hour, minute, marker = int(m.group(1)), m.group(2), m.group(3)
        if minute is not None or marker:
            return _normalise_hour(hour, int(minute) if minute else 0, marker)
    for m in matches:
        hour, minute, marker = int(m.group(1)), m.group(2), m.group(3)
        return _normalise_hour(hour, int(minute) if minute else 0, marker)
    return None


def _normalise_hour(hour: int, minute: int, marker: str | None) -> tuple[int, int] | None:
    if not 0 <= minute <= 59:
        return None
    if marker:
        if not 1 <= hour <= 12:
            return None
        if marker.lower() == "am":
            hour = 0 if hour == 12 else hour
        else:
            hour = 12 if hour == 12 else hour + 12
    elif not 0 <= hour <= 23:
        return None
    return hour, minute


def _interpret_time_text(time_text: str, now_utc: datetime.datetime) -> tuple[datetime.datetime, datetime.datetime]:
    """Interpret the named time text ("tomorrow 10:00 AM IST") into
    (start, end) aware datetimes, 1 hour long.

    Day: today / tomorrow / weekday name (next occurrence) / explicit
    date (2026-11-19, 19 Nov, Nov 19). Timezone: IST when "IST" is
    named, UTC for "UTC"/"GMT", otherwise IST (the user's local
    time). Unparseable text falls back to the old default shape —
    tomorrow 10:00 — in IST rather than UTC, so a bare "tomorrow"
    books the user's morning, not 15:30 IST."""
    text = time_text or ""
    tz = _IST
    if re.search(r"\butc\b|\bgmt\b", text, re.IGNORECASE):
        tz = datetime.timezone.utc
    now_tz = now_utc.astimezone(tz)

    # Explicit dates are digit-bearing, so blank them out before the
    # time search — otherwise "2026-11-19" reads its month as 11am.
    day: datetime.date | None = None
    scrubbed = text
    m = _ISO_DATE_RE.search(text)
    if m:
        try:
            day = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            day = None
        scrubbed = text[:m.start()] + " " * (m.end() - m.start()) + text[m.end():]
    if day is None:
        m = _DMY_DATE_RE.search(text)
        if m:
            year = int(m.group(3)) if m.group(3) else now_tz.year
            try:
                day = datetime.date(year, _MONTHS[m.group(2).lower()], int(m.group(1)))
            except ValueError:
                day = None
            scrubbed = text[:m.start()] + " " * (m.end() - m.start()) + text[m.end():]
    if day is None:
        m = _MDY_DATE_RE.search(text)
        if m:
            year = int(m.group(3)) if m.group(3) else now_tz.year
            try:
                day = datetime.date(year, _MONTHS[m.group(1).lower()], int(m.group(2)))
            except ValueError:
                day = None
            scrubbed = text[:m.start()] + " " * (m.end() - m.start()) + text[m.end():]

    clock = _parse_time_of_day(scrubbed)

    if day is None:
        low = text.lower()
        if re.search(r"\btomorrow\b", low):
            day = (now_tz + datetime.timedelta(days=1)).date()
        elif re.search(r"\btoday\b", low):
            day = now_tz.date()
        else:
            m = re.search(
                r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", low
            )
            if m:
                target = _WEEKDAYS[m.group(1)]
                days_ahead = (target - now_tz.weekday()) % 7
                if days_ahead == 0 and clock is not None and clock <= (now_tz.hour, now_tz.minute):
                    days_ahead = 7
                day = now_tz.date() + datetime.timedelta(days=days_ahead)
            else:
                day = (now_tz + datetime.timedelta(days=1)).date()

    hour, minute = clock if clock is not None else (10, 0)
    start_dt = datetime.datetime.combine(day, datetime.time(hour, minute), tzinfo=tz)
    return start_dt, start_dt + datetime.timedelta(hours=1)


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

    The named time text IS interpreted now (fix 7): day (today /
    tomorrow / weekday / explicit date), clock time (12h or 24h) and
    timezone (IST default, UTC/GMT when named). Before this fix the
    parse always booked tomorrow 10:00 AM UTC, so "SIH review
    tomorrow 10:00 AM IST" would have landed at 15:30 IST. Text with
    no usable time still falls back to tomorrow 10:00, in IST.
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
    start_dt, end_dt = _interpret_time_text(time_text, now)
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
                f"• **When:** {start_dt.strftime('%A, %B %d, %Y at %I:%M %p %Z')}\n"
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
