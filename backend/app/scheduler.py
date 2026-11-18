"""Lightweight scheduled tasks (Task 9).

An in-process, SQLite-backed scheduler: a job is an instruction plus
a human schedule (``"every 6h"``, ``"every 30m"``, ``"daily 09:00"``
UTC, or a 5-field cron). A background runner thread claims due jobs
and creates real tasks from them.

Honest limits, stated in code and docs:

* The runner is in-process. A backend restart loses nothing (jobs and
  ``next_run_at`` live in SQLite) but a job due during downtime runs
  late on the next tick, and two backend processes would double-claim
  (``BEGIN IMMEDIATE`` only serialises within one process). A real
  deployment should use the OS scheduler instead — see
  docs/MEMORY_LONG_HORIZON.md.
* A scheduled run is a normal task: it goes through the SAME gates as
  an interactive task — domain allow-list, confirmation gate,
  takeover, payment limits. A scheduled job can never silently buy,
  log in, or exfiltrate; it halts for the operator like any task.
  Scheduled jobs therefore always run with ``background=True`` and
  the operator's configured allow-list.
* Under pytest the lifespan does not start the loop (tests drive
  ``claim_due_jobs`` / ``execute_job`` directly).
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import logging
import re
import time
import uuid
from typing import Any, Optional

from . import config, db
from . import secrets as secrets_store
from .hub import hub

logger = logging.getLogger("navigator.scheduler")

_EVERY_RE = re.compile(r"^every\s+(\d+)\s*([mhd])$", re.IGNORECASE)
_DAILY_RE = re.compile(r"^daily\s+(\d{1,2}):(\d{2})$", re.IGNORECASE)


def parse_schedule(schedule: str) -> dict[str, Any]:
    """Parse a human schedule into the stored form.

    Returns ``{"schedule_kind", "cron", "interval_seconds"}``.
    Raises ValueError on anything unsupported — a job that cannot be
    parsed must never be created silently."""
    raw = (schedule or "").strip()
    match = _EVERY_RE.fullmatch(raw)
    if match:
        amount, unit = int(match.group(1)), match.group(2).lower()
        seconds = amount * {"m": 60, "h": 3600, "d": 86400}[unit]
        if not 60 <= seconds <= 7 * 86400:
            raise ValueError("interval must be between 1 minute and 7 days")
        return {"schedule_kind": "interval", "cron": None, "interval_seconds": seconds}
    match = _DAILY_RE.fullmatch(raw)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour > 23 or minute > 59:
            raise ValueError("daily time must be HH:MM in 24-hour UTC")
        return {"schedule_kind": "cron", "cron": f"{minute} {hour} * * *", "interval_seconds": None}
    # A raw 5-field cron is accepted as-is (validated by next_due_at).
    if len(raw.split()) == 5:
        from .monitoring_scheduler import next_due_at

        next_due_at("cron", raw, 0, time.time())  # raises on invalid cron
        return {"schedule_kind": "cron", "cron": raw, "interval_seconds": None}
    raise ValueError(
        "unsupported schedule: use 'every 30m' / 'every 6h' / 'every 1d', "
        "'daily 09:00' (UTC), or a 5-field cron"
    )


def _next_run(parsed: dict[str, Any], now: float) -> float:
    from .monitoring_scheduler import next_due_at

    if parsed["schedule_kind"] == "interval":
        return now + int(parsed["interval_seconds"])
    return next_due_at("cron", parsed["cron"], 0, now)


def _row_to_job(row: Any) -> dict[str, Any]:
    d = dict(row)
    try:
        d["allowed_domains"] = json.loads(d.get("allowed_domains") or "[]")
    except (json.JSONDecodeError, TypeError):
        d["allowed_domains"] = []
    d["long_horizon"] = bool(d.get("long_horizon"))
    return d


def create_job(
    instruction: str,
    schedule: str,
    *,
    allowed_domains: Optional[list[str]] = None,
    long_horizon: bool = False,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    instruction = (instruction or "").strip()
    if not instruction or len(instruction) > config.MAX_INSTRUCTION_CHARS:
        raise ValueError("instruction must be 1-8000 characters")
    # REVIEW (Task 9 audit): a non-string entry (e.g. a number from a
    # hand-built JSON body) used to reach the DB and only fail later
    # inside effective_allowlist when the job ran — the job would then
    # never execute, silently. Reject it at creation.
    if allowed_domains is not None and (
        not isinstance(allowed_domains, list)
        or any(not isinstance(d, str) or not d.strip() for d in allowed_domains)
    ):
        raise ValueError("allowed_domains must be a list of non-empty domain strings")
    parsed = parse_schedule(schedule)
    now = time.time()
    job_id = uuid.uuid4().hex
    db.init_db(db_path)
    with db.get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO scheduled_jobs(id, instruction, schedule, schedule_kind, cron, interval_seconds,"
            " status, next_run_at, created_at, updated_at, allowed_domains, long_horizon)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                job_id, instruction, schedule.strip(), parsed["schedule_kind"], parsed["cron"],
                parsed["interval_seconds"], "active", _next_run(parsed, now), now, now,
                json.dumps(list(allowed_domains) if allowed_domains is not None else list(config.ALLOWED_DOMAINS)),
                1 if long_horizon else 0,
            ),
        )
        conn.commit()
    return get_job(job_id, db_path=db_path) or {"id": job_id}


def get_job(job_id: str, *, db_path: Optional[str] = None) -> Optional[dict[str, Any]]:
    db.init_db(db_path)
    with db.get_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM scheduled_jobs WHERE id = ?", (job_id,)).fetchone()
    return _row_to_job(row) if row else None


def list_jobs(*, db_path: Optional[str] = None) -> list[dict[str, Any]]:
    db.init_db(db_path)
    with db.get_connection(db_path) as conn:
        rows = conn.execute("SELECT * FROM scheduled_jobs ORDER BY created_at DESC").fetchall()
    return [_row_to_job(row) for row in rows]


def delete_job(job_id: str, *, db_path: Optional[str] = None) -> bool:
    db.init_db(db_path)
    with db.get_connection(db_path) as conn:
        return conn.execute("DELETE FROM scheduled_jobs WHERE id = ?", (job_id,)).rowcount > 0


def set_job_status(job_id: str, status: str, *, db_path: Optional[str] = None) -> bool:
    if status not in {"active", "paused"}:
        raise ValueError("status must be 'active' or 'paused'")
    db.init_db(db_path)
    with db.get_connection(db_path) as conn:
        cur = conn.execute(
            "UPDATE scheduled_jobs SET status = ?, updated_at = ? WHERE id = ?",
            (status, time.time(), job_id),
        )
        conn.commit()
        return cur.rowcount > 0


def claim_due_jobs(now: Optional[float] = None, limit: int = 5, *, db_path: Optional[str] = None) -> list[dict[str, Any]]:
    """Atomically claim due jobs and advance their next run. A claimed
    job is never claimed twice by this process; a job whose execution
    crashes keeps its advanced next_run_at (it runs again on schedule,
    not in a tight loop)."""
    current = time.time() if now is None else now
    db.init_db(db_path)
    claims: list[dict[str, Any]] = []
    with db.get_connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT * FROM scheduled_jobs WHERE status='active' AND next_run_at IS NOT NULL"
            " AND next_run_at <= ? ORDER BY next_run_at ASC LIMIT ?",
            (current, limit),
        ).fetchall()
        for row in rows:
            job = _row_to_job(row)
            parsed = {
                "schedule_kind": job["schedule_kind"],
                "cron": job.get("cron"),
                "interval_seconds": job.get("interval_seconds"),
            }
            conn.execute(
                "UPDATE scheduled_jobs SET next_run_at = ?, last_run_at = ? WHERE id = ?",
                (_next_run(parsed, current), current, job["id"]),
            )
            claims.append(job)
    # Task 10: audit — every claim (job id + schedule kind only; the
    # instruction text is never written to the audit log).
    if claims:
        try:
            from .audit import append_event

            for job in claims:
                append_event("scheduler_claim",
                             f"scheduled job {job['id']} claimed ({job['schedule_kind']})",
                             actor="system")
        except Exception:
            pass
    return claims


async def execute_job(job: dict[str, Any], *, db_path: Optional[str] = None) -> Optional[str]:
    """Run one scheduled job as a normal background task.

    The task is created with the job's allow-list (or the global one)
    and ``background=True``; it goes through run_task, which wires the
    confirmation gate, takeover gate, and payment limits exactly like
    an interactive task. A scheduled run that needs the operator halts
    in ``awaiting_confirmation`` / ``awaiting_takeover`` until the
    operator answers — it never self-approves."""
    task_id, task = start_job(job, db_path=db_path)
    try:
        await task
    except Exception as exc:  # noqa: BLE001 — a failed scheduled run must not kill the loop
        logger.warning("scheduled job %s failed: %s", job["id"], exc)
    # Record when this run settled. The run's outcome stays visible via
    # last_task_id -> GET /api/task/{id} (completed / failed /
    # awaiting_confirmation). The job row's `status` stays "active" /
    # "paused" — it is the job's enabled state, not the last outcome.
    db.init_db(db_path)
    with db.get_connection(db_path) as conn:
        conn.execute(
            "UPDATE scheduled_jobs SET last_run_at = ?, updated_at = ? WHERE id = ?",
            (time.time(), time.time(), job["id"]),
        )
        conn.commit()
    return task_id


# Fire-and-forget scheduled runs kept alive for the process lifetime.
_job_tasks: set[asyncio.Task] = set()


def start_job(job: dict[str, Any], *, db_path: Optional[str] = None) -> tuple[str, "asyncio.Task"]:
    """Create the task for a job and start run_task in the background.
    Returns (task_id, asyncio.Task). The scheduler loop awaits the
    task; the run-now endpoint returns the id immediately."""
    from .agentLoop import run_task
    from .allowlist import effective_allowlist

    instruction = job["instruction"]
    effective = effective_allowlist(job.get("allowed_domains") or None, config.ALLOWED_DOMAINS)
    record = hub.new_task(
        instruction,
        chat_id=None,
        allowed_domains=effective,
        long_horizon=bool(job.get("long_horizon")),
        background=True,
    )
    record.state = "planning"
    db.init_db(db_path)
    with db.get_connection(db_path) as conn:
        conn.execute(
            "UPDATE scheduled_jobs SET last_task_id = ?, updated_at = ? WHERE id = ?",
            (record.task_id, time.time(), job["id"]),
        )
        conn.commit()
    task = asyncio.create_task(run_task(record.task_id))
    _job_tasks.add(task)
    task.add_done_callback(_job_tasks.discard)
    return record.task_id, task


async def scheduler_loop(stop: asyncio.Event, tick_seconds: float | None = None) -> None:
    tick = tick_seconds if tick_seconds is not None else config.SCHEDULER_TICK_S
    running: set[asyncio.Task] = set()
    while not stop.is_set():
        try:
            for job in claim_due_jobs(limit=max(0, 5 - len(running))):
                task = asyncio.create_task(execute_job(job))
                running.add(task)
                task.add_done_callback(running.discard)
        except Exception as exc:  # noqa: BLE001 — the loop must survive a bad claim
            logger.warning("scheduler tick failed: %s", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=tick)
        except asyncio.TimeoutError:
            pass
    if running:
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)


@asynccontextmanager
async def scheduler_lifespan(_app):
    """Start the in-process scheduler. Skipped under pytest: tests
    drive claim_due_jobs / execute_job directly, and a background loop
    claiming jobs would make test runs non-deterministic."""
    if secrets_store.is_pytest():
        yield
        return
    stop = asyncio.Event()
    task = asyncio.create_task(scheduler_loop(stop))
    try:
        yield
    finally:
        stop.set()
        await task
