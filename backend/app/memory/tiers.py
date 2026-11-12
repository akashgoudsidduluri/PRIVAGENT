"""Storage boundaries for memory tiers, with finite retention and privacy gates."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .. import db
from ..privacy import Sensitivity, classify_sensitivity, scrub_persistent


@dataclass
class WorkingMemory:
    """In-flight observations are deliberately never written to SQLite."""
    steps: list[dict[str, Any]] = field(default_factory=list)

    def add(self, observation: dict[str, Any]) -> None:
        self.steps.append(observation)
        if len(self.steps) > 20:
            self.steps.pop(0)

    def clear(self) -> None:
        self.steps.clear()


class EpisodicMemory:
    def record(self, chat_id: str, outcome: str, *, db_path=None) -> dict:
        return db.add_message(chat_id, "assistant", scrub_persistent(outcome), db_path=db_path)

    def history(self, chat_id: str, *, db_path=None) -> list[dict]:
        return db.get_messages(chat_id, db_path=db_path)


class SemanticMemory:
    CATEGORIES = frozenset({"personal", "retail", "coding"})

    def remember(self, category: str, key: str, value: str, *, ttl_days: int = 90, db_path=None) -> str:
        if category not in self.CATEGORIES or not key or len(key) > 80 or not value or len(value) > 1000:
            raise ValueError("invalid semantic fact")
        if not 1 <= ttl_days <= 365:
            raise ValueError("semantic retention must be 1 to 365 days")
        if classify_sensitivity(value, key) >= Sensitivity.RESTRICTED_PII:
            raise ValueError("credentials and PII are not allowed in semantic memory")
        identifier = uuid.uuid4().hex
        now = time.time()
        db.init_db(db_path)
        with db.get_connection(db_path) as conn:
            conn.execute("""INSERT INTO user_semantic_facts(id,category,fact_key,fact_value,created_at,expires_at)
                            VALUES(?,?,?,?,?,?) ON CONFLICT(category,fact_key) DO UPDATE SET
                            fact_value=excluded.fact_value, created_at=excluded.created_at,
                            expires_at=excluded.expires_at""",
                         (identifier, category, key, value, now, now + ttl_days * 86400))
        return identifier

    def recall(self, category: str | None = None, *, db_path=None) -> list[dict]:
        db.init_db(db_path)
        with db.get_connection(db_path) as conn:
            conn.execute("DELETE FROM user_semantic_facts WHERE expires_at <= ?", (time.time(),))
            rows = conn.execute("""SELECT id,category,fact_key,fact_value,created_at,expires_at
                                 FROM user_semantic_facts WHERE (? IS NULL OR category = ?)
                                 ORDER BY created_at DESC""", (category, category)).fetchall()
        return [dict(row) for row in rows]

    def forget(self, identifier: str, *, db_path=None) -> bool:
        with db.get_connection(db_path) as conn:
            return conn.execute("DELETE FROM user_semantic_facts WHERE id = ?", (identifier,)).rowcount > 0


class MissionMemory:
    def missions(self, *, db_path=None) -> list[dict]:
        with db.get_connection(db_path) as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM monitoring_missions ORDER BY created_at DESC")]


class ArtifactMemory:
    def artifacts(self, conversation_id: str, *, db_path=None) -> list[dict]:
        return db.list_conversation_artifacts(conversation_id, db_path=db_path)
