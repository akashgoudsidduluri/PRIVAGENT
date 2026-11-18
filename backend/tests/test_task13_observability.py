"""Task 13 — observability: one structured JSON log line per executed step.

Pins the format (task_id, step, action type, durations, outcome,
batched/cached flags), the logger name (``navigator.observe``), and
the privacy property: no page text, element name, typed value, URL,
or target id ever reaches the log — even when the page and the
instruction are full of distinctive strings.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app import agentLoop
from app.latency import LoopTimings
from app.observability import EVENT_LOOP_STEP, log_step

LOGIN_URL = "https://moodle.vce.ac.in/login/index.php"


def _el(target_id: str, role: str, name: str, tag: str = "input",
         input_type: str | None = None) -> dict[str, Any]:
    el: dict[str, Any] = {
        "targetId": target_id, "role": role, "name": name, "tag": tag,
        "visible": True, "enabled": True,
        "bounds": {"x": 100, "y": 100, "width": 200, "height": 30},
    }
    if input_type is not None:
        el["inputType"] = input_type
    return el


def _obs() -> dict[str, Any]:
    return {
        "tabId": 1, "url": LOGIN_URL, "title": "Log in SECRET-TITLE-9f3a",
        "interactive": [
            _el("username", "textbox", "Username SECRET-NAME-7b2c", input_type="text"),
            _el("password", "textbox", "Password", input_type="password"),
            _el("loginbtn", "button", "Log in", tag="button"),
        ],
        "scroll": {"y": 0, "viewport": {"w": 1280, "h": 800}},
        "dpr": 1,
    }


def _type(ref: str, name: str, target_id: str, text: str) -> dict[str, Any]:
    return {
        "type": "type", "risk": "write", "verify": "typed",
        "timeoutMs": 5000,
        "target": {"targetId": target_id, "name": name, "elementRef": ref},
        "params": {"text": text, "clear": True, "submit": False},
    }


def _click(ref: str, name: str, target_id: str) -> dict[str, Any]:
    return {
        "type": "click", "risk": "write", "verify": "clicked",
        "timeoutMs": 5000,
        "target": {"targetId": target_id, "name": name, "elementRef": ref},
    }


def _step(action: dict[str, Any]) -> dict[str, Any]:
    return {"why": "w", "action": action}


class _Hub:
    def __init__(self, fail_types: frozenset[str] = frozenset()) -> None:
        self.fail_types = fail_types
        self.dispatched: list[str] = []

    async def send_action(self, task_id: str, action: dict[str, Any],
                          timeout_s: float = 45.0) -> dict[str, Any]:
        self.dispatched.append(action.get("type", "?"))
        if action.get("type") in self.fail_types:
            return {"ok": False, "error": {"code": "TIMEOUT", "message": "SECRET-ERROR-5d1e", "recoverable": True}}
        return {"ok": True, "evidence": "SECRET-EVIDENCE-4c8a", "observation": _obs()}


def _observed(caplog) -> list[dict[str, Any]]:
    return [json.loads(r.message) for r in caplog.records
            if r.name == "navigator.observe"]


# ---------------------------------------------------------------------------
# Unit level
# ---------------------------------------------------------------------------

def test_log_step_format_and_outcome(caplog):
    timings = LoopTimings()
    timings.begin_step()
    entry = timings.flush_step(1, "type", batched=False)
    with caplog.at_level(logging.INFO, logger="navigator.observe"):
        log_step("task-123", entry, "ok")
        log_step("task-123", entry, "anything-else")
    lines = _observed(caplog)
    assert len(lines) == 2
    first, second = lines
    assert first["event"] == EVENT_LOOP_STEP == "loop_step"
    assert first["task_id"] == "task-123"
    assert first["step"] == 1 and first["action"] == "type"
    assert first["outcome"] == "ok"
    assert second["outcome"] == "failed"  # any non-"ok" outcome normalises
    assert first["batched"] is False and first["cache"] in ("hit", "miss", "n/a")
    assert isinstance(first["total_ms"], int) and isinstance(first["overhead_ms"], int)
    assert isinstance(first["plan_ms"], int) and isinstance(first["execute_ms"], int)
    # Keys are sorted — the line is diff/grep stable.
    first_record = next(r for r in caplog.records if r.name == "navigator.observe")
    assert first_record.message == json.dumps(first, sort_keys=True)


def test_log_step_never_raises(caplog):
    # A logging failure must not fail the task it observes.
    with caplog.at_level(logging.INFO, logger="navigator.observe"):
        log_step("task-123", object(), "ok")  # no as_dict() at all
        log_step(None, None, "ok")  # type: ignore[arg-type]
    assert _observed(caplog) == []


# ---------------------------------------------------------------------------
# Loop level
# ---------------------------------------------------------------------------

def _run_login(hub, caplog, fail_types=frozenset()):
    calls = {"planner": 0}

    def planner(instruction, payload, history, working_memory=None):
        calls["planner"] += 1
        if calls["planner"] == 1:
            return {"goal": "g", "steps": [
                _step(_type("e0", "Username", "username", "SECRET-TYPED-1a9d")),
                _step(_type("e1", "Password", "password", "SECRET-TYPED-2b8e")),
                _step(_click("e2", "Log in", "loginbtn")),
            ]}
        return {"goal": "g", "steps": []}

    with caplog.at_level(logging.INFO, logger="navigator.observe"):
        result = asyncio.run(agentLoop.run_with_injection(
            hub, planner=planner, obs_factory=lambda: _obs(),
            task_id="task13-observe", instruction="log in SECRET-INSTRUCTION-6e7f",
        ))
    return result, calls


def test_loop_emits_one_json_line_per_step_with_no_page_content(caplog):
    result, _ = _run_login(_Hub(), caplog)
    assert result["final"] == "completed"
    lines = _observed(caplog)
    assert len(lines) == 3  # two types + the click
    assert [ln["step"] for ln in lines] == [1, 2, 3]
    assert [ln["action"] for ln in lines] == ["type", "type", "click"]
    assert all(ln["task_id"] == "task13-observe" for ln in lines)
    assert all(ln["outcome"] == "ok" for ln in lines)
    # The batched steps (2nd type + click) ran inside one round trip.
    assert lines[0]["batched"] is False
    assert lines[1]["batched"] is True and lines[2]["batched"] is True
    # Nothing page-derived reaches the log: not the typed values, not
    # the element names, not the title, not the URL, not the evidence,
    # not the instruction, not even a target id.
    blob = "\n".join(json.dumps(ln) for ln in lines)
    for secret in ("SECRET-TYPED-1a9d", "SECRET-TYPED-2b8e",
                   "SECRET-NAME-7b2c", "SECRET-TITLE-9f3a",
                   "SECRET-INSTRUCTION-6e7f", "SECRET-EVIDENCE-4c8a",
                   "moodle.vce.ac.in", "username", "password", "loginbtn"):
        assert secret not in blob


def test_loop_failed_step_is_logged_failed(caplog):
    result, _ = _run_login(_Hub(fail_types=frozenset({"click"})), caplog)
    lines = _observed(caplog)
    assert len(lines) >= 1
    # The two types succeeded; the click failed (recoverable, so the
    # loop retries it — every attempt is logged).
    assert lines[0]["outcome"] == "ok" and lines[1]["outcome"] == "ok"
    assert any(ln["action"] == "click" and ln["outcome"] == "failed" for ln in lines)
    assert "SECRET-ERROR-5d1e" not in "\n".join(json.dumps(ln) for ln in lines)
