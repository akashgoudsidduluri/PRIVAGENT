"""Bounded agent loop: budgets, cancellation, repeated-failure recovery."""
import asyncio

import pytest

from app import agentLoop


class FakeHub:
    """Scripted extension+planner stand-in. Feeds queued results per action type."""
    def __init__(self, script: dict[str, list[dict]]):
        self.script = script
        self.executed: list[str] = []
        self.alerted: list[str] = []
        self.ext_online = True

    async def send_action(self, task_id: str, action: dict, timeout_s: float = 45.0):
        await asyncio.sleep(0.02)  # let cancellation land mid-loop in tests
        t = action["type"]
        self.executed.append(t)
        q = self.script.setdefault(t, [{}])
        if not q:
            return {"ok": True}
        head = q[0]
        if "fail_n" in head:
            n = head.setdefault("_n", head["fail_n"])
            if n > 0:
                head["_n"] = n - 1
                return {"ok": False, "error": {"code": head.get("code", "TIMEOUT"), "message": head.get("msg", "x"), "recoverable": True}}
            q.pop(0)  # exhausted — subsequent calls fall through to next item
            return {"ok": True}
        if len(q) > 1:
            return q.pop(0)
        return q[0]


def test_budget_abort_at_max_steps(monkeypatch):
    monkeypatch.setattr(agentLoop, "MAX_STEPS", 3)
    hub = FakeHub({"click": [{"ok": True}]})
    state = asyncio.run(agentLoop.run_with_injection(
        hub, planner=lambda i, p, h: {"goal": "loop forever",
                                      "steps": [{"why": "again", "action": {"type": "click", "risk": "write", "verify": "v", "timeoutMs": 100, "target": {"targetId": "b1"}}}]},
        obs_factory=lambda: {"tabId": 1, "url": "http://x/", "title": "t", "interactive": [], "scroll": {}, "dpr": 1},
    ))
    assert state["final"] == "budget_exceeded"
    assert len(hub.executed) <= 4


def test_cancellation_between_actions():
    hub = FakeHub({"click": [{"ok": True} for _ in range(10)]})
    state_holder = {}

    class Planner:
        calls = 0
        def __call__(self, i, p, h):
            return {"goal": "g", "steps": [{"why": "w", "action": {"type": "click", "risk": "write", "verify": "v", "timeoutMs": 100, "target": {"targetId": "b1"}}}]}

    async def scenario():
        async def cancel_soon():
            await asyncio.sleep(0.05)
            agentLoop.request_cancel("test-task")
        hub2 = FakeHub({"click": [{"ok": True} for _ in range(10)]})
        state_holder["hub"] = hub2
        state = await asyncio.gather(
            agentLoop.run_with_injection(
                hub2, planner=Planner(),
                obs_factory=lambda: {"tabId": 1, "url": "u", "title": "t", "interactive": [], "scroll": {}, "dpr": 1},
            ),
            cancel_soon(),
        )
        return state[0]

    state = asyncio.run(scenario())
    assert state["final"] == "cancelled"


def test_repeated_identical_error_triggers_replan_penalized():
    hub = FakeHub({"click": [{"ok": False, "error": {"code": "STALE_TARGET", "message": "gone", "recoverable": True}, "fail_n": 9},
                             {"ok": True}]})
    plans: list[dict] = []
    calls = {"n": 0}

    class Planner:
        def __call__(self, i, p, h):
            plans.append(h)
            calls["n"] += 1
            if calls["n"] >= 3:
                return {"goal": "g", "steps": []}
            return {"goal": "g", "steps": [{"why": "w", "action": {"type": "click", "risk": "write", "verify": "v", "timeoutMs": 100, "target": {"targetId": "b1"}}}]}

    state = asyncio.run(agentLoop.run_with_injection(
        hub, planner=Planner(),
        obs_factory=lambda: {"tabId": 1, "url": "u", "title": "t", "interactive": [], "scroll": {}, "dpr": 1},
    ))
    assert state["final"] == "completed"
    # after the first identical STALE_TARGET failure, the next planner call must see a penalty entry
    assert any(isinstance(h, dict) and "penalty" in h for call_hist in plans for h in call_hist)


def test_summary_contains_evidence():
    hub = FakeHub({"navigate": [{"ok": True, "evidence": "https://www.google.com/search?q=jwst"}]})
    calls = {"n": 0}
    class Planner:
        def __call__(self, i, p, h):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"goal": "search", "steps": [{"why": "go", "action": {"type": "navigate", "risk": "write", "verify": "url", "timeoutMs": 1000, "params": {"url": "https://www.google.com/search?q=jwst"}}}]}
            return {"goal": "search", "steps": []}

    state = asyncio.run(agentLoop.run_with_injection(
        hub, planner=Planner(),
        obs_factory=lambda: {"tabId": 1, "url": "https://www.google.com/search?q=jwst", "title": "jwst - Google Search", "interactive": [], "scroll": {}, "dpr": 1},
    ))
    assert state["final"] == "completed"
    assert "google.com" in (state["summary"] or "")


