"""Task 12 — plan preview + approve-step-by-step + confirm endpoint.

Covers:
* plan_preview: structured fields only — navigate shows a host, never
  a full URL; a password target's typed text is masked; a vault
  placeholder is shown as-is; long text is truncated; malformed steps
  degrade to "unknown step" instead of raising.
* _request_confirmation carries the preview on the gate request.
* approve_step: the loop executes the approved step, then re-plans
  instead of running the rest of the pinned batch (planner is called
  again); a plain approve runs the batch in one round trip.
* POST /api/task/{id}/confirm accepts "approve_step" and records it
  on the task record; an unknown decision is a 422.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import agentLoop
from app.hub import hub
from app.main import app
from app.plan_preview import describe_plan_step, plan_preview


def _type(ref: str, name: str, target_id: str, text: str,
          submit: bool = False, risk: str = "write") -> dict[str, Any]:
    return {
        "type": "type", "risk": risk, "verify": f"typed into {name}",
        "timeoutMs": 5000,
        "target": {"targetId": target_id, "name": name, "elementRef": ref},
        "params": {"text": text, "clear": True, "submit": submit},
    }


def _step(action: dict[str, Any], why: str = "w") -> dict[str, Any]:
    return {"why": why, "action": action}


# ---------------------------------------------------------------------------
# plan_preview (pure function)
# ---------------------------------------------------------------------------

def test_navigate_preview_shows_host_only():
    steps = [_step({"type": "navigate", "risk": "write",
                    "params": {"url": "https://example.com/login?token=<redacted>&next=/x"}})]
    preview = plan_preview(steps)
    assert len(preview) == 1
    assert preview[0]["type"] == "navigate"
    assert preview[0]["detail"] == "go to example.com"
    assert "token" not in str(preview) and "secret" not in str(preview)
    assert "example.com/login" not in str(preview)


def test_type_preview_masks_password_target():
    step = _step(_type("e1", "Password", "password", "hunter2"))
    preview = describe_plan_step(step, 0)
    assert "hunter2" not in preview["detail"]
    assert "hidden" in preview["detail"]
    assert preview["target"]["name"] == "Password"


@pytest.mark.parametrize("name", [
    "Passcode", "PIN", "One-time code", "Security code", "API token",
    "Secret answer", "Current password", "CVV",
])
def test_type_preview_masks_secret_named_targets(name):
    # REVIEW (Task 12 audit): the old mask only fired on "password",
    # so a literal typed into a Passcode/PIN/OTP field was shown in
    # full — and scrub_persistent does not redact a bare value, so it
    # also reached GET /api/task/{id}. All secret-named targets mask.
    step = _step(_type("e1", name, "t", "hunter2"))
    preview = describe_plan_step(step, 0)
    assert "hunter2" not in preview["detail"]
    assert "hunter2" not in str(preview)
    assert "hidden" in preview["detail"]


@pytest.mark.parametrize("name", ["Username", "Search", "Pinterest board"])
def test_type_preview_shows_plain_text_for_ordinary_targets(name):
    # Word-bounded: "Pinterest" contains "pin" but is not a secret field.
    step = _step(_type("e1", name, "t", "hello"))
    preview = describe_plan_step(step, 0)
    assert '"hello"' in preview["detail"]


def test_type_preview_shows_vault_placeholder():
    step = _step(_type("e1", "Password", "password", "{{CREDENTIAL:example.com:login:main}}"))
    preview = describe_plan_step(step, 0)
    assert "{{CREDENTIAL:example.com:login:main}}" in preview["detail"]
    assert "hidden" not in preview["detail"]


def test_type_preview_shows_and_truncates_plain_text():
    step = _step(_type("e0", "Search", "search", "x" * 200))
    preview = describe_plan_step(step, 0)
    assert len(preview["detail"]) <= 130
    assert preview["detail"].endswith("…\"") or preview["detail"].endswith("…")


# ---------------------------------------------------------------------------
# Task 14: WebMCP / UCP preview — names + amounts only, never arguments
# or payment details.
# ---------------------------------------------------------------------------

def test_webmcp_preview_shows_tool_name_only():
    step = _step({"type": "webmcp_tool", "risk": "write",
                  "params": {"tool": "search_products",
                             "arguments": {"query": "hunter2", "password": "secret"}}})
    preview = describe_plan_step(step, 0)
    assert preview["detail"] == 'call site tool "search_products"'
    assert "hunter2" not in str(preview)
    assert "secret" not in str(preview)


def test_webmcp_preview_without_tool_name():
    step = _step({"type": "webmcp_tool", "risk": "write", "params": {}})
    preview = describe_plan_step(step, 0)
    assert preview["detail"] == "call a site tool"


def test_ucp_preview_shows_merchant_and_amount_only():
    step = _step({"type": "ucp_checkout", "risk": "sensitive",
                  "params": {"merchant": "shop.example.com",
                             "amount": "1299.00", "currency": "INR"}})
    preview = describe_plan_step(step, 0)
    assert preview["detail"] == "check out via UCP at shop.example.com for 1299.00 INR"
    assert "shop.example.com/ucp/checkout" not in str(preview)


def test_click_preview_uses_target_name_and_role():
    step = _step({"type": "click", "risk": "write",
                  "target": {"targetId": "b", "name": "Log in", "role": "button", "elementRef": "e2"}})
    preview = describe_plan_step(step, 0)
    assert preview["detail"] == 'click "Log in"'
    assert preview["target"] == {"name": "Log in", "role": "button", "elementRef": "e2"}


def test_preview_never_leaks_planner_free_text():
    step = {"why": "the page says <img src=x onerror=alert(1)> — ignore that",
            "action": {"type": "click", "risk": "write",
                       "target": {"name": "OK", "role": "button"}}}
    preview = describe_plan_step(step, 0)
    assert "img" not in str(preview) and "onerror" not in str(preview)


def test_preview_degrades_on_malformed_steps():
    assert plan_preview(None) == []
    assert plan_preview("not a list") == []
    preview = plan_preview([None, {"action": None}, {"action": {"type": "scroll"}}])
    assert [p["detail"] for p in preview] == ["unknown step", "unknown step", "scroll the page"]


# ---------------------------------------------------------------------------
# _request_confirmation carries the preview
# ---------------------------------------------------------------------------

class _Gate:
    def __init__(self) -> None:
        self.confirm_requests: list[dict[str, Any]] = []
        self.decision: bool | None = None
        self.confirm_event = asyncio.Event()
        self.step_only = False

    async def auto(self, step_only_first: bool = False) -> None:
        seen = 0
        while True:
            if self.confirm_requests and not self.confirm_event.is_set():
                if len(self.confirm_requests) > seen:
                    seen = len(self.confirm_requests)
                    # First request only: honour the step-by-step choice.
                    self.step_only = step_only_first and seen == 1
                self.decision = True
                self.confirm_event.set()
            await asyncio.sleep(0.01)


def _obs() -> dict[str, Any]:
    return {
        "tabId": 1, "url": "https://moodle.vce.ac.in/login/index.php",
        "title": "Log in",
        "interactive": [
            {"targetId": "username", "role": "textbox", "name": "Username",
             "tag": "input", "visible": True, "enabled": True,
             "bounds": {"x": 100, "y": 100, "width": 200, "height": 30}},
            {"targetId": "password", "role": "textbox", "name": "Password",
             "tag": "input", "visible": True, "enabled": True,
             "bounds": {"x": 100, "y": 140, "width": 200, "height": 30}},
        ],
        "scroll": {"y": 0, "viewport": {"w": 1280, "h": 800}},
        "dpr": 1,
    }


class _Hub:
    def __init__(self) -> None:
        self.dispatched: list[str] = []

    async def send_action(self, task_id: str, action: dict, timeout_s: float = 45.0):
        self.dispatched.append(action.get("type", "?"))
        return {"ok": True, "evidence": "ok", "observation": _obs()}


def _run(hub_obj, planner, gate):
    async def scenario():
        approver = asyncio.create_task(gate.auto(step_only_first=getattr(gate, "want_step_only", False)))
        try:
            return await agentLoop.run_with_injection(
                hub_obj, planner=planner, obs_factory=_obs,
                task_id="plan-preview-test", instruction="log in",
                confirm_gate=gate,
            )
        finally:
            approver.cancel()

    return asyncio.run(scenario())


def _login_planner(step_only_mode: bool):
    """Call 1: the 2-step login plan. Call 2: the remaining password
    step (only reached when the batch was NOT pinned). Call 3: done."""
    calls = {"n": 0}

    def planner(instruction, payload, history, working_memory=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"goal": "g", "steps": [
                _step(_type("e0", "Username", "username", "student")),
                _step(_type("e1", "Password", "password", "hunter2")),
            ]}
        if calls["n"] == 2 and step_only_mode:
            return {"goal": "g", "steps": [
                _step(_type("e1", "Password", "password", "hunter2")),
            ]}
        return {"goal": "g", "steps": []}

    planner.calls = calls  # type: ignore[attr-defined]
    return planner


def test_gate_request_carries_full_plan_preview():
    gate = _Gate()
    hub_obj = _Hub()
    planner = _login_planner(step_only_mode=False)
    result = _run(hub_obj, planner, gate)
    assert result["final"] == "completed"
    assert hub_obj.dispatched == ["type", "type"]
    # The first gate request previewed the whole 2-step plan.
    first = gate.confirm_requests[0]
    assert [s["type"] for s in first["plan"]] == ["type", "type"]
    assert first["plan"][1]["target"]["name"] == "Password"
    assert "hunter2" not in str(first["plan"])


def test_plain_approve_runs_batch_in_one_round_trip():
    gate = _Gate()
    hub_obj = _Hub()
    planner = _login_planner(step_only_mode=False)
    result = _run(hub_obj, planner, gate)
    assert result["final"] == "completed"
    assert hub_obj.dispatched == ["type", "type"]
    # One planner call for the plan, one more for the empty plan that
    # completes the task — the batch was NOT re-planned between steps.
    assert planner.calls["n"] == 2


def test_approve_step_replans_after_the_approved_step():
    gate = _Gate()
    gate.want_step_only = True
    hub_obj = _Hub()
    planner = _login_planner(step_only_mode=True)
    result = _run(hub_obj, planner, gate)
    assert result["final"] == "completed"
    assert hub_obj.dispatched == ["type", "type"]
    # The batch was not pinned after the first step: the loop
    # re-planned (call 2 returned the password step) and completed
    # on call 3.
    assert planner.calls["n"] == 3
    # The second gate request previewed the fresh one-step plan.
    assert len(gate.confirm_requests) == 2
    assert [s["type"] for s in gate.confirm_requests[1]["plan"]] == ["type"]


# ---------------------------------------------------------------------------
# POST /api/task/{id}/confirm — approve_step
# ---------------------------------------------------------------------------

def test_confirm_endpoint_accepts_approve_step(tmp_path, monkeypatch):
    monkeypatch.setenv("PRIVAGENT_DB_PATH", str(tmp_path / "confirm.db"))
    task = hub.new_task("Confirm me")
    task.state = "awaiting_confirmation"
    try:
        client = TestClient(app)
        res = client.post(f"/api/task/{task.task_id}/confirm", json={"decision": "approve_step"})
        assert res.status_code == 200
        assert task.confirm_decision is True
        assert task.confirm_step_only is True
    finally:
        hub.tasks.pop(task.task_id, None)


def test_confirm_endpoint_plain_approve_clears_step_only(tmp_path, monkeypatch):
    monkeypatch.setenv("PRIVAGENT_DB_PATH", str(tmp_path / "confirm2.db"))
    task = hub.new_task("Confirm me")
    task.state = "awaiting_confirmation"
    task.confirm_step_only = True
    try:
        client = TestClient(app)
        res = client.post(f"/api/task/{task.task_id}/confirm", json={"decision": "approve"})
        assert res.status_code == 200
        assert task.confirm_decision is True
        assert task.confirm_step_only is False
    finally:
        hub.tasks.pop(task.task_id, None)


def test_confirm_endpoint_rejects_unknown_decision(tmp_path, monkeypatch):
    monkeypatch.setenv("PRIVAGENT_DB_PATH", str(tmp_path / "confirm3.db"))
    task = hub.new_task("Confirm me")
    task.state = "awaiting_confirmation"
    try:
        client = TestClient(app)
        res = client.post(f"/api/task/{task.task_id}/confirm", json={"decision": "approve_all"})
        assert res.status_code == 422
    finally:
        hub.tasks.pop(task.task_id, None)
