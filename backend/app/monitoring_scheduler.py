"""Bounded, non-blocking persistent mission scheduler (UTC cron / interval)."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
import logging
import time
import uuid

from . import db
from .hub import hub

logger = logging.getLogger(__name__)

# A watch is not killed by one bad tick. A transient failure (DNS,
# connect timeout, a site hiccup) keeps the mission active so it
# retries on its normal cadence; only a permanent refusal, or this
# many transient failures in a row, ends it — and the end is recorded
# as a monitoring event so the user can see the watch died, instead
# of it vanishing silently (claim_due only ever selects 'active').
MAX_CONSECUTIVE_MONITOR_FAILURES = 5


def _is_transient_refusal(reason: str | None) -> bool:
    """Only a DNS-resolution refusal is transient; every other URL
    refusal (malformed URL, disallowed scheme, private/internal
    host) is a stable fact about the URL."""
    return bool(reason) and "does not resolve" in reason


def _record_monitor_failure(mission_id: str, reason: str, *, permanent: bool = False) -> tuple[str, dict | None]:
    """Record one failed check tick. Returns (status, death_event).

    Transient: count rises, mission stays 'active' and retries next
    tick. Permanent, or MAX_CONSECUTIVE_MONITOR_FAILURES transient
    failures in a row: status becomes 'failed' and a monitoring
    event is recorded describing why the watch stopped."""
    now = time.time()
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT consecutive_failures FROM monitoring_missions WHERE id=?", (mission_id,)
        ).fetchone()
        prior = int(row["consecutive_failures"] or 0) if row else 0
        count = 0 if permanent else prior + 1
        if permanent or count >= MAX_CONSECUTIVE_MONITOR_FAILURES:
            conn.execute(
                "UPDATE monitoring_missions SET status='failed', last_run_at=?, consecutive_failures=? WHERE id=?",
                (now, count, mission_id),
            )
            event = {
                "id": str(uuid.uuid4()), "mission_id": mission_id, "task_id": None,
                "detected_at": now, "title": "Monitoring stopped",
                "summary": f"Watch stopped after {count} consecutive failed check(s): {reason}",
                "diff_data": {"reason": reason}, "state_hash": "",
            }
            conn.execute(
                """INSERT INTO monitoring_events (id, mission_id, detected_at, title, summary, diff_data, state_hash)
                   VALUES (?,?,?,?,?,?,?)""",
                (event["id"], mission_id, now, event["title"], event["summary"],
                 json.dumps(event["diff_data"]), ""),
            )
            return "failed", event
        conn.execute(
            "UPDATE monitoring_missions SET last_run_at=?, consecutive_failures=? WHERE id=?",
            (now, count, mission_id),
        )
        return "active", None


def _record_monitor_success(mission_id: str) -> None:
    """A clean check resets the consecutive-failure count."""
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE monitoring_missions SET consecutive_failures=0 WHERE id=?", (mission_id,)
        )


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

        # Also claim due email intelligence missions (avoid duplicate claims if mirrored in monitoring_missions)
        rem_limit = max(0, limit - len(claims))
        claimed_ids = {c["id"] for c in claims}
        if rem_limit > 0:
            try:
                email_rows = conn.execute("""SELECT * FROM email_monitoring_missions
                    WHERE status='active' AND next_run_at IS NOT NULL AND next_run_at<=?
                    ORDER BY next_run_at ASC LIMIT ?""", (current, rem_limit)).fetchall()
                for erow in email_rows:
                    ed = dict(erow)
                    if ed["id"] in claimed_ids:
                        continue
                    freq = ed.get("frequency_seconds") or 300
                    next_at = current + freq
                    conn.execute("UPDATE email_monitoring_missions SET next_run_at=? WHERE id=?", (next_at, ed["id"]))
                    claims.append({
                        "id": ed["id"],
                        "target_url": f"email://{ed.get('account_email') or 'inbox'}",
                        "condition_type": "email_intelligence",
                        "conversation_id": ed.get("conversation_id"),
                        "frequency_seconds": freq,
                        # The tick resolves the mission owner's
                        # connection from these — never the globals.
                        "user_id": ed.get("user_id"),
                        "connection_id": ed.get("connection_id"),
                    })
            except Exception as exc:
                logger.error("Failed to claim email monitoring missions: %s", exc)
    return claims


async def execute_monitor(mission: dict) -> None:
    from .agentLoop import run_task
    from .change_detector import evaluate_observation, broadcast_event
    target_url = mission["target_url"]

    if target_url.startswith("email://") or mission.get("condition_type") == "email_intelligence":
        from .email_intelligence import run_email_mission_tick
        try:
            await run_email_mission_tick(mission["id"])
        except Exception:
            with db.get_connection() as conn:
                conn.execute("UPDATE email_monitoring_missions SET last_sync_at=? WHERE id=?", (time.time(), mission["id"]))
        return

    # Task 2 live monitoring: a plain public http(s) watch does not
    # need a live Chrome extension. When no extension is connected,
    # check the page over HTTP instead of failing with
    # EXTENSION_DISCONNECTED; when one IS connected, keep the
    # existing browser path (it sees rendered/JS content the HTTP
    # fetch cannot). Either way the check is read-only.
    if target_url.startswith(("http://", "https://")) and hub.extension_ws is None:
        from .monitor_check import _validate_monitor_url, check_monitor_http

        ok, reason = _validate_monitor_url(target_url)
        if not ok:
            logger.warning("monitor %s: HTTP check refused (%s)", mission["id"], reason)
            # A DNS refusal is transient — the watch survives and
            # retries next tick. A structural refusal (private host,
            # bad scheme) ends it, with a recorded event.
            _status, death_event = _record_monitor_failure(
                mission["id"], reason or "URL refused",
                permanent=not _is_transient_refusal(reason),
            )
            if death_event:
                await broadcast_event(death_event)
            return
        try:
            event = await asyncio.to_thread(check_monitor_http, mission["id"], target_url)
            _record_monitor_success(mission["id"])
            if event:
                await broadcast_event(event)
        except Exception as exc:
            logger.warning("monitor %s: HTTP check failed: %s", mission["id"], exc)
            _status, death_event = _record_monitor_failure(mission["id"], str(exc))
            if death_event:
                await broadcast_event(death_event)
        return

    record = hub.new_task(f"Open {target_url} and observe its public visible page text and price. Report only observed facts.",
                          chat_id=mission["conversation_id"], user_id=mission.get("user_id"))
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
        _record_monitor_success(mission["id"])
        event = evaluate_observation(mission["id"], {"text": text, "elements": names}, task_id=record.task_id)
        if event:
            await broadcast_event(event)
    except Exception as exc:
        _status, death_event = _record_monitor_failure(mission["id"], str(exc))
        if death_event:
            await broadcast_event(death_event)


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
