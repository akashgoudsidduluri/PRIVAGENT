"""Encrypted secret store, .env migration, and at-rest DB protection (Task 2).

Every test here pins the store to a temp directory with a fixed master key
so the suite never touches (or creates) real key material in backend/data/.
"""
from __future__ import annotations

import base64
import json
import os
import stat

import pytest

from app import auth, db
from app import secrets as secrets_store

# 32 zero bytes as a urlsafe-base64 Fernet-style master key (test only).
TEST_MASTER_KEY = base64.urlsafe_b64encode(b"\x00" * 32).decode("ascii")
OTHER_MASTER_KEY = base64.urlsafe_b64encode(b"\x01" * 32).decode("ascii")


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    """Point the secret store at a temp dir with a fixed master key."""
    store_dir = tmp_path / "secrets"
    monkeypatch.setenv(secrets_store.SECRETS_DIR_ENV, str(store_dir))
    monkeypatch.setenv(secrets_store.MASTER_KEY_ENV, TEST_MASTER_KEY)
    secrets_store.reset_for_tests()
    yield store_dir
    secrets_store.reset_for_tests()


def _store_file(store_dir):
    return store_dir / "secrets.enc.json"


# ---------------------------------------------------------------------------
# Round-trip / wrong key
# ---------------------------------------------------------------------------

def test_encrypt_decrypt_round_trip(isolated_store):
    token = secrets_store.encrypt_text("hello-secret-value")
    assert token.startswith(secrets_store.ENC_PREFIX)
    assert token != "hello-secret-value"
    assert secrets_store.decrypt_text(token) == "hello-secret-value"
    # Empty / None pass through (NULL/empty DB columns stay NULL/empty).
    assert secrets_store.encrypt_text("") == ""
    assert secrets_store.encrypt_text(None) is None
    assert secrets_store.decrypt_text("") == ""
    assert secrets_store.decrypt_text(None) is None


def test_legacy_plaintext_still_reads(isolated_store):
    assert secrets_store.decrypt_text("plain old value") == "plain old value"
    assert secrets_store.is_encrypted("plain") is False
    assert secrets_store.is_encrypted(secrets_store.encrypt_text("x")) is True


def test_wrong_master_key_fails_closed(isolated_store, monkeypatch):
    secrets_store.set_secret("oauth:google:access_token", "tok-abc-123")
    assert secrets_store.get_secret("oauth:google:access_token") == "tok-abc-123"
    # Rotate to a different master key: the stored secret must NOT come back.
    monkeypatch.setenv(secrets_store.MASTER_KEY_ENV, OTHER_MASTER_KEY)
    secrets_store.reset_for_tests()
    assert secrets_store.get_secret("oauth:google:access_token") is None
    # Column-level decryption fails closed (raises) instead of returning
    # ciphertext as if it were the value.
    token = secrets_store.encrypt_text("x")  # encrypted under the NEW key
    monkeypatch.setenv(secrets_store.MASTER_KEY_ENV, TEST_MASTER_KEY)
    secrets_store.reset_for_tests()
    with pytest.raises(ValueError):
        secrets_store.decrypt_text(token)


def test_set_get_delete_and_list(isolated_store):
    secrets_store.set_secret("llm:groq_api_key", "gsk-test")
    secrets_store.set_secret("llm:gemini_api_key", "gem-test")
    assert secrets_store.get_secret("llm:groq_api_key") == "gsk-test"
    assert secrets_store.get_secret("missing") is None
    assert secrets_store.list_secret_names() == ["llm:gemini_api_key", "llm:groq_api_key"]
    assert secrets_store.delete_secret("llm:groq_api_key") is True
    assert secrets_store.delete_secret("llm:groq_api_key") is False
    assert secrets_store.get_secret("llm:groq_api_key") is None


def test_store_file_contains_no_plaintext_secret(isolated_store):
    secrets_store.set_secret("oauth:google:access_token", "super-secret-token-123")
    raw = _store_file(isolated_store).read_text(encoding="utf-8")
    assert "super-secret-token-123" not in raw
    assert stat.S_IMODE(_store_file(isolated_store).stat().st_mode) == 0o600


# ---------------------------------------------------------------------------
# .env migration
# ---------------------------------------------------------------------------

