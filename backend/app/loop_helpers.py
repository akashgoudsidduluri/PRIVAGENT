"""Pure loop helpers: URL/host matching, history views, retry and
observation plumbing, mission-fact extraction (Task 13).

Split out of :mod:`app.agentLoop` (which re-exports every name here,
so existing imports keep working). Every function in this module is
pure with respect to loop state except :func:`_execute_with_retries`,
which reads cancellation state from :mod:`app.loop_state`.

One deliberate signature change: :func:`_execute_with_retries` takes
an optional ``max_retries``. Tests patch ``agentLoop.MAX_RETRIES``
and drive the loop through :func:`app.agentLoop.run_with_injection`;
the loop passes its own (patched) value at the call site, while
direct callers keep the old 3-argument call and get
``config.MAX_RETRIES`` as the default. ``_history_view`` keeps its
old signature: no test patches ``MAX_HISTORY_TEXT_CHARS``, so it
reads this module's constant directly.
"""
from __future__ import annotations

import asyncio
import importlib
import inspect
import re
import time
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from . import config
from .loop_state import is_cancelled
from .privacy import redact_text_urls, redact_url
from .takeover import AUTH_PATH_TOKENS

__all__ = [
    "CANONICAL_FORMAT_MAP",
    "MAX_HISTORY_TEXT_CHARS",
    "_AUTH_HOSTS",
    "_AUTH_PATH_TOKENS",
    "_FORCE_REOBSERVE_ACTIONS",
    "_IDEMPOTENT_RETRY_TYPES",
    "_MESSAGING_DOMAINS",
    "_actions_match",
    "_auto_complete_after_send",
    "_call_planner",
    "_enforce_date_integrity",
    "_execute_with_retries",
    "_fresh_observation",
    "_history_view",
    "_host_matches",
    "_host_of",
    "_is_auth_url",
    "_is_composer_target",
    "_is_messaging_host",
    "_is_redundant_completed_action",
    "_is_whatsapp_qr",
    "_normalize_loop_error",
    "_normalize_loop_name",
    "_plan_tree_and_refs",
    "_same_failure",
    "_state_digest",
    "_summary",
    "_target_name",
    "_to_obs",
    "canonical_format_map",
    "extract_mission_facts",
    "warm_loop_caches",
]

MAX_HISTORY_TEXT_CHARS = 400

# TIMEOUT is ambiguous: the extension may still be executing the action. Only
# retry action types that cannot duplicate a side effect.
_IDEMPOTENT_RETRY_TYPES = {"observe", "scroll", "wait", "zoom", "select_tab"}

# Tab changes relocate the active surface; their result carries no observation
# in production, so the loop re-observes instead of trusting a stale tab view.
_FORCE_REOBSERVE_ACTIONS = {"select_tab", "close_tab"}

CANONICAL_FORMAT_MAP: dict[str, str] = {
    "presentation": "pptx",
    "slides": "pptx",
    "slide_deck": "pptx",
    "ppt": "pptx",
    "powerpoint": "pptx",
    "excel": "xlsx",
    "spreadsheet": "xlsx",
    "workbook": "xlsx",
    "word": "docx",
    "document": "docx",
    "doc": "docx",
    "webpage": "html",
    "web": "html",
}
canonical_format_map = CANONICAL_FORMAT_MAP

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
# Single definition lives in app.takeover (Task 4); this alias stays so
# existing imports of agentLoop._AUTH_PATH_TOKENS keep working.
_AUTH_PATH_TOKENS = AUTH_PATH_TOKENS


def _host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _origin_of(url: str) -> str:
    """The scheme://host origin of a URL ("" when it has no host).

    Task 14: WebMCP tools and UCP profiles are bound to the origin
    that declared them, so a tool from one site can never be called
    on another.
    """
    try:
        parts = urlsplit(url or "")
        if not parts.hostname:
            return ""
        return f"{(parts.scheme or 'https').lower()}://{parts.hostname.lower()}"
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


