"""Persistent SQLite storage for multi-turn chats, messages, and session memory."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional

DB_DIR = Path(__file__).resolve().parents[1] / "data"


def get_default_db_path() -> Path:
    env_override = os.environ.get("PRIVAGENT_DB_PATH")
    if env_override:
        return Path(env_override)
    if "pytest" in sys.modules or os.environ.get("PYTEST_CURRENT_TEST"):
        return DB_DIR / "test_privagent.db"
    return DB_DIR / "privagent.db"


DEFAULT_DB_PATH = DB_DIR / "privagent.db"


def _get_db_path(override_path: Optional[str] = None) -> str:
    if override_path:
        return override_path
    DB_DIR.mkdir(parents=True, exist_ok=True)
    return str(get_default_db_path())


def get_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = _get_db_path(db_path)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db(db_path: Optional[str] = None) -> None:
    """Initialize SQLite tables if they do not exist."""
    with get_connection(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS chats (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'active'
            );

            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                chat_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                evidence TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS chat_memory (
                chat_id TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                PRIMARY KEY (chat_id, key),
                FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS user_preferences (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS user_semantic_facts (
                id TEXT PRIMARY KEY, category TEXT NOT NULL CHECK(category IN ('personal','retail','coding')),
                fact_key TEXT NOT NULL, fact_value TEXT NOT NULL, created_at REAL NOT NULL,
                expires_at REAL NOT NULL, UNIQUE(category, fact_key)
            );

            CREATE TABLE IF NOT EXISTS browser_sessions (
                session_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                origin TEXT NOT NULL,
                primary_tab_id INTEGER,
                child_tab_ids TEXT DEFAULT '[]',
                status TEXT NOT NULL,
                created_at REAL NOT NULL,
                last_activity_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS task_checkpoints (
                task_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                session_id TEXT,
                step_index INTEGER NOT NULL,
                last_url TEXT,
                history TEXT NOT NULL,
                working_memory TEXT NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS artifacts (
                artifact_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                filepath TEXT NOT NULL,
                file_format TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                file_size_bytes INTEGER NOT NULL,
                title TEXT NOT NULL,
                summary TEXT,
                metadata TEXT NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS research_packages (
                package_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                topic_title TEXT NOT NULL,
                status TEXT NOT NULL,
                data_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS monitoring_missions (
                id TEXT PRIMARY KEY, conversation_id TEXT, title TEXT NOT NULL,
                target_url TEXT NOT NULL, trigger_type TEXT NOT NULL DEFAULT 'interval',
                schedule_cron TEXT, interval_seconds INTEGER NOT NULL CHECK(interval_seconds >= 60),
                condition_type TEXT NOT NULL DEFAULT 'text_change', condition_params TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'paused', last_run_at REAL, next_run_at REAL,
                last_state_hash TEXT, last_state_json TEXT, last_alert_hash TEXT, last_alert_at REAL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS monitoring_events (
                id TEXT PRIMARY KEY, mission_id TEXT NOT NULL REFERENCES monitoring_missions(id) ON DELETE CASCADE,
                task_id TEXT, detected_at REAL NOT NULL, title TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL, diff_data TEXT NOT NULL DEFAULT '{}',
                notification_sent INTEGER NOT NULL DEFAULT 0, acknowledged INTEGER NOT NULL DEFAULT 0,
                state_hash TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS mcp_audit (
                id TEXT PRIMARY KEY, tool_name TEXT NOT NULL, input_hash TEXT NOT NULL,
                latency_ms INTEGER NOT NULL, status TEXT NOT NULL, output_hash TEXT NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_messages_chat_id ON messages(chat_id);
            CREATE INDEX IF NOT EXISTS idx_semantic_expiry ON user_semantic_facts(expires_at);
            CREATE INDEX IF NOT EXISTS idx_chats_updated_at ON chats(updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_sessions_conv ON browser_sessions(conversation_id, last_activity_at DESC);
            CREATE INDEX IF NOT EXISTS idx_checkpoints_conv ON task_checkpoints(conversation_id, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_artifacts_task ON artifacts(task_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_artifacts_conv ON artifacts(conversation_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_packages_conv ON research_packages(conversation_id, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_packages_task ON research_packages(task_id, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_monitor_due ON monitoring_missions(status, next_run_at);
            CREATE INDEX IF NOT EXISTS idx_monitor_events ON monitoring_events(mission_id, detected_at DESC);
            CREATE INDEX IF NOT EXISTS idx_mcp_audit_created ON mcp_audit(created_at DESC);
            """
        )
        # Earlier control-center previews created narrower tables. Add only
        # missing columns so persisted missions and events survive upgrades.
        migrations = {
            "monitoring_missions": {
                "conversation_id": "TEXT", "trigger_type": "TEXT NOT NULL DEFAULT 'interval'",
                "schedule_cron": "TEXT", "condition_type": "TEXT NOT NULL DEFAULT 'text_change'",
                "condition_params": "TEXT NOT NULL DEFAULT '{}'", "last_state_json": "TEXT",
                "last_alert_hash": "TEXT", "last_alert_at": "REAL",
            },
            "monitoring_events": {
                "task_id": "TEXT", "title": "TEXT NOT NULL DEFAULT ''",
                "diff_data": "TEXT NOT NULL DEFAULT '{}'", "notification_sent": "INTEGER NOT NULL DEFAULT 0",
                "state_hash": "TEXT NOT NULL DEFAULT ''",
            },
        }
        for table, columns in migrations.items():
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            for name, spec in columns.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {spec}")


