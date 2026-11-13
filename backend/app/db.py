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

            CREATE TABLE IF NOT EXISTS email_monitoring_missions (
                id TEXT PRIMARY KEY,
                conversation_id TEXT,
                account_email TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT 'gmail',
                frequency_seconds INTEGER NOT NULL DEFAULT 300 CHECK(frequency_seconds >= 60),
                status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'paused', 'stopped', 'failed')),
                sync_cursor TEXT,
                policy_authorized INTEGER NOT NULL DEFAULT 0,
                dry_run INTEGER NOT NULL DEFAULT 1,
                cleanup_policy_json TEXT NOT NULL DEFAULT '{}',
                notification_policy_json TEXT NOT NULL DEFAULT '{}',
                last_sync_at REAL,
                next_run_at REAL,
                total_processed INTEGER NOT NULL DEFAULT 0,
                unnecessary_detected INTEGER NOT NULL DEFAULT 0,
                moved_to_trash INTEGER NOT NULL DEFAULT 0,
                important_detected INTEGER NOT NULL DEFAULT 0,
                neglected_detected INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS email_classifications (
                id TEXT PRIMARY KEY,
                mission_id TEXT NOT NULL REFERENCES email_monitoring_missions(id) ON DELETE CASCADE,
                message_id TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                sender TEXT NOT NULL,
                recipient TEXT NOT NULL DEFAULT '',
                subject TEXT NOT NULL,
                snippet TEXT NOT NULL DEFAULT '',
                received_at REAL NOT NULL,
                category TEXT NOT NULL CHECK(category IN ('unnecessary', 'important', 'uncertain')),
                subcategory TEXT NOT NULL DEFAULT 'unknown',
                importance_score REAL NOT NULL DEFAULT 0.0,
                unnecessary_confidence REAL NOT NULL DEFAULT 0.0,
                reasoning TEXT NOT NULL,
                deadline TEXT,
                required_action TEXT,
                is_unread INTEGER NOT NULL DEFAULT 1,
                is_replied INTEGER NOT NULL DEFAULT 0,
                action_taken TEXT NOT NULL DEFAULT 'none' CHECK(action_taken IN ('none', 'dry_run_flagged', 'moved_to_trash', 'restored')),
                action_verified INTEGER NOT NULL DEFAULT 0,
                action_timestamp REAL,
                notified INTEGER NOT NULL DEFAULT 0,
                notification_dismissed INTEGER NOT NULL DEFAULT 0,
                raw_headers_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(mission_id, message_id)
            );

            CREATE TABLE IF NOT EXISTS email_audit_log (
                id TEXT PRIMARY KEY,
                mission_id TEXT NOT NULL REFERENCES email_monitoring_missions(id) ON DELETE CASCADE,
                message_id TEXT,
                action TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS email_notifications (
                id TEXT PRIMARY KEY,
                mission_id TEXT NOT NULL REFERENCES email_monitoring_missions(id) ON DELETE CASCADE,
                message_id TEXT NOT NULL,
                sender TEXT NOT NULL,
                subject TEXT NOT NULL,
                importance_reason TEXT NOT NULL,
                neglect_reason TEXT NOT NULL,
                received_at REAL NOT NULL,
                deadline TEXT,
                recommended_action TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'sent', 'dismissed', 'resolved')),
                created_at REAL NOT NULL,
                sent_at REAL,
                resolved_at REAL,
                UNIQUE(mission_id, message_id)
            );

            CREATE INDEX IF NOT EXISTS idx_email_missions_status ON email_monitoring_missions(status, next_run_at);
            CREATE INDEX IF NOT EXISTS idx_email_class_mission ON email_classifications(mission_id, received_at DESC);
            CREATE INDEX IF NOT EXISTS idx_email_class_category ON email_classifications(mission_id, category);
            CREATE INDEX IF NOT EXISTS idx_email_audit_mission ON email_audit_log(mission_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_email_notif_status ON email_notifications(mission_id, status);
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
            "messages": {
                "task_id": "TEXT",
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
        messages = get_messages(chat_id, limit=100, db_path=db_path)
        checkpoints = get_checkpoints_for_chat(chat_id, db_path=db_path)

        def _format_history_steps(hist: list[dict[str, Any]]) -> list[dict[str, Any]]:
            formatted = []
            for idx, h in enumerate(hist):
                action = h.get("action") or {}
                act_type = action.get("type") or "step"
                ev = h.get("evidence") or ""
                display = f"Step {idx + 1}: {act_type} -> {ev}" if ev else f"Step {idx + 1}: {act_type}"
                formatted.append({
                    "stepNum": idx + 1,
                    "message": display,
                    "action": action,
                    "status": h.get("status") or ("verified" if h.get("result_ok") else "failed"),
                    "result_ok": bool(h.get("result_ok")),
                })
            return formatted

        cp_by_task = {cp["task_id"]: cp for cp in checkpoints if cp.get("task_id")}
        agent_msgs = [m for m in messages if m.get("role") == "agent"]
        for idx, m in enumerate(agent_msgs):
            tid = m.get("task_id")
            cp = cp_by_task.get(tid) if tid else None
            if not cp and idx < len(checkpoints):
                cp = checkpoints[idx]
            if cp:
                hist = cp.get("history") or []
                m["steps"] = _format_history_steps(hist)
                if not m.get("task_id"):
                    m["task_id"] = cp.get("task_id")

        chat["messages"] = messages
        chat["memory"] = get_chat_memory(chat_id, db_path=db_path)
        chat["checkpoints"] = checkpoints
        chat["total_steps"] = sum(len(cp.get("history", [])) for cp in checkpoints)
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
    task_id: Optional[str] = None,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    from .privacy import scrub_persistent
    content = scrub_persistent(content)
    evidence = scrub_persistent(evidence)
    msg_id = uuid.uuid4().hex
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO messages (id, chat_id, role, content, evidence, task_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (msg_id, chat_id, role, content, evidence, task_id),
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
        "task_id": task_id,
    }


def get_messages(chat_id: str, limit: int = 50, db_path: Optional[str] = None) -> list[dict[str, Any]]:
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, chat_id, role, content, evidence, task_id, created_at
            FROM messages
            WHERE chat_id = ?
            ORDER BY created_at ASC, rowid ASC
            LIMIT ?
            """,
            (chat_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_recent_messages(chat_id: str, limit: int = 6, db_path: Optional[str] = None) -> list[dict[str, Any]]:
    """Return the most recent messages for a chat in chronological order.

    ``get_messages`` keeps the oldest rows when a limit is supplied, which is
    right for a paginated transcript but wrong for conversational context: a
    long chat would replay its first turns forever.
    """
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, chat_id, role, content, evidence, created_at
            FROM messages
            WHERE chat_id = ?
            ORDER BY created_at DESC, rowid DESC
            LIMIT ?
            """,
            (chat_id, max(1, int(limit))),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]


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


def get_checkpoints_for_chat(conversation_id: str, db_path: Optional[str] = None) -> list[dict[str, Any]]:
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM task_checkpoints
            WHERE conversation_id = ?
            ORDER BY updated_at ASC, rowid ASC
            """,
            (conversation_id,),
        ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            try:
                d["history"] = json.loads(d.get("history") or "[]")
            except Exception:
                d["history"] = []
            try:
                d["working_memory"] = json.loads(d.get("working_memory") or "{}")
            except Exception:
                d["working_memory"] = {}
            result.append(d)
        return result


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


# ---------------------------------------------------------------------------
# Email Intelligence Persistence and Audit Logging
# ---------------------------------------------------------------------------

def create_email_mission(
    account_email: str,
    provider: str = "gmail",
    frequency_seconds: int = 300,
    cleanup_policy: Optional[dict[str, Any]] = None,
    notification_policy: Optional[dict[str, Any]] = None,
    conversation_id: Optional[str] = None,
    mission_id: Optional[str] = None,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    """Create or register an email monitoring mission in SQLite."""
    ident = mission_id or f"email_{uuid.uuid4().hex[:12]}"
    now = time.time()
    clean_pol = json.dumps(cleanup_policy or {
        "allow_promotions": True,
        "allow_newsletters": True,
        "allow_notifications": True,
        "min_confidence": 0.80,
        "protected_categories": ["financial", "security", "education", "recruitment", "personal"],
    })
    notif_pol = json.dumps(notification_policy or {
        "notify_deadlines": True,
        "notify_interviews": True,
        "notify_unanswered_important": True,
        "neglect_threshold_seconds": 3600,
    })

    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO email_monitoring_missions (
                id, conversation_id, account_email, provider, frequency_seconds,
                status, policy_authorized, dry_run, cleanup_policy_json,
                notification_policy_json, next_run_at, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, 'active', 0, 1, ?, ?, ?, ?, ?)
            """,
            (ident, conversation_id, account_email, provider.lower(), frequency_seconds,
             clean_pol, notif_pol, now, now, now),
        )
        # Mirror into monitoring_missions so the shared continuous monitoring loop claims it
        conn.execute(
            """
            INSERT OR REPLACE INTO monitoring_missions (
                id, conversation_id, title, target_url, trigger_type,
                interval_seconds, condition_type, condition_params,
                status, next_run_at, created_at
            )
            VALUES (?, ?, ?, ?, 'interval', ?, 'email_intelligence', '{}', 'active', ?, ?)
            """,
            (ident, conversation_id, f"Email Intelligence ({account_email})",
             f"email://{provider.lower()}/{account_email}", frequency_seconds, now, now),
        )
        conn.commit()

    log_email_audit(
        ident,
        "mission_created",
        {"account_email": account_email, "provider": provider, "frequency_seconds": frequency_seconds},
        db_path=db_path,
    )
    return get_email_mission(ident, db_path=db_path) or {"id": ident, "account_email": account_email}


