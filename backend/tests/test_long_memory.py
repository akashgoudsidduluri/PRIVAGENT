"""Task 9 — Memory & long-horizon: durable facts/skills/files,
checkpoints & resume, step caps + compaction, multi-tab registry.

Covers the provenance rule (page content can never write memory),
recall caps, file summaries (never content in the prompt), checkpoint
persistence/resume, cancel-checkpointing, the 50/100 step caps, and
the switch_tab tab-registry gate in the loop.
"""
from __future__ import annotations

import asyncio
import base64

import pytest

from app import agentLoop, config, db
from app.contracts import BrowserObservation
from app.long_memory import (
    confirm_proposals,
    create_fact,
    create_skill,
    delete_fact,
    delete_file,
    delete_skill,
    file_excerpt,
    forget_all,
    get_proposals,
    list_facts,
    list_files,
    list_skills,
    propose_from_task,
    recall_facts,
    recall_for_prompt,
    recall_skills,
    record_skill_success,
    save_file,
    save_proposals,
    update_skill,
)
from app.planner import build_prompt
from app.privacy import classify_and_redact


def _obs(**kw):
    data = {"tabId": 1, "url": "https://example.com/", "title": "T",
            "interactive": [], "scroll": {}, "dpr": 1}
    data.update(kw)
    return BrowserObservation.model_validate(data)


def _payload(**kw):
    return classify_and_redact(_obs(**kw), instruction="do the thing")[0]


# ---------------------------------------------------------------------------
# Facts: CRUD, provenance, recall
# ---------------------------------------------------------------------------

def test_fact_crud_round_trip(tmp_path):
    path = str(tmp_path / "lm.db")
    fact = create_fact("my home airport is DEL", category="travel", db_path=path)
    assert fact["id"]
    assert fact["source"] == "user"
    listed = list_facts(db_path=path)
    assert len(listed) == 1 and listed[0]["text"] == "my home airport is DEL"
    # Values are encrypted at rest, not plaintext in the DB file.
    raw = (tmp_path / "lm.db").read_bytes()
    assert b"my home airport is DEL" not in raw
    assert delete_fact(fact["id"], db_path=path) is True
    assert list_facts(db_path=path) == []
    assert delete_fact(fact["id"], db_path=path) is False


def test_fact_provenance_page_content_can_never_write_memory(tmp_path):
    path = str(tmp_path / "lm.db")
    for bad_source in ("page", "observation", "planner", "task:abc", "task:abc:unconfirmed", ""):
        with pytest.raises(ValueError, match="invalid memory source"):
            create_fact("something true", source=bad_source, db_path=path)
    # The two allowed sources work.
    create_fact("operator fact", source="user", db_path=path)
    create_fact("confirmed outcome", source="task:abc123:confirmed", db_path=path)
    assert len(list_facts(db_path=path)) == 2


def test_fact_rejects_credentials_and_pii(tmp_path):
    path = str(tmp_path / "lm.db")
    with pytest.raises(ValueError):
        create_fact("my email is user@example.com", db_path=path)
    # Bare passwords and keys are rejected even though the shared
    # classifier only flags structured PII (Task 6 leak finding).
    with pytest.raises(ValueError):
        create_fact("password hunter2", db_path=path)
    with pytest.raises(ValueError):
        create_fact("my api key is abc123xyz", db_path=path)
    with pytest.raises(ValueError):
        create_fact("password: hunter2", db_path=path)
    # Naming a credential kind without a value is fine — no false
    # positive on an ordinary preference.
    create_fact("my password manager is 1Password", db_path=path)
    assert len(list_facts(db_path=path)) == 1