def create_chat(title: Optional[str] = None, db_path: Optional[str] = None) -> dict[str, Any]:
    from .privacy import scrub_persistent
    chat_id = uuid.uuid4().hex
    display_title = scrub_persistent((title or "New Mission").strip())
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO chats (id, title, status) VALUES (?, ?, 'active')",
            (chat_id, display_title),
        )
    return get_chat(chat_id, db_path=db_path) or {"id": chat_id, "title": display_title}


def list_chats(db_path: Optional[str] = None) -> list[dict[str, Any]]:
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, title, created_at, updated_at, status
            FROM chats
            ORDER BY updated_at DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]


def get_chat(chat_id: str, db_path: Optional[str] = None) -> Optional[dict[str, Any]]:
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT id, title, created_at, updated_at, status FROM chats WHERE id = ?",
            (chat_id,),
        ).fetchone()
        if not row:
            return None
        chat = dict(row)
        chat["messages"] = get_messages(chat_id, limit=50, db_path=db_path)
        chat["memory"] = get_chat_memory(chat_id, db_path=db_path)
        return chat


def delete_chat(chat_id: str, db_path: Optional[str] = None) -> bool:
    with get_connection(db_path) as conn:
        cur = conn.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
        return cur.rowcount > 0


def update_chat_title(chat_id: str, title: str, db_path: Optional[str] = None) -> None:
    from .privacy import scrub_persistent
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE chats SET title = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (scrub_persistent(title.strip()), chat_id),
        )


def touch_chat(chat_id: str, db_path: Optional[str] = None) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE chats SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (chat_id,),
        )


def add_message(
    chat_id: str,
    role: str,
    content: str,
    evidence: Optional[str] = None,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    from .privacy import scrub_persistent
    content = scrub_persistent(content)
    evidence = scrub_persistent(evidence)
    msg_id = uuid.uuid4().hex
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO messages (id, chat_id, role, content, evidence)
            VALUES (?, ?, ?, ?, ?)
            """,
            (msg_id, chat_id, role, content, evidence),
        )
        conn.execute(
            "UPDATE chats SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (chat_id,),
        )
    return {
        "id": msg_id,
        "chat_id": chat_id,
        "role": role,
        "content": content,
        "evidence": evidence,
    }


def get_messages(chat_id: str, limit: int = 20, db_path: Optional[str] = None) -> list[dict[str, Any]]:
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, chat_id, role, content, evidence, created_at
            FROM messages
            WHERE chat_id = ?
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (chat_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def set_chat_memory(chat_id: str, key: str, value: str, db_path: Optional[str] = None) -> None:
    from .privacy import scrub_persistent
    value = scrub_persistent(str(value), key)
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO chat_memory (chat_id, key, value)
            VALUES (?, ?, ?)
            ON CONFLICT(chat_id, key) DO UPDATE SET value = excluded.value
            """,
            (chat_id, key.strip(), str(value).strip()),
        )


