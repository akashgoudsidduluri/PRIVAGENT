"""Task 13 — encrypted-row failure mode (Task 2 design flaw).

A row encrypted under a lost/rotated master key used to make
``list_facts``/``list_skills`` raise, so the whole list endpoint
500'd. Now undecryptable rows are skipped (counted + logged, never
their content) and the endpoint returns the decryptable rows.
Fail-closed is preserved for writes; only the LIST read degrades
gracefully.
"""
from __future__ import annotations

import logging
import time
import uuid

from cryptography.fernet import Fernet

from app import db
from app.long_memory import (
    create_fact,
    create_skill,
    list_facts,
    list_facts_with_skips,
    list_skills,
    list_skills_with_skips,
    recall_facts,
    recall_skills,
)

BAD_FACT_TEXT = "SECRET-BAD-ROW-VALUE-12345 must never appear in logs"
BAD_SKILL_STEPS = "SECRET-BAD-SKILL-VALUE-67890 must never appear in logs"


def _other_key_ciphertext(plaintext: str) -> str:
    """Encrypt under a DIFFERENT master key than the store uses —
    exactly the lost/rotated-key situation."""
    other = Fernet(Fernet.generate_key())
    return "enc:v1:" + other.encrypt(plaintext.encode("utf-8")).decode("ascii")


def _insert_bad_fact(path: str) -> str:
    row_id = uuid.uuid4().hex
    now = time.time()
    db.init_db(path)
    with db.get_connection(path) as conn:
        conn.execute(
            "INSERT INTO long_memory_facts(id, text, category, source, confidence, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (row_id, _other_key_ciphertext(BAD_FACT_TEXT), "general", "user", 0.9, now, now),
        )
        conn.commit()
    return row_id


def _insert_bad_skill(path: str) -> str:
    row_id = uuid.uuid4().hex
    now = time.time()
    db.init_db(path)
    with db.get_connection(path) as conn:
        conn.execute(
            "INSERT INTO long_memory_skills(id, name, steps, domain, version, success_count, source, created_at, updated_at)"
            " VALUES(?,?,?,?,1,0,?,?,?)",
            (row_id, "bad skill", _other_key_ciphertext(BAD_SKILL_STEPS), "general", "user", now, now),
        )
        conn.commit()
    return row_id


def test_fact_row_encrypted_under_other_key_is_skipped(tmp_path, caplog):
    path = str(tmp_path / "lm.db")
    create_fact("my home airport is DEL", category="travel", db_path=path)
    create_fact("i prefer aisle seats", category="travel", db_path=path)
    bad_id = _insert_bad_fact(path)

    with caplog.at_level(logging.WARNING, logger="app.long_memory"):
        rows, skipped = list_facts_with_skips(db_path=path)
    assert skipped == 1
    assert sorted(r["text"] for r in rows) == [
        "i prefer aisle seats", "my home airport is DEL"]
    # The plain list call never raises and returns the good rows.
    assert sorted(r["text"] for r in list_facts(db_path=path)) == [
        "i prefer aisle seats", "my home airport is DEL"]
    # Recall still works off the decryptable rows.
    assert any(r["text"] == "my home airport is DEL" for r in recall_facts("airport", db_path=path))
    # The skip is logged with the row id only — never the row content.
    assert bad_id in caplog.text
    assert BAD_FACT_TEXT not in caplog.text


def test_skill_row_encrypted_under_other_key_is_skipped(tmp_path, caplog):
    path = str(tmp_path / "lm.db")
    create_skill("book a flight", "1. open the airline site", domain="travel", db_path=path)
    bad_id = _insert_bad_skill(path)

    with caplog.at_level(logging.WARNING, logger="app.long_memory"):
        rows, skipped = list_skills_with_skips(db_path=path)
    assert skipped == 1
    assert len(rows) == 1 and rows[0]["name"] == "book a flight"
    assert len(list_skills(db_path=path)) == 1
    assert any(s["name"] == "book a flight" for s in recall_skills("flight", db_path=path))
    assert bad_id in caplog.text
    assert BAD_SKILL_STEPS not in caplog.text


def test_all_rows_bad_returns_empty_list_not_500(tmp_path):
    path = str(tmp_path / "lm.db")
    _insert_bad_fact(path)
    _insert_bad_skill(path)
    rows, skipped = list_facts_with_skips(db_path=path)
    assert rows == [] and skipped == 1
    rows, skipped = list_skills_with_skips(db_path=path)
    assert rows == [] and skipped == 1


def test_api_list_endpoints_report_skip_count(authed_client, tmp_path, monkeypatch):
    monkeypatch.setenv("PRIVAGENT_DB_PATH", str(tmp_path / "api.db"))
    path = str(tmp_path / "api.db")
    res = authed_client.post("/api/memory/long/facts", json={
        "text": "my home airport is DEL", "category": "travel"})
    assert res.status_code == 201
    _insert_bad_fact(path)
    res = authed_client.get("/api/memory/long/facts")
    assert res.status_code == 200
    assert res.json()["skipped_undecryptable"] == 1
    assert any(f["text"] == "my home airport is DEL" for f in res.json()["facts"])

    res = authed_client.post("/api/memory/long/skills", json={
        "name": "book a flight", "steps": "1. open the airline site", "domain": "travel"})
    assert res.status_code == 201
    _insert_bad_skill(path)
    res = authed_client.get("/api/memory/long/skills")
    assert res.status_code == 200
    assert res.json()["skipped_undecryptable"] == 1
    assert any(s["name"] == "book a flight" for s in res.json()["skills"])


