"""Bounded agent control loop: observe → plan → validate → act → verify.

Injectable dependencies (hub, planner, obs_factory) so tests drive the loop
without Chrome. Production wiring (main.py) passes the real hub and planner.
"""
from __future__ import annotations

import asyncio
import inspect
import re
import time
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from . import config
from .contracts import PrivacyViolation
from .hub import Hub, hub as real_hub, TaskRecord
from .planner import plan_from_observation
from .privacy import classify_and_redact, redact_text_urls, redact_url, sanitize_text
from .providers.errors import ProviderUnavailable

MAX_STEPS = config.MAX_STEPS
MAX_RETRIES = config.MAX_RETRIES
TASK_TIMEOUT_S = config.TASK_TIMEOUT_S
MAX_NOTES_CHARS = 4000
MAX_HISTORY_TEXT_CHARS = 400
# A malformed plan must not kill the mission. Give the model a bounded number
# of replans with the exact validation error before failing honestly.
MAX_INVALID_PLANS = 3

# TIMEOUT is ambiguous: the extension may still be executing the action. Only
# retry action types that cannot duplicate a side effect.
_IDEMPOTENT_RETRY_TYPES = {"observe", "scroll", "wait", "zoom", "select_tab"}

# Tab changes relocate the active surface; their result carries no observation
# in production, so the loop re-observes instead of trusting a stale tab view.
_FORCE_REOBSERVE_ACTIONS = {"select_tab", "close_tab"}

_cancelled: set[str] = set()

# Chat/messaging hosts where "typed with submit" genuinely means "sent".
# Kept as registrable domains so web./www. subdomains match too.
_MESSAGING_DOMAINS = (
    "whatsapp.com",
    "messenger.com",
    "telegram.org",
    "telegram.me",
    "instagram.com",
    "x.com",
    "twitter.com",
    "linkedin.com",
    "discord.com",
    "slack.com",
    "teams.microsoft.com",
    "teams.live.com",
    "skype.com",
    "signal.org",
    "chat.google.com",
    "messages.google.com",
)

# Full hostnames that are sign-in surfaces (not whole registrable domains:
# google.com itself is a search engine, not an auth page).
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

# Whole path segments that indicate a credential / payment surface.
_AUTH_PATH_TOKENS = {
    "signin", "sign-in", "login", "log-in", "logon", "log-on", "signup",
    "sign-up", "auth", "authenticate", "authorize", "oauth", "oauth2",
    "otp", "verify", "verification", "mfa", "2fa", "password", "passwd",
    "checkout", "payment", "pay-now", "makepayment", "billing", "purchase",
}


def request_cancel(task_id: str) -> None:
    _cancelled.add(task_id)


def is_cancelled(task_id: str) -> bool:
    return task_id in _cancelled


def _noop_progress(record: TaskRecord | dict[str, Any], *_: Any) -> None:
    pass


def _host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _host_matches(host: str, domain: str) -> bool:
    return bool(host) and (host == domain or host.endswith("." + domain))


def _is_messaging_host(url: str) -> bool:
    host = _host_of(url)
    return any(_host_matches(host, d) for d in _MESSAGING_DOMAINS)


def _is_auth_url(url: str) -> bool:
    """True only for genuine sign-in/payment surfaces.

    Substring matching used to flag any URL containing "auth" (including
    /author/, /authority/) and force a human gate. Match whole path segments
    and known auth hostnames instead.
    """
    host = _host_of(url)
    if not host:
        return False
    if any(_host_matches(host, h) for h in _AUTH_HOSTS):
        return True
    try:
        path = urlsplit(url).path.lower()
    except ValueError:
        return False
    # Split on separators but keep hyphenated tokens ("sign-in", "log-in").
    tokens: set[str] = set()
    for token in re.split(r"[/_.]+", path):
        if not token:
            continue
        tokens.add(token)
        if "-" in token:
            tokens.update(part for part in token.split("-") if part)
    return bool(tokens & _AUTH_PATH_TOKENS)


def _target_name(target: Any) -> str:
    if isinstance(target, dict):
        return str(target.get("name") or target.get("targetId") or "").strip().lower()
    return str(target or "").strip().lower()