def _youtube_obs():
    return {"tabId": 1, "url": "https://www.youtube.com/", "title": "YouTube",
            "interactive": [], "scroll": {}, "dpr": 1}


class _RawProvider:
    """Provider double that always returns one scripted raw plan."""

    def __init__(self, raw):
        self.raw = raw

    def plan_call(self, instruction, payload, history, working_memory=None):
        return self.raw


def _validating_planner(provider):
    """Production-shaped planner: validation happens inside plan_from_observation."""
    from app.planner import plan_from_observation
    from app.privacy import PrivacyFilteredPayload

    def planner(instruction, payload, history, working_memory=None):
        pf = PrivacyFilteredPayload(**payload) if isinstance(payload, dict) else payload
        return plan_from_observation(
            instruction=instruction, payload=pf, provider=provider,
            history=history, working_memory=working_memory,
        ).model_dump(by_alias=True, exclude_none=True)

    return planner


_UNDERSPECIFIED_CLICK = {
    "goal": "play",
    "steps": [{"why": "click the first video", "action": {
        "type": "click", "risk": "write", "verify": "video opened", "timeoutMs": 1000,
        "target": {"role": "link"},
    }}],
}


def test_invalid_plan_replans_instead_of_killing_the_mission():
    """One malformed plan produced "loop error: PLAN_INVALID" after 1 step."""
    calls = {"n": 0}
    seen_histories: list[list[dict]] = []

    class Provider:
        def plan_call(self, instruction, payload, history, working_memory=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return _UNDERSPECIFIED_CLICK
            return {"goal": "play", "steps": [], "answer": "Opened the first video"}

    def planner(instruction, payload, history, working_memory=None):
        seen_histories.append(history)
        from app.planner import plan_from_observation
        from app.privacy import PrivacyFilteredPayload
        pf = PrivacyFilteredPayload(**payload) if isinstance(payload, dict) else payload
        return plan_from_observation(
            instruction=instruction, payload=pf, provider=Provider(),
            history=history, working_memory=working_memory,
        ).model_dump(by_alias=True, exclude_none=True)

    state = asyncio.run(agentLoop.run_with_injection(
        FakeHub({}), planner=planner, obs_factory=_youtube_obs,
    ))

    assert state["final"] == "completed"
    assert calls["n"] == 2
    assert any("invalid plan" in str(h.get("penalty", "")) for h in seen_histories[1])


def test_persistently_invalid_plans_fail_honestly():
    calls = {"n": 0}

    class Provider:
        def plan_call(self, instruction, payload, history, working_memory=None):
            calls["n"] += 1
            return _UNDERSPECIFIED_CLICK

    state = asyncio.run(agentLoop.run_with_injection(
        FakeHub({}), planner=_validating_planner(Provider()), obs_factory=_youtube_obs,
    ))

    assert state["final"] == "failed"
    assert calls["n"] == agentLoop.MAX_INVALID_PLANS
    summary = (state["summary"] or "").lower()
    assert "incomplete" in summary
    assert "valid action" in summary


def test_provider_outage_fails_fast_without_consuming_a_step():
    """A daily-quota 429 must be reported, not retried into a 40s stall."""
    from app.providers.errors import ProviderUnavailable

    def planner(instruction, payload, history, working_memory=None):
        raise ProviderUnavailable("Groq quota exhausted; retry in about 23m.")

    state = asyncio.run(agentLoop.run_with_injection(
        FakeHub({}), planner=planner, obs_factory=_youtube_obs,
    ))

    assert state["final"] == "failed"
    assert state["steps"] == 0
    summary = state["summary"] or ""
    assert summary.startswith("Planner unavailable:")
    assert "23m" in summary


def test_transient_dns_blip_is_retried_with_backoff(monkeypatch):
    """A transient DNS blip must not kill the mission on the first
    attempt: the old loop waited 1.5s and gave up after 2 attempts,
    so a blip lasting a few seconds (live price hunt, 02:07 IST:
    "[Errno 11002] getaddrinfo failed") ended the mission even
    though the gateway was reachable a minute later."""
    from app.providers.errors import ProviderUnavailable

    real_sleep = asyncio.sleep
    sleeps: list[float] = []

    async def fast_sleep(delay):
        sleeps.append(round(float(delay), 1))
        if delay < 0.5:
            await real_sleep(delay)

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    calls = {"n": 0}

    def planner(instruction, payload, history, working_memory=None):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise ProviderUnavailable("Planner call failed: [Errno 11002] getaddrinfo failed")
        return {"goal": "go", "steps": [], "answer": "done"}

    state = asyncio.run(agentLoop.run_with_injection(
        FakeHub({}), planner=planner, obs_factory=_youtube_obs,
    ))

    assert state["final"] == "completed"
    assert calls["n"] == 3
    # Backoff actually waits (2s, then 5s), not the old 1.5s floor.
    assert sleeps[:2] == [2.0, 5.0]


def test_persistent_transient_planner_failure_names_the_hop(monkeypatch):
    """When the planner gateway stays unreachable, the failure must
    name the failing hop (the planner call) and must not blame the
    requested sites, which were never tested. The live run's agent
    claimed 'browser automation can't resolve amazon.in / flipkart.com
    / croma.com' when the first failure was the planner's DNS lookup."""
    from app.providers.errors import ProviderUnavailable

    real_sleep = asyncio.sleep
    sleeps: list[float] = []

    async def fast_sleep(delay):
        sleeps.append(round(float(delay), 1))
        if delay < 0.5:
            await real_sleep(delay)

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    def planner(instruction, payload, history, working_memory=None):
        raise ProviderUnavailable("Planner call failed: [Errno 11002] getaddrinfo failed")

    state = asyncio.run(agentLoop.run_with_injection(
        FakeHub({}), planner=planner, obs_factory=_youtube_obs,
    ))

    assert state["final"] == "failed"
    summary = state["summary"] or ""
    assert summary.startswith("Planner unavailable:")
    assert "planner call itself failed" in summary.lower()
    assert "were not tested" in summary.lower()
    # Four transient attempts (initial + 3 retries) with 2s/5s/10s backoff.
    assert sleeps == [2.0, 5.0, 10.0]


def test_non_transient_planner_failure_still_fails_fast(monkeypatch):
    """The extra attempts are only for transient network blips: an
    auth failure must still fail after the old 2 attempts, with the
    old 1.5s wait — no backoff inflation for errors a retry cannot fix."""
    from app.providers.errors import ProviderUnavailable

    real_sleep = asyncio.sleep
    sleeps: list[float] = []

    async def fast_sleep(delay):
        sleeps.append(round(float(delay), 1))
        if delay < 0.5:
            await real_sleep(delay)

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    calls = {"n": 0}

    def planner(instruction, payload, history, working_memory=None):
        calls["n"] += 1
        raise ProviderUnavailable("Planner call failed: HTTP 401 invalid API key")

    state = asyncio.run(agentLoop.run_with_injection(
        FakeHub({}), planner=planner, obs_factory=_youtube_obs,
    ))

    assert state["final"] == "failed"
    assert calls["n"] == 2
    assert sleeps == [1.5]
    summary = state["summary"] or ""
    assert "planner call itself failed" not in summary.lower()


def test_loop_reports_phase_metrics():
    seen: list[dict] = []
    hub = FakeHub({"navigate": [{"ok": True, "evidence": "https://www.google.com/"}]})
    calls = {"n": 0}

    class Planner:
        def __call__(self, i, p, h):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"goal": "go", "steps": [{"why": "go", "action": {
                    "type": "navigate", "risk": "write", "verify": "url", "timeoutMs": 1000,
                    "params": {"url": "https://www.google.com/"}}}]}
            return {"goal": "go", "steps": [], "answer": "done"}

    state = asyncio.run(agentLoop.run_with_injection(
        hub, planner=Planner(), obs_factory=_youtube_obs, on_metrics=seen.append,
    ))

    assert state["final"] == "completed"
    phases = [m["phase"] for m in seen]
    assert "plan" in phases
    assert "act" in phases
    assert all(isinstance(m["ms"], int) and m["ms"] >= 0 for m in seen)


