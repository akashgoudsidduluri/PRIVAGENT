"""Task 10 — Zero-retention mode.

Covers: history pruning (last N, minimal fields); checkpoints are
skipped; screenshots are stripped before planning; trajectory records
are scrubbed; and the end-to-end guarantee — plant a synthetic secret
token, run a task through the loop with zero-retention on, and assert
the token appears in no persisted record (no checkpoint written, no
token in the returned history, no screenshot reaching the planner).
"""
from __future__ import annotations

import asyncio

import pytest

from app import agentLoop, db
from app import zero_retention as zr

TOKEN = "ZR-PLANTED-TOKEN-7F3A9"


def _entry(action_type="click", evidence="done", error=None, result_ok=True):
    entry = {
        "action": {"type": action_type, "risk": "write", "verify": "v", "timeoutMs": 100,
                   "target": {"targetId": "b1", "name": "Button"}},
        "result_ok": result_ok,
        "status": "verified" if result_ok else "failed",
        "evidence": evidence,
        "narrative": f"Step 1: {action_type} -> {evidence}",
        "grounded_ref": "e3",
        "screenshot_attached": True,
    }
    if error is not None:
        entry["error"] = error
    return entry


def test_prune_history_keeps_last_n_minimal(monkeypatch):
    monkeypatch.setattr("app.config.ZERO_RETENTION_HISTORY_KEEP", 3)
    history = [_entry(evidence=f"page said {TOKEN} {i}") for i in range(5)]
    history[2]["error"] = "gone"
    history[2]["result_ok"] = False
    history[2]["status"] = "failed"
    pruned = zr.prune_history(history)
    assert len(pruned) == 3
    for entry in pruned:
        assert set(entry.keys()) <= {"action", "result_ok", "status", "error"}
        assert entry["action"] == {"type": "click"}
        assert "evidence" not in entry and "narrative" not in entry
        assert "grounded_ref" not in entry and "screenshot_attached" not in entry
    # The failed entry's (already redacted) error survives: loop
    # detection reads it.
    assert pruned[0]["error"] == "gone"
    assert TOKEN not in str(pruned)


def test_prune_history_respects_keep_setting(monkeypatch):
    monkeypatch.setattr("app.config.ZERO_RETENTION_HISTORY_KEEP", 1)
    history = [_entry() for _ in range(4)]
    assert len(zr.prune_history(history)) == 1


def test_checkpoints_allowed_flag(monkeypatch):
    monkeypatch.setattr("app.config.ZERO_RETENTION", False)
    assert zr.checkpoints_allowed() is True
    monkeypatch.setattr("app.config.ZERO_RETENTION", True)
    assert zr.checkpoints_allowed() is False


def test_strip_screenshot_dict_and_object():
    class Payload:
        screenshot = "data:image/png;base64,AAA"
    payload = Payload()
    raw = {"screenshot": "data:image/png;base64,AAA", "url": "https://example.com/"}
    zr.strip_screenshot(payload, raw)
    assert payload.screenshot is None
    assert raw["screenshot"] is None


def test_redact_trajectory_record_scrubs_error_and_drops_page_text():
    record = {
        "scenario": "Login fixture",
        "passed": False,
        "duration_ms": 12.5,
        "mode": "deterministic_fixture",
        # Credential-pattern values in the error must be scrubbed; the
        # arbitrary page token survives only if it is inside a field
        # that is dropped outright (page_text/screenshot below) — the
        # scrub is pattern-based, not magic, so the guarantee for
        # arbitrary page content is the field drop, not the scrub.
        "error": "step failed: password hunter2hunter2 / api_key=AKIAIOSFODNN7EXAMPLE",
        "page_text": f"secret page text {TOKEN}",
        "screenshot": "data:image/png;base64,AAA",
    }
    clean = zr.redact_trajectory_record(record)
    assert clean["scenario"] == "Login fixture"
    assert clean["passed"] is False
    assert "hunter2hunter2" not in str(clean)
    assert "AKIAIOSFODNN7EXAMPLE" not in str(clean)
    assert "page_text" not in clean and "screenshot" not in clean
    assert TOKEN not in str(clean)


