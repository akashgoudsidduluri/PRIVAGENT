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



def test_comparison_package_renders_xlsx_from_tracker():
    """A browser comparison task's tracker facts render into a real
    .xlsx through the research artifact pipeline (SIH review demo
    item 2: research/compare + a document).

    Before this, browser comparison tasks never reached the document
    pipeline, so "give the comparison in a xcel" produced no file —
    and the classifier did not even recognise the "xcel" spelling.
    """
    import shutil

    import openpyxl

    from app.loop_research import _finalize_research_artifacts, build_comparison_package

    instruction = (
        "Find the lowest price for the Logitech MX Master 3S wireless mouse "
        "across Amazon.in, Flipkart and Croma. For each store, give the price, "
        "a link to the listing, and the delivery estimate. Tell me which store "
        "is cheapest and give the detailed comparison in a xcel."
    )
    tracker = {"required_sites": {
        "amazon": {"site_key": "amazon", "name": "Amazon", "domains": ["amazon.in"],
                   "default_url": "https://www.amazon.in/s?k=logitech+mx+master+3s",
                   "status": "completed",
                   "data": {"price": "₹8,499", "url": "https://www.amazon.in/dp/B09X", "variant": "Graphite"},
                   "visited_urls": ["https://www.amazon.in/s?k=logitech+mx+master+3s"]},
        "flipkart": {"site_key": "flipkart", "name": "Flipkart", "domains": ["flipkart.com"],
                     "default_url": "https://www.flipkart.com/search?q=logitech+mx+master+3s",
                     "status": "completed",
                     "data": {"price": "₹8,999", "url": "https://www.flipkart.com/product/p/itm271ade01ff274", "variant": "Graphite"},
                     "visited_urls": []},
        "croma": {"site_key": "croma", "name": "Croma", "domains": ["croma.com"],
                  "default_url": "https://www.croma.com/search/?text=logitech+mx+master+3s",
                  "status": "pending", "data": None, "visited_urls": []},
    }, "total_count": 3}

    pkg = build_comparison_package("task_cmp_xlsx", instruction, tracker, summary_msg="Amazon is cheapest at ₹8,499.")
    assert pkg is not None
    assert "MX Master 3S" in pkg.topic.title
    assert len(pkg.tables) == 1
    assert pkg.tables[0].headers == ["Store", "Price", "Variant", "Link"]
    assert len(pkg.tables[0].rows) == 2
    assert pkg.tables[0].rows[0][0] == "Amazon"
    assert len(pkg.sources) == 2 and len(pkg.claims) == 2

    # Nothing verified -> no package (never render an empty document).
    assert build_comparison_package("task_cmp_empty", instruction, {"required_sites": {
        "croma": {"site_key": "croma", "name": "Croma", "domains": ["croma.com"], "status": "pending", "data": None},
    }}) is None

    task_id = "task_cmp_xlsx_test"
    wm = {"research_knowledge_package": pkg.to_dict(), "requested_format": "xlsx",
          "instruction": instruction}
    task_dir = ARTIFACTS_DIR / task_id
    try:
        art = _finalize_research_artifacts(task_id, "chat_cmp_xlsx", wm, summary_msg="Amazon is cheapest at ₹8,499.")
        assert art is not None
        assert art["file_format"] == "xlsx"
        assert art["filename"].endswith(".xlsx")
        f = Path(art["filepath"])
        assert f.exists() and f.stat().st_size > 0
        wb = openpyxl.load_workbook(f, read_only=True)
        assert "Comparison & Metrics" in wb.sheetnames
        ws = wb["Comparison & Metrics"]
        rows = list(ws.iter_rows(values_only=True))
        assert rows[0][:4] == ("Store", "Price", "Variant", "Link")
        # The renderer parses price strings into real Excel numbers.
        assert rows[1][0] == "Amazon" and rows[1][1] == 8499
        assert rows[2][0] == "Flipkart" and rows[2][1] == 8999
        wb.close()
    finally:
        shutil.rmtree(task_dir, ignore_errors=True)
        db.delete_artifact(f"art_{task_id}_xlsx")
