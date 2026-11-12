"""Integration tests for Artifact Storage, Download, and Security Endpoints."""
from __future__ import annotations

import tempfile
from pathlib import Path
from fastapi.testclient import TestClient

from app import db
from app.main import ARTIFACTS_DIR, app


def test_artifact_api_endpoints():
    client = TestClient(app)

    task_id = "task_art_test_100"
    chat_id = "chat_art_test_100"
    art_id = "art_test_file_001"

    # Create test artifact file in ARTIFACTS_DIR
    task_dir = ARTIFACTS_DIR / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    test_file = task_dir / "Research_Summary.docx"
    test_file.write_bytes(b"TEST_DOCX_PAYLOAD_CONTENT_BYTES")

    try:
        # Register artifact in database
        art_record = {
            "artifact_id": art_id,
            "task_id": task_id,
            "conversation_id": chat_id,
            "filename": "Research_Summary.docx",
            "filepath": str(test_file),
            "file_format": "docx",
            "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "file_size_bytes": len(b"TEST_DOCX_PAYLOAD_CONTENT_BYTES"),
            "title": "Quantum Computing Research Summary",
            "summary": "10-page technical overview of superconducting qubits.",
            "metadata": {"pages": 10, "citations": 8},
        }
        db.save_artifact(art_record)

        # 1. List by task
        res_task = client.get(f"/api/tasks/{task_id}/artifacts")
        assert res_task.status_code == 200
        task_arts = res_task.json()
        assert len(task_arts) >= 1
        assert task_arts[0]["artifact_id"] == art_id

        # 2. List by chat
        res_chat = client.get(f"/api/chats/{chat_id}/artifacts")
        assert res_chat.status_code == 200
        chat_arts = res_chat.json()
        assert len(chat_arts) >= 1

        # 3. Get metadata
        res_meta = client.get(f"/api/artifacts/{art_id}")
        assert res_meta.status_code == 200
        meta = res_meta.json()
        assert meta["title"] == "Quantum Computing Research Summary"
        assert meta["metadata"]["citations"] == 8

        # 4. Download endpoint
        res_dl = client.get(f"/api/artifacts/{art_id}/download")
        assert res_dl.status_code == 200
        assert res_dl.content == b"TEST_DOCX_PAYLOAD_CONTENT_BYTES"
        assert 'attachment; filename="Research_Summary.docx"' in res_dl.headers.get("Content-Disposition", "")

        # 5. Preview endpoint
        res_prev = client.get(f"/api/artifacts/{art_id}/preview")
        assert res_prev.status_code == 200
        assert res_prev.content == b"TEST_DOCX_PAYLOAD_CONTENT_BYTES"
        assert 'inline; filename="Research_Summary.docx"' in res_prev.headers.get("Content-Disposition", "")

        # 6. Non-existent artifact
        res_404 = client.get("/api/artifacts/non_existent_id")
        assert res_404.status_code == 404

    finally:
        test_file.unlink(missing_ok=True)
        db.delete_artifact(art_id)


def test_artifact_path_traversal_rejection():
    """Verify that path traversal attempts outside the artifacts directory are blocked with 403."""
    client = TestClient(app)

    malicious_art_id = "art_malicious_traversal"
    # Attempting to access sensitive system path outside artifacts directory
    art_record = {
        "artifact_id": malicious_art_id,
        "task_id": "task_evil",
        "conversation_id": "chat_evil",
        "filename": "evil.txt",
        "filepath": "C:/Windows/System32/drivers/etc/hosts",
        "file_format": "txt",
        "mime_type": "text/plain",
        "file_size_bytes": 100,
        "title": "Malicious Traversal Attempt",
    }
    db.save_artifact(art_record)

    try:
        res = client.get(f"/api/artifacts/{malicious_art_id}/download")
        assert res.status_code == 403
        assert "path traversal" in res.json().get("detail", "").lower()

        res_prev = client.get(f"/api/artifacts/{malicious_art_id}/preview")
        assert res_prev.status_code == 403
    finally:
        db.delete_artifact(malicious_art_id)