def test_state_aware_loop_detection_identical_state():
    hub = FakeHub({"click": [{"ok": True}]})
    plans: list[dict] = []
    calls = {"n": 0}

    class Planner:
        def __call__(self, i, p, h):
            plans.append(h)
            calls["n"] += 1
            # Propose the exact same click action repeatedly
            return {
                "goal": "click item",
                "steps": [{
                    "why": "click product link",
                    "action": {
                        "type": "click",
                        "risk": "write",
                        "verify": "v",
                        "timeoutMs": 1000,
                        "target": {"targetId": "nav-el-1", "name": "Acer Laptop"},
                    },
                }],
            }

    # obs_factory always returns the exact same unchanged observation
    state = asyncio.run(agentLoop.run_with_injection(
        hub, planner=Planner(),
        obs_factory=lambda: {
            "tabId": 1, "url": "https://www.amazon.com/s?k=laptop", "title": "Search",
            "interactive": [{"targetId": "nav-el-1", "role": "link", "name": "Acer Laptop", "tag": "a", "visible": True, "enabled": True}],
            "scroll": {}, "dpr": 1,
        },
    ))
    assert state["final"] == "budget_exceeded"
    assert "Loop detected" in state["summary"]
    assert "identical page state" in state["summary"]
    # Verify that the state-aware loop warning was injected into planner penalties
    has_warning = any(
        "State-aware loop warning" in str(h.get("penalty", ""))
        for call_hist in plans
        for h in call_hist
        if isinstance(h, dict)
    )
    assert has_warning