def get_email_mission(mission_id: str, db_path: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Retrieve an email monitoring mission by ID."""
    with get_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM email_monitoring_missions WHERE id = ?", (mission_id,)).fetchone()
        if not row:
            return None
        res = dict(row)
        try:
            res["cleanup_policy"] = json.loads(res.get("cleanup_policy_json") or "{}")
        except Exception:
            res["cleanup_policy"] = {}
        try:
            res["notification_policy"] = json.loads(res.get("notification_policy_json") or "{}")
        except Exception:
            res["notification_policy"] = {}
        return res


def list_email_missions(db_path: Optional[str] = None) -> list[dict[str, Any]]:
    """List all email monitoring missions."""
    with get_connection(db_path) as conn:
        rows = conn.execute("SELECT * FROM email_monitoring_missions ORDER BY created_at DESC").fetchall()
        results = []
        for r in rows:
            d = dict(r)
            try:
                d["cleanup_policy"] = json.loads(d.get("cleanup_policy_json") or "{}")
            except Exception:
                d["cleanup_policy"] = {}
            try:
                d["notification_policy"] = json.loads(d.get("notification_policy_json") or "{}")
            except Exception:
                d["notification_policy"] = {}
            results.append(d)
        return results


def update_email_mission(mission_id: str, updates: dict[str, Any], db_path: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Update fields on an email monitoring mission and sync with monitoring_missions table."""
    valid_fields = {
        "status", "sync_cursor", "policy_authorized", "dry_run",
        "cleanup_policy_json", "notification_policy_json", "last_sync_at",
        "next_run_at", "total_processed", "unnecessary_detected",
        "moved_to_trash", "important_detected", "neglected_detected",
        "frequency_seconds",
    }
    set_clauses = []
    params = []
    for k, v in updates.items():
        if k in valid_fields:
            set_clauses.append(f"{k} = ?")
            params.append(v)
    if not set_clauses:
        return get_email_mission(mission_id, db_path=db_path)

    set_clauses.append("updated_at = ?")
    now = time.time()
    params.append(now)
    params.append(mission_id)

    with get_connection(db_path) as conn:
        conn.execute(f"UPDATE email_monitoring_missions SET {', '.join(set_clauses)} WHERE id = ?", params)
        if "status" in updates or "next_run_at" in updates or "frequency_seconds" in updates:
            m_set = []
            m_params = []
            if "status" in updates:
                m_set.append("status = ?")
                m_params.append(updates["status"])
            if "next_run_at" in updates:
                m_set.append("next_run_at = ?")
                m_params.append(updates["next_run_at"])
            if "frequency_seconds" in updates:
                m_set.append("interval_seconds = ?")
                m_params.append(updates["frequency_seconds"])
            if m_set:
                m_params.append(mission_id)
                conn.execute(f"UPDATE monitoring_missions SET {', '.join(m_set)} WHERE id = ?", m_params)
        conn.commit()

    return get_email_mission(mission_id, db_path=db_path)


def save_email_classification(record: dict[str, Any], db_path: Optional[str] = None) -> dict[str, Any]:
    """Persist or update an email classification record."""
    ident = record.get("id") or f"cls_{uuid.uuid4().hex[:12]}"
    now = time.time()
    headers_json = json.dumps(record.get("raw_headers", {}))

    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO email_classifications (
                id, mission_id, message_id, thread_id, sender, recipient,
                subject, snippet, received_at, category, subcategory,
                importance_score, unnecessary_confidence, reasoning,
                deadline, required_action, is_unread, is_replied,
                action_taken, action_verified, action_timestamp,
                notified, notification_dismissed, raw_headers_json,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(mission_id, message_id) DO UPDATE SET
                thread_id = excluded.thread_id,
                sender = excluded.sender,
                recipient = excluded.recipient,
                subject = excluded.subject,
                snippet = excluded.snippet,
                received_at = excluded.received_at,
                category = excluded.category,
                subcategory = excluded.subcategory,
                importance_score = excluded.importance_score,
                unnecessary_confidence = excluded.unnecessary_confidence,
                reasoning = excluded.reasoning,
                deadline = excluded.deadline,
                required_action = excluded.required_action,
                is_unread = excluded.is_unread,
                is_replied = excluded.is_replied,
                action_taken = excluded.action_taken,
                action_verified = excluded.action_verified,
                action_timestamp = excluded.action_timestamp,
                notified = excluded.notified,
                notification_dismissed = excluded.notification_dismissed,
                raw_headers_json = excluded.raw_headers_json,
                updated_at = excluded.updated_at
            """,
            (
                ident, record["mission_id"], record["message_id"],
                record.get("thread_id", record["message_id"]),
                record.get("sender", ""), record.get("recipient", ""),
                record.get("subject", ""), record.get("snippet", ""),
                float(record.get("received_at") or now),
                record.get("category", "uncertain"),
                record.get("subcategory", "unknown"),
                float(record.get("importance_score", 0.0)),
                float(record.get("unnecessary_confidence", 0.0)),
                record.get("reasoning", ""),
                record.get("deadline"), record.get("required_action"),
                int(bool(record.get("is_unread", 1))),
                int(bool(record.get("is_replied", 0))),
                record.get("action_taken", "none"),
                int(bool(record.get("action_verified", 0))),
                record.get("action_timestamp"),
                int(bool(record.get("notified", 0))),
                int(bool(record.get("notification_dismissed", 0))),
                headers_json,
                float(record.get("created_at") or now),
                now,
            ),
        )
        conn.commit()

    return get_email_classification_by_message_id(record["mission_id"], record["message_id"], db_path=db_path) or record


def get_email_classification_by_message_id(mission_id: str, message_id: str, db_path: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Retrieve classification record for a given message ID."""
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM email_classifications WHERE mission_id = ? AND message_id = ?",
            (mission_id, message_id),
        ).fetchone()
        if not row:
            return None
        res = dict(row)
        try:
            res["raw_headers"] = json.loads(res.get("raw_headers_json") or "{}")
        except Exception:
            res["raw_headers"] = {}
        return res


def get_email_classifications(
    mission_id: str,
    category: Optional[str] = None,
    action_taken: Optional[str] = None,
    limit: int = 100,
    db_path: Optional[str] = None,
) -> list[dict[str, Any]]:
    """List classified emails for a mission with optional category or action filters."""
    query = "SELECT * FROM email_classifications WHERE mission_id = ?"
    params: list[Any] = [mission_id]
    if category:
        query += " AND category = ?"
        params.append(category)
    if action_taken:
        query += " AND action_taken = ?"
        params.append(action_taken)
    query += " ORDER BY received_at DESC LIMIT ?"
    params.append(limit)

    with get_connection(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            try:
                d["raw_headers"] = json.loads(d.get("raw_headers_json") or "{}")
            except Exception:
                d["raw_headers"] = {}
            results.append(d)
        return results


def log_email_audit(
    mission_id: str,
    action: str,
    details: dict[str, Any],
    message_id: Optional[str] = None,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    """Record an audit trail event for an email monitoring action."""
    ident = f"aud_{uuid.uuid4().hex[:12]}"
    now = time.time()
    from .privacy import scrub_persistent
    sanitized_details = scrub_persistent(details)
    details_json = json.dumps(sanitized_details)

    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO email_audit_log (id, mission_id, message_id, action, details_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (ident, mission_id, message_id, action, details_json, now),
        )
        conn.commit()

    return {"id": ident, "mission_id": mission_id, "action": action, "timestamp": now}


def get_email_audit_logs(mission_id: str, limit: int = 100, db_path: Optional[str] = None) -> list[dict[str, Any]]:
    """Retrieve chronological audit logs for an email mission."""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM email_audit_log WHERE mission_id = ? ORDER BY created_at DESC LIMIT ?",
            (mission_id, limit),
        ).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            try:
                d["details"] = json.loads(d.get("details_json") or "{}")
            except Exception:
                d["details"] = {}
            results.append(d)
        return results


def save_email_notification(notif: dict[str, Any], db_path: Optional[str] = None) -> dict[str, Any]:
    """Persist an important neglected email notification."""
    ident = notif.get("id") or f"notif_{uuid.uuid4().hex[:12]}"
    now = time.time()

    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO email_notifications (
                id, mission_id, message_id, sender, subject,
                importance_reason, neglect_reason, received_at,
                deadline, recommended_action, status, created_at, sent_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'sent', ?, ?)
            ON CONFLICT(mission_id, message_id) DO UPDATE SET
                importance_reason = excluded.importance_reason,
                neglect_reason = excluded.neglect_reason,
                deadline = excluded.deadline,
                recommended_action = excluded.recommended_action
            """,
            (
                ident, notif["mission_id"], notif["message_id"],
                notif.get("sender", ""), notif.get("subject", ""),
                notif.get("importance_reason", ""), notif.get("neglect_reason", ""),
                float(notif.get("received_at") or now),
                notif.get("deadline"), notif.get("recommended_action", "Review and respond"),
                now, now,
            ),
        )
        conn.commit()

    return {"id": ident, "message_id": notif["message_id"], "status": "sent"}


def get_email_notifications(
    mission_id: str,
    status: Optional[str] = None,
    limit: int = 50,
    db_path: Optional[str] = None,
) -> list[dict[str, Any]]:
    """List notifications for an email mission."""
    query = "SELECT * FROM email_notifications WHERE mission_id = ?"
    params: list[Any] = [mission_id]
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    with get_connection(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def update_email_notification_status(
    notif_id: str,
    status: str,
    db_path: Optional[str] = None,
) -> bool:
    """Update notification status (dismissed / resolved)."""
    now = time.time()
    field = "resolved_at" if status == "resolved" else "sent_at"
    with get_connection(db_path) as conn:
        cur = conn.execute(
            f"UPDATE email_notifications SET status = ?, {field} = ? WHERE id = ?",
            (status, now, notif_id),
        )
        conn.commit()
        return cur.rowcount > 0


def get_email_dashboard_stats(mission_id: str, db_path: Optional[str] = None) -> dict[str, Any]:
    """Aggregate live counts and summary data for the Email Intelligence Dashboard."""
    with get_connection(db_path) as conn:
        mission = conn.execute("SELECT * FROM email_monitoring_missions WHERE id = ?", (mission_id,)).fetchone()
        if not mission:
            return {}

        counts = conn.execute(
            """
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN category = 'unnecessary' THEN 1 ELSE 0 END) as unnecessary_count,
                SUM(CASE WHEN category = 'important' THEN 1 ELSE 0 END) as important_count,
                SUM(CASE WHEN category = 'uncertain' THEN 1 ELSE 0 END) as uncertain_count,
                SUM(CASE WHEN action_taken = 'moved_to_trash' THEN 1 ELSE 0 END) as trashed_count,
                SUM(CASE WHEN action_taken = 'dry_run_flagged' THEN 1 ELSE 0 END) as dry_run_count,
                SUM(CASE WHEN category = 'important' AND is_replied = 1 THEN 1 ELSE 0 END) as handled_count,
                SUM(CASE WHEN category = 'important' AND is_replied = 0 THEN 1 ELSE 0 END) as neglected_count
            FROM email_classifications
            WHERE mission_id = ?
            """,
            (mission_id,),
        ).fetchone()

        m_dict = dict(mission)
        return {
            "mission_id": mission_id,
            "account_email": m_dict.get("account_email"),
            "provider": m_dict.get("provider"),
            "status": m_dict.get("status"),
            "policy_authorized": bool(m_dict.get("policy_authorized")),
            "dry_run": bool(m_dict.get("dry_run")),
            "last_sync_at": m_dict.get("last_sync_at"),
            "next_run_at": m_dict.get("next_run_at"),
            "frequency_seconds": m_dict.get("frequency_seconds"),
            "total_processed": counts["total"] or 0,
            "unnecessary_detected": counts["unnecessary_count"] or 0,
            "moved_to_trash": counts["trashed_count"] or 0,
            "dry_run_flagged": counts["dry_run_count"] or 0,
            "important_detected": counts["important_count"] or 0,
            "important_handled": counts["handled_count"] or 0,
            "neglected_awaiting_attention": counts["neglected_count"] or 0,
            "uncertain_review_required": counts["uncertain_count"] or 0,
        }




