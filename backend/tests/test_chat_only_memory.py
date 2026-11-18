"""Chat-only memory entry (2026-11-18): the "Remember a fact" form is
gone, so facts are learned from the conversation. A "remember ..."
instruction stores the fact directly (same store, same PII/credential
rejection as the form) and confirms in chat. Recall questions and
"remember to ..." reminders are not store instructions.
"""
import pytest

from app import db
from app.memory_companion import (
    handle_remember_instruction,
    is_remember_directive,
)
from app.memory.tiers import SemanticMemory


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = str(tmp_path / "chat-memory.sqlite3")
    monkeypatch.setenv("PRIVAGENT_DB_PATH", path)
    db.init_db()
    return path


def test_remember_directive_detection():
    assert is_remember_directive("remember my preferred language is Python")
    assert is_remember_directive("remember that my preferred language is Python")
    assert is_remember_directive("Please remember my preferred language is Python")
    assert is_remember_directive("remember my favourite store is Amazon")
    assert is_remember_directive("remember: preferred language = Python")
    assert is_remember_directive("remember I like dark mode")
    # Recall questions ask what is already known — not a store.
    assert not is_remember_directive("do you remember my preferred language?")
    assert not is_remember_directive("what do you remember about me?")
    assert not is_remember_directive("can you remember this for later?")
    # "remember to ..." is a reminder, not a fact.
    assert not is_remember_directive("remember to check the price tomorrow")
    assert not is_remember_directive("remember to buy milk")
    # Bare "remember" with nothing attached is not storable.
    assert not is_remember_directive("remember")
    assert not is_remember_directive("remember this")
    assert not is_remember_directive("what is the price of iPhone 16")
    assert not is_remember_directive("")


def test_remember_stores_fact_and_confirms(database):
    answer = handle_remember_instruction(
        "remember my preferred language is Python", db_path=database)
    assert "preferred language" in answer.lower()
    assert "Python" in answer
    assert "90 days" in answer
    facts = SemanticMemory().recall(db_path=database)
    assert len(facts) == 1
    assert facts[0]["fact_key"] == "preferred language"
    assert facts[0]["fact_value"] == "Python"
    assert facts[0]["category"] == "coding"


def test_remember_infers_category(database):
    handle_remember_instruction("remember my favourite store is Amazon", db_path=database)
    handle_remember_instruction("remember my hometown is Hyderabad", db_path=database)
    by_key = {f["fact_key"]: f for f in SemanticMemory().recall(db_path=database)}
    assert by_key["favourite store"]["category"] == "retail"
    assert by_key["hometown"]["category"] == "personal"


def test_remember_colon_and_fallback_forms(database):
    handle_remember_instruction("remember: preferred language = Python", db_path=database)
    handle_remember_instruction("remember I like dark mode", db_path=database)
    by_key = {f["fact_key"]: f for f in SemanticMemory().recall(db_path=database)}
    assert by_key["preferred language"]["fact_value"] == "Python"
    assert by_key["Note"]["fact_value"] == "I like dark mode"


def test_remember_rejects_credentials_and_pii(database):
    answer = handle_remember_instruction(
        "remember my password is hunter2-secret", db_path=database)
    assert "can't store" in answer.lower()
    assert SemanticMemory().recall(db_path=database) == []
    answer = handle_remember_instruction(
        "remember my card number is 4111111111111111", db_path=database)
    assert "can't store" in answer.lower()
    assert SemanticMemory().recall(db_path=database) == []


def test_remember_updates_existing_fact(database):
    handle_remember_instruction("remember my preferred language is Python", db_path=database)
    handle_remember_instruction("remember my preferred language is Go", db_path=database)
    facts = SemanticMemory().recall(db_path=database)
    assert len(facts) == 1
    assert facts[0]["fact_value"] == "Go"


def test_agent_loop_routes_remember_directive():
    # The chat path only works if the agent loop intercepts "remember"
    # before the browser task / LLM provider, like the monitoring
    # directive. Static check: the branch must exist in run_task.
    # Resolved from this test file, not the cwd — pytest runs from
    # both the repo root and backend/.
    from pathlib import Path
    loop_path = Path(__file__).resolve().parent.parent / "app" / "agentLoop.py"
    source = loop_path.read_text(encoding="utf-8")
    assert "is_remember_directive(record.instruction)" in source
    assert "handle_remember_instruction(record.instruction)" in source


@pytest.mark.asyncio
async def test_recall_question_reaches_stored_fact(monkeypatch, tmp_path):
    """A recall question must be answered from the stored fact. The
    conversational fast-path used to call chat_call with only the
    instruction and history — the recalled semantic facts (loaded into
    initial_memory by run_task) never reached the model, so "do you
    remember my preferred language?" could only get a generic reply.
    The fix appends the facts to the system prompt. Verified live
    against deepseek-v4.1-flash (2026-11-18): with the facts in the
    system prompt it answers "Python"; for an unstored fact it says
    it does not remember, without inventing one."""
    import asyncio

    from app import agentLoop
    from app.agentLoop import real_hub, run_task
    from app.hub import TaskRecord

    temp_db = str(tmp_path / "recall.sqlite3")
    db.init_db(db_path=temp_db)
    old_init_db = db.init_db
    original_get_conn = db.get_connection
    db.init_db = lambda *args, **kwargs: None
    db.get_connection = lambda p=None: original_get_conn(temp_db)
    try:
        SemanticMemory().remember("coding", "preferred language", "Python", db_path=temp_db)
        assert SemanticMemory().recall(db_path=temp_db), "fact was not stored"

        chat = db.create_chat(title="recall", db_path=temp_db)
        task_id = "task_recall_python"
        record = TaskRecord(
            task_id=task_id,
            instruction="do you remember my preferred language?",
            state="created",
            chat_id=chat["id"],
        )
        real_hub.tasks[task_id] = record

        seen: dict = {}

        class RecordingProvider:
            def chat_call(self, prompt, history=None, system_prompt=None):
                seen["prompt"] = prompt
                seen["system_prompt"] = system_prompt
                # Echo what the model would see: the fact must be in the
                # system prompt for the answer to be possible.
                return "Python" if system_prompt and "Python" in system_prompt else "I don't remember."

        monkeypatch.setattr(agentLoop, "_provider_for_config", lambda: RecordingProvider())

        await run_task(task_id)
        await asyncio.sleep(0.05)
        assert record.state == "completed", record.message
        assert seen.get("system_prompt"), "chat_call got no system prompt"
        assert "preferred language = Python" in seen["system_prompt"]
        assert "Python" in (record.message or "")
    finally:
        db.init_db = old_init_db
        db.get_connection = original_get_conn
