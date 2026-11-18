"""Chat-only monitoring (2026-11-18): the monitor form page is gone, so a
watch is created in chat and managed from the conversation. The backend
must (a) let the chat card cancel a watch (DELETE), and (b) tag the
confirmation with a machine-readable marker the workspace turns into
the interactive card — without pointing at the removed dashboard page.
"""
import re

import pytest
from fastapi.testclient import TestClient

from app import db
from app.change_detector import evaluate_observation
from app.continuous_monitoring_companion import handle_continuous_monitoring_instruction
from app.main import app


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = str(tmp_path / "chat-monitor.sqlite3")
    monkeypatch.setenv("PRIVAGENT_DB_PATH", path)
    db.init_db()
    return path


def _mission(database):
    # Insert directly: create_mission() validates the URL with a live
    # DNS lookup, which the sandbox cannot do. The delete path under
    # test only needs a stored mission + events.
    import time
    import uuid
    mission_id = str(uuid.uuid4())
    with db.get_connection(database) as conn:
        conn.execute(
            """INSERT INTO monitoring_missions
            (id, conversation_id, title, target_url, trigger_type, schedule_cron,
             interval_seconds, condition_type, condition_params, status, created_at)
            VALUES (?, 'chat-1', 'Price watch', 'https://example.org/product',
                    'interval', NULL, 60, 'text_change', '{}', 'paused', ?)""",
            (mission_id, time.time()),
        )
        conn.commit()
    return mission_id


def test_delete_mission_removes_watch_and_events(database):
    mission_id = _mission(database)
    # A detected change is recorded against the mission first, so the
    # delete must remove the event history with it.
    evaluate_observation(mission_id, {"text": "₹100"}, observed_at=100, db_path=database)
    event = evaluate_observation(mission_id, {"text": "₹90"}, observed_at=160, db_path=database)
    assert event is not None
    client = TestClient(app)
    result = client.delete(f"/api/control/missions/{mission_id}")
    assert result.status_code == 200
    assert result.json() == {"id": mission_id, "deleted": True}
    assert all(m["id"] != mission_id for m in client.get("/api/control/missions").json())
    assert client.get(f"/api/control/missions/{mission_id}/events").status_code == 404
    # Deleting twice is a 404, not a silent success.
    assert client.delete(f"/api/control/missions/{mission_id}").status_code == 404


def test_delete_unknown_mission_is_404(database):
    assert TestClient(app).delete("/api/control/missions/does-not-exist").status_code == 404


def test_watch_confirmation_carries_marker_and_no_dashboard_link(database):
    # A below-price instruction creates a real watch and returns a
    # confirmation the workspace can turn into an interactive card.
    answer = __import__("asyncio").run(handle_continuous_monitoring_instruction(
        "watch the Amazfit Helio Strap, alert me when it drops below ₹8000",
        chat_id="chat-1", db_path=database))
    match = re.search(r"<!--\s*privagent-watch:\s*([0-9a-fA-F-]{36})\s*-->", answer)
    assert match, "confirmation has no watch marker: " + answer[:200]
    mission_id = match.group(1)
    # The marker's id is the mission that was actually stored.
    with db.get_connection(database) as conn:
        row = conn.execute("SELECT id, status FROM monitoring_missions WHERE id=?", (mission_id,)).fetchone()
    assert row is not None and row["status"] == "active"
    # The removed Missions page must not be referenced: the watch lives
    # in this conversation, not on a dashboard.
    assert "Control Center dashboard" not in answer
    assert "localhost:5173" not in answer


def test_watch_confirmation_without_product_has_no_marker(database):
    # No product and no URL: no watch is created, so no marker — the
    # agent asks which product to watch instead.
    answer = __import__("asyncio").run(handle_continuous_monitoring_instruction(
        "monitor this", chat_id="chat-1", db_path=database))
    assert "privagent-watch:" not in answer
    assert "don't have a product to watch yet" in answer
