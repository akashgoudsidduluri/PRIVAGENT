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


# Track-new-items alternatives, shared by the directive pattern and
# the handler's own ask detector so the two cannot drift apart.
# ("track SDE internships and alert me when a new one appears",
# "watch this page and tell me when a new listing shows up".)
_TRACK_NEW_ALT = (
    r"\b(?:monitor|track|watch)\b[^.\n]{0,80}\bnew\b[^.\n]{0,40}\b(?:listing|listings|job|jobs|internship|internships|role|roles|position|positions|product|products|item|items|post|posts|opportunit(?:y|ies)|opening|openings)\b|"
    r"\b(?:alert|notify|tell|let)\b[^.\n]{0,60}\bnew\b[^.\n]{0,40}\b(?:listing|listings|job|jobs|internship|internships|role|roles|position|positions|product|products|item|items|post|posts|opportunit(?:y|ies)|opening|openings)\b|"
    r"\bwhen\s+a\s+new\s+one\b[^.\n]{0,40}\bappears?\b|"
    # Product availability ("alert me when a new iPhone 16 drops on
    # Amazon"): "new" + a thing + "drops". The plain "price drops"
    # has no "new", so it cannot match this branch.
    r"\b(?:alert|notify|tell|let)\b[^.\n]{0,60}\bnew\b[^.\n]{0,40}\bdrops?\b"
)
_TRACK_NEW_RE = re.compile(_TRACK_NEW_ALT, re.IGNORECASE)


MONITORING_DIRECTIVE_PATTERN = re.compile(
    r"\b(?:continuously\s+monitor|monitor\s+this|monitor\s+(?:the\s+)?(?:product|price|page|site|webpage|item|deal)|track\s+(?:the\s+)?price|alert\s+me\s+if\s+(?:the\s+)?price\s+drops|price\s+drop\s+alert|keep\s+an\s+eye\s+on\s+(?:the\s+)?price)\b|"
    r"\b(?:monitor|track)\s+.*?\b(?:price|drops?|stock|availability|changes?)\b|"
    # Daily price report ("yes every day at 9am just notify me the
    # prices"): a notify + price(s) ask, or a price(s) ask with a daily
    # cadence. "prizes" is the common misspelling of "prices".
    r"\bnotify\s+me\b[^.\n]{0,60}\b(?:price|prize)s?\b|"
    r"\b(?:price|prize)s?\b[^.\n]{0,60}\b(?:every\s+day|everyday|daily|every\s+morning|each\s+morning)\b|"
    r"\b(?:every\s+day|everyday|daily|every\s+morning|each\s+morning)\b[^.\n]{0,60}\b(?:price|prize)s?\b|"
    # A notify + daily cadence on its own ("notify me everyday") is a
    # monitoring cadence even when the word "price" never appears —
    # the product/threshold comes from the rest of the instruction,
    # and the handler asks which product when there is none, instead
    # of inventing one.
    r"\bnotify\s+me\b[^.\n]{0,40}\b(?:every\s+day|everyday|daily|every\s+morning|each\s+morning)\b|"
    # Absolute threshold ("alert me when the price falls below ₹5000",
    # "notify me if it drops under 3000"): a directive verb + below /
    # under + a number. The verb requirement keeps a plain statement
    # ("the price is below 5000") from becoming a watch.
    r"\b(?:alert|notify|tell|let)\b[^.\n]{0,60}\b(?:below|under)\s*(?:₹|Rs\.?|INR|\$)?\s*[\d,]+(?:\.\d{1,2})?\b|"
    r"\b(?:monitor|track|watch)\b[^.\n]{0,60}\b(?:below|under)\s*(?:₹|Rs\.?|INR|\$)?\s*[\d,]+(?:\.\d{1,2})?\b|"
    # The threshold forms users actually type, mirroring
    # parse_below_price: "reaches <5000", "drops to ₹5000", "falls
    # below 5000", "<₹5000". "check" is a verb here too ("check the
    # strap and tell me if it reaches <5000") — without it the ask
    # falls through to a browser task that only looks once.
    r"\b(?:alert|notify|tell|let|check)\b[^.\n]{0,60}?(?:reaches?|drops?|falls?|goes?|gets?|dips?)\s+(?:below|under|to)\s*(?:₹|Rs\.?|INR|\$)?\s*[\d,]+(?:\.\d{1,2})?\b|"
    r"\b(?:alert|notify|tell|let|check|watch|monitor|track)\b[^.\n]{0,60}?<\s*(?:₹|Rs\.?|INR|\$)?\s*[\d,]+(?:\.\d{1,2})?\b|"
    # An interval cadence on its own ("alert me every 5 minutes",
    # "check its price and just alert me every 5 minutes"): the
    # directive verb + a per-N-minutes/hours cadence. Without this a
    # follow-up that only changes the cadence fell through to a
    # research task and died in the completion guard ("Research
    # incomplete") — the product/threshold come from the history.
    # The number may be a word ("every five minutes"), not only a
    # digit — the handler parses both (see _parse_interval_seconds).
    r"\b(?:alert|notify|tell|let|check|monitor|track|watch)\b[^.\n]{0,60}?\bevery\s+(?:(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty)\s*)?(?:minutes?|mins?|hours?)\b|"
    + _TRACK_NEW_ALT,
    re.IGNORECASE,
)


