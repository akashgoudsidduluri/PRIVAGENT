"""Plan preview for the operator confirmation gate (Task 12).

When the loop pauses for confirmation, the operator should see WHAT
the agent is about to do — the whole validated plan, in plain
language — before approving anything. This module turns a validated
plan (a list of steps, each ``{"action": {...}}``) into a list of
plain preview dicts built from structured fields only:

* ``type`` / ``risk`` — the action type and its risk class.
* ``target`` — name / role / elementRef, when the step addresses an
  element. These are planner-chosen labels from the accessibility
  tree, not page markup.
* ``detail`` — one short plain-language line: a host (never a full
  URL — query strings can carry secrets), a truncated typed text, or
  a generic description.

Safety rules:

* No raw page text, no planner free text (``why``), no URLs with
  path/query, no evidence strings. The preview is data the UI
  renders with ``textContent``; it must be safe even if a field
  echoes page content, because the transport (``pending_confirmation``)
  is also passed through ``scrub_persistent`` and the UI never puts
  it into an HTML sink.
* Typed text into a secret-named target (password, passcode, PIN,
  OTP, token, ...) is masked — the operator approves the action,
  not the secret. A vault placeholder
  (``{{CREDENTIAL:domain:kind:label}}``) is shown as-is: it is
  already a non-secret reference, and showing it tells the operator
  which saved credential will be used.
* A malformed step degrades to ``"unknown step"`` instead of raising:
  the preview must never be the reason a task crashes.
"""
from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import urlsplit

MAX_DETAIL_CHARS = 120
MASKED_TEXT = "•••••• (hidden — sensitive field)"

# A typed value that is a vault reference, not a secret.
_VAULT_PLACEHOLDER_PREFIX = "{{CREDENTIAL:"

# REVIEW (Task 12 audit): the old mask only fired when the target
# name/role contained "password", so a literal typed into a field
# named "Passcode", "PIN", "One-time code", "Security code" or
# "API token" was shown in the preview in full — and scrub_persistent
# does not redact a bare value, so it also reached the
# GET /api/task/{id} response. The vocabulary mirrors the sensitive
# names in privacy.py (SENSITIVE_NAME_PATTERNS), word-bounded so a
# field named "Pinterest" is not masked. Over-masking is fail-safe
# here: the operator sees "hidden" instead of the value, and can
# still approve the action.
_SECRET_NAME_RE = re.compile(
    r"\b(password|passwd|pwd|passcode|pin|otp|one[- ]time|secret|token|"
    r"cvv|cvc|security[- ]code|auth[- ]code|private[- ]key|"
    r"seed[- ]phrase|ssn)\b",
    re.IGNORECASE,
)


def _action_of(step: Any) -> dict[str, Any]:
    if isinstance(step, dict):
        action = step.get("action")
        return action if isinstance(action, dict) else {}
    action = getattr(step, "action", None)
    if isinstance(action, dict):
        return action
    if action is not None:
        return {
            "type": getattr(action, "type", None),
            "target": getattr(action, "target", None),
            "params": getattr(action, "params", None),
            "risk": getattr(action, "risk", None),
        }
    return {}


def _target_of(action: dict[str, Any]) -> dict[str, Any]:
    target = action.get("target")
    if isinstance(target, dict):
        return target
    if target is None:
        return {}
    return {
        "targetId": getattr(target, "target_id", None),
        "target_id": getattr(target, "target_id", None),
        "name": getattr(target, "name", None),
        "role": getattr(target, "role", None),
        "elementRef": getattr(target, "element_ref", None),
        "element_ref": getattr(target, "element_ref", None),
    }


def _params_of(action: dict[str, Any]) -> dict[str, Any]:
    params = action.get("params")
    if isinstance(params, dict):
        return params
    if params is None:
        return {}
    return {
        "submit": getattr(params, "submit", None),
        "text": getattr(params, "text", None),
        "url": getattr(params, "url", None),
    }


def _truncate(text: str, limit: int = MAX_DETAIL_CHARS) -> str:
    text = " ".join(str(text).split())
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def _host_only(url: str) -> str:
    """Hostname only — never the path or query (they can carry secrets)."""
    try:
        host = (urlsplit(str(url)).hostname or "").lower()
    except Exception:
        host = ""
    return host or "unknown site"


