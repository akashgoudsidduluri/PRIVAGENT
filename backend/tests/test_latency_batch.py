"""Task 11 — latency: profiling, observation cache, batch actions.

Covers the loop-level behaviour (not the benchmark script):
* batch selection rules (pure function),
* a batch of independent types executing in one planner round trip,
* the batch stopping after a navigation / on a failed step,
* a risky step inside a batch still going through its own
  confirmation gate,
* dependent (unref-grounded) steps never being batched,
* the observation cache (hit after an invalid plan, invalidation on
  failure / tab change / navigation),
* the checker and the allow-list still firing on the planning
  iteration (the checker is only skipped for pinned batch steps),
* timings carrying durations + action types only — never names,
  values, or page text.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app import agentLoop
from app.batch import batch_prefix, is_non_submitting_type, is_ref_grounded
from app.latency import LoopTimings
from app.obs_cache import ObservationCache, observation_fingerprint


LOGIN_URL = "https://moodle.vce.ac.in/login/index.php"
SEARCH_URL = "https://moodle.vce.ac.in/"


def _el(target_id: str, role: str, name: str, tag: str = "input",
         input_type: str | None = None) -> dict[str, Any]:
    el: dict[str, Any] = {
        "targetId": target_id,
        "role": role,
        "name": name,
        "tag": tag,
        "visible": True,
        "enabled": True,
        "bounds": {"x": 100, "y": 100, "width": 200, "height": 30},
    }
    if input_type is not None:
        el["inputType"] = input_type
    return el


def _obs(url: str = LOGIN_URL, title: str = "Log in",
         elements: list[dict[str, Any]] | None = None,
         page_text: str | None = None) -> dict[str, Any]:
    obs: dict[str, Any] = {
        "tabId": 1,
        "url": url,
        "title": title,
        "interactive": elements if elements is not None else [
            _el("username", "textbox", "Username", input_type="text"),
            _el("password", "textbox", "Password", input_type="password"),
            _el("loginbtn", "button", "Log in", tag="button"),
        ],
        "scroll": {"y": 0, "viewport": {"w": 1280, "h": 800}},
        "dpr": 1,
    }
    if page_text is not None:
        obs["page_text"] = page_text
    return obs


def _type(ref: str, name: str, target_id: str, text: str,
          submit: bool = False, risk: str = "write") -> dict[str, Any]:
    return {
        "type": "type", "risk": risk, "verify": f"typed into {name}",
        "timeoutMs": 5000,
        "target": {"targetId": target_id, "name": name, "elementRef": ref},
        "params": {"text": text, "clear": True, "submit": submit},
    }


def _click(ref: str, name: str, target_id: str, risk: str = "write") -> dict[str, Any]:
    return {
        "type": "click", "risk": risk, "verify": f"clicked {name}",
        "timeoutMs": 5000,
        "target": {"targetId": target_id, "name": name, "elementRef": ref},
    }


def _step(action: dict[str, Any], why: str = "w") -> dict[str, Any]:
    return {"why": why, "action": action}


class _Hub:
    """Fake extension hub; returns the observation of the page the
    action leaves the browser on."""

    def __init__(self, after: dict[str, Any] | None = None,
                 fail_on: dict[str, int] | None = None,
                 no_obs_types: frozenset[str] = frozenset()) -> None:
        self.after = after if after is not None else _obs()
        self.fail_on = fail_on or {}
        self.no_obs_types = no_obs_types
        self.calls = 0
        self.dispatched: list[str] = []

    async def send_action(self, task_id: str, action: dict[str, Any],
                          timeout_s: float = 45.0) -> dict[str, Any]:
        self.calls += 1
        self.dispatched.append(action.get("type", "?"))
        if self.fail_on.get(action.get("type", ""), 0) >= self.calls:
            return {"ok": False,
                    "error": {"code": "STALE_TARGET", "message": "gone",
                              "recoverable": True}}
        if action.get("type") in self.no_obs_types:
            return {"ok": True, "evidence": "ok"}
        if action.get("type") == "click" and (action.get("target") or {}).get("targetId") == "loginbtn":
            return {"ok": True, "evidence": "logged in",
                    "observation": _obs(url=SEARCH_URL, title="Home", elements=[
                        _el("search", "searchbox", "Search courses", input_type="search"),
                        _el("searchbtn", "button", "Search courses", tag="button"),
                    ])}
        return {"ok": True, "evidence": "ok", "observation": self.after}


def _run(hub: _Hub, planner, obs_factory, **kwargs) -> dict[str, Any]:
    return asyncio.run(agentLoop.run_with_injection(
        hub, planner=planner, obs_factory=obs_factory,
        task_id="latency-batch-test", instruction="log in and search",
        **kwargs,
    ))


# ---------------------------------------------------------------------------
# Batch selection (pure function)
# ---------------------------------------------------------------------------

def test_batch_prefix_type_run_plus_terminator():
    refs = {"e0", "e1", "e2"}
    steps = [
        _step(_type("e0", "Username", "username", "student")),
        _step(_type("e1", "Password", "password", "hunter2")),
        _step(_click("e2", "Log in", "loginbtn")),
    ]
    assert batch_prefix(steps, refs) == steps


def test_batch_prefix_stops_after_terminator():
    refs = {"e0", "e1", "e2", "e3"}
    steps = [
        _step(_type("e0", "Username", "username", "student")),
        _step(_click("e1", "Log in", "loginbtn")),
        _step(_type("e2", "Search", "search", "x")),
    ]
    assert batch_prefix(steps, refs) == steps[:2]


def test_batch_prefix_non_type_first_step_is_single():
    refs = {"e0", "e1"}
    steps = [
        _step(_click("e0", "Log in", "loginbtn")),
        _step(_type("e1", "Search", "search", "x")),
    ]
    assert batch_prefix(steps, refs) == steps[:1]


def test_batch_prefix_submitting_type_is_terminator():
    refs = {"e0", "e1"}
    steps = [
        _step(_type("e0", "Search", "search", "maths", submit=True)),
        _step(_click("e1", "Next", "nextbtn")),
    ]
    assert batch_prefix(steps, refs) == steps[:1]


def test_batch_prefix_name_only_step_is_not_batched():
    refs = {"e0", "e1"}
    nameless = _type("e0", "Username", "username", "student")
    nameless["target"] = {"name": "Password"}  # no ref, no targetId
    steps = [
        _step(_type("e0", "Username", "username", "student")),
        _step(nameless),
    ]
    assert batch_prefix(steps, refs) == steps[:1]


def test_batch_prefix_unknown_ref_is_not_batched():
    refs = {"e0"}
    steps = [
        _step(_type("e0", "Username", "username", "student")),
        _step(_type("e9", "Later field", "later", "x")),
    ]
    assert batch_prefix(steps, refs) == steps[:1]


def test_batch_prefix_target_id_grounds_without_ref():
    refs = {"e0"}
    second = _type("e0", "Password", "password", "hunter2")
    second["target"] = {"targetId": "password", "name": "Password"}
    steps = [
        _step(_type("e0", "Username", "username", "student")),
        _step(second),
    ]
    assert batch_prefix(steps, refs) == steps


def test_batch_prefix_empty_and_single():
    assert batch_prefix([], {"e0"}) == []
    assert len(batch_prefix([_step(_click("e0", "Go", "go"))], {"e0"})) == 1


def test_is_non_submitting_type_and_ref_grounding():
    assert is_non_submitting_type(_step(_type("e0", "U", "u", "x")))
    assert not is_non_submitting_type(_step(_type("e0", "U", "u", "x", submit=True)))
    assert not is_non_submitting_type(_step(_click("e0", "Go", "go")))
    assert is_ref_grounded(_step(_type("e0", "U", "u", "x")), {"e0"})
    assert not is_ref_grounded(_step(_type("e9", "U", "u", "x")), {"e0"})


# ---------------------------------------------------------------------------
# Batching in the loop
# ---------------------------------------------------------------------------

def test_two_types_execute_in_one_planner_round_trip():
    planner_calls = 0
    hub = _Hub()

    def planner(instruction, payload, history, working_memory=None):
        nonlocal planner_calls
        planner_calls += 1
        if planner_calls == 1:
            return {"goal": "g", "steps": [
                _step(_type("e0", "Username", "username", "student")),
                _step(_type("e1", "Password", "password", "hunter2")),
                _step(_click("e2", "Log in", "loginbtn")),
            ]}
        return {"goal": "g", "steps": []}

    result = _run(hub, planner, lambda: _obs())

    assert result["final"] == "completed"
    assert result["steps"] == 3
    # One plan for the whole login batch, one empty plan to finish.
    assert planner_calls == 2
    assert hub.dispatched == ["type", "type", "click"]
    timings = result["timings"]
    assert timings["batched_steps"] == 2
    assert timings["steps_executed"] == 3
    assert [s["action"] for s in timings["steps"]] == ["type", "type", "click"]
    assert timings["steps"][1]["batched"] is True
    assert timings["steps"][2]["batched"] is True


def test_batch_stops_after_navigation_step():
    planner_calls = 0
    hub = _Hub()

    def planner(instruction, payload, history, working_memory=None):
        nonlocal planner_calls
        planner_calls += 1
        if planner_calls == 1:
            return {"goal": "g", "steps": [
                _step(_type("e0", "Username", "username", "student")),
                _step({"type": "navigate", "risk": "write", "verify": "went",
                       "timeoutMs": 5000, "params": {"url": SEARCH_URL}}),
            ]}
        return {"goal": "g", "steps": []}

    result = _run(hub, planner, lambda: _obs())
    assert result["final"] == "completed"
    assert result["steps"] == 2
    # The navigate ended the batch; the second plan ran on the new page.
    assert planner_calls == 2
    assert hub.dispatched == ["type", "navigate"]


def test_batch_stops_on_failed_step_and_replans_from_fresh_observation():
    planner_calls = 0
    obs_calls = 0

    class FailingHub(_Hub):
        async def send_action(self, task_id, action, timeout_s=45.0):
            if action.get("type") == "type" and (action.get("target") or {}).get("targetId") == "password":
                self.calls += 1
                self.dispatched.append("type")
                return {"ok": False,
                        "error": {"code": "STALE_TARGET", "message": "gone",
                                  "recoverable": True}}
            return await super().send_action(task_id, action, timeout_s)

    hub = FailingHub()

    def planner(instruction, payload, history, working_memory=None):
        nonlocal planner_calls
        planner_calls += 1
        if planner_calls == 1:
            return {"goal": "g", "steps": [
                _step(_type("e0", "Username", "username", "student")),
                _step(_type("e1", "Password", "password", "hunter2")),
                _step(_click("e2", "Log in", "loginbtn")),
            ]}
        return {"goal": "g", "steps": []}

    def obs_factory():
        nonlocal obs_calls
        obs_calls += 1
        return _obs()

    result = _run(hub, planner, obs_factory)
    # Only the username type succeeded; the password type failed, the
    # batch stopped, the loop re-observed and re-planned (empty plan).
    # steps counts dispatched actions, so the failed attempt counts
    # too — but the click after it was never dispatched. (The password
    # type is dispatched 1 + MAX_RETRIES times: _execute_with_retries
    # re-tries a recoverable failure inside the same step — pre-existing
    # behaviour, not batching.)
    assert result["steps"] == 2
    assert result["final"] == "completed"
    assert planner_calls == 2
    assert "click" not in hub.dispatched
    assert hub.dispatched.count("type") == agentLoop.MAX_RETRIES + 2
    # The failure forced a live re-observation (the cache was invalidated).
    assert obs_calls == 2


def test_dependent_step_with_unknown_ref_is_not_batched():
    planner_calls = 0
    hub = _Hub()

    def planner(instruction, payload, history, working_memory=None):
        nonlocal planner_calls
        planner_calls += 1
        if planner_calls == 1:
            return {"goal": "g", "steps": [
                _step(_type("e0", "Username", "username", "student")),
                _step(_type("e9", "Later field", "later", "x")),
            ]}
        return {"goal": "g", "steps": []}

    result = _run(hub, planner, lambda: _obs())
    assert result["final"] == "completed"
    assert result["steps"] == 1
    # The unknown-ref step was not batched: the loop re-planned for it.
    assert planner_calls == 2
    assert hub.dispatched == ["type"]


def test_redundant_first_step_is_skipped_not_batched():
    """Regression: the Requirement 8 dedup must see the full plan.

    A plan whose steps[0] is a navigation already completed in
    history must advance to steps[1] (the type) instead of executing
    — or batching — the redundant navigation. Trimming the plan to
    its batch prefix before the dedup discarded steps[1], so the
    dedup had nothing to advance to and the loop re-planned forever
    with nothing executed (this broke
    test_provider_recovery::test_never_repeat_completed_browser_actions_after_fallback).
    """
    planner_calls = 0
    hub = _Hub()

    def planner(instruction, payload, history, working_memory=None):
        nonlocal planner_calls
        planner_calls += 1
        if planner_calls == 1:
            return {"goal": "g", "steps": [
                {"why": "re-navigating", "action": {
                    "type": "navigate", "risk": "write",
                    "params": {"url": "https://www.google.com"}}},
                _step(_type("e0", "Search", "search", "privagent", submit=True)),
            ]}
        return {"goal": "g", "steps": []}

    result = _run(
        hub, planner,
        lambda: _obs(url="https://www.google.com/", title="Google", elements=[
            _el("search", "searchbox", "Search", input_type="search"),
        ]),
        initial_memory={"prior_checkpoint_history": [
            {"action": {"type": "navigate",
                        "params": {"url": "https://www.google.com"}},
             "result_ok": True},
        ]},
    )
    assert result["final"] == "completed"
    assert hub.dispatched[0] == "type"
    assert "navigate" not in hub.dispatched


# ---------------------------------------------------------------------------
# Confirmation inside a batch (risky steps keep their own gate)
# ---------------------------------------------------------------------------

class _Gate:
    """Fake confirmation gate that auto-approves or rejects per action type."""

    def __init__(self, reject_types: frozenset[str] = frozenset()) -> None:
        self.confirm_requests: list[dict[str, Any]] = []
        self.decision: bool | None = None
        self.confirm_event = asyncio.Event()
        self.reject_types = reject_types

    async def auto(self) -> None:
        while True:
            if self.confirm_requests and not self.confirm_event.is_set():
                action = self.confirm_requests[-1]["action"]
                self.decision = action.get("type") not in self.reject_types
                self.confirm_event.set()
            await asyncio.sleep(0.01)


def _run_with_gate(hub, planner, obs_factory, gate):
    async def scenario():
        approver = asyncio.create_task(gate.auto())
        try:
            return await agentLoop.run_with_injection(
                hub, planner=planner, obs_factory=obs_factory,
                task_id="latency-batch-gate", instruction="log in",
                confirm_gate=gate,
            )
        finally:
            approver.cancel()

    return asyncio.run(scenario())


def test_risky_step_in_batch_still_requires_confirmation():
    gate = _Gate()
    hub = _Hub()
    planner_calls = 0

    def planner(instruction, payload, history, working_memory=None):
        nonlocal planner_calls
        planner_calls += 1
        if planner_calls == 1:
            return {"goal": "g", "steps": [
                _step(_type("e0", "Username", "username", "student")),
                _step(_type("e1", "Password", "password", "hunter2")),
                _step(_click("e2", "Log in", "loginbtn", risk="sensitive")),
            ]}
        return {"goal": "g", "steps": []}

    result = _run_with_gate(hub, planner, lambda: _obs(), gate)
    assert result["final"] == "completed"
    assert result["steps"] == 3
    # The sensitive click went through the gate on its own — the batch
    # did not merge or skip the confirmation.
    click_requests = [r for r in gate.confirm_requests
                      if r["action"].get("type") == "click"]
    assert len(click_requests) == 1
    assert click_requests[0]["action"].get("risk") == "sensitive"
    assert hub.dispatched == ["type", "type", "click"]
    assert result["timings"]["totals_ms"]["confirm_wait_ms"] >= 0


def test_rejected_risky_step_stops_the_batch():
    gate = _Gate(reject_types=frozenset({"click"}))
    hub = _Hub()
    planner_calls = 0

    def planner(instruction, payload, history, working_memory=None):
        nonlocal planner_calls
        planner_calls += 1
        if planner_calls == 1:
            return {"goal": "g", "steps": [
                _step(_type("e0", "Username", "username", "student")),
                _step(_type("e1", "Password", "password", "hunter2")),
                _step(_click("e2", "Log in", "loginbtn", risk="sensitive")),
            ]}
        return {"goal": "g", "steps": []}

    result = _run_with_gate(hub, planner, lambda: _obs(), gate)
    assert result["final"] == "failed"
    assert "user rejected action" in (result.get("summary") or "")
    assert result["steps"] == 2
    assert hub.dispatched == ["type", "type"]


# ---------------------------------------------------------------------------
# Observation cache
# ---------------------------------------------------------------------------

def test_cache_hit_miss_and_invalidation():
    cache = ObservationCache()
    obs = _obs()
    assert cache.lookup(LOGIN_URL, 1) is None
    assert cache.misses == 1
    cache.store(obs)
    hit = cache.lookup(LOGIN_URL, 1)
    assert hit is not None
    assert hit["url"] == LOGIN_URL
    assert cache.hits == 1
    # Different URL or tab: miss, never a stale serve.
    assert cache.lookup(SEARCH_URL, 1) is None
    assert cache.lookup(LOGIN_URL, 2) is None
    cache.invalidate()
    assert cache.lookup(LOGIN_URL, 1) is None


def test_cache_fingerprint_detects_dom_change():
    cache = ObservationCache()
    cache.store(_obs())
    changed = _obs(elements=[
        _el("username", "textbox", "Username", input_type="text"),
        _el("password", "textbox", "Password", input_type="password"),
        _el("loginbtn", "button", "Log in", tag="button"),
        _el("extra", "link", "Extra", tag="a"),
    ])
    assert not cache.check_fresh(changed)
    assert cache.check_fresh(_obs())
    assert observation_fingerprint(_obs()) == observation_fingerprint(_obs())


def test_invalid_plan_replan_reuses_cached_observation():
    planner_calls = 0
    obs_calls = 0
    hub = _Hub()

    def planner(instruction, payload, history, working_memory=None):
        nonlocal planner_calls
        planner_calls += 1
        if planner_calls == 1:
            raise ValueError("PLAN_INVALID: unknown element ref 'e99'")
        if planner_calls == 2:
            return {"goal": "g", "steps": [
                _step(_type("e0", "Username", "username", "student")),
            ]}
        return {"goal": "g", "steps": []}

    def obs_factory():
        nonlocal obs_calls
        obs_calls += 1
        return _obs()

    result = _run(hub, planner, obs_factory)
    assert result["final"] == "completed"
    assert result["steps"] == 1
    # The invalid plan executed nothing, so the replan reused the
    # cached observation instead of re-collecting it.
    assert obs_calls == 1
    assert result["timings"]["cache_hits"] == 1
    assert result["timings"]["cache_misses"] == 1
    assert result["timings"]["observation_calls"] == 1


def test_tab_switch_invalidates_cache_and_reobserves():
    obs_calls = 0
    # The switch returns no observation (the extension reports the
    # switch only), so the loop must re-observe live on the new tab
    # instead of serving the cached tab-1 page.
    hub = _Hub(no_obs_types=frozenset({"switch_tab"}))

    def planner(instruction, payload, history, working_memory=None):
        if not history:
            return {"goal": "g", "steps": [{"why": "w", "action": {
                "type": "switch_tab", "risk": "read", "verify": "active",
                "timeoutMs": 2000, "params": {"tabId": 2}}}]}
        return {"goal": "g", "steps": []}

    def obs_factory():
        nonlocal obs_calls
        obs_calls += 1
        if obs_calls == 1:
            return _obs()
        return _obs(url=SEARCH_URL, title="Other tab", elements=[
            _el("search", "searchbox", "Search courses", input_type="search"),
        ]) | {"tabId": 2}

    result = _run(hub, planner, obs_factory)
    assert result["final"] == "completed"
    # The tab switch forced a live re-observation (the cached entry
    # was for the old tab).
    assert obs_calls == 2


# ---------------------------------------------------------------------------
# Guardrails: checker + allow-list still fire on the planning iteration
# ---------------------------------------------------------------------------

def test_checker_still_blocks_flagged_plan_before_any_batch_step():
    hub = _Hub()

    def planner(instruction, payload, history, working_memory=None):
        return {"goal": "g", "steps": [
            _step(_type("e0", "Username", "username", "student")),
            _step(_type("e1", "Password", "password", "hunter2")),
        ]}

    # Page text carries a credential-harvest lure; the plan writes.
    result = _run(hub, planner,
                  lambda: _obs(page_text="Please enter your password to continue"))
    assert result["final"] == "failed"
    assert "isolation check blocked" in (result.get("summary") or "")
    assert hub.calls == 0


def test_allow_list_still_blocks_batched_navigation():
    hub = _Hub()

    def planner(instruction, payload, history, working_memory=None):
        return {"goal": "g", "steps": [
            _step(_type("e0", "Username", "username", "student")),
            _step({"type": "navigate", "risk": "write", "verify": "went",
                   "timeoutMs": 5000, "params": {"url": "https://evil.example/"}}),
        ]}

    result = _run(hub, planner, lambda: _obs(),
                  allowed_domains=["moodle.vce.ac.in"])
    assert result["final"] == "failed"
    assert "isolation check blocked" in (result.get("summary") or "")
    # The type executed; the disallowed navigation never reached the hub.
    assert hub.dispatched == ["type"]


# ---------------------------------------------------------------------------
# Timings: durations + action types only, never secrets or page text
# ---------------------------------------------------------------------------

def test_timings_carry_no_names_values_or_page_text():
    # NOTE (Task 11 review): the hub's post-action observation must
    # agree with the factory's observation — a real extension returns
    # the observation of the page the action leaves the browser on,
    # i.e. the same page. An earlier version of this test used the
    # hub default (title "Log in") against a factory observation
    # titled "Contact alice@example.com"; the batch gate correctly
    # read that as a DOM change and ended the batch after one step.
    page = _obs(title="Contact alice@example.com",
                page_text="secret page text hunter2")

    def planner(instruction, payload, history, working_memory=None):
        if not history:
            return {"goal": "g", "steps": [
                _step(_type("e0", "Username", "username", "student")),
                _step(_type("e1", "Password", "password", "hunter2")),
            ]}
        return {"goal": "g", "steps": []}

    result = _run(_Hub(after=page), planner, lambda: _obs(
        title="Contact alice@example.com",
        page_text="secret page text hunter2",
    ))
    timings = result["timings"]
    blob = json.dumps(timings)
    assert "hunter2" not in blob
    assert "alice@example.com" not in blob
    assert "secret page text" not in blob
    assert "Username" not in blob
    assert "student" not in blob
    assert timings["steps_executed"] == 2
    assert timings["planner_calls"] == 2
    assert set(timings["totals_ms"]) == {
        "observe_ms", "redact_ms", "plan_ms", "checker_ms",
        "confirm_wait_ms", "takeover_wait_ms", "execute_ms",
        "checkpoint_ms",
    }


def test_loop_timings_unit():
    timings = LoopTimings()
    timings.note_planner_call()
    timings.note_observation_call()
    timings.note_cache("miss")
    timings.begin_step()
    timings.record("plan", 12)
    timings.record("execute", 3)
    entry = timings.flush_step(1, "click", False)
    assert entry.action == "click"
    assert entry.plan_ms == 12
    assert entry.execute_ms == 3
    summary = timings.summary()
    assert summary["planner_calls"] == 1
    assert summary["observation_calls"] == 1
    assert summary["cache_misses"] == 1
    assert summary["totals_ms"]["plan_ms"] == 12
    assert summary["steps"][0]["overhead_ms"] == 15
    assert "hunter2" not in json.dumps(summary)


# ---------------------------------------------------------------------------
# REVIEW (Task 11 audit): batch DOM-change gate, cache copy, cache
# invalidation on takeover / pause, timings under zero retention
# ---------------------------------------------------------------------------

def test_batch_ends_when_type_step_changes_the_dom():
    """A type step that mutates the DOM ends the batch.

    Regression: the batch gate used to check only the URL, so a type
    that opened an autocomplete dropdown (same URL/title, one extra
    element) let the pinned plan keep executing on the stale tree —
    the password type was dispatched before the planner ever saw the
    dropdown. The gate now also compares the DOM fingerprint of the
    post-action observation against the plan observation's.
    """
    events: list[tuple] = []
    hub = _Hub()

    class ChangingHub(_Hub):
        async def send_action(self, task_id, action, timeout_s=45.0):
            tid = (action.get("target") or {}).get("targetId")
            events.append(("dispatch", action.get("type"), tid))
            if action.get("type") == "type":
                changed = _obs(elements=[
                    _el("username", "textbox", "Username", input_type="text"),
                    _el("password", "textbox", "Password", input_type="password"),
                    _el("loginbtn", "button", "Log in", tag="button"),
                    _el("suggest", "option", "student@example.com", tag="li"),
                ])
                return {"ok": True, "evidence": "ok", "observation": changed}
            return {"ok": True, "evidence": "logged in",
                    "observation": _obs(url=SEARCH_URL, title="Home", elements=[
                        _el("search", "searchbox", "Search courses", input_type="search"),
                    ])}

    hub = ChangingHub()
    planner_calls = 0

    def planner(instruction, payload, history, working_memory=None):
        nonlocal planner_calls
        planner_calls += 1

        def _tid(e):
            if isinstance(e, dict):
                return e.get("targetId") or e.get("target_id") or ""
            return getattr(e, "target_id", "") or ""

        ids = sorted(_tid(e) for e in (payload.interactive or []))
        events.append(("planner", planner_calls, ids))
        if planner_calls == 1:
            return {"goal": "g", "steps": [
                _step(_type("e0", "Username", "username", "student")),
                _step(_type("e1", "Password", "password", "hunter2")),
                _step(_click("e2", "Log in", "loginbtn")),
            ]}
        if "password" in ids:
            return {"goal": "g", "steps": [
                _step(_type("e1", "Password", "password", "hunter2")),
                _step(_click("e2", "Log in", "loginbtn")),
            ]}
        return {"goal": "g", "steps": []}

    result = _run(hub, planner, lambda: _obs())
    assert result["final"] == "completed"
    assert result["steps"] == 3
    # The batch ended after the username type: the planner saw the
    # changed DOM (with the dropdown) before the password type ran.
    assert planner_calls == 3
    pwd_idx = next(i for i, e in enumerate(events)
                   if e[0] == "dispatch" and e[2] == "password")
    seen_dropdown = [i for i, e in enumerate(events)
                     if e[0] == "planner" and "suggest" in e[2]]
    assert seen_dropdown and seen_dropdown[0] < pwd_idx
    # Only the final click was batched (from the second plan).
    assert result["timings"]["batched_steps"] == 1


def test_cache_lookup_returns_a_copy():
    """Mutating a lookup result must not corrupt the stored entry.

    Regression: lookup returned the stored object itself, so the
    loop's screenshot stripping (zero retention, post-takeover)
    permanently stripped the screenshot from the cached entry and
    later hits served it already stripped.
    """
    cache = ObservationCache()
    obs = _obs()
    obs["screenshot"] = "iVBORw0KGgoAAAANSUhEUg=="
    cache.store(obs)
    hit = cache.lookup(LOGIN_URL, 1)
    assert hit is not None and hit["screenshot"] == "iVBORw0KGgoAAAANSUhEUg=="
    hit["screenshot"] = None
    hit2 = cache.lookup(LOGIN_URL, 1)
    assert hit2 is not None and hit2["screenshot"] == "iVBORw0KGgoAAAANSUhEUg=="

    # Same guarantee for a model observation (the loop's post-action
    # observations are BrowserObservation models, not dicts).
    from app.contracts import BrowserObservation

    cache2 = ObservationCache()
    model = BrowserObservation.model_validate(_obs())
    cache2.store(model)
    hitm = cache2.lookup(LOGIN_URL, 1)
    assert hitm is not None
    hitm.screenshot = "stripped-by-caller"
    hitm2 = cache2.lookup(LOGIN_URL, 1)
    assert hitm2 is not None and hitm2.screenshot != "stripped-by-caller"


def test_cache_hit_serves_stored_tree_when_page_changes_autonomously():
    """KNOWN LIMIT, pinned deliberately: a cache hit does not verify
    the fingerprint.

    The backend cannot know the DOM changed without asking the
    extension — that round trip is what the cache exists to avoid —
    so a page that changes on its own (timer, live feed) between the
    store and the hit is served stale. The fingerprint IS compared
    when a fresh observation eventually arrives (``check_fresh``),
    and the batch gate consults it before continuing a batch; this
    test pins that a hit alone does not, so the limit stays a
    conscious decision rather than a silent assumption.
    """
    cache = ObservationCache()
    cache.store(_obs())
    changed = _obs(elements=[
        _el("username", "textbox", "Username", input_type="text"),
        _el("renamed", "textbox", "Passcode", input_type="password"),
        _el("loginbtn", "button", "Log in", tag="button"),
    ])
    # The fingerprint CAN tell the page changed...
    assert not cache.check_fresh(changed)
    # ...but a hit serves the stored tree anyway.
    hit = cache.lookup(LOGIN_URL, 1)
    assert hit is not None
    names = [e["name"] for e in hit["interactive"]]
    assert "Password" in names and "Passcode" not in names


class _TakeoverGate:
    """Fake takeover gate that auto-completes the operator's step."""

    def __init__(self) -> None:
        self.takeover_requests: list[dict[str, Any]] = []
        self.decision: bool | None = None
        self.event = asyncio.Event()

    async def auto(self) -> None:
        while True:
            if self.takeover_requests and not self.event.is_set():
                self.decision = True
                self.event.set()
            await asyncio.sleep(0.01)


