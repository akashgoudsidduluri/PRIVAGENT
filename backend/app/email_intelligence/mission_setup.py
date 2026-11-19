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
    # "check/scan/monitor my gmail/email/inbox every N minutes (or every
    # hour/day)": a monitoring setup, not a one-off read. The cadence
    # may be numbered ("every 5 minutes") or bare ("every hour");
    # "second" is excluded from the bare form ("every second Tuesday"
    # is a weekday, not an interval). The mailbox may carry ".com"
    # ("outlook.com"/"gmail.com") — the filler window cannot cross a ".".
    r"\b(?:check|scan|monitor)\s+(?:my\s+)?(?:e-?mails?|mail|inbox|gmail(?:\.com)?|outlook(?:\.com)?)\b[^.]{0,60}?\bevery\s+(?:\d+\s*(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?)|(?:hours?|hrs?|minutes?|mins?|days?))\b|"
    r"\b(?:continuously\s+scan|monitor|moinitor|scan\s+and\s+clean|clean\s+up|cleanup|scan\s+my)\s+(?:my\s+)?emails?\b|"
    r"\b(?:identify\s+unnecessary\s+emails|remove\s+unnecessary|detect\s+important\s+emails|email\s+monitoring)\b|"
    r"\b(?:remind|notify|alert)\s+(?:me\s+)?(?:if|when|about)\s+.*?\b(?:emails?|mails?|inbox)\b|"
    r"\b(?:emails?|mails?|inbox)\b.*?\b(?:notify|alert|remind)\s+me\b|"
    r"\b(?:monitor|watch|track)\b.*?\b(?:emails?|mails?|inbox)\b",
    re.IGNORECASE,
)

# Mission-control phrasings. These must NOT be treated as a setup
# instruction: "pause email monitoring" matches SETUP_TRIGGER
# ("email monitoring"), so without this guard the re-bind would
# re-activate the mission instead of pausing it.
_CONTROL_PAUSE_RE = re.compile(r"\bpause\b[^.]{0,30}?\b(?:email|mail|inbox|monitoring|scan)\b", re.IGNORECASE)
_CONTROL_RESUME_RE = re.compile(r"\bresume\b[^.]{0,30}?\b(?:email|mail|inbox|monitoring|scan)\b", re.IGNORECASE)
_CONTROL_STOP_RE = re.compile(
    r"\b(?:cancel|stop)\b[^.]{0,30}?\b(?:email\s+)?(?:mission|monitoring)\b"
    r"|\bstop\s+(?:monitoring\s+)?(?:my\s+)?(?:email|mail|inbox)\b",
    re.IGNORECASE,
)


# Words that mark a capture as a clause, not a topic: a pronoun or
# determiner at the start ("for it"), a time expression ("tomorrow at
# 10am"), a reminder ("with a reminder"), a calendar word, or a verb
# ("create a ... event"). A topic is a short noun phrase
# ("internships", "job offers", "SDE internships").
_PRONOUN_START_RE = re.compile(
    r"^(?:it|this|that|these|those|the|a|an|my|your|his|her|their|our|them|us|me)\b",
    re.IGNORECASE,
)
_TIME_WORD_RE = re.compile(
    r"\b(?:today|tomorrow|yesterday|monday|tuesday|wednesday|thursday|friday|saturday|sunday|next|last)\b|\b\d",
    re.IGNORECASE,
)
_CLAUSE_WORD_RE = re.compile(
    r"\b(?:remind|reminder|calendar|event|create|monitor|scan|find|check|add|book|schedule|send|delete)\b",
    re.IGNORECASE,
)
# A phrase that is only a generic "important mail(s)" reference is
# not a topic: "notify for any important mails" must not produce
# the topic "any important mails". Real topics are nouns like
# "internships" or "job offers", not a restatement of "important mail".
_GENERIC_IMPORTANT_RE = re.compile(
    r"^(?:any|all|the|some|my)?\s*(?:important\s+)?(?:mails?|emails?|inbox)$",
    re.IGNORECASE,
)


def _is_plausible_topic(p: str) -> bool:
    """True when a candidate string looks like an email alert topic.

    Guards the extractor against a compound instruction's second half:
    \"create a Google Calendar event for it tomorrow at 10am with a
    reminder 3 days before\" used to yield the topic \"it tomorrow at
    10am with a\" — the \"for\" introducer plus \"remind\" matching inside
    \"reminder\". A topic is a short noun phrase; a pronoun, a time, a
    digit, a reminder, or a clause verb means the capture is not one.
    """
    t = (p or "").strip().lower()
    if len(t) <= 2 or len(t.split()) > 4:
        return False
    if _PRONOUN_START_RE.search(t):
        return False
    if _TIME_WORD_RE.search(t):
        return False
    if _CLAUSE_WORD_RE.search(t):
        return False
    return True