def test_repeated_failure_loop_detection_stops_stale_clicks():
    """A planner that keeps re-emitting an action that FAILS with the same
    error must be stopped — not allowed to burn the whole step budget one
    failure at a time.

    Live run 2026-11-17 (price hunt on Flipkart): 38 identical
    popup-close clicks, each failing STALE_TARGET as the page
    re-rendered, because loop detection only ran when the last step
    SUCCEEDED. The task ran to the 50-step cap with 1/3 sites done.

    Production shape matters here: the extension embeds the changing
    target in every STALE_TARGET message ('no matching element for
    {"elementRef":"e12","targetId":"...","name":"Close"}'), so the raw
    error strings are never equal twice — and the planner re-targets
    with a fresh id on every attempt. Matching must therefore be by
    normalized cause and normalized name, not raw strings. A second
    live run (2026-11-18, after the first fix was pulled + the backend
    restarted) looped 32 more times for exactly this reason.
    """
    # 60 distinct failures: the list must never run out mid-run, or the
    # hub would start repeating its last entry and the old exact-match
    # check would stop the loop by accident. In production every
    # failure carries a fresh ref, so no two messages are ever equal.
    failures = [
        {"ok": False, "error": {
            "code": "STALE_TARGET",
            "message": 'no matching element for {"elementRef":"e%d","targetId":"popup-close-%d","name":"Close"}' % (12 + 3 * i, i + 1),
            "recoverable": True,
        }}
        for i in range(60)
    ]
    hub = FakeHub({"click": failures})
    plans: list[dict] = []
    calls = {"n": 0}

    class Planner:
        def __call__(self, i, p, h):
            plans.append(h)
            calls["n"] += 1
            n = calls["n"]
            return {
                "goal": "dismiss popup",
                "steps": [{
                    "why": "close popup",
                    "action": {
                        "type": "click",
                        "risk": "write",
                        "verify": "v",
                        "timeoutMs": 100,
                        "target": {"targetId": f"popup-close-{n}", "name": "Close"},
                    },
                }],
            }

    state = asyncio.run(agentLoop.run_with_injection(
        hub, planner=Planner(),
        # Empty tree: nothing to re-ground against, so the stale-ref
        # recovery finds no fresh target and each step fails cleanly.
        obs_factory=lambda: {
            "tabId": 1, "url": "https://www.flipkart.com/search?q=logitech", "title": "Search",
            "interactive": [],
            "scroll": {}, "dpr": 1,
        },
    ))
    assert state["final"] == "budget_exceeded"
    assert "Loop detected" in state["summary"]
    assert "failed" in state["summary"]
    # Stopped after a few failures, not at the 50-step cap.
    assert state["steps"] <= 5
    # The repeated-failure warning reached the planner before the stop.
    has_warning = any(
        "Repeated failure warning" in str(h.get("penalty", ""))
        for call_hist in plans
        for h in call_hist
        if isinstance(h, dict)
    )
    assert has_warning


def test_repeated_failure_loop_detection_stops_icon_only_stale_clicks():
    """The repeated-failure stop must fire when the target has no
    usable identity. A popup close button is often an icon-only
    control (name "✕" normalizes to ""), and the extension
    re-renders it with a fresh targetId on every attempt — so
    `_actions_match` (name / equal targetId / params) returns False
    for every failure and the stop never fires.

    Live run 2026-11-18 (merged PR #8): the price hunt ran to the
    50-step cap with steps 9–49 all "checking for a popup on
    Flipkart", each click failing STALE_TARGET. The named-target
    regression test above passes for this shape, but it uses
    name "Close" — this test uses the icon-only shape and must
    also stop within a few failures.
    """
    failures = [
        {"ok": False, "error": {
            "code": "STALE_TARGET",
            "message": 'no matching element for {"elementRef":"e%d","targetId":"popup-close-%d","name":"\u2715"}' % (12 + 3 * i, i + 1),
            "recoverable": True,
        }}
        for i in range(60)
    ]
    hub = FakeHub({"click": failures})
    calls = {"n": 0}

    class Planner:
        def __call__(self, i, p, h):
            calls["n"] += 1
            n = calls["n"]
            return {
                "goal": "dismiss popup",
                "steps": [{
                    "why": "close popup",
                    "action": {
                        "type": "click",
                        "risk": "write",
                        "verify": "v",
                        "timeoutMs": 100,
                        "target": {"targetId": f"popup-close-{n}", "name": "\u2715"},
                    },
                }],
            }

    state = asyncio.run(agentLoop.run_with_injection(
        hub, planner=Planner(),
        obs_factory=lambda: {
            "tabId": 1, "url": "https://www.flipkart.com/search?q=logitech", "title": "Search",
            "interactive": [],
            "scroll": {}, "dpr": 1,
        },
    ))
    assert state["final"] == "budget_exceeded"
    assert "Loop detected" in state["summary"]
    assert "failed" in state["summary"]
    assert state["steps"] <= 5


