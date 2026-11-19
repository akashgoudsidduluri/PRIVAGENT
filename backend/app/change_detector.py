"""Atomic monitoring baselines, condition checks and deduplicated events."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import time
import uuid
from typing import Any

from . import db
from .privacy import _redact_text, scrub_persistent

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
    if condition == "new_listings":
        # New items on a listing / search page (jobs, internships,
        # products): fires when accessible names appear that were not
        # in the previous snapshot. The caller only evaluates this
        # condition when the state changed, so the first run only
        # baselines and never alerts. Reordering alone can surface a
        # name the snapshot window dropped, so the summary names the
        # new items for the human to judge.
        before_names = {item.casefold() for item in before.get("elements") or []}
        if not before_names:
            # The previous snapshot had no items (login wall, empty
            # results, failed load): do NOT treat the first non-empty
            # snapshot as "everything is new" — baseline it instead,
            # or the watch fires once on every existing listing.
            return False, "Baseline: no prior listings to compare"
        new_items = [item for item in (after.get("elements") or []) if item.casefold() not in before_names]
        if not new_items:
            return False, "No new listings"
        shown = ", ".join(new_items[:3])
        extra = f" and {len(new_items) - 3} more" if len(new_items) > 3 else ""
        return True, f"New listings: {shown}{extra}"
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


def _alert_text(event: dict[str, Any]) -> str:
    """Human-readable chat text for a monitoring event."""
    title = str(event.get("title") or "Monitoring alert").strip()
    summary = str(event.get("summary") or "").strip()
    diff = event.get("diff_data") or {}
    old_price = diff.get("old_price")
    new_price = diff.get("new_price")
    lines = [f"🔔 **Monitoring alert — {title}**"]
    if summary:
        lines.append(f"• {summary}")
    if isinstance(old_price, (int, float)) and isinstance(new_price, (int, float)):
        lines.append(f"• Price: ₹{old_price:g} → ₹{new_price:g}")
    elif isinstance(new_price, (int, float)):
        lines.append(f"• Price now: ₹{new_price:g}")
    return "\n".join(lines)


def _persist_event_to_chat(event: dict[str, Any], db_path: str | None = None) -> str | None:
    """Persist one monitoring event into its mission's conversation.

    Mirrors the email-alert pattern (email_intelligence/sync_engine:
    add_message + touch_chat). Returns the conversation_id, or None
    when there is nothing to persist (no mission row, no conversation,
    or already persisted).

    Dedupe: an event is persisted at most once. Once its
    monitoring_events row has notification_sent=1 the row is never
    written again, so a repeated broadcast of the same event cannot
    spam the chat — while a NEW event for the same mission (its own
    row, flag 0) still persists. Persisting happens before the
    WebSocket send, so the alert is never lost when no client is
    connected at trigger time.

    Guards (review pass): the conversation may have been deleted
    (delete_chat removes only the chat row; the mission survives),
    and a mission stamped with an owner may only write into that
    owner's chat — a notification-write failure must never fail the
    tick or kill the watch.
    """
    event_id = event.get("id")
    mission_id = event.get("mission_id")
    if not event_id or not mission_id:
        return None
    with db.get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT conversation_id, user_id FROM monitoring_missions WHERE id=?", (mission_id,)
        ).fetchone()
        if row is None:
            return None
        conv_id = row["conversation_id"]
        if not conv_id:
            return None
        chat_row = conn.execute(
            "SELECT user_id FROM chats WHERE id=?", (conv_id,)
        ).fetchone()
        if chat_row is None:
            return None
        mission_user = row["user_id"]
        if mission_user is not None and chat_row["user_id"] != mission_user:
            return None
        sent = conn.execute(
            "SELECT notification_sent FROM monitoring_events WHERE id=?", (event_id,)
        ).fetchone()
        if sent is not None and int(sent[0]) == 1:
            return conv_id
    text = str(scrub_persistent(_alert_text(event)))
    try:
        db.add_message(conv_id, "agent", text, evidence="monitoring_event",
                       task_id=event.get("task_id"), db_path=db_path)
    except sqlite3.IntegrityError:
        # TOCTOU: the chat was deleted between the check above and
        # the insert. Losing one alert beats killing the watch, and
        # the flag stays 0 so a later broadcast retries instead of
        # silently dropping the event.
        logging.getLogger(__name__).warning(
            "monitor %s: alert could not be persisted to chat %s", mission_id, conv_id)
        return None
    db.touch_chat(conv_id, db_path=db_path)
    with db.get_connection(db_path) as conn:
        conn.execute("UPDATE monitoring_events SET notification_sent=1 WHERE id=?", (event_id,))
    return conv_id


async def broadcast_event(event: dict[str, Any], db_path: str | None = None) -> None:
    from starlette.websockets import WebSocketDisconnect, WebSocketState
    from .hub import hub
    # Persist first: the chat copy is the durable delivery channel.
    # A live socket is only the fast path — an alert must survive a
    # trigger that fires while nobody is connected.
    conversation_id = _persist_event_to_chat(event, db_path=db_path)
    envelope = {"type": "MISSION_EVENT_TRIGGERED", "taskId": event.get("task_id"),
                "conversationId": conversation_id, "payload": event}
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
        with db.get_connection(db_path) as conn:
            conn.execute("UPDATE monitoring_events SET notification_sent=1 WHERE id=?", (event.get("id"),))
