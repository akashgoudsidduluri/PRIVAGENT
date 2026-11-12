"""Atomic monitoring baselines, condition checks and deduplicated events."""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from typing import Any

from . import db
from .privacy import _redact_text

_PRICE = re.compile(r"(?:₹|\b(?:Rs\.?|INR|USD)\s*|\$)\s*([\d,]+(?:\.\d{1,2})?)", re.I)


def _state(observation: dict[str, Any]) -> dict[str, Any]:
    text = _redact_text(re.sub(r"\s+", " ", str(observation.get("text") or "")).strip(), 4000)
    names = [_redact_text(str(name), 120) for name in (observation.get("elements") or [])[:100]]
    match = _PRICE.search(text)
    price = float(match.group(1).replace(",", "")) if match else None
    return {"text": text, "elements": names, "price": price}


def _triggered(condition: str, params: dict, before: dict, after: dict) -> tuple[bool, str]:
    if condition == "price_drop":
        old, new = before.get("price"), after.get("price")
        threshold = float(params.get("min_percent", 0))
        if not 0 <= threshold <= 100:
            raise ValueError("price drop threshold must be 0-100%")
        if old is None or new is None or old <= 0:
            return False, "Price unavailable"
        return new < old and (old - new) * 100 / old >= threshold, f"Price changed from {old:g} to {new:g}"
    if condition == "regex":
        pattern = str(params.get("pattern") or "")
        # A narrow, useful regex subset avoids catastrophic backtracking in
        # the scheduler process. Grouping, backreferences and compound
        # quantifiers are deliberately not accepted.
        if (not pattern or len(pattern) > 120 or any(ch in pattern for ch in "(){}")
                or len(re.findall(r"(?<!\\)[*+?]", pattern)) > 1 or re.search(r"\\[1-9]", pattern)):
            raise ValueError("invalid or unsafe monitor regex")
        found = bool(re.search(pattern, after["text"], re.I))
        previously = bool(re.search(pattern, before["text"], re.I))
        return found and not previously, "Requested text pattern appeared"
    if condition == "element":
        name = str(params.get("name") or "").casefold()
        if not name or len(name) > 120:
            raise ValueError("element condition requires a short accessible name")
        found = any(name in item.casefold() for item in after["elements"])
        previously = any(name in item.casefold() for item in before["elements"])
        return found and not previously, "Requested element appeared"
    return before["text"] != after["text"], "Observed public page content changed"


def evaluate_observation(mission_id: str, observation: dict[str, Any], *,
                         task_id: str | None = None, observed_at: float | None = None,
                         db_path: str | None = None) -> dict[str, Any] | None:
    now = time.time() if observed_at is None else observed_at
    state = _state(observation)
    serialized = json.dumps(state, sort_keys=True)
    digest = hashlib.sha256(serialized.encode()).hexdigest()
    db.init_db(db_path)
    with db.get_connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM monitoring_missions WHERE id=?", (mission_id,)).fetchone()
        if row is None:
            raise ValueError("unknown mission")
        before = json.loads(row["last_state_json"] or "{}")
        changed = bool(row["last_state_hash"] and row["last_state_hash"] != digest)
        should_alert, summary = _triggered(row["condition_type"], json.loads(row["condition_params"] or "{}"),
                                           before, state) if changed else (False, "Unchanged")
        event = None
        if changed and should_alert and digest != row["last_alert_hash"]:
            event = {"id": str(uuid.uuid4()), "mission_id": mission_id, "task_id": task_id,
                     "detected_at": now, "title": row["title"], "summary": summary,
                     "diff_data": {"before": before.get("text", "")[:250], "after": state["text"][:250],
                                   "old_price": before.get("price"), "new_price": state.get("price")}}
            conn.execute("""INSERT INTO monitoring_events
                (id,mission_id,task_id,detected_at,title,summary,diff_data,state_hash)
                VALUES(?,?,?,?,?,?,?,?)""", (event["id"], mission_id, task_id, now, row["title"],
                  summary, json.dumps(event["diff_data"]), digest))
            conn.execute("UPDATE monitoring_missions SET last_alert_hash=?,last_alert_at=? WHERE id=?",
                         (digest, now, mission_id))
        from .monitoring_scheduler import next_due_at
        next_run = next_due_at(row["trigger_type"], row["schedule_cron"], row["interval_seconds"], now)
        conn.execute("""UPDATE monitoring_missions SET last_state_hash=?,last_state_json=?,last_run_at=?,next_run_at=?
            WHERE id=?""", (digest, serialized, now, next_run, mission_id))
    return event


async def broadcast_event(event: dict[str, Any]) -> None:
    from .hub import hub
    envelope = {"type": "MISSION_EVENT_TRIGGERED", "taskId": event.get("task_id"), "payload": event}
    delivered = False
    for ws in list(hub.client_sockets):
        try:
            await ws.send_json(envelope)
            delivered = True
        except Exception:
            hub.client_sockets.remove(ws) if ws in hub.client_sockets else None
    if delivered:
        with db.get_connection() as conn:
            conn.execute("UPDATE monitoring_events SET notification_sent=1 WHERE id=?", (event["id"],))