def test_tab_opened_by_click_registers_and_can_be_selected():
    """A tab opened by a click (not a `new_tab` action) must register
    in the task's tab registry, so a later select_tab to it is allowed.

    Live Flipkart run 2026-11-18: the agent opened the product page
    in a new tab (step 5), scrolled (step 6), then the switch was
    rejected — "unknown tab: tab <id> is not in this task's tab
    registry" — because only `new_tab` actions registered tabs. The
    tab the loop is observing is ground truth for a tab the task owns,
    so it registers on every iteration now.
    """
    hub = FakeHub({"click": [{"ok": True}], "select_tab": [{"ok": True}]})
    calls = {"n": 0, "obs": 0}

    def obs_factory():
        calls["obs"] += 1
        if calls["obs"] == 1:
            return {"tabId": 1, "url": "https://www.flipkart.com/search?q=logitech", "title": "Search",
                    "interactive": [{"targetId": "prod-link", "role": "link", "name": "Logitech MX Master 3S", "tag": "a", "visible": True, "enabled": True}],
                    "scroll": {}, "dpr": 1}
        return {"tabId": 2, "url": "https://www.flipkart.com/product/p/itm271ade01ff274", "title": "Logitech MX Master 3S",
                "interactive": [], "scroll": {}, "dpr": 1}

    class Planner:
        def __call__(self, i, p, h):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"goal": "open product", "steps": [{"why": "open product", "action": {
                    "type": "click", "risk": "write", "verify": "v", "timeoutMs": 100,
                    "target": {"targetId": "prod-link", "name": "Logitech MX Master 3S"}}}]}
            if calls["n"] == 2:
                return {"goal": "open product", "steps": [{"why": "switch to product tab", "action": {
                    "type": "select_tab", "risk": "write", "verify": "v", "timeoutMs": 100,
                    "params": {"tabId": 2}}}]}
            return {"goal": "open product", "steps": []}

    state = asyncio.run(agentLoop.run_with_injection(
        hub, planner=Planner(), obs_factory=obs_factory,
        initial_memory={"primary_tab_id": 1},
    ))
    assert state["final"] == "completed"
    assert "unknown tab" not in (state["summary"] or "")


def test_closed_tab_pruned_from_registry_and_select_rejected():
    """A tab the operator closed must leave the registry, so a later
    select_tab to it is rejected as unknown — not let through the
    gate to fail confusingly at the extension.

    The extension sends the task's open tabs in every observation
    (``tabs``). Tab 2 is registered when the loop observes it; the
    next observation (back on tab 1) lists only tab 1, so tab 2 is
    pruned. A select_tab to tab 2 must then fail the gate.
    """
    hub = FakeHub({"click": [{"ok": True}], "select_tab": [{"ok": True}]})
    calls = {"n": 0, "obs": 0}

    def obs_factory():
        calls["obs"] += 1
        if calls["obs"] == 1:
            return {"tabId": 1, "url": "https://www.flipkart.com/search?q=logitech", "title": "Search",
                    "tabs": [{"tabId": 1, "url": "https://www.flipkart.com/search?q=logitech", "title": "Search", "active": True}],
                    "interactive": [{"targetId": "prod-link", "role": "link", "name": "Logitech MX Master 3S", "tag": "a", "visible": True, "enabled": True}],
                    "scroll": {}, "dpr": 1}
        if calls["obs"] == 2:
            return {"tabId": 2, "url": "https://www.flipkart.com/product/p/itm271ade01ff274", "title": "Logitech MX Master 3S",
                    "tabs": [{"tabId": 1, "url": "https://www.flipkart.com/search?q=logitech", "title": "Search", "active": False},
                             {"tabId": 2, "url": "https://www.flipkart.com/product/p/itm271ade01ff274", "title": "Logitech MX Master 3S", "active": True}],
                    "interactive": [], "scroll": {}, "dpr": 1}
        # Tab 2 was closed by the operator: only tab 1 remains open.
        return {"tabId": 1, "url": "https://www.flipkart.com/search?q=logitech", "title": "Search",
                "tabs": [{"tabId": 1, "url": "https://www.flipkart.com/search?q=logitech", "title": "Search", "active": True}],
                "interactive": [], "scroll": {}, "dpr": 1}

    class Planner:
        def __call__(self, i, p, h):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"goal": "open product", "steps": [{"why": "open product", "action": {
                    "type": "click", "risk": "write", "verify": "v", "timeoutMs": 100,
                    "target": {"targetId": "prod-link", "name": "Logitech MX Master 3S"}}}]}
            if calls["n"] == 2:
                return {"goal": "switch back", "steps": [{"why": "switch back", "action": {
                    "type": "select_tab", "risk": "write", "verify": "v", "timeoutMs": 100,
                    "params": {"tabId": 1}}}]}
            # Tab 2 is closed now; the planner (working from stale
            # memory) tries to switch to it. The gate must reject it.
            return {"goal": "switch to closed tab", "steps": [{"why": "switch to closed tab", "action": {
                "type": "select_tab", "risk": "write", "verify": "v", "timeoutMs": 100,
                "params": {"tabId": 2}}}]}

    state = asyncio.run(agentLoop.run_with_injection(
        hub, planner=Planner(), obs_factory=obs_factory,
        initial_memory={"primary_tab_id": 1},
    ))
    assert state["final"] == "failed"
    assert "unknown tab" in (state["summary"] or "")