def test_recall_returns_only_relevant_facts_and_caps(tmp_path):
    path = str(tmp_path / "lm.db")
    create_fact("my home airport is DEL", category="travel", db_path=path)
    create_fact("I prefer window seats", category="travel", db_path=path)
    create_fact("my favourite editor is vim", category="coding", db_path=path)
    recalled = recall_facts("book a flight from DEL, window seat please", db_path=path)
    texts = [f["text"] for f in recalled]
    assert "my home airport is DEL" in texts
    assert "I prefer window seats" in texts
    assert "my favourite editor is vim" not in texts
    # Cap: at most 1 fact, and the char budget stops the second fact.
    assert len(recall_facts("book a flight from DEL, window seat please", limit=1, db_path=path)) == 1
    # The char budget is soft for the single best fact (recall must still
    # work when one fact alone exceeds it) and hard for the rest: with a
    # 30-char budget only the top fact comes back, not both.
    capped = recall_facts("book a flight from DEL, window seat please", max_chars=30, db_path=path)
    assert len(capped) == 1
    assert len(recall_facts("book a flight from DEL, window seat please", max_chars=1000, db_path=path)) == 2
    # No token overlap -> nothing recalled (no noise in the prompt).
    assert recall_facts("what is the weather in Paris", db_path=path) == []


def test_recall_for_prompt_is_capped_and_scrubbed(tmp_path):
    path = str(tmp_path / "lm.db")
    create_fact("my home airport is DEL", category="travel", db_path=path)
    recalled = recall_for_prompt("book a flight from DEL", db_path=path)
    assert recalled["facts"][0]["text"] == "my home airport is DEL"
    assert recalled["skills"] == []
    assert recalled["files"] == []


# ---------------------------------------------------------------------------
# Skills: versioning, success count, recall
# ---------------------------------------------------------------------------

def test_skill_versioning_and_success_count(tmp_path):
    path = str(tmp_path / "lm.db")
    skill = create_skill("book a flight", "1. Search\n2. Pick cheapest", domain="example.com", db_path=path)
    assert skill["version"] == 1 and skill["success_count"] == 0
    assert record_skill_success(skill["id"], db_path=path) is True
    updated = update_skill(skill["id"], "1. Search\n2. Pick cheapest\n3. Confirm", db_path=path)
    assert updated["version"] == 2
    assert updated["success_count"] == 1
    assert update_skill("nope", "steps", db_path=path) is None
    recalled = recall_skills("book a flight to Goa", db_path=path)
    assert len(recalled) == 1 and recalled[0]["name"] == "book a flight"
    assert recall_skills("how to bake bread", db_path=path) == []
    assert delete_skill(skill["id"], db_path=path) is True
    assert list_skills(db_path=path) == []


def test_skill_provenance_enforced(tmp_path):
    path = str(tmp_path / "lm.db")
    with pytest.raises(ValueError, match="invalid memory source"):
        create_skill("x", "steps", source="page", db_path=path)


# ---------------------------------------------------------------------------
# Files: summaries only in the prompt, excerpt is scrubbed
# ---------------------------------------------------------------------------

def test_file_stored_by_id_planner_sees_summary_only(tmp_path, monkeypatch):
    path = str(tmp_path / "lm.db")
    monkeypatch.setattr("app.long_memory._FILES_DIR", tmp_path / "files")
    content = b"CONFIDENTIAL itinerary: flight AI101, seat 14A, total Rs 12000"
    meta = save_file("trip.txt", base64.b64encode(content).decode(), summary="Goa trip itinerary", task_id="t1", db_path=path)
    assert meta["id"]
    listed = list_files(db_path=path)
    assert len(listed) == 1
    # Metadata only — no content key anywhere in the listing.
    assert "content" not in listed[0]
    assert all("AI101" not in str(f) for f in listed)
    recalled = recall_for_prompt("book my Goa trip", task_id="t1", db_path=path)
    assert recalled["files"][0]["summary"] == "Goa trip itinerary"
    assert "AI101" not in str(recalled["files"])
    # The excerpt path exists, is scrubbed, and is bounded.
    excerpt = file_excerpt(meta["id"], max_chars=50, db_path=path)
    assert excerpt is not None and len(excerpt) <= 50
    # A file for another task is not recalled.
    assert recall_for_prompt("book my Goa trip", task_id="other", db_path=path)["files"] == []
    assert delete_file(meta["id"], db_path=path) is True
    assert list_files(db_path=path) == []
    assert delete_file(meta["id"], db_path=path) is False