def extract_custom_topics(text: str) -> list[str]:
    """Extract requested custom alert topics from the prompt (e.g. internships, jobs)."""
    # The terminator words are whole words: without the trailing \b,
    # "remind" matches inside "reminder" and the capture stops mid-word
    # ("for it tomorrow at 10am with a reminder" -> "it tomorrow at
    # 10am with a"). A capture that still looks like a clause is
    # dropped by _is_plausible_topic below.
    m = re.search(r"\b(?:rel+ated\s+to|about|for|regarding|concerning|on)\s+(.+?)(?:\s+(?:notify|alert|remind|then|please)\b|$)", text, re.IGNORECASE)
    if m:
        raw = m.group(1).strip()
        parts = re.split(r"\s*(?:,|and|\bor\b)\s*", raw)
        clean = []
        for p in parts:
            # Strip trailing sentence punctuation: "notify me about
            # urgent ones." must yield "urgent ones", not "urgent ones."
            p_clean = p.strip().lower().strip(".,;:!?\"'").strip()
            if p_clean in ("one", "ones"):
                continue
            if _GENERIC_IMPORTANT_RE.match(p_clean):
                continue
            if len(p_clean) > 2 and p_clean not in ("mail", "email", "emails", "inbox", "important", "imp"):
                if ("inter" in p_clean and "ship" in p_clean) or "interh" in p_clean:
                    p_clean = "internships"
                if _is_plausible_topic(p_clean):
                    clean.append(p_clean)
        return clean
    return []


FREQUENCY_RE = re.compile(
    r"\bevery\s+(\d+)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?)\b", re.IGNORECASE
)
# Bare cadence: "every hour" / "every day" (no number) means one unit.
# "second" is deliberately excluded from the bare form: "every second
# Tuesday" is a weekday, not a 1-second interval.
BARE_FREQUENCY_RE = re.compile(
    r"\bevery\s+(hours?|hrs?|minutes?|mins?|days?)\b", re.IGNORECASE
)
# An explicit deletion authorization in the setup instruction itself:
# "delete them", "trash promotional emails", "move to trash". "Remove"
# is deliberately NOT in this list — it is ambiguous (remove from my
# attention / flag for review), and the original two-step design keeps
# setup in dry-run until a separate authorization. "Delete"/"trash"
# are unambiguous, so they authorize in one step.
DELETE_DIRECTIVE_RE = re.compile(
    r"(?:\b(?:delete|trash)\b[^.]{0,80}"
    r"\b(?:them|those|unnecessary|promotion(?:al)?|newsletters?|junk|spam|emails?|mails?)\b"
    r"|\bmove\s+(?:them\s+)?to\s+trash\b"
    r"|\b(?:them|those)\b[^.]{0,30}\bto\s+trash\b)",
    re.IGNORECASE,
)
DELETE_NEGATION_RE = re.compile(
    r"\b(?:don't|do not|never|not)\s+(?:delete|trash)\b", re.IGNORECASE
)


# Mail providers explicitly named in an instruction. "Microsoft interview"
# is a TOPIC, not a mailbox — bare "microsoft" must not select Outlook.
# Only a mailbox-anchored "microsoft" ("microsoft account/inbox/mail/365")
# or "outlook" itself names the provider.
_OUTLOOK_PROVIDER_RE = re.compile(
    r"\boutlook\b"
    r"|\bmicrosoft\s+(?:365|account|inbox|mail|email|outlook)\b"
    r"|\boffice\s*365\b|\bo365\b",
    re.IGNORECASE,
)
_GMAIL_PROVIDER_RE = re.compile(r"\b(?:gmail|google)\b", re.IGNORECASE)


def named_email_providers(text: str) -> set[str]:
    """The mail providers the instruction names, if any.

    Returns a subset of {"outlook", "gmail"}; empty when no provider is
    named, so the caller falls back to the connected account order.
    """
    t = text or ""
    out: set[str] = set()
    if _OUTLOOK_PROVIDER_RE.search(t):
        out.add("outlook")
    if _GMAIL_PROVIDER_RE.search(t):
        out.add("gmail")
    return out


def parse_frequency_seconds(text: str) -> int | None:
    """Parse 'every N minutes/hours/seconds' into seconds, or None.

    A bare cadence ('every hour', 'every day') means one unit.
    Clamped to the mission table's CHECK (min 60s) and to 24h max.
    """
    m = FREQUENCY_RE.search(text or "")
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
    else:
        b = BARE_FREQUENCY_RE.search(text or "")
        if not b:
            return None
        n = 1
        unit = b.group(1).lower()
    if unit.startswith("day"):
        secs = n * 86400
    elif unit.startswith("hour") or unit.startswith("hr"):
        secs = n * 3600
    elif unit.startswith("min"):
        secs = n * 60
    else:
        secs = n
    return max(60, min(secs, 86400))