def get_chat_memory(chat_id: str, db_path: Optional[str] = None) -> dict[str, str]:
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT key, value FROM chat_memory WHERE chat_id = ?",
            (chat_id,),
        ).fetchall()
        return {r["key"]: r["value"] for r in rows}


def delete_chat_memory(chat_id: str, key: str, db_path: Optional[str] = None) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            "DELETE FROM chat_memory WHERE chat_id = ? AND key = ?",
            (chat_id, key),
        )


def get_user_preferences(db_path: Optional[str] = None) -> dict[str, str]:
    with get_connection(db_path) as conn:
        rows = conn.execute("SELECT key, value FROM user_preferences ORDER BY key ASC;").fetchall()
        return {r["key"]: r["value"] for r in rows}


def set_user_preference(key: str, value: str, db_path: Optional[str] = None) -> None:
    from .privacy import classify_sensitivity, Sensitivity
    if classify_sensitivity(str(value), key) >= Sensitivity.RESTRICTED_PII:
        raise ValueError("sensitive facts cannot be stored as preferences")
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO user_preferences (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
            """,
            (key.strip().lower(), str(value).strip()),
        )
        conn.commit()


def delete_user_preference(key: str, db_path: Optional[str] = None) -> bool:
    with get_connection(db_path) as conn:
        cur = conn.execute("DELETE FROM user_preferences WHERE key = ?;", (key.strip().lower(),))
        conn.commit()
        return cur.rowcount > 0


def save_browser_session(session_dict: dict[str, Any], db_path: Optional[str] = None) -> None:
    sid = session_dict.get("session_id") or session_dict.get("sessionId")
    tid = session_dict.get("task_id") or session_dict.get("taskId")
    cid = session_dict.get("conversation_id") or session_dict.get("conversationId") or ""
    origin = session_dict.get("origin") or "new_task"
    primary_tab_id = session_dict.get("primary_tab_id") if "primary_tab_id" in session_dict else session_dict.get("primaryTabId")
    child_tab_ids = session_dict.get("child_tab_ids") or session_dict.get("childTabIds") or []
    status = session_dict.get("status") or "active"
    created_at = session_dict.get("created_at") or session_dict.get("createdAt") or time.time()
    last_act = session_dict.get("last_activity_at") or session_dict.get("lastActivityAt") or time.time()

    child_json = json.dumps(list(child_tab_ids)) if isinstance(child_tab_ids, (list, set)) else str(child_tab_ids)

    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO browser_sessions (session_id, task_id, conversation_id, origin, primary_tab_id, child_tab_ids, status, created_at, last_activity_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                task_id = excluded.task_id,
                primary_tab_id = excluded.primary_tab_id,
                child_tab_ids = excluded.child_tab_ids,
                status = excluded.status,
                last_activity_at = excluded.last_activity_at
            """,
            (sid, tid, cid, origin, primary_tab_id, child_json, status, created_at, last_act),
        )
        conn.commit()


def get_browser_session(session_id: str, db_path: Optional[str] = None) -> Optional[dict[str, Any]]:
    with get_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM browser_sessions WHERE session_id = ?", (session_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["child_tab_ids"] = json.loads(d.get("child_tab_ids") or "[]")
        except Exception:
            d["child_tab_ids"] = []
        return d


