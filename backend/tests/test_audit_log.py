"""Task 10 — Append-only, hash-chained audit log.

Covers: chain verifies after N events; tampering with one line is
detected at the right index; deleting a line breaks the chain at the
line after the deletion; summaries are scrubbed (a planted secret
never lands in the file); file/dir permissions; the GET /api/audit
endpoint (auth required, filters work).
"""
from __future__ import annotations

import json
import os
import stat

import pytest

from app import audit


@pytest.fixture
def log_path(tmp_path, monkeypatch):
    path = tmp_path / "audit" / "audit.log"
    monkeypatch.setattr("app.config.AUDIT_LOG_PATH", path)
    return path


def _append_many(path, n=5):
    for i in range(n):
        audit.append_event(f"event_{i}", f"test event {i}", task_id=f"task-{i}", log_path=path)
    return path


def test_chain_verifies_after_many_events(log_path):
    _append_many(log_path, 7)
    ok, broken = audit.verify_chain(log_path)
    assert ok is True and broken is None
    entries = audit.read_events(log_path)
    assert len(entries) == 7
    assert [e["event"] for e in entries] == [f"event_{i}" for i in range(7)]
    # Each entry links to the previous hash; the first links to genesis.
    assert entries[0]["prev"] == "0" * 64
    for prev, cur in zip(entries, entries[1:]):
        assert cur["prev"] == prev["hash"]


def test_tampering_detected_at_the_right_index(log_path):
    _append_many(log_path, 5)
    lines = log_path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[2])
    entry["summary"] = "forged summary"
    lines[2] = json.dumps(entry, sort_keys=True)
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ok, broken = audit.verify_chain(log_path)
    assert ok is False and broken == 2


def test_tampering_first_line_detected_at_index_zero(log_path):
    _append_many(log_path, 5)
    lines = log_path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[0])
    entry["summary"] = "forged genesis"
    lines[0] = json.dumps(entry, sort_keys=True)
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ok, broken = audit.verify_chain(log_path)
    assert ok is False and broken == 0


def test_truncation_of_tail_is_not_detectable(log_path):
    # REVIEW (Task 10 audit): honest limit of a backward-only hash
    # chain — deleting the final entries leaves a shorter but still
    # valid chain. The docstring used to claim "any deletion is
    # detectable"; this test pins the real behaviour so nobody claims
    # otherwise.
    _append_many(log_path, 5)
    lines = log_path.read_text(encoding="utf-8").splitlines()
    log_path.write_text("\n".join(lines[:3]) + "\n", encoding="utf-8")
    assert audit.verify_chain(log_path) == (True, None)
    assert len(audit.read_events(log_path)) == 3


def test_deleting_a_line_breaks_the_chain_after_it(log_path):
    _append_many(log_path, 5)
    lines = log_path.read_text(encoding="utf-8").splitlines()
    del lines[1]
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ok, broken = audit.verify_chain(log_path)
    assert ok is False and broken == 1  # entry 2's prev no longer matches


def test_reordering_breaks_the_chain(log_path):
    _append_many(log_path, 4)
    lines = log_path.read_text(encoding="utf-8").splitlines()
    lines[1], lines[2] = lines[2], lines[1]
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ok, broken = audit.verify_chain(log_path)
    assert ok is False and broken == 1


def test_empty_and_missing_log_verify(log_path):
    assert audit.verify_chain(log_path) == (True, None)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("", encoding="utf-8")
    assert audit.verify_chain(log_path) == (True, None)
    assert audit.read_events(log_path) == []


def test_planted_secrets_never_land_in_the_file(log_path):
    # A bare password, an AWS key, a URL token, and an api-key value:
    # none of the secret VALUES may appear in the file afterwards.
    audit.append_event("confirmation_denied",
                       "operator denied; note said password hunter2hunter2",
                       log_path=log_path)
    audit.append_event("auth_failure",
                       "bad key AKIAIOSFODNN7EXAMPLE presented",
                       log_path=log_path)
    audit.append_event("allowlist_denied",
                       "redirect to https://evil.example/x?token=SECRETVALUE123",
                       log_path=log_path)
    audit.append_event("memory_write",
                       "fact api_key=sk-live-abcdef0123456789 stored",
                       log_path=log_path)
    raw = log_path.read_text(encoding="utf-8")
    assert "hunter2hunter2" not in raw
    assert "AKIAIOSFODNN7EXAMPLE" not in raw
    assert "SECRETVALUE123" not in raw
    assert "sk-live-abcdef0123456789" not in raw
    # ...but the event types and the scrubbed labels are still there.
    assert "[REDACTED]" in raw
    ok, broken = audit.verify_chain(log_path)
    assert ok is True and broken is None


def test_summary_is_truncated(log_path):
    audit.append_event("confirmation_approved", "x" * 5000, log_path=log_path)
    entry = audit.read_events(log_path)[0]
    assert len(entry["summary"]) <= 500


# Every event type the docs claim is recorded (see docs/PRIVACY.md).
ALL_EVENT_TYPES = [
    "auth_failure", "allowlist_denied",
    "confirmation_approved", "confirmation_denied",
    "takeover_started", "takeover_ended",
    "payment_attempt",
    "memory_write", "memory_delete", "memory_forget_all",
    "scheduler_claim",
]