def format_frequency_seconds(text: str, seconds: int) -> str:
    """Human frequency line, echoing the user's own phrasing when one
    was stated ('every 60 minutes' stays '60 minutes', not '1 hour')."""
    m = FREQUENCY_RE.search(text or "")
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
    else:
        b = BARE_FREQUENCY_RE.search(text or "")
        if b:
            n = 1
            unit = b.group(1).lower()
        else:
            n = None
            unit = ""
    if n is not None:
        if unit.startswith("day"):
            word = "day" if n == 1 else "days"
        elif unit.startswith("hour") or unit.startswith("hr"):
            word = "hour" if n == 1 else "hours"
        elif unit.startswith("min"):
            word = "minute" if n == 1 else "minutes"
        else:
            word = "second" if n == 1 else "seconds"
        return f"Every {n} {word} ({seconds} seconds)"
    if seconds % 3600 == 0:
        h = seconds // 3600
        return f"Every {h} hour{'s' if h > 1 else ''} ({seconds} seconds)"
    if seconds % 60 == 0:
        mnt = seconds // 60
        return f"Every {mnt} minute{'s' if mnt > 1 else ''} ({seconds} seconds)"
    return f"Every {seconds} seconds"


def has_explicit_delete_directive(text: str) -> bool:
    """True when the setup instruction itself authorizes deletion.

    A negated directive ("don't delete anything") is not authorization.
    """
    t = text or ""
    if DELETE_NEGATION_RE.search(t):
        return False
    return bool(DELETE_DIRECTIVE_RE.search(t))


def is_email_mission_setup_instruction(text: str) -> bool:
    """Return True if user is instructing PrivAgent to configure continuous email monitoring."""
    t = (text or "").strip()
    if not t:
        return False
    # A control instruction is not a setup: "pause email monitoring"
    # and "stop email monitoring" both match SETUP_TRIGGER ("email
    # monitoring"), but they must pause/stop the mission, not re-bind
    # it to active. The control dispatcher handles them first.
    if _CONTROL_PAUSE_RE.search(t) or _CONTROL_RESUME_RE.search(t) or _CONTROL_STOP_RE.search(t):
        return False
    return bool(SETUP_TRIGGER.search(t))


# --- Chat control: interval changes and deletion receipts ---
# These are instructions the agent loop must handle deterministically.
# Before this fix, "check for every 5 minutes" matched NO trigger
# (it has no email/mail word), so it fell through to the LLM, which
# hallucinated a confirmation while the mission stayed at 3600s.
# Likewise "send '.' when you delete" was agreed to in chat but had
# no code behind it. Both are real handlers now.

_INTERVAL_VERB_RE = re.compile(
    r"\b(?:check|scan|monitor|sync|refresh)\b[^.]{0,40}?\bevery\s+\d+\s*(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?)\b"
    r"|\bchange\b[^.]{0,40}?\bevery\s+\d+\s*(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?)\b"
    r"|^\s*every\s+\d+\s*(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?)\b"
    # Bare cadence ("check every hour", "every day") means one unit.
    # "second" is excluded: "every second Tuesday" is a weekday.
    r"|\b(?:check|scan|monitor|sync|refresh|change)\b[^.]{0,40}?\bevery\s+(?:hours?|hrs?|minutes?|mins?|days?)\b"
    r"|^\s*every\s+(?:hours?|hrs?|minutes?|mins?|days?)\b",
    re.IGNORECASE,
)
# A non-email object means the interval change is not about email:
# "monitor this product every 5 minutes" is a product watch,
# "check my flight status every 5 minutes" is a flight check,
# "refresh this page every 10 minutes" is a browser refresh — none
# of them should silently change the email interval.
_NON_EMAIL_OBJECT_RE = re.compile(
    r"\b(?:product|flight|page|website|webpage|stock|stocks?|crypto|weather|train|bus|order|package|delivery|news|ticket|hotel|share|shares?|bitcoin|ethereum|url|link|teams|slack|telegram|discord|whatsapp)\b",
    re.IGNORECASE,
)


def is_email_interval_change_instruction(text: str) -> bool:
    """True when the user asks to change how often email is checked.

    A full setup instruction that happens to state a frequency
    ("scan my mails every 60 minutes and delete promo") is NOT an
    interval change — setup handles its own frequency. A price-watch
    interval ("check the price every 5 minutes") is not email either.
    A non-email object ("monitor this product every 5 minutes",
    "check my flight status every 5 minutes", "refresh this page
    every 10 minutes") is a product/flight/page watch, not email —
    it must not silently change the email interval. A bare interval
    change ("check every 5 minutes") is ambiguous; the routing
    guards it with the active-mission check."""
    t = (text or "").strip()
    if not t:
        return False
    if is_email_mission_setup_instruction(t):
        return False
    if re.search(r"\bprice\b", t, re.IGNORECASE):
        return False
    if re.search(r"\b(?:delete|trash)\b", t, re.IGNORECASE):
        return False
    if _NON_EMAIL_OBJECT_RE.search(t):
        return False
    if re.search(r"https?://|www\.|\.com\b|\.in\b", t, re.IGNORECASE):
        return False
    return bool(_INTERVAL_VERB_RE.search(t))