def test_primary_tab_never_pruned_by_observation_tabs():
    """A partial observation tab list that omits the primary tab must
    not prune it: the primary is the task's anchor, and its closure
    is a separate failure, not a prune. The planner must still be
    able to select back to the primary."""
    hub = FakeHub({"click": [{"ok": True}], "select_tab": [{"ok": True}]})
    calls = {"n": 0, "obs": 0}

    def obs_factory():
        calls["obs"] += 1
        if calls["obs"] == 1:
            return {"tabId": 1, "url": "https://example.com/", "title": "Home",
                    "tabs": [{"tabId": 1, "url": "https://example.com/", "title": "Home", "active": True}],
                    "interactive": [{"targetId": "lnk", "role": "link", "name": "Docs", "tag": "a", "visible": True, "enabled": True}],
                    "scroll": {}, "dpr": 1}
        # Stale/partial list: only the observed tab 2, primary omitted.
        return {"tabId": 2, "url": "https://example.com/docs", "title": "Docs",
                "tabs": [{"tabId": 2, "url": "https://example.com/docs", "title": "Docs", "active": True}],
                "interactive": [], "scroll": {}, "dpr": 1}

    class Planner:
        def __call__(self, i, p, h):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"goal": "open docs", "steps": [{"why": "open docs", "action": {
                    "type": "click", "risk": "write", "verify": "v", "timeoutMs": 100,
                    "target": {"targetId": "lnk", "name": "Docs"}}}]}
            if calls["n"] == 2:
                return {"goal": "back home", "steps": [{"why": "back home", "action": {
                    "type": "select_tab", "risk": "write", "verify": "v", "timeoutMs": 100,
                    "params": {"tabId": 1}}}]}
            return {"goal": "back home", "steps": []}

    state = asyncio.run(agentLoop.run_with_injection(
        hub, planner=Planner(), obs_factory=obs_factory,
        initial_memory={"primary_tab_id": 1},
    ))
    assert state["final"] == "completed"
    assert "unknown tab" not in (state["summary"] or "")


# ---------------------------------------------------------------------------
# Fix 5 (live failure 2026-11-18 02:07 IST): after the Fit3 suggestion
# the agent gave a verdict but never asked "do you want me to alert
# you?". A completed comparison with verified listings must offer the
# alert; a comparison with nothing verified must not.
# ---------------------------------------------------------------------------

def _fit3_tracker(completed: bool = True):
    flipkart = {
        "name": "Flipkart",
        "status": "completed" if completed else "unavailable",
        "domains": ["flipkart.com"],
        "default_url": "https://www.flipkart.com/samsung-galaxy-fit3/p/itm123",
        "reason": "No search input or listing found on Flipkart",
    }
    if completed:
        flipkart["data"] = {
            "price": "₹3,899",
            "url": "https://www.flipkart.com/samsung-galaxy-fit3/p/itm123",
            "variant": "Standard",
            "title": "Samsung Galaxy Fit3",
            "image": "https://rukminim2.flixcart.com/image/312/312/xif0q/smartwatch/f/i/t/fit3.jpg",
        }
    return {
        "required_sites": {
            "flipkart": flipkart,
            "amazon": {
                "name": "Amazon.in",
                "status": "unavailable",
                "domains": ["amazon.in"],
                "default_url": "https://www.amazon.in/s?k=galaxy+fit3",
                "reason": "No search input or listing found on Amazon.in",
            },
        }
    }


# Keep the literal here (not an import of the new constant) so the
# pre-fix run fails on the missing offer itself, not on an ImportError.
_OFFER_MARKER = "Want me to keep an eye on it?"


def _answer_only_run(tracker):
    hub = FakeHub({})
    state = asyncio.run(agentLoop.run_with_injection(
        hub,
        planner=lambda i, p, h: {
            "goal": "compare",
            "steps": [],
            "answer": "The Samsung Galaxy Fit3 is the best budget band.",
        },
        obs_factory=lambda: {
            "tabId": 1,
            "url": "https://www.flipkart.com/samsung-galaxy-fit3/p/itm123",
            "title": "t", "interactive": [], "scroll": {}, "dpr": 1,
        },
        initial_memory={"multi_site_research": tracker},
    ))
    return state, _OFFER_MARKER


def test_completed_comparison_offers_price_alert():
    state, marker = _answer_only_run(_fit3_tracker(completed=True))
    assert state["final"] == "completed"
    summary = state["summary"] or ""
    # Verified listing renders with image + link (ogImage table).
    assert "Price & Availability Comparison" in summary
    assert "rukminim2.flixcart.com" in summary
    assert "flipkart.com/samsung-galaxy-fit3" in summary
    # And the alert offer follows, naming the real channel.
    assert marker in summary
    assert "right here in this conversation" in summary
    assert "9:00 AM IST" in summary


def test_unverified_comparison_offers_no_alert():
    state, marker = _answer_only_run(_fit3_tracker(completed=False))
    summary = state["summary"] or ""
    assert marker not in summary
    assert "No verified listings were found" in summary