def _run_loop_with_token(monkeypatch, zero_retention: bool, save_calls: list):
    """Run a 2-step loop whose observation and action evidence both
    carry the planted token. Returns the loop result + planner payloads seen."""
    monkeypatch.setattr("app.config.ZERO_RETENTION", zero_retention)
    monkeypatch.setattr("app.config.ZERO_RETENTION_HISTORY_KEEP", 3)
    # Screenshots are withheld by the redactor unless explicitly opted
    # in — enable them here so the test actually exercises the
    # zero-retention strip (otherwise the assertion would be vacuous).
    monkeypatch.setattr("app.config.ALLOW_SCREENSHOTS", True)
    # Record (and block) every checkpoint write attempt.
    monkeypatch.setattr(db, "save_task_checkpoint",
                        lambda **kw: save_calls.append(kw))
    monkeypatch.setattr(db, "init_db", lambda *a, **k: None)

    seen_payloads: list = []

    class Planner:
        calls = {"n": 0}

        def __call__(self, instruction, payload, history):
            seen_payloads.append(payload)
            self.calls["n"] += 1
            if self.calls["n"] == 1:
                return {"goal": "g", "steps": [{"why": "w", "action": {
                    "type": "click", "risk": "write", "verify": "v",
                    "timeoutMs": 100, "target": {"targetId": "b1"}}}]}
            return {"goal": "g", "steps": []}

    class Hub:
        ext_online = True

        async def send_action(self, task_id, action, timeout_s=45.0):
            await asyncio.sleep(0.01)
            return {"ok": True, "evidence": f"page said {TOKEN}"}

    state = asyncio.run(agentLoop.run_with_injection(
        Hub(), planner=Planner(),
        obs_factory=lambda: {
            "tabId": 1, "url": "https://example.com/", "title": "t",
            "interactive": [], "scroll": {}, "dpr": 1,
            "page_text": f"welcome, your code is {TOKEN}",
            "screenshot": "data:image/png;base64,AAA",
        },
    ))
    return state, seen_payloads


def test_zero_retention_loop_persists_nothing(monkeypatch):
    save_calls: list = []
    state, seen_payloads = _run_loop_with_token(monkeypatch, True, save_calls)
    assert state["final"] == "completed"
    # No checkpoint was written at all.
    assert save_calls == []
    # The planner never saw a screenshot. (The loop hands the planner
    # a dict from payload.model_dump(), so read the key, not an attr.)
    assert seen_payloads and all(
        (p.get("screenshot") if isinstance(p, dict) else getattr(p, "screenshot", None)) is None
        for p in seen_payloads
    )
    # The returned history carries no trace of the token.
    assert TOKEN not in str(state["history"])
    # ...and no history entry carries evidence/narrative at all.
    assert all("evidence" not in e and "narrative" not in e for e in state["history"])


def test_zero_retention_off_checkpoints_and_history_kept(monkeypatch):
    # Control: with the flag off, the same loop DOES checkpoint and
    # DOES keep evidence — proving the test above can detect a leak.
    save_calls: list = []
    state, seen_payloads = _run_loop_with_token(monkeypatch, False, save_calls)
    assert state["final"] == "completed"
    assert len(save_calls) >= 1
    assert TOKEN in str(state["history"])
    first = seen_payloads[0]
    shot = first.get("screenshot") if isinstance(first, dict) else getattr(first, "screenshot", None)
    assert shot is not None


def test_zero_retention_silences_provider_telemetry(monkeypatch, caplog):
    import logging
    from app.providers.limits import log_provider_call

    monkeypatch.setattr("app.config.ZERO_RETENTION", True)
    with caplog.at_level(logging.INFO, logger="privagent.providers"):
        log_provider_call(provider="groq", model="m", request_duration_ms=5,
                          token_usage={"total_tokens": 10}, http_status=200)
    assert "GROQ" not in caplog.text

    monkeypatch.setattr("app.config.ZERO_RETENTION", False)
    with caplog.at_level(logging.INFO, logger="privagent.providers"):
        log_provider_call(provider="groq", model="m", request_duration_ms=5,
                          token_usage={"total_tokens": 10}, http_status=200)
    assert "GROQ" in caplog.text


# --- REVIEW (Task 10 audit) --------------------------------------------------
# The loop-level tests above drive run_with_injection, which skips
# run_task's post-loop finalisation. That finalisation persisted the
# agent summary (which quotes the last evidence) and the extracted
# mission facts (last_result = summary[:240]) into the chat SQLite
# tables even with zero retention on — so a zero-retention task run
# through the production entrypoint DID persist page text. These
# tests drive run_task itself with a stubbed loop.

async def _run_task_with_stubbed_loop(monkeypatch, zero_retention: bool):
    """Run run_task with the loop stubbed to a completed 1-step task
    whose evidence/summary carry the planted token. Returns
    (record, chat_id, task_id)."""
    from app.hub import TaskRecord
    from app.session_manager import session_manager

    monkeypatch.setattr("app.config.ZERO_RETENTION", zero_retention)
    db.init_db()
    session_manager.clear()

    class MockHub:
        extension_sockets = ["dummy_ws"]
        tasks = {}

        def spawn(self, coro):
            return asyncio.create_task(coro)

        async def broadcast_progress(self, record):
            pass

        async def send_action(self, task_id, action, timeout_s=45.0):
            if action.get("type") == "init_session":
                return {"ok": True, "primaryTabId": 1}
            return {"ok": True}

    mock_hub = MockHub()
    monkeypatch.setattr(agentLoop, "real_hub", mock_hub)

    chat = db.create_chat("ZR review chat")
    chat_id = chat["id"]
    # Unique per invocation: the pytest DB (backend/data/test_privagent.db)
    # is shared across tests and processes, so a fixed task id would see
    # a checkpoint left by an earlier run.
    import uuid

    task_id = f"zr-review-task-{uuid.uuid4().hex[:12]}"
    record = TaskRecord(task_id=task_id, instruction="Go to example.com and click the button",
                        chat_id=chat_id)
    mock_hub.tasks[task_id] = record

    async def fake_loop(*args, **kwargs):
        return {
            "final": "completed",
            "steps": 1,
            "summary": f"final url: https://example.com/ | last evidence: clicked the button showing {TOKEN}",
            "history": [{"action": {"type": "click"}, "result_ok": True,
                         "status": "verified",
                         "evidence": f"clicked the button showing {TOKEN}"}],
            "url": "https://example.com/",
            "working_memory": {},
            "last_obs": None,
        }

    monkeypatch.setattr(agentLoop, "run_with_injection", fake_loop)
    await agentLoop.run_task(task_id)
    session_manager.clear()
    return record, chat_id, task_id


