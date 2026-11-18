"""Bounded agent control loop: observe → plan → validate → act → verify.

Injectable dependencies (hub, planner, obs_factory) so tests drive the loop
without Chrome. Production wiring (main.py) passes the real hub and planner.
"""
from __future__ import annotations

import asyncio
import atexit
from concurrent.futures import ThreadPoolExecutor
import inspect
import logging
import re
import threading
import time
from typing import Any, Callable, Optional
import urllib.parse
from urllib.parse import urlsplit
from pathlib import Path

from . import config
from .allowlist import check_url, effective_allowlist
from .batch import batch_prefix, is_non_submitting_type
from .checker import check_plan, scan_page_text
from .plan_preview import plan_preview
from .contracts import PrivacyViolation
from .hub import Hub, hub as real_hub, TaskRecord
from .latency import LoopTimings, phase_ms
# Task 13: loop state (cancel / pause / takeover registries) lives in
# loop_state.py; agentLoop re-exports every name so existing imports
# (``agentLoop.request_cancel``, ``agentLoop._pause_events``, ...) keep
# working — the registries are the same objects, not copies.
from .loop_checkpoints import (
    _compact_history,
    _maybe_compact_history,
    _persist_agent_message,
    _persist_derived_memory,
    _prune_tabs_not_in_observation,
    _register_tab_in_memory,
    _remove_tab_from_memory,
    _save_checkpoint_defensive,
    _set_active_tab_in_memory,
    save_cancel_checkpoint,
)
from .loop_gates import (
    HANDOFF_MESSAGE,
    _ConfirmGate,
    _TakeoverGate,
    _gate_message,
    _request_confirmation,
)
from .loop_helpers import (
    CANONICAL_FORMAT_MAP,
    MAX_HISTORY_TEXT_CHARS,
    MAX_TRANSIENT_PLAN_RETRIES,
    _AUTH_HOSTS,
    _AUTH_PATH_TOKENS,
    _FORCE_REOBSERVE_ACTIONS,
    _IDEMPOTENT_RETRY_TYPES,
    _MESSAGING_DOMAINS,
    _actions_match,
    _failure_action_matches,
    _auto_complete_after_send,
    _call_planner,
    _enforce_date_integrity,
    _execute_with_retries,
    _fresh_observation,
    _history_view,
    _host_matches,
    _host_of,
    _origin_of,
    _is_auth_url,
    _is_composer_target,
    _is_messaging_host,
    _is_redundant_completed_action,
    _is_whatsapp_qr,
    _normalize_loop_error,
    _plan_tree_and_refs,
    _same_failure,
    _state_digest,
    _summary,
    _target_name,
    _to_obs,
    canonical_format_map,
    extract_mission_facts,
    fresh_tree_for_observation,
    is_transient_planner_error,
    ref_freshness,
    transient_plan_backoff_s,
    warm_loop_caches,
)
from .loop_research import (
    _finalize_research_artifacts,
    _finalize_research_summary,
    build_comparison_package,
)
from .loop_state import (
    _await_takeover,
    _cancelled,
    _notify_extension_takeover,
    _pause_events,
    _takeover_wait_events,
    is_cancelled,
    request_cancel,
    request_pause,
    request_resume,
)
from .obs_cache import ObservationCache, observation_fingerprint, observation_tab_id, observation_url
from .outcome import (
    is_dialog_error,
    is_stale_target_error,
    resolve_fresh_ref,
    verify_step_outcome,
)
from .planner import plan_from_observation
from .privacy import classify_and_redact, redact_text_urls, redact_url
from .providers.errors import ProviderUnavailable
from .takeover import (
    AUTH_PATH_TOKENS,
    detect_takeover_trigger,
    is_blocked_action,
    is_payment_surface,
    takeover_reason_for_wait,
)
from .vault import (
    contains_placeholder,
    parse_placeholder,
    placeholder_value,
    scrub_value_from_text,
    substitute_placeholders_in_action,
)

logger = logging.getLogger("navigator.loop")

MAX_STEPS = config.MAX_STEPS
# Task 9: long-horizon tasks get a bigger budget (100 steps) and a
# deterministic compaction summary every 25 steps so the prompt stays
# bounded while the history stays useful.
MAX_LONG_STEPS = config.MAX_LONG_STEPS
MAX_RETRIES = config.MAX_RETRIES
TASK_TIMEOUT_S = config.TASK_TIMEOUT_S
PLANNER_TIMEOUT_S = float(getattr(config, "PLANNER_TIMEOUT_S", 60.0))
MAX_CONCURRENT_PLANNERS = int(getattr(config, "MAX_CONCURRENT_PLANNERS", 4))
_planner_executor: Optional[ThreadPoolExecutor] = None
_planner_executor_lock = threading.Lock()


def get_planner_executor() -> ThreadPoolExecutor:
    """Singleton ThreadPoolExecutor bounding concurrent planner executions to MAX_CONCURRENT_PLANNERS."""
    global _planner_executor
    if _planner_executor is None:
        with _planner_executor_lock:
            if _planner_executor is None:
                _planner_executor = ThreadPoolExecutor(
                    max_workers=MAX_CONCURRENT_PLANNERS,
                    thread_name_prefix="privagent-planner",
                )
    return _planner_executor


def shutdown_planner_executor(wait: bool = False, cancel_futures: bool = True) -> None:
    """Cleanly shut down the planner executor and discard queued tasks."""
    global _planner_executor
    with _planner_executor_lock:
        if _planner_executor is not None:
            try:
                _planner_executor.shutdown(wait=wait, cancel_futures=cancel_futures)
            except TypeError:
                _planner_executor.shutdown(wait=wait)
            _planner_executor = None


atexit.register(shutdown_planner_executor)


