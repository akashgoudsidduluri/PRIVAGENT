"""Task 1 (automation): outcome verification + stale-ref / dialog recovery.

Covers, at the unit level and through the real loop:

(a) a step that reports ok but changes nothing is a silent no-op:
    the loop records it as unverified, does NOT advance on the pinned
    plan, and re-observes + replans instead;
(b) a stale element ref (STALE_TARGET from the extension) is
    recovered by one re-observe + one retry with a freshly resolved
    ref — exactly once, recorded in the step record;
    the same machinery for a popup/dialog error;
(c) the recorded demo run (backend/scripts/demo_automation.py)
    completes across its two fixture sites with verified outcomes
    and exactly one recovery.

Fixture URLs use https://site.local — never a real site, and never
the banned demo anchor.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from typing import Any

from app import agentLoop
from app.grounding import AxNode, AxTree, build_ax_tree
from app.outcome import (
    detect_dialog,
    is_dialog_error,
    is_stale_target_error,
    resolve_fresh_ref,
    verify_step_outcome,
)

SITE_URL = "https://site.local/start"
OTHER_URL = "https://site.local/other"


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


def _obs(url: str = SITE_URL, title: str = "Start",
         elements: list[dict[str, Any]] | None = None,
         page_text: str = "start page") -> dict[str, Any]:
    return {
        "tabId": 1,
        "url": url,
        "title": title,
        "page_text": page_text,
        "interactive": elements if elements is not None else [
            _el("go", "link", "Go onward", tag="a"),
        ],
        "scroll": {"y": 0, "viewport": {"w": 1280, "h": 800}},
        "dpr": 1,
    }


def _run(hub, planner, obs_factory, **kw):
    return asyncio.run(agentLoop.run_with_injection(
        hub, planner=planner, obs_factory=obs_factory,
        task_id="automation-test", instruction="do the job", **kw))


# ---------------------------------------------------------------------------
# Unit: verify_step_outcome
# ---------------------------------------------------------------------------

def test_navigate_noop_is_unverified():
    action = {"type": "navigate", "params": {"url": SITE_URL}}
    result = {"ok": True, "evidence": "ok", "observation": _obs()}
    outcome = verify_step_outcome(action, result, before=_obs())
    assert outcome["verified"] is False
    assert outcome["expects_change"] is True
    assert "no-op" in outcome["reason"]


def test_navigate_arrival_is_verified():
    action = {"type": "navigate", "params": {"url": OTHER_URL}}
    result = {"ok": True, "evidence": "ok", "observation": _obs(url=OTHER_URL, title="Other")}
    outcome = verify_step_outcome(action, result, before=_obs())
    assert outcome["verified"] is True


def test_click_with_surface_change_is_verified():
    action = {"type": "click", "target": {"targetId": "go", "name": "Go onward"}}
    result = {"ok": True, "evidence": "ok",
              "observation": _obs(url=OTHER_URL, title="Other", page_text="other page")}
    outcome = verify_step_outcome(action, result, before=_obs())
    assert outcome["verified"] is True
    assert outcome["signals"]["url_changed"] is True


def test_click_with_no_change_and_no_evidence_is_unverified():
    action = {"type": "click", "target": {"targetId": "go", "name": "Go onward"}}
    result = {"ok": True, "evidence": "ok", "observation": _obs()}
    outcome = verify_step_outcome(action, result, before=_obs())
    assert outcome["verified"] is False


def test_click_with_target_echo_is_verified():
    # The executor aimed at the right element and says the dispatch
    # landed, even though the observation shows no surface delta
    # (a toggle, a select that fills a hidden field).
    action = {"type": "click", "target": {"targetId": "go", "name": "Go onward"}}
    result = {"ok": True, "evidence": "ok", "observation": _obs(), "targetId": "go"}
    outcome = verify_step_outcome(action, result, before=_obs())
    assert outcome["verified"] is True


def test_non_submitting_type_needs_no_surface_change():
    # Typing into a field legitimately leaves the AX observation
    # unchanged (no value echo in the tree): the dispatch is the
    # outcome. Requiring a surface change here would break batching.
    action = {"type": "type", "target": {"targetId": "q", "name": "Search"},
              "params": {"text": "hello", "submit": False}}
    result = {"ok": True, "evidence": "ok", "observation": _obs()}
    outcome = verify_step_outcome(action, result, before=_obs())
    assert outcome["verified"] is True
    assert outcome["expects_change"] is False


def test_submitting_type_without_change_is_unverified():
    action = {"type": "type", "target": {"targetId": "q", "name": "Search"},
              "params": {"text": "hello", "submit": True}}
    result = {"ok": True, "evidence": "ok", "observation": _obs()}
    outcome = verify_step_outcome(action, result, before=_obs())
    assert outcome["verified"] is False


def test_observe_and_wait_have_no_outcome_to_verify():
    for atype in ("observe", "scroll", "wait", "zoom"):
        outcome = verify_step_outcome(
            {"type": atype}, {"ok": True, "observation": _obs()}, before=_obs())
        assert outcome["verified"] is True
        assert outcome["expects_change"] is False


def test_failed_action_is_never_verified():
    outcome = verify_step_outcome(
        {"type": "click"}, {"ok": False, "error": {"message": "gone"}}, before=_obs())
    assert outcome["verified"] is False


# ---------------------------------------------------------------------------
# Unit: dialog detection + fresh-ref resolution
# ---------------------------------------------------------------------------

def test_detect_dialog_from_role_and_error_code():
    dialog_obs = _obs(elements=[
        _el("ok", "button", "OK", tag="button"),
        {"targetId": "dlg", "role": "dialog", "name": "Sign up",
         "tag": "div", "visible": True, "enabled": True,
         "bounds": {"x": 0, "y": 0, "width": 400, "height": 300}},
    ])
    assert detect_dialog(dialog_obs, {"ok": True}) is True
    assert detect_dialog(_obs(), {"ok": False, "error": {"code": "DIALOG"}}) is True
    assert detect_dialog(_obs(), {"ok": True, "observation": _obs()}) is False
    assert is_dialog_error({"ok": False, "error": {"code": "UNEXPECTED_DIALOG"}}) is True
    assert is_dialog_error({"ok": False, "error": {"code": "STALE_TARGET"}}) is False
    assert is_stale_target_error({"ok": False, "error": {"code": "STALE_TARGET"}}) is True


def test_resolve_fresh_ref_by_target_id_then_name_role():
    plan = build_ax_tree([
        _el("go", "link", "Go onward", tag="a"),
        _el("back", "link", "Back", tag="a"),
    ])
    node = plan.nodes[0]
    # Same target id under a new ref (list order shifted).
    moved_ref = AxTree(nodes=[
        AxNode(ref="e0", role="link", name="Home", target_id="home"),
        AxNode(ref="e1", role="link", name="Go onward", target_id="go"),
    ])
    ref, found = resolve_fresh_ref(node, moved_ref)
    assert ref == "e1" and found is not None and found.target_id == "go"
    # Same name + role, target id reassigned too.
    re_ided = build_ax_tree([
        _el("home", "link", "Home", tag="a"),
        _el("go-v2", "link", "Go onward", tag="a"),
    ])
    ref, found = resolve_fresh_ref(node, re_ided)
    assert ref == "e1" and found is not None and found.target_id == "go-v2"
    # Ambiguous: two controls share the name + role — never guess.
    ambiguous = build_ax_tree([
        _el("go-a", "link", "Go onward", tag="a"),
        _el("go-b", "link", "Go onward", tag="a"),
    ])
    ref, found = resolve_fresh_ref(node, ambiguous)
    assert (ref, found) == (None, None)
    # Gone entirely.
    gone = build_ax_tree([_el("back", "link", "Back", tag="a")])
    ref, found = resolve_fresh_ref(node, gone)
    assert (ref, found) == (None, None)


# ---------------------------------------------------------------------------
# Loop: (a) a silent no-op does not advance the loop
# ---------------------------------------------------------------------------

class _Hub:
    def __init__(self, after: dict[str, Any] | None = None) -> None:
        self.after = after if after is not None else _obs()
        self.dispatched: list[dict[str, Any]] = []

    async def send_action(self, task_id: str, action: dict[str, Any],
                          timeout_s: float = 45.0) -> dict[str, Any]:
        self.dispatched.append(action)
        return {"ok": True, "evidence": "ok", "observation": self.after}


def test_noop_navigate_is_recorded_unverified_and_forces_replan():
    obs_calls = {"n": 0}
    seen_histories: list[list[dict[str, Any]]] = []

    def obs_factory():
        obs_calls["n"] += 1
        return _obs()

    calls = {"planner": 0}

    def planner(instruction, payload, history, working_memory=None):
        calls["planner"] += 1
        seen_histories.append(list(history))
        if calls["planner"] == 1:
            return {"goal": "g", "steps": [{"why": "w", "action": {
                "type": "navigate", "risk": "read", "verify": "arrived",
                "timeoutMs": 5000, "params": {"url": SITE_URL},
            }}]}
        return {"goal": "g", "steps": []}

    result = _run(_Hub(), planner, obs_factory)
    assert result["final"] == "completed"
    assert len(result["history"]) == 1
    entry = result["history"][0]
    assert entry["result_ok"] is True
    assert entry["status"] == "completed_unverified"
    assert entry["outcome"]["verified"] is False
    assert "no-op" in entry["outcome"]["reason"]
    # The loop did NOT trust the cached post-action observation: it
    # re-observed fresh before replanning.
    assert obs_calls["n"] == 2
    assert calls["planner"] == 2
    # ... and the planner was told, as a penalty, that the step had no
    # effect, so it cannot silently repeat it.
    assert any(
        isinstance(h, dict) and "penalty" in h and "unverified" in str(h["penalty"]).lower()
        for h in seen_histories[-1]
    )
    # No batch was pinned on the no-op.
    assert result["timings"]["batched_steps"] == 0


def test_verified_navigate_does_not_force_extra_observation():
    # Control: the same shape with a real arrival reuses the cached
    # post-action observation (no extra round trip).
    obs_calls = {"n": 0}

    def obs_factory():
        obs_calls["n"] += 1
        return _obs()

    calls = {"planner": 0}

    def planner(instruction, payload, history, working_memory=None):
        calls["planner"] += 1
        if calls["planner"] == 1:
            return {"goal": "g", "steps": [{"why": "w", "action": {
                "type": "navigate", "risk": "read", "verify": "arrived",
                "timeoutMs": 5000, "params": {"url": OTHER_URL},
            }}]}
        return {"goal": "g", "steps": []}

    hub = _Hub(after=_obs(url=OTHER_URL, title="Other", page_text="other page"))
    result = _run(hub, planner, obs_factory)
    assert result["final"] == "completed"
    assert result["history"][0]["outcome"]["verified"] is True
    assert result["history"][0]["status"] == "verified"
    assert obs_calls["n"] == 1


# ---------------------------------------------------------------------------
# Loop: (b) stale-ref / dialog recovery — one re-observe + one retry
# ---------------------------------------------------------------------------

class _MovingHub:
    """First click on the target fails STALE_TARGET; the page model
    moves the control to a new ref / target id, so only a retry
    against the fresh observation can succeed."""

    def __init__(self) -> None:
        self.dispatched: list[dict[str, Any]] = []
        self.moved = False

    async def send_action(self, task_id: str, action: dict[str, Any],
                          timeout_s: float = 45.0) -> dict[str, Any]:
        self.dispatched.append(dict(action))
        target = action.get("target") or {}
        if str(target.get("targetId")) == "go":
            self.moved = True
            return {"ok": False, "error": {
                "code": "STALE_TARGET", "message": "element moved", "recoverable": True}}
        if str(target.get("targetId")) == "go-v2":
            return {"ok": True, "evidence": "clicked",
                    "observation": _obs(url=OTHER_URL, title="Other", page_text="other page")}
        return {"ok": True, "evidence": "ok", "observation": _obs()}


def _click_plan(ref: str = "e0") -> list[dict[str, Any]]:
    return [{"why": "w", "action": {
        "type": "click", "risk": "write", "verify": "clicked Go onward",
        "timeoutMs": 5000,
        "target": {"targetId": "go", "name": "Go onward", "elementRef": ref},
    }}]


def test_stale_ref_recovery_retries_once_with_fresh_ref(monkeypatch):
    # MAX_RETRIES=0 keeps _execute_with_retries from burning identical
    # retries first (that behaviour is pinned elsewhere); the recovery
    # under test is the loop's single fresh-ref retry.
    monkeypatch.setattr(agentLoop, "MAX_RETRIES", 0)
    hub = _MovingHub()
    holder = {"moved": False}

    def obs_factory():
        if hub.moved:
            # Fresh observation after the move: a link appeared above,
            # so the target shifted ref (e0 -> e1) and id (go -> go-v2).
            return _obs(elements=[
                _el("home", "link", "Home", tag="a"),
                _el("go-v2", "link", "Go onward", tag="a"),
            ])
        return _obs()

    calls = {"planner": 0}

    def planner(instruction, payload, history, working_memory=None):
        calls["planner"] += 1
        if calls["planner"] == 1:
            return {"goal": "g", "steps": _click_plan()}
        return {"goal": "g", "steps": []}

    result = _run(hub, planner, obs_factory)
    assert result["final"] == "completed"
    assert len(result["history"]) == 1
    entry = result["history"][0]
    assert entry["result_ok"] is True
    assert entry["outcome"]["verified"] is True
    recovery = entry["recovery"]
    assert recovery is not None
    assert recovery["kind"] == "stale_ref_retry"
    assert recovery["trigger"] == "STALE_TARGET"
    assert recovery["old_ref"] == "e0"
    assert recovery["new_ref"] == "e1"
    assert recovery["new_target_id"] == "go-v2"
    assert recovery["result_ok"] is True
    # Exactly two dispatches: the stale attempt + the one fresh retry.
    assert len(hub.dispatched) == 2
    retry = hub.dispatched[1]
    assert (retry.get("target") or {}).get("elementRef") == "e1"
    assert (retry.get("target") or {}).get("targetId") == "go-v2"
    # The recovery is also in working memory, for the timeline/demo.
    assert any(
        r.get("kind") == "stale_ref_retry" and r.get("step") == 1
        for r in (result.get("working_memory") or {}).get("automation_recoveries", [])
    )


def test_stale_ref_with_no_fresh_match_fails_honestly(monkeypatch):
    monkeypatch.setattr(agentLoop, "MAX_RETRIES", 0)
    hub = _MovingHub()
    obs_calls = {"n": 0}

    def obs_factory():
        # Plan time: the control is there. Recovery re-observation:
        # it is simply gone from the fresh observation.
        obs_calls["n"] += 1
        if obs_calls["n"] == 1:
            return _obs()
        return _obs(elements=[_el("home", "link", "Home", tag="a")])

    calls = {"planner": 0}

    def planner(instruction, payload, history, working_memory=None):
        calls["planner"] += 1
        if calls["planner"] == 1:
            return {"goal": "g", "steps": _click_plan()}
        return {"goal": "g", "steps": []}

    result = _run(hub, planner, obs_factory)
    assert result["final"] == "completed"
    entry = result["history"][0]
    assert entry["result_ok"] is False
    recovery = entry["recovery"]
    assert recovery is not None
    assert recovery["kind"] == "stale_ref_retry"
    assert recovery["new_ref"] is None
    assert recovery["result_ok"] is False
    # No blind retry was dispatched against a control that is not there.
    assert len(hub.dispatched) == 1


class _DialogHub:
    """First click fails with a dialog error; the fresh observation
    shows the same target with the dialog gone."""

    def __init__(self) -> None:
        self.dispatched: list[dict[str, Any]] = []
        self.attempts = 0

    async def send_action(self, task_id: str, action: dict[str, Any],
                          timeout_s: float = 45.0) -> dict[str, Any]:
        self.dispatched.append(dict(action))
        self.attempts += 1
        if self.attempts == 1:
            return {"ok": False, "error": {
                "code": "DIALOG", "message": "popup blocked the click",
                "recoverable": True}}
        return {"ok": True, "evidence": "clicked",
                "observation": _obs(url=OTHER_URL, title="Other", page_text="other page")}


def test_dialog_error_retries_once_on_fresh_observation(monkeypatch):
    monkeypatch.setattr(agentLoop, "MAX_RETRIES", 0)
    hub = _DialogHub()
    calls = {"planner": 0}

    def planner(instruction, payload, history, working_memory=None):
        calls["planner"] += 1
        if calls["planner"] == 1:
            return {"goal": "g", "steps": _click_plan()}
        return {"goal": "g", "steps": []}

    result = _run(hub, planner, lambda: _obs())
    assert result["final"] == "completed"
    entry = result["history"][0]
    assert entry["result_ok"] is True
    recovery = entry["recovery"]
    assert recovery is not None
    assert recovery["kind"] == "dialog_retry"
    assert recovery["trigger"] == "DIALOG"
    assert recovery["result_ok"] is True
    assert len(hub.dispatched) == 2


# ---------------------------------------------------------------------------
# (c) the recorded demo run
# ---------------------------------------------------------------------------

def _load_demo_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "demo_automation.py"
    spec = importlib.util.spec_from_file_location("demo_automation", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_demo_run_completes_with_verified_outcomes_and_one_recovery(tmp_path):
    demo = _load_demo_module()
    out = tmp_path / "automation_run.json"
    record = demo.run_demo(out)
    assert record["final"] == "completed"
    assert record["steps_done"] == 3
    assert record["provenance"]["live_chrome"] is False
    assert all(s["outcome_verified"] for s in record["steps"])
    assert all(s["result_ok"] for s in record["steps"])
    assert [s["ref_used"] for s in record["steps"]] == ["e1", "e1", "e1"]
    recoveries = record["recoveries"]
    assert len(recoveries) == 1
    rec = recoveries[0]
    assert rec["kind"] == "stale_ref_retry"
    assert rec["old_ref"] == "e0" and rec["new_ref"] == "e1"
    assert rec["result_ok"] is True
    assert record["counts"]["planner_calls"] == 4
    assert record["counts"]["actions_dispatched"] >= 4
    assert "on-device perception" in (record["summary"] or "")
    assert out.exists()
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["final"] == "completed"
    assert saved["steps"][2]["recovery"]["kind"] == "stale_ref_retry"