def _is_composer_target(target: Any) -> bool:
    name = _target_name(target)
    return (
        "type a message" in name
        or "composer" in name
        or name == "message"
        or name.startswith("message to ")
    )


def _auto_complete_after_send(history: list[dict[str, Any]], action: dict[str, Any], url: str) -> bool:
    """Detect a redundant follow-up after a composer send on a chat app.

    Scoped to known messaging hosts and to the same composer / explicit send
    controls: a later search-box "type" must not be mistaken for a resend.
    """
    if not _is_messaging_host(url) or not history:
        return False
    last = history[-1]
    if not last.get("result_ok"):
        return False
    last_act = last.get("action") or {}
    params = last_act.get("params") or {}
    if last_act.get("type") != "type" or not params.get("submit"):
        return False
    if not _is_composer_target(last_act.get("target")):
        return False

    nxt_type = action.get("type")
    nxt_target = action.get("target") or {}
    if nxt_type == "type":
        return _is_composer_target(nxt_target)
    target_text = str(nxt_target).lower()
    return any(w in target_text for w in ("send", "voice", "ptt", "microphone"))


async def run_with_injection(
    ext_hub: Any,
    *,
    planner: Callable[..., dict[str, Any]],
    obs_factory: Callable[[], dict[str, Any]],
    task_id: str = "test-task",
    instruction: str = "loop",
    on_progress: Optional[Callable[..., None]] = None,
    confirm_gate: Optional[Any] = None,
    confirm_timeout_s: float = 120.0,
    on_metrics: Optional[Callable[[dict[str, Any]], None]] = None,
    initial_memory: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Test entrypoint mirroring run_task's flow with injectable parts."""
    try:
        return await _run_loop(
            ext_hub,
            planner=planner,
            obs_factory=obs_factory,
            task_id=task_id,
            instruction=instruction,
            on_progress=on_progress,
            confirm_gate=confirm_gate,
            confirm_timeout_s=confirm_timeout_s,
            on_metrics=on_metrics,
            initial_memory=initial_memory,
        )
    finally:
        # Never leak cancellation markers: task ids are unique, but a stale
        # marker would make a replayed id silently cancel itself.
        _cancelled.discard(task_id)


async def _run_loop(
    ext_hub: Any,
    *,
    planner: Callable[..., dict[str, Any]],
    obs_factory: Callable[[], dict[str, Any]],
    task_id: str,
    instruction: str,
    on_progress: Optional[Callable[..., None]],
    confirm_gate: Optional[Any],
    confirm_timeout_s: float,
    on_metrics: Optional[Callable[[dict[str, Any]], None]] = None,
    initial_memory: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    history: list[dict[str, Any]] = []
    penalties: list[str] = []
    working_memory: dict[str, Any] = dict(initial_memory or {})
    steps_done = 0
    invalid_plans = 0
    start = time.monotonic()
    # A successful action already returns the post-action observation from the
    # extension. Replanning on that snapshot removes one full observe round
    # trip per step. It is revalidated and re-redacted before every prompt.
    cached_obs: Any = None

    def progress(msg: str, screenshot: Optional[str] = None) -> None:
        if not on_progress:
            return
        try:
            on_progress(steps_done, msg, screenshot=screenshot)
        except TypeError:
            try:
                on_progress(steps_done, msg)
            except TypeError:
                on_progress()

    def metrics(phase: str, started: float) -> None:
        """Report one phase's wall time. Never lets telemetry break the loop."""
        if not on_metrics:
            return
        try:
            on_metrics({"phase": phase, "ms": int((time.perf_counter() - started) * 1000)})
        except Exception:
            pass

    while steps_done < MAX_STEPS:
        if is_cancelled(task_id):
            return {"final": "cancelled", "steps": steps_done, "summary": None}
        if time.monotonic() - start > TASK_TIMEOUT_S:
            return {"final": "budget_exceeded", "steps": steps_done, "summary": "timeout"}

        if cached_obs is not None:
            raw_obs = cached_obs
            cached_obs = None
        else:
            observe_started = time.perf_counter()
            _maybe = obs_factory()
            raw_obs = await _maybe if asyncio.iscoroutine(_maybe) else _maybe
            metrics("observe", observe_started)
        try:
            payload, _decision = classify_and_redact(_to_obs(raw_obs), instruction=instruction)
        except PrivacyViolation:
            return {"final": "failed", "steps": steps_done, "summary": "privacy violation — payload not sent"}

        raw_url = str(
            getattr(raw_obs, "url", None)
            or (raw_obs.get("url") if isinstance(raw_obs, dict) else "")
            or ""
        )

        h_view = _history_view(history, penalties, working_memory)
        payload_dict = payload.model_dump() if hasattr(payload, "model_dump") else payload
        # Planner providers are synchronous HTTP clients. Run them off the
        # event loop so cancel/confirm endpoints stay responsive during a
        # slow model call.
        raw_plan = None
        plan_started = time.perf_counter()
        try:
            raw_plan = await asyncio.to_thread(
                _call_planner, planner, instruction, payload_dict, h_view, working_memory
            )
        except ValueError as exc:
            # A plan that fails schema/target validation is a planner mistake,
            # not a mission-fatal error. Replan with the exact reason instead of
            # surfacing "loop error" after one step.
            invalid_plans += 1
            reason = redact_text_urls(str(exc))[:300]
            penalties.append(
                f"invalid plan ({reason}); re-emit a valid plan. Every click/type "
                "needs target.targetId or target.name from the page context."
            )
            if invalid_plans >= MAX_INVALID_PLANS:
                return {
                    "final": "failed",
                    "steps": steps_done,
                    "summary": f"Task incomplete: the planner could not produce a valid action ({reason}).",
                }
            progress(f"Replanning after invalid plan: {reason}")
            continue
        except ProviderUnavailable as exc:
            # Quota/outage is not a mission bug. Fail fast with the operator
            # message instead of retrying a provider that cannot answer.
            return {
                "final": "failed",
                "steps": steps_done,
                "summary": f"Planner unavailable: {redact_text_urls(str(exc))[:400]}",
            }
        finally:
            metrics("plan", plan_started)
        invalid_plans = 0
        if is_cancelled(task_id):
            return {"final": "cancelled", "steps": steps_done, "summary": None}
        if not isinstance(raw_plan, dict):
            return {"final": "failed", "steps": steps_done, "summary": "planner returned a non-object plan"}

        new_notes = raw_plan.get("notes")
        if new_notes:
            # Bound model-written memory: a hostile page must not be able to
            # inflate every subsequent prompt (cost/latency) without limit.
            # Notes are re-sent on every step, so they need the same free-text
            # scrub as the page body: URL tokens *and* PII patterns.
            working_memory["notes"] = sanitize_text(str(new_notes), MAX_NOTES_CHARS)

        steps = raw_plan.get("steps", [])
        if not steps:
            final_ans = raw_plan.get("answer") or raw_plan.get("summary")
            summary_msg = final_ans if final_ans else _summary(raw_obs, history)
            if raw_plan.get("blocked"):
                # The planner guard proved the task is incomplete. Report it as
                # a failure so the UI never renders "Mission Completed" over an
                # unverified claim.
                return {"final": "failed", "steps": steps_done,
                        "summary": summary_msg or "Task incomplete: the result could not be verified."}
            return {"final": "completed", "steps": steps_done, "summary": summary_msg}

        first = steps[0] if isinstance(steps, list) else None
        action = first.get("action") if isinstance(first, dict) else None
        if not isinstance(action, dict) or not action.get("type"):
            # Belt-and-braces: every real provider is schema-validated, but a
            # malformed injected plan must never crash the loop with a KeyError.
            return {"final": "failed", "steps": steps_done, "summary": "planner returned a malformed step"}

        # Loop prevention & chat auto-completion (host-scoped; no false
        # positives on ordinary search forms that happen to have a send button).
        if history and history[-1].get("result_ok"):
            if _auto_complete_after_send(history, action, raw_url):
                notes_summary = working_memory.get("notes") or "Message successfully sent to recipient."
                return {
                    "final": "completed",
                    "steps": steps_done,
                    "summary": f"Completed: {notes_summary[:160]}",
                }

            consecutive_repeats = 0
            for h in reversed(history):
                if h.get("result_ok") and _actions_match(h.get("action", {}), action):
                    consecutive_repeats += 1
                else:
                    break

            if consecutive_repeats >= 3 and action.get("type") != "scroll":
                return {
                    "final": "budget_exceeded",
                    "steps": steps_done,
                    "summary": f"Loop detected: action '{action.get('type')}' repeated {consecutive_repeats + 1} times without progress."
                }
            if consecutive_repeats >= 1 and action.get("type") != "scroll":
                penalties.append(f"loop warning: action '{action.get('type')}' was already completed. Do not repeat it; proceed to next step or conclude.")

        is_wa_qr = _is_whatsapp_qr(raw_url, raw_obs)
        is_auth_page = _is_auth_url(raw_url)
        is_strict = getattr(confirm_gate, "strict_write_confirm", True) if confirm_gate is not None else False
        needs_confirm = action.get("risk") == "sensitive" or is_auth_page or is_wa_qr or (
            is_strict and action.get("risk") == "write" and action.get("type") not in ("navigate", "scroll", "zoom", "wait", "observe", "select_tab")
        )
        if confirm_gate is not None and needs_confirm:
            gate_msg = _gate_message(action, is_wa_qr, is_auth_page)
            approved = await _request_confirmation(confirm_gate, action, task_id, confirm_timeout_s, custom_message=gate_msg)
            if is_cancelled(task_id):
                return {"final": "cancelled", "steps": steps_done, "summary": None}
            if approved is False:
                return {"final": "failed", "steps": steps_done,
                        "summary": f"user rejected action: {action.get('type')}"}

        action_started = time.perf_counter()
        result = await _execute_with_retries(ext_hub, action, task_id)
        metrics("act", action_started)
        if is_cancelled(task_id):
            return {"final": "cancelled", "steps": steps_done, "summary": None}
        steps_done += 1
        cached_obs = _fresh_observation(result, action)

        evidence: Any = result.get("evidence")
        if not evidence:
            if result.get("ok"):
                evidence = "ok"
            else:
                err_msg = (result.get("error") or {}).get("message", "failed")
                evidence = err_msg
        raw_screenshot = raw_obs.get("screenshot") if isinstance(raw_obs, dict) else getattr(raw_obs, "screenshot", None)
        fresh_shot = (
            getattr(cached_obs, "screenshot", None)
            if cached_obs
            else (result.get("observation") or {}).get("screenshot")
        )
        step_screenshot = fresh_shot or raw_screenshot
        safe_evidence: Any = redact_text_urls(evidence) if isinstance(evidence, str) else evidence
        progress(f"Step {steps_done}: {action.get('type')} -> {safe_evidence}", screenshot=step_screenshot)

        entry = {"action": action, "result_ok": result.get("ok"), "evidence": safe_evidence}
        if not result.get("ok"):
            err = result.get("error") or {}
            err_msg = str(err.get("message", "unknown"))
            entry["error"] = redact_text_urls(err_msg)
            if not err.get("recoverable", False):
                return {"final": "failed", "steps": steps_done,
                        "summary": f"non-recoverable error: {redact_text_urls(err_msg)}"}
            penalty = _same_failure(history, entry)
            if penalty:
                penalties.append(penalty)
        history.append(entry)

        if steps_done >= MAX_STEPS:
            break

    if is_cancelled(task_id):
        return {"final": "cancelled", "steps": steps_done, "summary": None}
    final_obs = cached_obs
    if final_obs is None:
        final_obs = obs_factory()
        if asyncio.iscoroutine(final_obs):
            try:
                final_obs = await final_obs
            except Exception:
                final_obs = {}
    return {"final": "budget_exceeded", "steps": steps_done, "summary": _summary(final_obs, history)}


def _call_planner(
    planner: Callable[..., dict[str, Any]],
    instruction: str,
    payload: dict[str, Any],
    history: list[dict[str, Any]],
    working_memory: dict[str, Any],
) -> dict[str, Any]:
    """Call the planner with working_memory only when it declares support.

    Intent inspection beats catching TypeError: a TypeError raised *inside*
    a planner must surface instead of silently replaying the call without
    working memory (which could duplicate side effects).
    """
    try:
        params = inspect.signature(planner).parameters
    except (TypeError, ValueError):
        params = {}
    supports_wm = "working_memory" in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    )
    if supports_wm:
        return planner(instruction, payload, history, working_memory=working_memory)
    return planner(instruction, payload, history)


def _actions_match(a1: dict[str, Any], a2: dict[str, Any]) -> bool:
    if a1.get("type") != a2.get("type"):
        return False
    t1 = a1.get("target") or {}
    t2 = a2.get("target") or {}
    p1 = a1.get("params") or {}
    p2 = a2.get("params") or {}
    if t1 and t2:
        if t1.get("name") and t1.get("name") == t2.get("name"):
            return True
        if t1.get("targetId") and t1.get("targetId") == t2.get("targetId"):
            return True
    if p1.get("text") and p1.get("text") == p2.get("text"):
        return True
    if p1.get("url") and p1.get("url") == p2.get("url"):
        return True
    return False


def _is_whatsapp_qr(raw_url: str, raw_obs: Any) -> bool:
    if not _host_matches(_host_of(raw_url), "whatsapp.com"):
        return False
    elements = getattr(raw_obs, "interactive", None)
    if elements is None and isinstance(raw_obs, dict):
        elements = raw_obs.get("interactive") or []

    has_chats = False
    has_qr_prompt = False
    for el in elements or []:
        name = str(getattr(el, "name", None) or (el.get("name") if isinstance(el, dict) else "")).lower()
        role = str(getattr(el, "role", None) or (el.get("role") if isinstance(el, dict) else "")).lower()
        if (
            "search or start new chat" in name
            or "type a message" in name
            or role == "listitem"
            or name in ("chats", "status", "channels", "communities")
        ):
            has_chats = True
        if (
            "scan this qr code" in name
            or "use whatsapp on your computer" in name
            or "link with phone number" in name
            or "to use whatsapp on your computer" in name
            or ("qr" in name and "code" in name and not has_chats)
        ):
            has_qr_prompt = True

    # If chats or search input are visible, user is logged in — never block on QR gate
    if has_chats:
        return False
    return has_qr_prompt


HANDOFF_MESSAGE = "Please solve the CAPTCHA / sign in manually in your browser, then click Continue."


def _gate_message(action: dict[str, Any], is_wa_qr: bool, is_auth_page: bool) -> Optional[str]:
    """Operator-facing instruction shown with the confirmation gate."""
    if is_wa_qr:
        return ("WhatsApp Web requires QR code scan. Please scan the QR code in your "
                "WhatsApp tab to log in, then click Continue.")
    if action.get("type") == "wait" and action.get("risk") == "sensitive":
        # The planner's human-in-the-loop handoff (CAPTCHA / login wall).
        return HANDOFF_MESSAGE
    if is_auth_page:
        return ("Site requires Sign-In or Payment Credentials. Please sign in or complete "
                "authentication in your browser tab, then click Continue.")
    return None


async def _request_confirmation(gate: Any, action: dict[str, Any], task_id: str, timeout_s: float, custom_message: Optional[str] = None) -> bool:
    """Pause and await a human decision. Returns True if approved.
    gate contract: confirm_requests list + asyncio.Event confirm_event + decision:bool|None."""
    req = {
        "taskId": task_id,
        "action": action,
        "message": custom_message or f"NAVIGATOR wants to {action.get('type')} (risk {action.get('risk')})"
    }
    if hasattr(gate, "add_request"):
        gate.add_request(req)
    else:
        gate.confirm_requests.append(req)
    gate.decision = None
    gate.confirm_event.clear()
    try:
        await asyncio.wait_for(gate.confirm_event.wait(), timeout_s)
    except asyncio.TimeoutError:
        return False
    return bool(gate.decision)


def _summary(obs: Any, history: list[dict[str, Any]]) -> str:
    last_ok = next((h.get("evidence") for h in reversed(history) if h.get("result_ok") and h.get("evidence")), None)
    raw_url = getattr(obs, "url", None) or (obs.get("url") if isinstance(obs, dict) else "") or ""
    # Summary is persisted and rendered; never surface tokens from the URL or
    # from evidence strings that echo navigated-to URLs.
    evidence = redact_text_urls(str(last_ok)) if last_ok else "none"
    # Running out of steps is a failure, not a victory. Say why, so the UI shows
    # a truthful reason instead of a generic completion line.
    note = ""
    last = history[-1] if history else None
    if last and last.get("result_ok") is False:
        act = (last.get("action") or {}).get("type") or "action"
        note = f"Task incomplete: the last {act} failed ({redact_text_urls(str(last.get('error') or 'error'))}). "
    elif _is_auth_url(str(raw_url)):
        note = "Task incomplete: the page is still a sign-in screen, so the requested data was not retrieved. "
    return note + f"final url: {redact_url(str(raw_url))} | last evidence: {evidence}"


def _same_failure(history: list[dict[str, Any]], entry: dict[str, Any]) -> Optional[str]:
    for prev in reversed(history):
        if prev.get("result_ok"):
            return None
        if prev.get("error") == entry.get("error") and prev["action"].get("type") == entry["action"].get("type"):
            return f"repeated failure: {entry['error']} on {entry['action'].get('type')}"
    return None


def _history_view(history: list[dict[str, Any]], penalties: list[str], working_memory: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for h in history[-5:]:
        entry = dict(h)
        for key in ("evidence", "error"):
            if isinstance(entry.get(key), str):
                entry[key] = entry[key][:MAX_HISTORY_TEXT_CHARS]
        out.append(entry)
    if working_memory and working_memory.get("notes"):
        out.append({"working_memory": working_memory["notes"]})
    for p in penalties[-3:]:
        out.append({"penalty": p})
    return out


async def _execute_with_retries(ext_hub: Any, action: dict[str, Any], task_id: str) -> dict[str, Any]:
    act = dict(action)
    if "timeoutMs" not in act:
        act["timeoutMs"] = act.get("timeout_ms", 10000)
    # Wait slightly longer than the extension's own action deadline (+5s) so
    # the backend never declares TIMEOUT while the action may still finish,
    # then retry it and double-execute a click.
    raw_timeout = act.get("timeoutMs")
    timeout_s = 45.0
    if isinstance(raw_timeout, (int, float)) and raw_timeout > 0:
        timeout_s = min(float(raw_timeout) / 1000.0, 60.0) + 10.0

    res: dict[str, Any] = {"ok": False, "error": {"code": "CANCELLED", "message": "task cancelled", "recoverable": False}}
    for attempt in range(MAX_RETRIES + 1):
        if is_cancelled(task_id):
            return {"ok": False, "error": {"code": "CANCELLED", "message": "task cancelled", "recoverable": False}}
        # Route with the real task id: "task" was a placeholder that broke
        # per-task socket routing and cancellation audits.
        res = await ext_hub.send_action(task_id, act, timeout_s=timeout_s)
        if res.get("ok"):
            return res
        err = res.get("error") or {}
        if err.get("code") == "TIMEOUT" and act.get("type") not in _IDEMPOTENT_RETRY_TYPES:
            return res
        if not err.get("recoverable", False) or attempt == MAX_RETRIES:
            return res
        await asyncio.sleep(0.2 * (attempt + 1))
    return res


def _fresh_observation(result: dict[str, Any], action: dict[str, Any]) -> Any:
    """Return a validated post-action observation, or None to re-observe.

    Only successful, non-tab-changing actions qualify. Validation is
    defensive: a malformed extension payload must fall back to obs_factory()
    rather than reach the privacy layer as a partial object.
    """
    if not result.get("ok") or action.get("type") in _FORCE_REOBSERVE_ACTIONS:
        return None
    obs = result.get("observation")
    if not obs:
        return None
    try:
        return _to_obs(obs)
    except Exception:
        return None


def _to_obs(raw: dict[str, Any]):
    from .contracts import BrowserObservation
    if isinstance(raw, BrowserObservation):
        return raw
    return BrowserObservation.model_validate(raw)


def extract_mission_facts(instruction: str, summary: str, history: Optional[list[dict[str, Any]]] = None) -> dict[str, str]:
    """Extract structured key-value entities from mission execution for persistent chat_memory."""
    facts: dict[str, str] = {}
    full_text = f"{instruction} | {summary}"

    # 1. Price extraction
    price_match = re.search(r"(?:₹|Rs\.?|INR|\$)\s?[\d,]+(?:\.\d+)?", full_text, re.IGNORECASE)
    if price_match:
        facts["extracted_price"] = price_match.group(0).strip()

    # 2. Canonical URL extraction from summary or history evidence
    url_match = re.search(r"https?://[^\s\"'<>)]+", summary)
    if not url_match and history:
        for h in reversed(history):
            ev = str(h.get("evidence") or "")
            u_match = re.search(r"https?://[^\s\"'<>)]+", ev)
            if u_match:
                url_match = u_match
                break
    if url_match:
        facts["canonical_url"] = url_match.group(0).strip()

    # 3. Product / Subject Target
    cleaned_target = re.sub(
        r"(?i)\b(find|search|buy|check|compare|look for|me|the|a|an|on amazon|on flipkart|on google|specs|price|and|for)\b",
        " ",
        instruction,
    )
    cleaned_target = re.sub(r"\s+", " ", cleaned_target).strip()
    if cleaned_target and len(cleaned_target) > 2:
        facts["target_item"] = cleaned_target[:60]

    # 4. Core constraints
    constraint_match = re.search(r"(?i)\b(under\s+[\d,]+|below\s+[\d,]+|budget\s+[\d,]+|size\s+[a-z0-9]+|to checkout)\b", instruction)
    if constraint_match:
        facts["core_constraint"] = constraint_match.group(0).strip()

    # 5. Last result summary snippet
    if summary:
        facts["last_result"] = summary[:240].strip()

    return facts


async def run_task(task_id: str) -> None:
    """Production entrypoint — wires real hub, real planner, live observation."""
    record: TaskRecord = real_hub.tasks[task_id]
    chat_id = record.chat_id

    initial_memory: dict[str, Any] = {}
    try:
        from . import db
        db.init_db()
        user_prefs = db.get_user_preferences()
        if user_prefs:
            initial_memory["user_preferences"] = user_prefs
    except Exception:
        pass

    if chat_id:
        try:
            from . import db
            raw_msgs = db.get_messages(chat_id, limit=6)
            if raw_msgs:
                initial_memory["conversation_history"] = [
                    {"role": m["role"], "content": m["content"]} for m in raw_msgs
                ]
            chat_mem = db.get_chat_memory(chat_id)
            if chat_mem:
                initial_memory["session_memory"] = chat_mem

            db.add_message(chat_id, "user", record.instruction)

            chat_info = db.get_chat(chat_id)
            if chat_info and chat_info.get("title") in ("New Mission", "New Chat", ""):
                trunc_title = record.instruction[:36].strip()
                if len(record.instruction) > 36:
                    trunc_title += "..."
                db.update_chat_title(chat_id, trunc_title)
        except Exception:
            pass

    def on_step_progress(steps: int = 0, msg: str = "", screenshot: Optional[str] = None) -> None:
        record.steps_done = steps
        record.message = msg
        # A cancelled task must not be flipped back to running by a late step.
        if record.state != "cancelled":
            record.state = "running"
        if screenshot:
            record.screenshot = screenshot
        real_hub.spawn(real_hub.broadcast_progress(record))

    def on_metrics(fields: dict[str, Any]) -> None:
        """Phase timings are attached to the next progress broadcast."""
        phase = fields.get("phase")
        ms = fields.get("ms")
        if not isinstance(ms, int):
            return
        if phase == "plan":
            record.planner_ms = ms
        elif phase == "observe":
            record.observe_ms = ms
        elif phase == "act":
            record.act_ms = ms

    try:
        state = await run_with_injection(
            real_hub,
            planner=_real_planner(record),
            obs_factory=_live_observation_factory(record),
            task_id=task_id,
            instruction=record.instruction,
            on_progress=on_step_progress,
            confirm_gate=_ConfirmGate(record),
            on_metrics=on_metrics,
            initial_memory=initial_memory,
        )
        final_state = {"completed": "completed", "failed": "failed", "cancelled": "cancelled",
                       "budget_exceeded": "failed"}.get(state["final"], "failed")
        if record.cancelled:
            final_state = "cancelled"
        record.state = final_state  # type: ignore[assignment]
        record.message = state.get("summary") or ""
        await real_hub.broadcast_progress(record)

        if chat_id:
            try:
                from . import db
                summary = state.get("summary") or "Mission finished."
                evidence_info = f"{final_state} ({state.get('steps', record.steps_done)} steps)"
                db.add_message(chat_id, "agent", summary, evidence=evidence_info)
                if final_state == "completed":
                    facts = extract_mission_facts(record.instruction, summary, state.get("history") or [])
                    for k, v in facts.items():
                        db.set_chat_memory(chat_id, k, v)
                db.touch_chat(chat_id)
            except Exception:
                pass
    except Exception as exc:
        cancelled = record.cancelled or is_cancelled(task_id)
        record.state = "cancelled" if cancelled else "failed"  # type: ignore[assignment]
        record.message = "" if cancelled else f"loop error: {exc}"
        await real_hub.broadcast_progress(record)

        if chat_id:
            try:
                from . import db
                db.add_message(chat_id, "agent", f"Error: {exc}", evidence="failed")
                db.touch_chat(chat_id)
            except Exception:
                pass
    finally:
        record.pending_confirmation = None


class _ConfirmGate:
    """Bridges the loop's confirm contract onto the Hub TaskRecord a REST
    client posts to. Event + decision live on the record itself."""
    def __init__(self, record: TaskRecord) -> None:
        self._record = record
        self.confirm_requests = []
        self.strict_write_confirm = False

    @property
    def confirm_event(self) -> asyncio.Event:
        return self._record.confirm_event

    @property
    def decision(self) -> Optional[bool]:
        return self._record.confirm_decision

    @decision.setter
    def decision(self, v: Optional[bool]) -> None:
        self._record.confirm_decision = v

    def add_request(self, req: dict[str, Any]) -> None:
        self.confirm_requests.append(req)
        self._record.pending_confirmation = req
        self._record.state = "awaiting_confirmation"
        self._record.message = req.get("message", "Awaiting confirmation")
        real_hub.spawn(real_hub.broadcast_progress(self._record))


class _LiveObservation:
    """Observation factory: asks the extension for the active tab view each
    round. Raises if the extension socket is gone (loop fails cleanly)."""
    def __init__(self, task_id: str) -> None:
        self._task_id = task_id

    async def __call__(self) -> dict[str, Any]:
        if not real_hub.extension_sockets:
            raise RuntimeError("EXTENSION_DISCONNECTED: no extension socket")
        for attempt in range(2):
            if is_cancelled(self._task_id):
                raise RuntimeError("task cancelled")
            res = await real_hub.send_action(self._task_id, {
                "type": "observe", "risk": "read", "verify": "fresh observation", "timeoutMs": 25000,
            })
            if res.get("ok") and res.get("observation"):
                return res["observation"]
            if is_cancelled(self._task_id):
                raise RuntimeError("task cancelled")
            if attempt == 0:
                await asyncio.sleep(0.5)
        raise RuntimeError(f"observation failed: {res.get('error')}")


def _live_observation_factory(record: TaskRecord) -> _LiveObservation:
    return _LiveObservation(record.task_id)


def _provider_for_config():
    """Pick the planner provider reported by ``config.resolve_provider()``.

    Explicit mock wins even with keys present. A remote provider without its
    key falls back exactly as health reports it, so the planner used here and
    the provider advertised by ``/api/health`` can never disagree.
    """
    from . import config
    from .providers.mock import MockProvider
    effective = config.resolve_provider()["effective"]
    if effective == "groq":
        from .providers.groq import GroqProvider
        return GroqProvider(api_key=config.GROQ_API_KEY)
    if effective == "gemini":
        from .providers.gemini import GeminiProvider
        return GeminiProvider(api_key=config.GEMINI_API_KEY)
    return MockProvider()


def _real_planner(record: TaskRecord):
    instruction = record.instruction
    provider = _provider_for_config()

    def planner(_instr: str, payload: Any, history: list[dict[str, Any]], working_memory: Optional[dict[str, Any]] = None):
        from .planner import plan_from_observation
        from .privacy import PrivacyFilteredPayload
        pf = PrivacyFilteredPayload(**payload) if isinstance(payload, dict) else payload
        # exclude_none keeps the WebSocket payload aligned with the TS contract:
        # unset optional params are omitted, never serialized as JSON null.
        return plan_from_observation(instruction=instruction, payload=pf, provider=provider, history=history, working_memory=working_memory).model_dump(by_alias=True, exclude_none=True)

    return planner