# India Standard Time (the user's timezone) is UTC+5:30 year-round —
# no daylight saving — so a fixed offset converts a local morning time
# to the UTC cron the scheduler stores ("five UTC fields").
_IST_OFFSET_MINUTES = 330
_DAILY_TIME_RE = re.compile(
    r"\b(?:every\s+day|everyday|daily|every\s+morning|each\s+morning)\b[^0-9\n]{0,40}?"
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
    r"[^0-9\n]{0,40}?\b(?:every\s+day|everyday|daily|every\s+morning|each\s+morning)\b",
    re.IGNORECASE,
)
# Bare daily cadence with no clock time ("notify me every day",
# "check it daily"): the time-bearing regexes above need an hour,
# so this one only proves the cadence exists; the default 9:00 AM
# IST is applied by parse_daily_time.
_BARE_DAILY_RE = re.compile(
    r"\b(?:every\s+day|everyday|daily|every\s+morning|each\s+morning)\b",
    re.IGNORECASE,
)


_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30,
}

_INTERVAL_RE = re.compile(
    r"\bevery\s+(?:(\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty)\s*)?(minutes?|mins?|hours?)\b",
    re.IGNORECASE,
)


def _parse_interval_seconds(instruction: str) -> int:
    """Parse \"every 5 minutes\" / \"every two hours\" into seconds.

    The handler used to hardcode 300s for every interval watch, so
    \"check every 2 hours\" silently became a 5-minute watch. No
    cadence in the instruction -> the 300s default. Clamped to a
    60-second floor: a sub-minute tick would burn the scheduler.
    """
    m = _INTERVAL_RE.search(instruction or "")
    if not m:
        return 300
    num_s, unit = m.group(1), m.group(2).lower()
    num = 1 if num_s is None else (int(num_s) if num_s.isdigit() else _WORD_NUMBERS.get(num_s.lower(), 5))
    seconds = num * 3600 if unit.startswith("hour") else num * 60
    return max(60, seconds)


def _describe_interval(interval_seconds: int) -> str:
    """Human cadence for an interval watch: \"every 5 minutes\",
    \"every 2 hours\". Used in replies so the message quotes the
    watch's real cadence, never a hardcoded one."""
    seconds = interval_seconds or 300
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"every {hours} hour" + ("s" if hours != 1 else "")
    minutes = max(1, seconds // 60)
    return f"every {minutes} minute" + ("s" if minutes != 1 else "")


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
        # A bare daily cadence ("notify me every day", "check it daily")
        # names no clock time. Default to the 9:00 AM IST morning
        # report — the same default the demo's daily digest uses —
        # instead of dropping the cadence and falling back to a
        # 5-minute interval watch the user never asked for.
        if _BARE_DAILY_RE.search(instruction or ""):
            return "30 3 * * *", "9:00 AM IST"
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
    r"(?:\b(?:below|under|less\s+than|at\s+most)\s*(?:₹|Rs\.?|INR|\$)?\s*([\d,]+(?:\.\d{1,2})?)"
    r"|\b(?:reaches?|drops?|falls?|goes?|gets?|dips?)\s+(?:below|under|to|under)\s*(?:₹|Rs\.?|INR|\$)?\s*([\d,]+(?:\.\d{1,2})?)"
    r"|<\s*(?:₹|Rs\.?|INR|\$)?\s*([\d,]+(?:\.\d{1,2})?))",
    re.IGNORECASE,
)


def parse_below_price(instruction: str) -> float | None:
    """Parse 'alert me if the price falls below ₹5000' into 5000.0.

    Also accepts the forms users actually type: 'reaches <5000',
    'drops to ₹5000', 'falls below 5000', '<₹5000'. Returns None
    when no below/under threshold is named. The caller (a
    monitoring directive) has already established the intent, so
    this only extracts the number."""
    m = _BELOW_PRICE_RE.search(instruction or "")
    if not m:
        return None
    raw = next((g for g in m.groups() if g is not None), None)
    if raw is None:
        return None
    try:
        value = float(raw.replace(",", ""))
    except ValueError:
        return None
    return value if value > 0 else None


