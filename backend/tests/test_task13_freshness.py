"""Task 13 — check_freshness is real now (Task 7 design flaw).

The old loop compared a node against the tree it was resolved from,
so "changed" was unreachable — a node always matches its own tree.
These tests pin the real behaviour:

* grounding level: ``check_freshness`` reports "changed" when the
  role, name, OR stable target id moves between plan and execution;
  ``ref_freshness`` / ``fresh_tree_for_observation`` (the loop's
  helpers) agree with it.
* loop level, batch path: a post-action observation that renames,
  removes, or re-ids the NEXT pinned target ends the batch and
  replans — the Task 11 fingerprint gate alone would NOT catch these
  (same URL/title/count/ids).
* loop level, single-step path: a confirmed step is re-verified
  against one fresh observation after the operator wait — a changed
  element proceeds on the LIVE node (target re-pinned), a vanished
  one replans instead of executing, an unchanged one proceeds with
  no extra replan.
"""
from __future__ import annotations

import asyncio
from typing import Any

from app import agentLoop
from app.contracts import BrowserObservation
from app.grounding import AxNode, AxTree, build_ax_tree, check_freshness
from app.loop_helpers import (
    _plan_tree_and_refs,
    fresh_tree_for_observation,
    ref_freshness,
)
from app.privacy import classify_and_redact

LOGIN_URL = "https://moodle.vce.ac.in/login/index.php"


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
         elements: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
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


def _type(ref: str, name: str, target_id: str, text: str,
          risk: str = "write") -> dict[str, Any]:
    return {
        "type": "type", "risk": risk, "verify": f"typed into {name}",
        "timeoutMs": 5000,
        "target": {"targetId": target_id, "name": name, "elementRef": ref},
        "params": {"text": text, "clear": True, "submit": False},
    }


def _click(ref: str, name: str, target_id: str, risk: str = "write") -> dict[str, Any]:
    return {
        "type": "click", "risk": risk, "verify": f"clicked {name}",
        "timeoutMs": 5000,
        "target": {"targetId": target_id, "name": name, "elementRef": ref},
    }


def _step(action: dict[str, Any], why: str = "w") -> dict[str, Any]:
    return {"why": why, "action": action}


def _run(hub, planner, obs_factory, **kw):
    return asyncio.run(agentLoop.run_with_injection(
        hub, planner=planner, obs_factory=obs_factory,
        task_id="task13-freshness", instruction="log in", **kw))


# ---------------------------------------------------------------------------
# Grounding level
# ---------------------------------------------------------------------------

def test_check_freshness_target_id_change_is_changed():
    plan = build_ax_tree([
        _el("username", "textbox", "Username", input_type="text"),
        _el("password", "textbox", "Password", input_type="password"),
    ])
    node = plan.nodes[1]
    assert check_freshness(node, plan) == "ok"
    # Same role/name, different stable id — a re-render reassigned it.
    re_ided = AxTree(nodes=[
        AxNode(ref="e0", role="textbox", name="Username", target_id="username"),
        AxNode(ref="e1", role="textbox", name="Password", target_id="password2"),
    ])
    assert check_freshness(node, re_ided) == "changed"
    # A node built without an id is compared on role/name alone.
    no_id = AxTree(nodes=[
        AxNode(ref="e0", role="textbox", name="Username"),
        AxNode(ref="e1", role="textbox", name="Password"),
    ])
    assert check_freshness(node, no_id) == "ok"


def test_ref_freshness_verdicts():
    plan_tree = build_ax_tree([
        _el("username", "textbox", "Username", input_type="text"),
        _el("password", "textbox", "Password", input_type="password"),
    ])
    fresh, live, plan_node = ref_freshness(plan_tree, "e1", None)
    assert fresh == "ok" and live is plan_node and plan_node.name == "Password"
    renamed = build_ax_tree([
        _el("username", "textbox", "Username", input_type="text"),
        _el("password", "textbox", "One-time code", input_type="password"),
    ])
    fresh, live, plan_node = ref_freshness(plan_tree, "e1", renamed)
    assert fresh == "changed"
    assert live is not None and live.name == "One-time code"
    assert plan_node is not None and plan_node.name == "Password"
    gone = build_ax_tree([
        _el("username", "textbox", "Username", input_type="text"),
    ])
    fresh, live, plan_node = ref_freshness(plan_tree, "e1", gone)
    assert fresh == "stale" and live is None and plan_node.name == "Password"
    fresh, live, plan_node = ref_freshness(plan_tree, "e9", renamed)
    assert fresh == "stale" and live is None and plan_node is None