def test_migrate_env_file_removes_plaintext(tmp_path, isolated_store):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# operator config\n"
        "GEMINI_API_KEY=gem-secret-123\n"
        "GROQ_API_KEY=\n"
        "PRIVAGENT_GOOGLE_ACCESS_TOKEN=ya29.google-token\n"
        "PRIVAGENT_GOOGLE_REFRESH_TOKEN=1//refresh-token\n"
        "PRIVAGENT_GOOGLE_OAUTH_CLIENT_ID=my-client-id\n"
        "PRIVAGENT_GOOGLE_OAUTH_CLIENT_SECRET=google-client-secret\n"
        "NAVIGATOR_PROVIDER=gemini\n"
        "MAX_STEPS=25\n",
        encoding="utf-8",
    )
    migrated = secrets_store.migrate_env_file(str(env_file))
    assert sorted(migrated) == [
        "GEMINI_API_KEY",
        "PRIVAGENT_GOOGLE_ACCESS_TOKEN",
        "PRIVAGENT_GOOGLE_OAUTH_CLIENT_SECRET",
        "PRIVAGENT_GOOGLE_REFRESH_TOKEN",
    ]
    # Secrets now come back from the store, and only from the store.
    assert secrets_store.get_secret("llm:gemini_api_key") == "gem-secret-123"
    assert secrets_store.get_secret("oauth:google:access_token") == "ya29.google-token"
    assert secrets_store.get_secret("oauth:google:refresh_token") == "1//refresh-token"
    assert secrets_store.get_secret("oauth:google:client_secret") == "google-client-secret"
    # .env no longer contains any of the migrated values in plaintext...
    remaining = env_file.read_text(encoding="utf-8")
    for leaked in ("gem-secret-123", "ya29.google-token", "1//refresh-token", "google-client-secret"):
        assert leaked not in remaining
    # ...but non-secrets (and the client ID, which is not a secret) survive.
    assert "PRIVAGENT_GOOGLE_OAUTH_CLIENT_ID=my-client-id" in remaining
    assert "NAVIGATOR_PROVIDER=gemini" in remaining
    assert "MAX_STEPS=25" in remaining
    assert "GEMINI_API_KEY=\n" in remaining
    # Migration is idempotent: a second run migrates nothing.
    assert secrets_store.migrate_env_file(str(env_file)) == []


def test_migrate_env_file_missing_file_is_noop(tmp_path, isolated_store):
    assert secrets_store.migrate_env_file(str(tmp_path / "nope.env")) == []