MAX_NOTES_CHARS = 4000
# A malformed plan must not kill the mission. Give the model a bounded number
# of replans with the exact validation error before failing honestly.
MAX_INVALID_PLANS = 3


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
    takeover_gate: Optional[Any] = None,
    takeover_timeout_s: float = 600.0,
    on_metrics: Optional[Callable[[dict[str, Any]], None]] = None,
    initial_memory: Optional[dict[str, Any]] = None,
    session_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    allowed_domains: Optional[list[str]] = None,
    max_steps: Optional[int] = None,
    long_horizon: bool = False,
) -> dict[str, Any]:
    """Test entrypoint mirroring run_task's flow with injectable parts."""
    # Task 11: per-step timings ride along with the run and are
    # attached to the result (durations + counts + action types only).
    timings = LoopTimings()
    try:
        mem = initial_memory if initial_memory is not None else {}
        res = await _run_loop(
            ext_hub,
            planner=planner,
            obs_factory=obs_factory,
            task_id=task_id,
            instruction=instruction,
            on_progress=on_progress,
            confirm_gate=confirm_gate,
            confirm_timeout_s=confirm_timeout_s,
            takeover_gate=takeover_gate,
            takeover_timeout_s=takeover_timeout_s,
            on_metrics=on_metrics,
            initial_memory=mem,
            session_id=session_id,
            conversation_id=conversation_id,
            allowed_domains=allowed_domains,
            max_steps=max_steps,
            long_horizon=long_horizon,
            timings=timings,
        )
        if isinstance(res, dict) and "working_memory" not in res:
            res["working_memory"] = mem
        if isinstance(res, dict):
            res.setdefault("timings", timings.summary())
        return res
    finally:
        # Never leak cancellation markers: task ids are unique, but a stale
        # marker would make a replayed id silently cancel itself.
        _cancelled.discard(task_id)
        _pause_events.pop(task_id, None)
        _takeover_wait_events.pop(task_id, None)


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
    takeover_gate: Optional[Any] = None,
    takeover_timeout_s: float = 600.0,
    on_metrics: Optional[Callable[[dict[str, Any]], None]] = None,
    initial_memory: Optional[dict[str, Any]] = None,
    session_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    allowed_domains: Optional[list[str]] = None,
    max_steps: Optional[int] = None,
    long_horizon: bool = False,
    timings: Optional[LoopTimings] = None,
) -> dict[str, Any]:
    # Task 4 (latency): pay the loop's one-time per-process costs now —
    # Fernet key derivation + the redaction modules' first imports —
    # before the first step, so step 1 does not look ~200 ms slower
    # than every later step. Best-effort and ~0 ms once warm; a failure
    # just means the first step pays the cost as before.
    warm_loop_caches()
    history: list[dict[str, Any]] = []
    if initial_memory and initial_memory.get("prior_checkpoint_history"):
        history = list(initial_memory["prior_checkpoint_history"])
    penalties: list[str] = []
    working_memory: dict[str, Any] = initial_memory if initial_memory is not None else {}
    # Task 9: step budget. A long-horizon task gets MAX_LONG_STEPS
    # (100) with compaction; everything else gets MAX_STEPS (50).
    # An explicit max_steps (tests / scheduler) wins over both.
    effective_max_steps = max_steps if max_steps is not None else (MAX_LONG_STEPS if long_horizon else MAX_STEPS)
    working_memory["long_horizon"] = bool(long_horizon)
    working_memory["max_steps"] = effective_max_steps
    # REVIEW (Task 9 audit): the instruction must be in the checkpoint
    # or a resumed task loses it. main._require restores a task from
    # SQLite as "interrupted" and reads the instruction from the
    # checkpoint's working memory — but the loop never stored it there
    # (only save_cancel_checkpoint did), so a task interrupted by a
    # backend restart resumed with instruction "Task" and planned
    # nonsense. setdefault: a restored checkpoint's instruction wins
    # when one is already present.
    working_memory.setdefault("instruction", instruction)
    # Attachment awareness: a file the user attached to this mission is
    # stored in memory (file_uploads) and is only ever handed to the
    # extension for an `upload_file` action — but the planner was never
    # told it exists, so it could never plan to use it. Record the file
    # NAME + kind (metadata only, never the bytes) in working memory so
    # build_prompt can tell the planner. The bytes stay out of the
    # prompt, logs and SQLite by design (file_uploads docstring).
    if "attachment" not in working_memory:
        try:
            _task = real_hub.tasks.get(task_id)
            _token = getattr(_task, "file_token", None) or ""
            if _token:
                from .file_uploads import get as _get_attachment

                _att = _get_attachment(_token)
                if _att:
                    _fname = _att[0]
                    _kind = _fname.rsplit(".", 1)[-1].lower() if "." in _fname else ""
                    working_memory["attachment"] = {"fileName": _fname, "kind": _kind}
        except Exception:
            pass
    # Task 3: the effective allow-list travels in working memory so the
    # planner providers can state it in the prompt (build_prompt) and the
    # loop can enforce it in code. None = not configured (allow all,
    # warned at task creation).
    effective_domains = allowed_domains if allowed_domains is not None else None
    if effective_domains is not None:
        working_memory["allowed_domains"] = list(effective_domains)

    # Give plan validation the session's pinned primary tab so close_tab on it
    # can be rejected deterministically.
    if session_id and "primary_tab_id" not in working_memory:
        try:
            from .session_manager import session_manager
            sess0 = session_manager.get_session(session_id)
            if sess0 and sess0.primary_tab_id is not None:
                working_memory["primary_tab_id"] = sess0.primary_tab_id
        except Exception:
            pass
    # Task 9: seed the tab registry with the primary tab so the
    # planner's OPEN TABS list is never empty and switch_tab has
    # something to validate against.
    if working_memory.get("primary_tab_id") is not None and not working_memory.get("tabs"):
        _register_tab_in_memory(
            working_memory, int(working_memory["primary_tab_id"]), url="", title="primary", active=True
        )
    try:
        from .extractors.research import init_multi_site_tracker
        ms_tracker = init_multi_site_tracker(instruction, working_memory.get("multi_site_research"))
        if ms_tracker:
            working_memory["multi_site_research"] = ms_tracker
    except Exception:
        pass
    if working_memory.get("is_research") and "research_knowledge_package" not in working_memory:
        try:
            from .research.models import ResearchKnowledgePackage, ResearchTopic
            working_memory["research_knowledge_package"] = ResearchKnowledgePackage(
                package_id=f"pkg_{task_id}",
                topic=ResearchTopic(
                    title=working_memory.get("research_topic") or instruction,
                    objective=instruction,
                    target_format=working_memory.get("requested_format"),
                ),
            ).to_dict()
        except Exception:
            pass
    steps_done = len(history)
    invalid_plans = 0
    start = time.monotonic()
    # A successful action already returns the post-action observation from the
    # extension. Replanning on that snapshot removes one full observe round
    # trip per step. It is revalidated and re-redacted before every prompt.
    cached_obs: Any = None
    last_digest: Optional[str] = None
    zero_change_repeats = 0
    # Task 11: latency instrumentation, observation cache, batching.
    # ``timings`` is created by run_with_injection (and attached to the
    # result); a direct _run_loop caller gets a fresh one.
    if timings is None:
        timings = LoopTimings()
    obs_cache = ObservationCache()
    # Last observed tab+URL, for cache lookups. Updated whenever an
    # observation is stored; reset when the active surface changes
    # out from under the cache (tab switch/close).
    last_obs_url: str = ""
    last_obs_tab_id: Optional[int] = None
    # Pinned batch (Task 11): the remaining steps of an already
    # validated plan, with the observation + AX tree they were built
    # from. Consumed (set to None) at the top of the next iteration,
    # so any mid-iteration ``continue`` re-plans instead of resuming
    # a stale batch.
    pending_batch: Optional[dict[str, Any]] = None
    # The AX tree + valid refs + URL of the observation the current
    # plan was built from. Built once per plan, not once per step.
    plan_tree: Any = None
    plan_refs: Optional[set[str]] = None
    plan_url: str = ""
    # REVIEW (Task 11 audit): the DOM fingerprint of the observation
    # the current plan was built from. The batch gate compares the
    # post-action observation against it — a type step can mutate the
    # DOM (autocomplete, validation, dynamic content) without changing
    # the URL, and a batch pinned to the stale tree would execute on
    # elements the planner never saw. None until a plan is built.
    plan_fingerprint: Optional[str] = None
    # Task 7: stale element refs. A ref that no longer resolves against
    # the current observation gets ONE re-observe + replan; a second
    # stale ref in a row fails honestly instead of looping forever.
    grounding_stale_retries = 0
    url_history: list[str] = []
    last_site_name: Optional[str] = None

    def progress(msg: str, screenshot: Optional[str] = None, observation: Any = None) -> None:
        if not on_progress:
            return
        try:
            on_progress(steps_done, msg, screenshot=screenshot, observation=observation)
        except TypeError:
            try:
                on_progress(steps_done, msg, screenshot=screenshot)
            except TypeError:
                try:
                    on_progress(steps_done, msg)
                except TypeError:
                    on_progress()

    def metrics(phase: str, started: float) -> None:
        """Report one phase's wall time. Never lets telemetry break the loop."""
        ms = phase_ms(started)
        try:
            # Task 11: "act" is the execution phase; the timings
            # record keeps the "execute" name for readability.
            timings.record({"act": "execute"}.get(phase, phase), ms)
        except Exception:
            pass
        if not on_metrics:
            return
        try:
            on_metrics({"phase": phase, "ms": ms})
        except Exception:
            pass

    # -- Task 4: takeover state -------------------------------------------
    # The first observation after a completed takeover carries no
    # screenshot: the operator may have left a password, card, or OTP
    # visible on screen, and a screenshot would send it to the planner.
    post_takeover_no_screenshot = False
    suppress_step_screenshot = False

    async def _enter_takeover(trigger: Any) -> str:
        """Enter takeover for *trigger*.

        Returns ``"completed"`` (operator finished, resume the loop),
        ``"already_done"`` (this surface was already taken over once —
        proceed without taking over again), ``"exhausted"`` (the task
        already used its takeover budget on new surfaces), ``"no_gate"``
        (no takeover gate exists — fail closed), ``"rejected"`` or
        ``"timeout"``. On entry the cached observation is discarded
        and the extension is told to stop observing/screenshotting.
        """
        nonlocal cached_obs, last_digest, post_takeover_no_screenshot
        completed_keys: list[str] = working_memory.setdefault("takeover_completed", [])
        count = int(working_memory.get("takeover_count", 0))
        if trigger.key in completed_keys:
            return "already_done"
        if count >= config.MAX_TAKEOVERS_PER_TASK:
            return "exhausted"
        if takeover_gate is None:
            return "no_gate"
        cached_obs = None
        last_digest = None
        # Task 11: the operator is about to act in the browser — the
        # cached observation is for a page that is about to change.
        obs_cache.invalidate()
        await _notify_extension_takeover(ext_hub, task_id, True)
        # Task 10: audit — takeover start (kind + reason only; the
        # reason names the surface, never its contents).
        try:
            from .audit import append_event as _audit_event

            _audit_event("takeover_started", f"takeover started: {trigger.kind}",
                         actor="system", task_id=task_id)
        except Exception:
            pass
        progress(f"Takeover: {trigger.reason}")
        takeover_started = time.perf_counter()
        outcome = await _await_takeover(takeover_gate, task_id, trigger, takeover_timeout_s)
        metrics("takeover_wait", takeover_started)
        await _notify_extension_takeover(ext_hub, task_id, False)
        # Task 10: audit — takeover end with the outcome.
        try:
            from .audit import append_event as _audit_event2

            _audit_event2("takeover_ended", f"takeover ended: {outcome} ({trigger.kind})",
                          actor="system", task_id=task_id)
        except Exception:
            pass
        if outcome == "completed":
            working_memory["takeover_count"] = count + 1
            completed_keys.append(trigger.key)
            post_takeover_no_screenshot = True
        return outcome

    def _takeover_failure_summary(outcome: str, trigger: Any) -> str:
        """Honest failure message for a takeover that did not complete.

        Returns only the message — the call sites build the dict literal
        themselves, because the verifier-honesty test requires every
        ``_run_loop`` return to be a dict literal carrying ``history``.
        """
        if outcome == "exhausted":
            return (
                f"Task incomplete: the task already used its takeover budget "
                f"({config.MAX_TAKEOVERS_PER_TASK} takeovers) and a new "
                f"{trigger.kind} surface appeared — stopping instead of "
                f"looping on manual steps."
            )
        if outcome == "no_gate":
            return (
                f"Task incomplete: a {trigger.kind} step requires takeover, "
                f"but no takeover gate is available — failing closed instead "
                f"of typing credentials or a one-time code."
            )
        if outcome == "timeout":
            return (
                f"Task incomplete: takeover timed out after "
                f"{int(takeover_timeout_s)}s — the operator did not finish "
                f"the {trigger.kind} step, so the task stopped. Nothing was "
                f"typed or submitted during takeover."
            )
        # rejected (including a cancel during takeover)
        return (
            f"Task stopped: the operator cancelled the {trigger.kind} "
            f"takeover before any credential or payment step."
        )

    while steps_done < effective_max_steps:
        pause_event = _pause_events.get(task_id)
        if pause_event is not None and not pause_event.is_set():
            pause_started = time.monotonic()
            await pause_event.wait()
            start += time.monotonic() - pause_started
            # Task 11: the operator may have acted in the browser
            # while paused — the cached observation is untrusted now.
            obs_cache.invalidate()
        if is_cancelled(task_id):
            return {"final": "cancelled", "steps": steps_done, "summary": None, "history": history}
        if time.monotonic() - start > TASK_TIMEOUT_S:
            return {"final": "budget_exceeded", "steps": steps_done, "summary": "timeout", "history": history}

        # Task 11: batch continuation reuses the pinned plan (no new
        # observation, no new planner call); anything else re-plans
        # from a fresh observation.
        timings.begin_step()
        batch_ctx = pending_batch
        pending_batch = None
        batch_continuation = False
        # Task 12: reset per iteration — see the confirmation block
        # below, which sets it only for the step just approved.
        step_only_approval = False
        if batch_ctx is not None and batch_ctx.get("steps"):
            steps = batch_ctx["steps"]
            payload = batch_ctx["payload"]
            raw_obs = batch_ctx["raw_obs"]
            plan_tree = batch_ctx["tree"]
            plan_refs = batch_ctx["refs"]
            plan_url = batch_ctx["url"]
            # REVIEW (Task 11 audit): the fingerprint of the plan
            # observation rides along with the pinned batch, so the
            # gate at the end of the iteration can tell whether the
            # page still looks like the one the plan was built from.
            plan_fingerprint = batch_ctx.get("fingerprint")
            batch_continuation = True
            timings.note_cache("n/a")
        elif cached_obs is not None:
            raw_obs = cached_obs
            cached_obs = None
            timings.note_cache("n/a")
        else:
            cached = obs_cache.lookup(last_obs_url, last_obs_tab_id)
            if cached is not None:
                # No action executed since this observation was stored,
                # so the page cannot have changed because of the agent
                # — reuse it without a round trip. It is still
                # re-redacted below, exactly like the loop's existing
                # post-action observation reuse.
                raw_obs = cached
                timings.note_cache("hit")
            else:
                observe_started = time.perf_counter()
                _maybe = obs_factory()
                raw_obs = await _maybe if asyncio.iscoroutine(_maybe) else _maybe
                metrics("observe", observe_started)
                timings.note_observation_call()
                timings.note_cache("miss")
                # If the cache held an entry for this tab+URL with a
                # different fingerprint, the page changed while the
                # cache was invalid — the miss was necessary.
                if obs_cache.has_entry_for(observation_url(raw_obs), observation_tab_id(raw_obs)) and not obs_cache.check_fresh(raw_obs):
                    timings.note_stale_detected()
                obs_cache.store(raw_obs)
                last_obs_url = observation_url(raw_obs)
                last_obs_tab_id = observation_tab_id(raw_obs)
        # Tab registry sync: the tab the loop is OBSERVING is ground
        # truth for "a tab this task owns". The registry previously
        # only learned about tabs from `new_tab` actions that returned
        # an observation tabId — a tab opened any other way (a click
        # on a link that opens a new tab, a PDF opened in a new tab)
        # never registered, so the next select_tab to it was rejected
        # as "unknown tab" even though the planner had learned the id
        # from the observation itself (live Flipkart run 2026-11-18:
        # product page opened in a new tab, then "unknown tab" on the
        # switch). Register the observed tab here, every iteration,
        # idempotently; the gate below still rejects tabs the task has
        # never observed, so the operator's personal tabs stay out of
        # reach. Membership only — the active marker is NOT touched
        # here: it is owned by the post-action sync (a new_tab /
        # switch moves it), and stealing it on every observation
        # would mark the last-observed tab active even when the task
        # just switched elsewhere. The session's child registry is
        # synced the same way so the persisted session record matches.
        _observed_tab_id = observation_tab_id(raw_obs)
        if _observed_tab_id is not None:
            try:
                _register_tab_in_memory(
                    working_memory, int(_observed_tab_id),
                    url=observation_url(raw_obs) or "",
                )
            except (TypeError, ValueError):
                pass
            if session_id:
                try:
                    from .session_manager import session_manager
                    _sess = session_manager.get_session(session_id)
                    if _sess and int(_observed_tab_id) != _sess.primary_tab_id:
                        session_manager.register_child_tab(session_id, int(_observed_tab_id))
                except Exception:
                    pass
            # Prune closed tabs: the observation carries the task's
            # open tabs (``tabs``). A tab the operator closed is gone
            # from that list but would otherwise stay in the registry
            # as a zombie the select_tab gate lets through. Only a
            # fresh snapshot prunes (non-empty, containing the
            # observed tab); a missing list prunes nothing. The
            # primary tab is never pruned here.
            try:
                _obs_tabs = raw_obs.get("tabs") if isinstance(raw_obs, dict) else getattr(raw_obs, "tabs", None)
            except Exception:
                _obs_tabs = None
            if isinstance(_obs_tabs, list) and _obs_tabs:
                _open_ids: list[int] = []
                for _t in _obs_tabs:
                    if isinstance(_t, dict):
                        _tid = _t.get("tabId", _t.get("tab_id"))
                        if isinstance(_tid, int):
                            _open_ids.append(_tid)
                if int(_observed_tab_id) in _open_ids:
                    _pruned = _prune_tabs_not_in_observation(
                        working_memory, _open_ids,
                        observed_tab_id=int(_observed_tab_id),
                        primary_tab_id=working_memory.get("primary_tab_id"),
                    )
                    if _pruned and session_id:
                        try:
                            from .session_manager import session_manager
                            for _pid in _pruned:
                                session_manager.unregister_child_tab(session_id, int(_pid))
                        except Exception:
                            pass
        try:
            if batch_continuation:
                # The pinned payload is already redacted; re-running
                # redaction would be pure overhead.
                pass
            else:
                redact_started = time.perf_counter()
                payload, _decision = classify_and_redact(_to_obs(raw_obs), instruction=instruction)
                metrics("redact", redact_started)
            if post_takeover_no_screenshot:
                # Task 4: the first observation after takeover must not
                # carry a screenshot — the operator may have left a
                # password, card, or OTP visible on screen. The
                # redacted payload is the planner's view; the raw
                # screenshot is dropped from the UI frame as well, and
                # the step's own screenshot is suppressed below.
                payload.screenshot = None
                post_takeover_no_screenshot = False
                suppress_step_screenshot = True
                if isinstance(raw_obs, dict):
                    raw_obs["screenshot"] = None
                else:
                    try:
                        raw_obs.screenshot = None
                    except Exception:
                        pass
            else:
                # Task 10: zero retention — no screenshot ever reaches
                # the planner, so a rendered page (which may show a
                # password, card, or OTP the DOM redactor never saw)
                # is never sent to a provider or persisted.
                try:
                    from .zero_retention import is_enabled as _zr_enabled

                    if _zr_enabled():
                        from .zero_retention import strip_screenshot as _zr_strip

                        _zr_strip(payload, raw_obs)
                        suppress_step_screenshot = True
                except Exception:
                    pass
            progress("", observation=raw_obs)
        except PrivacyViolation:
            return {"final": "failed", "steps": steps_done, "summary": "privacy violation — payload not sent", "history": history}

        raw_url = str(
            getattr(raw_obs, "url", None)
            or (raw_obs.get("url") if isinstance(raw_obs, dict) else "")
            or ""
        )
        raw_title = str(
            getattr(raw_obs, "title", None)
            or (raw_obs.get("title") if isinstance(raw_obs, dict) else "")
            or ""
        )
        raw_text = str(
            getattr(raw_obs, "page_text", None)
            or (raw_obs.get("page_text") if isinstance(raw_obs, dict) else "")
            or getattr(raw_obs, "readable_text", None)
            or (raw_obs.get("readable_text") if isinstance(raw_obs, dict) else "")
            or ""
        )
        # Product image the page declares via og:image. The extension
        # sends it as camelCase "ogImage" in the raw dict (or as the
        # og_image attribute on a validated BrowserObservation); the
        # extractor re-validates it before it reaches any fact.
        raw_image = str(
            getattr(raw_obs, "og_image", None)
            or (raw_obs.get("ogImage") if isinstance(raw_obs, dict) else "")
            or ""
        )
        # -- Task 14: standards state for this observation ----------------
        # WebMCP tools ride the redacted payload (validated at
        # redaction time); the UCP profile is fetched here — same
        # origin only, egress-gated, cached per host for the task —
        # and both land in working_memory so validation and the prompt
        # can see them. The planner never sees a profile the loop did
        # not fetch and validate itself. This sits AFTER raw_url is
        # computed: the stash needs the current page's origin, and on
        # the first iteration raw_url does not exist yet above.
        if not batch_continuation:
            # WebMCP tools: always stashed (a UCP fetch failure below
            # must never drop them — the two are independent).
            try:
                working_memory["webmcp_tools"] = list(getattr(payload, "webmcp_tools", None) or [])
                working_memory["webmcp_origin"] = _origin_of(raw_url)
            except Exception:
                working_memory["webmcp_tools"] = []
                working_memory["webmcp_origin"] = ""
            # UCP profile: fetched per origin, cached for the task. A
            # missing profile URL means the merchant has no UCP
            # checkout. A FAILED fetch is different: the merchant
            # declared a profile URL but it could not be read (DNS,
            # egress, HTTP, invalid JSON) — record the reason instead
            # of silently treating it as "no UCP checkout".
            try:
                _page_origin = _origin_of(raw_url)
                _ucp_url = getattr(payload, "ucp_profile_url", None)
                if _ucp_url and _origin_of(_ucp_url) == _page_origin:
                    _ucp_cache = working_memory.setdefault("ucp_profiles", {})
                    if _page_origin not in _ucp_cache:
                        from .ucp import fetch_ucp_profile

                        _fetched = await asyncio.to_thread(
                            fetch_ucp_profile, _page_origin
                        )
                        # Cache the plain-dict form, not the UCPProfile
                        # object: working_memory is checkpointed as JSON,
                        # and a dataclass would break serialization.
                        _ucp_cache[_page_origin] = (
                            _fetched.to_dict() if _fetched is not None else None
                        )
                    _prof = _ucp_cache.get(_page_origin)
                    payload.ucp_profile = _prof
                    working_memory["ucp_profile"] = _prof
                    working_memory["ucp_profile_error"] = None
                    payload.ucp_profile_error = None
                else:
                    working_memory["ucp_profile"] = None
                    working_memory["ucp_profile_error"] = None
                    payload.ucp_profile_error = None
            except Exception as exc:
                working_memory["ucp_profile"] = None
                working_memory["ucp_profile_error"] = str(exc)[:200] or "UCP profile fetch failed"
                payload.ucp_profile_error = working_memory["ucp_profile_error"]
        # -- Task 4: takeover detection -----------------------------------
        # A login / payment / OTP / CAPTCHA surface must never be planned
        # on: the observation that detected it is the last one the agent
        # sees until the operator finishes the step themselves. The
        # planner is not called on this round at all.
        _trigger = detect_takeover_trigger(raw_url, payload.interactive, raw_text)
        # Takeover needs a gate. Production (run_task) always wires one;
        # a caller without one (e.g. a harness driving only the
        # confirmation gate) keeps the pre-Task 4 behaviour: the planner
        # plans on, and the confirmation gate halts consequential
        # actions. Failing closed here would break that legitimate flow.
        # Task 11: a pinned batch step plans from the same observation
        # this check already ran on — re-running it cannot fire.
        if not batch_continuation and _trigger is not None and takeover_gate is not None:
            _outcome = await _enter_takeover(_trigger)
            if _outcome == "completed":
                continue
            if _outcome == "already_done":
                pass  # already taken over once for this surface: proceed
            elif is_cancelled(task_id):
                return {"final": "cancelled", "steps": steps_done, "summary": None, "history": history}
            else:
                _save_checkpoint_defensive(task_id, conversation_id, session_id, steps_done, "", history, working_memory)
                return {"final": "failed", "steps": steps_done, "summary": _takeover_failure_summary(_outcome, _trigger), "history": history}
        # Task 11: a pinned batch step reads the same observation the
        # research extraction already ran on — nothing new to harvest.
        if not batch_continuation and raw_url and raw_text:
            try:
                from .extractors.research import extract_page_research, update_multi_site_tracker
                fact = extract_page_research(raw_url, raw_title, raw_text, image_url=raw_image or None)
                if fact:
                    if "collected_research" not in working_memory:
                        working_memory["collected_research"] = {}
                    working_memory["collected_research"][fact["domain"]] = fact
                if "multi_site_research" in working_memory:
                    working_memory["multi_site_research"] = update_multi_site_tracker(
                        working_memory["multi_site_research"],
                        raw_url,
                        raw_title,
                        raw_text,
                        working_memory.get("collected_research", {}),
                    )
                if working_memory.get("is_research"):
                    from .research.collector import harvest_page_evidence, is_search_results_page, extract_substantive_links_from_search
                    from .research.models import ResearchKnowledgePackage

                    pkg_data = working_memory.get("research_knowledge_package")
                    if pkg_data:
                        pkg = ResearchKnowledgePackage.from_dict(pkg_data) if isinstance(pkg_data, dict) else pkg_data
                        harvest_page_evidence(raw_url, raw_title, raw_text, pkg)
                        working_memory["research_knowledge_package"] = pkg.to_dict()
                        if is_search_results_page(raw_url, raw_title, raw_text):
                            interactive = getattr(raw_obs, "interactive", []) if raw_obs else []
                            links = extract_substantive_links_from_search(raw_url, interactive, raw_text)
                            if links:
                                if "pending_research_links" not in working_memory:
                                    working_memory["pending_research_links"] = []
                                for lk in links:
                                    url = lk.get("url") if isinstance(lk, dict) else lk
                                    if url and url not in working_memory["pending_research_links"]:
                                        working_memory["pending_research_links"].append(url)
            except Exception:
                pass
        current_digest = _state_digest(raw_obs)

        h_view = _history_view(history, penalties, working_memory)
        payload_dict = payload.model_dump() if hasattr(payload, "model_dump") else payload
        # Planner providers are synchronous HTTP clients. Run them in a bounded
        # ThreadPoolExecutor so cancel/confirm endpoints stay responsive during a
        # slow model call, and active worker threads cannot exceed MAX_CONCURRENT_PLANNERS.
        raw_plan = None
        plan_started = time.perf_counter()
        if is_cancelled(task_id):
            return {"final": "cancelled", "steps": steps_done, "summary": None, "history": history}
        planner_executor = get_planner_executor()
        loop = asyncio.get_running_loop()

        plan_deadline = time.monotonic() + PLANNER_TIMEOUT_S
        MAX_PLAN_RETRIES = 2
        # The attempt loop covers the transient budget too: non-transient
        # errors still stop after MAX_PLAN_RETRIES (the fail check
        # below), transient ones get MAX_TRANSIENT_PLAN_RETRIES.
        for plan_attempt in range(max(MAX_PLAN_RETRIES, MAX_TRANSIENT_PLAN_RETRIES)):
            if batch_continuation:
                # Task 11: no planner call — execute the next step of
                # the already-validated plan. The stub keeps the shared
                # post-plan code (notes, completion paths) working.
                raw_plan = {"goal": instruction, "steps": steps}
                break
            time_left = plan_deadline - time.monotonic()
            if time_left <= 0 and plan_attempt > 0:
                _save_checkpoint_defensive(task_id, conversation_id, session_id, steps_done, raw_url, history, working_memory)
                return {
                    "final": "failed",
                    "steps": steps_done,
                    "summary": f"Planner timed out: shared planning deadline ({int(PLANNER_TIMEOUT_S)}s) expired (step {steps_done} state preserved).", "history": history,
                }
            try:
                timings.note_planner_call()
                raw_plan = await asyncio.wait_for(
                    loop.run_in_executor(
                        planner_executor,
                        _call_planner,
                        planner,
                        instruction,
                        payload_dict,
                        h_view,
                        working_memory,
                        plan_deadline,
                    ),
                    timeout=max(0.001, time_left),
                )
                break
            except asyncio.TimeoutError:
                _save_checkpoint_defensive(task_id, conversation_id, session_id, steps_done, raw_url, history, working_memory)
                return {
                    "final": "failed",
                    "steps": steps_done,
                    "summary": f"Planner timed out: model provider did not respond within {int(PLANNER_TIMEOUT_S)}s deadline (step {steps_done} state preserved).", "history": history,
                }
            except ValueError as exc:
                invalid_plans += 1
                reason = redact_text_urls(str(exc))[:300]
                penalties.append(
                    f"invalid plan ({reason}); re-emit a valid plan. Every click/type "
                    "needs target.targetId or target.name from the page context."
                )
                if invalid_plans >= MAX_INVALID_PLANS:
                    _save_checkpoint_defensive(task_id, conversation_id, session_id, steps_done, raw_url, history, working_memory)
                    return {
                        "final": "failed",
                        "steps": steps_done,
                        "summary": f"Task incomplete: the planner could not produce a valid action ({reason}).", "history": history,
                    }
                progress(f"Replanning after invalid plan: {reason}")
                break
            except ProviderUnavailable as exc:
                err_text = str(exc)
                retry_s = 0.0
                try:
                    from .providers.limits import parse_retry_seconds
                    retry_s = parse_retry_seconds(err_text)
                except Exception:
                    pass

                # Transient network/DNS blips (the gateway unreachable)
                # earn more attempts with a growing backoff (2s, 5s,
                # 10s): the old 2-attempt / 1.5s loop killed a live
                # price hunt on a DNS blip that lasted a few seconds.
                # Auth, quota and plan-shape failures keep the old
                # fast-fail behaviour.
                is_transient = is_transient_planner_error(err_text)
                limit = MAX_TRANSIENT_PLAN_RETRIES if is_transient else MAX_PLAN_RETRIES

                # If cooldown is long (> 3.0s) or explicitly in quota exhaustion, do not repeatedly call providers still in cooldown
                is_long_cooldown = retry_s > 3.0 or "quota exhausted" in err_text or "1m" in err_text
                if is_long_cooldown or plan_attempt + 1 >= limit:
                    _save_checkpoint_defensive(task_id, conversation_id, session_id, steps_done, raw_url, history, working_memory)
                    # Error attribution: name the failing hop. A
                    # planner-call failure says nothing about the
                    # requested sites — no browser navigation was
                    # attempted in this step, so the sites were never
                    # tested and must not be blamed for it.
                    suffix = (
                        " The planner call itself failed (the model gateway could not be reached); "
                        "this says nothing about the requested sites, which were not tested."
                        if is_transient else ""
                    )
                    return {
                        "final": "failed",
                        "steps": steps_done,
                        "summary": f"Planner unavailable: {redact_text_urls(err_text)[:400]} (step {steps_done} state preserved).{suffix}", "history": history,
                    }

                if is_transient:
                    wait_time = max(retry_s + 0.5, transient_plan_backoff_s(plan_attempt))
                else:
                    wait_time = max(retry_s + 0.5, 1.5)
                # Never sleep past the shared planning deadline: the
                # next iteration reports the timeout instead.
                time_left = plan_deadline - time.monotonic()
                wait_time = min(wait_time, max(0.0, time_left))
                progress(f"Provider unavailable; retrying ({plan_attempt + 1}/{limit})...")
                await asyncio.sleep(wait_time)
                continue
            finally:
                metrics("plan", plan_started)
        if is_cancelled(task_id):
            return {"final": "cancelled", "steps": steps_done, "summary": None, "history": history}
        if raw_plan is None:
            if invalid_plans > 0:
                continue
            return {"final": "failed", "steps": steps_done, "summary": "planner returned a non-object plan", "history": history}
        invalid_plans = 0
        if not isinstance(raw_plan, dict):
            return {"final": "failed", "steps": steps_done, "summary": "planner returned a non-object plan", "history": history}

        new_notes = raw_plan.get("notes")
        if new_notes:
            existing = working_memory.get("notes")
            if existing and existing != str(new_notes) and str(new_notes) not in existing:
                working_memory["notes"] = f"{existing}\n- {new_notes}"[:MAX_NOTES_CHARS]
            else:
                working_memory["notes"] = str(new_notes)[:MAX_NOTES_CHARS]

        steps = raw_plan.get("steps", [])
        if not steps:
            final_ans = raw_plan.get("answer") or raw_plan.get("summary")
            summary_msg = final_ans if final_ans else _summary(raw_obs, history)
            ms_tracker = working_memory.get("multi_site_research")
            if ms_tracker:
                try:
                    from .extractors.research import is_multi_site_task_complete, get_pending_sites, format_comparison_table, extract_target_product_query
                    if not is_multi_site_task_complete(ms_tracker):
                        pending = get_pending_sites(ms_tracker)
                        if pending:
                            penalties.append(
                                f"Multi-site task incomplete: {len(pending)} websites remaining to compare: {', '.join(s['name'] for s in pending)}. Do not conclude yet."
                            )
                            next_s = pending[0]
                            cur_host = (urlsplit(raw_url).hostname or "").lower()
                            is_on_next_s = cur_host and any(d in cur_host for d in next_s.get("domains", []))
                            if is_on_next_s:
                                interactive = getattr(raw_obs, "interactive", []) if raw_obs else []
                                search_el = None
                                for el in (interactive or []):
                                    name = str(getattr(el, "name", "") or (el.get("name") if isinstance(el, dict) else "")).lower()
                                    role = str(getattr(el, "role", "") or (el.get("role") if isinstance(el, dict) else "")).lower()
                                    if role in ("searchbox", "combobox") or "search" in name:
                                        search_el = el
                                        break
                                query = extract_target_product_query(instruction)
                                if search_el and query:
                                    t_id = getattr(search_el, "target_id", None) or (search_el.get("targetId") if isinstance(search_el, dict) else "search")
                                    t_name = getattr(search_el, "name", None) or (search_el.get("name") if isinstance(search_el, dict) else "Search")
                                    action = {
                                        "type": "type",
                                        "risk": "write",
                                        "target": {"targetId": t_id, "name": t_name},
                                        "params": {"text": query, "submit": True},
                                        "verify": f"searched for {query}",
                                        "timeoutMs": 15000,
                                    }
                                    first = {"why": f"On {next_s['name']}: searching for '{query}'", "action": action}
                                    steps = [first]
                                else:
                                    next_s["status"] = "unavailable"
                                    next_s["reason"] = f"No search input or listing found on {next_s['name']}"
                                    remaining = get_pending_sites(ms_tracker)
                                    if remaining:
                                        rem = remaining[0]
                                        action = {
                                            "type": "navigate",
                                            "risk": "write",
                                            "params": {"url": rem["default_url"]},
                                            "verify": f"navigated to {rem['name']}",
                                            "timeoutMs": 15000,
                                        }
                                        first = {"why": f"Moving to next comparison site: navigating to {rem['name']}", "action": action}
                                        steps = [first]
                            else:
                                first = {
                                    "why": f"Continuing multi-site comparison: navigating to {next_s['name']}",
                                    "action": {
                                        "type": "navigate",
                                        "risk": "write",
                                        "params": {"url": next_s["default_url"]},
                                        "verify": f"navigated to {next_s['name']}",
                                        "timeoutMs": 15000,
                                    },
                                }
                                steps = [first]
                                action = first["action"]
                        else:
                            return {
                                "final": "failed",
                                "steps": steps_done,
                                "summary": summary_msg or "Task incomplete: requested comparison websites were not all visited.",
                                "history": history, "url": raw_url, "last_obs": raw_obs,
                            }
                    else:
                        comp_table = format_comparison_table(ms_tracker, instruction)
                        if comp_table and "Price & Availability Comparison" not in summary_msg:
                            summary_msg = f"{summary_msg}\n\n{comp_table}" if summary_msg else comp_table
                        # Fix 5: after a comparison with verified
                        # listings finishes, proactively offer the
                        # alert (live case: the Fit3 verdict never
                        # asked "do you want me to alert you?", so
                        # the user never reached the daily digest).
                        # No offer when nothing was verified — the
                        # verifier already fails that turn honestly.
                        from .continuous_monitoring_companion import ALERT_OFFER_MARKER, ALERT_OFFER_TEXT
                        from .extractors.research import get_completed_sites
                        if get_completed_sites(ms_tracker) and ALERT_OFFER_MARKER not in summary_msg:
                            summary_msg = f"{summary_msg}\n\n{ALERT_OFFER_TEXT}" if summary_msg else ALERT_OFFER_TEXT
                except Exception:
                    pass
            if not steps:
                if raw_plan.get("blocked"):
                    return {"final": "failed", "steps": steps_done,
                            "summary": summary_msg or "Task incomplete: the result could not be verified.",
                            "history": history, "url": raw_url, "last_obs": raw_obs, "working_memory": working_memory}

                if working_memory.get("is_research"):
                    from .research.collector import is_search_results_page
                    inst_lower = (instruction or "").lower()
                    requires_multi = any(k in inst_lower for k in ["multiple", "compare", "credible sources", "independent sources", "different sources", "cross-reference"])

                    pkg_data = working_memory.get("research_knowledge_package") or {}
                    sources = pkg_data.get("sources") or {}
                    substantive_domains = set()
                    for s in sources.values():
                        s_url = s.get("url", "")
                        dom = s.get("domain", "")
                        if dom and not is_search_results_page(s_url):
                            parts = dom.lower().split(".")
                            root_dom = ".".join(parts[-2:]) if len(parts) >= 2 else dom.lower()
                            substantive_domains.add(root_dom)

                    pending_links = list(working_memory.get("pending_research_links") or [])
                    is_on_search = is_search_results_page(raw_url, raw_title, raw_text)

                    from .research.adaptive_planner import (
                        ResearchPlan,
                        detect_evidence_gaps,
                        generate_targeted_gap_queries,
                    )
                    plan_data = working_memory.get("research_plan")
                    plan = ResearchPlan.model_validate(plan_data) if plan_data else None
                    min_required_sources = plan.min_sources if plan else (2 if requires_multi else 1)

                    # 1. If currently on a search page and we have pending extracted article links:
                    if is_on_search and pending_links:
                        next_url = pending_links.pop(0)
                        working_memory["pending_research_links"] = pending_links
                        first = {
                            "why": f"Navigating from search results to substantive article: {next_url}",
                            "action": {
                                "type": "navigate",
                                "risk": "write",
                                "params": {"url": next_url},
                                "verify": "navigated to substantive research article",
                                "timeoutMs": 15000,
                            },
                        }
                        steps = [first]

                    # 2. Dynamic source requirement (adapts to simple=1, standard=2, complex=3+):
                    elif len(substantive_domains) < min_required_sources:
                        if pending_links:
                            next_url = pending_links.pop(0)
                            working_memory["pending_research_links"] = pending_links
                            first = {
                                "why": f"Navigating to additional independent source ({len(substantive_domains)}/{min_required_sources}): {next_url}",
                                "action": {
                                    "type": "navigate",
                                    "risk": "write",
                                    "params": {"url": next_url},
                                    "verify": "navigated to independent research source",
                                    "timeoutMs": 15000,
                                },
                            }
                            steps = [first]
                            penalties.append(f"Adaptive source requirement ({len(substantive_domains)}/{min_required_sources} visited). Moving to next source: {next_url}.")
                        else:
                            cand_sources = (working_memory.get("autonomous_discovery") or {}).get("candidate_sources", [])
                            unvisited_cand = []
                            for c in cand_sources:
                                c_dom = (c.get("domain") or "").lower()
                                parts = c_dom.split(".")
                                c_root = ".".join(parts[-2:]) if len(parts) >= 2 else c_dom
                                if c_root and c_root not in substantive_domains:
                                    unvisited_cand.append(c)
                            if unvisited_cand:
                                next_cand = unvisited_cand[0]
                                first = {
                                    "why": f"Navigating to independent candidate source ({next_cand.get('domain')}): {next_cand.get('url')}",
                                    "action": {
                                        "type": "navigate",
                                        "risk": "write",
                                        "params": {"url": next_cand["url"]},
                                        "verify": f"navigated to {next_cand.get('domain')}",
                                        "timeoutMs": 15000,
                                    },
                                }
                                steps = [first]
                                penalties.append(f"Adaptive source requirement ({len(substantive_domains)}/{min_required_sources} visited). Moving to {next_cand.get('domain')}.")
                            else:
                                if len(substantive_domains) == 0:
                                    return {
                                        "final": "failed",
                                        "steps": steps_done,
                                        "summary": f"Research incomplete: No independent sources could be verified.\n\n{summary_msg}",
                                        "history": history,
                                        "url": raw_url,
                                        "last_obs": raw_obs,
                                        "working_memory": working_memory,
                                    }

                    # 3. Adaptive Research Expansion (Evidence Gap Detection across subtopics)
                    elif plan and pkg_data:
                        pkg_obj = ResearchKnowledgePackage.from_dict(pkg_data) if isinstance(pkg_data, dict) else pkg_data
                        gaps = detect_evidence_gaps(plan, pkg_obj)
                        if gaps and plan.expansion_count < plan.max_expansions:
                            plan.expansion_count += 1
                            working_memory["research_plan"] = plan.model_dump()
                            gap_queries = generate_targeted_gap_queries(gaps, plan.topic_title)
                            if gap_queries:
                                targeted_q = gap_queries[0]
                                disc = working_memory.get("autonomous_discovery") or {}
                                cat = disc.get("category", "")
                                if cat == "software_architecture":
                                    gap_url = f"https://www.google.com/search?q={urllib.parse.quote_plus(targeted_q)}+architecture"
                                elif cat == "cloud_services":
                                    gap_url = f"https://www.google.com/search?q={urllib.parse.quote_plus(targeted_q)}+documentation"
                                elif cat == "shopping":
                                    gap_url = f"https://www.amazon.in/s?k={urllib.parse.quote_plus(targeted_q)}" if working_memory.get("user_region") == "IN" else f"https://www.google.com/search?q={urllib.parse.quote_plus(targeted_q)}"
                                else:
                                    gap_url = f"https://www.google.com/search?q={urllib.parse.quote_plus(targeted_q)}"
                                first = {
                                    "why": f"Evidence gap detected for subtopic '{gaps[0].title}'. Expanding research with targeted query: {targeted_q}",
                                    "action": {
                                        "type": "navigate",
                                        "risk": "write",
                                        "params": {"url": gap_url},
                                        "verify": f"navigated to search results for {targeted_q}",
                                        "timeoutMs": 15000,
                                    },
                                }
                                steps = [first]
                                penalties.append(
                                    f"Adaptive expansion ({plan.expansion_count}/{plan.max_expansions}): Subtopic '{gaps[0].title}' lacks verified evidence. Investigating '{targeted_q}'."
                                )

                if not steps:
                    if working_memory.get("is_research"):
                        summary_msg = _finalize_research_summary(
                            task_id, conversation_id, working_memory, summary_msg
                        )
                    return {"final": "completed", "steps": steps_done, "summary": summary_msg,
                            "history": history, "url": raw_url, "last_obs": raw_obs, "working_memory": working_memory}

        if not batch_continuation and isinstance(steps, list):
            # Task 11: build the AX tree once per plan (not once per
            # step — the validator already built it once more).
            # NOTE: the plan is deliberately NOT trimmed to its batch
            # prefix here. The Requirement 8 dedup below and the
            # checker both need the FULL plan: a redundant steps[0]
            # must be skippable in favour of steps[1], and a flag on
            # any step must block the whole plan. The trim happens
            # just before execution, once `first` is final.
            plan_tree, plan_refs = _plan_tree_and_refs(payload)
            plan_url = raw_url
            # REVIEW (Task 11 audit): fingerprint the observation the
            # plan was built from, for the batch gate below. The raw
            # observation is fingerprinted (not the redacted payload):
            # the post-action observation the gate compares against is
            # raw too, so the two are computed over the same shape.
            plan_fingerprint = observation_fingerprint(raw_obs)

        first = steps[0] if isinstance(steps, list) else None
        action = first.get("action") if isinstance(first, dict) else None
        if not isinstance(action, dict) or not action.get("type"):
            # Belt-and-braces: every real provider is schema-validated, but a
            # malformed injected plan must never crash the loop with a KeyError.
            return {"final": "failed", "steps": steps_done, "summary": "planner returned a malformed step", "history": history}

        # Requirement 8: Never repeat completed browser actions after fallback
        if _is_redundant_completed_action(action, history, raw_url):
            if len(steps) > 1:
                first = steps[1]
                action = first.get("action") if isinstance(first, dict) else None
                if not isinstance(action, dict) or not action.get("type"):
                    action = None
            else:
                action = None
                ms_tracker = working_memory.get("multi_site_research")
                if ms_tracker:
                    try:
                        from .extractors.research import is_multi_site_task_complete, get_pending_sites
                        if not is_multi_site_task_complete(ms_tracker):
                            pending = get_pending_sites(ms_tracker)
                            if pending:
                                next_site = pending[0]
                                cur_h = (urlsplit(raw_url).hostname or "").lower()
                                if cur_h and any(d in cur_h for d in next_site.get("domains", [])):
                                    rem = [s for s in pending if not any(d in cur_h for d in s.get("domains", []))]
                                    if rem:
                                        next_site = rem[0]
                                action = {
                                    "type": "navigate",
                                    "risk": "write",
                                    "params": {"url": next_site["default_url"]},
                                    "verify": f"navigated to {next_site['name']}",
                                    "timeoutMs": 15000,
                                }
                                first = {"why": f"Action was redundant. Transitioning to pending comparison site {next_site['name']}", "action": action}
                    except Exception:
                        pass
                if action is None:
                    if working_memory.get("collected_research") or working_memory.get("notes"):
                        # If multi-site tracker is present and not complete, do NOT complete prematurely
                        if not ms_tracker or is_multi_site_task_complete(ms_tracker):
                            summary_notes = working_memory.get("notes") or "Research completed."
                            pkg_data = working_memory.get("research_knowledge_package") or {}
                            if working_memory.get("is_research") and not (
                                pkg_data.get("sources")
                                or pkg_data.get("evidence")
                                or pkg_data.get("tables")
                            ):
                                # The task repeated its first navigation without
                                # ever loading a source page. Report this honestly
                                # instead of claiming a completed research run.
                                return {
                                    "final": "failed",
                                    "steps": steps_done,
                                    "summary": (
                                        "Research incomplete: the source page did not load, "
                                        "so no sources or evidence were collected and no "
                                        "deliverable was generated."
                                    ),
                                    "history": history, "url": raw_url, "last_obs": raw_obs,
                                }
                            summary_notes = _finalize_research_summary(
                                task_id, conversation_id, working_memory, summary_notes
                            )
                            return {"final": "completed", "steps": steps_done, "summary": summary_notes,
                                    "history": history, "url": raw_url, "last_obs": raw_obs}
                    penalties.append(
                        "You emitted an action that was already executed and completed in a previous step. Do not repeat completed browser actions."
                    )
                    continue

        # Loop prevention & chat auto-completion (host-scoped; no false
        # positives on ordinary search forms that happen to have a send button).
        if history and history[-1].get("result_ok"):
            if _auto_complete_after_send(history, action, raw_url):
                notes_summary = working_memory.get("notes") or "Message successfully sent to recipient."
                notes_summary = _finalize_research_summary(
                    task_id, conversation_id, working_memory, notes_summary
                )
                return {
                    "final": "completed",
                    "steps": steps_done,
                    "summary": notes_summary if working_memory.get("is_research")
                    else f"Completed: {notes_summary[:160]}",
                    "history": history,
                }

            # State-aware loop detection: detect repeated actions with zero page state change
            if last_digest is not None and current_digest == last_digest:
                last_entry = history[-1]
                last_act = last_entry.get("action") or {}
                if _actions_match(last_act, action):
                    zero_change_repeats += 1
                    if zero_change_repeats >= 3 and action.get("type") != "scroll":
                        return {
                            "final": "budget_exceeded",
                            "steps": steps_done,
                            "summary": f"Loop detected: action '{action.get('type')}' repeated on identical page state with zero effect.",
                            "history": history,
                        }
                    if zero_change_repeats >= 1 and action.get("type") != "scroll":
                        penalties.append(
                            f"State-aware loop warning: action '{action.get('type')}' was already executed on this exact page state, but produced zero state change (URL and DOM remain identical). Do not repeat this action without changing target or strategy. If navigating, select an alternative link or conclude."
                        )
                else:
                    zero_change_repeats = 0
            else:
                zero_change_repeats = 0

            consecutive_repeats = 0
            for h in reversed(history):
                if h.get("result_ok") and _actions_match(h.get("action", {}), action):
                    consecutive_repeats += 1
                else:
                    break

            if action.get("type") == "scroll":
                if consecutive_repeats >= 3:
                    return {
                        "final": "budget_exceeded",
                        "steps": steps_done,
                        "summary": "Loop detected: repeated scrolling without selecting a target or finding progress.",
                        "history": history,
                    }
                if consecutive_repeats >= 1:
                    penalties.append(
                        "Loop warning: You have already scrolled down. Do not scroll again. Look at the available links in <page-context> and click a matching product/result, or conclude if information is found."
                    )
            else:
                if consecutive_repeats >= 3:
                    return {
                        "final": "budget_exceeded",
                        "steps": steps_done,
                        "summary": f"Loop detected: action '{action.get('type')}' repeated {consecutive_repeats + 1} times without progress.",
                        "history": history,
                    }
                if consecutive_repeats >= 1:
                    penalties.append(f"loop warning: action '{action.get('type')}' was already completed. Do not repeat it; proceed to next step or conclude.")

        # Repeated-failure loop detection: the checks above only run
        # when the last step SUCCEEDED, so a planner that keeps
        # re-emitting an action that FAILS with the same error was
        # never stopped — it burned the whole step budget one failure
        # at a time (live Flipkart run 2026-11-17: 38 identical
        # popup-close clicks, each STALE_TARGET as the page
        # re-rendered). Count trailing failures of this same action
        # with this same error; three in a row stops the task, one
        # earns a warning penalty — mirroring the success-path
        # thresholds above. Runs on every iteration, regardless of
        # the last step's outcome. Errors are compared by normalized
        # cause, not raw text: the extension embeds the changing
        # target (refs, ids, coords) in the message, so two failures
        # with the same cause never share a raw string.
        failed_repeats = 0
        if history and history[-1].get("result_ok") is False and history[-1].get("error"):
            _last_err = history[-1].get("error")
            _last_key = _normalize_loop_error(_last_err)
            if _last_key:
                for _h in reversed(history):
                    if (
                        _h.get("result_ok") is False
                        and _failure_action_matches(_h.get("action", {}), action)
                        and _normalize_loop_error(_h.get("error")) == _last_key
                    ):
                        failed_repeats += 1
                    else:
                        break
            if failed_repeats >= 3:
                return {
                    "final": "budget_exceeded",
                    "steps": steps_done,
                    "summary": (
                        f"Loop detected: action '{action.get('type')}' failed "
                        f"{failed_repeats} times in a row with the same error "
                        f"({_last_err}); stopping instead of repeating it."
                    ),
                    "history": history,
                }
            if failed_repeats >= 1:
                penalties.append(
                    f"Repeated failure warning: action '{action.get('type')}' already "
                    f"failed {failed_repeats} time(s) with the same error ({_last_err}). "
                    "Do not repeat it without a new target or strategy; conclude if "
                    "the goal cannot be reached."
                )

        # Task 7: resolve element refs against this *redacted*
        # observation before anything executes. A ref the planner quoted
        # is looked up in the current accessibility tree; a match pins
        # the live target id and a coordinate fallback. A ref that is
        # gone (or whose element changed role/name) is stale: re-observe
        # once and replan on the fresh tree; a second stale ref in a row
        # fails honestly with "element no longer found" instead of
        # clicking whatever now sits at those coordinates. Actions
        # without a ref keep the legacy name-based semantic match below.
        # Ground ambiguous planner labels against this *redacted*
        # observation. A semantic match supplies a fresh target ID and a
        # spatial fallback; no match leaves the executor to report
        # STALE_TARGET honestly.
        _verified_ref: str | None = None
        _verified_node = None
        if action and action.get("type") in ("click", "type", "select_option", "set_date", "upload_file"):
            target = action.get("target") or {}
            if isinstance(target, dict):
                _ref = target.get("elementRef") or target.get("element_ref")
                if _ref:
                    from .grounding import node_center as _node_center

                    # Task 11: reuse the tree built once per plan above,
                    # falling back to a fresh build only when the plan
                    # came from a path that never built one.
                    _tree = plan_tree
                    if _tree is None:
                        _tree, _built_refs = _plan_tree_and_refs(payload)
                        plan_tree = _tree
                        plan_refs = _built_refs
                        # REVIEW (Task 11 audit): keep the fingerprint
                        # in step — a batch pinned from this path still
                        # needs a baseline to compare the DOM against.
                        if plan_fingerprint is None:
                            plan_fingerprint = observation_fingerprint(raw_obs)
                    # Task 13: freshness is real now. The old code
                    # compared the node against the tree it was resolved
                    # from, so "changed" was unreachable — a node always
                    # matches its own tree (the Task 7 design flaw the
                    # Moodle-fix review found). The fresh tree comes
                    # from the FRESHEST observation we hold: in a batch
                    # continuation that is the post-action observation
                    # (cached_obs — still in memory because the batch
                    # branch took precedence over the cached_obs branch
                    # at iteration top, so no round trip), built through
                    # the same redaction pipeline as the plan-time tree
                    # so a difference is a real page change, not a
                    # redaction artifact. Outside a batch there is no
                    # fresher observation than the plan-time one — the
                    # verdict is trivially "ok", and the post-wait
                    # re-verification below covers the window where the
                    # page can move (confirmation / pause).
                    _fresh_tree = None
                    # REVIEW (Task 13 audit): a fresh tree that could
                    # not be built is "cannot verify", NOT "ok".
                    # ref_freshness returns "ok" when no fresh tree is
                    # given, which is correct for a single step (there
                    # is no fresher observation than the plan-time
                    # one), but in a batch continuation we DID have a
                    # fresher observation and redaction failed on it.
                    # Executing the pinned step anyway would run blind
                    # on the plan-time tree — the exact failure the
                    # post-wait path (below) refuses. Track it and end
                    # the batch on the shared one-retry counter.
                    _fresh_unavailable = False
                    if batch_ctx is not None and cached_obs is not None:
                        _built = fresh_tree_for_observation(cached_obs, instruction)
                        if _built is not None:
                            _fresh_tree = _built[0]
                        else:
                            _fresh_unavailable = True
                    _fresh, _node, _plan_node = ref_freshness(_tree, str(_ref), _fresh_tree)
                    _verified_ref = str(_ref)
                    _verified_node = _plan_node
                    # A changed element inside a batch ends the batch:
                    # the pinned plan was built from a moved page, so
                    # re-observe and replan rather than execute it.
                    # It takes the same one-retry-then-fail path as a
                    # stale ref. Task 11's fingerprint gate already ends
                    # the batch on a coarse DOM change; this adds the
                    # per-ref name/role/target-id check the fingerprint
                    # misses (same count/ids, different name/role) — the
                    # two must agree, and both end the batch.
                    _batch_changed = batch_ctx is not None and _fresh == "changed"
                    if _node is None or _fresh == "stale" or _batch_changed or _fresh_unavailable:
                        if grounding_stale_retries < 1:
                            grounding_stale_retries += 1
                            if _fresh_unavailable:
                                penalties.append(
                                    f"Could not re-verify element ref {_ref!r} against the "
                                    "post-action observation (redaction failed) — ending "
                                    "the batch, re-observing once and replanning instead "
                                    "of executing blind."
                                )
                            elif _batch_changed:
                                penalties.append(
                                    f"Element ref {_ref!r} changed since the plan "
                                    "was made (the page re-rendered mid-batch) — "
                                    "ending the batch, re-observing once and "
                                    "replanning on the fresh accessibility tree."
                                )
                            else:
                                penalties.append(
                                    f"Element ref {_ref!r} is stale in the current "
                                    "observation — re-observing once and replanning "
                                    "on the fresh accessibility tree."
                                )
                            logger.warning(
                                "task %s: %s element ref %r; re-observing once",
                                task_id, "unverifiable" if _fresh_unavailable else ("changed" if _batch_changed else "stale"), _ref,
                            )
                            cached_obs = None
                            last_digest = None
                            # Task 11: the page changed under us — the
                            # cached observation is for the old tree.
                            obs_cache.invalidate()
                            continue
                        _save_checkpoint_defensive(task_id, conversation_id, session_id, steps_done, raw_url, history, working_memory)
                        if _fresh_unavailable:
                            return {"final": "failed", "steps": steps_done,
                                    "summary": f"could not re-verify element ref {_ref!r}: the post-action observation could not be redacted, so the target could not be re-grounded. Stopping instead of executing blind.",
                                    "history": history}
                        if _batch_changed:
                            return {"final": "failed", "steps": steps_done,
                                    "summary": f"element changed mid-batch: the element ref {_ref!r} changed (role, name, or target id moved) and could not be re-grounded after a fresh observation. Stopping instead of clicking the wrong control.",
                                    "history": history}
                        return {"final": "failed", "steps": steps_done,
                                "summary": f"element no longer found: the element ref {_ref!r} is not in the current page — the page changed and the target could not be re-grounded. Stopping instead of clicking the wrong control.",
                                "history": history}
                    if _fresh == "changed":
                        logger.warning(
                            "task %s: element ref %r changed since observation "
                            "(role/name moved); proceeding on the live element",
                            task_id, _ref,
                        )
                        penalties.append(
                            f"Element ref {_ref!r} changed since the observation "
                            "(role or name moved) — proceeding on the live "
                            "element; verify the result before continuing."
                        )
                    target = dict(target)
                    if _node.target_id:
                        target["targetId"] = _node.target_id
                    target.setdefault("name", _node.name)
                    if _node.role:
                        target.setdefault("role", _node.role)
                    _center = _node_center(_node)
                    if _center:
                        target.setdefault("coords", _center)
                    action["target"] = target
                    grounding_stale_retries = 0
                else:
                    from .observation import match_semantic_target
                    matched = match_semantic_target(
                        payload.interactive, target_id=target.get("targetId") or "",
                        name=target.get("name") or "", role=target.get("role") or "",
                        expected_bounds=target.get("expectedBounds"))
                    if matched:
                        target = dict(target)
                        target["targetId"] = matched.target_id
                        target.setdefault("name", matched.name)
                        bounds = matched.semantic.interactability["bounds"]
                        target.setdefault("coords", {"x": bounds["x"] + bounds["width"] / 2,
                                                     "y": bounds["y"] + bounds["height"] / 2})
                        action["target"] = target

        last_digest = current_digest

        if raw_url:
            url_history.append(raw_url)
            if len(url_history) >= 4:
                if (url_history[-1] == url_history[-3] and
                    url_history[-2] == url_history[-4] and
                    url_history[-1] != url_history[-2]):
                    penalties.append(
                        "Oscillation warning: You are repeatedly bouncing between two pages. Do not navigate back and forth; execute your search or extraction on the destination page."
                    )

        is_wa_qr = _is_whatsapp_qr(raw_url, raw_obs)
        is_auth_page = _is_auth_url(raw_url)
        is_strict = getattr(confirm_gate, "strict_write_confirm", True) if confirm_gate is not None else False
        needs_confirm = action.get("risk") == "sensitive" or is_auth_page or is_wa_qr or (
            is_strict and action.get("risk") == "write" and action.get("type") not in ("navigate", "scroll", "zoom", "wait", "observe", "select_tab")
        )

        # -- Task 4: blocked outright (no gate, no override) ---------------
        # Account creation and permanent account deletion are never
        # executed, whatever the planner or the page says.
        _blocked_reason = is_blocked_action(instruction, action, raw_url, payload.interactive)
        if _blocked_reason:
            _save_checkpoint_defensive(task_id, conversation_id, session_id, steps_done, raw_url, history, working_memory)
            return {"final": "failed", "steps": steps_done, "summary": _blocked_reason, "history": history}

        # -- Task 4: sensitive wait / payment surface -> takeover ----------
        # A planner-emitted sensitive wait on a takeover surface, or any
        # action on a payment surface that only appeared after planning
        # (e.g. the planner navigated to checkout), becomes a takeover,
        # not a plain confirmation. The operator does the step in the
        # tab; the agent observes nothing while they do. A completed
        # takeover re-observes from the top of the loop.
        _wait_trigger = None
        if action.get("type") == "wait" and action.get("risk") == "sensitive":
            _wait_trigger = takeover_reason_for_wait(raw_url, payload.interactive)
        elif action.get("type") == "ucp_checkout":
            # Task 14: a UCP checkout always goes through takeover —
            # the operator approves the amount and does the payment
            # step themselves; the agent never submits a payment.
            from .takeover import TakeoverTrigger

            _wait_trigger = TakeoverTrigger(
                kind="payment",
                reason=(
                    "A UCP checkout is about to run. Payments always "
                    "require your approval and your own hands in the browser — "
                    "the agent never submits a payment."
                ),
                url=raw_url,
            )
        elif action.get("type") == "webmcp_tool":
            # Task 14: a WebMCP tool that asks for credentials or a
            # payment is a takeover — the operator supplies it in the
            # tab; the agent never types a secret into a page tool.
            _tool_name = (action.get("params") or {}).get("tool")
            _tool = next(
                (t for t in (working_memory.get("webmcp_tools") or [])
                 if isinstance(t, dict) and t.get("name") == _tool_name),
                None,
            )
            if _tool is not None:
                try:
                    from .webmcp import WebMCPTool as _WMT
                    from .webmcp import tool_requests_sensitive as _sens

                    _kind = _sens(_WMT.from_dict(_tool))
                except Exception:
                    _kind = None
                if _kind is not None:
                    from .takeover import TakeoverTrigger

                    _wait_trigger = TakeoverTrigger(
                        kind="credentials" if _kind == "credentials" else "payment",
                        reason=(
                            f"The site tool '{_tool_name}' asks for {_kind}. "
                            "Supply it yourself in the browser — the agent "
                            "never types a secret into a page tool."
                        ),
                        url=raw_url,
                    )
        elif is_payment_surface(raw_url, payload.interactive) and action.get("type") not in {"observe", "wait", "scroll", "zoom", "select_tab"}:
            from .takeover import TakeoverTrigger

            _wait_trigger = TakeoverTrigger(
                kind="payment",
                reason=(
                    "A payment / checkout surface is visible. Payments always "
                    "require your approval and your own hands in the browser — "
                    "the agent never submits a payment."
                ),
                url=raw_url,
            )
        if _wait_trigger is not None and takeover_gate is not None:
            _outcome = await _enter_takeover(_wait_trigger)
            if _outcome == "completed":
                continue
            if _outcome == "already_done":
                pass  # fall through to the normal confirmation gate
            elif is_cancelled(task_id):
                return {"final": "cancelled", "steps": steps_done, "summary": None, "history": history}
            else:
                _save_checkpoint_defensive(task_id, conversation_id, session_id, steps_done, "", history, working_memory)
                return {"final": "failed", "steps": steps_done, "summary": _takeover_failure_summary(_outcome, _wait_trigger), "history": history}

        # -- Task 3: domain allow-list + web-blind checker ----------------
        # Both run before any consequential action executes. A blocked
        # navigation is rejected (never executed) and the task halts for
        # a human decision through the same confirmation gate as a
        # sensitive action; approving allows that one navigation as a
        # logged one-off override. A checker flag does the same. With no
        # gate available there is nobody to ask, so the task fails
        # closed instead of executing.
        checker_msg: Optional[str] = None
        task3_flagged = False
        if action.get("type") in {"navigate", "new_tab"}:
            nav_url = (action.get("params") or {}).get("url")
            if nav_url and effective_domains:
                ok, why = check_url(nav_url, effective_domains)
                if not ok:
                    task3_flagged = True
                    logger.warning("task %s: blocked navigation: %s", task_id, why)
                    # Task 10: audit — allow-list denial (host only;
                    # the full URL may carry query secrets).
                    try:
                        from .audit import append_event as _audit_event3
                        from urllib.parse import urlsplit as _audit_split

                        _audit_host = (_audit_split(nav_url).hostname or "unknown")
                        _audit_event3("allowlist_denied",
                                      f"navigation to {_audit_host} denied by allow-list",
                                      actor="system", task_id=task_id)
                    except Exception:
                        pass
                    checker_msg = (
                        f"Blocked navigation: {why}. The action was not executed. "
                        "Approve to allow this one navigation anyway, or reject to stop the task."
                    )
        findings = []
        # Task 11: a pinned batch step is a subset of a plan the
        # checker already approved on this same observation — re-running
        # the scan + check on every batched step is redundant work.
        # The navigate allow-list above still runs per step.
        if batch_continuation:
            timings.note_checker_skipped()
        else:
            # Scan the raw observation text (not the cleansed planner
            # payload — cleansing quarantines the very patterns the scan
            # looks for). Accept both "pageText" (extension) and "page_text".
            scan_text = raw_text or ""
            if not scan_text:
                if isinstance(raw_obs, dict):
                    scan_text = str(raw_obs.get("pageText") or raw_obs.get("page_text") or "")
                else:
                    scan_text = str(
                        getattr(raw_obs, "page_text", None)
                        or getattr(raw_obs, "pageText", None)
                        or ""
                    )
            if scan_text:
                findings = scan_page_text(scan_text, raw_url)
        if findings:
            result = check_plan(instruction, steps, findings, raw_url, allowed_domains=None)
            if result.flagged:
                task3_flagged = True
                reasons = "; ".join(result.reasons)
                logger.warning("task %s: isolation checker flagged plan: %s", task_id, reasons)
                checker_msg = (
                    (checker_msg + " " if checker_msg else "")
                    + f"Isolation checker flagged this plan: {reasons}. "
                    "Approve to proceed anyway, or reject to stop the task."
                )
        if task3_flagged:
            needs_confirm = True
            if confirm_gate is None:
                _save_checkpoint_defensive(task_id, conversation_id, session_id, steps_done, raw_url, history, working_memory)
                return {"final": "failed", "steps": steps_done,
                        "summary": f"isolation check blocked the action: {checker_msg}",
                        "history": history}

        approved = False
        # Task 13: set when this iteration actually waited for the
        # operator (confirmation and/or pause). The operator had the
        # browser in that window, so a ref step is re-verified against
        # a fresh observation below before it executes.
        _waited_for_operator = False
        # Task 12: "approve this step only" — the operator approved the
        # pending step but asked to see a fresh plan before the next
        # one. The flag is consumed by the batch gate at the end of
        # this iteration (no pending_batch is pinned), and reset at
        # the top of every iteration so it can never leak into a later
        # step the operator did not choose it for.
        if confirm_gate is not None and needs_confirm:
            gate_msg = checker_msg or _gate_message(action, is_wa_qr, is_auth_page)
            confirm_started = time.perf_counter()
            # Task 12: the gate request carries the whole validated
            # plan as a preview (structured fields only — see
            # plan_preview.py), so the operator sees what they are
            # approving. `steps` here is the FULL plan: the batch trim
            # happens later, just before execution. Start from `first`
            # (the dedup may have advanced past a redundant steps[0]).
            _plan_start = next((i for i, s in enumerate(steps) if s is first), 0) if isinstance(steps, list) else 0
            approved = await _request_confirmation(
                confirm_gate, action, task_id, confirm_timeout_s,
                custom_message=gate_msg,
                plan=plan_preview(steps[_plan_start:] if isinstance(steps, list) else []),
            )
            _waited_for_operator = True
            step_only_approval = bool(getattr(confirm_gate, "step_only", False))
            metrics("confirm_wait", confirm_started)
            if is_cancelled(task_id):
                return {"final": "cancelled", "steps": steps_done, "summary": None, "history": history}
            if approved is False:
                return {"final": "failed", "steps": steps_done,
                        "summary": f"user rejected action: {action.get('type')}", "history": history}
            if task3_flagged:
                # A human overrode a Task 3 flag. Record it so the
                # override is visible in the task history, not silent.
                logger.warning("task %s: operator approved a flagged action: %s", task_id, checker_msg)
                penalties.append(f"operator approved a flagged action: {checker_msg}")

        pause_event = _pause_events.get(task_id)
        if pause_event is not None and not pause_event.is_set():
            pause_started = time.monotonic()
            await pause_event.wait()
            _waited_for_operator = True
            start += time.monotonic() - pause_started
            # Task 11: the operator may have acted in the browser
            # while paused — the cached observation is untrusted now.
            obs_cache.invalidate()
        if is_cancelled(task_id):
            return {"final": "cancelled", "steps": steps_done, "summary": None, "history": history}

        # Task 13: post-wait re-verification. The operator had the
        # browser while the loop waited (confirmation and/or pause) —
        # the page may have moved under the plan, and that window is
        # exactly what the old vacuous check could never see. A ref
        # step is re-verified against ONE fresh observation before it
        # executes. A changed element proceeds on the LIVE node
        # (target re-pinned); a stale one re-observes and replans
        # once, then fails honestly; an observation that cannot be
        # redacted is treated as "cannot verify" — never execute
        # blind. All three failure paths share the one-retry counter
        # with the ref site above, so a persistently broken
        # observation source cannot loop the operator through
        # confirmations forever. A changed element inside a batch
        # ends the batch, like the ref-site rule.
        if _verified_node is not None and _waited_for_operator:
            _fresh_raw = None
            try:
                _maybe_fresh = obs_factory()
                _fresh_raw = await _maybe_fresh if asyncio.iscoroutine(_maybe_fresh) else _maybe_fresh
                timings.note_observation_call()
            except Exception:
                _fresh_raw = None
            _built2 = fresh_tree_for_observation(_fresh_raw, instruction) if _fresh_raw is not None else None
            if _built2 is None:
                if grounding_stale_retries < 1:
                    grounding_stale_retries += 1
                    penalties.append(
                        f"Could not re-verify element ref {_verified_ref!r} after "
                        "the operator wait (fresh observation unavailable) — "
                        "re-observing and replanning instead of executing blind."
                    )
                    cached_obs = None
                    last_digest = None
                    obs_cache.invalidate()
                    continue
                _save_checkpoint_defensive(task_id, conversation_id, session_id, steps_done, raw_url, history, working_memory)
                return {"final": "failed", "steps": steps_done,
                        "summary": f"could not re-verify element ref {_verified_ref!r} after the operator wait (fresh observation unavailable) — stopping instead of executing blind.",
                        "history": history}
            _fresh2, _live2, _ = ref_freshness(plan_tree, _verified_ref, _built2[0])
            _post_batch_changed = batch_ctx is not None and _fresh2 == "changed"
            if _fresh2 == "stale" or _post_batch_changed:
                if grounding_stale_retries < 1:
                    grounding_stale_retries += 1
                    if _post_batch_changed:
                        penalties.append(
                            f"Element ref {_verified_ref!r} changed while waiting "
                            "for the operator (the page re-rendered mid-batch) — "
                            "ending the batch, re-observing once and replanning "
                            "on the fresh accessibility tree."
                        )
                    else:
                        penalties.append(
                            f"Element ref {_verified_ref!r} disappeared while "
                            "waiting for the operator — re-observing once and "
                            "replanning on the fresh accessibility tree."
                        )
                    cached_obs = None
                    last_digest = None
                    obs_cache.invalidate()
                    continue
                _save_checkpoint_defensive(task_id, conversation_id, session_id, steps_done, raw_url, history, working_memory)
                if _post_batch_changed:
                    return {"final": "failed", "steps": steps_done,
                            "summary": f"element changed mid-batch: the element ref {_verified_ref!r} changed while waiting for the operator and could not be re-grounded after a fresh observation. Stopping instead of clicking the wrong control.",
                            "history": history}
                return {"final": "failed", "steps": steps_done,
                        "summary": f"element no longer found: the element ref {_verified_ref!r} disappeared while waiting for the operator and could not be re-grounded. Stopping instead of clicking the wrong control.",
                        "history": history}
            if _fresh2 == "changed" and _live2 is not None:
                logger.warning(
                    "task %s: element ref %r changed while waiting for the "
                    "operator; proceeding on the live element",
                    task_id, _verified_ref,
                )
                penalties.append(
                    f"Element ref {_verified_ref!r} changed while waiting for "
                    "the operator (role, name, or target id moved) — "
                    "proceeding on the live element; verify the result "
                    "before continuing."
                )
                from .grounding import node_center as _node_center2

                _target2 = dict(action.get("target") or {})
                if _live2.target_id:
                    _target2["targetId"] = _live2.target_id
                _target2["name"] = _live2.name
                if _live2.role:
                    _target2["role"] = _live2.role
                _center2 = _node_center2(_live2)
                if _center2:
                    _target2["coords"] = _center2
                action["target"] = _target2
            # The post-action observation (below) is newer than this
            # one, so the fresh observation is used only for this
            # verification — it is not kept as the cached observation.

        _enforce_date_integrity(action, instruction)
        # -- Task 9: tab registry gate ----------------------------------
        # switch_tab / select_tab may only target a tab the task owns.
        # An unknown tabId is rejected here — before anything is sent
        # to the extension — instead of letting the browser switch to
        # a tab outside the task (e.g. the operator's personal tab).
        # An empty registry means "no tabs tracked yet" (legacy /
        # test flows): nothing to validate against, so allow it.
        if action.get("type") in {"select_tab", "switch_tab"}:
            _target_tab = (action.get("params") or {}).get("tabId")
            _known_tabs = working_memory.get("tabs") or []
            if _known_tabs and _target_tab is not None and not any(
                t.get("tab_id") == _target_tab for t in _known_tabs
            ):
                _save_checkpoint_defensive(task_id, conversation_id, session_id, steps_done, raw_url, history, working_memory)
                return {"final": "failed", "steps": steps_done,
                        "summary": f"unknown tab: tab {_target_tab} is not in this task's tab registry",
                        "history": history}
        action_started = time.perf_counter()
        execution_action = action
        # -- Task 4: vault placeholder substitution ------------------------
        # The planner only ever sees placeholders. The real value is
        # substituted here — after the confirmation gate, just before
        # the action is sent to the extension — and never anywhere else.
        # History keeps the placeholder form; the evidence below is
        # scrubbed of the real value before it is recorded.
        _substituted_values: list[str] = []
        if action.get("type") == "type":
            _params = action.get("params") or {}
            _text = _params.get("text")
            if isinstance(_text, str) and contains_placeholder(_text):
                try:
                    _remaining = _text
                    while True:
                        _parsed = parse_placeholder(_remaining)
                        if _parsed is None:
                            break
                        _substituted_values.append(placeholder_value(*_parsed))
                        _remaining = _remaining.replace(
                            "{{CREDENTIAL:%s:%s:%s}}" % _parsed, "", 1
                        )
                    execution_action = substitute_placeholders_in_action(action)
                except (KeyError, ValueError) as exc:
                    _save_checkpoint_defensive(task_id, conversation_id, session_id, steps_done, raw_url, history, working_memory)
                    return {"final": "failed", "steps": steps_done,
                            "summary": f"credential placeholder could not be resolved: {exc}",
                            "history": history}
        if action.get("type") == "upload_file":
            from .file_uploads import get as get_attachment
            from .hub import hub
            attachment = get_attachment(hub.tasks.get(task_id).file_token or "") if hub.tasks.get(task_id) else None
            if not attachment:
                return {"final": "failed", "steps": steps_done,
                        "summary": "File upload blocked: select the exact attachment in the workspace first.", "history": history}
            execution_action = dict(action)
            execution_action["params"] = {"fileName": attachment[0], "base64": attachment[1]}
        # Task 11: trim the plan to its executable batch prefix — only
        # now, after the dedup and the checker have seen the full plan.
        # The batch starts at the step actually about to execute
        # (`first`, which the dedup may have advanced past a redundant
        # steps[0]); trimming earlier discarded steps the dedup needs
        # to advance to. If `first` is a step the planner did not emit
        # (the multi-site fallback synthesises one), the batch is just
        # [first]. Pinned iterations skip this: their `steps` is already
        # the remainder of a batch.
        if not batch_continuation and isinstance(steps, list):
            _start = next((i for i, s in enumerate(steps) if s is first), None)
            if _start is None:
                steps = [first]
            else:
                steps = batch_prefix(steps[_start:], plan_refs)
        # Task 14: record how this step is being executed, so the
        # workspace can show it ("via WebMCP tool X" / "via UCP
        # checkout") and the record carries it after the task ends.
        if action.get("type") == "webmcp_tool":
            working_memory["route"] = {
                "via": "webmcp_tool",
                "tool": (action.get("params") or {}).get("tool"),
            }
        elif action.get("type") == "ucp_checkout":
            working_memory["route"] = {
                "via": "ucp_checkout",
                "merchant": (action.get("params") or {}).get("merchant"),
                "amount": (action.get("params") or {}).get("amount"),
                "currency": (action.get("params") or {}).get("currency"),
            }
        # Task 14: a UCP checkout is executed by the backend, never
        # sent to the extension and never retried — a payment step
        # runs exactly once. The result is shaped like an executor
        # result so the rest of the loop (cache invalidation,
        # evidence, failure handling) treats it uniformly. No
        # observation is returned: the page has (or has failed to)
        # change, so the loop re-observes from the top.
        if action.get("type") == "ucp_checkout":
            from .ucp import execute_ucp_checkout

            _params = action.get("params") or {}
            _prof = working_memory.get("ucp_profile")
            try:
                _ucp_outcome = await asyncio.to_thread(
                    execute_ucp_checkout,
                    _prof if isinstance(_prof, dict) else None,
                    amount=_params.get("amount"),
                    currency=_params.get("currency"),
                    task_id=task_id,
                )
            except Exception as exc:  # never raises inside, but never trust that
                _ucp_outcome = {"ok": False, "error": {"message": f"UCP checkout failed: {exc}"}}
            # The executor returns the action-result shape: ok plus
            # evidence on success, error{message} on failure.
            if _ucp_outcome.get("ok"):
                result = {
                    "ok": True,
                    "evidence": _ucp_outcome.get("evidence", "UCP checkout completed"),
                }
            else:
                _ucp_err = _ucp_outcome.get("error")
                _ucp_msg = (
                    _ucp_err.get("message")
                    if isinstance(_ucp_err, dict)
                    else str(_ucp_err or "UCP checkout failed")
                )
                result = {"ok": False, "error": {"message": _ucp_msg}}
            metrics("act", action_started)
        else:
            # Task 13: the loop passes its own MAX_RETRIES (a patch point
            # tests use) — _execute_with_retries defaults to
            # config.MAX_RETRIES for direct callers.
            result = await _execute_with_retries(ext_hub, execution_action, task_id, session_id=session_id, max_retries=MAX_RETRIES)
            metrics("act", action_started)
        # Task 1 (automation): one bounded execution-time recovery.
        # A ref that was fresh at plan time can still go stale in the
        # window between planning and execution (the page re-rendered,
        # or a popup/dialog moved the target): the extension reports
        # STALE_TARGET (or a dialog error) and the step fails. Failing
        # the step and replanning from scratch loses the step's place
        # in the plan; instead, re-observe ONCE, resolve the same
        # control again in the fresh tree (same target id, else same
        # role + name, unambiguous only — never a guess), and retry
        # the step once with the fresh ref. Exactly one retry per
        # step: a second failure is recorded honestly and the loop
        # replans next iteration, as before. UCP checkouts never
        # retry (a payment step runs exactly once). The recovery is
        # attached to the step record below (entry["recovery"]) and
        # to working_memory["automation_recoveries"].
        _recovery: Optional[dict[str, Any]] = None
        # The raw observation the recovery re-observed with. When the
        # step still fails, the next iteration plans from THIS
        # observation (it is already the freshest page state) instead
        # of spending another observation round trip on the same page.
        _recovery_raw_obs: Any = None
        if action.get("type") != "ucp_checkout" and (
            is_stale_target_error(result) or is_dialog_error(result)
        ):
            _err_code = str((result.get("error") or {}).get("code") or "")
            _kind = "dialog_retry" if is_dialog_error(result) else "stale_ref_retry"
            _fresh_raw: Any = None
            try:
                _maybe_recover = obs_factory()
                _fresh_raw = await _maybe_recover if asyncio.iscoroutine(_maybe_recover) else _maybe_recover
                timings.note_observation_call()
            except Exception:
                _fresh_raw = None
            _recovery_raw_obs = _fresh_raw
            _built_recover = fresh_tree_for_observation(_fresh_raw, instruction) if _fresh_raw is not None else None
            _old_ref = (
                _verified_ref
                or (action.get("target") or {}).get("elementRef")
                or (action.get("target") or {}).get("element_ref")
            )
            if _built_recover is None:
                _recovery = {
                    "kind": _kind, "trigger": _err_code,
                    "old_ref": _old_ref, "new_ref": None,
                    "result_ok": False,
                    "note": "fresh observation unavailable; no retry attempted",
                }
            else:
                _fresh_ref, _fresh_node = resolve_fresh_ref(
                    _verified_node, _built_recover[0],
                    target=action.get("target") if isinstance(action.get("target"), dict) else None,
                )
                # A retry is warranted when the control moved: a new
                # ref, a new stable target id, or both. (Refs are
                # positional — build_ax_tree assigns e0, e1, ... in
                # list order — so a re-render that only re-assigns the
                # target id leaves the ref unchanged; the retry still
                # matters, because the executor resolves by id.)
                _old_target_id = (
                    (_verified_node.target_id if _verified_node is not None else None)
                    or (action.get("target") or {}).get("targetId")
                )
                _moved = (
                    _fresh_ref is not None and _fresh_node is not None
                    and (
                        str(_fresh_ref) != str(_old_ref or "")
                        or str(_fresh_node.target_id or "") != str(_old_target_id or "")
                    )
                )
                # A stale ref retries only when the control actually
                # moved (same ref + same id means nothing changed, so
                # a retry would just repeat the failure). A dialog
                # error is different: the target never moved — the
                # popup did — so re-attempting the same ref on the
                # freshly observed page (dialog gone or dismissed) is
                # the recovery.
                _should_retry = (
                    _moved if _kind == "stale_ref_retry"
                    else (_fresh_ref is not None and _fresh_node is not None)
                )
                if _should_retry:
                    from .grounding import node_center as _node_center_recover

                    _retry_target = dict(action.get("target") or {})
                    _retry_target["elementRef"] = _fresh_ref
                    if _fresh_node.target_id:
                        _retry_target["targetId"] = _fresh_node.target_id
                    _retry_target["name"] = _fresh_node.name
                    if _fresh_node.role:
                        _retry_target["role"] = _fresh_node.role
                    _center_r = _node_center_recover(_fresh_node)
                    if _center_r:
                        _retry_target["coords"] = _center_r
                    _retry_action = dict(execution_action)
                    _retry_action["target"] = _retry_target
                    obs_cache.invalidate()
                    _retry_result = await _execute_with_retries(
                        ext_hub, _retry_action, task_id,
                        session_id=session_id, max_retries=MAX_RETRIES)
                    _recovery = {
                        "kind": _kind, "trigger": _err_code,
                        "old_ref": _old_ref, "new_ref": _fresh_ref,
                        "new_target_id": _fresh_node.target_id,
                        "result_ok": bool(_retry_result.get("ok")),
                    }
                    if _retry_result.get("ok"):
                        result = _retry_result
                        action["target"] = _retry_target
                        grounding_stale_retries = 0
                        logger.warning(
                            "task %s: %s recovered: ref %r -> %r; retry succeeded",
                            task_id, _kind, _old_ref, _fresh_ref,
                        )
                    else:
                        logger.warning(
                            "task %s: %s retried with fresh ref %r but still failed; replanning next iteration",
                            task_id, _kind, _fresh_ref,
                        )
                else:
                    _recovery = {
                        "kind": _kind, "trigger": _err_code,
                        "old_ref": _old_ref, "new_ref": None,
                        "result_ok": False,
                        "note": (
                            "target found in the fresh observation but it did not move; "
                            "retrying the same ref would repeat the failure"
                            if _fresh_ref is not None
                            else "no unambiguous fresh ref for the target in the fresh observation; no retry attempted"
                        ),
                    }
        # REVIEW (Task 14 audit): a WebMCP tool result is untrusted
        # page content, and the extension reports it as evidence text
        # — which the loop copies into the history entry the planner
        # sees next round. The extension truncates the result, but
        # truncation is not sanitisation: a lure ("Ignore all previous
        # instructions...") reached the planner verbatim in round 2.
        # Sanitise it here, before anything downstream (narrative,
        # evidence, history, timeline) can consume it — bounded,
        # redacted, injection patterns quarantined, wrapped as
        # <untrusted_tool_content>, exactly as webmcp.py promises.
        # The failure fallback withholds the result rather than
        # passing it through raw.
        if action.get("type") == "webmcp_tool" and isinstance(result.get("evidence"), str):
            try:
                from .mcp.sanitizer import sanitize_tool_output

                result = dict(result)
                result["evidence"] = sanitize_tool_output(result["evidence"])["content"]
            except Exception:
                result = dict(result)
                result["evidence"] = (
                    "<untrusted_tool_content>[tool result withheld: "
                    "could not sanitize]</untrusted_tool_content>"
                )
        if session_id and result.get("ok"):
            obs = result.get("observation")
            if isinstance(obs, dict) and "tabId" in obs:
                from .session_manager import session_manager
                sess = session_manager.get_session(session_id)
                if sess:
                    is_new_tab = "opened new tab" in str(result.get("evidence", "")).lower() or obs["tabId"] == sess.primary_tab_id or obs["tabId"] in sess.child_tab_ids
                    if is_new_tab:
                        session_manager.register_child_tab(session_id, obs["tabId"])
            # After a successful close_tab, drop the tab from the session's
            # child registry so stale tabIds cannot authorize later actions.
            if session_id and action.get("type") == "close_tab":
                try:
                    closed_tab_id = int(action.get("params", {}).get("tabId"))
                except (TypeError, ValueError):
                    closed_tab_id = None
                if closed_tab_id is not None:
                    session_manager.unregister_child_tab(session_id, closed_tab_id)
        # -- Task 9: keep the tab registry in sync ---------------------
        # The registry is what the planner sees in <task-tabs> and
        # what switch_tab validates against, so it must track reality:
        # a new tab registers (and becomes active), a switch moves the
        # active marker, a close removes the tab.
        if result.get("ok"):
            _obs = result.get("observation")
            _obs_tab = _obs.get("tabId") if isinstance(_obs, dict) else None
            if action.get("type") == "new_tab" and _obs_tab is not None:
                _register_tab_in_memory(
                    working_memory, int(_obs_tab),
                    url=(action.get("params") or {}).get("url", ""),
                    active=True,
                )
            elif action.get("type") in {"select_tab", "switch_tab"}:
                _tab_param = (action.get("params") or {}).get("tabId")
                if _tab_param is not None:
                    _set_active_tab_in_memory(working_memory, int(_tab_param))
            elif action.get("type") == "close_tab":
                _tab_param = (action.get("params") or {}).get("tabId")
                if _tab_param is not None:
                    _remove_tab_from_memory(working_memory, int(_tab_param))
        if is_cancelled(task_id):
            return {"final": "cancelled", "steps": steps_done, "summary": None, "history": history}
        steps_done += 1
        cached_obs = _fresh_observation(result, action)
        # Task 11: keep the observation cache in sync with reality.
        # A successful action's returned observation is the freshest
        # page state; a failed action may have half-mutated the page;
        # a tab switch/close relocates the active surface and the loop
        # forces a live re-observation; a success with no observation
        # leaves the page state unknown. In all four cases the
        # pre-action entry must not be served again.
        if action.get("type") in {"select_tab", "close_tab"}:
            obs_cache.invalidate()
            last_obs_url = ""
            last_obs_tab_id = None
        elif not result.get("ok"):
            obs_cache.invalidate()
        elif cached_obs is not None:
            obs_cache.store(cached_obs)
            last_obs_url = observation_url(cached_obs)
            last_obs_tab_id = observation_tab_id(cached_obs)
        else:
            obs_cache.invalidate()
        if needs_confirm and approved:
            try:
                from .auth_validator import verify_authentication_state
                post_obs = cached_obs or raw_obs
                is_authed, auth_msg = verify_authentication_state(post_obs)
                if not is_authed:
                    penalties.append(f"Authentication unverified: {auth_msg}. Do not assume login succeeded.")
            except Exception:
                pass
        if not result.get("ok") or zero_change_repeats > 0:
            cached_obs = None
        if _recovery_raw_obs is not None and not result.get("ok"):
            # Task 1 (automation): the step still failed after the
            # recovery attempt, but the recovery re-observation is
            # already the freshest page state. Plan the next iteration
            # from it instead of spending another observation round
            # trip on the same page — a failed action must still force
            # a fresh observation, and this one already is fresh.
            try:
                _recovery_obs = _to_obs(_recovery_raw_obs)
                cached_obs = _recovery_obs
                obs_cache.store(_recovery_obs)
                last_obs_url = observation_url(_recovery_obs)
                last_obs_tab_id = observation_tab_id(_recovery_obs)
            except Exception:
                pass

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
        step_screenshot = None if suppress_step_screenshot else (fresh_shot or raw_screenshot)
        suppress_step_screenshot = False
        safe_evidence: Any = redact_text_urls(evidence) if isinstance(evidence, str) else evidence
        # Task 4: a substituted vault value must never be recorded in
        # evidence — scrub it even if the extension echoed it back.
        if isinstance(safe_evidence, str):
            for _value in _substituted_values:
                safe_evidence = scrub_value_from_text(safe_evidence, _value)
        try:
            from .step_narrator import generate_step_narrative, _extract_site_and_query
            step_narrative = generate_step_narrative(
                step_num=steps_done,
                action=action,
                result=result,
                instruction=instruction,
                why=first.get("why") if isinstance(first, dict) else None,
                raw_obs=raw_obs,
                previous_site=last_site_name,
            )
            cur_site, _ = _extract_site_and_query(raw_url or (action.get("params") or {}).get("url", ""))
            if cur_site and cur_site != "the website":
                last_site_name = cur_site
        except Exception:
            step_narrative = f"Step {steps_done}: {action.get('type')} -> {safe_evidence}"

        progress(step_narrative, screenshot=step_screenshot,
                 observation=result.get("observation") or raw_obs)

        action_ok = bool(result.get("ok"))
        if action_ok:
            from .completion_verifier import verify_action_receipt
            receipt = verify_action_receipt(action, result, before=raw_obs, goal=instruction)
            # Task 1 (automation): verify the outcome actually
            # happened (URL/title/text/digest change, or the receipt)
            # before the loop advances on this step. A step that
            # reported ok but changed nothing is a silent no-op, not
            # progress — see the handling after history.append.
            outcome = verify_step_outcome(action, result, before=raw_obs, receipt=receipt)
            action_status = "verified" if (receipt["verified"] or outcome["verified"]) else "completed_unverified"
        else:
            receipt = None
            outcome = verify_step_outcome(action, result, before=raw_obs, receipt=None)
            action_status = "failed"
        _outcome_unverified = action_ok and not outcome["verified"]

        entry = {
            "action": action,
            "result_ok": action_ok,
            "status": action_status,
            "evidence": safe_evidence,
            "verification": receipt,
            # Task 1 (automation): the outcome verdict for this step
            # (verified / reason / observable signals / dialog flag),
            # and the recovery if the step was retried with a fresh
            # ref. Both ride the step record, so the timeline and any
            # later audit can show exactly what happened and why.
            "outcome": outcome,
            "recovery": _recovery,
            "narrative": step_narrative,
            # Task 7: trajectory grounding — which element ref this step
            # acted on (if any), and whether the observation the step was
            # planned from carried a screenshot the planner could see.
            # A ref here means the step was grounded; its absence means
            # the legacy name path was used.
            "grounded_ref": (
                (action.get("target") or {}).get("elementRef")
                or (action.get("target") or {}).get("element_ref")
            ) if isinstance(action.get("target"), dict) else None,
            "screenshot_attached": bool(getattr(payload, "screenshot", None)),
        }
        if not action_ok:
            err = result.get("error") or {}
            err_msg = str(err.get("message", "unknown"))
            for _value in _substituted_values:
                err_msg = scrub_value_from_text(err_msg, _value)
            entry["error"] = redact_text_urls(err_msg)
            if not err.get("recoverable", False):
                _save_checkpoint_defensive(task_id, conversation_id, session_id, steps_done, raw_url, history, working_memory)
                return {"final": "failed", "steps": steps_done,
                        "summary": f"non-recoverable error: {redact_text_urls(err_msg)}", "history": history}
            penalty = _same_failure(history, entry)
            if penalty:
                penalties.append(penalty)
        history.append(entry)
        if _recovery is not None:
            working_memory.setdefault("automation_recoveries", []).append(
                {"step": steps_done, **_recovery}
            )
        if _outcome_unverified:
            # Task 1 (automation): a step that reported ok but changed
            # nothing is a silent no-op, not progress. Never pin the
            # next batch step on it (see the batch gate below) and
            # never plan from its cached observation — the next
            # iteration re-observes fresh and replans, with a penalty
            # telling the planner the action had no effect. The step
            # still counts against the budget (a no-op is not free),
            # and the task is NOT failed here: the planner gets one
            # honest chance to try a different target or strategy,
            # and the loop-detection / step-budget guards still bound
            # a planner that keeps repeating it.
            obs_cache.invalidate()
            cached_obs = None
            penalties.append(
                f"Outcome unverified: the {action.get('type')} step reported success but "
                f"{outcome['reason']}. Re-observing and replanning — do not repeat the same "
                "action on the same target without a new strategy."
                + (" A dialog/popup is covering the page." if outcome.get("dialog_detected") else "")
            )
            working_memory.setdefault("automation_recoveries", []).append({
                "kind": "outcome_noop",
                "step": steps_done,
                "action": action.get("type"),
                "reason": outcome["reason"],
                "dialog_detected": bool(outcome.get("dialog_detected")),
            })
        # Task 10: zero retention — the in-memory history is pruned to
        # the last few steps, stripped to the fields loop detection
        # needs; evidence/narratives (which can quote page text) are
        # dropped, and no checkpoint is persisted (see
        # _save_checkpoint_defensive).
        try:
            from .zero_retention import is_enabled as _zr_enabled2
            from .zero_retention import prune_history as _zr_prune

            if _zr_enabled2():
                _zr_prune(history)
        except Exception:
            pass
        # Task 9: long-horizon compaction — every 25 steps the loop
        # writes a deterministic summary into the checkpoint and plans
        # from the summary + the last steps from then on.
        _maybe_compact_history(working_memory, history, instruction, steps_done, long_horizon)
        checkpoint_started = time.perf_counter()
        _save_checkpoint_defensive(task_id, conversation_id, session_id, steps_done, raw_url, history, working_memory)
        metrics("checkpoint", checkpoint_started)
        # Task 11: snapshot this executed step's timings — after the
        # checkpoint, so the checkpoint cost belongs to the step
        # (action type + durations only — never names, values, or
        # page text).
        _step_entry = timings.flush_step(steps_done, action.get("type") or "step", batch_continuation)
        # Task 13: one structured JSON log line per executed step
        # (navigator.observe) — same StepTiming object, no second
        # timing system. See backend/app/observability.py.
        from .observability import log_step as _log_step

        _log_step(task_id, _step_entry, "ok" if result.get("ok") else "failed")

        # Task 11: batch continuation — execute the next step of the
        # already-validated plan without re-planning, when the step
        # just executed cannot have changed the page (a non-submitting
        # type), it succeeded, and the post-action observation is on
        # the same page the plan was built from. Anything else ends
        # the batch: the next iteration re-observes and re-plans.
        # REVIEW (Task 11 audit): "same page" means the same URL *and*
        # the same DOM fingerprint. A type step can mutate the DOM
        # (autocomplete dropdown, validation message, dynamic fields)
        # without navigating, and the pinned plan's refs were quoted
        # against the pre-type tree. URL equality alone let a batch run
        # on after such a change without the planner ever seeing it;
        # the fingerprint (url | title | element count | first 12
        # target ids) catches count/title/identity changes with no
        # extra round trip — the post-action observation is already in
        # memory. A missing baseline fingerprint pins no batch at all
        # (fail closed on the optimisation, never on safety).
        if steps_done < effective_max_steps:
            _exec_idx = next((i for i, s in enumerate(steps) if s is first), 0)
            _remainder = steps[_exec_idx + 1:]
            _dom_unchanged = (
                plan_fingerprint is not None
                and cached_obs is not None
                and observation_fingerprint(cached_obs) == plan_fingerprint
            )
            if (
                _remainder
                and result.get("ok")
                and is_non_submitting_type(first)
                and cached_obs is not None
                and observation_url(cached_obs) == plan_url
                and _dom_unchanged
                # Task 1 (automation): an unverified outcome is a
                # silent no-op — never pin the next batch step on it.
                # The next iteration re-observes and replans instead.
                and not _outcome_unverified
                # Task 12: the operator approved this step only — do
                # not pin the rest of the batch. The next iteration
                # re-observes and re-plans, so the next step gets its
                # own preview and its own confirmation.
                and not step_only_approval
            ):
                pending_batch = {
                    "steps": list(_remainder),
                    "payload": payload,
                    "raw_obs": raw_obs,
                    "tree": plan_tree,
                    "refs": plan_refs,
                    "url": plan_url,
                    "fingerprint": plan_fingerprint,
                }

        if steps_done >= effective_max_steps:
            break

    if is_cancelled(task_id):
        return {"final": "cancelled", "steps": steps_done, "summary": None, "history": history}
    final_obs = cached_obs
    if final_obs is None:
        final_obs = obs_factory()
        if asyncio.iscoroutine(final_obs):
            try:
                final_obs = await final_obs
            except Exception:
                final_obs = {}
    return {"final": "budget_exceeded", "steps": steps_done, "summary": _summary(final_obs, history), "history": history}