def test_fresh_tree_matches_plan_time_pipeline():
    obs = _obs()
    payload, _ = classify_and_redact(BrowserObservation.model_validate(obs), instruction="log in")
    plan_tree, _ = _plan_tree_and_refs(payload)
    built = fresh_tree_for_observation(_obs(), "log in")
    assert built is not None
    fresh_tree, _ = built
    assert [n.ref for n in fresh_tree.nodes] == [n.ref for n in plan_tree.nodes]
    assert [n.name for n in fresh_tree.nodes] == [n.name for n in plan_tree.nodes]
    # A renamed element shows up as a real "changed", not an artifact.
    renamed = _obs(elements=[
        _el("username", "textbox", "Username", input_type="text"),
        _el("password", "textbox", "One-time code", input_type="password"),
        _el("loginbtn", "button", "Log in", tag="button"),
    ])
    built2 = fresh_tree_for_observation(renamed, "log in")
    assert built2 is not None
    node = plan_tree.nodes[1]
    assert check_freshness(node, built2[0]) == "changed"


def test_fresh_tree_unredactable_returns_none():
    # An observation that cannot even be validated is "cannot verify",
    # never an exception leaking into the loop.
    assert fresh_tree_for_observation({"interactive": "not-a-list"}, "log in") is None


# ---------------------------------------------------------------------------
# Loop level — batch path (Task 11 fingerprint gate would NOT catch these)
# ---------------------------------------------------------------------------

class _Hub:
    """Fake extension hub; returns a fixed post-action observation."""

    def __init__(self, after: dict[str, Any] | None = None) -> None:
        self.after = after if after is not None else _obs()
        self.dispatched: list[dict[str, Any]] = []

    async def send_action(self, task_id: str, action: dict[str, Any],
                          timeout_s: float = 45.0) -> dict[str, Any]:
        self.dispatched.append(action)
        if action.get("type") == "click" and (action.get("target") or {}).get("targetId") == "loginbtn":
            return {"ok": True, "evidence": "logged in",
                    "observation": _obs(url="https://moodle.vce.ac.in/", title="Home", elements=[
                        _el("search", "searchbox", "Search courses", input_type="search"),
                        _el("searchbtn", "button", "Search courses", tag="button"),
                    ])}
        return {"ok": True, "evidence": "ok", "observation": self.after}


def _login_plan() -> list[dict[str, Any]]:
    return [
        _step(_type("e0", "Username", "username", "student")),
        _step(_type("e1", "Password", "password", "hunter2")),
        _step(_click("e2", "Log in", "loginbtn")),
    ]


def _run_batch(after: dict[str, Any]) -> tuple[dict[str, Any], _Hub, dict[str, int]]:
    hub = _Hub(after=after)
    calls = {"planner": 0}

    def planner(instruction, payload, history, working_memory=None):
        calls["planner"] += 1
        if calls["planner"] == 1:
            return {"goal": "g", "steps": _login_plan()}
        return {"goal": "g", "steps": []}

    result = _run(hub, planner, lambda: _obs())
    return result, hub, calls


def test_batch_ends_when_next_target_renamed():
    # Same URL/title/count/ids as the plan observation — the Task 11
    # fingerprint matches, so only the per-ref check can catch this.
    after = _obs(elements=[
        _el("username", "textbox", "Username", input_type="text"),
        _el("password", "textbox", "One-time code", input_type="password"),
        _el("loginbtn", "button", "Log in", tag="button"),
    ])
    result, hub, calls = _run_batch(after)
    assert result["final"] == "completed"
    assert calls["planner"] == 2  # replanned instead of executing blind
    assert [a.get("type") for a in hub.dispatched] == ["type"]  # only username
    assert all((a.get("target") or {}).get("targetId") != "password" for a in hub.dispatched)


