"""Post-action outcome verification and fresh-ref recovery (Task 1:
automation).

Two gaps the loop had before this module:

1. **Silent no-ops.** After an action the loop recorded the
   extension's ``ok`` and the completion verifier's receipt, but
   nothing ever *blocked advancement* on them: a click that changed
   nothing and a navigate that went nowhere were filed as
   ``completed_unverified`` and the loop carried on down the pinned
   plan (or, worse, pinned the next batch step) as if the step had
   worked. :func:`verify_step_outcome` gives every executed step an
   explicit verdict from observable signals — URL / title / page-text
   / DOM-digest change, plus the verifier receipt — with
   per-action-type expectations (a non-submitting ``type`` into a
   textbox legitimately leaves the page surface unchanged, so its
   outcome is the dispatch itself; a ``navigate`` that leaves the URL
   untouched is a no-op, full stop).

2. **Stale refs at execution time.** The plan-time freshness check
   (Task 13) only sees the tree the plan was built from, so a ref
   that goes stale *between* planning and execution surfaced as a
   ``STALE_TARGET`` error from the extension and the step just failed
   — the loop replanned from scratch instead of retrying the same
   step against the control it can still see. :func:`resolve_fresh_ref`
   finds that control again in a fresh tree (same stable target id,
   else the same role + accessible name, unambiguous only), so the
   loop can re-pin the step and retry it once. :func:`detect_dialog`
   recognises the other mid-run surprise — a popup/dialog appearing
   over the target — which gets the same one-retry treatment.

Everything here is pure (no loop state, no I/O) so the rules are
unit-testable without driving the loop. The loop itself (agentLoop)
owns the bounded retry (max 1 per step) and records the recovery in
the step record.
"""
from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlsplit

from .grounding import AxNode, AxTree, normalize_accessible_name

# Action types whose success is *defined* by a visible change. A
# successful dispatch with none of these signals is a silent no-op.
_CHANGE_EXPECTED_TYPES = frozenset({
    "navigate",
    "click",
    "select_option",
    "set_date",
    "upload_file",
})

# A submitting type is a mutating write (form submit, send, search
# submit): the page must answer it. A non-submitting type only fills
# a field — the AX observation carries no value echo, so no surface
# change is expected and the dispatch itself is the outcome.
_SUBMITTING_EXTRA = ("type",)

# Types with no observable outcome to verify: the action *is* the
# observation (observe), or moves the viewport / waits (scroll, wait,
# zoom), or moves between tabs the registry already tracks
# (select_tab, switch_tab, close_tab, new_tab).
_NO_CHANGE_EXPECTED_TYPES = frozenset({
    "observe", "scroll", "wait", "zoom",
    "select_tab", "switch_tab", "close_tab", "new_tab",
})

# Extension error codes that mean "a popup/dialog got in the way".
_DIALOG_ERROR_CODES = frozenset({"DIALOG", "UNEXPECTED_DIALOG", "MODAL_BLOCKED"})

# The extension reports STALE_TARGET (recoverable) when the element
# the loop aimed at is no longer where the plan said it was.
_STALE_ERROR_CODES = frozenset({"STALE_TARGET"})


def _field(obs: Any, *names: str) -> Any:
    """Read a field off a dict observation or a BrowserObservation."""
    if obs is None:
        return None
    for name in names:
        if isinstance(obs, dict):
            value = obs.get(name)
        else:
            value = getattr(obs, name, None)
        if value is not None:
            return value
    return None


def _text_of(obs: Any) -> str:
    value = _field(obs, "page_text", "pageText", "readable_text")
    return str(value or "")


def _url_parts(url: str) -> tuple[str, str]:
    try:
        parts = urlsplit(url or "")
        return ((parts.hostname or "").lower(), parts.path.rstrip("/") or "/")
    except ValueError:
        return ("", "")


def outcome_signals(before: Any, after: Any) -> dict[str, bool]:
    """The observable deltas between two observations.

    ``digest`` is the loop's state digest (URL | title | element count
    | first 12 target ids): it catches DOM re-renders that keep the
    URL and the visible text identical.
    """
    from .loop_helpers import _state_digest

    before_url = str(_field(before, "url") or "")
    after_url = str(_field(after, "url") or "")
    before_title = str(_field(before, "title") or "")
    after_title = str(_field(after, "title") or "")
    before_text = _text_of(before)
    after_text = _text_of(after)
    return {
        "url_changed": before_url != after_url,
        "title_changed": before_title != after_title,
        "text_changed": before_text != after_text,
        "digest_changed": _state_digest(before) != _state_digest(after),
    }


def _expects_change(action: dict[str, Any]) -> bool:
    atype = action.get("type")
    if atype in _CHANGE_EXPECTED_TYPES:
        return True
    if atype == "type":
        return bool((action.get("params") or {}).get("submit"))
    return False