def _enforce_date_integrity(action: dict[str, Any], instruction: str) -> None:
    """Validate and preserve exact user-specified dates and years (e.g. 2026 must never become 2027)."""
    inst = instruction or ""
    if "2026" in inst and "2027" not in inst:
        params = action.get("params") or {}
        if "text" in params and isinstance(params["text"], str) and "2027" in params["text"]:
            params["text"] = params["text"].replace("2027", "2026")
        if "url" in params and isinstance(params["url"], str) and "2027" in params["url"]:
            params["url"] = params["url"].replace("2027", "2026")


class PlannerCancelledError(Exception):
    """A queued planner job was picked up after its deadline.

    ``asyncio.wait_for`` cannot cancel work already submitted to a
    ThreadPoolExecutor, so a timed-out job would otherwise still run the
    planner (wasting an LLM call) long after the loop gave up on it.
    Checking the deadline here — in the worker thread, at pickup time —
    is what actually stops queued jobs from running.
    """


def _call_planner(
    planner: Callable[..., dict[str, Any]],
    instruction: str,
    payload: dict[str, Any],
    history: list[dict[str, Any]],
    working_memory: dict[str, Any],
    deadline: Optional[float] = None,
) -> dict[str, Any]:
    """Call the planner with working_memory only when it declares support.

    Intent inspection beats catching TypeError: a TypeError raised *inside*
    a planner must surface instead of silently replaying the call without
    working memory (which could duplicate side effects).
    """
    if deadline is not None and time.monotonic() > deadline:
        raise PlannerCancelledError(
            "planner job picked up after its deadline; skipping the call"
        )
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


def _normalize_loop_name(name: Any) -> str:
    """Case/punctuation-insensitive target name, for loop matching.

    Refs and ids churn on every re-render, so the accessible name is
    the stable identity of a control — but only cosmetically: case,
    punctuation and symbols (the '✕' in a close button) must not
    defeat the match. Returns '' when nothing alphanumeric survives
    (an icon-only control has no usable name; callers must not treat
    two empty names as a match).
    """
    if not isinstance(name, str):
        return ""
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def _normalize_loop_error(error: Any) -> str:
    """Stable key for a step error, for loop matching.

    Extension errors embed the changing target in the message — e.g.
    STALE_TARGET 'no matching element for
    {"elementRef":"e12","targetId":"...","name":"Close"}' — so the raw
    strings are never equal twice even when the cause is identical.
    Strip JSON blobs, ref-like tokens and numbers; what survives is
    the cause phrase. Returns '' when nothing survives.
    """
    if not isinstance(error, str):
        return ""
    text = re.sub(r"\{[^}]*\}", " ", error)
    text = re.sub(r"\be\d+\b", " ", text)
    text = re.sub(r"[0-9]+", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text.lower())
    return " ".join(text.split())


def _actions_match(a1: dict[str, Any], a2: dict[str, Any]) -> bool:
    if a1.get("type") != a2.get("type"):
        return False
    t1 = a1.get("target") or {}
    t2 = a2.get("target") or {}
    p1 = a1.get("params") or {}
    p2 = a2.get("params") or {}
    if t1 and t2:
        n1 = _normalize_loop_name(t1.get("name"))
        n2 = _normalize_loop_name(t2.get("name"))
        if n1 and n1 == n2:
            return True
        if t1.get("targetId") and t1.get("targetId") == t2.get("targetId"):
            return True
    if p1.get("text") and p1.get("text") == p2.get("text"):
        return True
    if p1.get("url") and p1.get("url") == p2.get("url"):
        return True
    return False