def test_batch_ends_when_next_target_disappears():
    after = _obs(elements=[
        _el("username", "textbox", "Username", input_type="text"),
        _el("loginbtn", "button", "Log in", tag="button"),
    ])
    result, hub, calls = _run_batch(after)
    assert result["final"] == "completed"
    assert calls["planner"] == 2
    assert [a.get("type") for a in hub.dispatched] == ["type"]


def test_batch_ends_when_next_target_id_moves():
    after = _obs(elements=[
        _el("username", "textbox", "Username", input_type="text"),
        _el("password", "textbox", "Password", input_type="password"),
        _el("loginbtn", "button", "Log in", tag="button"),
    ])
    # Same name/role — only the stable id moved (re-render reassigned it).
    after["interactive"][1]["targetId"] = "password2"
    result, hub, calls = _run_batch(after)
    assert result["final"] == "completed"
    assert calls["planner"] == 2
    assert [a.get("type") for a in hub.dispatched] == ["type"]


def test_batch_ends_when_fresh_tree_unavailable(monkeypatch):
    # REVIEW (Task 13 audit): a post-action observation the redactor
    # cannot process is "cannot verify", not "ok". Before the fix the
    # batch path passed ``None`` into ``ref_freshness``, which returns
    # "ok" for a missing fresh tree — so the pinned password step
    # dispatched blind on the plan-time tree. Now the batch ends on
    # the shared one-retry counter and replans instead.
    monkeypatch.setattr(agentLoop, "fresh_tree_for_observation", lambda *a, **k: None)
    result, hub, calls = _run_batch(_obs())
    assert result["final"] == "completed"
    assert calls["planner"] == 2  # replanned instead of executing blind
    assert [a.get("type") for a in hub.dispatched] == ["type"]  # only username
    assert all((a.get("target") or {}).get("targetId") != "password" for a in hub.dispatched)


# ---------------------------------------------------------------------------
# Loop level — single-step path: re-verification after the operator wait
# ---------------------------------------------------------------------------

class _Gate:
    """Fake confirmation gate; swaps the live observation before
    approving, simulating the operator (or the page) moving the
    target while the loop waited."""

    def __init__(self, swap_to: dict[str, Any] | None, holder: dict[str, Any]) -> None:
        self.confirm_requests: list[dict[str, Any]] = []
        self.decision: bool | None = None
        self.confirm_event = asyncio.Event()
        self.swap_to = swap_to
        self.holder = holder

    async def auto(self) -> None:
        while True:
            if self.confirm_requests and not self.confirm_event.is_set():
                if self.swap_to is not None:
                    self.holder["obs"] = self.swap_to
                self.decision = True
                self.confirm_event.set()
            await asyncio.sleep(0.01)


def _run_with_gate(hub, planner, holder, gate):
    async def scenario():
        approver = asyncio.create_task(gate.auto())
        try:
            return await agentLoop.run_with_injection(
                hub, planner=planner,
                obs_factory=lambda: holder["obs"],
                task_id="task13-freshness-gate", instruction="log in",
                confirm_gate=gate,
            )
        finally:
            approver.cancel()

    return asyncio.run(scenario())


def _click_plan():
    calls = {"planner": 0}

    def planner(instruction, payload, history, working_memory=None):
        calls["planner"] += 1
        if calls["planner"] == 1:
            return {"goal": "g", "steps": [
                _step(_click("e2", "Log in", "loginbtn", risk="sensitive")),
            ]}
        return {"goal": "g", "steps": []}

    return planner, calls


