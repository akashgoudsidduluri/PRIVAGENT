"""init_db must survive two backend processes starting at once.

Uvicorn --reload can run init_db concurrently (a reload storm after
a big checkout, or a stale process still shutting down). The
migration step is PRAGMA-then-ALTER, so two processes can both see a
column missing and both try to add it; the loser used to crash
startup with "duplicate column name: user_id". The retry wrapper
covers the other transient errors a concurrent init can surface.
"""
import os
import sqlite3
import tempfile
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import db

# Old-schema chats table: exactly what pre-auth code created (no user_id).
OLD_CHATS_SQL = """
CREATE TABLE chats (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status TEXT DEFAULT 'active'
);
"""


def _old_schema_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.executescript(OLD_CHATS_SQL)
    conn.commit()
    conn.close()
    return path


def _remove_db(path):
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(path + suffix)
        except OSError:
            pass


def test_init_db_retries_transient_errors(monkeypatch):
    calls = {"n": 0}

    def flaky(_path=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "_init_db_once", flaky)
    db.init_db("/tmp/does-not-matter.db")
    assert calls["n"] == 3


def test_init_db_retries_no_such_column(monkeypatch):
    calls = {"n": 0}

    def flaky(_path=None):
        calls["n"] += 1
        if calls["n"] < 2:
            raise sqlite3.OperationalError("no such column: user_id")

    monkeypatch.setattr(db, "_init_db_once", flaky)
    db.init_db("/tmp/does-not-matter.db")
    assert calls["n"] == 2


def test_init_db_does_not_retry_deterministic_errors(monkeypatch):
    calls = {"n": 0}

    def broken(_path=None):
        calls["n"] += 1
        raise sqlite3.OperationalError("no such table: nope")

    monkeypatch.setattr(db, "_init_db_once", broken)
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        db.init_db("/tmp/does-not-matter.db")
    assert calls["n"] == 1


def test_init_db_gives_up_after_retries(monkeypatch):
    calls = {"n": 0}

    def always_locked(_path=None):
        calls["n"] += 1
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "_init_db_once", always_locked)
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        db.init_db("/tmp/does-not-matter.db")
    assert calls["n"] == 3


def test_init_db_concurrent_migration_race():
    # Eight threads racing init_db on a fresh old-schema DB. Pre-fix
    # this raised "duplicate column name: user_id" from the
    # PRAGMA-then-ALTER migration step.
    failures = []
    for _ in range(10):
        path = _old_schema_db()
        try:
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda _: db.init_db(path), range(8)))
            # Init is idempotent afterwards, and user_id exists once.
            db.init_db(path)
            with sqlite3.connect(path) as conn:
                cols = [r[1] for r in conn.execute("PRAGMA table_info(chats)")]
            assert cols.count("user_id") == 1
        except Exception as exc:  # noqa: BLE001 - collected, asserted below
            failures.append(exc)
        finally:
            _remove_db(path)
    assert failures == []
