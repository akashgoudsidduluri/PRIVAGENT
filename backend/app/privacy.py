"""Privacy layer — redacts sensitive values BEFORE any model payload exists.

Fail-closed: internal redactors raise through; classify_and_redact wraps only
the assembly step so a crash anywhere yields PrivacyViolation, never a
partially-redacted payload.

Data minimization covers more than form values: URLs and page titles routinely
carry session tokens, emails and API keys in query strings. Those are scrubbed
here too, and screenshots are withheld unless explicitly opted in.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from threading import Lock
from collections import deque
import time
from typing import Any, Optional
from urllib.parse import unquote_plus, urlsplit, urlunsplit

from pydantic import BaseModel

from . import config
from .contracts import BrowserObservation, InteractiveElement, PrivacyDecision, PrivacyViolation

import re

MAX_NAME_LEN = 120
MAX_TITLE_LEN = 200
MAX_URL_LEN = 2048
REDACTED = "[REDACTED]"
_privacy_lock = Lock()
_privacy_counts = {"observations": 0, "redactedObservations": 0, "screenshotsWithheld": 0}
_privacy_events: deque[dict[str, Any]] = deque(maxlen=100)


def privacy_counters() -> dict[str, int]:
    with _privacy_lock:
        return dict(_privacy_counts)


def privacy_events() -> list[dict[str, Any]]:
    with _privacy_lock:
        return list(reversed(_privacy_events))


class Sensitivity(IntEnum):
    PUBLIC = 0
    INTERNAL = 1
    RESTRICTED_PII = 2
    CREDENTIAL = 3


_PRIVATE_KEY = re.compile(r"(?:password|passwd|secret|token|api[_-]?key|cookie|authorization|credential|otp|cvv|ssn|aadhaar|account[_-]?number|screenshot|base64|raw[_-]?html)", re.I)
_PII_VALUE = re.compile(r"\b(?:[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}|\+?\d[\d -]{8,}\d)\b")


def classify_sensitivity(value: str, key: str = "") -> Sensitivity:
    """Conservative pattern classification for persistence decisions."""
    if _PRIVATE_KEY.search(key) or any(PATTERNS[name].search(value) for name in
                                   ("api_key", "jwt", "aws_key", "slack_token", "private_key", "sensitive_kv")):
        return Sensitivity.CREDENTIAL
    if _PII_VALUE.search(value) or PATTERNS["credit_card"].search(value) or PATTERNS["ssn"].search(value):
        return Sensitivity.RESTRICTED_PII
    return Sensitivity.INTERNAL if value.strip() else Sensitivity.PUBLIC


def scrub_persistent(value: Any, key: str = "", depth: int = 0) -> Any:
    """Fail closed for known sensitive fields and redact PII in episodic records."""
    if depth > 15:
        raise ValueError("persistent payload nesting exceeds privacy limit")
    if _PRIVATE_KEY.search(key):
        return REDACTED
    if isinstance(value, dict):
        return {str(k): scrub_persistent(v, str(k), depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub_persistent(item, key, depth + 1) for item in value[:1000]]
    if isinstance(value, str):
        return _PII_VALUE.sub(REDACTED, _redact_text(redact_text_urls(value), 100_000))
    if value is None or isinstance(value, (int, float, bool)):
        return value
    raise ValueError("unsupported persistent payload type")

SENSITIVE_INPUT_TYPES = {"password", "hidden"}
SENSITIVE_NAME_PATTERNS = re.compile(
    r"\b("
    r"password|passwd|pwd|token|secret|api[_\s]?key|apikey|"
    r"credit[_\s]?card|creditcard|card[_\s]?number|cvv|cvc|ssn|social[_\s]?security|"
    r"pin|pincode|auth|authentication|bearer|private[_\s]?key|otp|one[_\s-]?time|session[_\s]?id|"
    r"cookie|authorization|security[_\s]?code|routing[_\s]?number|iban|"
    r"bank[_\s]?account|upi[_\s]?id"
    r")\b",
    re.IGNORECASE,
)


def _is_sensitive_name(name: Optional[str]) -> bool:
    if not name:
        return False
    return bool(SENSITIVE_NAME_PATTERNS.search(name))

# Query-string keys whose values must never leave the host.
SENSITIVE_QUERY_KEYS = re.compile(
    r"^(?:"
    r"(?:access|id|refresh|session|auth|user|csrf|xsrf|verification)_?token|"
    r"token|api_?(?:key|secret)|key|secret|client_?secret|"
    r"password|passwd|pwd|auth|authorization|session|session_?id|sid|"
    r"sig|signature|code|otp|email|user|username|login|"
    r"card|cc|pan|cvv|cvc|ssn|pin"
    r")$",
    re.IGNORECASE,
)

_SAFE_SCREENSHOT_RE = re.compile(
    r"^data:image/(?:jpeg|jpg|png|webp|gif);base64,[A-Za-z0-9+/=\s]+$",
    re.IGNORECASE,
)
URL_IN_TEXT_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)

PATTERNS = {
    "credit_card": re.compile(r"\b(?:\d[ -]*?){13,16}\b"),
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "api_key": re.compile(
        r"(?:bearer\s+[a-zA-Z0-9_\-\.]{16,}"
        r"|(?:ghp|gho|glpat|sk_live_|sk_test_|rk_live_|AIza)[a-zA-Z0-9_\-\.]{12,}"
        r"|\bsk-[a-zA-Z0-9_\-\.]{16,})",
        re.IGNORECASE,
    ),
    "jwt": re.compile(r"\beyJ[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+\b"),
    "aws_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "slack_token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "email": re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
    "sensitive_kv": re.compile(
        r"(?i)\b((?:session|auth|access|refresh|id|user|csrf|xsrf)_?token|client_?secret|api_?key|password|passwd|secret)\s*(?:[:=]|\bis\b)\s*([a-zA-Z0-9_\-\.]{8,})"
    ),
}


def _has_sensitive_value(val: str) -> bool:
    for pattern in PATTERNS.values():
        if pattern.search(val):
            return True
    return False


def _redact_text(value: str, max_len: int) -> str:
    """Mask known secret patterns in free text, then truncate."""
    out = value
    for name, pattern in PATTERNS.items():
        if name == "sensitive_kv":
            out = pattern.sub(r"\1: " + REDACTED, out)
        else:
            out = pattern.sub(REDACTED, out)
    return out[:max_len]


def _sensitive_query_key(key: str) -> bool:
    """Match a query key even when it is URL-encoded or array-suffixed."""
    k = unquote_plus(key).strip().lower()
    k = re.sub(r"\[[^\]]*\]$", "", k)  # token[] / token[0]
    return bool(SENSITIVE_QUERY_KEYS.match(k))


def redact_url(url: str) -> str:
    """Strip URL userinfo and mask sensitive query/fragment values.

    Falls back to pattern redaction for unparsable URLs — never returns the
    original unfiltered string.
    """
    if not url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return _redact_text(url, MAX_URL_LEN)

    netloc = parts.netloc
    if "@" in netloc:
        # userinfo may be "user:password" — keep only the host.
        netloc = REDACTED + "@" + netloc.rsplit("@", 1)[1]

    def _render_query(raw: str) -> str:
        pairs: list[str] = []
        for piece in raw.split("&"):
            if not piece:
                continue
            key, sep, value = piece.partition("=")
            if not sep:
                pairs.append(key)
                continue
            decoded = unquote_plus(value)
            if _sensitive_query_key(key) or _has_sensitive_value(decoded):
                pairs.append(f"{key}={REDACTED}")
            else:
                # Keep the original encoding: re-encoding an already-escaped
                # value would turn %20 into %2520 and corrupt the URL.
                pairs.append(piece)
        return "&".join(pairs)

    query = _render_query(parts.query) if parts.query else ""
    fragment = parts.fragment
    if fragment:
        # Hash routes often carry a query of their own: #/callback?token=...
        if "?" in fragment:
            route, _, frag_query = fragment.partition("?")
            fragment = f"{route}?{_render_query(frag_query)}"
        elif "=" in fragment:
            fragment = _render_query(fragment)
        else:
            fragment = _redact_text(fragment, MAX_URL_LEN)

    safe = urlunsplit((parts.scheme, netloc, parts.path, query, fragment))
    return _redact_text(safe, MAX_URL_LEN)


def redact_text_urls(text: str) -> str:
    """Apply :func:`redact_url` to every http(s) URL embedded in free text.

    Evidence and summary strings echo page URLs; those routinely contain
    session tokens. Redacting the whole string here keeps tokens out of the
    task record and the UI timeline.
    """
    if not text:
        return text
    def _one(match: re.Match[str]) -> str:
        raw = match.group(0)
        # Keep trailing punctuation out of the parsed URL.
        suffix = ""
        while raw and raw[-1] in ".,;:)]}":
            suffix = raw[-1] + suffix
            raw = raw[:-1]
        return redact_url(raw) + suffix
    return URL_IN_TEXT_RE.sub(_one, text)


def _redact_element(el: InteractiveElement) -> InteractiveElement:
    """Pure per-element redactor. Raises through on bad input."""
    value: Optional[str] = el.value
    if el.input_type in SENSITIVE_INPUT_TYPES:
        value = None
    if _is_sensitive_name(el.name):
        value = None
    if value is not None:
        if _has_sensitive_value(value):
            value = None
        else:
            value = _redact_text(value, MAX_NAME_LEN)
    update_dict: dict[str, Any] = {
        "value": value,
        "name": _redact_text(el.name or "", MAX_NAME_LEN),
    }
    if el.semantic:
        sem = el.semantic
        ia = sem.interactability
        bounds = ia.get("bounds", {})
        safe_bounds = {k: float(bounds[k]) for k in ("x", "y", "width", "height")}
        if not all(__import__("math").isfinite(v) and abs(v) < 100000 for v in safe_bounds.values()):
            raise ValueError("invalid semantic bounds")
        update_dict["semantic"] = sem.model_copy(update={
            "semantic_name": _redact_text(sem.semantic_name, MAX_NAME_LEN),
            "context_text": "" if el.input_type in SENSITIVE_INPUT_TYPES or _is_sensitive_name(el.name) or _has_sensitive_value(sem.context_text)
                or re.search(r"\b(?:token|secret|password|api[_-]?key|session|otp)\s*[:=]", sem.context_text, re.I)
                else _redact_text(sem.context_text, 160),
            "interactability": {"bounds": safe_bounds, "visible": bool(ia.get("visible")),
                "opacity": float(ia.get("opacity", 1)), "pointerEvents": str(ia.get("pointerEvents", ""))[:24],
                "occluded": bool(ia.get("occluded"))},
        })
    if el.href:
        update_dict["href"] = redact_url(el.href)
    return el.model_copy(update=update_dict)


@dataclass
class PrivacyFilteredPayload:
    """Sanitized observation ready for the planner, plus provenance counts."""
    tab_id: int
    url: str
    title: str
    interactive: list[InteractiveElement]
    scroll: dict
    dpr: float
    elements_original: int
    elements_sent: int
    screenshot: Optional[str] = None
    tabs: list[dict[str, Any]] = field(default_factory=list)
    page_text: Optional[str] = None
    query_terms: set[str] = field(default_factory=set)

    def to_context_block(self) -> str:
        """Render as an UNTRUSTED-DATA block for the planner prompt."""
        lines = [f"URL: {self.url}", f"TITLE: {self.title}", f"TAB_ID: {self.tab_id}"]
        if self.tabs and len(self.tabs) > 1:
            lines.append("OPEN TABS:")
            for t in self.tabs:
                tid = t.get("tabId")
                turl = t.get("url") or ""
                ttitle = t.get("title") or ""
                is_act = " (active)" if tid == self.tab_id or t.get("active") else ""
                lines.append(f"  - [Tab {tid}]{is_act} {turl} — {ttitle}")
        def _pri(e: InteractiveElement) -> int:
            nm = (e.name or "").lower()
            href = (e.href or "").lower()
            role = (e.role or "").lower()
            tag = (e.tag or "").lower()
            input_type = (e.input_type or "").lower()
            visible = bool(getattr(e, "visible", True))
            search_classified = bool(getattr(e, "search", None))

            # 0. Obstructive modal close, popup dismiss, ad skip.
            if role in ("close-button", "ad-skip") or any(k in nm for k in ("close", "skip ad", "cancel search")):
                return 0

            # 0. Primary search controls always outrank result/navigation noise.
            #    Classification comes from the observer (aria-label/label/name/
            #    id/type/form evidence) and is not just a name substring.
            is_search_field = (
                search_classified
                or role == "searchbox"
                or input_type == "search"
                or (
                    (role in ("textbox", "combobox", "input") or tag in ("input", "textarea"))
                    and visible
                    and (
                        bool(re.search(r"\b(search|query|keywords?|find)\b", nm))
                        or nm.strip() == "q"
                    )
                )
            )
            is_search_button = (
                search_classified
                and (role in ("button", "submit") or tag == "button")
            )
            if visible and (is_search_field or is_search_button):
                return 0

            # 1. Direct query match and ordinary text-entry controls. Only the
            #    search field above is promoted to 0; a page cannot flood the
            #    payload with every textbox at the top.
            if self.query_terms and any(t in nm for t in self.query_terms):
                return 1
            if visible and (
                role in ("textbox", "combobox", "input")
                or tag in ("textarea",)
                or (tag == "input" and input_type not in ("hidden", "password"))
            ):
                return 1

            # 2. Buttons and other actionable controls stay visible.
            if role in ("button", "submit", "menuitem", "tab", "checkbox", "radio", "switch") or tag in ("button", "select"):
                return 2

            # 3. Product/result links, prices, and structured result rows.
            is_product_link = any(seg in href for seg in ("/dp/", "/gp/", "/p/", "/item/", "/product/", "/wiki/", "/docs/"))
            has_price = any(sym in nm for sym in ("₹", "$", "€", "£", "rs."))
            if is_product_link or has_price:
                return 3
            if role in ("listitem", "row") and e.name:
                return 3

            # 4. Descriptive links (still below every control and result).
            if role == "link" and e.name and len(e.name) > 10:
                return 4

            if e.name:
                return 5
            return 6

        max_elements = int(getattr(config, "MAX_INTERACTIVE_ELEMENTS", 40))
        sorted_els = sorted(self.interactive, key=_pri)
        seen_hrefs: set[str] = set()
        deduped_els: list[InteractiveElement] = []
        for el in sorted_els:
            h = (el.href or "").split("?")[0].rstrip("/").lower()
            if h and any(seg in h for seg in ("/dp/", "/gp/product/", "/p/")):
                if h in seen_hrefs:
                    continue
                seen_hrefs.add(h)
            deduped_els.append(el)
            if len(deduped_els) >= max_elements:
                break

        for el in deduped_els:
            bits = [f"id={el.target_id}", el.role, f'name="{el.name}"', "disabled" if not el.enabled else ""]
            if el.href:
                clean_href = el.href[:120] + "..." if len(el.href) > 120 else el.href
                bits.append(f'href="{clean_href}"')
            if el.value is not None:
                bits.append(f'value="{el.value}"')
            if el.semantic:
                sem = el.semantic
                ia = sem.interactability
                bits.append(f"control={sem.control_type}")
                bits.append(f"semantic_name={sem.semantic_name!r}")
                bounds = ia.get("bounds")
                if bounds:
                    bits.append(f"bounds={bounds['x']},{bounds['y']},{bounds['width']},{bounds['height']}")
                if ia.get("occluded") or not ia.get("visible") or ia.get("pointerEvents") == "none":
                    bits.append("non_interactable")
                if sem.context_text:
                    bits.append(f"context={sem.context_text!r}")
            lines.append("- " + " ".join(b for b in bits if b))
        if self.page_text:
            from .adversarial_guard import quarantine_text
            cleaned_text = re.sub(r"[ \t]+", " ", self.page_text)
            cleaned_text = re.sub(r"\n\s*\n+", "\n", cleaned_text).strip()
            if cleaned_text:
                lines.append("\n<page-text>")
                lines.append(quarantine_text(cleaned_text[:2000], "page"))
                lines.append("</page-text>")
        return "\n".join(lines)


def classify_and_redact(
    obs: BrowserObservation, *, instruction: str, record_metrics: bool = True
) -> tuple[PrivacyFilteredPayload, PrivacyDecision]:
    _stopwords = {
        "and", "the", "for", "check", "its", "find", "open", "show", "get", "with",
        "from", "on", "in", "to", "a", "an", "is", "of", "at", "by", "website",
        "page", "site", "please", "can", "you", "now", "it", "or", "what", "how",
    }
    query_terms = {
        w.lower()
        for w in re.findall(r"\b[a-zA-Z0-9_\-\.]{2,}\b", instruction or "")
        if w.lower() not in _stopwords
    }
    try:
        redacted = [_redact_element(e) for e in obs.interactive]
        # Fail-closed privacy: if any element on page is sensitive (password/auth/card), omit screenshot
        has_sensitive_element = any(
            e.input_type in SENSITIVE_INPUT_TYPES or _is_sensitive_name(e.name)
            for e in obs.interactive
        )
        has_sensitive_element = has_sensitive_element or any(
            _has_sensitive_value(str(text or ""))
            for text in (obs.title, getattr(obs, "page_text", None))
        )
        safe_screenshot = None
        if getattr(config, "ALLOW_SCREENSHOTS", False) and not has_sensitive_element:
            raw_shot = getattr(obs, "screenshot", None)
            if (
                isinstance(raw_shot, str)
                and len(raw_shot) <= 4 * 1024 * 1024
                and _SAFE_SCREENSHOT_RE.match(raw_shot)
            ):
                safe_screenshot = raw_shot
        scrubbed_tabs = []
        for t in getattr(obs, "tabs", None) or []:
            scrubbed_tabs.append({
                "tabId": t.get("tabId"),
                "url": redact_url(t.get("url") or ""),
                "title": _redact_text(t.get("title") or "", MAX_TITLE_LEN),
                "active": bool(t.get("active")),
            })
        safe_page_text = None
        raw_text = getattr(obs, "page_text", None) or (obs.get("pageText") if isinstance(obs, dict) else None)
        if raw_text:
            from .adversarial_guard import cleanse_untrusted
            max_chars = int(getattr(config, "MAX_PAGE_TEXT_CHARS", 2500))
            safe_page_text = cleanse_untrusted(_redact_text(redact_text_urls(str(raw_text)), max_chars), max_chars)
        payload = PrivacyFilteredPayload(
            tab_id=obs.tab_id,
            url=redact_url(obs.url),
            title=_redact_text(obs.title, MAX_TITLE_LEN),
            interactive=redacted,
            scroll=obs.scroll,
            dpr=obs.dpr,
            elements_original=len(obs.interactive),
            elements_sent=len(redacted),
            screenshot=safe_screenshot,
            tabs=scrubbed_tabs,
            page_text=safe_page_text,
            query_terms=query_terms,
        )
        decision = PrivacyDecision(send=True, reason="redaction succeeded", violations=[])
        redacted_any = (payload.url != obs.url or payload.title != obs.title or
                        any((e.value != safe.value or e.name != safe.name) for e, safe in zip(obs.interactive, redacted)) or
                        bool(raw_text and safe_page_text != str(raw_text)))
        if record_metrics:
            with _privacy_lock:
                _privacy_counts["observations"] += 1
                _privacy_counts["redactedObservations"] += int(redacted_any)
                _privacy_counts["screenshotsWithheld"] += int(bool(getattr(obs, "screenshot", None) and safe_screenshot is None))
                if redacted_any:
                    _privacy_events.append({"at": time.time(), "type": "observation_redacted"})
                if getattr(obs, "screenshot", None) and safe_screenshot is None:
                    _privacy_events.append({"at": time.time(), "type": "screenshot_withheld"})
        return payload, decision
    except Exception as exc:  # fail-closed: any filter crash blocks the send
        raise PrivacyViolation(f"privacy filter failed; payload NOT sent: {exc}") from exc
