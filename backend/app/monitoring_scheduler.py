"""Bounded, non-blocking persistent mission scheduler (UTC cron / interval)."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import time

from . import db
from .hub import hub


def _cron_part(raw: str, low: int, high: int) -> set[int]:
    values: set[int] = set()
    for chunk in raw.split(","):
        base, slash, step_text = chunk.partition("/")
        step = int(step_text) if slash and step_text.isdigit() else 1
        if slash and not step_text.isdigit() or step < 1:
            raise ValueError("unsupported cron step")
        if base == "*":
            start, end = low, high
        elif "-" in base:
            pieces = base.split("-")
            if len(pieces) != 2 or not all(p.isdigit() for p in pieces):
                raise ValueError("invalid cron range")
            start, end = map(int, pieces)
        elif base.isdigit() and not slash:
            start = end = int(base)
        else:
            raise ValueError("invalid cron field")
        if start < low or end > high or start > end:
            raise ValueError("cron field out of range")
        values.update(range(start, end + 1, step))
    return values


def next_due_at(trigger: str, cron: str | None, interval: int, now: float) -> float | None:
    if trigger == "webhook":
        return None
    if trigger == "interval":
        if not 60 <= interval <= 604800:
            raise ValueError("interval must be between 60 seconds and 7 days")
        return now + interval
    if trigger != "cron" or not cron:
        raise ValueError("cron schedule requires five UTC fields")
    fields = cron.split()
    if len(fields) != 5:
        raise ValueError("cron schedule requires five UTC fields")
    minute, hour, day, month, weekday = (
        _cron_part(value, low, high) for value, low, high in zip(fields, (0, 0, 1, 1, 0), (59, 23, 31, 12, 7)))
    candidate = datetime.fromtimestamp(now, timezone.utc).replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(366 * 24 * 60):
        cron_weekday = (candidate.weekday() + 1) % 7
        if (candidate.minute in minute and candidate.hour in hour and candidate.day in day
                and candidate.month in month and (cron_weekday in weekday or cron_weekday == 0 and 7 in weekday)):
            return candidate.timestamp()
        candidate += timedelta(minutes=1)
    raise ValueError("cron schedule has no occurrence in the next year")


def claim_due(now: float | None = None, limit: int = 5, db_path: str | None = None) -> list[dict]:
    current = time.time() if now is None else now
    db.init_db(db_path)
    claims: list[dict] = []
    with db.get_connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute("""SELECT * FROM monitoring_missions
            WHERE status='active' AND next_run_at IS NOT NULL AND next_run_at<=?
            ORDER BY next_run_at ASC LIMIT ?""", (current, limit)).fetchall()
        for row in rows:
            next_at = next_due_at(row["trigger_type"], row["schedule_cron"], row["interval_seconds"], current)
            conn.execute("UPDATE monitoring_missions SET next_run_at=? WHERE id=?", (next_at, row["id"]))
            claims.append(dict(row))
    return claims


async def execute_monitor(mission: dict) -> None:
    from .agentLoop import run_task
    from .change_detector import evaluate_observation, broadcast_event
    target_url = mission["target_url"]
    record = hub.new_task(f"Open {target_url} and observe its public visible page text and price. Report only observed facts.",
                          chat_id=mission["conversation_id"])
    record.state = "planning"
    await hub.broadcast_progress(record)
    try:
        await run_task(record.task_id)
        observation = record.last_observation
        if record.state != "completed" or not observation:
            raise RuntimeError("monitor observation was not verified")
        from urllib.parse import urlsplit
        observed_url = observation.get("url", "") if isinstance(observation, dict) else getattr(observation, "url", "")
        if urlsplit(observed_url).hostname != urlsplit(target_url).hostname:
            raise RuntimeError("monitor observed the wrong domain")
        text = observation.get("pageText", "") if isinstance(observation, dict) else getattr(observation, "page_text", "")
        interactive = observation.get("interactive", []) if isinstance(observation, dict) else getattr(observation, "interactive", [])
        names = [str(el.get("name", "") if isinstance(el, dict) else getattr(el, "name", "")) for el in interactive]
        event = evaluate_observation(mission["id"], {"text": text, "elements": names}, task_id=record.task_id)
        if event:
            await broadcast_event(event)
    except Exception:
        with db.get_connection() as conn:
            conn.execute("UPDATE monitoring_missions SET status='failed',last_run_at=? WHERE id=?", (time.time(), mission["id"]))


async def scheduler_loop(stop: asyncio.Event, tick_seconds: float = 10.0) -> None:
    running: set[asyncio.Task] = set()
    while not stop.is_set():
        for mission in claim_due(limit=max(0, 5 - len(running))):
            task = asyncio.create_task(execute_monitor(mission))
            running.add(task)
            task.add_done_callback(running.discard)
        try:
            await asyncio.wait_for(stop.wait(), timeout=tick_seconds)
        except asyncio.TimeoutError:
            pass
    if running:
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)


@asynccontextmanager
async def monitoring_lifespan(_app):
    stop = asyncio.Event()
    task = asyncio.create_task(scheduler_loop(stop))
    try:
        yield
    finally:
        stop.set()
        await task
