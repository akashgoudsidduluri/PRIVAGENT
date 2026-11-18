"""Attachment awareness: the agent must recognise a file the user attached.

Bug 2026-11-18: selecting a file and sending it in chat uploaded the
file and stored it in memory, but the planner was never told it exists
(file_uploads.py: "No bytes reach planner, logs or SQLite"). The only
use was an `upload_file` action the planner could never plan, because
it did not know a file was attached. The user also saw no confirmation
in the chat bubble.

Fix: the agent loop records the attachment NAME + kind (metadata only,
never the bytes) in working_memory, and build_prompt tells the planner
explicitly — so it can plan an `upload_file` action, and so it does not
claim to have read the file's content, which it cannot see.
"""
import asyncio
import base64

from app import agentLoop
from app import file_uploads
from app.hub import TaskRecord, hub as real_hub
from app.planner import build_prompt
from app.privacy import PrivacyFilteredPayload


def _payload() -> PrivacyFilteredPayload:
    return PrivacyFilteredPayload(
        tab_id=1,
        url="http://x/",
        title="t",
        interactive=[],
        scroll={},
        dpr=1.0,
        elements_original=0,
        elements_sent=0,
    )


def test_build_prompt_tells_planner_about_attachment():
    prompt = build_prompt(
        "Upload the attached file to the form",
        _payload(),
        [],
        working_memory={"attachment": {"fileName": "report.pdf", "kind": "pdf"}},
    )
    assert "<attachment" in prompt, "planner prompt has no attachment block"
    assert "report.pdf" in prompt, "planner prompt does not name the attached file"
    assert "pdf" in prompt, "planner prompt does not state the file kind"
    assert "upload_file" in prompt, "planner prompt does not say the file is usable via upload_file"
    assert "CONTENT is not visible" in prompt or "content is not visible" in prompt.lower(), (
        "planner prompt does not warn that the file content is not visible"
    )


def test_build_prompt_has_no_attachment_block_without_attachment():
    prompt = build_prompt("Do a price hunt", _payload(), [], working_memory={})
    assert "<attachment" not in prompt, "planner prompt invents an attachment block with no file attached"
    prompt2 = build_prompt("Do a price hunt", _payload(), [], working_memory=None)
    assert "<attachment" not in prompt2, "planner prompt invents an attachment block with no working memory"


def test_build_prompt_never_carries_file_bytes():
    # The file content (base64) must never reach the prompt, even when
    # the attachment metadata is present. Only the name + kind go in.
    secret_b64 = base64.b64encode(b"SECRET-FILE-CONTENT-12345").decode()
    prompt = build_prompt(
        "Summarise the attached file",
        _payload(),
        [],
        working_memory={"attachment": {"fileName": "notes.txt", "kind": "txt"}},
    )
    assert secret_b64 not in prompt, "file bytes leaked into the planner prompt"
    assert "SECRET-FILE-CONTENT" not in prompt, "file content leaked into the planner prompt"


class _FakeHub:
    """Minimal extension stand-in: the loop never reaches it (empty plan)."""

    ext_online = True

    async def send_action(self, task_id, action, timeout_s=45.0):
        return {"ok": True}


def _run_loop_with_attachment(task_id, file_name):
    """Register a task with an attachment, run the loop once, return the
    working_memory the planner actually received."""
    token = file_uploads.put(file_name, base64.b64encode(b"dummy bytes").decode())
    real_hub.tasks[task_id] = TaskRecord(task_id=task_id, instruction="loop", file_token=token)
    seen: dict = {}

    def planner(instruction, payload, history, working_memory=None):
        seen.update(working_memory or {})
        return {"goal": "done", "steps": []}

    try:
        result = asyncio.run(
            agentLoop.run_with_injection(
                _FakeHub(),
                planner=planner,
                obs_factory=lambda: {"tabId": 1, "url": "http://x/", "title": "t", "interactive": [], "scroll": {}, "dpr": 1},
                task_id=task_id,
            )
        )
    finally:
        real_hub.tasks.pop(task_id, None)
        file_uploads.remove(token)
    assert result["final"] == "completed", f"loop did not complete: {result}"
    return seen


def test_loop_records_attachment_in_working_memory():
    seen = _run_loop_with_attachment("test-attach-1", "report.pdf")
    assert seen.get("attachment") == {"fileName": "report.pdf", "kind": "pdf"}, (
        f"planner never saw the attachment metadata: {seen.get('attachment')}"
    )


def test_loop_has_no_attachment_without_file():
    real_hub.tasks["test-attach-2"] = TaskRecord(task_id="test-attach-2", instruction="loop")
    seen: dict = {}

    def planner(instruction, payload, history, working_memory=None):
        seen.update(working_memory or {})
        return {"goal": "done", "steps": []}

    try:
        result = asyncio.run(
            agentLoop.run_with_injection(
                _FakeHub(),
                planner=planner,
                obs_factory=lambda: {"tabId": 1, "url": "http://x/", "title": "t", "interactive": [], "scroll": {}, "dpr": 1},
                task_id="test-attach-2",
            )
        )
    finally:
        real_hub.tasks.pop("test-attach-2", None)
    assert result["final"] == "completed"
    assert "attachment" not in seen, "loop invented an attachment with no file attached"


def test_checkpoint_does_not_persist_attachment_name(tmp_path, monkeypatch):
    # Review fix: working_memory is checkpointed to SQLite, so the
    # attachment NAME would persist there long after the file itself
    # expired (10-minute TTL). The name is ephemeral metadata — it
    # must be stripped from the saved copy, while the live
    # working_memory keeps it for the planner.
    from app import db
    from app.loop_checkpoints import _save_checkpoint_defensive

    db_path = str(tmp_path / "attach-checkpoint.db")
    monkeypatch.setenv("PRIVAGENT_DB_PATH", db_path)
    working_memory = {
        "instruction": "Upload the attached file",
        "attachment": {"fileName": "secret-report.pdf", "kind": "pdf"},
    }
    _save_checkpoint_defensive(
        "test-attach-ckpt", "conv-1", "sess-1", 1, "http://x/", [], working_memory
    )
    saved = db.get_task_checkpoint("test-attach-ckpt")
    assert saved is not None, "checkpoint was not saved"
    assert "attachment" not in saved["working_memory"], (
        "attachment metadata persisted in the SQLite checkpoint: "
        f"{saved['working_memory'].get('attachment')}"
    )
    assert saved["working_memory"].get("instruction") == "Upload the attached file", (
        "stripping the attachment also dropped the instruction"
    )
    # The live working_memory is untouched — the planner still sees it.
    assert working_memory.get("attachment") == {"fileName": "secret-report.pdf", "kind": "pdf"}, (
        "checkpointing mutated the live working_memory"
    )