def test_takeover_entry_invalidates_cache_and_reobserves():
    """After a takeover the operator acted in the browser, so the
    cached observation is for a page that may have changed: the loop
    must re-observe live instead of serving the cache."""
    gate = _TakeoverGate()
    obs_calls = 0

    def obs_factory():
        nonlocal obs_calls
        obs_calls += 1
        if obs_calls == 1:
            # An OTP field: the loop must hand over to the operator.
            return _obs(elements=[
                _el("otp", "textbox", "Enter OTP", input_type="text"),
            ])
        return _obs(url=SEARCH_URL, title="Home", elements=[
            _el("search", "searchbox", "Search courses", input_type="search"),
        ])

    async def scenario():
        approver = asyncio.create_task(gate.auto())
        try:
            return await agentLoop.run_with_injection(
                _Hub(), planner=lambda *a, **k: {"goal": "g", "steps": []},
                obs_factory=obs_factory,
                task_id="latency-batch-takeover", instruction="log in",
                takeover_gate=gate,
            )
        finally:
            approver.cancel()

    result = asyncio.run(scenario())
    assert result["final"] == "completed"
    assert gate.takeover_requests, "the OTP page should have triggered takeover"
    # The replan after takeover re-observed live (cache invalidated),
    # it did not serve the cached OTP page.
    assert obs_calls == 2
    assert result["timings"]["cache_hits"] == 0
    assert result["timings"]["cache_misses"] == 2