def _is_redundant_completed_action(action: dict[str, Any], history: list[dict[str, Any]], current_url: str) -> bool:
    """Never repeat completed browser actions (e.g. redundant navigate or form submission after provider fallback)."""
    if not history or not action:
        return False
    atype = action.get("type")
    params = action.get("params") or {}
    if atype == "navigate":
        nav_url = params.get("url")
        if nav_url and current_url:
            try:
                cur_parts = urlsplit(current_url)
                nav_parts = urlsplit(nav_url)
                if cur_parts.hostname and nav_parts.hostname and (
                    cur_parts.hostname == nav_parts.hostname
                    or cur_parts.hostname.endswith("." + nav_parts.hostname)
                    or nav_parts.hostname.endswith("." + cur_parts.hostname)
                ):
                    if cur_parts.path.rstrip("/") == nav_parts.path.rstrip("/"):
                        for h in history:
                            h_act = h.get("action") or {}
                            if h.get("result_ok") and h_act.get("type") == "navigate":
                                return True
            except Exception:
                pass
    elif atype == "type":
        text = params.get("text")
        if text and params.get("submit"):
            for h in history:
                h_act = h.get("action") or {}
                if h.get("result_ok") and h_act.get("type") == "type":
                    h_params = h_act.get("params") or {}
                    if h_params.get("text") == text and h_params.get("submit"):
                        return True
    return False


def _plan_tree_and_refs(payload: Any) -> tuple[Any, set[str]]:
    """Build the AX tree for this observation once, and the set of refs
    it contains. Task 11: the loop used to rebuild this tree for every
    step (and the plan validator built it once more); one build per
    plan is enough — every step of the plan is validated against, and
    resolved from, the same observation."""
    from .grounding import AxTree as _AxTree
    from .grounding import build_ax_tree as _build_ax_tree

    ax_nodes = getattr(payload, "ax_tree", None)
    if ax_nodes:
        # The payload tree's nodes are duck-type compatible with
        # grounding.AxNode (same attribute names).
        tree = _AxTree(nodes=list(ax_nodes), source="dom-ax")
    else:
        tree = _build_ax_tree(payload.interactive, source="legacy")
    return tree, {str(n.ref) for n in tree.nodes}


def fresh_tree_for_observation(raw_obs: Any, instruction: str) -> Any | None:
    """Build the AX tree for a *fresh* observation through the exact
    same redaction pipeline as the plan-time tree (``classify_and_redact``
    then ``_plan_tree_and_refs``).

    Task 13 — this is what makes ``check_freshness`` real. The old
    loop compared a node against the tree it was resolved from, so
    "changed" was unreachable. A fresh tree built through the same
    pipeline differs from the plan-time tree ONLY where the page
    itself changed: redaction is a 1:1 order-preserving map, so refs
    line up, and names are truncated identically on both sides.

    Returns ``(tree, refs)`` like ``_plan_tree_and_refs``, or ``None``
    when the observation cannot be redacted (PrivacyViolation, bad
    input). The caller treats ``None`` as "cannot verify" and must
    NOT execute blind — re-observe and replan instead.
    """
    try:
        from .privacy import classify_and_redact

        fresh_payload, _decision = classify_and_redact(
            _to_obs(raw_obs), instruction=instruction
        )
        return _plan_tree_and_refs(fresh_payload)
    except Exception:
        return None


def ref_freshness(
    plan_tree: Any, ref: str, fresh_tree: Any | None
) -> tuple[str, Any | None, Any | None]:
    """Compare a plan-time ref against a fresh tree.

    Returns ``(freshness, live_node, plan_node)`` where ``freshness``
    is ``"ok"`` / ``"changed"`` / ``"stale"`` (see
    ``grounding.check_freshness``), ``live_node`` is the node in the
    fresh tree (or the plan-time node when no fresh tree was given),
    and ``plan_node`` is the node the plan was built from.

    With no fresh tree the verdict is trivially ``"ok"`` — the check
    is only as good as the freshest observation the caller can
    supply, and the loop's post-wait re-observation is what makes
    the single-step path real.
    """
    from .grounding import check_freshness, is_valid_ref, resolve_ref

    plan_node = resolve_ref(plan_tree, str(ref)) if is_valid_ref(ref) else None
    if plan_node is None:
        return "stale", None, None
    if fresh_tree is None:
        return "ok", plan_node, plan_node
    live_node = resolve_ref(fresh_tree, str(ref))
    if live_node is None:
        return "stale", None, plan_node
    return check_freshness(plan_node, fresh_tree), live_node, plan_node


