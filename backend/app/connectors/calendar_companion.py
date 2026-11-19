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
        # "add it / the Microsoft interview to my calendar": an object
        # between the verb and "to my calendar". The \S+ after the verb
        # keeps a bare "add to my calendar" (a question, no object) out.
        r"\b(?:add|put)\s+\S+[^.]{0,50}?\b(?:to|on|onto)\s+(?:my\s+)?calendar\b",
        r"\bgoogle\s+calendar\b",
    ]
    return any(re.search(p, t) for p in patterns)


_CREATE_RE = re.compile(
    r"\b(?:schedule|create|add|book)\s+(?:an?\s+)?(?:calendar\s+)?(?:event|meeting|appointment|call)\s+(?:called|titled|for)?\s*[\"']?([^\"'\n]+?)[\"']?\s+(?:on|at|for)\s+(.+)",
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


def _time_span(text: str) -> tuple[int, int] | None:
    """Span of the time expression in the text (day word / explicit
    date plus clock time), or None. Used to split a create instruction
    into title and time: the title is what remains after the verb
    phrase and the time span are removed. A clock time is required —
    a bare day word ("schedule a meeting with Priya tomorrow") is not
    a create: it must fail honestly, not silently default to 10:00.
    An explicit date without a clock still parses through the legacy
    fallback below (its 'on/at/for' shape carries the date)."""
    low = text.lower()
    starts: list[int] = []
    ends: list[int] = []
    # Clock time: the first time-like token (with minutes or am/pm).
    # Without one, there is no time expression to split on.
    clock_end = None
    has_clock = False
    for m in _TIME_RE.finditer(text):
        if m.group(2) is not None or m.group(3):
            starts.append(m.start())
            ends.append(m.end())
            clock_end = m.end()
            has_clock = True
            break
    if not has_clock:
        return None
    for pat in (
        r"\btoday\b",
        r"\btomorrow\b",
        # "next Monday" / "this Friday": the qualifier belongs to the
        # time, not the title — without it, "Add a calendar event next
        # Monday at 9:30am called Chat with Priya" titles the event
        # "next   called Chat with Priya".
        r"\b(?:next|this)\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    ):
        m = re.search(pat, low)
        if m:
            starts.append(m.start())
            ends.append(m.end())
    for rex in (_ISO_DATE_RE, _DMY_DATE_RE, _MDY_DATE_RE):
        m = rex.search(text)
        if m:
            starts.append(m.start())
            ends.append(m.end())
    end = max(ends)
    # A timezone marker right after the clock ("10:00 AM IST", "10am
    # UTC") belongs to the time, not the title — and _interpret_time_text
    # needs to see it to pick the right zone.
    if clock_end is not None:
        tz = re.match(r"\s+(IST|UTC|GMT)\b", text[clock_end:], re.IGNORECASE)
        if tz:
            end = max(end, clock_end + tz.end())
    return min(starts), end


_REMINDER_AMOUNT_RE = re.compile(
    r"\bremind(?:er|ers)?\s+(?:me\s+)?(\d+)\s*(days?|hours?|hrs?|minutes?|mins?)\s+before\b",
    re.IGNORECASE,
)


def _extract_reminder(text: str) -> tuple[str, int | None]:
    """Split a reminder clause out of a create instruction.

    \"with a reminder 3 days before\" is a reminder (3 days = 4320
    minutes before the event), not part of the event title — without
    this, the title comes out as \"... with a reminder 3 days before\"
    and the reminder is silently lost. Returns (cleaned text, minutes
    or None). \"remind me 2 hours before\" is the same shape.
    """
    m = _REMINDER_AMOUNT_RE.search(text or "")
    if not m:
        return text, None
    n = int(m.group(1))
    unit = m.group(2).lower()
    if unit.startswith("day"):
        minutes = n * 24 * 60
    elif unit.startswith("hour") or unit.startswith("hr"):
        minutes = n * 60
    else:
        minutes = n
    # Remove the whole reminder clause (\"with a reminder ... before\"
    # or \"remind me ... before\"), not just the amount.
    start = m.start()
    with_m = re.search(r"\bwith\s+a\s+$", (text or "")[:start], re.IGNORECASE)
    if with_m:
        start = with_m.start()
    cleaned = ((text or "")[:start] + (text or "")[m.end():]).strip()
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,;:!?\"'").strip()
    return cleaned, minutes


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

    The title is the instruction minus the verb phrase and the time
    span — not a regex capture up to the first "on/at/for", which
    swallowed the day word ("Create an event SIH review tomorrow at
    10am" used to title the event "SIH review tomorrow"). Text with
    no time expression is not a create (None), so it can never
    silently default to tomorrow 10:00.
    """
    # A reminder clause ("with a reminder 3 days before") is stripped
    # up front so it can neither pollute the title nor the time span;
    # its minutes ride along in the returned dict.
    instruction, reminder_minutes = _extract_reminder(instruction)
    verb = re.search(
        # One modifier word may sit between the article and the event
        # noun ("Book a dentist appointment", "Create a team meeting"):
        # without it the verb phrase never matches and a perfectly
        # parseable instruction returns None. The modifier + noun are
        # the title ("dentist appointment"), not filler to drop.
        # "Google" may precede "Calendar" ("Create a Google Calendar
        # event ..."): without the optional google slot the verb never
        # matches and the create silently falls through to a listing.
        r"\b(?:schedule|create|add|book)\s+(?:an?\s+)?(?:google\s+)?(?:calendar\s+)?(?:(\w+)\s+)?(event|meeting|appointment|call)\b",
        instruction or "",
        re.IGNORECASE,
    )
    if not verb:
        return None
    modifier = verb.group(1)
    noun = verb.group(2)
    tail = instruction[verb.end():]
    span = _time_span(tail)
    if span is not None:
        time_text = tail[span[0]:span[1]].strip()
        title = (tail[:span[0]] + " " + tail[span[1]:]).strip()
        if modifier:
            # The modifier + noun are the event's name when nothing
            # else remains ("Book a dentist appointment tomorrow at
            # 4pm" -> "dentist appointment"); when a title follows the
            # time it is appended after it.
            title = f"{modifier} {noun} {title}".strip()
        # Strip the connector words and punctuation around the title.
        # "with" is kept — "with Priya" says who the meeting is with.
        title = re.sub(r"^(?:called|titled|for|about|on|at)\s+", "", title, flags=re.IGNORECASE)
        title = re.sub(r"\s+(?:called|titled|on|at|for)\s*$", "", title, flags=re.IGNORECASE)
        # Attendee emails are captured separately; they are not the title.
        title = re.sub(r"[\w\.-]+@[\w\.-]+\.\w+", "", title)
        title = title.strip(" .,;:!?\"'").strip()
        # A trailing "for <title>" after the time ("tomorrow at 10am for
        # SIH review"): the title is the part after "for".
        fm = re.search(r"\bfor\s+([^\"'\n]+?)\s*[.!?]*$", title, re.IGNORECASE)
        if fm and fm.group(1).strip():
            title = fm.group(1).strip()
        title = re.sub(r"[\w\.-]+@[\w\.-]+\.\w+", "", title).strip(" .,;:!?\"'").strip()
        # Removing an email can leave a dangling "with" ("SIH review with").
        title = re.sub(r"\s+with\s*$", "", title, flags=re.IGNORECASE).strip()
        if title and time_text:
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
                "reminder_minutes": reminder_minutes,
                "start": start_dt,
                "end": end_dt,
            }
    # No day/clock in the text: fall back to the legacy title-first
    # shape ("schedule a meeting called Chat on whenever"). The time
    # text is then interpreted with the documented fallback (tomorrow
    # 10:00 IST), never silent UTC. This is safe here because there is
    # no real time for the title capture to swallow. If even that
    # shape is absent ("Create a calendar event for SIH review"),
    # this is not a create.
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
        "reminder_minutes": reminder_minutes,
        "start": start_dt,
        "end": end_dt,
    }


async def handle_calendar_instruction(instruction: str, chat_id: Optional[str] = None, user_id: Optional[str] = None) -> str:
    """Handle calendar listing or event creation requests via connected Google OAuth.

    ``user_id`` is the task owner's id: a session user's call verifies
    and uses that user's own Google connection, never the legacy globals.
    """
    t = instruction.lower()

    # 1. Verify Google Connection
    google_status = oauth_manager.verify_connection("google", user_id=user_id)
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
    connector = oauth_manager.connector("google_calendar", user_id=user_id)

    # 2. Check for Event Creation
    parsed = parse_calendar_create(instruction, account_email)
    if parsed is not None:
        title = parsed["title"]
        start_dt = parsed["start"]
        end_dt = parsed["end"]
        attendees = parsed["attendees"]
        reminder_minutes = parsed.get("reminder_minutes")

        try:
            reminders = None
            if reminder_minutes is not None:
                reminders = [{"method": "popup", "minutes": reminder_minutes}]
            invite = connector.create_invite(
                calendar_id="primary",
                title=title or "New Meeting",
                start=start_dt.isoformat(),
                end=end_dt.isoformat(),
                attendees=attendees,
                reminders=reminders,
            )
            html_link = invite.get("htmlLink", "")
            link_md = f"\n• **Calendar Link:** [Open in Google Calendar]({html_link})" if html_link else ""
            att_text = f"\n• **Attendees:** {', '.join(attendees)}" if attendees else ""
            if reminder_minutes is not None:
                if reminder_minutes % 1440 == 0:
                    rem_text = f"\n• **Reminder:** {reminder_minutes // 1440} day{'s' if reminder_minutes // 1440 != 1 else ''} before (popup)"
                elif reminder_minutes % 60 == 0:
                    rem_text = f"\n• **Reminder:** {reminder_minutes // 60} hour{'s' if reminder_minutes // 60 != 1 else ''} before (popup)"
                else:
                    rem_text = f"\n• **Reminder:** {reminder_minutes} minute{'s' if reminder_minutes != 1 else ''} before (popup)"
            else:
                rem_text = ""

            return (
                f"### ✓ Google Calendar Invite Created\n\n"
                f"• **Event:** **{title}**\n"
                f"• **When:** {start_dt.strftime('%A, %B %d, %Y at %I:%M %p %Z')}\n"
                f"• **Calendar:** `{account_email}`{att_text}{rem_text}{link_md}\n\n"
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


# --- Compound instructions: a calendar event asked for alongside
# another companion task (email monitoring, tracking, ...) ---

_CALENDAR_CREATE_VERB_RE = re.compile(
    r"\b(?:create|add|schedule|book)\s+(?:an?\s+)?(?:google\s+)?(?:calendar\s+)?(?:\w+\s+)?(?:event|meeting|appointment|call)\b"
    # "add it / the Microsoft interview to my calendar": the clause
    # starts at the verb, with the event object before "to my calendar".
    r"|\b(?:add|put)\s+\S+[^.]{0,50}?\b(?:to|on|onto)\s+(?:my\s+)?calendar\b",
    re.IGNORECASE,
)
# The thing the event is about, named in the other half of the
# instruction: "find the Microsoft interview invitation" -> the event
# is the "Microsoft interview". The proper-noun part must be
# capitalised (so a lowercase "the" is never the referent); the noun
# itself is matched case-insensitively.
_REFERENT_RE = re.compile(
    r"([A-Z][\w]*(?:\s+[A-Z][\w]*)*)\s+"
    r"(?i:(interview|invitation|meeting|deadline|assessment|exam|event|internship|offer|call|appointment))\b"
)
_PRONOUN_OBJECT_RE = re.compile(
    r"(\b(?:for|called|titled|about)\s+)(it|that|this|them)\b"
    # Direct object after add/put/schedule/book: "add it to my calendar".
    r"|(\b(?:add|put|schedule|book)\s+)(it|that|this|them)\b",
    re.IGNORECASE,
)


def extract_calendar_create_clause(instruction: str) -> str | None:
    """Return the calendar-create clause of an instruction, or None.

    A compound instruction puts the calendar ask after (or before)
    another task: \"Monitor my email ..., find the Microsoft interview
    invitation, and create a Google Calendar event for it tomorrow at
    10am ...\". The clause is the text from the calendar verb to the
    end of the instruction.
    """
    m = _CALENDAR_CREATE_VERB_RE.search(instruction or "")
    if not m:
        return None
    return (instruction or "")[m.start():].strip().strip(" .,;:!?\"'").strip() or None


def resolve_calendar_event_pronoun(clause: str, instruction: str) -> str:
    """Replace a bare pronoun event object with its referent.

    \"create a Google Calendar event for it tomorrow at 10am\" — \"it\"
    is the interview invitation named in the email half of the
    instruction. Without this the event would be titled \"it\". The
    referent is searched in the instruction outside the calendar
    clause, so it works whether the email half comes first or second.
    When no referent is found the clause is returned unchanged and the
    caller must refuse to create a pronoun-titled event.
    """
    if not _PRONOUN_OBJECT_RE.search(clause or ""):
        return clause
    rest = (instruction or "").replace(clause, " ", 1)
    m = _REFERENT_RE.search(rest)
    if not m:
        return clause
    referent = f"{m.group(1)} {m.group(2)}".strip()
    # A trailing "invitation" after "interview" is noise in a title:
    # "Microsoft interview invitation" -> "Microsoft interview".
    referent = re.sub(r"\s+interview\s+invitation$", " interview", referent, flags=re.IGNORECASE)
    return _PRONOUN_OBJECT_RE.sub(
        lambda mm: (mm.group(1) or mm.group(3)) + referent, clause, count=1
    )


async def handle_compound_calendar(instruction: str, chat_id: Optional[str] = None, user_id: Optional[str] = None) -> str | None:
    """Handle the calendar half of a compound instruction.

    The dispatcher routes a compound instruction to the first matching
    companion (email monitoring wins the if/elif chain), so the
    calendar half would otherwise vanish silently — no event, no
    message, nothing. This runs the calendar clause through the normal
    calendar handler and returns its result to append to the first
    half's answer, or None when there is no calendar-create clause.
    A clause that is present but unparseable, or whose event is still
    a bare pronoun after resolution, gets an honest note instead of a
    silently dropped ask or a nonsense-titled event.
    """
    if not is_calendar_query(instruction):
        return None
    clause = extract_calendar_create_clause(instruction)
    if clause is None:
        return None
    resolved = resolve_calendar_event_pronoun(clause, instruction)
    parsed = parse_calendar_create(resolved)
    if parsed is None:
        return (
            "### 📅 Calendar\n\n"
            "I couldn't work out the calendar event in that instruction — "
            "I couldn't parse a time or title from it. Please say it again "
            "as its own message, e.g. *'Create a calendar event tomorrow at "
            "10am for Microsoft interview'*."
        )
    if parsed["title"].strip().lower() in ("it", "that", "this", "them"):
        return (
            "### 📅 Calendar\n\n"
            "I found the email part, but the calendar event is still just "
            f"\"{parsed['title'].strip()}\" — I couldn't tell which event to "
            "put on your calendar. Please name it, e.g. *'Create a calendar "
            "event tomorrow at 10am for Microsoft interview'*."
        )
    return await handle_calendar_instruction(resolved, chat_id=chat_id, user_id=user_id)