def test_no_event_type_leaks_credential_values(log_path):
    # Plant the same bare-credential values in every event type's
    # summary; none of the secret VALUES may land in the file.
    for event in ALL_EVENT_TYPES:
        audit.append_event(
            event,
            "operator note said password hunter2hunter2 and api_key=AKIAIOSFODNN7EXAMPLE",
            log_path=log_path)
    raw = log_path.read_text(encoding="utf-8")
    assert "hunter2hunter2" not in raw
    assert "AKIAIOSFODNN7EXAMPLE" not in raw
    assert "[REDACTED]" in raw
    assert len(audit.read_events(log_path)) == len(ALL_EVENT_TYPES)
    assert audit.verify_chain(log_path) == (True, None)


def test_memory_write_events_cover_file_save_and_skill_update(authed_client, log_path, tmp_path, monkeypatch):
    # REVIEW (Task 10 audit): POST /long/files and PATCH /long/skills
    # are memory writes but emitted no audit event (only their
    # deletions did). Every content-changing memory endpoint must
    # leave a memory_write event.
    # Isolated DB: the shared pytest DB (backend/data/test_privagent.db)
    # persists across processes while the Fernet master key is
    # per-process, so rows written by an earlier process are
    # undecryptable here and would fail list_skills during update.
    import base64

    monkeypatch.setenv("PRIVAGENT_DB_PATH", str(tmp_path / "audit-wiring.db"))

    res = authed_client.post("/api/memory/long/files", json={
        "fileName": "note.txt",
        "base64": base64.b64encode(b"hello").decode(),
        "summary": "a note", "taskId": ""})
    assert res.status_code == 201, res.text
    res = authed_client.post("/api/memory/long/skills", json={
        "name": "weekly report", "steps": "do the report", "domain": "general",
        "source": "user"})
    assert res.status_code == 201, res.text
    skill_id = res.json()["id"]
    res = authed_client.patch(f"/api/memory/long/skills/{skill_id}",
                              json={"steps": "do the report, then file it"})
    assert res.status_code == 200, res.text
    res = authed_client.post(f"/api/memory/long/skills/{skill_id}/success")
    assert res.status_code == 200, res.text
    writes = [e["summary"] for e in audit.read_events(log_path, event="memory_write")]
    assert any("reference file saved" in s for s in writes), writes
    assert any("skill updated" in s for s in writes), writes
    assert any("skill success recorded" in s for s in writes), writes


def test_log_file_and_dir_permissions(log_path):
    audit.append_event("confirmation_approved", "ok", log_path=log_path)
    assert stat.S_IMODE(os.stat(log_path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(log_path.parent).st_mode) == 0o700


def test_append_never_raises_on_unwritable_path(tmp_path):
    # A log path under a file (not a directory) cannot be created;
    # append_event must return None, not raise.
    bad = tmp_path / "not-a-dir"
    bad.write_text("x", encoding="utf-8")
    assert audit.append_event("confirmation_approved", "ok", log_path=bad / "audit.log") is None


def test_read_events_filters_and_limits(log_path):
    for i in range(3):
        audit.append_event("confirmation_approved", f"ok {i}", log_path=log_path)
    for i in range(2):
        audit.append_event("auth_failure", f"bad {i}", log_path=log_path)
    assert len(audit.read_events(log_path)) == 5
    assert len(audit.read_events(log_path, event="auth_failure")) == 2
    assert len(audit.read_events(log_path, limit=2)) == 2
    assert audit.read_events(log_path, limit=2)[-1]["event"] == "auth_failure"


def test_audit_endpoint_returns_entries_and_chain_health(authed_client, log_path):
    _append_many(log_path, 3)
    resp = authed_client.get("/api/audit")
    assert resp.status_code == 200
    body = resp.json()
    assert body["chainOk"] is True and body["brokenIndex"] is None
    assert len(body["entries"]) == 3
    resp = authed_client.get("/api/audit", params={"event": "event_1"})
    assert [e["event"] for e in resp.json()["entries"]] == ["event_1"]
    resp = authed_client.get("/api/audit", params={"limit": 2})
    assert len(resp.json()["entries"]) == 2


def test_audit_endpoint_reports_broken_chain(authed_client, log_path):
    _append_many(log_path, 3)
    lines = log_path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[1])
    entry["summary"] = "tampered"
    lines[1] = json.dumps(entry, sort_keys=True)
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    body = authed_client.get("/api/audit").json()
    assert body["chainOk"] is False and body["brokenIndex"] == 1


def test_audit_endpoint_requires_auth(disable_auto_auth, authed_client):
    resp = authed_client.get("/api/audit")
    assert resp.status_code == 401


def test_auth_failure_is_audited(authed_client, disable_auto_auth, log_path):
    # An unauthenticated request to a protected route appends an
    # auth_failure event (with no token value recorded).
    resp = authed_client.get("/api/task/nonexistent")
    assert resp.status_code == 401
    entries = audit.read_events(log_path, event="auth_failure")
    assert len(entries) >= 1
    # The path is recorded (forensics: which route was probed), but no
    # credential value ever is.
    assert any("/api/task/nonexistent" in e["summary"] for e in entries)
    raw = log_path.read_text(encoding="utf-8")
    assert "Bearer" not in raw
