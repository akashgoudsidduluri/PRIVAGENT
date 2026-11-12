from fastapi.testclient import TestClient

from app import db, config, agentLoop
from app.hub import hub
from app.main import app


def test_inspector_returns_redacted_semantic_map_and_verification(tmp_path, monkeypatch):
    monkeypatch.setenv("PRIVAGENT_DB_PATH", str(tmp_path / "inspector.db"))
    db.init_db()
    task = hub.new_task("Inspect page")
    task.last_observation = {"tabId": 2, "url": "https://example.org/?token=secret",
        "title": "Account for person@example.org", "interactive": [
            {"targetId": "a", "role": "button", "name": "Continue", "tag": "button", "visible": True,
             "enabled": True, "semantic": {"elementId": "a", "role": "button", "semanticName": "Continue",
                "controlType": "generic", "interactability": {"bounds": {"x": 10, "y": 20, "width": 60, "height": 30}, "visible": True}}}],
        "scroll": {"x": 0, "y": 0}, "dpr": 1, "pageText": "person@example.org"}
    db.save_task_checkpoint(task.task_id, "local", None, 1, "https://example.org",
                            [{"verification": {"layers": {"target": "pass"}, "result_ok": True}}], {}, db_path=str(tmp_path / "inspector.db"))
    try:
        result = TestClient(app).get("/api/control/inspector")
        assert result.status_code == 200
        payload = result.json()
        assert payload["taskId"] == task.task_id
        assert payload["semantic"][0]["name"] == "Continue"
        assert payload["verification"][0]["layers"]["target"] == "pass"
        assert "secret" not in payload["url"]
        assert "person@example.org" not in str(payload)
        activity = TestClient(app).get("/api/control/activity").json()
        assert any(item["type"] == "browser" for item in activity)
    finally:
        hub.tasks.pop(task.task_id, None)


def test_privacy_consent_timeouts_and_mission_patch_preflight(tmp_path, monkeypatch):
    monkeypatch.setenv("PRIVAGENT_DB_PATH", str(tmp_path / "settings.db"))
    original_screenshots = config.ALLOW_SCREENSHOTS
    original_task = agentLoop.TASK_TIMEOUT_S
    original_planner = agentLoop.PLANNER_TIMEOUT_S
    client = TestClient(app)
    try:
        assert client.get("/api/control/privacy").status_code == 200
        assert client.post("/api/control/privacy/screenshot", json={"enabled": True}).json()["screenshotsAllowed"]
        assert client.post("/api/control/settings", json={"taskSeconds": 180, "plannerSeconds": 30}).json() == {"taskSeconds": 180, "plannerSeconds": 30}
        assert client.post("/api/control/settings", json={"taskSeconds": 1, "plannerSeconds": 30}).status_code == 422
        preflight = client.options("/api/control/missions/id", headers={"Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "PATCH"})
        assert preflight.status_code == 200
        assert "PATCH" in preflight.headers["access-control-allow-methods"]
    finally:
        config.ALLOW_SCREENSHOTS = original_screenshots
        agentLoop.TASK_TIMEOUT_S = original_task
        agentLoop.PLANNER_TIMEOUT_S = original_planner