async def run_task(task_id: str) -> None:
    """Production entrypoint — wires real hub, real planner, live observation."""
    record: TaskRecord = real_hub.tasks[task_id]
    chat_id = record.chat_id
    latest_cp: Optional[dict[str, Any]] = None

    initial_memory: dict[str, Any] = {}
    try:
        from . import db
        db.init_db()
        user_prefs = db.get_user_preferences()
        if user_prefs:
            initial_memory["user_preferences"] = user_prefs
        from .memory import SemanticMemory
        facts = SemanticMemory().recall()
        if facts:
            initial_memory["semantic_facts"] = [
                {"category": f["category"], "key": f["fact_key"], "value": f["fact_value"]}
                for f in facts
            ]
    except Exception:
        pass

    # Task 9: long-term memory recall. Facts/skills come from the
    # operator only (user-stated or confirmed task outcomes); file
    # summaries are attached for this task's files. The planner sees
    # the recall block in build_prompt — never whole files.
    try:
        from .long_memory import recall_for_prompt

        initial_memory["long_memory"] = recall_for_prompt(record.instruction, task_id=task_id)
    except Exception:
        pass

    # REVIEW (Task 9 audit): the checkpoint must carry the background
    # flag so a task restored after a restart (main._require) keeps it.
    initial_memory["background"] = record.background

    # Task 9: resume from a checkpoint. A task restored from SQLite
    # (main._require) was interrupted by a restart; its checkpoint
    # carries the history and working memory, so the loop continues
    # from where it stopped instead of starting over. A fresh task
    # has no checkpoint for its id, so this is a no-op for new runs.
    try:
        from . import db

        cp = db.get_task_checkpoint(task_id)
        if cp and cp.get("history"):
            initial_memory["prior_checkpoint_history"] = cp["history"]
            for _k, _v in (cp.get("working_memory") or {}).items():
                initial_memory.setdefault(_k, _v)
            if (cp.get("working_memory") or {}).get("long_horizon"):
                record.long_horizon = True
    except Exception:
        pass

    if chat_id:
        try:
            from . import db
            raw_msgs = db.get_recent_messages(chat_id, limit=6)
            if raw_msgs:
                initial_memory["conversation_history"] = [
                    {"role": m["role"], "content": m["content"]} for m in raw_msgs
                ]
            chat_mem = db.get_chat_memory(chat_id)
            if chat_mem:
                initial_memory["session_memory"] = chat_mem
                for k in ("research_topic", "requested_format", "deliverable_type", "extracted_price", "target_item"):
                    if k in chat_mem and k not in initial_memory:
                        initial_memory[k] = chat_mem[k]

            latest_cp = db.get_latest_checkpoint_for_chat(chat_id)

            db.add_message(chat_id, "user", record.instruction, task_id=task_id)

            chat_info = db.get_chat(chat_id)
            if chat_info and chat_info.get("title") in ("New Mission", "New Chat", ""):
                trunc_title = record.instruction[:36].strip()
                if len(record.instruction) > 36:
                    trunc_title += "..."
                db.update_chat_title(chat_id, trunc_title)
        except Exception:
            pass

    # 1. Intent Classification Stage (P5.2 & Operation Disambiguation)
    from .intent_router import classify_intent, TaskIntent, ResearchOperationType
    from .session_manager import session_manager

    active_session = None
    if chat_id:
        active_session = session_manager.get_active_session_for_chat(chat_id)

    latest_pkg_record = None
    if chat_id:
        try:
            from . import db
            latest_pkg_record = db.get_latest_package_for_chat(chat_id)
        except Exception:
            pass

    intent_context = {
        "active_session": active_session,
        "conversation_history": initial_memory.get("conversation_history", []),
        "session_memory": initial_memory.get("session_memory", {}),
        "is_research": bool(initial_memory.get("is_research") or (initial_memory.get("session_memory", {}).get("research_topic")) or latest_pkg_record),
        "requested_format": initial_memory.get("requested_format"),
        "deliverable_type": initial_memory.get("deliverable_type"),
        "research_topic": initial_memory.get("research_topic") or (latest_pkg_record.get("topic_title") if latest_pkg_record else None),
        "research_knowledge_package": latest_pkg_record.get("data") if latest_pkg_record else (latest_cp.get("working_memory", {}).get("research_knowledge_package") if latest_cp else None),
        "research_package_status": latest_pkg_record.get("status") if latest_pkg_record else None,
        "latest_checkpoint": latest_cp,
    }

    classification = classify_intent(record.instruction, context=intent_context)
    record.intent = classification.intent.value
    record.is_browser_task = classification.is_browser

    # -- Task 14: connector auto-routing --------------------------------
    # A Gmail / Calendar / Drive read (or a gated write) goes to the
    # native API connector instead of driving the web UI — but only
    # when the instruction is not a research operation (export /
    # resume / refine / new research own those) and not ambiguous.
    # The runner fails honestly when the connector is not configured;
    # it never silently falls back to the browser. Reads answer
    # directly (the planner never sees connector data); writes go
    # through the confirmation gate inside the runner.
    if classification.operation_type not in {
        ResearchOperationType.EXPORT_EXISTING_PACKAGE,
        ResearchOperationType.RESUME_EXISTING_MISSION,
        ResearchOperationType.REFINE_EXISTING_RESEARCH,
        ResearchOperationType.NEW_RESEARCH,
    } and not classification.is_ambiguous:
        try:
            from .connector_router import route_instruction
            from .connector_runner import run_connector_route

            _route = route_instruction(record.instruction)
        except Exception:
            _route = None
        if _route is not None:
            record.route = _route.to_dict()
            initial_memory["route"] = record.route
            record.state = "running"
            record.message = f"Routing {_route.label}…"
            await real_hub.broadcast_progress(record)
            _outcome = await run_connector_route(
                _route, task_id=task_id, instruction=record.instruction, record=record
            )
            # The runner's ok flag IS the outcome — no completion
            # verifier here. The verifier's navigation rule requires a
            # browser observation of a destination, which a connector
            # answer legitimately has none of ("No emails matched" is a
            # completed answer, not an incomplete navigation).
            record.steps_done = 1
            record.state = "completed" if _outcome.ok else "failed"
            record.message = _outcome.message
            await real_hub.broadcast_progress(record)
            if chat_id:
                try:
                    _persist_agent_message(
                        chat_id, record.message,
                        evidence="connector" if _outcome.ok else "failed",
                        task_id=task_id,
                    )
                except Exception:
                    pass
            return

    # -----------------------------------------------------------------------
    # Explicit Operation Dispatch:
    # 1. EXPORT_EXISTING_PACKAGE: Deliverable-only execution (Zero browser ops)
    # 2. RESUME_EXISTING_MISSION: Continue/resume pending research or export
    # 3. REFINE_EXISTING_RESEARCH: Refine existing package preserving evidence
    # 4. NEW_RESEARCH: Fresh isolated research mission
    # -----------------------------------------------------------------------

    # A. Deliverable-Only Execution
    if classification.operation_type == ResearchOperationType.EXPORT_EXISTING_PACKAGE:
        export_session_id = f"sess_export_{task_id}"
        record.state = "running"
        record.message = "Preparing deliverable export..."
        await real_hub.broadcast_progress(record)

        from .research.package_resolver import resolve_target_package
        res = resolve_target_package(
            chat_id=chat_id,
            instruction=record.instruction,
            context=intent_context,
            requested_format=classification.requested_format or "pdf",
            completed_only=True,
        )
        if res.is_ambiguous:
            record.steps_done = 1
            record.state = "completed"
            record.message = res.clarification_message
            _save_checkpoint_defensive(
                task_id=task_id,
                conversation_id=chat_id,
                session_id=export_session_id,
                steps_done=1,
                raw_url="",
                history=[{"role": "assistant", "content": record.message}],
                working_memory={"research_stage": "completed", "clarification": res.clarification_message},
            )
            await real_hub.broadcast_progress(record)
            if chat_id:
                try:
                    _persist_agent_message(chat_id, record.message, evidence="ambiguity_clarification", task_id=task_id)
                except Exception:
                    pass
            return

        if not res.package:
            record.steps_done = 1
            record.state = "completed"
            record.message = (
                "No prior research findings or package found in this conversation to export. "
                "Please specify a topic to research first (e.g., 'Research transformer architecture and generate a PDF')."
            )
            _save_checkpoint_defensive(
                task_id=task_id,
                conversation_id=chat_id,
                session_id=export_session_id,
                steps_done=1,
                raw_url="",
                history=[{"role": "assistant", "content": record.message}],
                working_memory={"research_stage": "completed", "error": record.message},
            )
            await real_hub.broadcast_progress(record)
            if chat_id:
                try:
                    _persist_agent_message(chat_id, record.message, evidence="conversational", task_id=task_id)
                except Exception:
                    pass
            return

        pkg_data = res.package.get("data") or res.package
        from .research.models import ResearchKnowledgePackage
        pkg = ResearchKnowledgePackage.from_dict(pkg_data) if isinstance(pkg_data, dict) else pkg_data

        raw_target_fmt = (
            classification.requested_format
            or ("pptx" if classification.deliverable_type == "presentation" else ("xlsx" if classification.deliverable_type == "dataset" else "pdf"))
        ).lower().strip(" .")
        target_fmt = canonical_format_map.get(raw_target_fmt, raw_target_fmt)
        deliv_type = classification.deliverable_type or ("presentation" if target_fmt == "pptx" else "report")
        working_memory = dict(initial_memory)
        from .research.adaptive_planner import generate_adaptive_research_plan
        plan = generate_adaptive_research_plan(record.instruction, {
            "research_topic": pkg.topic.title,
            "deliverable_type": deliv_type,
            "requested_format": target_fmt,
        })
        working_memory["research_plan"] = plan.model_dump()
        pkg.metadata["research_plan"] = plan.model_dump()
        working_memory["research_knowledge_package"] = pkg.to_dict()
        working_memory["requested_format"] = target_fmt
        working_memory["deliverable_type"] = deliv_type
        working_memory["is_research"] = True
        working_memory["instruction"] = record.instruction

        primary_art = _finalize_research_artifacts(task_id, chat_id, working_memory, summary_msg=pkg.topic.findings_summary or pkg.topic.title)
        if primary_art and Path(primary_art["filepath"]).exists() and primary_art["file_size_bytes"] > 0:
            record.steps_done = 1
            record.state = "completed"
            fn = primary_art["filename"]
            fmt_upper = primary_art["file_format"].upper()
            sz_kb = primary_art["file_size_bytes"] / 1024.0
            record.message = (
                f"Successfully compiled the research findings into **{fn}** ({fmt_upper}, {sz_kb:.1f} KB).\n\n"
                f"📄 **Deliverable Ready:** [{fn}] ({fmt_upper}, {sz_kb:.1f} KB). Available for download."
            )
            working_memory["research_stage"] = "completed"
            working_memory["last_deliverable"] = fn
            _save_checkpoint_defensive(
                task_id=task_id,
                conversation_id=chat_id,
                session_id=export_session_id,
                steps_done=1,
                raw_url="",
                history=[{"role": "assistant", "content": record.message}],
                working_memory=working_memory,
            )
            await real_hub.broadcast_progress(record)
            if chat_id:
                try:
                    _persist_derived_memory(chat_id, "last_deliverable", fn)
                    _persist_derived_memory(chat_id, "requested_format", primary_art["file_format"])
                    _persist_agent_message(chat_id, record.message, evidence="deliverable_exported", task_id=task_id)
                except Exception:
                    pass
            return
        else:
            record.state = "failed"
            record.message = f"Failed to generate artifact in {target_fmt.upper()} format."
            _save_checkpoint_defensive(
                task_id=task_id,
                conversation_id=chat_id,
                session_id=export_session_id,
                steps_done=1,
                raw_url="",
                history=[{"role": "assistant", "content": record.message}],
                working_memory={"research_stage": "failed", "error": record.message},
            )
            await real_hub.broadcast_progress(record)
            return

    # B. Continuation / Resumption Handling
    if classification.operation_type == ResearchOperationType.RESUME_EXISTING_MISSION:
        from . import db
        # Check for interrupted in-flight checkpoint
        if latest_cp and latest_cp.get("step_index", 0) > 0 and latest_cp.get("working_memory", {}).get("research_stage") != "completed":
            initial_memory.update(latest_cp.get("working_memory", {}))
            if latest_cp.get("history"):
                initial_memory["prior_checkpoint_history"] = latest_cp["history"]
            record.message = f"Resuming interrupted research from step {latest_cp.get('step_index', 0)}..."
            await real_hub.broadcast_progress(record)
        else:
            # Check for completed package with pending export or already complete
            latest_pkg_row = db.get_latest_package_for_chat(chat_id) if chat_id else None
            if latest_pkg_row:
                topic_title = latest_pkg_row.get("topic_title", "Research")
                existing_arts = db.list_conversation_artifacts(chat_id) if chat_id else []
                raw_target_fmt = classification.requested_format or (initial_memory.get("session_memory") or {}).get("requested_format")
                if not raw_target_fmt and classification.deliverable_type == "presentation":
                    raw_target_fmt = "pptx"
                elif not raw_target_fmt and classification.deliverable_type == "dataset":
                    raw_target_fmt = "xlsx"
                target_fmt = canonical_format_map.get(raw_target_fmt.lower().strip(" ."), raw_target_fmt) if raw_target_fmt else None
                has_matching_art = any(a.get("file_format") == target_fmt for a in existing_arts) if target_fmt else bool(existing_arts)

                if target_fmt and not has_matching_art:
                    pkg_data = latest_pkg_row.get("data") or {}
                    working_mem = dict(initial_memory)
                    working_mem["research_knowledge_package"] = pkg_data
                    working_mem["requested_format"] = target_fmt
                    working_mem["deliverable_type"] = classification.deliverable_type or "report"
                    working_mem["is_research"] = True
                    primary_art = _finalize_research_artifacts(task_id, chat_id, working_mem, summary_msg=topic_title)
                    if primary_art:
                        record.steps_done = 1
                        record.state = "completed"
                        fn = primary_art["filename"]
                        fmt_upper = primary_art["file_format"].upper()
                        sz_kb = primary_art["file_size_bytes"] / 1024.0
                        record.message = (
                            f"Successfully compiled the pending deliverable for **{topic_title}** into **{fn}** ({fmt_upper}, {sz_kb:.1f} KB).\n\n"
                            f"📄 **Deliverable Ready:** [{fn}] ({fmt_upper}, {sz_kb:.1f} KB). Available for download."
                        )
                        await real_hub.broadcast_progress(record)
                        if chat_id:
                            try:
                                _persist_derived_memory(chat_id, "last_deliverable", fn)
                                _persist_agent_message(chat_id, record.message, evidence="deliverable_exported", task_id=task_id)
                            except Exception:
                                pass
                        return
                else:
                    last_deliv = (initial_memory.get("session_memory") or {}).get("last_deliverable") or (existing_arts[0]["filename"] if existing_arts else "")
                    record.steps_done = 1
                    record.state = "completed"
                    deliv_info = f" and deliverable **{last_deliv}** are" if last_deliv else " is"
                    record.message = f"The research on **{topic_title}**{deliv_info} already completed. What would you like to explore next?"
                    await real_hub.broadcast_progress(record)
                    if chat_id:
                        try:
                            _persist_agent_message(chat_id, record.message, evidence="conversational", task_id=task_id)
                        except Exception:
                            pass
                    return
            else:
                record.steps_done = 1
                record.state = "completed"
                record.message = "There is no active or paused mission to continue in this conversation. What would you like to research?"
                await real_hub.broadcast_progress(record)
                if chat_id:
                    try:
                        _persist_agent_message(chat_id, record.message, evidence="conversational", task_id=task_id)
                    except Exception:
                        pass
                return

    # C. Refinement of Existing Research
    if classification.operation_type == ResearchOperationType.REFINE_EXISTING_RESEARCH:
        from .research.package_resolver import resolve_target_package
        from .research.models import ResearchKnowledgePackage, ResearchSubtopic
        res = resolve_target_package(chat_id=chat_id, instruction=record.instruction, context=intent_context)
        if res.package:
            pkg_data = res.package.get("data") or res.package
            pkg = ResearchKnowledgePackage.from_dict(pkg_data) if isinstance(pkg_data, dict) else pkg_data
            refinement_focus = classification.research_topic or record.instruction
            sub_id = f"sub_{len(pkg.topic.subtopics) + 1:03d}"
            pkg.topic.subtopics.append(ResearchSubtopic(
                subtopic_id=sub_id,
                title=refinement_focus,
                queries=[f"{pkg.topic.title} {refinement_focus}"],
                status="in_progress",
            ))
            initial_memory["research_knowledge_package"] = pkg.to_dict()
            initial_memory["research_topic"] = f"{pkg.topic.title} - {refinement_focus}"
            initial_memory["is_research"] = True
            initial_memory["requested_format"] = classification.requested_format or pkg.topic.target_format
            initial_memory["deliverable_type"] = classification.deliverable_type
            if chat_id:
                try:
                    from . import db
                    db.save_research_package(pkg.to_dict(), chat_id, task_id, status="running")
                except Exception:
                    pass

    # D. New Research Mission: Isolated state, step 0, no old completion notes
    elif classification.operation_type == ResearchOperationType.NEW_RESEARCH:
        initial_memory["is_research"] = True
        initial_memory["research_topic"] = classification.research_topic or record.instruction
        norm_fmt = classification.requested_format or ("pptx" if classification.deliverable_type == "presentation" else ("xlsx" if classification.deliverable_type == "dataset" else None))
        initial_memory["requested_format"] = canonical_format_map.get(norm_fmt.lower().strip(" ."), norm_fmt) if norm_fmt else None
        initial_memory["deliverable_type"] = classification.deliverable_type
        from .research.models import ResearchKnowledgePackage, ResearchSubtopic, ResearchTopic
        from .research.adaptive_planner import generate_adaptive_research_plan

        plan = generate_adaptive_research_plan(record.instruction, {
            "research_topic": classification.research_topic or record.instruction,
            "deliverable_type": classification.deliverable_type,
            "requested_format": classification.requested_format,
        })
        initial_memory["research_plan"] = plan.model_dump()

        pkg = ResearchKnowledgePackage(
            package_id=f"pkg_{task_id}",
            topic=ResearchTopic(
                title=classification.research_topic or record.instruction,
                objective=record.instruction,
                target_format=classification.requested_format,
            ),
        )
        pkg.metadata["research_plan"] = plan.model_dump()
        for sp in plan.subtopics:
            pkg.topic.subtopics.append(ResearchSubtopic(
                subtopic_id=sp.subtopic_id,
                title=sp.title,
                queries=[sp.search_query],
            ))
        initial_memory["research_knowledge_package"] = pkg.to_dict()
        if chat_id:
            try:
                from . import db
                db.save_research_package(pkg.to_dict(), chat_id, task_id, status="running")
                db.set_chat_memory(chat_id, "research_topic", str(initial_memory["research_topic"]))
                if initial_memory.get("requested_format"):
                    db.set_chat_memory(chat_id, "requested_format", str(initial_memory["requested_format"]))
                if initial_memory.get("deliverable_type"):
                    db.set_chat_memory(chat_id, "deliverable_type", str(initial_memory["deliverable_type"]))
            except Exception:
                pass

    # 2. NON_BROWSER Intent: Conversational fast-path (Zero browser interactions)
    if not classification.is_browser:
        from .capabilities import check_supported_capability, is_capability_query, format_capability_overview
        from .completion_verifier import verify_task_completion

        # Check for explicitly unsupported system operations
        is_supp, supp_reason = check_supported_capability(record.instruction)
        if not is_supp:
            record.state = "failed"
            record.message = supp_reason or "Unsupported capability requested."
            await real_hub.broadcast_progress(record)
            if chat_id:
                try:
                    _persist_agent_message(chat_id, record.message, evidence="unsupported", task_id=task_id)
                except Exception:
                    pass
            return

        record.state = "running"
        record.message = "Thinking..."
        await real_hub.broadcast_progress(record)

        try:
            from .email_intelligence.mission_setup import (
                is_email_mission_setup_instruction,
                handle_email_intelligence_instruction,
            )
            from .email_intelligence.companion_queries import is_email_companion_query
            from .connectors.calendar_companion import (
                is_calendar_query,
                handle_calendar_instruction,
                handle_compound_calendar,
            )

            from .continuous_monitoring_companion import (
                is_continuous_monitoring_directive,
                handle_continuous_monitoring_instruction,
            )
            from .memory_companion import (
                is_remember_directive,
                handle_remember_instruction,
            )
            from .email_dispatch import (
                is_email_dispatch_instruction,
                handle_email_dispatch,
            )

            is_dispatch, dispatch_recip = is_email_dispatch_instruction(record.instruction)

            if is_email_mission_setup_instruction(record.instruction) or is_email_companion_query(record.instruction):
                answer = await handle_email_intelligence_instruction(record.instruction, chat_id=chat_id)
                # Compound: the same instruction may also ask for a
                # calendar event ("...find the Microsoft interview
                # invitation, and create a calendar event for it...").
                # The email handler consumes the whole instruction, so
                # without this the calendar half vanishes silently —
                # no event, no message, nothing. Only for a mission
                # setup, not a pure companion query.
                if is_email_mission_setup_instruction(record.instruction):
                    cal_section = await handle_compound_calendar(record.instruction, chat_id=chat_id)
                    if cal_section:
                        answer = f"{answer}\n\n---\n\n{cal_section}"
            elif is_calendar_query(record.instruction):
                answer = await handle_calendar_instruction(record.instruction, chat_id=chat_id)
            elif is_continuous_monitoring_directive(
                record.instruction,
                history=initial_memory.get("conversation_history", []),
            ):
                answer = await handle_continuous_monitoring_instruction(
                    record.instruction,
                    chat_id=chat_id,
                    history=initial_memory.get("conversation_history", []),
                )
            elif is_remember_directive(record.instruction):
                # Chat-only memory entry (2026-11-18): "remember ..." in
                # chat stores the fact directly (same store and PII
                # rejection as the removed form) — no browser task.
                answer = handle_remember_instruction(record.instruction)
            elif is_dispatch and dispatch_recip:
                answer = await handle_email_dispatch(
                    record.instruction,
                    recipient=dispatch_recip,
                    chat_id=chat_id,
                    history=initial_memory.get("conversation_history", []),
                )
            elif is_capability_query(record.instruction):
                answer = format_capability_overview()
            else:
                provider = _provider_for_config()
                history = initial_memory.get("conversation_history", [])
                # Recall: the operator's stored facts must reach the
                # conversational path too. The browser path gets them via
                # working_memory, but chat_call only received the
                # instruction and history — so a recall question ("do you
                # remember my preferred language?") could never be
                # answered from memory. Append the recalled facts (and
                # preferences) to the system prompt; the model is told
                # to say it does not remember when the fact is absent,
                # never to invent one.
                sys_prompt = None
                mem_lines: list[str] = []
                prefs = initial_memory.get("user_preferences") or {}
                if prefs:
                    mem_lines.append(f"Operator preferences: {json.dumps(prefs)}")
                facts = initial_memory.get("semantic_facts") or []
                if facts:
                    mem_lines.append("Facts the operator asked you to remember:")
                    for f in facts:
                        mem_lines.append(
                            f"- {f.get('category', '')}: {f.get('key', '')} = {f.get('value', '')}"
                        )
                if mem_lines:
                    from .capabilities import SYSTEM_CAPABILITY_PROMPT

                    sys_prompt = (
                        SYSTEM_CAPABILITY_PROMPT
                        + "\n\n<remembered-facts (operator-stated, trusted)>\n"
                        + "\n".join(mem_lines)
                        + "\nAnswer recall questions from these facts. If the fact is not listed, say you do not remember it — do not invent one.\n</remembered-facts>"
                    )
                if hasattr(provider, "chat_call"):
                    try:
                        answer = provider.chat_call(record.instruction, history=history, system_prompt=sys_prompt)
                    except Exception:
                        from .providers.mock import MockProvider
                        answer = MockProvider().chat_call(record.instruction, history=history)
                else:
                    answer = "I have processed your request."
                # Fix 5: a conversational product suggestion states
                # prices no browsing ever verified (live case: the
                # fitness-band suggestion answered from LLM knowledge
                # with ₹3,899 and a Flipkart link). Say so plainly
                # and offer the alert, so the daily-digest / watch
                # path is reachable instead of ending in a bare
                # verdict.
                from .continuous_monitoring_companion import (
                    ALERT_OFFER_MARKER,
                    ALERT_OFFER_TEXT,
                    UNVERIFIED_SUGGESTION_NOTE,
                    needs_unverified_suggestion_note,
                )
                if needs_unverified_suggestion_note(record.instruction, answer):
                    if "didn't check the stores" not in answer:
                        answer = f"{answer}\n\n{UNVERIFIED_SUGGESTION_NOTE}"
                    if ALERT_OFFER_MARKER not in answer:
                        answer = f"{answer}\n\n{ALERT_OFFER_TEXT}"

            verified = verify_task_completion(
                instruction=record.instruction,
                candidate_state="completed",
                message=answer,
                observation=None,
                working_memory=initial_memory,
            )
            record.steps_done = 1
            record.state = verified.state
            record.message = verified.message
            await real_hub.broadcast_progress(record)
            if chat_id:
                try:
                    evidence_tag = "conversational" if verified.state == "completed" else "failed"
                    _persist_agent_message(chat_id, verified.message, evidence=evidence_tag, task_id=task_id)
                except Exception:
                    pass
            return
        except Exception as exc:
            cancelled = record.cancelled or is_cancelled(task_id)
            record.state = "cancelled" if cancelled else "failed"  # type: ignore[assignment]
            record.message = "" if cancelled else f"Error: {exc}"
            await real_hub.broadcast_progress(record)
            if chat_id:
                try:
                    _persist_agent_message(chat_id, f"Error: {exc}", evidence="failed", task_id=task_id)
                except Exception:
                    pass
            return

    # 3. BROWSER Tasks (NEW_BROWSER_TASK, CONTINUE_BROWSER_TASK, USE_EXISTING_USER_TAB)
    if not real_hub.extension_sockets:
        record.state = "failed"
        record.message = "EXTENSION_DISCONNECTED: Chrome extension is not connected."
        await real_hub.broadcast_progress(record)
        if chat_id:
            try:
                _persist_agent_message(chat_id, "Error: Chrome extension is not connected.", evidence="failed", task_id=task_id)
            except Exception:
                pass
        return

    # Check for ambiguous user tab request or ambiguous destination entity
    if classification.is_ambiguous:
        from .website_resolver import is_ambiguous_entity, format_ambiguity_clarification
        entity_hint = classification.target_tab_hint or record.instruction
        is_ambig, term, candidates = is_ambiguous_entity(entity_hint)
        if is_ambig and candidates:
            clarification_msg = format_ambiguity_clarification(term or entity_hint, candidates)
            record.state = "failed"
            record.message = clarification_msg
        else:
            record.state = "failed"
            record.message = "AMBIGUOUS_TAB: Cannot determine target tab. Please specify a specific site or tab."
        await real_hub.broadcast_progress(record)
        if chat_id:
            try:
                _persist_agent_message(chat_id, record.message, evidence="ambiguity_refusal", task_id=task_id)
            except Exception:
                pass
        return

    # Autonomous Website Discovery initialization (Scenario A vs B)
    try:
        from .website_resolver import website_resolver
        discovery_res = website_resolver.discover_websites_for_task(
            record.instruction,
            region=initial_memory.get("user_region", "IN"),
            context=initial_memory,
        )
        initial_memory["autonomous_discovery"] = discovery_res
    except Exception:
        pass

    # Session acquisition
    origin = "new_task"
    if classification.intent == TaskIntent.CONTINUE_BROWSER_TASK and active_session:
        session = session_manager.continue_session(active_session.session_id, task_id)
        if not session:
            session = session_manager.create_session(
                conversation_id=chat_id or "default",
                task_id=task_id,
                origin="new_task",
            )
        else:
            origin = "reused_session"
    elif classification.intent == TaskIntent.USE_EXISTING_USER_TAB:
        session = session_manager.create_session(
            conversation_id=chat_id or "default",
            task_id=task_id,
            origin="existing_user_tab",
        )
        origin = "existing_user_tab"
    else:
        session = session_manager.create_session(
            conversation_id=chat_id or "default",
            task_id=task_id,
            origin="new_task",
        )

    record.session_id = session.session_id
    record.primary_tab_id = session.primary_tab_id
    record.child_tab_ids = session.child_tab_ids

    # Initialize session in the extension
    init_res = await real_hub.send_action(task_id, {
        "type": "init_session",
        "sessionId": session.session_id,
        "useExistingTab": (origin == "existing_user_tab"),
        "tabHint": classification.target_tab_hint,
        "timeoutMs": 15000,
    })

    if not init_res.get("ok"):
        err_info = init_res.get("error") or {}
        err_code = err_info.get("code", "SESSION_INIT_FAILED")
        err_msg = err_info.get("message", "Failed to initialize browser session")
        record.state = "failed"
        record.message = f"{err_code}: {err_msg}"
        session_manager.update_status(session.session_id, "failed")
        await real_hub.broadcast_progress(record)
        if chat_id:
            try:
                _persist_agent_message(chat_id, f"Error ({err_code}): {err_msg}", evidence="failed", task_id=task_id)
            except Exception:
                pass
        return

    # Pin primary tab ID
    primary_tab_id = init_res.get("primaryTabId")
    if primary_tab_id:
        session_manager.pin_primary_tab(session.session_id, primary_tab_id)
        record.primary_tab_id = primary_tab_id
        await real_hub.broadcast_progress(record)

    def on_step_progress(steps: int = 0, msg: str = "", screenshot: Optional[str] = None,
                         observation: Any = None) -> None:
        if observation is not None:
            record.last_observation = observation
        # REVIEW (Task 9 audit): the loop's tab registry lives in the
        # loop's working memory, but GET /api/task/{id} reports
        # record.tabs — and hub.register_tab is never called by the
        # loop, so record.tabs was [] forever. Mirror the registry
        # onto the record on every progress call so polling sees it.
        _wm_tabs = initial_memory.get("tabs")
        if isinstance(_wm_tabs, list):
            record.tabs = [dict(t) for t in _wm_tabs if isinstance(t, dict)]
        # Task 14: mirror the routing decision the same way, so the
        # workspace can show "via Gmail connector" / "via WebMCP tool
        # X" / "via UCP checkout" while the task runs.
        _wm_route = initial_memory.get("route")
        if isinstance(_wm_route, dict):
            record.route = dict(_wm_route)
        if not msg:
            return
        record.steps_done = steps
        record.message = msg
        sess = session_manager.get_session(session.session_id)
        if sess:
            record.primary_tab_id = sess.primary_tab_id
            record.child_tab_ids = sess.child_tab_ids
        # A terminal task must not be flipped back to running by a late step.
        if record.state not in ("completed", "failed", "cancelled", "paused"):
            record.state = "running"
        # The record tracks the CURRENT step's screenshot, including
        # None: a screenshot-less step must clear the frame, not leave
        # the previous step's image up as if it were this step's visual
        # (the old `if screenshot:` guard made it sticky).
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
            takeover_gate=_TakeoverGate(record),
            takeover_timeout_s=config.TAKEOVER_TIMEOUT_S,
            on_metrics=on_metrics,
            initial_memory=initial_memory,
            session_id=session.session_id,
            conversation_id=chat_id or "default",
            allowed_domains=record.allowed_domains,
            long_horizon=record.long_horizon,
        )
        candidate_final = {"completed": "completed", "failed": "failed", "cancelled": "cancelled",
                           "budget_exceeded": "failed"}.get(state["final"], "failed")
        from .completion_verifier import verify_task_completion
        verified = verify_task_completion(
            instruction=record.instruction,
            candidate_state=candidate_final,
            message=state.get("summary") or "",
            observation=state.get("last_obs") or ({"url": state["url"]} if state.get("url") else None),
            working_memory=state.get("working_memory") or initial_memory,
            history=state.get("history") or [],
        )
        record.last_observation = state.get("last_obs")
        # Task 11: per-step timings (durations + counts + action types
        # only) for GET /api/task/{id} and progress broadcasts.
        record.timings = state.get("timings")
        if record.cancelled or is_cancelled(task_id):
            final_state = "cancelled"
            record.message = "Task cancelled by user."
        elif record.state != "cancelled":
            final_state = verified.state
            record.message = verified.message
        else:
            final_state = "cancelled"
        record.state = final_state  # type: ignore[assignment]
        # Comparison document: the instruction asked for a document
        # format and the task collected multi-site comparison facts.
        # Browser comparison tasks never reached the research document
        # pipeline, so "give the comparison in an Excel" produced no
        # file. Bridge it here through the existing artifact pipeline
        # (xlsx/docx/pdf/pptx + download endpoint). A missing or
        # failed render must never fail the task itself.
        if classification.requested_format:
            try:
                _cmp_wm = state.get("working_memory") or initial_memory
                _cmp_tracker = _cmp_wm.get("multi_site_research")
                if _cmp_tracker:
                    _cmp_pkg = build_comparison_package(
                        task_id, record.instruction, _cmp_tracker,
                        summary_msg=record.message,
                        target_format=classification.requested_format,
                    )
                    if _cmp_pkg is not None:
                        _cmp_wm["research_knowledge_package"] = _cmp_pkg.to_dict()
                        _cmp_wm["requested_format"] = canonical_format_map.get(
                            classification.requested_format, classification.requested_format
                        )
                        _cmp_wm["instruction"] = record.instruction
                        _cmp_art = _finalize_research_artifacts(task_id, chat_id, _cmp_wm, summary_msg=record.message)
                        if _cmp_art and Path(_cmp_art["filepath"]).exists() and _cmp_art["file_size_bytes"] > 0:
                            _fn = _cmp_art["filename"]
                            _fmt = _cmp_art["file_format"].upper()
                            _kb = _cmp_art["file_size_bytes"] / 1024.0
                            record.message = (record.message or "") + (
                                f"\n\n📄 **Deliverable Ready:** [{_fn}] ({_fmt}, {_kb:.1f} KB). Available for download."
                            )
            except Exception:
                pass
        is_planner_err = "Planner unavailable" in (record.message or "") or "Planner timed out" in (record.message or "")
        sess_status = "active" if (is_planner_err or final_state == "completed") else final_state
        session_manager.update_status(session.session_id, sess_status)
        await real_hub.broadcast_progress(record)

        if chat_id:
            try:
                summary = state.get("summary") or "Mission finished."
                evidence_info = f"{final_state} ({state.get('steps', record.steps_done)} steps)"
                _persist_agent_message(chat_id, summary, evidence=evidence_info, task_id=task_id)
                if final_state == "completed" or (state.get("history") and any(h.get("result_ok") for h in state.get("history"))):
                    facts = extract_mission_facts(record.instruction, summary, state.get("history") or [])
                    for k, v in facts.items():
                        _persist_derived_memory(chat_id, k, v)
            except Exception:
                pass

        # REVIEW (Task 9 audit): record the terminal outcome in the
        # checkpoint. Without it, a task whose record was pruned (e.g.
        # after a backend restart) is restored by main._require as
        # "interrupted" even when it actually completed — the old code
        # guessed from the last chat message, which scheduled tasks
        # (no chat) do not have. This save runs BEFORE the proposal
        # block below: save_proposals re-reads the checkpoint and adds
        # proposals to it, so the final_state marker survives.
        _final_wm = state.get("working_memory") or initial_memory
        _final_wm["final_state"] = final_state
        _final_tabs = _final_wm.get("tabs")
        if isinstance(_final_tabs, list):
            record.tabs = [dict(t) for t in _final_tabs if isinstance(t, dict)]
        _save_checkpoint_defensive(
            task_id, chat_id, session.session_id,
            int(state.get("steps", record.steps_done) or 0),
            str(state.get("url") or ""), state.get("history") or [], _final_wm,
        )

        # Task 9: propose memory from this task's confirmed outcomes.
        # Proposals are stored in the checkpoint — NOT as facts. The
        # operator confirms each one explicitly via
        # POST /api/memory/long/proposals/{task_id}/confirm; unconfirmed
        # proposals are never recalled into any prompt.
        try:
            from .long_memory import propose_from_task, save_proposals

            _proposals = propose_from_task(
                task_id, record.instruction,
                state.get("summary") or "",
                state.get("history") or [],
            )
            if _proposals:
                save_proposals(task_id, _proposals)
        except Exception:
            pass
    except Exception as exc:
        cancelled = record.cancelled or is_cancelled(task_id)
        if record.state != "cancelled":
            record.state = "cancelled" if cancelled else "failed"  # type: ignore[assignment]
        record.message = "" if cancelled else f"loop error: {exc}"
        session_manager.update_status(session.session_id, "cancelled" if cancelled else "failed")
        await real_hub.broadcast_progress(record)

        # REVIEW (Task 9 audit): same terminal marker as the normal
        # path, without touching the already-checkpointed history.
        try:
            from . import db as _db_exc

            _cp_exc = _db_exc.get_task_checkpoint(task_id)
            if _cp_exc is not None:
                _wm_exc = dict(_cp_exc.get("working_memory") or {})
                _wm_exc["final_state"] = record.state
                _save_checkpoint_defensive(
                    task_id, chat_id, session.session_id, record.steps_done,
                    "", _cp_exc.get("history") or [], _wm_exc)
        except Exception:
            pass

        if chat_id:
            try:
                _persist_agent_message(chat_id, f"Error: {exc}", evidence="failed", task_id=task_id)
            except Exception:
                pass
    finally:
        record.pending_confirmation = None
        record.takeover = None


