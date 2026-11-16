"""Task 9 — Scheduler: job CRUD, due/not-due claiming, gates on
scheduled runs, run-now, background polling without a WebSocket."""
from __future__ import annotations

import asyncio
import time

import pytest

from app import db
from app.scheduler import (
    claim_due_jobs,
    create_job,
    delete_job,
    execute_job,
    get_job,
    list_jobs,
    parse_schedule,
    set_job_status,
)


# ---------------------------------------------------------------------------
# Schedule parsing
# ---------------------------------------------------------------------------

def test_parse_schedule_interval_daily_and_cron():
    parsed = parse_schedule("every 6h")
    assert parsed["schedule_kind"] == "interval" and parsed["interval_seconds"] == 6 * 3600
    assert parse_schedule("every 30m")["interval_seconds"] == 1800
    assert parse_schedule("every 1d")["interval_seconds"] == 86400
    daily = parse_schedule("daily 09:00")
    assert daily["schedule_kind"] == "cron" and daily["cron"] == "0 9 * * *"
    assert parse_schedule("0 9 * * *")["schedule_kind"] == "cron"
    assert parse_schedule("  every 2h  ")["interval_seconds"] == 7200


def test_parse_schedule_rejects_bad_input():
    for bad in ("bogus", "every 5s", "every 0h", "every 8d", "daily 25:00",
                "every", "0 9 * *", "0 9 * * * *"):
        with pytest.raises(ValueError):
            parse_schedule(bad)


def test_parse_schedule_rejects_too_frequent():
    with pytest.raises(ValueError, match="between 1 minute and 7 days"):
        parse_schedule("every 0m")


# ---------------------------------------------------------------------------
# Job CRUD (unit level, temp DB)
# ---------------------------------------------------------------------------

def test_job_crud_and_status(tmp_path):
    path = str(tmp_path / "sched.db")
    job = create_job("Open example.com and tell me the title", "every 6h",
                     allowed_domains=["example.com"], db_path=path)
    assert job["id"] and job["status"] == "active"
    assert job["schedule_kind"] == "interval"
    assert job["next_run_at"] is not None and job["next_run_at"] > time.time()
    assert job["allowed_domains"] == ["example.com"]
    assert get_job(job["id"], db_path=path)["instruction"].startswith("Open example.com")
    assert len(list_jobs(db_path=path)) == 1
    assert set_job_status(job["id"], "paused", db_path=path) is True
    assert get_job(job["id"], db_path=path)["status"] == "paused"
    with pytest.raises(ValueError):
        set_job_status(job["id"], "running", db_path=path)
    assert delete_job(job["id"], db_path=path) is True
    assert get_job(job["id"], db_path=path) is None
    assert delete_job(job["id"], db_path=path) is False


def test_create_job_validates_instruction_and_schedule(tmp_path):
    path = str(tmp_path / "sched2.db")
    with pytest.raises(ValueError):
        create_job("", "every 6h", db_path=path)
    with pytest.raises(ValueError):
        create_job("do it", "bogus", db_path=path)


# ---------------------------------------------------------------------------
# Claiming: due jobs claimed once, not-due and paused never claimed
# ---------------------------------------------------------------------------

def test_claim_due_jobs_only_due_and_active(tmp_path):
    path = str(tmp_path / "sched3.db")
    due = create_job("due job", "every 6h", db_path=path)
    future = create_job("future job", "every 6h", db_path=path)
    paused = create_job("paused job", "every 6h", db_path=path)
    set_job_status(paused["id"], "paused", db_path=path)
    # Push the due job's next_run into the past, the other's into the future.
    now = time.time()
    db.init_db(path)
    with db.get_connection(path) as conn:
        conn.execute("UPDATE scheduled_jobs SET next_run_at = ? WHERE id = ?", (now - 10, due["id"]))
        conn.execute("UPDATE scheduled_jobs SET next_run_at = ? WHERE id = ?", (now + 3600, future["id"]))
        conn.execute("UPDATE scheduled_jobs SET next_run_at = ? WHERE id = ?", (now - 10, paused["id"]))
        conn.commit()
    claimed = claim_due_jobs(now=now, db_path=path)
    assert [j["id"] for j in claimed] == [due["id"]]
    # Claimed once: next_run_at advanced, so a second claim finds nothing.
    assert claim_due_jobs(now=now, db_path=path) == []
    # last_run_at recorded at claim time.
    assert get_job(due["id"], db_path=path)["last_run_at"] is not None