def _is_password_target(target: dict[str, Any]) -> bool:
    name = str(target.get("name") or "")
    role = str(target.get("role") or "")
    return bool(_SECRET_NAME_RE.search(name) or _SECRET_NAME_RE.search(role))


def _type_detail(action: dict[str, Any], target: dict[str, Any]) -> str:
    params = _params_of(action)
    text = params.get("text")
    if text is None or str(text) == "":
        return "type text into the field"
    raw = str(text)
    if raw.startswith(_VAULT_PLACEHOLDER_PREFIX):
        # A vault reference is already non-secret; show which saved
        # credential will be substituted just before execution.
        return f"type saved credential {raw}"
    if _is_password_target(target):
        return f"type {MASKED_TEXT}"
    return f'type "{_truncate(raw)}"'


def describe_plan_step(step: Any, index: int) -> dict[str, Any]:
    """One plain-language preview dict for one validated plan step."""
    action = _action_of(step)
    atype = str(action.get("type") or "unknown")
    target = _target_of(action)
    name = target.get("name")
    role = target.get("role")
    ref = target.get("elementRef") or target.get("element_ref")
    params = _params_of(action)

    if atype in {"navigate", "new_tab"}:
        detail = f"go to {_host_only(params.get('url') or '')}"
    elif atype == "type":
        detail = _type_detail(action, target)
        if params.get("submit"):
            detail += " and submit"
    elif atype == "click":
        detail = "click" + (f' "{_truncate(str(name))}"' if name else " the element")
    elif atype == "select_option":
        detail = "choose an option" + (f' on "{_truncate(str(name))}"' if name else "")
    elif atype == "set_date":
        detail = "set a date" + (f' on "{_truncate(str(name))}"' if name else "")
    elif atype == "upload_file":
        detail = "upload the attached file" + (f' to "{_truncate(str(name))}"' if name else "")
    elif atype == "scroll":
        detail = "scroll the page"
    elif atype == "zoom":
        detail = "zoom the page"
    elif atype == "wait":
        detail = "wait for the page"
    elif atype == "observe":
        detail = "look at the page"
    elif atype == "webmcp_tool":
        # Task 14: tool name only — arguments may carry operator data
        # and are never rendered in the preview.
        _tool = str(params.get("tool") or "")
        detail = f"call site tool \"{_truncate(_tool)}\"" if _tool else "call a site tool"
    elif atype == "ucp_checkout":
        # Task 14: merchant host + amount + currency only (structured
        # fields); no item or payment detail ever reaches the preview.
        # The merchant param is a bare host by validation, so it is
        # shown as-is (never a URL — _host_only would drop it).
        _merchant = str(params.get("merchant") or "unknown merchant")
        _amount = str(params.get("amount") or "")
        _currency = str(params.get("currency") or "")
        detail = f"check out via UCP at {_merchant}" + (f" for {_amount} {_currency}" if _amount else "")
    elif atype in {"switch_tab", "select_tab"}:
        detail = "switch to another tab"
    elif atype == "close_tab":
        detail = "close the tab"
    else:
        detail = "unknown step" if atype == "unknown" else atype

    return {
        "index": index,
        "type": atype,
        "risk": str(action.get("risk") or "read"),
        "target": {
            "name": _truncate(str(name)) if name else None,
            "role": _truncate(str(role)) if role else None,
            "elementRef": str(ref) if ref else None,
        },
        "detail": detail,
    }


def plan_preview(steps: Any) -> list[dict[str, Any]]:
    """Preview dicts for a whole validated plan (never raises)."""
    if not isinstance(steps, list):
        return []
    out: list[dict[str, Any]] = []
    for i, step in enumerate(steps):
        try:
            out.append(describe_plan_step(step, i))
        except Exception:
            out.append({
                "index": i,
                "type": "unknown",
                "risk": "read",
                "target": {"name": None, "role": None, "elementRef": None},
                "detail": "unknown step",
            })
    return out