class _LiveObservation:
    """Observation factory: asks the extension for the active tab view each
    round. Raises if the extension socket is gone (loop fails cleanly)."""
    def __init__(self, task_id: str, session_id: Optional[str] = None, tab_id: Optional[int] = None) -> None:
        self._task_id = task_id
        self._session_id = session_id
        self._tab_id = tab_id

    async def __call__(self) -> dict[str, Any]:
        if not real_hub.extension_sockets:
            raise RuntimeError("EXTENSION_DISCONNECTED: no extension socket")
        for attempt in range(2):
            if is_cancelled(self._task_id):
                raise RuntimeError("task cancelled")
            act: dict[str, Any] = {
                "type": "observe", "risk": "read", "verify": "fresh observation", "timeoutMs": 25000,
            }
            if self._session_id:
                act["sessionId"] = self._session_id
            if self._tab_id:
                act["tabId"] = self._tab_id
            res = await real_hub.send_action(self._task_id, act)
            if res.get("ok") and res.get("observation"):
                return res["observation"]
            if is_cancelled(self._task_id):
                raise RuntimeError("task cancelled")
            if attempt == 0:
                await asyncio.sleep(0.5)
        raise RuntimeError(f"observation failed: {res.get('error')}")


def _live_observation_factory(record: TaskRecord) -> _LiveObservation:
    # If session_id is present, let extension dynamically observe session.activeTabId
    tab = None if record.session_id else record.primary_tab_id
    return _LiveObservation(record.task_id, session_id=record.session_id, tab_id=tab)


