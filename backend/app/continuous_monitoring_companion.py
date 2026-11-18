"""Continuous Monitoring Directive Handler.

Allows users to trigger continuous background monitoring (e.g. price drops,
page updates, stock alerts) directly from conversation.
Registers persistent monitoring missions in SQLite with the interval scheduler
and broadcasts status updates to connected workspace clients over WebSocket,
which surface in the conversation and in the Control Center monitoring view.
That is the only alert channel: there is no phone/SMS ping, and port 5173
is only the address of the workspace web UI, never the monitoring channel.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any, Optional
from urllib.parse import urlsplit

from . import db
from .hub import hub


MONITORING_DIRECTIVE_PATTERN = re.compile(
    r"\b(?:continuously\s+monitor|monitor\s+this|monitor\s+(?:the\s+)?(?:product|price|page|site|webpage|item|deal)|track\s+(?:the\s+)?price|alert\s+me\s+if\s+(?:the\s+)?price\s+drops|price\s+drop\s+alert|keep\s+an\s+eye\s+on\s+(?:the\s+)?price)\b|"
    r"\b(?:monitor|track)\s+.*?\b(?:price|drops?|stock|availability|changes?)\b|"
    # Daily price report ("yes every day at 9am just notify me the
    # prices"): a notify + price(s) ask, or a price(s) ask with a daily
    # cadence. "prizes" is the common misspelling of "prices".
    r"\bnotify\s+me\b[^.\n]{0,60}\b(?:price|prize)s?\b|"
    r"\b(?:price|prize)s?\b[^.\n]{0,60}\b(?:every\s+day|daily|every\s+morning|each\s+morning)\b|"
    r"\b(?:every\s+day|daily|every\s+morning|each\s+morning)\b[^.\n]{0,60}\b(?:price|prize)s?\b|"
    # Absolute threshold ("alert me when the price falls below ₹5000",
    # "notify me if it drops under 3000"): a directive verb + below /
    # under + a number. The verb requirement keeps a plain statement
    # ("the price is below 5000") from becoming a watch.
    r"\b(?:alert|notify|tell|let)\b[^.\n]{0,60}\b(?:below|under)\s*(?:₹|Rs\.?|INR|\$)?\s*[\d,]+(?:\.\d{1,2})?\b|"
    r"\b(?:monitor|track|watch)\b[^.\n]{0,60}\b(?:below|under)\s*(?:₹|Rs\.?|INR|\$)?\s*[\d,]+(?:\.\d{1,2})?\b",
    re.IGNORECASE,
)


# India Standard Time (the user's timezone) is UTC+5:30 year-round —
# no daylight saving — so a fixed offset converts a local morning time
# to the UTC cron the scheduler stores ("five UTC fields").
_IST_OFFSET_MINUTES = 330
_DAILY_TIME_RE = re.compile(
    r"\b(?:every\s+day|daily|every\s+morning|each\s+morning)\b[^0-9\n]{0,40}?"
    r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b",
    re.IGNORECASE,
)
# Hour-first order ("notify me the price at 9am every day"): the time
# comes before the daily phrase. Either "at <hour>" or an am/pm
# marker is required, so a bare number (a price, a count) is never
# read as a time.
_DAILY_TIME_RE_AFTER = re.compile(
    r"(?:\bat\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b"
    r"|\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b)"
    r"[^0-9\n]{0,40}?\b(?:every\s+day|daily|every\s+morning|each\s+morning)\b",
    re.IGNORECASE,
)


def parse_daily_time(instruction: str) -> tuple[str, str] | None:
    """Parse "every day at 9am" into a UTC cron + a display string.

    Returns ``(cron, "9:00 AM IST")`` or ``None`` when the instruction
    carries no daily time. A bare hour with no am/pm marker is read as
    a morning hour (the "every morning" reading the user means). The
    daily phrase may come before the hour ("every day at 9am") or
    after it ("at 9am every day").
    """
    m = _DAILY_TIME_RE.search(instruction or "") or _DAILY_TIME_RE_AFTER.search(instruction or "")
    if not m:
        return None
    if m.re is _DAILY_TIME_RE:
        hour_s, minute_s, marker = m.group(1), m.group(2), m.group(3)
    else:
        hour_s = m.group(1) or m.group(4)
        minute_s = m.group(2) or m.group(5)
        marker = m.group(3) or m.group(6)
    hour = int(hour_s)
    minute = int(minute_s or 0)
    marker = (marker or "").lower()
    if marker == "pm" and hour != 12:
        hour += 12
    elif marker == "am" and hour == 12:
        hour = 0
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None
    display_hour = hour % 12 or 12
    display = f"{display_hour}:{minute:02d} {'PM' if hour >= 12 else 'AM'} IST"
    utc_total = (hour * 60 + minute - _IST_OFFSET_MINUTES) % (24 * 60)
    return f"{utc_total % 60} {utc_total // 60} * * *", display


_BELOW_PRICE_RE = re.compile(
    r"\b(?:below|under|less\s+than|at\s+most)\s*(?:₹|Rs\.?|INR|\$)?\s*([\d,]+(?:\.\d{1,2})?)",
    re.IGNORECASE,
)


def parse_below_price(instruction: str) -> float | None:
    """Parse 'alert me if the price falls below ₹5000' into 5000.0.

    Returns None when no below/under threshold is named. The caller
    (a monitoring directive) has already established the intent, so
    this only extracts the number."""
    m = _BELOW_PRICE_RE.search(instruction or "")
    if not m:
        return None
    try:
        value = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return value if value > 0 else None


# Brands a monitored product can belong to. The extractor matches
# "<brand> <model...>" in the instruction first, then in the recent
# history — so a fitness band, a phone or a laptop all work, and the
# product is never a hardcoded fallback model.
_MONITORED_BRANDS = (
    "samsung|amazfit|xiaomi|fitbit|garmin|apple|huawei|noise|boat|fire-boltt|"
    "realme|oneplus|oppo|vivo|google|sony|lenovo|honor|zebronics|fastrack"
)
# Brand + up to three model tokens ("Galaxy Fit3", "Smart Band 10",
# "Band 7"). Trailing stop-words are stripped after the match so a
# sentence like "Samsung Galaxy Fit3 is the best" yields just the model.
_PRODUCT_RE = re.compile(
    rf"\b(?:{_MONITORED_BRANDS})((?:\s+[A-Za-z0-9][\w\-]*){{1,3}})",
    re.IGNORECASE,
)
_PRODUCT_STOPWORDS = {
    "is", "the", "a", "an", "best", "price", "on", "at", "and", "if", "when",
    "for", "with", "under", "in", "of", "to", "from", "by", "vs", "or", "it",
    "this", "that", "please", "now", "just", "me", "my", "i", "want", "like",
}


def _clean_product_match(text: str) -> str:
    """Trim a brand+model match to the model name (drop stop-words)."""
    tokens = [t for t in text.split() if t.lower() not in _PRODUCT_STOPWORDS]
    return " ".join(tokens).strip(" -")[:80]


def is_continuous_monitoring_directive(text: str, history: Optional[list[dict[str, Any]]] = None) -> bool:
    """Check if the user instruction requests continuous background monitoring."""
    if not text:
        return False
    # If it is strictly an email scan mission, let email intelligence handle it
    if re.search(r"\b(?:e-?mails?|inbox|mailbox)\b", text, re.IGNORECASE) and not re.search(r"\b(?:price|product|store|amazon|flipkart|croma)\b", text, re.IGNORECASE):
        return False
    if MONITORING_DIRECTIVE_PATTERN.search(text):
        return True
    # Fix 5: a bare "yes" answering the alert offer the agent just
    # made ("Want me to keep an eye on it?") is an acceptance, not a
    # new conversational turn — route it to the handler so a real
    # watch is created for the product in the history, instead of the
    # LLM merely promising one. Requires the offer in the last
    # assistant message and a real product (brand+model or store URL)
    # in the history; otherwise a bare "yes" stays conversational.
    if history and _is_bare_affirmative(text):
        last_assistant = next(
            (m for m in reversed(history) if isinstance(m, dict) and m.get("role") == "assistant"),
            None,
        )
        content = str((last_assistant or {}).get("content") or (last_assistant or {}).get("message") or "")
        if ALERT_OFFER_MARKER in content and _history_has_monitored_product(history):
            return True
    return False


# A whole instruction that is only a go-ahead ("yes", "sure") — used
# for accepting the alert offer. Deliberately narrower than a general
# continuation: "retry"/"continue" mean "redo the failed task", not
# "yes, watch it".
_BARE_AFFIRMATIVES = {
    "yes", "yeah", "yep", "yup", "ok", "okay", "sure", "please do",
    "go ahead", "do it", "yes please", "yeah please", "sure go ahead",
    "yes go ahead", "yeah go ahead",
}


def _is_bare_affirmative(text: str) -> bool:
    """True when the instruction is only an affirmative go-ahead."""
    t = (text or "").strip().lower()
    t = re.sub(r"[.!?,\s]+$", "", t).strip()
    return t in _BARE_AFFIRMATIVES


def _history_has_monitored_product(history: list[dict[str, Any]]) -> bool:
    """True when the recent history names a real product to watch."""
    for entry in reversed(history or []):
        if not isinstance(entry, dict):
            continue
        content = str(entry.get("content") or entry.get("message") or "")
        if _PRODUCT_RE.search(content):
            return True
        if re.search(r"https?://[^\s\"'<>)]*(?:amazon\.|flipkart\.|croma\.)", content, re.IGNORECASE):
            return True
    return False


# The alert offer appended after a product suggestion / comparison
# finishes, and the plain note for a conversational suggestion whose
# prices no browsing verified. ALERT_OFFER_MARKER is the exact
# sentence the offer-acceptance detector looks for — keep in sync.
ALERT_OFFER_MARKER = "Want me to keep an eye on it?"
ALERT_OFFER_TEXT = (
    "Want me to keep an eye on it? I can alert you right here in this conversation "
    "if the price drops — just say \"alert me if it drops below ₹X\" — or send you a "
    "daily price update every morning at 9:00 AM IST (\"every day at 9am tell me the price\")."
)
UNVERIFIED_SUGGESTION_NOTE = (
    "Note: I didn't check the stores for this — these prices come from my general "
    "knowledge, not verified live listings, so treat them as a starting point, not a quote."
)

_SUGGESTION_RE = re.compile(
    r"\b(?:suggest|recommend|best|cheapest|cheaper|compare|comparison|versus|vs\.?|"
    r"price\s+of|how\s+much|budget|cheapest)\b",
    re.IGNORECASE,
)
_PRICE_RE = re.compile(r"(?:₹|\$|Rs\.?\s?)\s?[\d,]+")
_STORE_LINK_RE = re.compile(
    r"https?://[^\s\"'<>)]*(?:amazon\.|flipkart\.|croma\.)", re.IGNORECASE
)


def needs_unverified_suggestion_note(instruction: str, answer: str) -> bool:
    """True when a conversational (no-browsing) answer states product
    prices the agent never verified: the user asked about a product
    (suggestion / comparison / brand+model) and the answer states a
    price or a store link. Live case: the fitness-band suggestion
    answered from LLM knowledge with ₹3,899 and a Flipkart link no
    browsing had ever checked."""
    if not instruction or not answer:
        return False
    if not (_SUGGESTION_RE.search(instruction) or _PRODUCT_RE.search(instruction)):
        return False
    if not (_PRICE_RE.search(answer) or _STORE_LINK_RE.search(answer)):
        return False
    return True


def _extract_monitored_product(instruction: str, history: Optional[list[dict[str, Any]]] = None) -> tuple[str, str]:
    """Extract product title and best available URL from instruction or recent history.

    Returns ("", "") when neither a product nor a store URL can be
    found anywhere — the caller must ask the user instead of
    inventing a watch. The old fallback watched the Flipkart homepage
    for "your last suggestion", and the detector reads the first ₹
    price on the page, so that watch tracked a random banner price.
    """
    candidate_title = ""
    # A brand + model named in the instruction wins ("I want like the
    # Samsung Galaxy Fit3", "monitor the Xiaomi Smart Band 10").
    prod_m = _PRODUCT_RE.search(instruction or "")
    if prod_m:
        candidate_title = _clean_product_match(prod_m.group(0))
    # Weaker signal: "monitor price of X" / "track X on Amazon".
    if not candidate_title:
        m = re.search(r"\b(?:for|of|on)\s+([^,.\n]+?)(?:\s+(?:on|at|and|if|when)|$)", instruction, re.IGNORECASE)
        raw = m.group(1).strip() if m else ""
        if raw.lower() not in ("this", "this product", "the product", "this price", "it", ""):
            candidate_title = raw

    # Look back in history for product name and URL
    best_url = ""
    if history:
        for entry in reversed(history):
            content = entry.get("content") or entry.get("message") or ""
            # Check for URL
            url_match = re.search(r"https?://[^\s\"'<>)]+", content)
            if url_match and ("amazon" in url_match.group(0) or "flipkart" in url_match.group(0) or "croma" in url_match.group(0)):
                best_url = url_match.group(0)
            # Check for a brand + model mention in the suggestion (e.g.
            # the ranked fitness-band list) when the instruction only
            # says "I want like this".
            if not candidate_title:
                prod_m = _PRODUCT_RE.search(content)
                if prod_m:
                    candidate_title = _clean_product_match(prod_m.group(0))

    if not candidate_title and not best_url:
        # No product was named anywhere: say so instead of inventing
        # one. The old hardcoded phone fallback ("Samsung Galaxy A27")
        # would have watched the wrong product entirely.
        return "", ""
    if not candidate_title:
        # A store URL but no name: the watch targets a real product
        # page; title it by its store rather than inventing a model.
        host = (urlsplit(best_url).hostname or "store").removeprefix("www.")
        candidate_title = f"Product on {host}"
    if not best_url:
        # A product but no store URL: watch its search page, never a
        # store homepage (whose first price is a banner, not the
        # product's).
        best_url = f"https://www.flipkart.com/search?q={urllib_quote(candidate_title)}"

    return candidate_title.title(), best_url


def urllib_quote(s: str) -> str:
    import urllib.parse
    return urllib.parse.quote_plus(s)


async def handle_continuous_monitoring_instruction(
    instruction: str,
    chat_id: Optional[str] = None,
    history: Optional[list[dict[str, Any]]] = None,
    db_path: Optional[str] = None,
) -> str:
    """Register a persistent continuous monitoring mission and return natural departure handoff."""
    db.init_db(db_path)
    product_title, target_url = _extract_monitored_product(instruction, history=history)
    if not product_title:
        # No product and no store URL anywhere: do NOT create a watch
        # for "your last suggestion" on a store homepage. Ask which
        # product to watch instead.
        return (
            "I don't have a product to watch yet — no product name or store link "
            "in this conversation. Tell me which product to watch (for example, "
            "\"watch the Samsung Galaxy Fit3\") or paste the store link, and I'll "
            "set up the watch."
        )

    mission_id = str(uuid.uuid4())
    now = time.time()
    # A daily time ("every day at 9am", "every morning at 9") becomes
    # a cron mission that REPORTS the price each morning, instead of a
    # 5-minute interval mission that only fires on a drop. The cron is
    # UTC (the scheduler stores five UTC fields); parse_daily_time
    # converts the user's IST morning into it.
    daily = parse_daily_time(instruction)
    below = parse_below_price(instruction)
    if daily:
        cron, display = daily
        trigger_type, schedule_cron, interval_sec = "cron", cron, 86400
        condition_type, condition_params = "daily_price", {"target": product_title}
        cadence_line = f"• **Check Schedule:** Every day at **{display}** — I'll send you the current price each morning\n"
        alert_line = "• **Alert Condition:** A price report every morning at the scheduled time, even when the price has not changed\n"
    elif below is not None:
        # Absolute threshold ("alert me when the price falls below
        # ₹5000"): a below_price mission, not a %-drop one. The
        # detector fires on the first run too when the price is
        # already under the threshold.
        trigger_type, schedule_cron, interval_sec = "interval", None, 300
        condition_type, condition_params = "below_price", {"target": product_title, "threshold": below}
        cadence_line = "• **Check Frequency:** Every 5 minutes (interval scheduler tick)\n"
        alert_line = (
            f"• **Alert Condition:** Alert the moment the price falls below ₹{below:g} "
            f"(if it is already below, I'll alert you on the first check)\n"
        )
    else:
        trigger_type, schedule_cron, interval_sec = "interval", None, 300
        condition_type, condition_params = "price_drop", {"target": product_title, "min_percent": 0}
        cadence_line = "• **Check Frequency:** Every 5 minutes (interval scheduler tick)\n"
        alert_line = "• **Alert Condition:** Instant notification whenever the price drops or special discount offers are detected\n"

    with db.get_connection(db_path) as conn:
        conn.execute(
            """INSERT INTO monitoring_missions
            (id, conversation_id, title, target_url, trigger_type, schedule_cron, interval_seconds,
             condition_type, condition_params, status, created_at, next_run_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
            (
                mission_id,
                chat_id or "default",
                f"Price Watch: {product_title}",
                target_url,
                trigger_type,
                schedule_cron,
                interval_sec,
                condition_type,
                json.dumps(condition_params),
                now,
                now,
            ),
        )
        conn.commit()

    # Inform connected workspace clients (alerts land in the
    # conversation the watch was created in).
    try:
        hub.spawn(hub.broadcast_control_state())
    except Exception:
        pass

    # The watch marker is machine-readable: the workspace strips it
    # before rendering and turns it into an interactive watch card
    # (Pause / Resume / Cancel / View changes) in the conversation.
    # Monitoring is chat-only since 2026-11-18 — there is no monitor
    # form or scheduled-missions page anymore, so the confirmation
    # must not point at the Control Center dashboard.
    return (
        f"### 🎯 Continuous Price Monitoring Activated\n\n"
        f"I've configured continuous background price tracking for **{product_title}**.\n\n"
        f"• **Status:** Active — I'll post an alert right here in this conversation the moment anything changes\n"
        f"• **Monitored Target:** [{product_title} Listing ↗]({target_url})\n"
        f"{cadence_line}"
        f"{alert_line}"
        f"• **Persistence:** Stored in SQLite — checks continue automatically even if you navigate away\n\n"
        f"---\n\n"
        f"*I've got the watch running in the background — I'll post an alert right here in this conversation the moment anything changes. Catch you later, buddy!* 👋\n"
        f"<!-- privagent-watch: {mission_id} -->"
    )

