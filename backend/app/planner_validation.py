"""Plan normalization + validation (Task 13).

Split out of :mod:`app.planner` (which re-exports every name here,
so existing imports keep working). This module is the hard gate:
a provider answer is normalized into the plan schema and then
validated — refs must resolve against the observation, element
actions need a grounded target, OTP / login / payment surfaces get
their extra rules — and an invalid answer raises instead of ever
being executed. It never calls the provider and never builds a
prompt; :mod:`app.planner` keeps prompt building and provider
dispatch, and imports :func:`_validate_plan` from here.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional
from urllib.parse import urlsplit

from pydantic import ValidationError

from .contracts import ACTION_TYPES, BrowserAction, PlannedStep, TaskPlan

__all__ = [
    "MAX_STEPS_PER_PLAN",
    "MAX_TIMEOUT_MS",
    "MIN_TIMEOUT_MS",
    "_NAV_DIRECTIONS",
    "_SCROLL_DIRECTIONS",
    "_ZOOM_DIRECTIONS",
    "_normalize_raw_plan",
    "_validate_plan",
    "_validate_step",
]


MAX_STEPS_PER_PLAN = 3
MIN_TIMEOUT_MS = 100
MAX_TIMEOUT_MS = 60_000
_SCROLL_DIRECTIONS = {"up", "down", "top", "bottom"}
_ZOOM_DIRECTIONS = {"in", "out", "reset"}
_NAV_DIRECTIONS = {"back", "forward", "reload"}


def _normalize_raw_plan(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return raw
    data = dict(raw)
    if "answer" not in data and "summary" in data:
        data["answer"] = data["summary"]
    if "notes" in data and not isinstance(data["notes"], (str, type(None))):
        data["notes"] = str(data["notes"])
    steps = data.get("steps")
    if not isinstance(steps, list):
        return data
    norm_steps = []
    for step in steps:
        if not isinstance(step, dict):
            norm_steps.append(step)
            continue
        s = dict(step)
        action = s.get("action")
        if isinstance(action, dict):
            act = dict(action)
            atype = act.get("type")
            risk = act.get("risk", "read")
            if atype in {"type", "click", "navigate", "press", "new_tab", "close_tab"} and risk == "read":
                act["risk"] = "write"

            # 1. Extract params
            params = dict(act.get("params", {})) if isinstance(act.get("params"), dict) else {}
            for field in ("url", "direction", "text", "clear", "submit", "key", "containerId", "tabId", "factor"):
                if field in act and field not in params:
                    params[field] = act.pop(field)

            # 2. Extract and normalize target from any location
            target = act.get("target")
            if target is None:
                if "target" in params:
                    target = params.pop("target")
                elif "targetId" in act:
                    target = {"targetId": act.pop("targetId")}
                elif "target_id" in act:
                    target = {"targetId": act.pop("target_id")}
                elif "name" in act:
                    target = {"name": act.pop("name")}
                elif "id" in act and act["id"] not in {"click", "type", "navigate", "wait", "press"}:
                    target = {"targetId": act.pop("id")}
                elif "targetId" in params:
                    target = {"targetId": params.pop("targetId")}
                elif "target_id" in params:
                    target = {"targetId": params.pop("target_id")}
                elif "name" in params and atype in {"click", "type"}:
                    target = {"name": params.pop("name")}

            if isinstance(target, str):
                act["target"] = {"targetId": target}
            elif isinstance(target, dict):
                t = dict(target)
                if "id" in t and "targetId" not in t:
                    t["targetId"] = t.pop("id")
                if "target_id" in t and "targetId" not in t:
                    t["targetId"] = t.pop("target_id")
                if "label" in t and "name" not in t:
                    t["name"] = t.pop("label")
                elif "title" in t and "name" not in t:
                    t["name"] = t.pop("title")
                elif "text" in t and "name" not in t:
                    t["name"] = t.pop("text")
                elif "selector" in t and "name" not in t:
                    t["name"] = t.pop("selector")
                act["target"] = t
            elif target is not None:
                act["target"] = {"targetId": str(target)}

            # 3. LLMs routinely emit {"type": "press", "target": {...}} to mean
            # "activate this control" (a thumbnail, a button). `press` is only
            # a physical key event, so reinterpret the intent instead of
            # failing the whole plan:
            #   press + target, no key -> click that target
            #   press with neither     -> Enter (the universal default)
            if act.get("type") == "press" and not _has_key(params.get("key")):
                if _has_target_ref(act.get("target")):
                    act["type"] = "click"
                    atype = "click"
                    # A click changes page state; never leave it below the floor.
                    if act.get("risk") == "read":
                        act["risk"] = "write"
                else:
                    params["key"] = "Enter"

            # 4. Fallback for click or type when the target is absent or empty.
            #    Models omit targets more often than they invent bad ones, so
            #    recover a usable label from every plausible field instead of
            #    rejecting the whole plan (which used to kill the mission on
            #    step 1).
            if act.get("type") in {"click", "type"} and not _has_target_ref(act.get("target")):
                candidate_name = None
                for source in (
                    params.get("name"),
                    params.get("label"),
                    params.get("selector"),
                    params.get("query"),
                    params.get("text") if atype == "click" else None,
                    act.get("text") if atype == "click" else None,
                    act.get("label"),
                    act.get("selector"),
                    act.get("name"),
                ):
                    if not isinstance(source, str):
                        continue
                    text = source.strip()
                    # Selectors are only useful as a fallback name when they
                    # quote a human label ("[...='Mummy']"); otherwise the
                    # raw selector would never match an accessible name.
                    if text:
                        candidate_name = text
                        break
                if candidate_name is None:
                    # `why` lives on the STEP, not inside the action. This was
                    # the actual reason targetless clicks were rejected.
                    for prose in (s.get("why"), act.get("why"), act.get("reason")):
                        if not isinstance(prose, str):
                            continue
                        quoted = _quoted_name(prose)
                        if quoted:
                            candidate_name = quoted
                            break

                if candidate_name and candidate_name.strip():
                    act["target"] = {"name": candidate_name.strip()}
                elif act.get("type") == "type":
                    act["target"] = {"name": "activeElement"}

            act["params"] = params

            # 5. Sanitize timeoutMs: default when missing or 0
            raw_timeout = act.get("timeoutMs") if "timeoutMs" in act else act.get("timeout_ms")
            if raw_timeout is None or raw_timeout == 0 or raw_timeout == "" or raw_timeout is False:
                act["timeoutMs"] = 10000
            else:
                try:
                    act["timeoutMs"] = int(raw_timeout)
                except (TypeError, ValueError):
                    act["timeoutMs"] = 10000

            if "verify" not in act or not act["verify"]:
                act["verify"] = f"executed {act.get('type') or atype}"
            s["action"] = act
        norm_steps.append(s)
    # Recovery plans sometimes overshoot the per-plan budget. Executing the
    # first MAX_STEPS_PER_PLAN steps is strictly better than aborting the run
    # with PLAN_INVALID; the loop still enforces MAX_STEPS globally.
    data["steps"] = norm_steps[:MAX_STEPS_PER_PLAN]
    return data


def _has_key(value: Any) -> bool:
    """A usable keyboard key: a non-blank string."""
    return isinstance(value, str) and bool(value.strip())


def _quoted_name(text: str) -> Optional[str]:
    """First quoted fragment in prose, e.g. 'Click "Mummy" in the list'."""
    for quote in ('"', "'", "“", "‘"):
        close = {'"': '"', "'": "'", "“": "”", "‘": "’"}[quote]
        start = text.find(quote)
        if start == -1:
            continue
        end = text.find(close, start + 1)
        if end > start + 1:
            return text[start + 1:end].strip()
    return None


def _has_target_ref(target: Any) -> bool:
    """True when the action points at something clickable."""
    if not isinstance(target, dict):
        return False
    coords = target.get("coords")
    return bool(
        target.get("targetId")
        or target.get("elementRef")
        or target.get("element_ref")
        or target.get("name")
        or (
            isinstance(coords, dict)
            and coords.get("x") is not None
            and coords.get("y") is not None
        )
    )


def _safe_nav_url(url: Optional[str]) -> bool:
    if not url:
        return False
    if any(ch in url for ch in "\r\n\t\\"):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return False
    # Never navigate with embedded credentials (they would be leaked to the
    # remote site and to every later URL snapshot).
    if parts.username or parts.password:
        return False
    return True


def _require_target(action: Any) -> None:
    target = action.target or {}
    if not (target.get("targetId") or target.get("name") or target.get("coords")
            or target.get("elementRef") or target.get("element_ref")):
        raise ValueError(
            f"PLAN_INVALID: action {action.type!r} requires a target "
            "(elementRef, targetId, name, or coords)"
        )


_CAPTCHA_MARKERS = (
    "recaptcha",
    "hcaptcha",
    "cf-turnstile",
    "turnstile",
    "i'm not a robot",
)
_LOGIN_SEGMENTS = {"login", "signin", "sign-in", "sign_in", "log-in", "logon", "auth/login"}
_LOGIN_HOSTS = ("accounts.google.com", "login.microsoftonline.com", "login.live.com")
_SEND_INTENT_RE = re.compile(r"\b(send|sending|message|text|dm|reply)\b", re.IGNORECASE)
_SUCCESS_WORD_RE = re.compile(r"\b(sent|delivered|successfully|success)\b", re.IGNORECASE)
_FAILURE_WORD_RE = re.compile(
    r"\b(not|never|unable|cannot|can't|couldn't|failed|blocked|incomplete)\b", re.IGNORECASE
)
_HANDOFF_VERIFY = "waiting for user to complete CAPTCHA and log in"
_HANDOFF_REASON = "Please solve the CAPTCHA / sign in manually in your browser, then click Continue."


def _payload_url(payload: Any) -> str:
    if payload is None:
        return ""
    url = getattr(payload, "url", None)
    if url is None and isinstance(payload, dict):
        url = payload.get("url")
    return str(url or "")


def _payload_interactive(payload: Any) -> list[Any]:
    els = getattr(payload, "interactive", None)
    if els is None and isinstance(payload, dict):
        els = payload.get("interactive")
    return list(els or [])


def _el_field(el: Any, name: str, default: Any = None) -> Any:
    if isinstance(el, dict):
        return el.get(name, default)
    attr = "target_id" if name == "targetId" else ("input_type" if name == "inputType" else name)
    return getattr(el, attr, default)


def _enrich_targets_from_role(norm: dict[str, Any], payload: Any) -> dict[str, Any]:
    """Resolve a role-only target when the page has exactly one match.

    Models increasingly emit ``{"target": {"role": "button"}}`` with no name.
    Guessing between several matches is worse than replanning, but a single
    unambiguous element can be resolved deterministically here.
    """
    els = _payload_interactive(payload)
    steps = norm.get("steps")
    if not els or not isinstance(steps, list):
        return norm
    for step in steps:
        if not isinstance(step, dict):
            continue
        act = step.get("action")
        if not isinstance(act, dict) or act.get("type") not in {"click", "type"}:
            continue
        target = act.get("target")
        if not isinstance(target, dict) or _has_target_ref(target):
            continue
        role = str(target.get("role") or "").strip().lower()
        if not role:
            continue
        matches = [
            el for el in els
            if str(_el_field(el, "role", "") or "").strip().lower() == role
            and _el_field(el, "visible", True)
            and _el_field(el, "enabled", True)
        ]
        if len(matches) != 1:
            continue
        el = matches[0]
        tid = _el_field(el, "targetId")
        name = _el_field(el, "name")
        resolved = dict(target)
        if tid:
            resolved["targetId"] = tid
        if name:
            resolved["name"] = name
        act["target"] = resolved
    return norm


def _element_text(el: Any) -> str:
    if isinstance(el, dict):
        parts = [el.get("name"), el.get("inputType"), el.get("role"), el.get("tag")]
    else:
        parts = [
            getattr(el, "name", None),
            getattr(el, "input_type", None),
            getattr(el, "role", None),
            getattr(el, "tag", None),
        ]
    return " ".join(str(p).lower() for p in parts if p)


def _has_captcha(payload: Any) -> bool:
    for el in _payload_interactive(payload):
        text = _element_text(el)
        if any(marker in text for marker in _CAPTCHA_MARKERS):
            return True
    return False


def _has_otp_field(payload: Any) -> bool:
    """True when the page shows a one-time-code / OTP input.

    Task 4: an OTP field is the most sensitive blocker — the agent must
    never see, type, or relay a one-time code, so it becomes a human
    blocker (takeover) before CAPTCHA / login are even considered.
    """
    from .takeover import is_otp_field

    return any(is_otp_field(el) for el in _payload_interactive(payload))


def _is_login_surface(url: str) -> bool:
    """True for a page that still looks like a sign-in gate.

    Substring matching is deliberately avoided for the path (``/login-help`` is
    not a login page); the explicit portal markers are matched on the full URL
    because they are unambiguous.
    """
    if not url:
        return False
    low = url.lower()
    if "loginrequired" in low or "msg=login" in low:
        return True
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if any(host == h or host.endswith("." + h) for h in _LOGIN_HOSTS):
        return True
    path = (parts.path or "").lower().rstrip("/")
    if path.endswith("default.aspx"):
        return True
    segments = {s for s in re.split(r"[/;]+", path) if s}
    return any(s in _LOGIN_SEGMENTS or s.startswith("login.") or s.startswith("signin.") for s in segments)


def _host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _claims_message_sent(answer: Optional[str]) -> bool:
    text = answer or ""
    return bool(_SUCCESS_WORD_RE.search(text)) and not _FAILURE_WORD_RE.search(text)


def _missing_chat_composer(
    payload: Any, history: list[dict[str, Any]], instruction: str, answer: Optional[str]
) -> bool:
    """A messaging completion claim with no open chat / composer.

    Task 8: this used to fire only on a hard-coded list of messaging
    hosts. The guard is generic — a claimed send with no successful
    ``type`` and no composer visible cannot be a real delivery on ANY
    site — so the host list is gone and the check runs everywhere.
    """
    if not _SEND_INTENT_RE.search(instruction or ""):
        return False
    if not _claims_message_sent(answer):
        return False
    if any("type a message" in _element_text(el) or "composer" in _element_text(el)
           for el in _payload_interactive(payload)):
        return False
    return not any(
        h.get("result_ok") and (h.get("action") or {}).get("type") == "type"
        for h in history
    )


def _handoff_recently(history: list[dict[str, Any]]) -> bool:
    """True when a human handoff already asked the operator for help."""
    for entry in history[-3:]:
        action = entry.get("action") or {}
        if action.get("type") == "wait" and action.get("risk") == "sensitive":
            return True
    return False


def _handoff_plan(goal: str, notes: Optional[str], reason: str) -> TaskPlan:
    wait = BrowserAction(
        type="wait",
        risk="sensitive",
        verify=_HANDOFF_VERIFY,
        timeout_ms=30000,
    )
    return TaskPlan(
        goal=goal,
        notes=notes,
        steps=[PlannedStep(why=f"{reason}. {_HANDOFF_REASON}", action=wait)],
    )


def _honest_failure(goal: str, notes: Optional[str], reason: str) -> TaskPlan:
    return TaskPlan(
        goal=goal,
        notes=notes,
        steps=[],
        answer=(
            f"Task incomplete: {reason}. I stopped instead of claiming success — "
            "check the page in your browser for the actual state."
        ),
        blocked=True,
    )


_SEARCH_INTENT_RE = re.compile(r"\b(search|find|buy|look for|specs|price|results)\b", re.IGNORECASE)


def _uncompleted_search_recovery(
    payload: Any, history: list[dict[str, Any]], instruction: str
) -> Optional[PlannedStep]:
    """Recover a search the model typed but never submitted.

    Task 8: this used to fire only on a hard-coded list of search hosts.
    The condition is generic — a search intent, text already typed, and
    a URL that shows no search/results state — so the host list is gone
    and recovery runs on any site.
    """
    if not _SEARCH_INTENT_RE.search(instruction or ""):
        return None
    url = _payload_url(payload)
    if not url:
        return None
    try:
        parts = urlsplit(url)
        path = (parts.path or "").rstrip("/").lower()
        query = (parts.query or "").lower()
    except ValueError:
        path, query = "", ""
    # If the URL already carries search/results state, results are loaded
    # or loading. Query parameters are generic search signals (q, query,
    # search, k, keyword); path segments are generic result-page markers.
    if any(seg in path for seg in ("/search", "results", "/s", "watch")):
        return None
    if any(key in query for key in ("q=", "query=", "search=", "k=", "keyword=")):
        return None
    typed_in_history = any(
        h.get("result_ok") and (h.get("action") or {}).get("type") == "type"
        for h in history
    )
    if not typed_in_history:
        return None

    # Look for a search submit button on the page
    for el in _payload_interactive(payload):
        role = str(_el_field(el, "role", "") or "").lower()
        name = str(_el_field(el, "name", "") or "").lower()
        if _el_field(el, "visible", True) and _el_field(el, "enabled", True):
            if (role in ("button", "submit") or _el_field(el, "tag", "") == "button") and (
                "search" in name or name == "search"
            ):
                tid = _el_field(el, "targetId")
                tname = _el_field(el, "name")
                return PlannedStep(
                    why="Submitting search form to load search results",
                    action=BrowserAction(
                        type="click",
                        target={"targetId": tid, "name": tname},
                        risk="write",
                        verify="search results loaded",
                        timeout_ms=10000,
                    ),
                )
    host = _host_of(url)
    return PlannedStep(
        why=f"Submitting search on {host or 'this page'} via Enter to load search results",
        action=BrowserAction(
            type="press",
            params={"key": "Enter"},
            risk="write",
            verify="search results loaded",
            timeout_ms=10000,
        ),
    )


_EXTRACTION_INTENT_RE = re.compile(
    r"\b(extract|paragraphs?|specifications?|specs?|price|details?|read article)\b",
    re.IGNORECASE,
)


def _uncompleted_extraction(
    payload: Any, history: list[dict[str, Any]], instruction: str, answer: Optional[str]
) -> bool:
    if not _EXTRACTION_INTENT_RE.search(instruction or ""):
        return False
    url = _payload_url(payload)
    if not url or url == "about:blank":
        return True
    try:
        path = (urlsplit(url).path or "").rstrip("/").lower()
        query = (urlsplit(url).query or "").lower()
    except ValueError:
        path, query = "", ""
    is_search_page = (
        "search" in path
        or "search=" in query
        or path.endswith("/s")
        or "/s" in path
        or path in ("", "/")
    )
    if is_search_page:
        has_nav = any(
            h.get("result_ok")
            and "navigated" in str(h.get("evidence") or "")
            and not any(s in str(h.get("evidence") or "") for s in ("/search", "index.php?search"))
            for h in history
        )
        if not has_nav:
            return True
    return False


def _detect_blocker(
    payload: Any, history: list[dict[str, Any]], instruction: str, answer: Optional[str]
) -> Optional[tuple[str, str]]:
    """Classify why a completion claim cannot be trusted.

    ``human`` blockers can be resolved by the operator (CAPTCHA / sign-in), so
    the first one becomes a single sensitive wait. Everything else is reported
    as an honest failure.
    """
    if _has_otp_field(payload):
        return ("human", "a one-time code / OTP field is on the page — the operator must enter the code themselves")
    if _has_captcha(payload):
        return ("human", "a CAPTCHA is blocking the page")
    if _is_login_surface(_payload_url(payload)):
        return ("human", "the page is still a sign-in/login screen")
    try:
        from .auth_validator import verify_authentication_state
        is_authed, auth_reason = verify_authentication_state(payload)
        if not is_authed and any(w in (instruction or "").lower() for w in ("sign in", "login", "profile", "account", "my profile")):
            return ("human", auth_reason)
    except Exception:
        pass
    if _missing_chat_composer(payload, history, instruction, answer):
        return ("failed", "no chat conversation is open, so the message was never sent")
    if _uncompleted_extraction(payload, history, instruction, answer):
        return ("failed", "the requested page or article was not opened; cannot claim extraction from search results")
    if history and history[-1].get("result_ok") is False:
        action = history[-1].get("action") or {}
        return ("failed", f"the last action ({action.get('type') or 'unknown'}) failed")
    return None


def _guard_multi_site_completion(
    plan: TaskPlan,
    payload: Any,
    history: list[dict[str, Any]],
    instruction: str,
    working_memory: Optional[dict[str, Any]] = None,
) -> Optional[TaskPlan]:
    from .extractors.research import (
        init_multi_site_tracker,
        is_multi_site_task_complete,
        get_pending_sites,
        get_completed_sites,
        get_unavailable_sites,
        format_comparison_table,
        claims_cheapest_on_single_listing,
        is_multi_site_instruction,
        extract_target_product_query,
    )
    if not is_multi_site_instruction(instruction):
        return None

    tracker = (working_memory or {}).get("multi_site_research")
    if not tracker:
        tracker = init_multi_site_tracker(instruction)
        if working_memory is not None and tracker:
            working_memory["multi_site_research"] = tracker

    if not tracker:
        return None

    curr_url = getattr(payload, "url", "") or ""

    # Check if all sites are resolved (completed or unavailable)
    if not is_multi_site_task_complete(tracker):
        pending = get_pending_sites(tracker)
        completed = get_completed_sites(tracker)
        unavailable = get_unavailable_sites(tracker)

        if pending:
            next_site = pending[0]
            # Hostname match, not substring: a Google search URL that
            # merely mentions "flipkart.com" in its query string is
            # not "on Flipkart".
            curr_host = (urlsplit(curr_url).hostname or "").lower() if curr_url else ""
            site_domains = [str(d).lower() for d in next_site.get("domains", [])]

            def _on_site(url: str) -> bool:
                try:
                    host = (urlsplit(str(url or "")).hostname or "").lower()
                except Exception:
                    return False
                return bool(host) and any(host == d or host.endswith("." + d) for d in site_domains)

            is_on_next_site = bool(curr_host) and any(
                curr_host == d or curr_host.endswith("." + d) for d in site_domains
            )

            # If we are ALREADY on the next_site, do NOT navigate to its homepage again!
            if is_on_next_site:
                # 1. Look for search box if on homepage or search page
                interactive = getattr(payload, "interactive", []) or []
                search_el = None
                for el in interactive:
                    name = str(getattr(el, "name", "") or "").lower()
                    role = str(getattr(el, "role", "") or "").lower()
                    tag = str(getattr(el, "tag", "") or "").lower()
                    if role in ("searchbox", "combobox") or "search" in name or (tag == "input" and "search" in str(getattr(el, "css_selector", "")).lower()):
                        search_el = el
                        break

                query = extract_target_product_query(instruction)
                # Count only genuine search attempts on this site: a
                # navigate to it, or a search submission (type). A
                # plain click — e.g. closing a login popup — is NOT a
                # search attempt: counting it consumed the two
                # attempts the guard allows and marked the site
                # "unavailable" without ever searching it (live
                # Flipkart pattern: navigate + popup-close click).
                recent_actions_on_site = sum(
                    1 for h in history[-2:]
                    if _on_site((h.get("action") or {}).get("params", {}).get("url", ""))
                    or (h.get("action") or {}).get("type") == "type"
                )

                if search_el and query and recent_actions_on_site < 2:
                    return TaskPlan(
                        goal=f"Search for {query} on {next_site['name']}",
                        notes=plan.notes,
                        steps=[
                            PlannedStep(
                                why=f"On {next_site['name']}: searching for '{query}'.",
                                action=BrowserAction(
                                    type="type",
                                    risk="write",
                                    target={"targetId": getattr(search_el, "target_id", None) or getattr(search_el, "targetId", "search"), "name": getattr(search_el, "name", "Search")},
                                    params={"text": query, "submit": True},
                                    verify=f"searched for {query}",
                                    timeout_ms=15000,
                                ),
                            )
                        ],
                        answer=None,
                        blocked=False,
                    )
                else:
                    # Site search failed or repeated without product found -> mark unavailable and advance
                    next_site["status"] = "unavailable"
                    next_site["reason"] = f"Product listing not found on {next_site['name']}"
                    remaining_pending = get_pending_sites(tracker)
                    if remaining_pending:
                        rem_site = remaining_pending[0]
                        return TaskPlan(
                            goal=f"Compare on {rem_site['name']}",
                            notes=plan.notes,
                            steps=[
                                PlannedStep(
                                    why=f"{next_site['name']} search produced no listings. Transitioning to {rem_site['name']} ({rem_site['default_url']}).",
                                    action=BrowserAction(
                                        type="navigate",
                                        risk="write",
                                        params={"url": rem_site["default_url"]},
                                        verify=f"navigated to {rem_site['name']}",
                                        timeout_ms=15000,
                                    ),
                                )
                            ],
                            answer=None,
                            blocked=False,
                        )

            # Not on next_site: navigate to its default URL
            recent_nav_to_next = sum(
                1 for h in history[-3:]
                if (h.get("action") or {}).get("type") == "navigate"
                and any(d in str((h.get("action") or {}).get("params", {}).get("url", "")) for d in next_site.get("domains", []))
            )
            if recent_nav_to_next >= 2:
                next_site["status"] = "unavailable"
                next_site["reason"] = f"Navigation or search on {next_site['name']} failed"
                remaining_pending = get_pending_sites(tracker)
                if remaining_pending:
                    rem_site = remaining_pending[0]
                    return TaskPlan(
                        goal=f"Compare on {rem_site['name']}",
                        notes=plan.notes,
                        steps=[
                            PlannedStep(
                                why=f"Moving to next comparison site: navigating to {rem_site['name']} ({rem_site['default_url']}).",
                                action=BrowserAction(
                                    type="navigate",
                                    risk="write",
                                    params={"url": rem_site["default_url"]},
                                    verify=f"navigated to {rem_site['name']}",
                                    timeout_ms=15000,
                                ),
                            )
                        ],
                        answer=None,
                        blocked=False,
                    )
            else:
                return TaskPlan(
                    goal=f"Compare on {next_site['name']}",
                    notes=plan.notes,
                    steps=[
                        PlannedStep(
                            why=f"Multi-site comparison in progress: {next_site['name']} has not been visited yet. Navigating to {next_site['default_url']} to inspect prices and specs.",
                            action=BrowserAction(
                                type="navigate",
                                risk="write",
                                params={"url": next_site["default_url"]},
                                verify=f"navigated to {next_site['name']}",
                                timeout_ms=15000,
                            ),
                        )
                    ],
                    answer=None,
                    blocked=False,
                )

        pending_names = ", ".join(s["name"] for s in pending)
        resolved_count = len(completed) + len(unavailable)
        total_count = len(tracker.get("required_sites", {}))
        return _honest_failure(
            plan.goal,
            plan.notes,
            f"multi-site comparison incomplete; {len(pending)} requested websites were not visited ({pending_names}). Only {resolved_count} of {total_count} resolved",
        )

    # All requested sites have been visited or marked unavailable!
    completed = get_completed_sites(tracker)
    comp_table = format_comparison_table(tracker, instruction)
    ans = plan.answer or ""

    if claims_cheapest_on_single_listing(ans, len(completed)):
        single_site = completed[0]["name"] if completed else "Unknown"
        single_price = completed[0]["data"].get("price", "") if completed and completed[0].get("data") else ""
        ans = (
            f"Comparison completed: {single_site} has a verified listing at {single_price}. "
            "A comparative cheapest option cannot be determined because only one retailer listing was available.\n\n"
            f"{comp_table}"
        )
    elif comp_table and "Price & Availability Comparison" not in ans:
        ans = f"{ans}\n\n{comp_table}" if ans else comp_table

    return TaskPlan(
        goal=plan.goal,
        notes=plan.notes,
        steps=[],
        answer=ans,
        blocked=False,
    )


def _guard_completion(
    plan: TaskPlan,
    payload: Any,
    history: list[dict[str, Any]],
    instruction: str,
    working_memory: Optional[dict[str, Any]] = None,
) -> TaskPlan:
    """Deterministic backstop for the prompt's completion rules.

    The model is instructed not to claim unverifiable success, but a single bad
    completion ends the mission with a false "Mission Completed". This only
    rewrites plans that claim completion (steps == []) while the observation or
    history proves the task is blocked.
    """
    if plan.steps:
        return plan
    search_recovery = _uncompleted_search_recovery(payload, history, instruction)
    if search_recovery is not None:
        recovery_attempts = sum(
            1 for h in history[-2:]
            if "Submitting search" in str((h.get("action") or {}).get("verify") or "")
        )
        if recovery_attempts < 2:
            return TaskPlan(
                goal=plan.goal,
                notes=plan.notes,
                steps=[search_recovery],
            )
    ms_plan = _guard_multi_site_completion(plan, payload, history, instruction, working_memory)
    if ms_plan is not None:
        return ms_plan
    blocker = _detect_blocker(payload, history, instruction, plan.answer)
    if blocker is None:
        return plan
    kind, reason = blocker
    if kind == "human" and not _handoff_recently(history):
        return _handoff_plan(plan.goal, plan.notes, reason)
    return _honest_failure(plan.goal, plan.notes, reason)


def _validate_step(
    step: PlannedStep,
    working_memory: Optional[dict[str, Any]] = None,
    allowed_domains: Optional[list[str]] = None,
    valid_refs: Optional[set[str]] = None,
    name_counts: Optional[dict[str, int]] = None,
    name_role_counts: Optional[dict[tuple[str, str], int]] = None,
) -> None:
    """Raise PLAN_INVALID for one structurally invalid action.

    ``valid_refs`` is the set of element refs the current observation
    actually contains (Task 7). When it is None (no observation — e.g.
    a unit test driving validation directly) ref checks are skipped;
    when it is a set, a quoted ref must be in it. ``name_counts`` maps
    normalised accessible names to how many tree nodes share them;
    when it is given, a name-only target for a duplicated name is
    rejected as ambiguous (Moodle fix) instead of letting the executor
    guess which same-named element was meant. ``name_role_counts``
    counts (name, role) pairs the same way, so a role that narrows a
    duplicated name to one usable element is not a false rejection.
    """
    if not step.why.strip():
        raise ValueError("PLAN_INVALID: every step needs a non-empty 'why'")
    a = step.action
    if a.type not in ACTION_TYPES:
        raise ValueError(f"PLAN_INVALID: action type {a.type!r} outside whitelist")
    if a.risk == "read" and a.type in {"type", "click", "navigate", "press", "new_tab", "close_tab", "select_option", "set_date", "upload_file", "webmcp_tool", "ucp_checkout"}:
        # floor breach: these change page/task state at minimum
        raise ValueError(f"PLAN_INVALID: action {a.type} cannot be risk=read")
    if not (MIN_TIMEOUT_MS <= a.timeout_ms <= MAX_TIMEOUT_MS):
        raise ValueError(
            f"PLAN_INVALID: timeoutMs {a.timeout_ms} outside "
            f"[{MIN_TIMEOUT_MS}, {MAX_TIMEOUT_MS}]"
        )

    p = a.params
    if a.type in {"click", "type", "select_option", "set_date", "upload_file"}:
        # Task 7: grounding rules for element-addressing actions. These
        # run BEFORE _require_target so a raw CSS selector is rejected
        # with its own reason instead of masquerading as "no target".
        # The planner must quote a ref from the current observation's
        # accessibility tree; a raw CSS selector is never an address.
        _target = a.target if isinstance(a.target, dict) else {}
        _css = (
            _target.get("cssSelector") or _target.get("css_selector")
            or _target.get("selector")
        )
        if _css:
            raise ValueError(
                "PLAN_INVALID: target carries a raw CSS selector — quote "
                "the element ref from <accessibility-tree> instead"
            )
        _tname = str(_target.get("name") or "")
        if _tname:
            from .grounding import selector_like

            if selector_like(_tname):
                raise ValueError(
                    "PLAN_INVALID: target name looks like a raw CSS selector "
                    f"({_tname!r}) — quote the element ref instead"
                )
        _require_target(a)
        _ref = _target.get("elementRef") or _target.get("element_ref")
        if _ref:
            from .grounding import is_valid_ref

            if not is_valid_ref(_ref):
                raise ValueError(f"PLAN_INVALID: malformed element ref {_ref!r}")
            if valid_refs is not None and _ref not in valid_refs:
                raise ValueError(
                    f"PLAN_INVALID: unknown element ref {_ref!r} — it is not "
                    "in this observation's accessibility tree; the model "
                    "must not invent refs"
                )
        if not _ref and _tname and name_counts is not None:
            # Moodle fix: a name-only target is ambiguous when several
            # usable elements in this observation share the name (e.g.
            # "I-SEM" eight times on a Moodle course list, or a course
            # title on both its image link and its title link).
            # Clicking the first match would be a guess; the planner
            # must quote the element ref from <accessibility-tree>
            # instead. Two things do disambiguate, and the extension
            # resolves them before name matching, so they are not
            # rejected here: an author targetId (stable identity), and
            # a role that narrows the name to exactly one usable node
            # (Moodle's search textbox and search button share the
            # name "Search courses" but have different roles). A role
            # that matches NONE of the duplicates is not a
            # disambiguator — the name is still ambiguous.
            from .grounding import normalize_accessible_name

            if _target.get("targetId") or _target.get("target_id"):
                pass
            else:
                _norm_name = normalize_accessible_name(_tname).casefold()
                _count = name_counts.get(_norm_name, 0)
                _role = str(_target.get("role") or "").casefold()
                if _role and name_role_counts is not None:
                    _role_count = name_role_counts.get((_norm_name, _role), 0)
                    if _role_count == 1:
                        _count = 1
                    elif _role_count > 1:
                        _count = _role_count
                if _count > 1:
                    raise ValueError(
                        f"PLAN_INVALID: ambiguous target name {_tname!r} — "
                        f"{_count} elements share that name in this "
                        "observation; quote the element ref (e.g. e12) from "
                        "<accessibility-tree> instead of the bare name"
                    )
    if a.type == "type":
        if p is None or p.text is None:
            raise ValueError("PLAN_INVALID: type action requires params.text")
        if p.text:
            # Task 4: the model must never invent a credential placeholder
            # and must never type a card number. A placeholder is valid
            # only if the vault actually holds that credential; an
            # unknown or malformed one is rejected here, and the loop
            # fails closed again at substitution time.
            if "{{CREDENTIAL:" in p.text:
                from .vault import parse_placeholder, placeholder_value

                _remaining = p.text
                _found = False
                while True:
                    _parsed = parse_placeholder(_remaining)
                    if _parsed is None:
                        break
                    _found = True
                    try:
                        placeholder_value(*_parsed)
                    except KeyError:
                        raise ValueError(
                            "PLAN_INVALID: unknown credential placeholder — the model must not invent vault references"
                        ) from None
                    _remaining = _remaining.replace(
                        "{{CREDENTIAL:%s:%s:%s}}" % _parsed, "", 1
                    )
                if not _found:
                    raise ValueError("PLAN_INVALID: malformed credential placeholder")
            if re.search(r"\b(?:\d[ -]?){13,19}\b", p.text):
                raise ValueError("PLAN_INVALID: type action must never contain a card number")
    elif a.type == "select_option":
        if p is None or not p.option or len(p.option) > 300:
            raise ValueError("PLAN_INVALID: select_option requires params.option")
    elif a.type == "set_date":
        import datetime
        try:
            if p is None or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.date or ""):
                raise ValueError("date must be ISO")
            datetime.date.fromisoformat(p.date)
        except (TypeError, ValueError):
            raise ValueError("PLAN_INVALID: set_date requires YYYY-MM-DD") from None
    elif a.type == "upload_file":
        if a.risk != "sensitive" or (p is not None and (p.file_name or p.base64 or p.file_token)):
            raise ValueError("PLAN_INVALID: upload_file needs a sensitive gate; file details are forbidden in plans")
    elif a.type == "navigate":
        if p is None or not (p.url or p.direction):
            raise ValueError("PLAN_INVALID: navigate needs params.url or params.direction")
        if p.direction is not None and p.direction not in _NAV_DIRECTIONS:
            raise ValueError(f"PLAN_INVALID: unknown navigate direction {p.direction!r}")
        if p.url is not None and not _safe_nav_url(p.url):
            raise ValueError(f"PLAN_INVALID: unsafe navigate url {p.url!r}")
        # Task 3: the domain allow-list is enforced in code, not in the
        # prompt. A plan that navigates outside it is invalid, full stop.
        if p.url is not None and allowed_domains:
            from .allowlist import url_allowed
            if not url_allowed(p.url, allowed_domains):
                raise ValueError(
                    f"PLAN_INVALID: navigate url {p.url!r} is not on the domain allow-list"
                )
    elif a.type == "press":
        if p is None or not (p.key or "").strip():
            raise ValueError("PLAN_INVALID: press action requires params.key")
    elif a.type == "scroll":
        if p is None or p.direction not in _SCROLL_DIRECTIONS:
            raise ValueError("PLAN_INVALID: scroll requires a valid params.direction")
    elif a.type == "zoom":
        if p is None:
            raise ValueError("PLAN_INVALID: zoom requires params")
        if p.direction not in _ZOOM_DIRECTIONS and p.factor is None:
            raise ValueError("PLAN_INVALID: zoom requires direction or factor")
        if p.factor is not None and not (0.25 <= p.factor <= 3.0):
            raise ValueError("PLAN_INVALID: zoom factor must be between 0.25 and 3.0")
    elif a.type in {"select_tab", "switch_tab", "close_tab"}:
        if p is None or p.tab_id is None or p.tab_id < 0:
            raise ValueError(f"PLAN_INVALID: {a.type} requires a non-negative params.tabId")
        if a.type == "close_tab" and working_memory:
            # Closing the primary tab strands the session; the planner must
            # target a child tab (or end the task) instead.
            primary = working_memory.get("primary_tab_id")
            if primary is not None and p.tab_id == primary:
                raise ValueError("PLAN_INVALID: cannot close the primary tab of an active session")
    elif a.type == "new_tab":
        if p is None or not _safe_nav_url(p.url):
            raise ValueError(f"PLAN_INVALID: unsafe new_tab url {getattr(p, 'url', None)!r}")
        if allowed_domains:
            from .allowlist import url_allowed
            if not url_allowed(p.url, allowed_domains):
                raise ValueError(
                    f"PLAN_INVALID: new_tab url {p.url!r} is not on the domain allow-list"
                )
    elif a.type == "webmcp_tool":
        # Task 14: a WebMCP call is only valid for a tool this
        # observation actually declared for this origin, with
        # arguments matching its input schema. The tool description
        # and the arguments are scanned by the isolation checker — a
        # description carrying an instruction override or a credential
        # lure is uncallable. A tool that asks for credentials or a
        # payment must be risk=sensitive (the loop forces takeover).
        from .webmcp import tool_requests_sensitive, validate_webmcp_call

        _tool_name = getattr(p, "tool", None) if p is not None else None
        _args = getattr(p, "arguments", None) if p is not None else None
        if not _tool_name:
            raise ValueError("PLAN_INVALID: webmcp_tool requires params.tool")
        if _args is None:
            _args = {}
        _tools = (working_memory or {}).get("webmcp_tools") or []
        _origin = (working_memory or {}).get("webmcp_origin") or ""
        _declared = [t for t in _tools if isinstance(t, dict) and (t.get("origin") or "") == _origin]
        if _origin and not _declared and _tools:
            raise ValueError(
                "PLAN_INVALID: webmcp tools were declared for a different origin "
                "than the current page — the model must not call a tool from another site"
            )
        _err = validate_webmcp_call(_tool_name, _args, _declared or _tools)
        if _err:
            raise ValueError(f"PLAN_INVALID: {_err}")
        _tool = next(
            (t for t in (_declared or _tools) if isinstance(t, dict) and t.get("name") == _tool_name),
            None,
        )
        if _tool is not None:
            from .checker import scan_page_text

            from .webmcp import WebMCPTool as _WMT

            _tool_obj = _WMT.from_dict(_tool)
            _scan_text = f"{_tool_obj.name} {_tool_obj.description} {json.dumps(_args, default=str)}"
            _findings = scan_page_text(_scan_text)
            _bad = {
                "ignore_instructions", "reveal_system_prompt",
                "exfiltrate_secret", "otp_exfiltration", "credential_harvest",
            }
            _hit = sorted({f.rule for f in _findings} & _bad)
            if _hit:
                raise ValueError(
                    "PLAN_INVALID: webmcp tool description/arguments look like a "
                    f"prompt-injection lure ({', '.join(_hit)}) — the tool is uncallable"
                )
            if tool_requests_sensitive(_tool_obj) is not None and a.risk != "sensitive":
                raise ValueError(
                    "PLAN_INVALID: webmcp tool asks for credentials or a payment — "
                    "it must be risk=sensitive so the operator takes over first"
                )
    elif a.type == "ucp_checkout":
        # Task 14: a UCP checkout is only valid against the profile
        # this observation's merchant actually published, and only
        # within the Task 4 payment policy (cap + currency — approval
        # cannot lift it). Risk must be sensitive: the step always
        # needs the operator's approval AND takeover.
        from .payments import validate_payment
        from .ucp import SUPPORTED_UCP_VERSIONS

        _merchant = (getattr(p, "merchant", None) or "").strip().lower() if p is not None else ""
        _amount = getattr(p, "amount", None) if p is not None else None
        _currency = getattr(p, "currency", None) if p is not None else None
        if a.risk != "sensitive":
            raise ValueError("PLAN_INVALID: ucp_checkout must be risk=sensitive (approval + takeover always required)")
        if not _merchant:
            raise ValueError("PLAN_INVALID: ucp_checkout requires params.merchant")
        if _amount is None or _currency is None:
            raise ValueError("PLAN_INVALID: ucp_checkout requires params.amount and params.currency")
        _prof = (working_memory or {}).get("ucp_profile") or {}
        _prof_origin = str(_prof.get("origin") or "")
        if _prof_origin and (urlsplit(_prof_origin).hostname or "").lower() != _merchant:
            raise ValueError(
                "PLAN_INVALID: ucp_checkout merchant does not match the merchant "
                "whose UCP profile this observation carries"
            )
        if _prof.get("version") and _prof["version"] not in SUPPORTED_UCP_VERSIONS:
            raise ValueError(
                f"PLAN_INVALID: UCP profile version {_prof['version']!r} is not supported by this build"
            )
        _services = _prof.get("services") or []
        if _services and not any(
            isinstance(s, dict) and s.get("capability") == "checkout" and s.get("transport") == "rest"
            for s in _services
        ):
            raise ValueError("PLAN_INVALID: merchant UCP profile has no REST checkout service")
        _decision = validate_payment(_amount, _currency, _merchant)
        if not _decision.allowed:
            raise ValueError(f"PLAN_INVALID: {_decision.reason}")


def _validate_plan(
    raw: dict[str, Any],
    *,
    payload: Any = None,
    history: Optional[list[dict[str, Any]]] = None,
    instruction: str = "",
    working_memory: Optional[dict[str, Any]] = None,
    allowed_domains: Optional[list[str]] = None,
) -> TaskPlan:
    norm = _enrich_targets_from_role(_normalize_raw_plan(raw), payload)
    try:
        plan = TaskPlan.model_validate(norm)
    except ValidationError as exc:
        raise ValueError(f"PLAN_INVALID: provider answer failed TaskPlan schema: {exc}") from exc

    if not plan.goal.strip():
        raise ValueError("PLAN_INVALID: plan goal must not be blank")
    if len(plan.steps) > MAX_STEPS_PER_PLAN:
        raise ValueError(
            f"PLAN_INVALID: plan has {len(plan.steps)} steps; maximum is {MAX_STEPS_PER_PLAN}"
        )

    # Task 7: element refs known for this observation. A provider plan
    # that quotes a ref must quote one the page actually has; when there
    # is no observation (unit tests driving validation directly) the
    # ref check is skipped. The same tree backs the OTP-by-ref check
    # below, so the ref -> element mapping is computed once.
    valid_refs: Optional[set[str]] = None
    otp_refs: set[str] = set()
    name_counts: Optional[dict[str, int]] = None
    name_role_counts: Optional[dict[tuple[str, str], int]] = None
    if payload is not None:
        try:
            from .grounding import build_ax_tree
            from .grounding import name_counts as _name_counts
            from .grounding import name_role_counts as _name_role_counts
            from .grounding import normalize_accessible_name as _norm_name

            _ax_nodes = getattr(payload, "ax_tree", None)
            if _ax_nodes:
                valid_refs = {str(n.ref) for n in _ax_nodes}
                _tree_nodes = list(_ax_nodes)
            else:
                _tree = build_ax_tree(_payload_interactive(payload))
                valid_refs = {n.ref for n in _tree.nodes}
                _tree_nodes = _tree.nodes
            # Moodle fix: how many nodes share each normalised name (and
            # each name+role pair), so a name-only step can be rejected
            # as ambiguous below — unless a role or targetId
            # disambiguates it.
            name_counts = _name_counts(_tree_nodes)
            name_role_counts = _name_role_counts(_tree_nodes)
            # OTP refs: a ref whose node is an OTP field. Nodes are
            # matched to elements by target id (or role+name when the
            # author id is empty) — never by list index, because a
            # shipped tree may have pruned nodes the element list kept.
            from .takeover import is_otp_field as _is_otp

            _els = _payload_interactive(payload)
            _by_id: dict[str, Any] = {}
            _by_role_name: dict[tuple[str, str], Any] = {}
            for _el in _els:
                _tid = str(_el_field(_el, "targetId") or "")
                if _tid:
                    _by_id[_tid] = _el
                # Node names are glyph-normalised at tree build time, so
                # the element side must be normalised too, or a glyph in
                # the page name breaks the role+name lookup.
                _by_role_name[
                    (
                        str(_el_field(_el, "role") or "").casefold(),
                        _norm_name(str(_el_field(_el, "name") or "")).casefold(),
                    )
                ] = _el
            for _node in _tree_nodes:
                _el = _by_id.get(str(getattr(_node, "target_id", "") or "")) or _by_role_name.get(
                    (
                        str(getattr(_node, "role", "") or "").casefold(),
                        str(getattr(_node, "name", "") or "").casefold(),
                    )
                )
                if _el is not None and _is_otp(_el):
                    otp_refs.add(_node.ref)
        except Exception:
            valid_refs = None
            otp_refs = set()
            name_counts = None
            name_role_counts = None

    # Execute the longest valid prefix instead of discarding an otherwise good
    # plan because a later step was underspecified. Only a bad FIRST step is
    # fatal, and the loop then replans with an explanatory penalty.
    valid_steps: list[PlannedStep] = []
    for index, step in enumerate(plan.steps):
        try:
            _validate_step(step, working_memory, allowed_domains=allowed_domains, valid_refs=valid_refs, name_counts=name_counts, name_role_counts=name_role_counts)
        except ValueError:
            if index == 0:
                raise
            break
        valid_steps.append(step)
    if len(valid_steps) != len(plan.steps):
        plan = plan.model_copy(update={"steps": valid_steps})

    # Task 4: the model must never type into a one-time-code field. The
    # loop's takeover detection is the primary guard (it fires before
    # planning); this is defence in depth for a plan that slips past
    # it — a type into an OTP field is invalid, not executable. The
    # target is resolved against the current page's elements by id, name,
    # or element ref; an unresolvable target is left to the loop's guard.
    if payload is not None and valid_steps:
        from .grounding import normalize_accessible_name as _norm_name
        from .takeover import is_otp_field

        _otp_by_id: set[str] = set()
        _otp_names: set[str] = set()
        for _el in _payload_interactive(payload):
            if is_otp_field(_el):
                _tid = _el_field(_el, "targetId")
                if _tid:
                    _otp_by_id.add(str(_tid))
                # Review (Moodle fix): both sides of this comparison
                # must be glyph-normalised. The planner reads the
                # stripped name from <accessibility-tree> while a
                # legacy observation can carry the glyph-prefixed
                # element name — comparing raw names lets a type into
                # an OTP field slip past this guard.
                _name = _norm_name(str(_el_field(_el, "name") or "")).casefold()
                if _name:
                    _otp_names.add(_name)
        for _step in valid_steps:
            if _step.action.type != "type":
                continue
            _target = _step.action.target
            if _target is None:
                continue
            # The target may be a dict (raw provider JSON) or a model.
            if isinstance(_target, dict):
                _tid = _target.get("targetId") or _target.get("target_id")
                _tname = _norm_name(str(_target.get("name") or "")).casefold()
                _tref = _target.get("elementRef") or _target.get("element_ref")
            else:
                _tid = getattr(_target, "target_id", None)
                _tname = _norm_name(str(getattr(_target, "name", "") or "")).casefold()
                _tref = getattr(_target, "element_ref", None)
            if (
                (_tid and str(_tid) in _otp_by_id)
                or (_tname and _tname in _otp_names)
                or (_tref and str(_tref) in otp_refs)
            ):
                raise ValueError(
                    "PLAN_INVALID: type action targets a one-time-code (OTP) field — the agent never types one-time codes"
                )

    # Requirement 4: Prevent unnecessary navigation back to already visited websites
    tracker = (working_memory or {}).get("multi_site_research")
    if tracker and valid_steps:
        from .extractors.research import is_already_resolved_site, get_pending_sites
        for step in valid_steps:
            if step.action.type == "navigate" and step.action.params and step.action.params.url:
                target_url = step.action.params.url
                if is_already_resolved_site(target_url, tracker):
                    pending = get_pending_sites(tracker)
                    if pending:
                        next_s = pending[0]
                        step.action.params.url = next_s["default_url"]
                        step.action.verify = f"navigated to {next_s['name']}"
                        step.why = f"Prevented redundant visit to already completed site. Navigating to pending comparison site {next_s['name']} instead."

    return _guard_completion(plan, payload, history or [], instruction, working_memory=working_memory)
