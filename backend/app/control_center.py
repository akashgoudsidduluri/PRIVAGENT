"""Control-center mission API. The scheduler owns execution and event writes."""
from __future__ import annotations

import json
import os
import re
import secrets
import time
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field, HttpUrl

from . import db
from .monitoring_scheduler import next_due_at
from .hub import hub

router = APIRouter(prefix="/api/control")


def initialize() -> None:
    db.init_db()


class MonitorInput(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    target_url: HttpUrl
    conversation_id: str | None = None
    trigger_type: Literal["interval", "cron", "webhook"] = "interval"
    schedule_cron: str | None = None
    interval_seconds: int = Field(default=300, ge=60, le=604800)
    condition_type: Literal["price_drop", "text_change", "regex", "element"] = "text_change"
    condition_params: dict[str, Any] = Field(default_factory=dict)


class MissionUpdate(BaseModel):
    status: Literal["active", "paused"] | None = None
    interval_seconds: int | None = Field(default=None, ge=60, le=604800)
    schedule_cron: str | None = None


def _public_mission(row) -> dict[str, Any]:
    item = dict(row)
    item["condition_params"] = json.loads(item["condition_params"] or "{}")
    item.pop("last_state_json", None)
    return item


@router.get("/missions")
def missions():
    initialize()
    with db.get_connection() as conn:
        return [_public_mission(r) for r in conn.execute("SELECT * FROM monitoring_missions ORDER BY created_at DESC")]


@router.post("/missions", status_code=201)
def create_mission(body: MonitorInput):
    from .website_resolver import is_safe_public_url
    from .privacy import redact_url, _redact_text, classify_sensitivity, Sensitivity
    url = str(body.target_url)
    if not is_safe_public_url(url)[0]:
        raise HTTPException(422, "Monitor requires a safe public URL")
    criteria = json.dumps(body.condition_params, sort_keys=True)
    if (redact_url(url) != url or _redact_text(body.title, 120) != body.title or
            _redact_text(criteria, 4000) != criteria or len(criteria) > 4000 or
            classify_sensitivity(criteria) >= Sensitivity.RESTRICTED_PII):
        raise HTTPException(422, "Remove sensitive values before saving a monitor")
    from .change_detector import _triggered
    try:
        _triggered(body.condition_type, body.condition_params,
                   {"text": "", "elements": [], "price": 100},
                   {"text": "", "elements": [], "price": 100})
    except (ValueError, re.error) as exc:
        raise HTTPException(422, f"Invalid monitor condition: {exc}") from None
    try:
        next_due_at(body.trigger_type, body.schedule_cron, body.interval_seconds, time.time())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    initialize()
    mission_id = str(uuid.uuid4())
    with db.get_connection() as conn:
        conn.execute("""INSERT INTO monitoring_missions
          (id,conversation_id,title,target_url,trigger_type,schedule_cron,interval_seconds,
           condition_type,condition_params,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,'paused',?)""",
          (mission_id, body.conversation_id, body.title, url, body.trigger_type,
           body.schedule_cron, body.interval_seconds, body.condition_type, criteria, time.time()))
    return {"id": mission_id, "status": "paused"}


@router.patch("/missions/{mission_id}")
def update_mission(mission_id: str, body: MissionUpdate):
    initialize()
    with db.get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM monitoring_missions WHERE id=?", (mission_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "mission not found")
        interval = body.interval_seconds or row["interval_seconds"]
        cron = body.schedule_cron if body.schedule_cron is not None else row["schedule_cron"]
        status = body.status or row["status"]
        if row["trigger_type"] == "webhook" and status == "active":
            next_run = None
        else:
            try:
                next_run = time.time() if status == "active" and row["status"] != "active" else next_due_at(row["trigger_type"], cron, interval, time.time())
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
        conn.execute("""UPDATE monitoring_missions SET status=?,interval_seconds=?,schedule_cron=?,next_run_at=? WHERE id=?""",
                     (status, interval, cron, next_run if status == "active" else None, mission_id))
    return {"id": mission_id, "status": status, "next_run_at": next_run}


@router.get("/missions/{mission_id}/events")
def mission_events(mission_id: str):
    initialize()
    with db.get_connection() as conn:
        if not conn.execute("SELECT 1 FROM monitoring_missions WHERE id=?", (mission_id,)).fetchone():
            raise HTTPException(404, "mission not found")
        return [{**dict(row), "diff_data": json.loads(row["diff_data"] or "{}")}
                for row in conn.execute("SELECT * FROM monitoring_events WHERE mission_id=? ORDER BY detected_at DESC LIMIT 100", (mission_id,))]


@router.post("/missions/{mission_id}/trigger")
def trigger_webhook(mission_id: str, x_privagent_token: str | None = Header(default=None)):
    expected = os.environ.get("PRIVAGENT_MONITOR_WEBHOOK_SECRET")
    if not expected:
        raise HTTPException(503, "webhook trigger is not configured")
    if not x_privagent_token or not secrets.compare_digest(x_privagent_token, expected):
        raise HTTPException(403, "invalid webhook token")
    with db.get_connection() as conn:
        row = conn.execute("SELECT trigger_type,status FROM monitoring_missions WHERE id=?", (mission_id,)).fetchone()
        if not row:
            raise HTTPException(404, "mission not found")
        if row["trigger_type"] != "webhook" or row["status"] != "active":
            raise HTTPException(409, "webhook mission is not active")
        conn.execute("UPDATE monitoring_missions SET next_run_at=? WHERE id=?", (time.time(), mission_id))
    return {"queued": True}


@router.post("/missions/{mission_id}/events/{event_id}/ack")
def acknowledge_event(mission_id: str, event_id: str):
    with db.get_connection() as conn:
        result = conn.execute("UPDATE monitoring_events SET acknowledged=1 WHERE id=? AND mission_id=?", (event_id, mission_id))
        if not result.rowcount:
            raise HTTPException(404, "event not found")
    return {"ok": True}


@router.get("/capabilities")
def capabilities():
    return [dict(name=name, kind="connector", status="not configured",
                 description="Native adapter and OAuth configuration required; no account access is assumed.")
            for name in ("Gmail", "Google Calendar", "Google Drive", "Slack", "GitHub")] + [
        dict(name="MCP tool runtime", kind="mcp", status="not connected",
             description="No external servers registered. Mutating tools require an execution-bound approval.")]


@router.get("/inspector")
def inspector():
    """Latest real observation and stored verification evidence for the local operator."""
    from .contracts import BrowserObservation
    from .privacy import classify_and_redact, scrub_persistent
    task = max(hub.tasks.values(), key=lambda t: t.created_at, default=None)
    if task is None:
        return {"taskId": None, "semantic": [], "verification": [], "screenshot": None}
    semantic = []
    screenshot = None
    url = ""
    raw = task.last_observation
    if raw:
        try:
            observation = BrowserObservation.model_validate(raw)
            filtered, _ = classify_and_redact(observation, instruction=task.instruction, record_metrics=False)
            semantic = [{"id": e.target_id, "role": e.role, "name": e.name,
                         "control": e.semantic.control_type if e.semantic else "generic",
                         "bounds": e.semantic.interactability.get("bounds") if e.semantic else e.bounds}
                        for e in filtered.interactive[:100]]
            screenshot = filtered.screenshot
            url = filtered.url
        except Exception:
            pass
    checkpoint = db.get_task_checkpoint(task.task_id)
    history = (checkpoint or {}).get("history") or []
    verification = [scrub_persistent(h.get("verification")) for h in history if h.get("verification")][-15:]
    return {"taskId": task.task_id, "state": task.state, "url": url,
            "semantic": semantic, "verification": verification, "screenshot": screenshot}


@router.get("/activity")
def activity():
    """Chronological, redacted receipts across browser, MCP and monitors."""
    db.init_db()
    events = []
    with db.get_connection() as conn:
        for row in conn.execute("SELECT task_id,updated_at,history FROM task_checkpoints ORDER BY updated_at DESC LIMIT 40"):
            try:
                history = json.loads(row["history"] or "[]")
            except (ValueError, TypeError):
                continue
            for step_index, item in enumerate(history[-20:]):
                if not isinstance(item, dict):
                    continue
                action = item.get("action") or {}
                if not isinstance(action, dict):
                    action = {}
                events.append({"type": "browser", "at": row["updated_at"], "taskId": row["task_id"],
                               "title": str(action.get("type") or "Browser step")[:80],
                               "detail": str(item.get("evidence") or "")[:250],
                               "status": "ok" if item.get("result_ok") else "unverified", "order": step_index})
        for row in conn.execute("SELECT tool_name,status,created_at FROM mcp_audit ORDER BY created_at DESC LIMIT 100"):
            events.append({"type": "mcp", "at": row["created_at"], "title": row["tool_name"],
                           "detail": "Hashed tool execution receipt", "status": row["status"]})
        for row in conn.execute("SELECT title,summary,detected_at FROM monitoring_events ORDER BY detected_at DESC LIMIT 100"):
            events.append({"type": "monitor", "at": row["detected_at"], "title": row["title"],
                           "detail": row["summary"], "status": "triggered"})
    return sorted(events, key=lambda e: (e["at"], e.get("order", 0)), reverse=True)[:100]


@router.get("/benchmark/{task_id}")
def benchmark_receipts(task_id: str):
    """Minimum evidence needed by the external benchmark runner."""
    from .privacy import scrub_persistent
    checkpoint = db.get_task_checkpoint(task_id)
    if checkpoint is None:
        raise HTTPException(404, "no checkpoint for task")
    task = hub.tasks.get(task_id)
    chat_id = checkpoint["conversation_id"]
    package = db.get_latest_package_for_chat(chat_id)
    data = package.get("data", {}) if package else {}
    sources = data.get("sources") or {}
    claims = data.get("claims") or {}
    anchored = sum(bool(c.get("primary_source_id") or c.get("supporting_source_ids"))
                   for c in claims.values() if isinstance(c, dict))
    sentence_count = 0
    anchored_sentences = 0
    for claim in claims.values():
        if not isinstance(claim, dict):
            continue
        sentences = [s for s in re.split(r"(?<=[.!?])\s+", str(claim.get("statement") or "").strip()) if s]
        sentence_count += len(sentences)
        if len(sentences) == 1 and (claim.get("primary_source_id") or claim.get("supporting_source_ids")):
            anchored_sentences += 1
    artifacts = db.list_task_artifacts(task_id)
    pdf_pages = 0
    from pathlib import Path
    artifact_root = (db.DB_DIR / "artifacts").resolve()
    for artifact in artifacts:
        if artifact.get("file_format") != "pdf":
            continue
        path = Path(artifact.get("filepath") or "").resolve()
        if path.is_file() and artifact_root in path.parents:
            try:
                from pypdf import PdfReader
                pdf_pages = max(pdf_pages, len(PdfReader(str(path)).pages))
            except Exception:
                pass
    return {"taskId": task_id, "lastUrl": checkpoint.get("last_url"),
            "history": [{"result_ok": bool(h.get("result_ok")),
                         "verification": h.get("verification"), "error": h.get("error", ""),
                         "evidence": str(h.get("evidence") or "")[:1000]}
                        for h in checkpoint.get("history", []) if isinstance(h, dict)],
            "summary": scrub_persistent(task.message) if task else "",
            "sourceCount": len(sources), "primarySourceCount": sum(bool(s.get("is_primary")) for s in sources.values() if isinstance(s, dict)),
            "claimCount": len(claims), "anchoredClaims": anchored,
            "sentenceCount": sentence_count, "anchoredSentences": anchored_sentences,
            "artifactFormats": [a.get("file_format") for a in artifacts], "pdfPages": pdf_pages}


@router.get("/privacy")
def privacy_status():
    from . import config
    from .privacy import privacy_counters, privacy_events
    return {"screenshotsAllowed": bool(config.ALLOW_SCREENSHOTS),
            "counters": privacy_counters(), "events": privacy_events(),
            "policy": "Detected credentials and PII are redacted before model input."}


class ScreenshotConsent(BaseModel):
    enabled: bool


@router.post("/privacy/screenshot")
def screenshot_consent(body: ScreenshotConsent):
    from . import config
    config.ALLOW_SCREENSHOTS = body.enabled
    return {"screenshotsAllowed": bool(config.ALLOW_SCREENSHOTS)}


class RuntimeTimeouts(BaseModel):
    task_seconds: int = Field(ge=60, le=3600, alias="taskSeconds")
    planner_seconds: int = Field(ge=5, le=180, alias="plannerSeconds")
    model_config = {"populate_by_name": True}


@router.get("/settings")
def runtime_settings():
    from . import agentLoop
    return {"taskSeconds": agentLoop.TASK_TIMEOUT_S, "plannerSeconds": agentLoop.PLANNER_TIMEOUT_S}


@router.post("/settings")
def update_runtime_settings(body: RuntimeTimeouts):
    from . import agentLoop
    agentLoop.TASK_TIMEOUT_S = body.task_seconds
    agentLoop.PLANNER_TIMEOUT_S = body.planner_seconds
    return runtime_settings()


def record_observation(mission_id: str, text: str, observed_at: float | None = None):
    """Legacy synchronous baseline helper used by older callers/tests."""
    from .change_detector import evaluate_observation
    return bool(evaluate_observation(mission_id, {"text": text}, observed_at=observed_at))