def get_latest_active_session_for_chat(conversation_id: str, db_path: Optional[str] = None) -> Optional[dict[str, Any]]:
    with get_connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT * FROM browser_sessions
            WHERE conversation_id = ? AND status IN ('active', 'completed')
            ORDER BY last_activity_at DESC
            LIMIT 1
            """,
            (conversation_id,),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["child_tab_ids"] = json.loads(d.get("child_tab_ids") or "[]")
        except Exception:
            d["child_tab_ids"] = []
        return d


def _json_serializable(obj: Any) -> Any:
    """Recursively convert Pydantic models, dataclasses, sets, and custom types to serializable primitives."""
    if hasattr(obj, "model_dump"):
        return _json_serializable(obj.model_dump())
    if hasattr(obj, "to_dict"):
        return _json_serializable(obj.to_dict())
    if isinstance(obj, dict):
        return {str(k): _json_serializable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_json_serializable(x) for x in obj]
    return obj


def save_task_checkpoint(
    task_id: str,
    conversation_id: str,
    session_id: Optional[str],
    step_index: int,
    last_url: str,
    history: list[dict[str, Any]],
    working_memory: dict[str, Any],
    db_path: Optional[str] = None,
) -> None:
    from .privacy import scrub_persistent, redact_url
    now = time.time()
    hist_json = json.dumps(scrub_persistent(_json_serializable(history)))
    wm_json = json.dumps(scrub_persistent(_json_serializable(working_memory)))
    last_url = redact_url(last_url)
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO task_checkpoints (task_id, conversation_id, session_id, step_index, last_url, history, working_memory, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                conversation_id = excluded.conversation_id,
                session_id = excluded.session_id,
                step_index = excluded.step_index,
                last_url = excluded.last_url,
                history = excluded.history,
                working_memory = excluded.working_memory,
                updated_at = excluded.updated_at
            """,
            (task_id, conversation_id, session_id, step_index, last_url, hist_json, wm_json, now),
        )
        conn.commit()


def get_task_checkpoint(task_id: str, db_path: Optional[str] = None) -> Optional[dict[str, Any]]:
    with get_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM task_checkpoints WHERE task_id = ?", (task_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["history"] = json.loads(d.get("history") or "[]")
        except Exception:
            d["history"] = []
        try:
            d["working_memory"] = json.loads(d.get("working_memory") or "{}")
        except Exception:
            d["working_memory"] = {}
        return d


def get_latest_checkpoint_for_chat(conversation_id: str, db_path: Optional[str] = None) -> Optional[dict[str, Any]]:
    with get_connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT * FROM task_checkpoints
            WHERE conversation_id = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (conversation_id,),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["history"] = json.loads(d.get("history") or "[]")
        except Exception:
            d["history"] = []
        try:
            d["working_memory"] = json.loads(d.get("working_memory") or "{}")
        except Exception:
            d["working_memory"] = {}
        return d


# ---------------------------------------------------------------------------
# Artifact Metadata Registry and Storage
# ---------------------------------------------------------------------------

def save_artifact(artifact: dict[str, Any], db_path: Optional[str] = None) -> dict[str, Any]:
    """Register or update an artifact deliverable in persistent SQLite storage."""
    from .privacy import scrub_persistent, classify_sensitivity, Sensitivity
    art_id = artifact.get("artifact_id") or uuid.uuid4().hex
    task_id = artifact.get("task_id") or ""
    conversation_id = artifact.get("conversation_id") or ""
    filename = artifact.get("filename") or f"artifact_{art_id}"
    filepath = artifact.get("filepath") or ""
    if any(classify_sensitivity(str(path)) >= Sensitivity.RESTRICTED_PII for path in (filename, filepath)):
        raise ValueError("artifact paths cannot contain credentials or PII")
    file_format = (artifact.get("file_format") or "bin").lower().strip(".")
    mime_type = artifact.get("mime_type") or "application/octet-stream"
    file_size_bytes = int(artifact.get("file_size_bytes") or 0)
    title = scrub_persistent(artifact.get("title") or filename)
    summary = scrub_persistent(artifact.get("summary") or "")
    meta_dict = artifact.get("metadata") or {}
    meta_json = json.dumps(scrub_persistent(_json_serializable(meta_dict)))
    created_at = float(artifact.get("created_at") or time.time())

    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO artifacts (
                artifact_id, task_id, conversation_id, filename, filepath,
                file_format, mime_type, file_size_bytes, title, summary,
                metadata, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(artifact_id) DO UPDATE SET
                task_id = excluded.task_id,
                conversation_id = excluded.conversation_id,
                filename = excluded.filename,
                filepath = excluded.filepath,
                file_format = excluded.file_format,
                mime_type = excluded.mime_type,
                file_size_bytes = excluded.file_size_bytes,
                title = excluded.title,
                summary = excluded.summary,
                metadata = excluded.metadata
            """,
            (
                art_id, task_id, conversation_id, filename, filepath,
                file_format, mime_type, file_size_bytes, title, summary,
                meta_json, created_at,
            ),
        )
        conn.commit()

    return get_artifact(art_id, db_path=db_path) or artifact


def get_artifact(artifact_id: str, db_path: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Retrieve an artifact by its unique ID."""
    with get_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["metadata"] = json.loads(d.get("metadata") or "{}")
        except Exception:
            d["metadata"] = {}
        return d


def list_task_artifacts(task_id: str, db_path: Optional[str] = None) -> list[dict[str, Any]]:
    """Retrieve all artifacts generated for a specific task."""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM artifacts WHERE task_id = ? ORDER BY created_at DESC",
            (task_id,),
        ).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            try:
                d["metadata"] = json.loads(d.get("metadata") or "{}")
            except Exception:
                d["metadata"] = {}
            results.append(d)
        return results