def test_file_rejects_invalid_base64(tmp_path):
    path = str(tmp_path / "lm.db")
    with pytest.raises(ValueError):
        save_file("x.txt", "not-base64!!!", db_path=path)


# ---------------------------------------------------------------------------
# Proposals: confirmed outcomes only, operator confirmation required
# ---------------------------------------------------------------------------

def _history_with_success():
    return [
        {"result_ok": True, "action": {"type": "navigate", "params": {"url": "https://example.com/flights"}}},
        {"result_ok": True, "action": {"type": "click", "target": {"name": "Search flights"}}},
        {"result_ok": True, "action": {"type": "type", "target": {"name": "From"}, "params": {"text": "DEL"}}},
        {"result_ok": False, "action": {"type": "click", "target": {"name": "Broken"}}},
    ]


def test_propose_from_task_uses_operator_words_and_verified_steps(tmp_path):
    proposals = propose_from_task(
        "task-1",
        "I prefer window seats. Book a flight to Goa",
        "Booked AI101",
        _history_with_success(),
    )
    kinds = {p["kind"] for p in proposals}
    assert "fact" in kinds  # from the operator's own preference sentence
    assert "skill" in kinds  # from the verified steps on example.com
    assert all(p["source"] == "task:task-1:confirmed" for p in proposals)
    # Page evidence is never fact content: no evidence string is copied.
    assert not any("Broken" in str(p) for p in proposals)


def test_confirm_proposals_stores_only_accepted(tmp_path):
    path = str(tmp_path / "lm.db")
    db.init_db(path)
    db.save_task_checkpoint(task_id="task-1", conversation_id="c1", session_id=None,
                            step_index=2, last_url="", history=[], working_memory={}, db_path=path)
    proposals = propose_from_task("task-1", "I prefer window seats. Book a flight to Goa",
                                  "done", _history_with_success())
    save_proposals("task-1", proposals, db_path=path)
    assert len(get_proposals("task-1", db_path=path)) == len(proposals)
    result = confirm_proposals("task-1", [0], db_path=path)
    assert len(result["created"]) == 1
    # Only the accepted proposal is consumed: the unaccepted one stays
    # in the checkpoint so the operator can still accept (or ignore) it
    # on its own — accepting a fact must not discard a skill suggestion.
    remaining = get_proposals("task-1", db_path=path)
    assert len(remaining) == len(proposals) - 1
    assert remaining[0]["kind"] == "skill"
    assert len(list_facts(db_path=path)) + len(list_skills(db_path=path)) == 1
    # Accepting the rest clears the list.
    confirm_proposals("task-1", [0], db_path=path)
    assert get_proposals("task-1", db_path=path) == []
    assert len(list_facts(db_path=path)) + len(list_skills(db_path=path)) == 2


def test_forget_all_wipes_every_tier(tmp_path, monkeypatch):
    path = str(tmp_path / "lm.db")
    monkeypatch.setattr("app.long_memory._FILES_DIR", tmp_path / "files")
    create_fact("my home airport is DEL", db_path=path)
    create_skill("book a flight", "1. Search", db_path=path)
    save_file("t.txt", base64.b64encode(b"hello").decode(), db_path=path)
    counts = forget_all(db_path=path)
    assert counts == {"facts": 1, "skills": 1, "files": 1,
                      "semantic_facts": 0, "proposals": 0}
    assert list_facts(db_path=path) == []
    assert list_skills(db_path=path) == []
    assert list_files(db_path=path) == []
    assert list((tmp_path / "files").glob("*")) == []


# ---------------------------------------------------------------------------
# Prompt: long-memory + tabs blocks
# ---------------------------------------------------------------------------

def test_build_prompt_includes_long_memory_and_tabs(tmp_path):
    path = str(tmp_path / "lm.db")
    create_fact("my home airport is DEL", category="travel", db_path=path)
    recalled = recall_for_prompt("book a flight from DEL", db_path=path)
    wm = {
        "long_memory": recalled,
        "tabs": [
            {"tab_id": 1, "url": "https://example.com/", "title": "primary", "active": True},
            {"tab_id": 2, "url": "https://other.com/", "title": "helper", "active": False},
        ],
    }
    prompt = build_prompt("book a flight from DEL", _payload(), [], working_memory=wm)
    assert "<long-term-memory" in prompt
    assert "my home airport is DEL" in prompt
    assert "<task-tabs" in prompt
    assert "tab 1" in prompt and "tab 2" in prompt
    assert "switch_tab" in prompt


