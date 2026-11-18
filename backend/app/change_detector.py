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

# Task 2 live monitoring: volatile noise that must NOT alert. Each
# match is replaced by a single space before hashing/diffing, so a
# page whose only change is a timestamp, a "5 minutes ago" label or a
# view counter keeps the same state hash. Real content and prices are
# untouched — a price is never part of these patterns, and a content
# change next to a timestamp still changes the hash.
_MONTHS = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
_VOLATILE_RES = [
    # ISO datetimes: 2026-11-17, 2026-11-17T09:28:00Z
    re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)?,?(?!\w)"),
    # Month-name dates: "17 November 2026", "November 17, 2026"
    re.compile(rf"\b\d{{1,2}}\s+{_MONTHS}\s+\d{{4}},?(?!\w)", re.I),
    re.compile(rf"\b{_MONTHS}\s+\d{{1,2}},?\s+\d{{4}},?(?!\w)", re.I),
    # Numeric dates: 17/11/2026, 11-17-2026
    re.compile(r"\b\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4},?(?!\w)"),
    # Relative time: "5 minutes ago", "an hour ago", "just now"
    re.compile(r"\b\d+\s+(?:seconds?|minutes?|hours?|days?|weeks?|months?|years?)\s+ago,?(?!\w)", re.I),
    re.compile(r"\b(?:a|an)\s+(?:second|minute|hour|day|week|month|year)\s+ago,?(?!\w)", re.I),
    re.compile(r"\bjust now,?(?!\w)", re.I),
    re.compile(r"\bmoments? ago,?(?!\w)", re.I),
    # Rapidly-changing counters: "1,234 views", "57 people online"
    re.compile(r"\b[\d,]+\s+(?:views?|visitors?|likes?|comments?|shares?|followers?|subscribers?|downloads?|plays?),?(?!\w)", re.I),
    re.compile(r"\b[\d,]+\s+people\s+(?:online|viewing|reading|here),?(?!\w)", re.I),
    re.compile(r"\b[\d,]+\s+online,?(?!\w)", re.I),
    # Clock times: 09:28, 09:28:14, 9:28 AM
    re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\s*(?:[AP]M|am|pm)?,?(?!\w)"),
]


def _normalise_volatile(text: str) -> str:
    """Strip volatile timestamps/counters so noise cannot alert."""
    out = text
    for pattern in _VOLATILE_RES:
        out = pattern.sub(" ", out)
    return re.sub(r"\s+", " ", out).strip()


def _state(observation: dict[str, Any]) -> dict[str, Any]:
    text = _redact_text(_normalise_volatile(re.sub(r"\s+", " ", str(observation.get("text") or "")).strip()), 4000)
    names = [_redact_text(_normalise_volatile(str(name)), 120) for name in (observation.get("elements") or [])[:100]]
    match = _PRICE.search(text)
    price = float(match.group(1).replace(",", "")) if match else None
    return {"text": text, "elements": names, "price": price}


def _triggered(condition: str, params: dict, before: dict, after: dict) -> tuple[bool, str]:
    if condition == "daily_price":
        # Daily price digest (e.g. "every morning at 9am, tell me the
        # price"): reports the current price on EVERY scheduled run,
        # even when it has not changed since yesterday. This is a
        # report, not a change alert — so it never depends on `before`.
        new = after.get("price")
        if new is None:
            return False, "Price unavailable"
        return True, f"Price today: ₹{new:g}"
    if condition == "price_drop":
        old, new = before.get("price"), after.get("price")
        threshold = float(params.get("min_percent", 0))
        if not 0 <= threshold <= 100:
            raise ValueError("price drop threshold must be 0-100%")
        if old is None or new is None or old <= 0:
            return False, "Price unavailable"
        return new < old and (old - new) * 100 / old >= threshold, f"Price changed from {old:g} to {new:g}"
    if condition == "below_price":
        # Absolute threshold ("alert me when the price falls below
        # ₹5000"): fires whenever the observed price is under the
        # threshold — a state, not a change — so the first run can
        # already alert when the product is cheap.
        try:
            threshold = float(params.get("threshold"))
        except (TypeError, ValueError):
            raise ValueError("below-price threshold must be a number") from None
        if threshold <= 0:
            raise ValueError("below-price threshold must be positive")
        new = after.get("price")
        if new is None:
            return False, "Price unavailable"
        return new < threshold, f"Price below ₹{threshold:g} (now ₹{new:g})"
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
        # A daily-price digest reports on every scheduled run, even when
        # nothing changed (and even on the first run, when there is no
        # baseline yet). A below-price threshold is likewise a state,
        # not a change: being under the threshold is alert-worthy on
        # the first run too (the product may already be cheap) — but
        # unlike the digest it is deduplicated by state digest, so a
        # steady below-threshold price alerts once, not every tick.
        # Every other condition only fires on a change, and is
        # deduplicated by the alert hash.
        if row["condition_type"] == "daily_price":
            should_alert, summary = _triggered(row["condition_type"], json.loads(row["condition_params"] or "{}"),
                                               before, state)
            fire = should_alert
        elif row["condition_type"] == "below_price":
            should_alert, summary = _triggered(row["condition_type"], json.loads(row["condition_params"] or "{}"),
                                               before, state)
            fire = should_alert and digest != row["last_alert_hash"]
        elif changed:
            should_alert, summary = _triggered(row["condition_type"], json.loads(row["condition_params"] or "{}"),
                                               before, state)
            fire = should_alert and digest != row["last_alert_hash"]
        else:
            should_alert, summary = False, "Unchanged"
            fire = False
        event = None
        if fire:
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
    from starlette.websockets import WebSocketDisconnect, WebSocketState
    from .hub import hub
    envelope = {"type": "MISSION_EVENT_TRIGGERED", "taskId": event.get("task_id"), "payload": event}
    delivered = False
    for ws in list(hub.client_sockets):
        if getattr(ws, "client_state", None) == WebSocketState.DISCONNECTED or getattr(ws, "application_state", None) == WebSocketState.DISCONNECTED:
            hub.client_sockets.remove(ws) if ws in hub.client_sockets else None
            continue
        try:
            await ws.send_json(envelope)
            delivered = True
        except (WebSocketDisconnect, ConnectionResetError, BrokenPipeError, OSError):
            hub.client_sockets.remove(ws) if ws in hub.client_sockets else None
        except Exception:
            hub.client_sockets.remove(ws) if ws in hub.client_sockets else None
    if delivered:
        with db.get_connection() as conn:
            conn.execute("UPDATE monitoring_events SET notification_sent=1 WHERE id=?", (event["id"],))