def get_active_email_mission(chat_id: Optional[str] = None, db_path: Optional[str] = None, user_id: Optional[str] = None) -> Optional[dict[str, Any]]:
    """The active email mission for this chat, or the single active one.

    ``user_id`` scopes the search to the caller's missions: the
    single-mission fallback must never return another user's mission.
    """
    missions = [m for m in db.list_email_missions(db_path=db_path, user_id=user_id) if m.get("status") == "active"]
    if not missions:
        return None
    if chat_id:
        for m in missions:
            if m.get("conversation_id") == chat_id:
                return m
    if len(missions) == 1:
        return missions[0]
    return None


async def handle_email_interval_change(
    instruction: str,
    chat_id: Optional[str] = None,
    user_id: Optional[str] = None,
    db_path: Optional[str] = None,
) -> str:
    """Change the active email mission's check interval. The reply
    quotes the value read back from the DB, so it cannot claim an
    update that did not persist."""
    secs = parse_frequency_seconds(instruction)
    if secs is None:
        return "I couldn't tell what interval you want. Say it like 'check every 5 minutes'."
    mission = get_active_email_mission(chat_id, db_path=db_path, user_id=user_id)
    if not mission:
        return "There's no active email monitoring mission in this chat to change. Set one up first (for example: 'scan my mails and notify me for important mails')."
    prev = mission.get("frequency_seconds")
    db.update_email_mission(
        mission["id"],
        {"frequency_seconds": secs, "next_run_at": time.time()},
        db_path=db_path,
    )
    updated = db.get_email_mission(mission["id"], db_path=db_path)
    actual = updated.get("frequency_seconds") if updated else None
    if actual != secs:
        return f"I tried to change the interval to every {secs} seconds, but the stored value is still {actual}. The change did not persist — please check the dashboard."
    freq_text = format_frequency_seconds(instruction, actual)
    prev_text = f"every {prev} seconds" if prev else "unset"
    return (
        f"Email check interval updated: {prev_text} → **{freq_text}**. "
        f"The next check runs immediately, then {freq_text.lower()}."
    )


_DELETE_RECEIPT_DOT_RE = re.compile(r"['\"]\s*\.\s*['\"]")
_DELETE_RECEIPT_WORD_RE = re.compile(
    r"\bsend\b[^.]{0,40}?\bdeleted\b|\bdeleted\b[^.]{0,40}?\bevery\s*time\b",
    re.IGNORECASE,
)


def is_delete_receipt_instruction(text: str) -> bool:
    """True when the user asks for a receipt ("." or "deleted") on
    every deletion, e.g. "whenever you delete any mail just send '.'
    here in this chat" or "send 'deleted' every time you delete".
    A full setup instruction is not a receipt request even if it
    mentions deletion — setup handles its own authorization."""
    t = text or ""
    if is_email_mission_setup_instruction(t):
        return False
    if not re.search(r"\b(?:delete|trash)\b", t, re.IGNORECASE):
        return False
    if _DELETE_RECEIPT_DOT_RE.search(t):
        return True
    if _DELETE_RECEIPT_WORD_RE.search(t):
        return True
    return False


def _receipt_text_for(instruction: str) -> str:
    # The user asked for "." first; "deleted" is the readable form.
    # Prefer the word when they name it, else the dot.
    if re.search(r"\bdeleted\b", instruction or "", re.IGNORECASE):
        return "deleted"
    return "."


async def handle_delete_receipt_instruction(
    instruction: str,
    chat_id: Optional[str] = None,
    db_path: Optional[str] = None,
    user_id: Optional[str] = None,
) -> str:
    """Record the user's deletion-receipt preference on the active
    email mission, so the background tick posts it on every trash."""
    mission = get_active_email_mission(chat_id, db_path=db_path, user_id=user_id)
    if not mission:
        return "There's no active email monitoring mission in this chat to attach that receipt to. Set one up first."
    receipt = _receipt_text_for(instruction)
    pol = dict(mission.get("notification_policy") or {})
    pol["delete_receipt_text"] = receipt
    db.update_email_mission(
        mission["id"],
        {"notification_policy_json": json.dumps(pol)},
        db_path=db_path,
    )
    return (
        f"Done — from now on, every time this mission moves a mail to Trash, I'll post '{receipt}' here in this chat. "
        "That receipt is posted by the background check itself, so it works even when I'm not chatting with you."
    )


# --- Mission control: pause / resume / dry-run / authorize / sync / cancel / topics ---
# These chat phrasings previously had NO deterministic handler. Some
# matched SETUP_TRIGGER and did the opposite ("pause email monitoring"
# re-activated the mission); the rest fell through to the LLM, which
# hallucinated confirmations. Each one below is a real handler that
# reads the result back from the DB.