# ---------------------------------------------------------------------------
# REVIEW (Task 13 audit) — Task 13 fixed the undecryptable-row 500 only
# in long_memory. The same ValueError escaped from every other list
# read, so one bad row 500'd GET /api/chat/{id} (get_messages /
# get_chat_memory), GET /api/memory/facts (SemanticMemory.recall) and
# the email dashboard (get_email_classifications /
# get_email_notifications). These tests pin the same per-row skip
# (counted + logged with the row id only, never the value) there.
# ---------------------------------------------------------------------------

BAD_MSG_CONTENT = "SECRET-BAD-MSG-VALUE-11111 must never appear in logs"
BAD_CHATMEM_VALUE = "SECRET-BAD-CHATMEM-VALUE-22222 must never appear in logs"
BAD_SEMANTIC_VALUE = "SECRET-BAD-SEMANTIC-VALUE-33333 must never appear in logs"
BAD_EMAIL_VALUE = "SECRET-BAD-EMAIL-VALUE-44444 must never appear in logs"


def test_two_bad_fact_rows_are_skipped(tmp_path):
    path = str(tmp_path / "lm2.db")
    create_fact("good fact one", category="general", db_path=path)
    create_fact("good fact two", category="general", db_path=path)
    _insert_bad_fact(path)
    _insert_bad_fact(path)
    rows, skipped = list_facts_with_skips(db_path=path)
    assert skipped == 2
    assert sorted(r["text"] for r in rows) == ["good fact one", "good fact two"]


def test_two_bad_skill_rows_are_skipped(tmp_path):
    path = str(tmp_path / "lm2.db")
    create_skill("good skill", "1. do the thing", domain="travel", db_path=path)
    _insert_bad_skill(path)
    _insert_bad_skill(path)
    rows, skipped = list_skills_with_skips(db_path=path)
    assert skipped == 2
    assert len(rows) == 1 and rows[0]["name"] == "good skill"


def test_semantic_recall_skips_bad_row(tmp_path, caplog):
    from app.memory.tiers import SemanticMemory

    path = str(tmp_path / "sem.db")
    mem = SemanticMemory()
    mem.remember("personal", "home_airport", "DEL", db_path=path)
    bad_id = uuid.uuid4().hex
    db.init_db(path)
    with db.get_connection(path) as conn:
        conn.execute(
            "INSERT INTO user_semantic_facts(id,category,fact_key,fact_value,created_at,expires_at)"
            " VALUES(?,?,?,?,?,?)",
            (bad_id, "personal", "bad_key", _other_key_ciphertext(BAD_SEMANTIC_VALUE),
             time.time(), time.time() + 86400),
        )
        conn.commit()
    with caplog.at_level(logging.WARNING, logger="app.memory.tiers"):
        rows = mem.recall(db_path=path)
    assert len(rows) == 1 and rows[0]["fact_value"] == "DEL"
    assert bad_id in caplog.text
    assert BAD_SEMANTIC_VALUE not in caplog.text


def test_chat_messages_and_memory_skip_bad_rows(tmp_path, caplog):
    path = str(tmp_path / "chat.db")
    db.init_db(path)
    chat = db.create_chat("t", db_path=path)
    cid = chat["id"]
    db.add_message(cid, "user", "hello good", db_path=path)
    db.set_chat_memory(cid, "good_key", "good value", db_path=path)
    bad_msg_id = uuid.uuid4().hex
    with db.get_connection(path) as conn:
        conn.execute(
            "INSERT INTO messages(id,chat_id,role,content,evidence,task_id) VALUES(?,?,?,?,?,?)",
            (bad_msg_id, cid, "assistant", _other_key_ciphertext(BAD_MSG_CONTENT), "", None),
        )
        conn.execute(
            "INSERT INTO chat_memory(chat_id,key,value) VALUES(?,?,?)",
            (cid, "bad_key", _other_key_ciphertext(BAD_CHATMEM_VALUE)),
        )
        conn.commit()
    with caplog.at_level(logging.WARNING, logger="app.db"):
        msgs = db.get_messages(cid, db_path=path)
        recent = db.get_recent_messages(cid, db_path=path)
        mem = db.get_chat_memory(cid, db_path=path)
    assert [m["content"] for m in msgs] == ["hello good"]
    assert [m["content"] for m in recent] == ["hello good"]
    assert mem == {"good_key": "good value"}
    assert bad_msg_id in caplog.text
    assert BAD_MSG_CONTENT not in caplog.text
    assert BAD_CHATMEM_VALUE not in caplog.text