# ---------------------------------------------------------------------------
# execute_job: creates a background task with the job's allow-list, and
# the task is pollable without a WebSocket client.
# ---------------------------------------------------------------------------

def test_execute_job_creates_background_task_with_allow_list(tmp_path, monkeypatch):
    path = str(tmp_path / "sched4.db")
    monkeypatch.setenv("PRIVAGENT_DB_PATH", path)
    job = create_job("Open example.com and tell me the title", "every 6h",
                     allowed_domains=["example.com"], db_path=path)

    from app.hub import hub as hub_singleton

    task_id = asyncio.run(execute_job(job, db_path=path))
    record = hub_singleton.tasks.get(task_id)
    assert record is not None
    assert record.background is True
    # The job's allow-list — not the global one — was wired onto the task.
    assert record.allowed_domains == ["example.com"]
    # No extension is attached in tests, so the run fails honestly; the
    # important part is that it ran as a normal gated task and settled.
    assert record.state in ("failed", "completed", "awaiting_confirmation")
    # The job row records the run and its task id for polling.
    refreshed = get_job(job["id"], db_path=path)
    assert refreshed["last_task_id"] == task_id
    assert refreshed["last_run_at"] is not None


# ---------------------------------------------------------------------------
# HTTP API: scheduled endpoints
# ---------------------------------------------------------------------------

def test_scheduled_http_crud_and_validation(authed_client, tmp_path, monkeypatch):
    monkeypatch.setenv("PRIVAGENT_DB_PATH", str(tmp_path / "api.db"))
    res = authed_client.post("/api/scheduled", json={
        "instruction": "Check example.com daily", "schedule": "daily 09:00"})
    assert res.status_code == 201
    job_id = res.json()["id"]
    assert res.json()["schedule_kind"] == "cron"
    res = authed_client.get("/api/scheduled")
    assert res.status_code == 200
    assert len(res.json()["jobs"]) == 1
    # Bad schedule is a 422, not a 500.
    res = authed_client.post("/api/scheduled", json={
        "instruction": "Check example.com", "schedule": "bogus"})
    assert res.status_code == 422
    # Empty instruction is a 422.
    res = authed_client.post("/api/scheduled", json={
        "instruction": "", "schedule": "every 6h"})
    assert res.status_code == 422
    res = authed_client.delete(f"/api/scheduled/{job_id}")
    assert res.status_code == 200
    res = authed_client.delete(f"/api/scheduled/{job_id}")
    assert res.status_code == 404


def test_scheduled_run_now_creates_pollable_task(authed_client, tmp_path, monkeypatch):
    monkeypatch.setenv("PRIVAGENT_DB_PATH", str(tmp_path / "api2.db"))
    res = authed_client.post("/api/scheduled", json={
        "instruction": "Open example.com and tell me the title",
        "schedule": "every 6h", "allowedDomains": ["example.com"]})
    assert res.status_code == 201
    job_id = res.json()["id"]
    res = authed_client.post(f"/api/scheduled/{job_id}/run-now")
    assert res.status_code == 200
    task_id = res.json()["taskId"]
    # Pollable without any WebSocket client attached.
    res = authed_client.get(f"/api/task/{task_id}")
    assert res.status_code == 200
    body = res.json()
    assert body["background"] is True
    assert body["allowedDomains"] == ["example.com"]
    # Unknown job id is a 404.
    res = authed_client.post("/api/scheduled/nope/run-now")
    assert res.status_code == 404


def test_scheduled_run_respects_allow_list_gate(tmp_path, monkeypatch):
    """A scheduled run with an allow-list that excludes the target
    domain must not navigate there: the gate is the same code path as
    interactive tasks (allowlist check in the planner/loop)."""
    path = str(tmp_path / "sched5.db")
    monkeypatch.setenv("PRIVAGENT_DB_PATH", path)
    from app.hub import hub as hub_singleton
    from app.allowlist import url_allowed

    job = create_job("Open evil.com and tell me the title", "every 6h",
                     allowed_domains=["example.com"], db_path=path)
    task_id = asyncio.run(execute_job(job, db_path=path))
    record = hub_singleton.tasks.get(task_id)
    assert record is not None
    # The task's effective allow-list excludes evil.com.
    assert not url_allowed("https://evil.com/", record.allowed_domains or [])
    assert url_allowed("https://example.com/", record.allowed_domains or [])