@pytest.mark.asyncio
async def test_screenshot_less_step_clears_task_screenshot(monkeypatch, tmp_path):
    """A step with no screenshot must clear the task's screenshot: the old
    `if screenshot:` guard in run_task's on_step_progress made
    record.screenshot sticky, so the Visual Perception frame kept
    showing the previous step's image as if it were the current step's."""
    from app import agentLoop
    from app.agentLoop import real_hub, run_task
    from app.hub import TaskRecord
    from app import db

    temp_db = str(tmp_path / "test_shot.db")
    db.init_db(db_path=temp_db)
    old_init_db = db.init_db
    original_get_conn = db.get_connection
    db.init_db = lambda *args, **kwargs: None
    db.get_connection = lambda p=None: original_get_conn(temp_db)
    task_id = "task_shot_clear"
    try:
        chat = db.create_chat(title="shot", db_path=temp_db)
        record = TaskRecord(
            task_id=task_id, instruction="open https://example.com/ and click the Go button, then click the Next button",
            state="created", chat_id=chat["id"],
        )
        real_hub.tasks[task_id] = record
        real_hub.extension_sockets.append(object())

        seen: list = []
        orig_broadcast = real_hub.broadcast_progress

        def capturing_broadcast(task):
            # Read at call time: the spawned coroutine runs later, after
            # the record may have moved on to the next step.
            seen.append(task.screenshot)
            return orig_broadcast(task)

        monkeypatch.setattr(real_hub, "broadcast_progress", capturing_broadcast)

        obs_calls = {"n": 0}

        def _obs(shot):
            obs = {"tabId": 1, "url": "https://example.com/", "title": "Example",
                   "interactive": [
                       {"targetId": "b1", "role": "button", "name": "Go", "tag": "button", "visible": True, "enabled": True, "bounds": {"x": 10, "y": 10, "width": 80, "height": 30}},
                       {"targetId": "b2", "role": "button", "name": "Next", "tag": "button", "visible": True, "enabled": True, "bounds": {"x": 100, "y": 10, "width": 80, "height": 30}},
                   ],
                   "scroll": {}, "dpr": 1}
            if shot:
                obs["screenshot"] = shot
            return obs

        async def fake_send_action(tid, action, timeout_s=45.0):
            t = action["type"]
            if t == "init_session":
                return {"ok": True, "primaryTabId": 1}
            if t == "observe":
                obs_calls["n"] += 1
                return {"ok": True, "observation": _obs("data:image/png;base64,AAA" if obs_calls["n"] == 1 else None)}
            if t == "click":
                return {"ok": True, "evidence": "clicked", "observation": _obs(None)}
            return {"ok": True}

        monkeypatch.setattr(real_hub, "send_action", fake_send_action)

        calls = {"n": 0}

        def planner(instr, payload, history, working_memory=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"goal": "click", "steps": [{"why": "go", "action": {"type": "click", "risk": "write", "verify": "clicked", "timeoutMs": 1000, "target": {"targetId": "b1"}}}]}
            if calls["n"] == 2:
                return {"goal": "click", "steps": [{"why": "next", "action": {"type": "click", "risk": "write", "verify": "clicked", "timeoutMs": 1000, "target": {"targetId": "b2"}}}]}
            return {"goal": "done", "steps": [], "answer": "clicked both buttons"}

        monkeypatch.setattr(agentLoop, "_real_planner", lambda record: planner)

        await run_task(task_id)
        await asyncio.sleep(0.05)  # let spawned broadcasts finish
        assert record.state == "completed"
        assert "data:image/png;base64,AAA" in seen, f"step 1 screenshot never broadcast: {seen}"
        assert seen[-1] is None, f"last broadcast still carries the stale screenshot: {seen}"
    finally:
        db.get_connection = original_get_conn
        db.init_db = old_init_db
        real_hub.tasks.pop(task_id, None)
        real_hub.extension_sockets.clear()


def test_persistent_navigation_oscillation_stops_after_warnings():
    """A planner that keeps bouncing between two pages (Wikipedia <->
    BBC) must be stopped, not left to run to the step cap or the
    planner timeout. The old code only appended a soft penalty, so a
    live research run ping-ponged for 10 steps (34 in an earlier run)
    burning ~118s of overhead on pure navigations. After two ignored
    warnings (three detections) the loop stops honestly."""
    hub = FakeHub({"navigate": [{"ok": True}]})
    url_a = "https://en.wikipedia.org/wiki/Quantum_mind"
    url_b = "https://www.bbc.co.uk/search?q=quantum+biology"
    calls = {"n": 0}

    def obs_factory():
        # The observed URL alternates A, B, A, B ... (the agent keeps
        # navigating back and forth between the two pages).
        calls["n"] += 1
        url = url_a if calls["n"] % 2 == 1 else url_b
        return {
            "tabId": 1, "url": url, "title": "t",
            "interactive": [], "scroll": {}, "dpr": 1,
        }

    class Planner:
        def __call__(self, i, p, h):
            # Alternate the navigation target A, B, A, B ...
            target = url_a if calls["n"] % 2 == 0 else url_b
            return {
                "goal": "research",
                "steps": [{
                    "why": "check page",
                    "action": {
                        "type": "navigate",
                        "risk": "read",
                        "verify": "v",
                        "timeoutMs": 1000,
                        "url": target,
                    },
                }],
            }

    state = asyncio.run(agentLoop.run_with_injection(
        hub, planner=Planner(), obs_factory=obs_factory,
    ))
    assert state["final"] == "budget_exceeded", state
    assert "Loop detected" in state["summary"], state
    assert "bouncing" in state["summary"], state
    # Stopped early (well under the 50-step cap / the 10-step live run).
    assert state["steps"] <= 8, state