def test_null_evidence_message_is_not_dropped(tmp_path):
    # A NULL evidence column is legitimate (add_message stores None
    # when no evidence is given) — the skip must fire only on a real
    # decrypt failure, not on a NULL. Regression guard for the
    # sentinel in db._decrypt_or_none.
    path = str(tmp_path / "chat2.db")
    db.init_db(path)
    chat = db.create_chat("t", db_path=path)
    cid = chat["id"]
    db.add_message(cid, "user", "hello", db_path=path)
    assert len(db.get_messages(cid, db_path=path)) == 1


def _insert_email_mission(path: str) -> str:
    mission_id = uuid.uuid4().hex
    db.init_db(path)
    with db.get_connection(path) as conn:
        now = time.time()
        conn.execute(
            "INSERT INTO email_monitoring_missions(id,conversation_id,account_email,provider,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (mission_id, None, "user@example.com", "gmail", now, now),
        )
        conn.commit()
    return mission_id


def _insert_email_classification(path: str, mission_id: str, message_id: str, sender: str) -> None:
    from app import secrets as secrets_store

    now = time.time()
    with db.get_connection(path) as conn:
        conn.execute(
            "INSERT INTO email_classifications(id,mission_id,message_id,thread_id,sender,recipient,"
            "subject,snippet,received_at,category,reasoning,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, mission_id, message_id, "t1", sender,
             secrets_store.encrypt_text("rcpt@example.com"),
             secrets_store.encrypt_text("subject"), secrets_store.encrypt_text("snippet"),
             now, "important", secrets_store.encrypt_text("reason"), now, now),
        )
        conn.commit()


def _insert_email_notification(path: str, mission_id: str, message_id: str, sender: str) -> None:
    now = time.time()
    with db.get_connection(path) as conn:
        conn.execute(
            "INSERT INTO email_notifications(id,mission_id,message_id,sender,subject,"
            "importance_reason,neglect_reason,received_at,recommended_action,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, mission_id, message_id, sender,
             "subject", "imp reason", "neg reason", now, "Review and respond", now),
        )
        conn.commit()


def test_email_lists_skip_bad_rows(tmp_path, caplog):
    path = str(tmp_path / "email.db")
    mission_id = _insert_email_mission(path)
    _insert_email_classification(path, mission_id, "m-good", "good@example.com")
    _insert_email_classification(path, mission_id, "m-bad", _other_key_ciphertext(BAD_EMAIL_VALUE))
    _insert_email_notification(path, mission_id, "n-good", "good@example.com")
    _insert_email_notification(path, mission_id, "n-bad", _other_key_ciphertext(BAD_EMAIL_VALUE))
    with caplog.at_level(logging.WARNING, logger="app.db"):
        classes = db.get_email_classifications(mission_id, db_path=path)
        notifs = db.get_email_notifications(mission_id, db_path=path)
    assert [c["message_id"] for c in classes] == ["m-good"]
    assert [n["message_id"] for n in notifs] == ["n-good"]
    assert "m-bad" in caplog.text and "n-bad" in caplog.text
    assert BAD_EMAIL_VALUE not in caplog.text


def test_api_chat_and_semantic_facts_survive_bad_rows(authed_client, tmp_path, monkeypatch):
    monkeypatch.setenv("PRIVAGENT_DB_PATH", str(tmp_path / "api2.db"))
    path = str(tmp_path / "api2.db")
    db.init_db(path)
    res = authed_client.post("/api/chat", json={"title": "t"})
    assert res.status_code == 200
    cid = res.json()["chatId"]
    db.add_message(cid, "user", "hello good", db_path=path)
    db.set_chat_memory(cid, "good_key", "good value", db_path=path)
    bad_msg_id = uuid.uuid4().hex
    with db.get_connection(path) as conn:
        conn.execute(
            "INSERT INTO messages(id,chat_id,role,content,evidence,task_id) VALUES(?,?,?,?,?,?)",
            (bad_msg_id, cid, "assistant", _other_key_ciphertext(BAD_MSG_CONTENT), "", None),
        )
        conn.execute(
            "INSERT INTO chat_memory(chat_id,key,value) VALUES(?,?,?)",
            (cid, "bad_key", _other_key_ciphertext(BAD_CHATMEM_VALUE)),
        )
        conn.commit()
    res = authed_client.get(f"/api/chat/{cid}")
    assert res.status_code == 200
    body = res.json()
    assert [m["content"] for m in body["messages"]] == ["hello good"]
    assert body["memory"] == {"good_key": "good value"}

    from app.memory.tiers import SemanticMemory

    SemanticMemory().remember("personal", "home_airport", "DEL", db_path=path)
    bad_id = uuid.uuid4().hex
    with db.get_connection(path) as conn:
        conn.execute(
            "INSERT INTO user_semantic_facts(id,category,fact_key,fact_value,created_at,expires_at)"
            " VALUES(?,?,?,?,?,?)",
            (bad_id, "personal", "bad_key", _other_key_ciphertext(BAD_SEMANTIC_VALUE),
             time.time(), time.time() + 86400),
        )
        conn.commit()
    res = authed_client.get("/api/memory/facts")
    assert res.status_code == 200
    assert [f["fact_value"] for f in res.json()] == ["DEL"]