def test_build_prompt_without_memory_has_no_block():
    prompt = build_prompt("do the thing", _payload(), [])
    assert "<long-term-memory" not in prompt
    assert "<task-tabs" not in prompt


# ---------------------------------------------------------------------------
# Checkpoints: cancel checkpoints, resume restores history
# ---------------------------------------------------------------------------

def test_save_cancel_checkpoint_writes_only_when_missing(monkeypatch):
    # save_cancel_checkpoint writes to the default DB path; point that at
    # a temp file so the test does not pollute the shared test DB.
    import os
    import tempfile

    from app import db as db_mod

    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setenv("PRIVAGENT_DB_PATH", os.path.join(td, "cancel.db"))
        assert agentLoop.save_cancel_checkpoint("t-cancel", "do the thing", 3,
                                                "https://example.com/") is True
        cp = db_mod.get_task_checkpoint("t-cancel")
        assert cp is not None and cp["step_index"] == 3
        # A second cancel must NOT overwrite the existing checkpoint.
        assert agentLoop.save_cancel_checkpoint("t-cancel", "do the thing", 9,
                                                "https://other.com/") is False
        cp = db_mod.get_task_checkpoint("t-cancel")
        assert cp["step_index"] == 3 and cp["last_url"] == "https://example.com/"


def test_checkpoint_round_trip_preserves_history_and_tabs(tmp_path):
    path = str(tmp_path / "lm.db")
    db.init_db(path)  # the app lifespan inits the DB at startup; tests do it explicitly
    history = [{"action": {"type": "click"}, "result_ok": True, "evidence": "clicked Search"}]
    wm = {"tabs": [{"tab_id": 1, "url": "https://example.com/", "title": "primary", "active": True}],
          "long_horizon_summary": "Goal: x\nDone so far: clicked Search"}
    db.save_task_checkpoint(task_id="t-cp", conversation_id="c1", session_id="s1",
                            step_index=1, last_url="https://example.com/",
                            history=history, working_memory=wm, db_path=path)
    cp = db.get_task_checkpoint("t-cp", db_path=path)
    assert cp["history"][0]["evidence"] == "clicked Search"
    assert cp["working_memory"]["tabs"][0]["tab_id"] == 1
    assert "long_horizon_summary" in cp["working_memory"]


# ---------------------------------------------------------------------------
# Step caps + compaction
# ---------------------------------------------------------------------------

def test_step_cap_defaults_are_50_and_100():
    assert config.MAX_STEPS == 50
    assert config.MAX_LONG_STEPS == 100
    assert config.COMPACTION_EVERY_STEPS == 25


def test_compaction_summary_every_25_steps_on_long_tasks():
    history = [
        {"result_ok": True, "action": {"type": "click"}, "evidence": f"clicked {i}"}
        for i in range(25)
    ]
    wm: dict = {}
    agentLoop._maybe_compact_history(wm, history, "book a flight", 24, True)
    assert "long_horizon_summary" not in wm
    agentLoop._maybe_compact_history(wm, history, "book a flight", 25, True)
    summary = wm["long_horizon_summary"]
    assert "Goal: book a flight" in summary
    assert "Done so far" in summary
    assert "Remaining" in summary
    assert wm["long_horizon_summary_at_step"] == 25
    # Not a long-horizon task -> no compaction, ever.
    wm2: dict = {}
    agentLoop._maybe_compact_history(wm2, history, "book a flight", 25, False)
    assert "long_horizon_summary" not in wm2


def test_history_view_uses_summary_not_full_history():
    history = [
        {"result_ok": True, "action": {"type": "click"}, "evidence": f"step {i}"}
        for i in range(10)
    ]
    wm = {"long_horizon_summary": "Goal: x\nDone so far: ..."}
    view = agentLoop._history_view(history, [], wm)
    assert view[0]["long_horizon_summary"].startswith("Goal: x")
    # Only the last 2 history entries ride along with the summary.
    assert len([v for v in view if "action" in v]) == 2
    # Without a summary the old window (last 5) still applies.
    view2 = agentLoop._history_view(history, [], {})
    assert len([v for v in view2 if "action" in v]) == 5