# Brands a monitored product can belong to. The extractor matches
# "<brand> <model...>" in the instruction first, then in the recent
# history — so a fitness band, a phone or a laptop all work, and the
# product is never a hardcoded fallback model.
_MONITORED_BRANDS = (
    "samsung|amazfit|xiaomi|fitbit|garmin|apple|huawei|noise|boat|fire-boltt|"
    "realme|oneplus|oppo|vivo|google|sony|lenovo|honor|zebronics|fastrack|"
    "logitech|hp|dell|asus|acer|razer|bose|jbl|nothing|iqoo|poco|motorola"
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
    return _truncate_title(" ".join(tokens).strip(" -"))


# App / service names that look like brand+model to _PRODUCT_RE
# ("Google Calendar", "Google Drive") but are never a product to
# watch. The agent's own greeting lists connected apps, so the
# history scan used to pick "Google Calendar" out of it and watch a
# calendar listing for a bike stand the user actually asked about.
_SERVICE_NAMES = {
    "google calendar", "google drive", "google docs", "google sheets",
    "google slides", "google meet", "google maps", "google photos",
    "google keep", "gmail", "outlook", "microsoft teams", "slack",
    "github", "google chrome", "google pay",
}


def _is_service_name(title: str) -> bool:
    """True when a cleaned brand+model match is really an app/service.
    Exact match plus a service name followed by a generic suffix
    (\"Google Calendar app\") — the exact-only check missed those."""
    t = (title or "").strip().lower()
    candidates = {t, t[:-1]} if t.endswith("s") else {t}
    return any(
        c in _SERVICE_NAMES or any(c.startswith(name + " ") for name in _SERVICE_NAMES)
        for c in candidates
    )


def _truncate_title(text: str, limit: int = 80) -> str:
    """Shorten a product title to at most `limit` chars without cutting
    a word in half: cut at the last space inside the limit. A title cut
    mid-word ("... Stand (Floo") reads broken in the watch card and in
    every reply that names the product, and it leaks into the search
    URL built from the title too."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text.rfind(" ", 0, limit)
    if cut <= 0:
        return text[:limit]
    return text[:cut]


def _product_phrase_from_text(text: str) -> str:
    """Product phrase named after a monitor/track/watch/check verb.

    "monitor this, Grandbiker Rear Wheel Paddock Stand ..." names a
    product with no brand in _MONITORED_BRANDS, so brand+model matching
    misses it entirely. Take the phrase after the verb, cut the
    threshold / cadence / alert tail off, and require at least two
    real words. A pronoun-led phrase ("check its price ...") is NOT a
    product — it resolves from history instead.
    """
    if not text:
        return ""
    m = re.search(
        # The pronoun after the verb must be a whole word (\b):
        # without it "check its price" matched "it" inside "its" and
        # the phrase became the nonsense "s price and just alert me".
        r"\b(?:monitor|track|watch|check)\s+(?:(?:this|that|it|the\s+product|this\s+product)\b)?\s*,?\s*(.+)$",
        text.strip(),
        re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return ""
    phrase = m.group(1).strip()
    # A bare cadence is not a product ("check every 2 hours", "check
    # every day"): the phrase after the verb is only the interval, so
    # strip a LEADING cadence and let the rest resolve from history —
    # otherwise the follow-up becomes a phantom watch titled
    # "Every 2 Hours" / "Every Day" and the real watch never changes.
    phrase = re.sub(
        r"^every\s+(?:(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty)\s*)?(?:minutes?|mins?|hours?|days?|mornings?|evenings?|nights?)\b\s*,?\s*",
        "",
        phrase,
        flags=re.IGNORECASE,
    ).strip()
    phrase = re.split(
        # The alert tail may follow a comma ("..., alert me") as well
        # as "and" — without the comma split the title became
        # "Grandbiker Rear Wheel Paddock Stand, Alert Me". The number
        # after "every" may be a word ("every five minutes"). A daily
        # / morning cadence cuts the title too ("watch the stand every
        # day" must not become a product called "Stand Every Day").
        r",\s*(?:alert|notify|tell)\b"
        r"|\s+if\s+the\s+price\b|\s+if\s+it\b|\s+when\b|\s+and\s+(?:alert|notify|tell)\b"
        r"|\s+every\s+(?:(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty)\s*)?(?:minutes?|mins?|hours?|days?|mornings?|evenings?|nights?)\b",
        phrase,
        flags=re.IGNORECASE,
    )[0]
    phrase = phrase.strip().strip("\"'").rstrip(".,;:!?").strip()
    phrase = re.sub(r"^(?:the|a|an)\s+", "", phrase, flags=re.IGNORECASE).strip()
    phrase = re.sub(r"^price\s+of\s+", "", phrase, flags=re.IGNORECASE).strip()
    # "the price of the X" leaves a second "the" behind after the
    # "price of" strip — strip the article once more.
    phrase = re.sub(r"^(?:the|a|an)\s+", "", phrase, flags=re.IGNORECASE).strip()
    words = phrase.split()
    if len(words) < 2:
        return ""
    # A product phrase never starts with a preposition ("keep a watch
    # on product prices" is the greeting's capability list, not a
    # product — without this the greeting alone counts as a product).
    # "out" covers the phrasal verbs ("check out the new deals",
    # "watch out for ..."), which name no product.
    if words[0].lower() in {"on", "for", "over", "about", "of", "at", "to", "in", "with", "from", "by", "out"}:
        return ""
    # Possessive pronouns name someone's account, not a product
    # ("watch my email", "check your inbox" must not become watches
    # for "my email" / "your inbox").
    if words[0].lower() in {"its", "it", "this", "that", "them", "their", "they", "he", "she", "my", "your", "his", "her", "our"}:
        return ""
    if _is_service_name(phrase):
        return ""
    return _truncate_title(phrase)


_FIND_VERB_RE = re.compile(r"\b(?:find|search|look\s+for|list|show\s+me|get\s+me)\b", re.IGNORECASE)
_TRACK_VERB_RE = re.compile(r"\b(?:track|watch|monitor|alert|notify)\b", re.IGNORECASE)


def _is_compound_find_and_track(text: str) -> bool:
    """True when the instruction asks to find/search something NOW
    and also to track it — a compound ask, not a pure tracking ask.

    The find/search verb must come before the tracking verb, and must
    not sit inside a "when you find ..." / "if you find ..." clause
    (that phrasing is a future-discovery tracking ask, not a
    search-now ask). A compound ask goes to the browser task, which
    searches now and offers the watch; routing it to the handler
    would create a watch without ever showing results."""
    if not text:
        return False
    fm = _FIND_VERB_RE.search(text)
    tm = _TRACK_VERB_RE.search(text)
    if not fm or not tm:
        return False
    if fm.start() >= tm.start():
        return False
    window = text[max(0, fm.start() - 15):fm.start()].lower()
    if re.search(r"\b(?:when|if|once|whenever)\b", window):
        return False
    return True


def is_continuous_monitoring_directive(text: str, history: Optional[list[dict[str, Any]]] = None) -> bool:
    """Check if the user instruction requests continuous background monitoring."""
    if not text:
        return False
    # A compound find-now + track ask is not a pure tracking ask.
    if _is_compound_find_and_track(text):
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
        prod_m = _PRODUCT_RE.search(content)
        if prod_m and not _is_service_name(_clean_product_match(prod_m.group(0))):
            return True
        if _product_phrase_from_text(content):
            return True
        if re.search(r"https?://[^\s\"'<>)]*(?:amazon\.|flipkart\.|croma\.)", content, re.IGNORECASE):
            return True
        # A listing/search page (internships, jobs) is watchable too:
        # a bare "yes" to the alert offer after a listing search must
        # create a new_listings watch, not stay conversational. The
        # store-URL check above only knows retail hosts.
        for m in _URL_RE.finditer(content):
            if any(d in m.group(0).lower() for d in _LISTING_DOMAINS):
                return True
    return False


# Bare pronouns that cannot title a watch ("Track them and alert me"
# -> "them"). When the extracted title is one of these, derive it
# from the watch URL instead.
_PRONOUN_TITLES = {"them", "it", "these", "those", "this", "that", "one", "ones", "me"}


def _title_from_listing_url(url: str) -> str:
    """Human title for a listing URL: the last meaningful path segment,
    de-slugified (e.g. software-development-internship -> 'software
    development internship'), else the hostname. ID-like segments
    (a Flipkart '/p/itm123' product ID) are skipped — they are not a
    title."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    segments = [s for s in (parts.path or "").split("/") if s and s not in ("internships", "jobs", "search")]
    for slug in reversed(segments):
        slug = re.sub(r"[?#].*$", "", slug)
        if re.fullmatch(r"(?:p|itm|pid|dp|product)?\d+", slug):
            continue
        if len(slug) < 3:
            continue
        title = re.sub(r"[-_]+", " ", slug).strip()
        if title:
            return title[:80]
    host = (parts.hostname or "").removeprefix("www.")
    return host[:80]


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
        cleaned_match = _clean_product_match(prod_m.group(0))
        if not _is_service_name(cleaned_match):
            candidate_title = cleaned_match
    # The product named after the verb ("monitor this, Grandbiker Rear
    # Wheel Paddock Stand ...") — no brand in _MONITORED_BRANDS, so
    # brand+model matching misses it entirely.
    if not candidate_title:
        candidate_title = _product_phrase_from_text(instruction or "")
    # Weaker signal: "monitor price of X" / "track X on Amazon".
    if not candidate_title:
        m = re.search(r"\b(?:for|of|on)\s+([^,.\n]+?)(?:\s+(?:on|at|and|if|when)|$)", instruction, re.IGNORECASE)
        raw = m.group(1).strip() if m else ""
        if raw.lower() not in ("this", "this product", "the product", "this price", "it", ""):
            candidate_title = raw

    # A store URL pasted in the instruction itself wins — the history
    # loop below only sees earlier messages, so a URL given with the
    # ask was ignored and the watch fell back to a Flipkart search
    # page (whose first price is a banner, not the product's).
    best_url = ""
    url_m = re.search(r"https?://[^\s\"'<>)]+", instruction or "")
    if url_m and any(d in url_m.group(0).lower() for d in ("amazon.", "flipkart.", "croma.")):
        best_url = url_m.group(0)

    # Look back in history for product name and URL. The history scan
    # used to take the first brand+model match from ANY message —
    # including the agent's own greeting ("Connected apps — Gmail,
    # Google Calendar ..."), which produced a watch for "Google
    # Calendar" when the user asked about a bike stand. Now: user
    # messages may name the product (verb phrase or brand+model);
    # assistant messages only count when they carry a real suggestion
    # (a store URL or a price), never a greeting or capability list.
    # Service names ("Google Calendar") are rejected everywhere.
    if history:
        for entry in reversed(history):
            content = entry.get("content") or entry.get("message") or ""
            role = (entry.get("role") or "").lower()
            # Check for URL (only when the instruction gave none)
            url_match = re.search(r"https?://[^\s\"'<>)]+", content)
            if not best_url and url_match and ("amazon" in url_match.group(0) or "flipkart" in url_match.group(0) or "croma" in url_match.group(0)):
                best_url = url_match.group(0)
            if candidate_title:
                continue
            if role == "user":
                phrase = _product_phrase_from_text(content)
                if phrase:
                    candidate_title = phrase
                    continue
                prod_m = _PRODUCT_RE.search(content)
                if prod_m:
                    cleaned_match = _clean_product_match(prod_m.group(0))
                    if not _is_service_name(cleaned_match):
                        candidate_title = cleaned_match
            elif _STORE_LINK_RE.search(content) or _PRICE_RE.search(content):
                prod_m = _PRODUCT_RE.search(content)
                if prod_m:
                    cleaned_match = _clean_product_match(prod_m.group(0))
                    if not _is_service_name(cleaned_match):
                        candidate_title = cleaned_match

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

    return _truncate_title(candidate_title.title()), best_url


def urllib_quote(s: str) -> str:
    import urllib.parse
    return urllib.parse.quote_plus(s)


# Sites whose listing / search pages a new-listings watch can target.
# URLs are extracted whole first, then filtered by domain — anchoring
# the domain inside the URL regex truncates the match to the domain.
_URL_RE = re.compile(r"https?://[^\s\"'<>)]+", re.IGNORECASE)
_LISTING_DOMAINS = ("internshala.com", "linkedin.com", "unstop.com", "amazon.", "flipkart.", "croma.")
_TRACK_LISTING_TITLE_RE = re.compile(
    r"\b(?:track|watch|monitor)\b\s+(?:the\s+)?(.+?)(?:\s+and\s+(?:alert|notify|tell)|"
    r"\s+when\s+a\s+new|\s+on\s+(?:internshala|linkedin|unstop|amazon|flipkart|croma)\b|\s*$)",
    re.IGNORECASE,
)
_NEW_NOUN_RE = re.compile(
    r"\bnew\s+(?:([A-Za-z][\w\-]*(?:\s+[A-Za-z][\w\-]*){0,3}))\s+(?:appears?|shows?\s+up|comes?\s+up|posted)",
    re.IGNORECASE,
)


def _is_track_new_ask(instruction: str) -> bool:
    """True when the instruction asks to watch for NEW items
    (listings, jobs, internships) rather than a price."""
    return bool(_TRACK_NEW_RE.search(instruction or ""))


def _extract_tracked_listing(instruction: str, history: Optional[list[dict[str, Any]]] = None) -> tuple[str, str]:
    """Extract the tracked query + a listing/search URL for a
    track-new-items ask. Returns ("", "") when either is missing —
    the caller must ask the user instead of inventing a watch.
    The URL comes from the instruction, else from the most recent
    listing-site URL in history (the search page the agent already
    used); a search URL is never constructed from a site name alone,
    because each site's URL scheme differs and a guessed URL would
    watch the wrong page."""
    title = ""
    m = _TRACK_LISTING_TITLE_RE.search(instruction or "")
    if m:
        title = m.group(1).strip(" .").strip()
    if not title:
        nm = _NEW_NOUN_RE.search(instruction or "")
        if nm and nm.group(1).lower() not in ("one", "ones"):
            title = nm.group(1).strip()
    if not title and history:
        for entry in reversed(history):
            content = str(entry.get("content") or entry.get("message") or "")
            m2 = _TRACK_LISTING_TITLE_RE.search(content)
            if m2:
                title = m2.group(1).strip(" .").strip()
                break
    title = re.sub(r"\s+", " ", title)[:80]
    # A URL pasted in the instruction must not leak into the title
    # ("Track new SDE internships on https://..." would otherwise
    # title the watch with the URL itself). Strip URLs, a leading
    # "new" (the mission title already says "New listings:"), and a
    # dangling preposition left behind by the URL removal.
    title = _URL_RE.sub("", title).strip()
    title = re.sub(r"^new\s+", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s+(?:on|at|for|in|and|&)\s*$", "", title).strip()
    title = re.sub(r"\s+", " ", title)[:80]

    url = ""
    for source in (instruction or "",):
        for m3 in _URL_RE.finditer(source):
            if any(d in m3.group(0).lower() for d in _LISTING_DOMAINS):
                url = m3.group(0)
                break
        if url:
            break
    if not url and history:
        for entry in reversed(history):
            content = str(entry.get("content") or entry.get("message") or "")
            for m4 in _URL_RE.finditer(content):
                if any(d in m4.group(0).lower() for d in _LISTING_DOMAINS):
                    url = m4.group(0)
                    break
            if url:
                break
    # A bare pronoun title ("Track them and alert me" -> "them") cannot
    # name a watch. Derive it from the URL the watch will check.
    if url and (not title or title.lower() in _PRONOUN_TITLES):
        title = _title_from_listing_url(url)
    return title, url


def _create_new_listings_watch(
    listing_title: str,
    listing_url: str,
    chat_id: Optional[str],
    db_path: Optional[str],
    user_id: Optional[str] = None,
) -> str:
    """Insert a new_listings mission (hourly) and return its confirmation.
    The watch's checks run through the same egress gate as browsing: if
    the site is not on the allow-list, the checks would fail forever,
    so refuse honestly instead of creating a watch that can never fire."""
    from .allowlist import url_allowed
    from . import config as _config

    if not url_allowed(listing_url, _config.ALLOWED_DOMAINS):
        host = urlsplit(listing_url).hostname or listing_url
        return (
            f"I can't watch {host} yet — it is not on the allowed-domains list, "
            f"so the watch's checks would be blocked. Add {host} to "
            f"PRIVAGENT_ALLOWED_DOMAINS in backend/.env and restart the backend, "
            f"then ask me again and I'll set up the watch."
        )
    mission_id = str(uuid.uuid4())
    now = time.time()
    with db.get_connection(db_path) as conn:
        conn.execute(
            """INSERT INTO monitoring_missions
            (id, conversation_id, user_id, title, target_url, trigger_type, schedule_cron, interval_seconds,
             condition_type, condition_params, status, created_at, next_run_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
            (
                mission_id,
                chat_id or "default",
                user_id,
                f"New listings: {listing_title}",
                listing_url,
                "interval",
                None,
                3600,
                "new_listings",
                json.dumps({"target": listing_title}),
                now,
                now,
            ),
        )
        conn.commit()
    try:
        hub.spawn(hub.broadcast_control_state())
    except Exception:
        pass
    return (
        f"### 👀 New-listings watch active\n\n"
        f"I'll check **{listing_title}** every hour and alert you right here in this conversation when a new one appears.\n\n"
        f"• **Status:** Active — alerts land in this conversation\n"
        f"• **Watching:** [{listing_title} ↗]({listing_url})\n"
        f"• **Check frequency:** Every hour\n"
        f"• **Alert:** The moment a new listing shows up\n\n"
        f"<!-- privagent-watch: {mission_id} -->"
    )


def _threshold_from_history(history: Optional[list[dict[str, Any]]]) -> float | None:
    """Most recent below-price threshold in the last few history turns.

    A follow-up that only changes the cadence ("check its price and
    just alert me every 5 minutes") names no threshold — the one from
    the ask it follows ("if the price is below 1000") still applies.
    Only the last few turns are scanned, so an old threshold for a
    different product in the same chat cannot leak in.
    """
    for entry in reversed((history or [])[-4:]):
        if not isinstance(entry, dict):
            continue
        content = str(entry.get("content") or entry.get("message") or "")
        value = parse_below_price(content)
        if value is not None:
            return value
    return None


async def handle_continuous_monitoring_instruction(
    instruction: str,
    chat_id: Optional[str] = None,
    history: Optional[list[dict[str, Any]]] = None,
    db_path: Optional[str] = None,
    user_id: Optional[str] = None,
) -> str:
    """Register a persistent continuous monitoring mission and return natural departure handoff."""
    db.init_db(db_path)
    # Price signals are read first: a price ask must never become a
    # listing watch. Dispatch order is daily -> below -> new_listings
    # -> price_drop, so the handler body matches the documented order.
    daily = parse_daily_time(instruction)
    below = parse_below_price(instruction)
    # A follow-up that only changes the cadence ("check its price and
    # just alert me every 5 minutes") names no threshold — the one from
    # the ask it follows ("if the price is below 1000") still applies.
    # Only when the instruction names NO product of its own (a pronoun
    # follow-up): an explicit new product must never inherit an old
    # threshold for a different product.
    if (
        below is None
        and not _PRODUCT_RE.search(instruction or "")
        and not _product_phrase_from_text(instruction or "")
    ):
        below = _threshold_from_history(history)
    # Track-new-items ("track SDE internships and alert me when a
    # new one appears"): a new_listings mission, not a price watch.
    # Hourly interval — listing pages change slowly; a 5-minute tick
    # would burn the scheduler and the sites for no gain.
    if _is_track_new_ask(instruction) and daily is None and below is None:
        listing_title, listing_url = _extract_tracked_listing(instruction, history=history)
        if not listing_title or not listing_url:
            return (
                "I can watch for new listings, but I need the search page to watch. "
                "Paste the search results URL (for example, the Internshala search page), "
                "or ask me to search for the listings first and I'll offer the watch after."
            )
        return _create_new_listings_watch(listing_title, listing_url, chat_id, db_path, user_id=user_id)
    product_title, target_url = _extract_monitored_product(instruction, history=history)
    if not product_title:
        # Bare "yes" answering the alert offer after a listing search
        # (e.g. internships): the history carries the search page, so
        # the watch is a new_listings one, not a price watch. This
        # branch runs only when there is NO product in the history —
        # a retail suggestion (Fit3 on Flipkart) has a brand+model, so
        # it takes the price-watch path below. The detector only routes
        # here when the offer marker is in the last assistant message
        # and the history has a tracked listing.
        if _is_bare_affirmative(instruction) and daily is None and below is None:
            listing_title, listing_url = _extract_tracked_listing(instruction, history=history)
            if listing_title and listing_url:
                return _create_new_listings_watch(listing_title, listing_url, chat_id, db_path, user_id=user_id)
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
    # A daily cadence and a threshold COMPOSE: "below ₹5000, notify
    # me every day" is a below_price mission on a daily cron — the
    # check runs once a morning and alerts only when the price is
    # under the threshold. Before this, daily swallowed the threshold
    # (a daily_price report with no alert) and a threshold alone fell
    # back to a 5-minute interval the user never asked for. The
    # scheduler's claim_due handles a cron trigger for any condition,
    # so the composed mission is checked the same way.
    if daily and below is not None:
        cron, display = daily
        trigger_type, schedule_cron, interval_sec = "cron", cron, 86400
        condition_type, condition_params = "below_price", {"target": product_title, "threshold": below}
        cadence_line = f"• **Check Schedule:** Every day at **{display}**\n"
        alert_line = (
            f"• **Alert Condition:** Alert when the price falls below ₹{below:g} "
            f"(if it is already below, I'll alert you on the first check)\n"
        )
    elif daily:
        # A daily time ("every day at 9am", "every morning at 9")
        # becomes a cron mission that REPORTS the price each morning,
        # instead of a 5-minute interval mission that only fires on a
        # drop. The cron is UTC (the scheduler stores five UTC
        # fields); parse_daily_time converts the user's IST morning
        # into it. (daily / below were already read at the top of
        # this handler for dispatch order.)
        cron, display = daily
        trigger_type, schedule_cron, interval_sec = "cron", cron, 86400
        condition_type, condition_params = "daily_price", {"target": product_title}
        cadence_line = f"• **Check Schedule:** Every day at **{display}** — I'll send you the current price each morning\n"
        alert_line = "• **Alert Condition:** A price report every morning at the scheduled time, even when the price has not changed\n"
    elif below is not None:
        # Absolute threshold ("alert me when the price falls below
        # ₹5000"): a below_price mission, not a %-drop one. The
        # detector fires on the first run too when the price is
        # already under the threshold. The cadence is the one the
        # user asked for ("every 2 hours" is 2 hours, not 5
        # minutes); no cadence in the instruction -> 300s default.
        interval_sec = _parse_interval_seconds(instruction)
        trigger_type, schedule_cron = "interval", None
        condition_type, condition_params = "below_price", {"target": product_title, "threshold": below}
        cadence_line = f"• **Check Frequency:** {_describe_interval(interval_sec).capitalize()} (interval scheduler tick)\n"
        alert_line = (
            f"• **Alert Condition:** Alert the moment the price falls below ₹{below:g} "
            f"(if it is already below, I'll alert you on the first check)\n"
        )
    else:
        interval_sec = _parse_interval_seconds(instruction)
        trigger_type, schedule_cron = "interval", None
        condition_type, condition_params = "price_drop", {"target": product_title, "min_percent": 0}
        cadence_line = f"• **Check Frequency:** {_describe_interval(interval_sec).capitalize()} (interval scheduler tick)\n"
        alert_line = "• **Alert Condition:** Instant notification whenever the price drops or special discount offers are detected\n"

    # A follow-up that only changes the cadence ("check its price and
    # just alert me every 5 minutes") must not create a SECOND watch
    # for the same product — the first watch already checks every 5
    # minutes, so a duplicate would double-alert on the same drop.
    # But the guard must compare the REQUESTED settings with the
    # STORED row: a daily cron watch exists and the user asks for
    # every 5 minutes is a settings CHANGE, not a duplicate — the old
    # code answered "is being checked every 5 minutes" and created
    # nothing, so the reply was false and the request was silently
    # dropped. Same settings -> say so; different settings -> update
    # the existing mission in place and say what changed.
    with db.get_connection(db_path) as conn:
        existing = conn.execute(
            """SELECT id, trigger_type, condition_type, condition_params, interval_seconds, schedule_cron
               FROM monitoring_missions
               WHERE conversation_id = ? AND title = ? AND status = 'active'
               ORDER BY created_at DESC LIMIT 1""",
            (chat_id or "default", f"Price Watch: {product_title}"),
        ).fetchone()
    if existing:
        try:
            stored_params = json.loads(existing["condition_params"] or "{}")
        except Exception:
            stored_params = {}
        stored_threshold = stored_params.get("threshold")
        same_settings = (
            existing["trigger_type"] == trigger_type
            and existing["interval_seconds"] == interval_sec
            and (existing["schedule_cron"] or None) == (schedule_cron or None)
            and existing["condition_type"] == condition_type
            and (stored_threshold if condition_type == "below_price" else None)
            == (below if condition_type == "below_price" else None)
        )
        if same_settings:
            cadence = _describe_interval(interval_sec) if trigger_type == "interval" else "daily"
            threshold_line = (
                f", alerting when the price falls below ₹{below:g}"
                if below is not None
                else ""
            )
            return (
                f"That watch is already running — **{product_title}** is being checked "
                f"{cadence}{threshold_line}."
            ) + f"\n\n<!-- privagent-watch: {existing['id']} -->"
        old_cadence = (
            "daily"
            if existing["trigger_type"] == "cron"
            else _describe_interval(existing["interval_seconds"] or 300)
        )
        new_cadence = _describe_interval(interval_sec) if trigger_type == "interval" else "daily"
        old_threshold = (
            f", alerting below ₹{stored_threshold:g}"
            if stored_threshold is not None
            else ""
        )
        new_threshold = (
            f", alerting when the price falls below ₹{below:g}"
            if below is not None
            else ""
        )
        with db.get_connection(db_path) as conn:
            conn.execute(
                """UPDATE monitoring_missions
                   SET target_url = ?, trigger_type = ?, schedule_cron = ?, interval_seconds = ?,
                       condition_type = ?, condition_params = ?, next_run_at = ?
                   WHERE id = ?""",
                (
                    target_url,
                    trigger_type,
                    schedule_cron,
                    interval_sec,
                    condition_type,
                    json.dumps(condition_params),
                    now,
                    existing["id"],
                ),
            )
            conn.commit()
        try:
            hub.spawn(hub.broadcast_control_state())
        except Exception:
            pass
        return (
            f"Updated the watch for **{product_title}** — it was checking {old_cadence}{old_threshold}, "
            f"now checking {new_cadence}{new_threshold}."
        ) + f"\n\n<!-- privagent-watch: {existing['id']} -->"

    with db.get_connection(db_path) as conn:
        conn.execute(
            """INSERT INTO monitoring_missions
            (id, conversation_id, user_id, title, target_url, trigger_type, schedule_cron, interval_seconds,
             condition_type, condition_params, status, created_at, next_run_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
            (
                mission_id,
                chat_id or "default",
                user_id,
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