_DRY_RUN_RE = re.compile(
    r"\bswitch\s+to\s+dry[\s-]?run\b|\bdry[\s-]?run\s+mode\b"
    r"|\bstop\s+deleting\b|\bstop\b[^.]{0,40}?\bfrom\s+deleting\b"
    r"|\bdisable\s+(?:autonomous\s+)?delet(?:ion|e)\b"
    r"|\bonly\s+flag\b|\bdo\s+not\s+delete\b|\bdon't\s+delete\b|\bnever\s+delete\b",
    re.IGNORECASE,
)
# Turn off the deletion receipt ("stop sending '.'", "stop the
# deletion receipt"). Without this, once set, the receipt posts
# forever with no way to stop it from chat. Note: no \b before a
# quote — a quote is not a word character, so \b never matches
# there.
_RECEIPT_OFF_RE = re.compile(
    r"\bstop\s+(?:sending|posting)\b[^.]{0,30}?(?:['\"]\s*\.\s*['\"]|\bdeleted\b|\breceipt\b)"
    r"|\bstop\s+the\s+(?:deletion\s+)?receipt\b"
    r"|\b(?:don't|do\s+not|never)\s+send\b[^.]{0,20}?(?:['\"]\s*\.\s*['\"]|\bdeleted\b)",
    re.IGNORECASE,
)
_AUTHORIZE_SHORT_RE = re.compile(
    r"\bdelete\s+them\b|\btrash\s+them\b"
    r"|\bdelete\s+(?:unnecessary|promotional?|junk|spam|the\s+unnecessary)\b[^.]{0,30}?\b(?:emails?|mails?)\b"
    r"|\btrash\s+(?:unnecessary|promotional?)\b[^.]{0,30}?\b(?:emails?|mails?)\b"
    r"|\b(?:yes|yeah|ok|okay|go\s+ahead)\b[^.]{0,25}?\bdelete\b",
    re.IGNORECASE,
)
_SYNC_NOW_RE = re.compile(
    r"\bsync\s+(?:my\s+)?(?:email|mail|inbox)?\s*now\b|\bcheck\s+(?:my\s+)?(?:email|mail|inbox)?\s*now\b"
    r"|\bsync\s+now\b",
    re.IGNORECASE,
)


def _is_question(text: str) -> bool:
    """True when the instruction is a question, not a command.
    'should I delete them?' asks whether to delete; it must not
    authorize deletion. A trailing '?' or an interrogative opening
    ('should', 'can', 'shall', 'do', 'would', 'could', 'will', 'is')
    marks it."""
    t = (text or "").strip()
    if not t:
        return False
    if t.endswith("?"):
        return True
    return bool(re.match(r"^(?:should|can|shall|do|does|did|would|could|will|is|are|am)\b", t, re.IGNORECASE))


def _is_authorize_short_instruction(text: str) -> bool:
    """True for a bare authorization ('delete them', 'yes, delete
    them'). A question ('should I delete them?') is NOT an
    authorization — it must not flip policy_authorized."""
    t = text or ""
    if _is_question(t):
        return False
    return bool(_AUTHORIZE_SHORT_RE.search(t))


def _is_sync_now_instruction(text: str) -> bool:
    """True for a sync command ('sync now', 'check my email now').
    A question with a purpose ('check my inbox now for the interview
    invite') is NOT a sync — it asks whether something arrived, and
    a tick's stats do not answer it."""
    t = text or ""
    if _is_question(t):
        return False
    if re.search(r"\bnow\s+for\b", t, re.IGNORECASE):
        return False
    return bool(_SYNC_NOW_RE.search(t))
_TOPICS_CHANGE_RE = re.compile(
    r"\bonly\s+notify\s+(?:me\s+)?about\s+(.+)$"
    r"|\bchange\s+topics?\s+to\s+(.+)$"
    r"|\bstop\s+monitoring\s+(?!my\s+(?:email|mail|inbox)\b|the\s+email\b)(.+)$",
    re.IGNORECASE,
)


def is_email_mission_control_instruction(text: str) -> bool:
    """True when the user is controlling an existing email mission
    (receipt, pause, resume, dry-run, authorize, sync, cancel,
    topics) rather than setting one up or asking about it. The
    interval change is NOT included here — it is handled by a
    separate routing branch guarded by the active-mission check, so
    a product/flight/page watch is never hijacked into an email
    interval change. A full setup sentence wins over control:
    'monitor my email and delete unnecessary emails' is a setup
    (create + authorize), not a bare authorize of an existing
    mission."""
    t = (text or "").strip()
    if not t:
        return False
    if is_email_mission_setup_instruction(t):
        return False
    if is_delete_receipt_instruction(t):
        return True
    if _RECEIPT_OFF_RE.search(t):
        return True
    if _CONTROL_PAUSE_RE.search(t) or _CONTROL_RESUME_RE.search(t) or _CONTROL_STOP_RE.search(t):
        return True
    if _DRY_RUN_RE.search(t) or _is_authorize_short_instruction(t) or _is_sync_now_instruction(t):
        return True
    if _TOPICS_CHANGE_RE.search(t):
        return True
    return False


