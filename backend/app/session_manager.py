"""Browser session manager and registry module.

Manages isolated browser sessions, pinned primary tabs, and associated child tabs.
Ensures every independent browser task receives a fresh isolated session,
while explicit continuation reuses the designated session.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class BrowserSession:
    session_id: str
    task_id: str
    conversation_id: str
    origin: str  # "new_task", "continuation", "user_tab"
    primary_tab_id: Optional[int] = None
    child_tab_ids: list[int] = field(default_factory=list)
    status: str = "active"  # "active", "completed", "failed", "closed"
    created_at: float = field(default_factory=time.time)
    last_activity_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sessionId": self.session_id,
            "session_id": self.session_id,
            "taskId": self.task_id,
            "task_id": self.task_id,
            "conversationId": self.conversation_id,
            "conversation_id": self.conversation_id,
            "origin": self.origin,
            "primaryTabId": self.primary_tab_id,
            "primary_tab_id": self.primary_tab_id,
            "childTabIds": list(self.child_tab_ids),
            "child_tab_ids": list(self.child_tab_ids),
            "status": self.status,
            "createdAt": self.created_at,
            "lastActivityAt": self.last_activity_at,
        }

    def contains_tab(self, tab_id: int) -> bool:
        if self.primary_tab_id == tab_id:
            return True
        return tab_id in self.child_tab_ids


class SessionManager:
    def __init__(self) -> None:
        self._sessions: dict[str, BrowserSession] = {}
        # Mapping from conversation_id -> list of session_ids
        self._conversation_sessions: dict[str, list[str]] = {}

    def create_session(
        self,
        conversation_id: str,
        task_id: str,
        origin: str = "new_task",
        primary_tab_id: Optional[int] = None,
    ) -> BrowserSession:
        """Create a new isolated browser session."""
        session_id = uuid.uuid4().hex
        session = BrowserSession(
            session_id=session_id,
            task_id=task_id,
            conversation_id=conversation_id,
            origin=origin,
            primary_tab_id=primary_tab_id,
            child_tab_ids=[],
            status="active",
            created_at=time.time(),
            last_activity_at=time.time(),
        )
        self._sessions[session_id] = session
        if conversation_id not in self._conversation_sessions:
            self._conversation_sessions[conversation_id] = []
        self._conversation_sessions[conversation_id].append(session_id)

        # Persist to db if available
        self._persist_session(session)
        return session

    def _session_from_dict(self, raw: dict[str, Any]) -> BrowserSession:
        return BrowserSession(
            session_id=raw["session_id"],
            task_id=raw["task_id"],
            conversation_id=raw.get("conversation_id", ""),
            origin=raw.get("origin", "new_task"),
            primary_tab_id=raw.get("primary_tab_id"),
            child_tab_ids=raw.get("child_tab_ids", []),
            status=raw.get("status", "active"),
            created_at=raw.get("created_at", time.time()),
            last_activity_at=raw.get("last_activity_at", time.time()),
        )

    def get_session(self, session_id: str) -> Optional[BrowserSession]:
        sess = self._sessions.get(session_id)
        if sess:
            return sess
        try:
            from . import db
            raw = db.get_browser_session(session_id)
            if raw:
                sess = self._session_from_dict(raw)
                self._sessions[session_id] = sess
                return sess
        except Exception:
            pass
        return None

    def get_active_session_for_chat(self, conversation_id: str) -> Optional[BrowserSession]:
        """Return the most recent active or completable session for this conversation."""
        session_ids = self._conversation_sessions.get(conversation_id, [])
        for sid in reversed(session_ids):
            sess = self._sessions.get(sid)
            if sess and sess.status in ("active", "completed"):
                return sess
        try:
            from . import db
            raw = db.get_latest_active_session_for_chat(conversation_id)
            if raw:
                sess = self._session_from_dict(raw)
                self._sessions[sess.session_id] = sess
                if conversation_id not in self._conversation_sessions:
                    self._conversation_sessions[conversation_id] = []
                if sess.session_id not in self._conversation_sessions[conversation_id]:
                    self._conversation_sessions[conversation_id].append(sess.session_id)
                return sess
        except Exception:
            pass
        return None

    def continue_session(self, session_id: str, new_task_id: str) -> Optional[BrowserSession]:
        """Rebind an existing session to a continuation task."""
        sess = self._sessions.get(session_id)
        if not sess:
            return None
        sess.task_id = new_task_id
        sess.status = "active"
        sess.last_activity_at = time.time()
        self._persist_session(sess)
        return sess

    def pin_primary_tab(self, session_id: str, tab_id: int) -> bool:
        sess = self._sessions.get(session_id)
        if not sess:
            return False
        sess.primary_tab_id = tab_id
        sess.last_activity_at = time.time()
        self._persist_session(sess)
        return True

    def register_child_tab(self, session_id: str, tab_id: int) -> bool:
        sess = self._sessions.get(session_id)
        if not sess:
            return False
        if tab_id != sess.primary_tab_id and tab_id not in sess.child_tab_ids:
            sess.child_tab_ids.append(tab_id)
            sess.last_activity_at = time.time()
            self._persist_session(sess)
        return True

    def unregister_child_tab(self, session_id: str, tab_id: int) -> bool:
        sess = self._sessions.get(session_id)
        if not sess:
            return False
        if tab_id in sess.child_tab_ids:
            sess.child_tab_ids.remove(tab_id)
            sess.last_activity_at = time.time()
            self._persist_session(sess)
            return True
        return False

    def is_tab_in_session(self, session_id: str, tab_id: int) -> bool:
        sess = self._sessions.get(session_id)
        if not sess:
            return False
        return sess.contains_tab(tab_id)

    def update_status(self, session_id: str, status: str) -> None:
        sess = self._sessions.get(session_id)
        if sess:
            sess.status = status
            sess.last_activity_at = time.time()
            self._persist_session(sess)

    def close_session(self, session_id: str) -> None:
        sess = self._sessions.get(session_id)
        if sess:
            sess.status = "closed"
            sess.last_activity_at = time.time()
            self._persist_session(sess)

    def _persist_session(self, session: BrowserSession) -> None:
        try:
            from . import db
            db.save_browser_session(session.to_dict())
        except Exception:
            pass

    def clear(self) -> None:
        """Reset all in-memory sessions (used for testing)."""
        self._sessions.clear()
        self._conversation_sessions.clear()


session_manager = SessionManager()

