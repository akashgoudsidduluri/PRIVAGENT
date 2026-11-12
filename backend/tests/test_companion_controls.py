from fastapi.testclient import TestClient

from app.hub import hub
from app.main import app
from app.agentLoop import _pause_events
from app import agentLoop
import asyncio


def test_pause_resume_and_cancel_gate():
    client = TestClient(app)
    task = hub.new_task("Observe a page")
    task.state = "running"
    try:
        assert client.post(f"/api/task/{task.task_id}/pause").json()["taskState"] == "paused"
        assert task.task_id in _pause_events
        assert not _pause_events[task.task_id].is_set()
        assert client.post(f"/api/task/{task.task_id}/pause").status_code == 409
        assert client.post(f"/api/task/{task.task_id}/resume").json()["taskState"] == "running"
        assert task.task_id not in _pause_events
        assert client.post(f"/api/task/{task.task_id}/cancel").json()["taskState"] == "cancelled"
        assert client.post(f"/api/task/{task.task_id}/resume").status_code == 409
    finally:
        hub.tasks.pop(task.task_id, None)
        _pause_events.pop(task.task_id, None)


def test_paused_loop_does_not_observe_until_resumed():
    seen = []

    async def scenario():
        agentLoop.request_pause("pause-loop")
        task = asyncio.create_task(agentLoop.run_with_injection(
            object(), task_id="pause-loop", instruction="hello",
            planner=lambda *_: {"goal": "hello", "steps": [], "answer": "Done"},
            obs_factory=lambda: seen.append("observed") or {
                "tabId": 1, "url": "https://example.org", "title": "Page", "interactive": [], "scroll": {}, "dpr": 1},
        ))
        await asyncio.sleep(.02)
        assert seen == []
        agentLoop.request_resume("pause-loop")
        return await asyncio.wait_for(task, 2)

    result = asyncio.run(scenario())
    assert seen == ["observed"]
    assert result["final"] == "completed"
    assert "pause-loop" not in _pause_events