async def handle_email_mission_control(
    instruction: str,
    chat_id: Optional[str] = None,
    user_id: Optional[str] = None,
    db_path: Optional[str] = None,
) -> str:
    """Handle a mission-control instruction deterministically. Always
    returns a reply (never None) when the control detector matched.
    The interval change is not handled here — it has its own routing
    branch guarded by the active-mission check, so a product/flight
    watch is never hijacked into an email interval change."""
    t = instruction or ""
    if is_delete_receipt_instruction(t):
        return await handle_delete_receipt_instruction(t, chat_id=chat_id, db_path=db_path, user_id=user_id)

    mission = get_active_email_mission(chat_id, db_path=db_path, user_id=user_id)
    # A paused mission is not "active". When a control command finds
    # no active mission but one exists paused in this chat, say so —
    # "no active mission" would be misleading.
    paused_mission = None
    if mission is None:
        paused = [m for m in db.list_email_missions(db_path=db_path, user_id=user_id) if m.get("status") == "paused"]
        if chat_id:
            paused = [m for m in paused if m.get("conversation_id") == chat_id]
        paused_mission = paused[0] if paused else None

    if _RECEIPT_OFF_RE.search(t):
        if not mission and not paused_mission:
            return "There's no email monitoring mission in this chat with a deletion receipt to stop."
        target = mission or paused_mission
        pol = dict(target.get("notification_policy") or {})
        pol.pop("delete_receipt_text", None)
        db.update_email_mission(
            target["id"], {"notification_policy_json": json.dumps(pol)}, db_path=db_path
        )
        return "Done — I won't post a '.' or 'deleted' receipt on deletions anymore."

    if _CONTROL_PAUSE_RE.search(t):
        if not mission:
            if paused_mission:
                return "Email monitoring is already paused."
            return "There's no active email monitoring mission in this chat to pause."
        db.update_email_mission(mission["id"], {"status": "paused"}, db_path=db_path)
        stats = db.get_email_dashboard_stats(mission["id"], db_path=db_path)
        trashed = stats.get("moved_to_trash", 0)
        return (
            f"Email monitoring paused. Nothing will be scanned or deleted until you say 'resume email'. "
            f"Your {trashed} trashed mail(s) stay in Trash (restorable)."
        )
    if _CONTROL_RESUME_RE.search(t):
        # A paused mission is not "active", so the lookup above
        # misses it — resume only a paused mission in THIS chat,
        # never another chat's mission.
        target = mission or paused_mission
        if not target:
            return "There's no paused email monitoring mission in this chat to resume. Set one up first."
        db.update_email_mission(
            target["id"], {"status": "active", "next_run_at": time.time()}, db_path=db_path
        )
        return "Email monitoring resumed. The next check runs immediately."
    if _DRY_RUN_RE.search(t):
        if not mission:
            if paused_mission:
                return "Email monitoring is paused. Say 'resume email' first, then I can switch it to dry-run."
            return "There's no active email monitoring mission in this chat to switch to dry-run."
        db.update_email_mission(
            mission["id"], {"policy_authorized": 0, "dry_run": 1}, db_path=db_path
        )
        return (
            "Switched to dry-run review. From now on I will only FLAG unnecessary mail for your review — "
            "nothing will be moved to Trash until you explicitly authorize deletion again (say 'delete them')."
        )
    if _is_authorize_short_instruction(t):
        if not mission:
            if paused_mission:
                return "Email monitoring is paused. Say 'resume email' first, then I can authorize deletion."
            return (
                "There's no active email monitoring mission in this chat to authorize. "
                "Set one up first — for example: 'monitor my email and delete unnecessary emails'."
            )
        db.update_email_mission(
            mission["id"], {"policy_authorized": 1, "dry_run": 0}, db_path=db_path
        )
        return (
            "Autonomous deletion authorized. Eligible unnecessary mail (promotions, newsletters, "
            "notifications at 80%+ confidence) will be moved to Trash on each check. "
            "Protected categories (banking, security, academic, recruitment, personal, deadlines) are never auto-deleted."
        )
    if _is_sync_now_instruction(t):
        if not mission:
            if paused_mission:
                return "Email monitoring is paused. Say 'resume email' first, then I can sync."
            return "There's no active email monitoring mission in this chat to sync."
        from app.email_intelligence import sync_engine
        result = await sync_engine.run_email_mission_tick(mission["id"], db_path=db_path)
        stats = db.get_email_dashboard_stats(mission["id"], db_path=db_path)
        if result.get("status") == "error":
            return f"Sync failed: {result.get('reason', 'unknown error')}. The mission stays active and will retry on its next scheduled check."
        return (
            f"Sync complete. Totals so far (all checks, not just this one): "
            f"{stats.get('total_processed', 0)} messages processed, "
            f"{stats.get('moved_to_trash', 0)} in Trash, "
            f"{stats.get('neglected_awaiting_attention', 0)} important emails awaiting attention."
        )
    if _CONTROL_STOP_RE.search(t) and not _TOPICS_CHANGE_RE.search(t):
        if not mission:
            if paused_mission:
                return "Email monitoring is already stopped (paused)."
            return "There's no active email monitoring mission in this chat to stop."
        db.update_email_mission(mission["id"], {"status": "paused"}, db_path=db_path)
        return (
            "Email monitoring stopped (paused). Nothing further will be scanned or deleted. "
            "Say 'resume email' to start it again, or delete the mission from the dashboard if you want it gone entirely."
        )
    m_topics = _TOPICS_CHANGE_RE.search(t)
    if m_topics:
        if not mission:
            if paused_mission:
                return "Email monitoring is paused. Say 'resume email' first, then I can change the topics."
            return "There's no active email monitoring mission in this chat to change topics for."
        raw = next((g for g in m_topics.groups() if g), "") or ""
        new_topics = extract_custom_topics("about " + raw.strip())
        # "stop monitoring X" removes X instead of replacing the list.
        if re.search(r"\bstop\s+monitoring\b", t, re.IGNORECASE):
            pol = dict(mission.get("notification_policy") or {})
            kws = [k for k in (pol.get("custom_keywords") or []) if k.lower() not in {x.lower() for x in new_topics}]
            pol["custom_keywords"] = kws
            db.update_email_mission(
                mission["id"], {"notification_policy_json": json.dumps(pol)}, db_path=db_path
            )
            return f"Stopped monitoring {'/'.join(new_topics) or 'that topic'}. Remaining topics: {', '.join(kws) or 'none (general important-mail monitoring)'}."
        pol = dict(mission.get("notification_policy") or {})
        pol["custom_keywords"] = new_topics
        db.update_email_mission(
            mission["id"], {"notification_policy_json": json.dumps(pol)}, db_path=db_path
        )
        if new_topics:
            return f"Topics updated — I'll now prioritize: {', '.join(new_topics)}."
        return "Topics cleared — I'll monitor important mail generally, with no custom topics."
    return "I couldn't tell which email control you meant. Try 'pause email', 'resume email', 'switch to dry-run', 'sync now', or 'check every 5 minutes'."