def test_confirmed_step_reverified_changed_proceeds_on_live_node():
    holder = {"obs": _obs()}
    renamed = _obs(elements=[
        _el("username", "textbox", "Username", input_type="text"),
        _el("password", "textbox", "Password", input_type="password"),
        _el("loginbtn", "button", "Sign in", tag="button"),
    ])
    gate = _Gate(swap_to=renamed, holder=holder)
    planner, calls = _click_plan()
    hub = _Hub()
    result = _run_with_gate(hub, planner, holder, gate)
    assert result["final"] == "completed"
    # Call 2 is the completion check (empty plan) — no replan happened.
    assert calls["planner"] == 2
    assert len(hub.dispatched) == 1
    clicked = hub.dispatched[0]
    assert clicked.get("type") == "click"
    # The target was re-pinned to the LIVE node, not the plan-time one.
    assert (clicked.get("target") or {}).get("name") == "Sign in"
    assert (clicked.get("target") or {}).get("targetId") == "loginbtn"


def test_confirmed_step_reverified_empty_live_name_still_pinned_by_ref_and_id():
    # REVIEW (Task 13 audit, follow-up): the post-wait changed path
    # overwrites ``_target2["name"]`` unconditionally with the live
    # node's name. If a re-render strips the accessible name, the
    # live node carries "" — the step must still proceed on the live
    # node, pinned by ref + targetId + role (the extension treats an
    # empty wanted name as vacuous agreement and still requires ref +
    # targetId + role agreement), not replan or dispatch a stale
    # name. Before this test the path was reviewed but never
    # exercised with an empty-name live node.
    holder = {"obs": _obs()}
    unnamed = _obs(elements=[
        _el("username", "textbox", "Username", input_type="text"),
        _el("password", "textbox", "Password", input_type="password"),
        _el("loginbtn", "button", "", tag="button"),
    ])
    gate = _Gate(swap_to=unnamed, holder=holder)
    planner, calls = _click_plan()
    hub = _Hub()
    result = _run_with_gate(hub, planner, holder, gate)
    assert result["final"] == "completed"
    # Changed (name lost), but the live node is still the same control:
    # call 2 is the completion check — no replan happened.
    assert calls["planner"] == 2
    assert len(hub.dispatched) == 1
    clicked = hub.dispatched[0]
    assert clicked.get("type") == "click"
    target = clicked.get("target") or {}
    assert target.get("targetId") == "loginbtn"
    assert target.get("role") == "button"
    # The re-pinned name is the LIVE node's empty name, not the stale
    # plan-time "Log in" — the extension resolves by ref + id + role.
    assert target.get("name") == ""


def test_confirmed_step_reverified_stale_replans_instead():
    holder = {"obs": _obs()}
    gone = _obs(elements=[
        _el("username", "textbox", "Username", input_type="text"),
        _el("password", "textbox", "Password", input_type="password"),
    ])
    gate = _Gate(swap_to=gone, holder=holder)
    planner, calls = _click_plan()
    hub = _Hub()
    result = _run_with_gate(hub, planner, holder, gate)
    assert result["final"] == "completed"
    assert calls["planner"] == 2  # replanned on the fresh tree
    assert hub.dispatched == []  # the vanished button was never clicked


def test_confirmed_step_unchanged_proceeds_without_extra_replan():
    holder = {"obs": _obs()}
    gate = _Gate(swap_to=None, holder=holder)
    planner, calls = _click_plan()
    hub = _Hub()
    result = _run_with_gate(hub, planner, holder, gate)
    assert result["final"] == "completed"
    # Call 2 is the completion check (empty plan) — no replan happened.
    assert calls["planner"] == 2
    assert len(hub.dispatched) == 1
    assert hub.dispatched[0].get("type") == "click"


def test_no_wait_means_no_extra_observation():
    # Without a confirmation/pause wait there is no window for the
    # page to move, so the loop must not spend an extra observation
    # re-verifying a ref it just resolved.
    obs_calls = {"n": 0}

    async def obs_factory():
        obs_calls["n"] += 1
        return _obs()

    planner, calls = _click_plan()
    result = _run(_Hub(), planner, obs_factory)
    assert result["final"] == "completed"
    # Call 2 is the completion check (empty plan) — no replan happened.
    assert calls["planner"] == 2
    assert obs_calls["n"] == 1