def test_oscillation_counter_resets_for_new_page_pair():
    """Three separate single round-trips between DIFFERENT page pairs
    are legitimate cross-checks, not a loop. Before the fix the
    oscillation counter was global and never reset, so three
    one-off A-B, C-D, E-F round-trips accumulated to a stop — the
    loop was killed for comparing sources. Only repeated cycles of
    the SAME pair count."""
    hub = FakeHub({"navigate": [{"ok": True}]})
    # FOUR single round-trips between different pairs: with a global
    # counter the 3rd pair's round-trip is the 3rd detection and the
    # loop stops at iteration 12, before the 4th pair. With per-pair
    # counting each pair resets the count and the loop completes.
    seq = [
        "https://en.wikipedia.org/wiki/Quantum_mind",
        "https://www.bbc.co.uk/search?q=quantum+biology",
        "https://en.wikipedia.org/wiki/Quantum_mind",
        "https://www.bbc.co.uk/search?q=quantum+biology",
        "https://en.wikipedia.org/wiki/Quantum_biology",
        "https://www.nature.com/search?q=quantum+biology",
        "https://en.wikipedia.org/wiki/Quantum_biology",
        "https://www.nature.com/search?q=quantum+biology",
        "https://arxiv.org/abs/quantum",
        "https://en.wikipedia.org/wiki/Quantum_computing",
        "https://arxiv.org/abs/quantum",
        "https://en.wikipedia.org/wiki/Quantum_computing",
        "https://www.bbc.co.uk/news/quantum",
        "https://en.wikipedia.org/wiki/Quantum_field_theory",
        "https://www.bbc.co.uk/news/quantum",
        "https://en.wikipedia.org/wiki/Quantum_field_theory",
    ]
    calls = {"n": 0}

    def obs_factory():
        calls["n"] += 1
        return {
            "tabId": 1, "url": seq[calls["n"] - 1], "title": "t",
            "interactive": [], "scroll": {}, "dpr": 1,
        }

    class Planner:
        def __call__(self, i, p, h):
            if calls["n"] >= len(seq):
                return {"goal": "research", "steps": []}
            target = seq[calls["n"]]
            return {
                "goal": "research",
                "steps": [{
                    "why": "check page",
                    "action": {
                        "type": "navigate",
                        "risk": "read",
                        "verify": "v",
                        "timeoutMs": 1000,
                        "url": target,
                    },
                }],
            }

    state = asyncio.run(agentLoop.run_with_injection(
        hub, planner=Planner(), obs_factory=obs_factory,
    ))
    assert state["final"] == "completed", state
    assert "Loop detected" not in (state["summary"] or ""), state


def test_live_observation_recovers_from_stale_takeover_flag(monkeypatch):
    """A TAKEOVER_ACTIVE flag no task owns must not wedge every later
    task. Live failure (2026-11-19): a research task failed with
    \"loop error: observation failed: TAKEOVER_ACTIVE\" and the
    immediate retry (\"check again\") failed the same way — the
    extension's global flag was stuck set after a takeover ended
    without its TAKEOVER_END. When no other task is waiting in
    takeover, the observation clears the stale flag and retries."""
    calls = {"n": 0}
    notified: list[dict] = []

    class Hub:
        extension_sockets = [object()]
        tasks: dict = {}

        async def send_action(self, task_id, action, timeout_s=45.0):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"ok": False, "error": {"code": "TAKEOVER_ACTIVE",
                                               "message": "takeover in progress",
                                               "recoverable": True}}
            return {"ok": True, "observation": {"tabId": 1, "url": "https://example.com", "title": "t"}}

        async def notify_extension(self, message):
            notified.append(message)

    monkeypatch.setattr(agentLoop, "real_hub", Hub())
    result = asyncio.run(agentLoop._LiveObservation("t1")())
    assert result["url"] == "https://example.com"
    assert notified == [{"type": "TAKEOVER_END", "taskId": "t1"}]


def test_live_observation_does_not_clear_genuine_takeover(monkeypatch):
    """The stale-flag recovery must NOT clear a takeover another task
    is genuinely waiting in — that would let the agent observe while
    the operator is typing a password or a payment. It fails
    honestly instead, naming the blocker instead of the raw error
    dict."""
    from types import SimpleNamespace

    class Hub:
        extension_sockets = [object()]
        tasks = {"other": SimpleNamespace(task_id="other", state="awaiting_takeover")}

        async def send_action(self, task_id, action, timeout_s=45.0):
            return {"ok": False, "error": {"code": "TAKEOVER_ACTIVE",
                                           "message": "takeover in progress",
                                           "recoverable": True}}

        async def notify_extension(self, message):
            raise AssertionError("must not clear a genuine takeover")

    monkeypatch.setattr(agentLoop, "real_hub", Hub())
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(agentLoop._LiveObservation("t1")())
    assert "takeover is still active" in str(exc.value), exc.value
    assert "TAKEOVER_ACTIVE" not in str(exc.value), exc.value