async def handle_email_intelligence_instruction(
    instruction: str,
    chat_id: Optional[str] = None,
    db_path: Optional[str] = None,
    user_id: Optional[str] = None,
) -> str:
    """Handle either mission setup or companion query for email intelligence.

    ``user_id`` is the task owner's id: the account resolved here is
    that user's own, and any mission created is stamped with it.
    """
    db.init_db(db_path)

    # 1. If it's a specific companion question (e.g. "What did you delete today?")
    if is_email_companion_query(instruction) and not is_email_mission_setup_instruction(instruction):
        ans = answer_email_companion_query(instruction, db_path=db_path, user_id=user_id)
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
        google_status = oauth_manager.verify_connection("google", user_id=user_id)
        ms_status = oauth_manager.verify_connection("microsoft", user_id=user_id)

        # The provider named in the instruction wins: "monitor my
        # Outlook inbox" must use Outlook even when Gmail is also
        # connected. Before this fix the code checked Google first and
        # silently set up Gmail when the user said Outlook. If the
        # named provider is not connected, say so honestly instead of
        # silently falling back to the other account.
        _named = named_email_providers(instruction)
        wants_outlook = "outlook" in _named
        wants_gmail = "gmail" in _named
        if wants_outlook and not wants_gmail:
            if ms_status.get("connected") and ms_status.get("account"):
                account_email = ms_status["account"]
                provider = "outlook"
                mode_label = f"[Live Connected Outlook: {account_email}]"
            else:
                error_reason = "outlook_not_connected"
        elif wants_gmail and not wants_outlook:
            if google_status.get("connected") and google_status.get("account"):
                account_email = google_status["account"]
                provider = "gmail"
                mode_label = f"[Live Connected Gmail: {account_email}]"
            else:
                error_reason = "gmail_not_connected"
        elif google_status.get("connected") and google_status.get("account"):
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
        if error_reason == "outlook_not_connected":
            expired_note = "You asked for Outlook, but no Outlook/Microsoft account is connected."
        elif error_reason == "gmail_not_connected":
            expired_note = "You asked for Gmail, but no Gmail/Google account is connected."
        else:
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
    # The instruction's own schedule and authorization, when stated:
    # "every 60 minutes" sets the tick frequency; an explicit "delete
    # them" authorizes autonomous cleanup in one step (otherwise the
    # mission starts in dry-run review); "urgent" becomes a
    # notification keyword so urgent mail alerts immediately instead
    # of waiting for the neglect threshold. A re-bind that states NO
    # frequency must PRESERVE the existing interval — before this fix
    # it silently reset a 3600s mission to 300s as a side effect.
    stated_freq = parse_frequency_seconds(instruction)
    delete_authorized = has_explicit_delete_directive(instruction)
    if re.search(r"\burgent\b", instruction, re.IGNORECASE) and "urgent" not in [k.lower() for k in custom_topics]:
        custom_topics = [*custom_topics, "urgent"]
    base_notif = {
        "notify_deadlines": True,
        "notify_interviews": True,
        "notify_unanswered_important": True,
        "neglect_threshold_seconds": 3600,
        "custom_keywords": custom_topics,
    }

    # Check for existing mission to re-bind or update — scoped to the
    # caller. An unscoped dedupe would re-bind another user's mission
    # row (same mailbox, different PrivAgent user) into this chat.
    existing_missions = db.list_email_missions(db_path=db_path, user_id=user_id)
    existing_mission = next((m for m in existing_missions if m.get("account_email") == account_email), None)

    if existing_mission:
        mission_id = existing_mission["id"]
        prev_pol = existing_mission.get("notification_policy") or {}
        # A fresh setup instruction defines the topics. Do NOT silently
        # carry forward topics from a previous mission: the user asked
        # for "important mails" generally and got "internships, jobs"
        # from an old mission, which is confusing and wrong. Topics are
        # replaced by what this instruction says (possibly none).
        # Parameter-only updates (e.g. an interval change) go through
        # a separate handler that leaves the policy untouched, so
        # topics are preserved there. A deletion receipt the user
        # asked for earlier is preserved across re-binds.
        base_notif["custom_keywords"] = custom_topics
        if prev_pol.get("delete_receipt_text"):
            base_notif["delete_receipt_text"] = prev_pol["delete_receipt_text"]

        _update_fields: dict[str, Any] = {
            "status": "active",
            "conversation_id": chat_id or existing_mission.get("conversation_id"),
            # Preserve the existing interval when this instruction
            # states none; only a stated frequency changes it.
            "frequency_seconds": stated_freq if stated_freq is not None else existing_mission.get("frequency_seconds", 300),
            "notification_policy_json": json.dumps(base_notif),
            "next_run_at": time.time(),
        }
        if delete_authorized:
            _update_fields["policy_authorized"] = 1
            _update_fields["dry_run"] = 0
        db.update_email_mission(
            mission_id,
            _update_fields,
            db_path=db_path,
        )
        mission = db.get_email_mission(mission_id, db_path=db_path)
    else:
        mission = db.create_email_mission(
            account_email=account_email,
            provider=provider,
            frequency_seconds=stated_freq or 300,
            conversation_id=chat_id,
            policy_authorized=delete_authorized,
            dry_run=not delete_authorized,
            user_id=user_id,
            connection_id=oauth_manager.find_connection_id(user_id, provider, account_email) if user_id else None,
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

    freq_text = format_frequency_seconds(instruction, mission.get("frequency_seconds", stated_freq or 300))
    if delete_authorized:
        mode_text = "**AUTONOMOUS CLEANUP ACTIVE**"
        auth_note = "You authorized deletion in this instruction — eligible messages are moved to Trash (restorable at any time)."
    else:
        mode_text = "**DRY-RUN REVIEW**"
        auth_note = "No messages will be moved to Trash until you **explicitly authorize** autonomous cleanup."

    summary_markdown = f"""### 📬 Autonomous Email Intelligence Mission Configured

I have configured continuous email monitoring for your account. Here is the operational summary:

• **Connected Account:** `{account_email}` ({provider.upper()}) {mode_label}  
• **Monitoring Frequency:** {freq_text}  {channel_text}{custom_topics_text}
• **Email Categories Monitored:**
  - **Unnecessary:** Promotional campaigns, newsletters, repeated automated notifications, bulk marketing.
  - **Important:** University/academic communication, recruitment/job opportunities, interview invitations, assessment deadlines, banking & financial statements, security alerts, personal communication.
  - **Uncertain:** Unknown senders or ambiguous context *(strictly preserved for human review)*.

---

### 🛡️ Autonomous Deletion Policy & Explicit Authorization Gate

> [!IMPORTANT]
> **Safety Guarantee: Zero Permanent Deletions.**
> Deletions move messages to **Trash** only. You can restore any message at any time.

• **Current Mode:** {mode_text}  
• **Deletion Eligibility Rules:**
  - Eligible messages must be classified as *unnecessary* with confidence >= 80%.
  - Protected categories (**Banking, Financial, Security, Academic, Recruitment, Personal, and Deadlines**) are **strictly locked and never automatically deleted**.
  - {auth_note}

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

