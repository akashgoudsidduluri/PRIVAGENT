"""Takeover detection + blocked-action policy (Task 4).

Takeover is the only way a login / payment / OTP / CAPTCHA step is ever
completed: the agent pauses, hands the browser to the operator, and
stops observing — no screenshots, no DOM snapshots, no page text sent
to any planner — until the operator says they are done (or cancels).

This module is deliberately pure (no asyncio, no hub, no LLM): the
detection rules are deterministic and unit-testable, and the agent
loop owns all waiting / state. The loop calls
:func:`detect_takeover_trigger` on every fresh redacted observation;
a trigger means "enter takeover now, do not plan on this observation".

What the agent can NEVER see
-----------------------------
Full card numbers, CVV/CVC, full passwords, OTP / TOTP / one-time
codes. Those values are never typed by the agent, never stored in the
vault (see :mod:`app.vault`), never screenshotted, and never sent to a
planner. If a page shows one of those fields, the correct action is
takeover, not typing.

Blocked outright (no gate, no override)
---------------------------------------
* Agent-initiated account creation: the agent never creates an account
  for the operator (sign-up / register surface + typing a password or
  submitting the form).
* Permanent deletion: the agent never clicks a control whose job is to
  permanently delete / close an account (or any element named as a
  permanent delete of an account).
These are heuristics on element names / URL segments, not a proof —
the docs say so. A human who wants to create an account or delete one
does it themselves, outside the agent.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlsplit

__all__ = [
    "TAKEOVER_KINDS",
    "NEVER_SEE",
    "TakeoverTrigger",
    "detect_takeover_trigger",
    "is_otp_field",
    "is_payment_surface",
    "is_blocked_action",
    "takeover_reason_for_wait",
]

TAKEOVER_KINDS = ("login", "payment", "otp", "captcha", "2fa")

#: Values the agent must never see, type, store, or send to a planner.
NEVER_SEE = (
    "full card number (PAN)",
    "CVV / CVC / security code",
    "full password",
    "OTP / TOTP / one-time code",
    "one-time verification code",
)

# Whole path segments that indicate a credential / payment surface.
# Kept in sync with agentLoop._AUTH_PATH_TOKENS (the loop imports the
# set from here so there is exactly one definition).
AUTH_PATH_TOKENS = {
    "signin", "sign-in", "login", "log-in", "logon", "log-on", "signup",
    "sign-up", "auth", "authenticate", "authorize", "oauth", "oauth2",
    "otp", "verify", "verification", "mfa", "2fa", "password", "passwd",
    "checkout", "payment", "pay-now", "makepayment", "billing", "purchase",
}

PAYMENT_PATH_TOKENS = {
    "checkout", "payment", "pay-now", "makepayment", "billing",
    "purchase", "pay", "order-review", "place-order",
}

_AUTH_HOSTS = (
    "accounts.google.com",
    "login.microsoftonline.com",
    "login.live.com",
    "login.microsoft.com",
    "appleid.apple.com",
    "auth0.com",
    "okta.com",
    "onelogin.com",
)

_CAPTCHA_MARKERS = (
    "recaptcha",
    "hcaptcha",
    "cf-turnstile",
    "turnstile",
    "i'm not a robot",
)

_OTP_NAME_RE = re.compile(
    r"\b(otp|one[_\s-]?time[_\s-]?(?:pass)?code|verification[_\s-]?code|"
    r"auth[_\s-]?code|2fa[_\s-]?code|mfa[_\s-]?code|security[_\s-]?code)\b",
    re.IGNORECASE,
)
_CARD_FIELD_RE = re.compile(
    r"\b(card[_\s-]?(?:number|no\.?|num)|credit[_\s-]?card|"
    r"card[_\s-]?holder|expir(?:y|ation)|cvv|cvc|security[_\s-]?code)\b",
    re.IGNORECASE,
)
_PERMANENT_DELETE_RE = re.compile(
    r"\b(delete|close|remove)\b[\s\S]{0,30}\b(account|profile)\b[\s\S]{0,20}\b(permanently|forever)\b"
    r"|\bpermanently\b[\s\S]{0,30}\b(delete|close|remove)\b[\s\S]{0,20}\b(account|profile)\b",
    re.IGNORECASE,
)
_SIGNUP_TOKENS = {"signup", "sign-up", "register", "create-account", "createaccount"}


def _host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _path_tokens(url: str) -> set[str]:
    try:
        path = urlsplit(url).path.lower()
    except ValueError:
        return set()
    tokens: set[str] = set()
    for token in re.split(r"[/_.]+", path):
        if not token:
            continue
        tokens.add(token)
        if "-" in token:
            tokens.update(part for part in token.split("-") if part)
    return tokens


def _host_matches(host: str, domain: str) -> bool:
    return bool(host) and (host == domain or host.endswith("." + domain))


def _el_text(el: Any) -> str:
    if isinstance(el, dict):
        parts = [el.get("name"), el.get("inputType"), el.get("role"), el.get("tag")]
    else:
        parts = [
            getattr(el, "name", None),
            getattr(el, "input_type", None),
            getattr(el, "role", None),
            getattr(el, "tag", None),
        ]
    return " ".join(str(p) for p in parts if p)


def _el_name(el: Any) -> str:
    if isinstance(el, dict):
        return str(el.get("name") or "")
    return str(getattr(el, "name", "") or "")


def _el_input_type(el: Any) -> str:
    if isinstance(el, dict):
        return str(el.get("inputType") or el.get("input_type") or "")
    return str(getattr(el, "input_type", "") or "")


def is_otp_field(el: Any) -> bool:
    """True for an element that asks for a one-time code.

    Name-based (``otp``, ``verification code`` …) or a short numeric
    field whose name mentions a code. A plain 6-digit field with no
    code-related name is *not* treated as OTP — that would false-positive
    on PIN / quantity fields.
    """
    name = _el_name(el)
    if _OTP_NAME_RE.search(name):
        return True
    if _el_input_type(el).lower() in {"tel", "text", "number"}:
        if re.search(r"\b(code|otp)\b", name, re.IGNORECASE):
            return True
    return False


def is_payment_surface(url: str, interactive: list[Any] | None = None) -> bool:
    """True for a checkout / payment page (URL) or a page showing card fields."""
    tokens = _path_tokens(url or "")
    if tokens & PAYMENT_PATH_TOKENS:
        return True
    for el in interactive or []:
        if _CARD_FIELD_RE.search(_el_name(el)):
            return True
    return False


def _is_auth_url(url: str) -> bool:
    host = _host_of(url or "")
    if not host:
        return False
    if any(_host_matches(host, h) for h in _AUTH_HOSTS):
        return True
    return bool(_path_tokens(url) & AUTH_PATH_TOKENS)


def _has_captcha(interactive: list[Any] | None, page_text: str = "") -> bool:
    hay = " ".join(_el_text(el) for el in (interactive or []))
    if any(m in hay.lower() for m in _CAPTCHA_MARKERS):
        return True
    return any(m in (page_text or "").lower() for m in _CAPTCHA_MARKERS)


@dataclass
class TakeoverTrigger:
    """Why the loop must hand control to the operator."""

    kind: str  # one of TAKEOVER_KINDS
    reason: str
    url: str = ""

    @property
    def key(self) -> str:
        """Stable identity for "this surface was already taken over once".

        The URL path (not the full URL — query strings carry tokens and
        session ids) plus the kind, so a resume that lands back on the
        same login page is recognised as the *same* blocker. Payment
        is keyed by merchant host only: after a payment the operator
        usually lands on ``/checkout/success`` or similar, which is
        the same merchant's payment flow, not a new payment.
        """
        host = _host_of(self.url)
        if self.kind == "payment":
            return f"payment:{host}"
        try:
            path = urlsplit(self.url).path.rstrip("/") or "/"
        except ValueError:
            path = "/"
        return f"{self.kind}:{host}{path}"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "reason": self.reason, "url": self.url}


def detect_takeover_trigger(
    url: str,
    interactive: list[Any] | None = None,
    page_text: str = "",
) -> Optional[TakeoverTrigger]:
    """Detect a surface the agent must never complete itself.

    Priority order matters: an OTP field is the most specific signal
    (it must win over a generic login page), then payment, then
    CAPTCHA, then a plain login surface.
    """
    interactive = list(interactive or [])
    for el in interactive:
        if is_otp_field(el):
            return TakeoverTrigger(
                kind="otp",
                reason=(
                    "A one-time-code (OTP) field is visible. The agent never "
                    "sees, types, or stores one-time codes — complete this "
                    "step yourself in the browser tab."
                ),
                url=url or "",
            )
    if is_payment_surface(url, interactive):
        return TakeoverTrigger(
            kind="payment",
            reason=(
                "A payment / checkout surface is visible. Payments always "
                "require your approval and your own hands in the browser — "
                "the agent never submits a payment."
            ),
            url=url or "",
        )
    if _has_captcha(interactive, page_text):
        return TakeoverTrigger(
            kind="captcha",
            reason=(
                "A CAPTCHA is blocking the page. The agent never solves "
                "CAPTCHAs — complete it yourself in the browser tab."
            ),
            url=url or "",
        )
    if _is_auth_url(url):
        return TakeoverTrigger(
            kind="login",
            reason=(
                "A sign-in surface is visible. Sign in yourself in the "
                "browser tab — the agent never types passwords."
            ),
            url=url or "",
        )
    return None


def takeover_reason_for_wait(url: str, interactive: list[Any] | None = None) -> Optional[TakeoverTrigger]:
    """Takeover trigger for a planner-emitted sensitive ``wait``.

    The planner's human handoff (CAPTCHA / login wall) is a wait with
    risk=sensitive; when the current surface is one of the takeover
    surfaces, that wait becomes a takeover instead of a plain
    confirmation — the operator does the step in the tab, and the
    agent observes nothing while they do.
    """
    return detect_takeover_trigger(url, interactive)


def is_blocked_action(
    instruction: str,
    action: dict[str, Any] | None,
    url: str = "",
    interactive: list[Any] | None = None,
) -> Optional[str]:
    """Return a reason if *action* is blocked outright, else None.

    Blocked outright (no gate, no override):
    * creating an account for the operator;
    * permanently deleting / closing an account.
    Both are name/URL heuristics, not proofs — see the module docstring.
    """
    if not isinstance(action, dict):
        return None
    action_type = action.get("type")
    target = action.get("target") or {}
    target_name = ""
    if isinstance(target, dict):
        target_name = str(target.get("name") or "")
    tokens = _path_tokens(url or "")

    # --- Account creation -------------------------------------------------
    if tokens & _SIGNUP_TOKENS:
        # Typing into a password field, or submitting, on a sign-up page
        # is the agent creating an account.
        if action_type == "type":
            # The target element (if we can resolve it) is a password field,
            # or the instruction itself asks to create an account.
            if "password" in target_name.lower() or re.search(
                r"\b(create|open|register|sign\s*up|set\s*up)\b[\s\S]{0,40}\baccount\b",
                instruction or "",
                re.IGNORECASE,
            ):
                return (
                    "Account creation is blocked: the agent never creates an "
                    "account for you. Create it yourself in the browser if "
                    "you want one, then ask the agent to continue."
                )
        if action_type == "click" and re.search(
            r"\b(create|open|register|sign\s*up)\b[\s\S]{0,30}\baccount\b",
            target_name,
            re.IGNORECASE,
        ):
            return (
                "Account creation is blocked: the agent never creates an "
                "account for you."
            )
    if re.search(
        r"\b(create|open|register|sign\s*up|set\s*up)\b[\s\S]{0,40}\baccount\b",
        instruction or "",
        re.IGNORECASE,
    ) and action_type == "type" and "password" in target_name.lower():
        return (
            "Account creation is blocked: the agent never creates an "
            "account for you."
        )

    # --- Permanent deletion ----------------------------------------------
    if action_type == "click" and _PERMANENT_DELETE_RE.search(target_name):
        return (
            "Permanent account deletion is blocked: the agent never deletes "
            "an account. Delete it yourself in the browser if you really "
            "mean to."
        )
    return None