def list_conversation_artifacts(conversation_id: str, db_path: Optional[str] = None) -> list[dict[str, Any]]:
    """Retrieve all artifacts associated with a conversation."""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM artifacts WHERE conversation_id = ? ORDER BY created_at DESC",
            (conversation_id,),
        ).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            try:
                d["metadata"] = json.loads(d.get("metadata") or "{}")
            except Exception:
                d["metadata"] = {}
            results.append(d)
        return results


def delete_artifact(artifact_id: str, db_path: Optional[str] = None) -> bool:
    """Delete an artifact record from the database."""
    with get_connection(db_path) as conn:
        cur = conn.execute("DELETE FROM artifacts WHERE artifact_id = ?", (artifact_id,))
        conn.commit()
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Research Knowledge Package Persistence and Management
# ---------------------------------------------------------------------------

def save_research_package(
    package: dict[str, Any],
    conversation_id: str,
    task_id: str,
    status: str = "completed",
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    """Persist or update a ResearchKnowledgePackage in SQLite."""
    pkg_id = package.get("package_id") or f"pkg_{task_id}"
    topic_obj = package.get("topic") or {}
    topic_title = topic_obj.get("title") if isinstance(topic_obj, dict) else getattr(topic_obj, "title", "Technical Research")
    from .privacy import scrub_persistent
    data_json = json.dumps(scrub_persistent(_json_serializable(package)))
    topic_title = scrub_persistent(str(topic_title))
    now = time.time()
    created_at = float(package.get("created_at") or now)

    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO research_packages (
                package_id, conversation_id, task_id, topic_title,
                status, data_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(package_id) DO UPDATE SET
                conversation_id = excluded.conversation_id,
                task_id = excluded.task_id,
                topic_title = excluded.topic_title,
                status = excluded.status,
                data_json = excluded.data_json,
                updated_at = excluded.updated_at
            """,
            (pkg_id, conversation_id, task_id, str(topic_title), status, data_json, created_at, now),
        )
        conn.commit()
    return get_research_package(pkg_id, db_path=db_path) or package


def get_research_package(package_id: str, db_path: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Retrieve a research package by its unique package_id."""
    with get_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM research_packages WHERE package_id = ?", (package_id,)).fetchone()
        if not row:
            return None
        res = dict(row)
        try:
            res["data"] = json.loads(res["data_json"])
        except Exception:
            res["data"] = {}
        return res


def get_latest_package_for_chat(conversation_id: str, db_path: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Retrieve the most recent research package for a conversation."""
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM research_packages WHERE conversation_id = ? ORDER BY updated_at DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()
        if not row:
            return None
        res = dict(row)
        try:
            res["data"] = json.loads(res["data_json"])
        except Exception:
            res["data"] = {}
        return res


def list_packages_for_chat(conversation_id: str, db_path: Optional[str] = None) -> list[dict[str, Any]]:
    """List all research packages stored for a conversation in reverse chronological order."""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM research_packages WHERE conversation_id = ? ORDER BY updated_at DESC",
            (conversation_id,),
        ).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            try:
                d["data"] = json.loads(d["data_json"])
            except Exception:
                d["data"] = {}
            results.append(d)
        return results