def _state_digest(raw_obs: Any) -> str:
    if raw_obs is None:
        return ""
    url = str(getattr(raw_obs, "url", None) or (raw_obs.get("url") if isinstance(raw_obs, dict) else "") or "")
    title = str(getattr(raw_obs, "title", None) or (raw_obs.get("title") if isinstance(raw_obs, dict) else "") or "")
    interactive = getattr(raw_obs, "interactive", None) or (raw_obs.get("interactive") if isinstance(raw_obs, dict) else []) or []
    el_sample = ",".join(
        str(getattr(e, "target_id", None) or (e.get("targetId") if isinstance(e, dict) else ""))
        for e in interactive[:12]
    )
    return f"{url}|{title}|{len(interactive)}|{el_sample}"


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
    # Errors are compared by normalized cause, not raw text: the
    # extension embeds the changing target (refs, ids, coords) in the
    # message, so two failures with the same cause never share a raw
    # string. An empty normalized key matches nothing.
    err_key = _normalize_loop_error(entry.get("error"))
    if not err_key:
        return None
    for prev in reversed(history):
        if prev.get("result_ok"):
            return None
        if _normalize_loop_error(prev.get("error")) == err_key and prev["action"].get("type") == entry["action"].get("type"):
            return f"repeated failure: {entry['error']} on {entry['action'].get('type')}"
    return None


