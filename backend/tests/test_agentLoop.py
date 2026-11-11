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