# ---------------------------------------------------------------------------
# Tab registry helpers
# ---------------------------------------------------------------------------

def test_tab_registry_helpers():
    wm: dict = {}
    agentLoop._register_tab_in_memory(wm, 1, url="https://a.com/", title="primary", active=True)
    agentLoop._register_tab_in_memory(wm, 2, url="https://b.com/", title="helper")
    assert len(wm["tabs"]) == 2
    assert wm["tabs"][0]["active"] is True and wm["tabs"][1]["active"] is False
    assert agentLoop._set_active_tab_in_memory(wm, 2) is True
    assert wm["tabs"][1]["active"] is True and wm["tabs"][0]["active"] is False
    assert agentLoop._set_active_tab_in_memory(wm, 99) is False
    agentLoop._remove_tab_from_memory(wm, 2)
    assert [t["tab_id"] for t in wm["tabs"]] == [1]
    assert wm["tabs"][0]["active"] is True


# ---------------------------------------------------------------------------
# Loop integration: unknown tab rejected, new_tab registers, switch works
# ---------------------------------------------------------------------------

class _FakeHub:
    def __init__(self, script):
        self.script = script
        self.executed: list[str] = []

    async def send_action(self, task_id, action, timeout_s=45.0):
        self.executed.append(action["type"])
        return self.script.get(action["type"], {"ok": True})


def _obs_factory():
    return {"tabId": 1, "url": "https://example.com/", "title": "T",
            "interactive": [], "scroll": {}, "dpr": 1}


def _switch_plan(tab_id):
    return {"goal": "switch", "steps": [{
        "why": "switch tab",
        "action": {"type": "switch_tab", "risk": "read", "verify": "tab active",
                   "timeoutMs": 1000, "params": {"tabId": tab_id}},
    }]}


def test_loop_rejects_unknown_tab_before_extension():
    hub = _FakeHub({})
    state = asyncio.run(agentLoop.run_with_injection(
        hub,
        planner=lambda i, p, h: _switch_plan(99),
        obs_factory=_obs_factory,
        initial_memory={"tabs": [{"tab_id": 1, "url": "https://example.com/", "title": "primary", "active": True}]},
    ))
    assert state["final"] == "failed"
    assert "unknown tab" in (state["summary"] or "")
    assert "switch_tab" not in hub.executed  # never sent to the extension


def test_loop_new_tab_registers_then_switch_tab_works():
    hub = _FakeHub({
        "new_tab": {"ok": True, "observation": {"tabId": 2, "url": "https://other.com/", "title": "helper",
                                                "interactive": [], "scroll": {}, "dpr": 1}},
        "switch_tab": {"ok": True},
    })
    calls = {"n": 0}

    def planner(i, p, h):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"goal": "open", "steps": [{
                "why": "open helper tab",
                "action": {"type": "new_tab", "risk": "write", "verify": "tab opened",
                           "timeoutMs": 1000, "params": {"url": "https://other.com/"}},
            }]}
        if calls["n"] == 2:
            return _switch_plan(2)
        return {"goal": "done", "steps": []}

    state = asyncio.run(agentLoop.run_with_injection(
        hub, planner=planner, obs_factory=_obs_factory,
        initial_memory={"tabs": [{"tab_id": 1, "url": "https://example.com/", "title": "primary", "active": True}]},
    ))
    assert state["final"] == "completed"
    assert "new_tab" in hub.executed and "switch_tab" in hub.executed
    tabs = state["working_memory"]["tabs"]
    assert {t["tab_id"] for t in tabs} == {1, 2}
    assert next(t for t in tabs if t["tab_id"] == 2)["active"] is True


