from app.completion_verifier import verify_action_receipt, verify_task_completion
from app.verification_layers import ActionEvidence, verify_layers
from app import file_uploads
from app.planner import _validate_step
from app.contracts import PlannedStep, BrowserAction
import base64
import pytest
from fastapi.testclient import TestClient
from app.main import app


def test_six_layers_require_authority_for_write():
    base = ActionEvidence(.98, True, True, 200, None, True)
    receipt = verify_layers(base, mutating=True)
    assert receipt["layers"]["authority"] == "unknown"
    assert not receipt["verified"]
    assert verify_layers(ActionEvidence(.98, True, True, 201, True, True), mutating=True)["verified"]
    assert not verify_layers(ActionEvidence(.84, True, True, 201, True, True), mutating=True)["verified"]
    assert not verify_layers(ActionEvidence(.98, True, True, 500, True, True), mutating=True)["verified"]


def test_dispatch_ack_does_not_imply_sent_or_paid():
    action = {"type": "type", "target": {"targetId": "composer"}, "params": {"submit": True}}
    result = {"ok": True, "targetId": "composer", "evidence": "accepted",
              "verification": {"dispatchAcknowledged": True, "goalAligned": True},
              "observation": {"pageText": "Sending...", "url": "https://chat.example"}}
    receipt = verify_action_receipt(action, result, before={"pageText": "", "url": "https://chat.example"}, goal="send a message")
    assert receipt["layers"]["authority"] == "unknown"
    assert not receipt["verified"]
    outcome = verify_task_completion("send a message", "completed", "Sent it", history=[
        {"action": action, "result_ok": True, "verification": receipt}])
    assert outcome.state == "failed"
    assert not outcome.is_verified


def test_authoritative_surface_affirmation_requires_all_other_layers():
    action = {"type": "type", "target": {"targetId": "composer"}, "params": {"submit": True}}
    result = {"ok": True, "targetId": "composer", "verification": {
        "dispatchAcknowledged": True, "transportStatus": 200, "goalAligned": True},
        "observation": {"pageText": "Message Sent", "url": "https://chat.example"}}
    receipt = verify_action_receipt(action, result, before={"pageText": "Ready", "url": "https://chat.example"}, goal="send a message")
    assert receipt["verified"]
    assert receipt["layers"]["authority"] == "passed"


def test_spoofed_confirmation_without_transport_cannot_complete():
    action = {"type": "click", "target": {"targetId": "pay"}}
    result = {"ok": True, "targetId": "pay", "verification": {
        "dispatchAcknowledged": True, "goalAligned": True, "surfaceChanged": True},
        "observation": {"pageText": "Order Placed", "url": "https://shop.example"}}
    receipt = verify_action_receipt(action, result, before={"pageText": "Cart", "url": "https://shop.example"}, goal="place an order")
    assert receipt["layers"]["authority"] == "unknown"
    assert not receipt["verified"]


def test_attachment_is_ephemeral_and_planner_cannot_supply_bytes():
    token = file_uploads.put("report.pdf", base64.b64encode(b"%PDF-1.0").decode())
    assert file_uploads.get(token) == ("report.pdf", base64.b64encode(b"%PDF-1.0").decode())
    file_uploads.remove(token)
    assert file_uploads.get(token) is None
    with pytest.raises(ValueError):
        file_uploads.put("payload.exe", base64.b64encode(b"x").decode())
    with pytest.raises(ValueError):
        file_uploads.put("report.pdf", base64.b64encode(b"x" * (5 * 1024 * 1024 + 1)).decode())
    safe = BrowserAction(type="upload_file", risk="sensitive", verify="file selected", timeoutMs=5000,
                         target={"targetId": "file"}, params={})
    _validate_step(PlannedStep(action=safe, why="Upload selected attachment"))
    hostile = BrowserAction(type="upload_file", risk="sensitive", verify="file selected", timeoutMs=5000,
                            target={"targetId": "file"}, params={"base64": "eA=="})
    with pytest.raises(ValueError, match="file details are forbidden"):
        _validate_step(PlannedStep(action=hostile, why="Invent file bytes"))


def test_attachment_api_binds_token_without_echoing_file_bytes(monkeypatch):
    from app.main import _spawn
    monkeypatch.setattr("app.main._spawn", lambda coro: coro.close())
    client = TestClient(app)
    encoded = base64.b64encode(b"sample private file").decode()
    response = client.post("/api/attachments", json={"fileName": "report.txt", "base64": encoded})
    assert response.status_code == 200
    assert encoded not in response.text
    token = response.json()["fileToken"]
    created = client.post("/api/task", json={"instruction": "Upload my selected file", "fileToken": token})
    assert created.status_code == 200
    from app.hub import hub
    task_id = created.json()["taskId"]
    assert hub.tasks[task_id].file_token == token
    assert encoded not in repr(hub.tasks[task_id])
    file_uploads.remove(token)
    hub.tasks.pop(task_id, None)