def verify_step_outcome(
    action: dict[str, Any],
    result: dict[str, Any],
    *,
    before: Any = None,
    receipt: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Verdict for one executed step: did the outcome actually happen?

    Returns ``{"verified": bool, "expects_change": bool, "reason": str,
    "signals": {...}, "dialog_detected": bool}``. ``verified`` is
    False exactly when the action *should* have changed something and
    nothing observable changed and the verifier receipt did not
    confirm it either. A step the extension reported as failed is
    never verified (its failure is recorded separately).

    Signals, in order of strength:

    * the completion-verifier receipt (``receipt["verified"]``) — an
      authoritative confirmation from the page / executor;
    * any surface delta (URL, title, page text, DOM digest);
    * the executor echoing the aimed-at target id
      (``result["targetId"]``) or an explicit dispatch acknowledgement
      (``verification.dispatchAcknowledged``) — for clicks whose
      effect is not visible in the observation (a toggle, a select
      that fills a hidden field).

    A ``navigate`` is verified only by arriving: the after-URL must
    match the requested URL's host + path, or the title/text must
    have changed (a redirect landing on an equivalent page still
    counts as an arrival).
    """
    atype = action.get("type") or "step"
    after = result.get("observation") if isinstance(result, dict) else None
    signals = outcome_signals(before, after) if after is not None else {
        "url_changed": False,
        "title_changed": False,
        "text_changed": False,
        "digest_changed": False,
    }
    dialog = detect_dialog(after, result)
    receipt_verified = bool(receipt and receipt.get("verified"))
    expects_change = _expects_change(action)

    if not result.get("ok"):
        return {
            "verified": False,
            "expects_change": expects_change,
            "reason": "action failed at the extension; nothing to verify",
            "signals": signals,
            "dialog_detected": dialog,
            "receipt_verified": receipt_verified,
        }

    if not expects_change:
        if atype == "type":
            reason = "typed into field (no surface change expected for a non-submitting type)"
        elif atype in _NO_CHANGE_EXPECTED_TYPES:
            reason = f"{atype} has no observable outcome to verify"
        else:
            # Backend-executed steps (webmcp_tool, ucp_checkout) and
            # anything unlisted: the executor's ok plus its own
            # evidence is the outcome.
            reason = f"{atype} executed by its own executor (ok reported)"
        return {
            "verified": True,
            "expects_change": False,
            "reason": reason,
            "signals": signals,
            "dialog_detected": dialog,
            "receipt_verified": receipt_verified,
        }

    if atype == "navigate":
        target_url = str((action.get("params") or {}).get("url") or "")
        after_url = str(_field(after, "url") or "")
        before_url = str(_field(before, "url") or "")
        arrived = _url_parts(target_url) == _url_parts(after_url) and bool(after_url)
        if arrived and _url_parts(before_url) != _url_parts(after_url):
            return {
                "verified": True,
                "expects_change": True,
                "reason": f"arrived at {target_url}",
                "signals": signals,
                "dialog_detected": dialog,
                "receipt_verified": receipt_verified,
            }
        if arrived and (signals["title_changed"] or signals["text_changed"]):
            # Same URL, visibly different page (redirect landing on an
            # equivalent page, or a refresh that re-rendered content).
            return {
                "verified": True,
                "expects_change": True,
                "reason": "navigation landed (title/text changed) though the URL is unchanged",
                "signals": signals,
                "dialog_detected": dialog,
                "receipt_verified": receipt_verified,
            }
        if arrived:
            # Navigating to the URL the browser is already on, with
            # nothing on the page changing, is a no-op — "arrived"
            # trivially, but nothing happened to verify.
            return {
                "verified": False,
                "expects_change": True,
                "reason": f"navigation no-op: already on {target_url} and the page did not change",
                "signals": signals,
                "dialog_detected": dialog,
                "receipt_verified": receipt_verified,
            }
        if signals["title_changed"] or signals["text_changed"]:
            return {
                "verified": True,
                "expects_change": True,
                "reason": "navigation landed (title/text changed) though the URL differs from the request (redirect)",
                "signals": signals,
                "dialog_detected": dialog,
                "receipt_verified": receipt_verified,
            }
        return {
            "verified": False,
            "expects_change": True,
            "reason": f"navigation no-op: URL still {before_url} and the page did not change",
            "signals": signals,
            "dialog_detected": dialog,
            "receipt_verified": receipt_verified,
        }

    if receipt_verified:
        return {
            "verified": True,
            "expects_change": True,
            "reason": "completion verifier confirmed the outcome",
            "signals": signals,
            "dialog_detected": dialog,
            "receipt_verified": True,
        }
    if any(signals.values()):
        changed = ", ".join(k for k, v in signals.items() if v)
        return {
            "verified": True,
            "expects_change": True,
            "reason": f"page changed after the action ({changed})",
            "signals": signals,
            "dialog_detected": dialog,
            "receipt_verified": receipt_verified,
        }
    # Executor-side evidence: it aimed at the right element and says
    # the dispatch landed, even though the observation shows no delta.
    target = action.get("target") or {}
    target_echo = bool(
        result.get("targetId") and target.get("targetId")
        and str(result.get("targetId")) == str(target.get("targetId"))
    )
    dispatch_ack = bool(
        isinstance(result.get("verification"), dict)
        and result["verification"].get("dispatchAcknowledged")
    )
    if target_echo or dispatch_ack:
        return {
            "verified": True,
            "expects_change": True,
            "reason": "executor confirmed the dispatch on the aimed-at target (no surface delta in the observation)",
            "signals": signals,
            "dialog_detected": dialog,
            "receipt_verified": receipt_verified,
        }
    return {
        "verified": False,
        "expects_change": True,
        "reason": f"{atype} reported ok but nothing observable changed (no URL/title/text/digest delta, no verifier confirmation, no dispatch acknowledgement)",
        "signals": signals,
        "dialog_detected": dialog,
        "receipt_verified": receipt_verified,
    }


def detect_dialog(after: Any, result: Optional[dict[str, Any]] = None) -> bool:
    """True when a popup/dialog is visible over the page after an action.

    Sources, strongest first: an explicit ``dialog`` marker on the
    result, a dialog error code, or a dialog/alertdialog role in the
    post-action observation's interactive elements. A dialog does not
    by itself fail a step — the extension dismisses obstructive
    modals itself — but a step whose outcome is unverified *and* has
    a dialog over the target is a blocked step, not a completed one,
    and the loop re-observes and retries it once.
    """
    if isinstance(result, dict):
        if result.get("dialog"):
            return True
        err = result.get("error") or {}
        if isinstance(err, dict) and str(err.get("code") or "") in _DIALOG_ERROR_CODES:
            return True
    elements = _field(after, "interactive") or []
    for el in elements or []:
        if isinstance(el, dict):
            role = str(el.get("role") or "").lower()
        else:
            role = str(getattr(el, "role", "") or "").lower()
        if role in ("dialog", "alertdialog"):
            return True
    return False


def is_stale_target_error(result: dict[str, Any]) -> bool:
    """True when the extension says the aimed-at element is gone."""
    err = (result or {}).get("error") or {}
    if not isinstance(err, dict):
        return False
    return str(err.get("code") or "") in _STALE_ERROR_CODES


def is_dialog_error(result: dict[str, Any]) -> bool:
    """True when the extension says a popup/dialog blocked the step."""
    err = (result or {}).get("error") or {}
    if not isinstance(err, dict):
        return False
    return str(err.get("code") or "") in _DIALOG_ERROR_CODES


def resolve_fresh_ref(
    plan_node: Any,
    fresh_tree: AxTree,
    *,
    target: Optional[dict[str, Any]] = None,
) -> tuple[Optional[str], Optional[AxNode]]:
    """Find the plan-time control again in a fresh AX tree.

    Returns ``(fresh_ref, fresh_node)`` or ``(None, None)``. The
    match order is deliberate:

    1. Same stable target id under a possibly new ref — a re-render
       that re-assigns refs but keeps the element's identity.
    2. Same role + normalised accessible name, *unambiguous* (exactly
       one candidate) — a re-render that re-assigns the target id too.

    Anything ambiguous returns nothing: guessing between two
    same-named controls is how the agent clicks the wrong element.
    A node without a name (an icon-only button) can only match by
    target id, never by name.
    """
    if fresh_tree is None:
        return None, None
    nodes = list(getattr(fresh_tree, "nodes", None) or [])

    plan_target_id = None
    plan_role = ""
    plan_name = ""
    if plan_node is not None:
        plan_target_id = getattr(plan_node, "target_id", None)
        plan_role = str(getattr(plan_node, "role", "") or "")
        plan_name = str(getattr(plan_node, "name", "") or "")
    if target:
        plan_target_id = plan_target_id or target.get("targetId")
        plan_role = plan_role or str(target.get("role") or "")
        plan_name = plan_name or str(target.get("name") or "")

    if plan_target_id:
        for node in nodes:
            if node.target_id and str(node.target_id) == str(plan_target_id):
                return node.ref, node

    if plan_name:
        wanted = normalize_accessible_name(plan_name).casefold()
        matches = [
            node for node in nodes
            if (node.role or "").casefold() == plan_role.casefold()
            and normalize_accessible_name(str(node.name or "")).casefold() == wanted
        ]
        if len(matches) == 1:
            return matches[0].ref, matches[0]

    return None, None


__all__ = [
    "detect_dialog",
    "is_dialog_error",
    "is_stale_target_error",
    "outcome_signals",
    "resolve_fresh_ref",
    "verify_step_outcome",
]