def test_migrate_never_blanks_a_key_it_could_not_store(tmp_path, isolated_store, monkeypatch):
    """If encryption fails, the .env value must be left untouched."""
    env_file = tmp_path / ".env"
    env_file.write_text("GEMINI_API_KEY=do-not-lose-me\n", encoding="utf-8")
    monkeypatch.setattr(secrets_store, "set_secret", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert secrets_store.migrate_env_file(str(env_file)) == []
    assert "GEMINI_API_KEY=do-not-lose-me" in env_file.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# OAuth / LLM keys go to the store, never to .env
# ---------------------------------------------------------------------------

def test_oauth_tokens_stored_encrypted_not_in_env_file(tmp_path, isolated_store, monkeypatch):
    from app.connectors.oauth import OAuthManager

    # No .env file exists in this repo clone; set_tokens must not create one
    # and must not write tokens anywhere in plaintext.
    monkeypatch.chdir(tmp_path)
    manager = OAuthManager()
    manager.set_tokens("google", access_token="ya29.test-token", refresh_token="1//test-refresh")
    manager.set_credentials("google", "cid-123", "csec-456")
    assert secrets_store.get_secret("oauth:google:access_token") == "ya29.test-token"
    assert secrets_store.get_secret("oauth:google:refresh_token") == "1//test-refresh"
    assert secrets_store.get_secret("oauth:google:client_secret") == "csec-456"
    assert manager._tokens["google"] == "ya29.test-token"
    assert manager.configured("google") is True
    assert not (tmp_path / ".env").exists()
    manager.disconnect("google")
    assert secrets_store.get_secret("oauth:google:access_token") is None
    assert manager._tokens.get("google") is None


def test_llm_key_retrieved_from_encrypted_store(isolated_store, monkeypatch):
    from app.providers import provider_settings

    secrets_store.set_secret("llm:groq_api_key", "gsk-from-store")
    monkeypatch.setattr("app.config.GROQ_API_KEY", "")
    provider_settings.reset_for_tests()
    try:
        provider_settings.configure(provider="groq")
        provider = provider_settings.provider_for_runtime()
        assert provider.__class__.__name__ == "GroqProvider"
        assert provider._api_key == "gsk-from-store"
    finally:
        provider_settings.reset_for_tests()


def test_runtime_llm_key_persisted_encrypted(isolated_store, monkeypatch):
    """POST /api/llm/config keys survive a restart via the store (Task 2).

    The production guard skips persistence under pytest, so this test
    flips that guard off for one call to exercise the real path.
    """
    from app.providers import provider_settings

    monkeypatch.setattr(secrets_store, "is_pytest", lambda: False)
    provider_settings.reset_for_tests()
    try:
        provider_settings.configure(provider="antseed", api_key="sk-antseed-1")
        assert secrets_store.get_secret("llm:antseed_api_key") == "sk-antseed-1"
        provider_settings.configure(provider="antseed", api_key="")
        assert secrets_store.get_secret("llm:antseed_api_key") is None
    finally:
        provider_settings.reset_for_tests()
        # monkeypatch restores the is_pytest override on teardown.


# ---------------------------------------------------------------------------
# Local operator token (Task 1) now lives in the encrypted store
# ---------------------------------------------------------------------------

def test_legacy_local_token_file_is_migrated_and_deleted(tmp_path, isolated_store, monkeypatch):
    token_file = tmp_path / ".local_token"
    token_file.write_text("a" * 64 + "\n", encoding="utf-8")
    monkeypatch.setattr(auth, "TOKEN_FILE", token_file)
    monkeypatch.setenv(auth.ENV_VAR, "")
    auth.reset_for_tests()
    try:
        token = auth.get_local_token()
        assert token == "a" * 64
        assert not token_file.exists()  # plaintext file removed after migration
        assert secrets_store.get_secret(auth.STORE_KEY) == "a" * 64
        # Survives a process restart (cache cleared, file gone): the store
        # is now the source of truth.
        auth.reset_for_tests()
        assert auth.get_local_token() == "a" * 64
        assert auth.verify_token("a" * 64) is True
        assert auth.verify_token("not-the-token") is False
    finally:
        auth.reset_for_tests()
        monkeypatch.setenv(auth.ENV_VAR, "test-token-123")


def test_generated_token_is_stored_encrypted_not_plaintext(tmp_path, isolated_store, monkeypatch):
    token_file = tmp_path / ".local_token"
    monkeypatch.setattr(auth, "TOKEN_FILE", token_file)
    monkeypatch.setenv(auth.ENV_VAR, "")
    auth.reset_for_tests()
    try:
        token = auth.get_local_token()
        assert len(token) == 64
        assert not token_file.exists()
        assert secrets_store.get_secret(auth.STORE_KEY) == token
        auth.reset_for_tests()
        assert auth.get_local_token() == token
    finally:
        auth.reset_for_tests()
        monkeypatch.setenv(auth.ENV_VAR, "test-token-123")


# ---------------------------------------------------------------------------
# SQLite at rest: 0600 files + encrypted columns
# ---------------------------------------------------------------------------

def test_db_files_are_mode_0600(tmp_path, isolated_store):
    db_path = str(tmp_path / "perm.db")
    db.init_db(db_path)
    chat = db.create_chat("perm test", db_path=db_path)
    db.add_message(chat["id"], "user", "hello", db_path=db_path)
    for suffix in ("", "-wal", "-shm"):
        candidate = db_path + suffix
        if os.path.exists(candidate):
            assert stat.S_IMODE(os.stat(candidate).st_mode) == 0o600, candidate
    assert stat.S_IMODE(os.stat(db_path).st_mode) == 0o600


def test_message_content_encrypted_at_rest(tmp_path, isolated_store):
    import sqlite3

    db_path = str(tmp_path / "enc.db")
    db.init_db(db_path)
    chat = db.create_chat("enc test", db_path=db_path)
    db.add_message(chat["id"], "user", "my secret message", evidence="went to example.org", db_path=db_path)
    # Accessors still return plaintext...
    msgs = db.get_messages(chat["id"], db_path=db_path)
    assert msgs[0]["content"] == "my secret message"
    assert msgs[0]["evidence"] == "went to example.org"
    # ...but raw SQL sees only ciphertext.
    with sqlite3.connect(db_path) as conn:
        raw = conn.execute("SELECT content, evidence FROM messages").fetchone()
    assert "my secret message" not in raw[0]
    assert "example.org" not in raw[1]
    assert raw[0].startswith(secrets_store.ENC_PREFIX)


def test_legacy_plaintext_db_rows_still_read(tmp_path, isolated_store):
    import sqlite3

    db_path = str(tmp_path / "legacy.db")
    db.init_db(db_path)
    chat = db.create_chat("legacy test", db_path=db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO messages (id, chat_id, role, content, evidence) VALUES (?, ?, ?, ?, ?)",
            ("m1", chat["id"], "user", "written before Task 2", None),
        )
        conn.commit()
    msgs = db.get_messages(chat["id"], db_path=db_path)
    assert msgs[0]["content"] == "written before Task 2"


def test_checkpoint_and_memory_columns_encrypted_at_rest(tmp_path, isolated_store):
    import sqlite3

    from app.memory import SemanticMemory

    db_path = str(tmp_path / "cols.db")
    db.init_db(db_path)
    chat = db.create_chat("cols test", db_path=db_path)
    db.set_chat_memory(chat["id"], "note", "memory secret", db_path=db_path)
    assert db.get_chat_memory(chat["id"], db_path=db_path)["note"] == "memory secret"
    db.save_task_checkpoint("t1", chat["id"], None, 1, "https://example.org",
                            [{"message": "checkpoint secret"}], {"k": "wm secret"}, db_path=db_path)
    cp = db.get_task_checkpoint("t1", db_path=db_path)
    assert cp["history"][0]["message"] == "checkpoint secret"
    assert cp["working_memory"]["k"] == "wm secret"
    facts = SemanticMemory()
    facts.remember("coding", "language", "Python", db_path=db_path)
    assert facts.recall("coding", db_path=db_path)[0]["fact_value"] == "Python"
    with sqlite3.connect(db_path) as conn:
        blob = " ".join(str(row[0]) for row in conn.execute(
            "SELECT value FROM chat_memory UNION ALL SELECT history FROM task_checkpoints "
            "UNION ALL SELECT working_memory FROM task_checkpoints "
            "UNION ALL SELECT fact_value FROM user_semantic_facts").fetchall())
    for leaked in ("memory secret", "checkpoint secret", "wm secret"):
        assert leaked not in blob