def _provider_for_config(skip_ollama: bool = False):
    """Pick a planner provider. Explicit mock wins even with keys present.

    Runtime selection (POST /api/llm/config) wins over static config so the
    provider can be switched without restarting the application. Raises only
    when a runtime selection itself cannot be built; the static paths below
    keep their existing fallthrough behavior.

    Task 10 (local planner): ``PRIVAGENT_PLANNER_LOCAL_ONLY=true`` makes
    the chain ollama -> fail — when Ollama is unreachable this raises
    :class:`ProviderUnavailable` and NEVER silently falls back to a cloud
    provider. ``PRIVAGENT_PLANNER_LOCAL_FIRST=true`` (or an explicit
    ``PRIVAGENT_PLANNER=ollama``) tries Ollama first; the Ollama provider
    itself falls back to the normal chain on failure. ``skip_ollama`` is
    the Ollama provider's own fallback guard: it asks for the normal
    chain without re-entering Ollama, so a fallback cannot recurse.
    """
    from . import config
    from .providers import provider_settings
    from .providers.errors import ProviderUnavailable
    if config.PLANNER_LOCAL_ONLY:
        # Local-only is a hard guard: it applies to the runtime selection
        # too, so a stale /api/llm/config choice can never silently send
        # page data to a cloud provider after the operator asked for
        # local-only. An explicit non-local selection is a configuration
        # error, not something to silently override.
        effective = config.PROVIDER
        if provider_settings.active():
            try:
                effective = provider_settings.public_state().get("provider") or effective
            except Exception:
                pass
        if effective not in ("ollama", "mock"):
            raise ProviderUnavailable(
                f"local-only mode is on (PRIVAGENT_PLANNER_LOCAL_ONLY=true) but "
                f"the selected planner is {effective!r}; select ollama (or "
                "mock) or turn local-only off"
            )
        if provider_settings.active():
            return provider_settings.provider_for_runtime()
        if effective == "mock":
            from .providers.mock import MockProvider
            return MockProvider()
        from .providers.ollama import OllamaProvider
        return OllamaProvider()
    if provider_settings.active():
        try:
            return provider_settings.provider_for_runtime()
        except ProviderUnavailable:
            raise
    from .providers.mock import MockProvider
    if config.PROVIDER == "mock":
        return MockProvider()
    if not skip_ollama and (
        config.PROVIDER == "ollama" or getattr(config, "PLANNER_LOCAL_FIRST", False)
    ):
        from .providers.ollama import OllamaProvider
        return OllamaProvider()
    if config.PROVIDER == "deepai" and getattr(config, "DEEPAI_GATEWAY_KEY", ""):
        from .providers.openai_compatible import OpenAICompatibleProvider
        return OpenAICompatibleProvider(
            api_key=config.DEEPAI_GATEWAY_KEY,
            base_url=config.DEEPAI_GATEWAY_BASE_URL,
            model=config.DEEPAI_GATEWAY_MODEL or "deepseek-v4.1-flash",
            provider_name="deepai",
        )
    if config.PROVIDER == "antseed" and config.ANTSEED_API_KEY_ENV:
        from .providers.openai_compatible import OpenAICompatibleProvider
        return OpenAICompatibleProvider(
            api_key=config.ANTSEED_API_KEY_ENV,
            base_url=config.ANTSEED_BASE_URL,
            model=config.ANTSEED_MODEL or None,
            provider_name="antseed",
        )
    if config.PROVIDER == "groq" and config.GROQ_API_KEY:
        from .providers.groq import GroqProvider
        return GroqProvider(api_key=config.GROQ_API_KEY)
    if config.PROVIDER == "gemini" and config.GEMINI_API_KEY:
        from .providers.gemini import GeminiProvider
        return GeminiProvider(api_key=config.GEMINI_API_KEY)
    if getattr(config, "DEEPAI_GATEWAY_KEY", ""):
        from .providers.openai_compatible import OpenAICompatibleProvider
        return OpenAICompatibleProvider(
            api_key=config.DEEPAI_GATEWAY_KEY,
            base_url=config.DEEPAI_GATEWAY_BASE_URL,
            model=config.DEEPAI_GATEWAY_MODEL or "deepseek-v4.1-flash",
            provider_name="deepai",
        )
    if config.GROQ_API_KEY:
        from .providers.groq import GroqProvider
        return GroqProvider(api_key=config.GROQ_API_KEY)
    if config.GEMINI_API_KEY:
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
        return plan_from_observation(instruction=instruction, payload=pf, provider=provider, history=history, working_memory=working_memory, allowed_domains=(working_memory or {}).get("allowed_domains")).model_dump(by_alias=True, exclude_none=True)

    return planner
