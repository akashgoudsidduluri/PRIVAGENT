"""Latency optimizations must not weaken privacy or freshness guarantees.

The loop may reuse the observation returned by a successful action, but it
must still revalidate and re-redact it on every iteration, and it must fall
back to a live observation after failures or tab changes.
"""
from __future__ import annotations

import asyncio
from typing import Any

from app import agentLoop


def _obs(url: str = "https://site.local/start", title: str = "Search") -> dict[str, Any]:
    return {
        "tabId": 1,
        "url": url,
        "title": title,
        "interactive": [],
        "scroll": {},
        "dpr": 1,
    }


def _scroll_action() -> dict[str, Any]:
    return {
        "type": "scroll",
        "risk": "read",
        "verify": "scrolled",
        "timeoutMs": 2000,
        "params": {"direction": "down"},
    }


def _click_action() -> dict[str, Any]:
    return {
        "type": "click",
        "risk": "write",
        "verify": "clicked",
        "timeoutMs": 2000,
        "target": {"targetId": "b1", "name": "Button"},
    }


class _Hub:
    """Returns a scripted result; records how many actions were dispatched."""

    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        self.calls = 0

    async def send_action(self, task_id: str, action: dict[str, Any], timeout_s: float = 45.0):
        self.calls += 1
        return self.result


def _run(hub: _Hub, planner, obs_factory, **kwargs) -> dict[str, Any]:
    return asyncio.run(agentLoop.run_with_injection(
        hub,
        planner=planner,
        obs_factory=obs_factory,
        task_id="latency-test",
        instruction="measure",
        **kwargs,
    ))


def test_successful_action_observation_is_reused_and_redacted():
    payloads: list[dict[str, Any]] = []
    obs_calls = 0
    after = _obs("https://site.local/results", "Contact alice@example.com")
    after["interactive"] = [{
        "targetId": "pw", "role": "textbox", "name": "Password",
        "tag": "input", "inputType": "password", "value": "hunter2",
        "visible": True, "enabled": True,
    }]
    hub = _Hub({"ok": True, "evidence": "done", "observation": after})

    async def obs_factory():
        nonlocal obs_calls
        obs_calls += 1
        return _obs()

    def planner(instruction: str, payload: dict[str, Any], history: list[dict[str, Any]]):
        payloads.append(payload)
        if len(payloads) >= 3:
            return {"goal": "done", "steps": []}
        return {"goal": "g", "steps": [{"why": "w", "action": _scroll_action()}]}

    result = _run(hub, planner, obs_factory)

    assert result["final"] == "completed"
    # Step 0 observes live; the next two planner calls reuse the action result.
    assert obs_calls == 1
    assert [p.url for p in payloads] == [
        "https://site.local/start",
        "https://site.local/results",
        "https://site.local/results",
    ]
    # The cached observation is still run through classify_and_redact().
    assert "alice@example.com" not in payloads[1].title
    assert "[REDACTED]" in payloads[1].title
    assert payloads[1].interactive[0].value is None
    assert "hunter2" not in str(payloads[1])


def test_failed_action_forces_a_fresh_observation(monkeypatch):
    monkeypatch.setattr(agentLoop, "MAX_RETRIES", 0)
    payloads: list[dict[str, Any]] = []
    obs_calls = 0
    # Even though the failure carries an observation, it must not be trusted.
    hub = _Hub({
        "ok": False,
        "error": {"code": "STALE_TARGET", "message": "gone", "recoverable": True},
        "observation": _obs("https://site.local/stale", "stale"),
    })

    async def obs_factory():
        nonlocal obs_calls
        obs_calls += 1
        return _obs()

    def planner(instruction: str, payload: dict[str, Any], history: list[dict[str, Any]]):
        payloads.append(payload)
        if len(payloads) >= 2:
            return {"goal": "done", "steps": []}
        return {"goal": "g", "steps": [{"why": "w", "action": _click_action()}]}

    _run(hub, planner, obs_factory)

    assert obs_calls == 2
    assert payloads[1].url == "https://site.local/start"


def test_tab_switch_forces_a_fresh_observation():
    payloads: list[dict[str, Any]] = []
    obs_calls = 0
    hub = _Hub({
        "ok": True,
        "evidence": "switched",
        "observation": _obs("https://site.local/wrong-tab", "wrong"),
    })

    async def obs_factory():
        nonlocal obs_calls
        obs_calls += 1
        return _obs()

    def planner(instruction: str, payload: dict[str, Any], history: list[dict[str, Any]]):
        payloads.append(payload)
        if len(payloads) >= 2:
            return {"goal": "done", "steps": []}
        return {
            "goal": "g",
            "steps": [{"why": "w", "action": {
                "type": "select_tab", "risk": "read", "verify": "active",
                "timeoutMs": 2000, "params": {"tabId": 2},
            }}],
        }

    # Tab 2 is registered up front: the loop only switches to tabs it
    # has observed (or that were seeded), and this test's point is the
    # fresh observation after the switch, not the gate.
    _run(hub, planner, obs_factory, initial_memory={"tabs": [
        {"tab_id": 1, "url": "https://site.local/start", "title": "primary", "active": True},
        {"tab_id": 2, "url": "https://site.local/wrong-tab", "title": "other", "active": False},
    ]})

    assert obs_calls == 2
    assert payloads[1].url == "https://site.local/start"


def test_malformed_action_observation_falls_back(monkeypatch):
    monkeypatch.setattr(agentLoop, "MAX_RETRIES", 0)
    payloads: list[dict[str, Any]] = []
    obs_calls = 0
    hub = _Hub({"ok": True, "observation": {"url": "https://site.local/partial"}})

    async def obs_factory():
        nonlocal obs_calls
        obs_calls += 1
        return _obs()

    def planner(instruction: str, payload: dict[str, Any], history: list[dict[str, Any]]):
        payloads.append(payload)
        if len(payloads) >= 2:
            return {"goal": "done", "steps": []}
        return {"goal": "g", "steps": [{"why": "w", "action": _scroll_action()}]}

    _run(hub, planner, obs_factory)

    assert obs_calls == 2
    assert payloads[1].url == "https://site.local/start"


def test_step_budget_summary_reuses_last_observation(monkeypatch):
    monkeypatch.setattr(agentLoop, "MAX_STEPS", 1)
    obs_calls = 0
    hub = _Hub({
        "ok": True,
        "evidence": "clicked",
        "observation": _obs("https://site.local/after-action", "After"),
    })

    async def obs_factory():
        nonlocal obs_calls
        obs_calls += 1
        return _obs()

    def planner(instruction: str, payload: dict[str, Any], history: list[dict[str, Any]]):
        return {"goal": "g", "steps": [{"why": "w", "action": _click_action()}]}

    result = _run(hub, planner, obs_factory)

    assert result["final"] == "budget_exceeded"
    assert result["steps"] == 1
    assert "after-action" in (result["summary"] or "")
    assert obs_calls == 1
