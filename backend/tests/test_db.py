"""Test SQLite persistence, session isolation, and chat memory."""
import os
import tempfile
import pytest

from app import db


@pytest.fixture
def temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init_db(path)
    yield path
    try:
        os.remove(path)
    except OSError:
        pass


def test_init_and_create_chat(temp_db):
    chat = db.create_chat(title="Test Mission", db_path=temp_db)
    assert chat["id"] is not None
    assert chat["title"] == "Test Mission"
    assert chat["status"] == "active"
    assert chat["messages"] == []
    assert chat["memory"] == {}


def test_list_and_delete_chat(temp_db):
    c1 = db.create_chat(title="Chat 1", db_path=temp_db)
    c2 = db.create_chat(title="Chat 2", db_path=temp_db)
    chats = db.list_chats(db_path=temp_db)
    assert len(chats) == 2
    assert {c["id"] for c in chats} == {c1["id"], c2["id"]}

    deleted = db.delete_chat(c1["id"], db_path=temp_db)
    assert deleted is True
    assert db.get_chat(c1["id"], db_path=temp_db) is None
    assert len(db.list_chats(db_path=temp_db)) == 1


def test_messages_persistence(temp_db):
    chat = db.create_chat(title="Chat With Messages", db_path=temp_db)
    cid = chat["id"]

    msg1 = db.add_message(cid, "user", "Hello agent", db_path=temp_db)
    msg2 = db.add_message(cid, "agent", "Hello user", evidence="navigated to google", db_path=temp_db)

    msgs = db.get_messages(cid, db_path=temp_db)
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"] == "Hello agent"
    assert msgs[1]["role"] == "agent"
    assert msgs[1]["evidence"] == "navigated to google"


def test_recent_messages_returns_latest_turns_in_order(temp_db):
    chat = db.create_chat(title="Long Chat", db_path=temp_db)
    cid = chat["id"]
    for i in range(8):
        db.add_message(cid, "user", f"question {i}", db_path=temp_db)
        db.add_message(cid, "agent", f"answer {i}", db_path=temp_db)

    recent = db.get_recent_messages(cid, limit=6, db_path=temp_db)
    assert [m["content"] for m in recent] == [
        "question 5", "answer 5", "question 6", "answer 6", "question 7", "answer 7",
    ]
    # The oldest-first transcript helper is unchanged for history display.
    assert db.get_messages(cid, limit=2, db_path=temp_db)[0]["content"] == "question 0"


def test_chat_memory_and_isolation(temp_db):
    c1 = db.create_chat(title="Session 1", db_path=temp_db)
    c2 = db.create_chat(title="Session 2", db_path=temp_db)

    db.set_chat_memory(c1["id"], "selected_product", "Realme 15 Pro", db_path=temp_db)
    db.set_chat_memory(c1["id"], "price", "₹24,999", db_path=temp_db)

    db.set_chat_memory(c2["id"], "selected_product", "Yellow Dress", db_path=temp_db)

    mem1 = db.get_chat_memory(c1["id"], db_path=temp_db)
    mem2 = db.get_chat_memory(c2["id"], db_path=temp_db)

    # Strict isolation: Session 1 does not see Session 2 memory
    assert mem1["selected_product"] == "Realme 15 Pro"
    assert mem1["price"] == "₹24,999"

    assert mem2["selected_product"] == "Yellow Dress"
    assert "price" not in mem2


def test_get_chat_rehydrates_checkpoints_and_steps(temp_db):
    chat = db.create_chat(title="Multi-turn Steps Chat", db_path=temp_db)
    cid = chat["id"]
    tid1 = "task-step-1"
    tid2 = "task-step-2"

    db.add_message(cid, "user", "turn 1 query", task_id=tid1, db_path=temp_db)
    db.save_task_checkpoint(
        task_id=tid1,
        conversation_id=cid,
        session_id="sess-1",
        step_index=2,
        last_url="https://site1.test",
        history=[
            {"action": {"type": "navigate"}, "evidence": "https://site1.test", "result_ok": True},
            {"action": {"type": "click"}, "evidence": "clicked button", "result_ok": True},
        ],
        working_memory={},
        db_path=temp_db,
    )
    db.add_message(cid, "agent", "turn 1 finished", evidence="completed (2 steps)", task_id=tid1, db_path=temp_db)

    db.add_message(cid, "user", "continue", task_id=tid2, db_path=temp_db)
    db.save_task_checkpoint(
        task_id=tid2,
        conversation_id=cid,
        session_id="sess-2",
        step_index=1,
        last_url="https://site2.test",
        history=[
            {"action": {"type": "navigate"}, "evidence": "https://site2.test", "result_ok": True},
        ],
        working_memory={},
        db_path=temp_db,
    )
    db.add_message(cid, "agent", "turn 2 finished", evidence="completed (1 steps)", task_id=tid2, db_path=temp_db)

    chat_detail = db.get_chat(cid, db_path=temp_db)
    assert chat_detail is not None
    assert chat_detail["total_steps"] == 3
    messages = chat_detail["messages"]
    assert len(messages) == 4
    agent_msg1 = messages[1]
    assert agent_msg1["role"] == "agent"
    assert "steps" in agent_msg1
    assert len(agent_msg1["steps"]) == 2
    assert agent_msg1["steps"][0]["message"] == "Step 1: navigate -> https://site1.test"
    assert agent_msg1["steps"][1]["message"] == "Step 2: click -> clicked button"

    agent_msg2 = messages[3]
    assert agent_msg2["role"] == "agent"
    assert "steps" in agent_msg2
    assert len(agent_msg2["steps"]) == 1
    assert agent_msg2["steps"][0]["message"] == "Step 1: navigate -> https://site2.test"



def test_user_preferences_vault(temp_db):
    db.set_user_preference("pincode", "500081", db_path=temp_db)
    db.set_user_preference("currency", "INR", db_path=temp_db)
    prefs = db.get_user_preferences(db_path=temp_db)
    assert prefs["pincode"] == "500081"
    assert prefs["currency"] == "INR"

    deleted = db.delete_user_preference("currency", db_path=temp_db)
    assert deleted is True
    prefs_after = db.get_user_preferences(db_path=temp_db)
    assert "currency" not in prefs_after
    assert prefs_after["pincode"] == "500081"


def test_extract_mission_facts():
    from app.agentLoop import extract_mission_facts
    instruction = "Find Realme 15 Pro on Amazon under 25000 to checkout"
    summary = "Found Realme 15 Pro at ₹24,999 on https://www.amazon.in/dp/B0XYZ"
    facts = extract_mission_facts(instruction, summary)

    assert facts["extracted_price"] == "₹24,999"
    assert facts["canonical_url"] == "https://www.amazon.in/dp/B0XYZ"
    assert "Realme 15 Pro" in facts["target_item"]
    assert "under 25000" in facts["core_constraint"]
