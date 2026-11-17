"""Continuous Monitoring Directive Handler.

Allows users to trigger continuous background monitoring (e.g. price drops,
page updates, stock alerts) directly from conversation.
Registers persistent monitoring missions in SQLite with the interval scheduler
and broadcasts status updates to the Control Center on port 5173.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any, Optional

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
    r"\b(?:every\s+day|daily|every\s+morning|each\s+morning)\b[^.\n]{0,60}\b(?:price|prize)s?\b",
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


def parse_daily_time(instruction: str) -> tuple[str, str] | None:
    """Parse "every day at 9am" into a UTC cron + a display string.

    Returns ``(cron, "9:00 AM IST")`` or ``None`` when the instruction
    carries no daily time. A bare hour with no am/pm marker is read as
    a morning hour (the "every morning" reading the user means).
    """
    m = _DAILY_TIME_RE.search(instruction or "")
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    marker = (m.group(3) or "").lower()
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


def is_continuous_monitoring_directive(text: str) -> bool:
    """Check if the user instruction requests continuous background monitoring."""
    if not text:
        return False
    # If it is strictly an email scan mission, let email intelligence handle it
    if re.search(r"\b(?:e-?mails?|inbox|mailbox)\b", text, re.IGNORECASE) and not re.search(r"\b(?:price|product|store|amazon|flipkart|croma)\b", text, re.IGNORECASE):
        return False
    return bool(MONITORING_DIRECTIVE_PATTERN.search(text))


def _extract_monitored_product(instruction: str, history: Optional[list[dict[str, Any]]] = None) -> tuple[str, str]:
    """Extract product title and best available URL from instruction or recent history."""
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
    best_url = "https://www.flipkart.com"
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

    if not candidate_title:
        # No product was named anywhere: say so instead of inventing
        # one. The old hardcoded phone fallback ("Samsung Galaxy A27")
        # would have watched the wrong product entirely.
        candidate_title = "your last suggestion"
    if "http" not in best_url:
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

    mission_id = str(uuid.uuid4())
    now = time.time()
    # A daily time ("every day at 9am", "every morning at 9") becomes
    # a cron mission that REPORTS the price each morning, instead of a
    # 5-minute interval mission that only fires on a drop. The cron is
    # UTC (the scheduler stores five UTC fields); parse_daily_time
    # converts the user's IST morning into it.
    daily = parse_daily_time(instruction)
    if daily:
        cron, display = daily
        trigger_type, schedule_cron, interval_sec = "cron", cron, 86400
        condition_type, condition_params = "daily_price", {"target": product_title}
        cadence_line = f"• **Check Schedule:** Every day at **{display}** — I'll send you the current price each morning\n"
        alert_line = "• **Alert Condition:** A price report every morning at the scheduled time, even when the price has not changed\n"
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

    # Inform WebSocket clients on port 5173
    try:
        hub.spawn(hub.broadcast_control_state())
    except Exception:
        pass

    return (
        f"### 🎯 Continuous Price Monitoring Activated\n\n"
        f"I've configured continuous background price tracking for **{product_title}**.\n\n"
        f"• **Status:** Active on your Control Center dashboard ([http://localhost:5173](http://localhost:5173))\n"
        f"• **Monitored Target:** [{product_title} Listing ↗]({target_url})\n"
        f"{cadence_line}"
        f"{alert_line}"
        f"• **Persistence:** Stored in SQLite — checks continue automatically even if you navigate away\n\n"
        f"---\n\n"
        f"*I've got the watch running on port 5173. I'm heading into the background to keep watch buddy, let's see you later!* 👋"
    )