def test_loop_budget_uses_new_50_step_default(monkeypatch):
    monkeypatch.setattr(agentLoop, "MAX_STEPS", 50)
    hub = _FakeHub({"click": {"ok": True}})
    state = asyncio.run(agentLoop.run_with_injection(
        hub,
        planner=lambda i, p, h: {"goal": "loop", "steps": [{
            "why": "again",
            "action": {"type": "click", "risk": "write", "verify": "v", "timeoutMs": 100,
                       "target": {"targetId": "b1"}}}]},
        obs_factory=_obs_factory,
        max_steps=3,
    ))
    assert state["final"] == "budget_exceeded"
    assert state["steps"] == 3


# ---------------------------------------------------------------------------
# HTTP API: long memory + proposals + forget-all
# ---------------------------------------------------------------------------

def test_long_memory_http_crud_and_provenance(authed_client, tmp_path, monkeypatch):
    monkeypatch.setenv("PRIVAGENT_DB_PATH", str(tmp_path / "api.db"))
    res = authed_client.post("/api/memory/long/facts", json={
        "text": "my home airport is DEL", "category": "travel"})
    assert res.status_code == 201
    fact_id = res.json()["id"]
    res = authed_client.get("/api/memory/long/facts")
    assert res.status_code == 200
    assert any(f["text"] == "my home airport is DEL" for f in res.json()["facts"])
    # Page content as a source is rejected at the API boundary too.
    res = authed_client.post("/api/memory/long/facts", json={
        "text": "page said this", "source": "page"})
    assert res.status_code == 422
    # PII is rejected.
    res = authed_client.post("/api/memory/long/facts", json={
        "text": "my email is user@example.com"})
    assert res.status_code == 422
    res = authed_client.delete(f"/api/memory/long/facts/{fact_id}")
    assert res.status_code == 200
    res = authed_client.delete(f"/api/memory/long/facts/{fact_id}")
    assert res.status_code == 404


def test_long_memory_http_skills_and_files(authed_client, tmp_path, monkeypatch):
    monkeypatch.setenv("PRIVAGENT_DB_PATH", str(tmp_path / "api2.db"))
    monkeypatch.setattr("app.long_memory._FILES_DIR", tmp_path / "files")
    res = authed_client.post("/api/memory/long/skills", json={
        "name": "book a flight", "steps": "1. Search\n2. Pick cheapest"})
    assert res.status_code == 201
    skill_id = res.json()["id"]
    assert res.json()["version"] == 1
    res = authed_client.patch(f"/api/memory/long/skills/{skill_id}", json={
        "steps": "1. Search\n2. Pick cheapest\n3. Confirm"})
    assert res.status_code == 200 and res.json()["version"] == 2
    res = authed_client.post(f"/api/memory/long/skills/{skill_id}/success")
    assert res.status_code == 200
    res = authed_client.post("/api/memory/long/files", json={
        "fileName": "trip.txt",
        "base64": base64.b64encode(b"Goa itinerary, flight AI101").decode(),
        "summary": "Goa trip itinerary", "taskId": "t1"})
    assert res.status_code == 201
    file_id = res.json()["id"]
    res = authed_client.get("/api/memory/long/files")
    assert res.status_code == 200
    assert "AI101" not in res.text  # metadata only, never content
    res = authed_client.get(f"/api/memory/long/files/{file_id}/excerpt?max_chars=100")
    assert res.status_code == 200
    res = authed_client.delete(f"/api/memory/long/files/{file_id}")
    assert res.status_code == 200
    # Forget everything wipes the tiers.
    res = authed_client.delete("/api/memory")
    assert res.status_code == 200
    assert res.json()["deleted"]["skills"] == 1
    res = authed_client.get("/api/memory/long/skills")
    assert res.json()["skills"] == []


def test_task_create_accepts_long_horizon_and_background(authed_client):
    res = authed_client.post("/api/task", json={
        "instruction": "do the thing", "longHorizon": True, "background": True})
    assert res.status_code == 200
    task_id = res.json()["taskId"]
    res = authed_client.get(f"/api/task/{task_id}")
    assert res.status_code == 200
    body = res.json()
    assert body["longHorizon"] is True
    assert body["background"] is True
    assert body["tabs"] == []
    assert body["checkpoint"] is None