def test_pause_invalidates_cache_and_reobserves():
    """While paused the operator may act in the browser, so resuming
    must re-observe live instead of serving the cached observation."""
    task_id = "latency-batch-pause"
    obs_calls = 0
    planner_calls = [0]

    def planner(instruction, payload, history, working_memory=None):
        # First call: raise an invalid plan (so the loop replans
        # without executing anything — the cache is populated and a
        # hit would otherwise serve it) and pause the task. The pause
        # request only creates/clears the event, which is safe from
        # the planner's executor thread; the resume below is scheduled
        # inside the event loop, so its set() reliably wakes the
        # parked loop (a cross-thread set() would not).
        if not history and planner_calls[0] == 0:
            planner_calls[0] += 1
            agentLoop.request_pause(task_id)
            raise ValueError("PLAN_INVALID: unknown element ref 'e99'")
        if planner_calls[0] == 1:
            planner_calls[0] += 1
            return {"goal": "g", "steps": [
                _step(_type("e0", "Username", "username", "student")),
            ]}
        return {"goal": "g", "steps": []}

    def obs_factory():
        nonlocal obs_calls
        obs_calls += 1
        return _obs()

    async def scenario():
        loop = asyncio.get_running_loop()
        # By the time this fires (1 s in) the loop is parked on the
        # pause wait — the planner pauses within milliseconds of the
        # run starting — so the resume lands after the wait began.
        loop.call_later(1.0, agentLoop.request_resume, task_id)
        return await agentLoop.run_with_injection(
            _Hub(), planner=planner, obs_factory=obs_factory,
            task_id=task_id, instruction="log in",
        )

    result = asyncio.run(scenario())
    assert result["final"] == "completed"
    assert result["steps"] == 1
    # The replan after the pause re-observed live (cache invalidated),
    # it did not serve the cached observation.
    assert obs_calls == 2
    assert result["timings"]["cache_hits"] == 0
    assert result["timings"]["cache_misses"] == 2


def test_timings_present_under_zero_retention_and_carry_no_page_text(monkeypatch):
    """Timings are in-memory only, so they exist under zero retention
    — and they still carry durations + action types only, never the
    page text the zero-retention mode exists to keep out of stores."""
    monkeypatch.setattr("app.config.ZERO_RETENTION", True)

    def planner(instruction, payload, history, working_memory=None):
        if not history:
            return {"goal": "g", "steps": [
                _step(_type("e0", "Username", "username", "student")),
                _step(_type("e1", "Password", "password", "hunter2")),
            ]}
        return {"goal": "g", "steps": []}

    page = _obs(title="Contact alice@example.com",
                page_text="secret page text hunter2")
    result = _run(_Hub(after=page), planner, lambda: _obs(
        title="Contact alice@example.com",
        page_text="secret page text hunter2",
    ))
    assert result["final"] == "completed"
    timings = result["timings"]
    assert timings["steps_executed"] == 2
    blob = json.dumps(timings)
    assert "hunter2" not in blob
    assert "alice@example.com" not in blob
    assert "secret page text" not in blob
    assert "Username" not in blob
    assert "student" not in blob