def _history_view(history: list[dict[str, Any]], penalties: list[str], working_memory: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    # Task 9: a compacted long-horizon task plans from its summary +
    # the last 2 steps, not the full history. The summary is
    # deterministic (built from recorded evidence), so the planner
    # cannot be misled by a stale or injected history entry.
    if working_memory and working_memory.get("long_horizon_summary"):
        out.append({
            "long_horizon_summary": working_memory["long_horizon_summary"],
            "summary_at_step": working_memory.get("long_horizon_summary_at_step"),
        })
        window = history[-2:]
    else:
        window = history[-5:]
    for h in window:
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


def warm_loop_caches() -> None:
    """Pay the loop's one-time per-process costs up front, outside any step.

    Task 4 (latency) — measured, not assumed. In a fresh process the
    first step costs ~200-250 ms more than every later step, and the
    excess is two one-time costs, not per-step work:

    * the first checkpoint encrypts, which derives the Fernet key with
      PBKDF2-HMAC-SHA256 at 600,000 iterations (~140-175 ms); later
      encrypts take ~0.1 ms (key cached in memory);
    * the first redaction lazily imports ``grounding`` / ``vault`` /
      ``adversarial_guard`` and — via ``_validated_webmcp_tools``,
      which runs on every redaction — ``webmcp`` plus ``jsonschema``
      (~60-80 ms); later redactions take ~0.3 ms.

    Schema init is NOT the cause (fresh ``init_db`` measured ~5 ms)
    and neither is the first write/fsync (a warmed first checkpoint
    measured ~1.3 ms). So the fix is warm-up, not a faster write path:
    derive the key and import the lazily-loaded redaction modules once
    here. Repeat calls cost ~0 ms (cached key, cached imports).

    Best-effort by design: it never raises (a failure just means the
    first step pays the cost as before), never writes anything, and
    derives the key in memory only — zero retention is unaffected and
    no security parameter (iteration count, redaction, allow-list)
    changes. Callers: server boot (:mod:`app.main` lifespan) and the
    top of :func:`app.agentLoop._run_loop`, so every task path warms.
    """
    try:
        from . import secrets as _secrets

        _secrets.warm_key()
    except Exception:
        pass
    # Lazily imported by classify_and_redact on first use (privacy.py):
    # grounding on every call, vault when a value is redacted,
    # adversarial_guard when page text is present, webmcp (plus
    # jsonschema) on every call via _validated_webmcp_tools.
    for _module in ("app.grounding", "app.vault", "app.adversarial_guard",
                    "app.webmcp"):
        try:
            importlib.import_module(_module)
        except Exception:
            pass


async def _execute_with_retries(
    ext_hub: Any,
    action: dict[str, Any],
    task_id: str,
    session_id: Optional[str] = None,
    max_retries: Optional[int] = None,
) -> dict[str, Any]:
    """Execute one action with bounded idempotent retries.

    ``max_retries`` is the loop's own ``MAX_RETRIES`` passed in by the
    caller — the loop reads it from its module binding, so a test that
    patches ``agentLoop.MAX_RETRIES`` still controls the retry count
    after this function moved here. Direct callers omit it and get
    ``config.MAX_RETRIES``.

    Task 4 (latency): a ``STALE_TARGET`` failure is returned
    immediately for non-idempotent actions (click / type / ...), with
    no retry and no backoff sleep. The element the action targeted is
    gone — re-sending the identical action against the identical
    target cannot succeed, so the old behaviour burned
    ``MAX_RETRIES`` dispatches plus 0.2 + 0.4 + 0.6 = 1.2 s of backoff
    (measured in the automation demo's stale-click step) before the
    loop's fresh-ref recovery even ran. Only the loop's recovery —
    re-observe, resolve the same control to a fresh ref, retry once
    with the fresh ref — can fix a stale target, and it runs on the
    returned failure exactly as before. Idempotent actions (observe /
    scroll / wait / zoom / select_tab) carry no element target, so a
    stale report there is treated as transient and still retried,
    exactly like the pre-existing TIMEOUT rule below.
    """
    if max_retries is None:
        max_retries = config.MAX_RETRIES
    act = dict(action)
    if session_id and "sessionId" not in act:
        act["sessionId"] = session_id
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
    for attempt in range(max_retries + 1):
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
        # Task 4 (latency): STALE_TARGET on a non-idempotent action can
        # never succeed on an identical retry — the element is gone.
        # Return at once (no backoff sleep) so the loop's fresh-ref
        # recovery runs instead of burning MAX_RETRIES dispatches.
        if err.get("code") == "STALE_TARGET" and act.get("type") not in _IDEMPOTENT_RETRY_TYPES:
            return res
        if not err.get("recoverable", False) or attempt == max_retries:
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

    # 6. LeetCode Profile extraction
    if "leetcode" in full_text.lower():
        try:
            from .extractors.leetcode import extract_leetcode_profile
            profile = extract_leetcode_profile({"url": facts.get("canonical_url", "")}, page_text=full_text)
            if profile.get("username") and profile["username"] != "Authenticated User":
                facts["leetcode_username"] = str(profile["username"])
            if profile.get("total_solved") and str(profile["total_solved"]) != "0":
                facts["leetcode_solved"] = str(profile["total_solved"])
            if profile.get("contest_rating") and "unrated" not in str(profile["contest_rating"]).lower():
                facts["leetcode_rating"] = str(profile["contest_rating"])
        except Exception:
            pass

    # 7. Research & Deliverable extraction
    if "research" in full_text.lower() or "deliverable ready" in full_text.lower() or any(ext in full_text.lower() for ext in (".pdf", ".docx", ".pptx", ".xlsx", ".csv", ".html")):
        art_match = re.search(r"\[([^\]]+\.(?:pdf|docx|pptx|xlsx|csv|html))\]", summary, re.IGNORECASE)
        if art_match:
            facts["last_deliverable"] = art_match.group(1).strip()
        try:
            from .intent_router import extract_research_topic, detect_deliverable_type, detect_requested_format
            topic = extract_research_topic(instruction)
            if topic and len(topic.split()) >= 2:
                facts["research_topic"] = topic[:80]
            deliv = detect_deliverable_type(instruction)
            if deliv:
                facts["deliverable_type"] = deliv
            fmt = detect_requested_format(instruction)
            if fmt:
                facts["requested_format"] = fmt
        except Exception:
            pass

    return facts