@pytest.mark.asyncio
async def test_zero_retention_run_task_persists_no_page_text(monkeypatch):
    record, chat_id, task_id = await _run_task_with_stubbed_loop(monkeypatch, True)
    # No checkpoint exists, so there is nothing to resume from.
    assert db.get_task_checkpoint(task_id) is None
    # No agent chat message and no derived mission fact may carry the
    # planted page token. (The operator's own user message persists —
    # it is the operator's own words, not page text — and it carries
    # no token here.)
    assert TOKEN not in str(db.get_messages(chat_id))
    assert TOKEN not in str(db.get_chat_memory(chat_id))
    # Memory write-back produces no proposals either: there is no
    # checkpoint to hold them, so nothing from this task is learned.
    from app.long_memory import get_proposals

    assert get_proposals(task_id) == []


@pytest.mark.asyncio
async def test_zero_retention_off_run_task_persists_summary_and_facts(monkeypatch):
    # Control: with the flag off, the same run DOES persist the agent
    # summary and the extracted facts — proving the test above can
    # detect the leak.
    record, chat_id, task_id = await _run_task_with_stubbed_loop(monkeypatch, False)
    assert db.get_task_checkpoint(task_id) is not None
    assert TOKEN in str(db.get_messages(chat_id))
    assert TOKEN in str(db.get_chat_memory(chat_id))


def test_confirmation_still_works_under_zero_retention(monkeypatch):
    """Pruning must not break the confirmation gate: a zero-retention
    task that needs approval mid-run still halts, still executes on
    approve, and still leaves no evidence behind."""
    monkeypatch.setattr("app.config.ZERO_RETENTION", True)
    monkeypatch.setattr("app.config.ZERO_RETENTION_HISTORY_KEEP", 3)

    class ConfirmHub:
        def __init__(self):
            self.executed: list[str] = []
            self.confirm_requests: list = []
            self.decision = None
            self.confirm_event = asyncio.Event()

        async def send_action(self, task_id, action, timeout_s=45.0):
            await asyncio.sleep(0.01)
            self.executed.append(action["type"])
            return {"ok": True, "evidence": f"did it, saw {TOKEN}"}

    hub = ConfirmHub()
    calls = {"n": 0}

    class Planner:
        def __call__(self, i, p, h):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"goal": "g", "steps": [{"why": "w", "action": {
                    "type": "type", "risk": "write", "verify": "v",
                    "timeoutMs": 100,
                    "target": {"targetId": "q"}, "params": {"text": "hi"}}}]}
            return {"goal": "g", "steps": []}

    async def scenario():
        async def approve_soon():
            await asyncio.sleep(0.2)
            hub.decision = True
            hub.confirm_event.set()

        state, _ = await asyncio.gather(
            agentLoop.run_with_injection(
                hub, planner=Planner(),
                obs_factory=lambda: {"tabId": 1, "url": "u", "title": "t",
                                     "interactive": [], "scroll": {}, "dpr": 1,
                                     "page_text": f"welcome, your code is {TOKEN}"},
                confirm_gate=hub,
            ),
            approve_soon(),
        )
        return state

    state = asyncio.run(scenario())
    assert state["final"] == "completed"
    assert hub.confirm_requests and hub.confirm_requests[0]["action"]["type"] == "type"
    assert "type" in hub.executed
    assert TOKEN not in str(state["history"])


def test_memory_store_unaffected_by_zero_retention(monkeypatch, tmp_path):
    """The Task 9 store itself is untouched by the mode: a fact written
    while zero retention is on still lands and is still recalled. (What
    does NOT happen is write-back: a zero-retention task produces no
    memory proposals — see test_zero_retention_run_task_persists_no_page_text.)"""
    from app.long_memory import create_fact, recall_for_prompt

    monkeypatch.setattr("app.config.ZERO_RETENTION", True)
    db_path = str(tmp_path / "zr-mem.db")
    create_fact("prefers dark mode", category="personal", source="user",
                db_path=db_path)
    recalled = recall_for_prompt("set dark mode", db_path=db_path)
    assert any("dark mode" in f.get("text", "") for f in recalled.get("facts", []))
    # And a proposal saved with no checkpoint behind it stores nothing.
    from app.long_memory import get_proposals, save_proposals

    save_proposals("no-such-task", [{"kind": "fact", "text": "x"}],
                   db_path=db_path)
    assert get_proposals("no-such-task", db_path=db_path) == []
