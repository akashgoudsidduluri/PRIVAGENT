import asyncio
import json
import time

from fastapi.testclient import TestClient
import pytest

from app import db
from app.change_detector import evaluate_observation, broadcast_event
from app.control_center import create_mission, MonitorInput
from app.hub import hub
from app.main import app
from app.monitoring_scheduler import claim_due, next_due_at, execute_monitor


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = str(tmp_path / "monitor.sqlite3")
    monkeypatch.setenv("PRIVAGENT_DB_PATH", path)
    db.init_db()
    return path


def _mission(condition="text_change", params=None):
    return create_mission(MonitorInput(title="Price watch", target_url="https://example.org/product",
                                       interval_seconds=60, condition_type=condition, condition_params=params or {}))["id"]


def test_persistence_claim_and_nonduplicate_text_events(database):
    mission_id = _mission()
    client = TestClient(app)
    assert client.patch(f"/api/control/missions/{mission_id}", json={"status": "active"}).status_code == 200
    due = claim_due(now=time.time() + 1, db_path=database)
    assert len(due) == 1 and due[0]["id"] == mission_id
    assert claim_due(now=time.time() + 1, db_path=database) == []
    assert evaluate_observation(mission_id, {"text": "₹100"}, observed_at=100, db_path=database) is None
    event = evaluate_observation(mission_id, {"text": "₹90"}, observed_at=160, db_path=database)
    assert event is not None and event["diff_data"]["old_price"] == 100
    assert evaluate_observation(mission_id, {"text": "₹90"}, observed_at=220, db_path=database) is None
    with db.get_connection(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM monitoring_events WHERE mission_id=?", (mission_id,)).fetchone()[0] == 1
        assert conn.execute("SELECT next_run_at FROM monitoring_missions WHERE id=?", (mission_id,)).fetchone()[0] == 280
    # A new process/connection sees the event and persists its acknowledgement.
    events = client.get(f"/api/control/missions/{mission_id}/events").json()
    assert len(events) == 1
    assert client.post(f"/api/control/missions/{mission_id}/events/{events[0]['id']}/ack").json() == {"ok": True}


def test_price_regex_element_and_sensitive_data(database):
    price = _mission("price_drop", {"min_percent": 10})
    assert evaluate_observation(price, {"text": "₹100"}, observed_at=100) is None
    assert evaluate_observation(price, {"text": "₹95"}, observed_at=160) is None
    assert evaluate_observation(price, {"text": "₹80"}, observed_at=220) is not None
    assert evaluate_observation(price, {"text": "₹80"}, observed_at=280) is None
    regex = _mission("regex", {"pattern": "in stock"})
    evaluate_observation(regex, {"text": "Sold out"}, observed_at=100)
    assert evaluate_observation(regex, {"text": "In stock"}, observed_at=160)
    element = _mission("element", {"name": "Buy now"})
    evaluate_observation(element, {"text": "Product", "elements": ["Details"]}, observed_at=100)
    assert evaluate_observation(element, {"text": "Product", "elements": ["Buy now"]}, observed_at=160)
    sensitive = _mission()
    evaluate_observation(sensitive, {"text": "Contact me at someone@example.org"}, observed_at=100)
    with db.get_connection(database) as conn:
        state = conn.execute("SELECT last_state_json FROM monitoring_missions WHERE id=?", (sensitive,)).fetchone()[0]
    assert "someone@example.org" not in state


def test_cron_webhook_and_alert_delivery(database, monkeypatch):
    now = 1_800_000_000
    due = next_due_at("cron", "*/5 * * * *", 60, now)
    assert due > now and (due - now) <= 300
    assert next_due_at("webhook", None, 60, now) is None
    client = TestClient(app)
    created = client.post("/api/control/missions", json={"title": "Webhook", "target_url": "https://example.org/",
        "trigger_type": "webhook", "interval_seconds": 60})
    assert created.status_code == 201
    ident = created.json()["id"]
    assert client.patch(f"/api/control/missions/{ident}", json={"status": "active"}).status_code == 200
    assert client.post(f"/api/control/missions/{ident}/trigger").status_code == 503
    monkeypatch.setenv("PRIVAGENT_MONITOR_WEBHOOK_SECRET", "test-secret")
    assert client.post(f"/api/control/missions/{ident}/trigger", headers={"X-Privagent-Token": "wrong"}).status_code == 403
    assert client.post(f"/api/control/missions/{ident}/trigger", headers={"X-Privagent-Token": "test-secret"}).json() == {"queued": True}
    assert [m["id"] for m in claim_due(now=time.time() + 1)] == [ident]
    assert claim_due(now=time.time() + 1) == []
    class WS:
        async def send_json(self, value):
            self.value = value
    ws = WS()
    hub.client_sockets.append(ws)
    try:
        event = evaluate_observation(ident, {"text": "A"}, observed_at=100)
        assert event is None
        event = evaluate_observation(ident, {"text": "B"}, observed_at=160)
        asyncio.run(broadcast_event(event))
        assert ws.value["type"] == "MISSION_EVENT_TRIGGERED"
        with db.get_connection(database) as conn:
            assert conn.execute("SELECT notification_sent FROM monitoring_events WHERE id=?", (event["id"],)).fetchone()[0] == 1
    finally:
        hub.client_sockets.remove(ws)


def test_scheduler_dispatches_task_record_then_detects_change(database, monkeypatch):
    mission_id = _mission()
    evaluate_observation(mission_id, {"text": "Before"}, observed_at=100)
    async def fake_run(task_id):
        record = hub.tasks[task_id]
        record.state = "completed"
        record.last_observation = {"url": "https://example.org/product", "pageText": "After", "interactive": []}
    monkeypatch.setattr("app.agentLoop.run_task", fake_run)
    with db.get_connection(database) as conn:
        mission = dict(conn.execute("SELECT * FROM monitoring_missions WHERE id=?", (mission_id,)).fetchone())
    asyncio.run(execute_monitor(mission))
    with db.get_connection(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM monitoring_events WHERE mission_id=?", (mission_id,)).fetchone()[0] == 1
