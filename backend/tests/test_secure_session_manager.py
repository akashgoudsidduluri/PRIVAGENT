"""Unit tests for SecureSessionManager."""
import os
import time
import pytest
from cryptography.fernet import Fernet

from app.db import init_db
from app.secure_session_manager import SecureSessionManager, _derive_fernet_key


@pytest.fixture
def temp_db(tmp_path):
    db_file = tmp_path / "test_session_mgr.db"
    init_db(str(db_file))
    return str(db_file)


@pytest.fixture
def session_mgr(temp_db):
    test_key = Fernet.generate_key()
    return SecureSessionManager(key=test_key, db_path=temp_db)


def test_encryption_and_decryption(session_mgr):
    data_dict = {"session_token": "abc-123-xyz", "user_id": 9999}
    enc = session_mgr.encrypt(data_dict)
    assert isinstance(enc, str)
    assert enc != ""
    assert "abc-123-xyz" not in enc  # Never plaintext in ciphertext

    decrypted = session_mgr.decrypt(enc)
    assert decrypted == data_dict

    # Test plain string
    enc_str = session_mgr.encrypt("my_cookie_value")
    assert session_mgr.decrypt(enc_str) == "my_cookie_value"


def test_save_and_retrieve_session(session_mgr):
    # Save a LeetCode session
    sess = session_mgr.save_session(
        service_id="leetcode",
        domain="leetcode.com",
        account_identifier="akash_coder",
        auth_type="browser_cookie",
        credentials={"LEETCODE_SESSION": "secret_cookie_token_456"},
        metadata={"preferred_lang": "python"},
    )

    assert sess["service_id"] == "leetcode"
    assert sess["domain"] == "leetcode.com"
    assert sess["account_identifier"] == "akash_coder"
    assert sess["is_authenticated"] is True
    assert "encrypted_credentials" not in sess  # Privacy sanitization

    # Retrieve by service_id
    retrieved = session_mgr.get_session("leetcode")
    assert retrieved is not None
    assert retrieved["account_identifier"] == "akash_coder"
    assert retrieved["status"] == "active"

    # Retrieve by domain
    by_domain = session_mgr.get_session("leetcode.com")
    assert by_domain is not None
    assert by_domain["service_id"] == "leetcode"

    # Check credentials decryption
    creds = session_mgr.get_credentials("leetcode")
    assert creds == {"LEETCODE_SESSION": "secret_cookie_token_456"}

    # Validation check
    assert session_mgr.is_session_valid("leetcode") is True
    assert session_mgr.is_session_valid("leetcode.com") is True


def test_touch_session(session_mgr):
    session_mgr.save_session(
        service_id="irctc",
        domain="irctc.co.in",
        account_identifier="irctc_user1",
    )
    time.sleep(0.01)
    prev = session_mgr.get_session("irctc")
    assert prev is not None

    success = session_mgr.touch_session("irctc", account_identifier="irctc_user_updated")
    assert success is True

    updated = session_mgr.get_session("irctc")
    assert updated["account_identifier"] == "irctc_user_updated"
    assert updated["last_verified_at"] >= prev["last_verified_at"]


def test_session_expiration(session_mgr):
    # Create an already-expired session
    past_time = time.time() - 3600
    session_mgr.save_session(
        service_id="expired_service",
        domain="expired.test",
        account_identifier="old_user",
        expires_at=past_time,
    )

    # Calling is_session_valid should return False
    assert session_mgr.is_session_valid("expired_service") is False

    # Calling get_session should reflect expired status
    sess = session_mgr.get_session("expired_service")
    assert sess["status"] == "expired"
    assert sess["is_authenticated"] is False

    # Credentials should not be decryptable once expired
    assert session_mgr.get_credentials("expired_service") is None


def test_invalidate_and_delete(session_mgr):
    session_mgr.save_session(
        service_id="github",
        domain="github.com",
        account_identifier="octocat",
    )
    assert session_mgr.is_session_valid("github") is True

    # Invalidate
    session_mgr.invalidate_session("github", reason="user_logout")
    assert session_mgr.is_session_valid("github") is False

    sess = session_mgr.get_session("github")
    assert sess["status"] == "invalidated"

    # Delete
    deleted = session_mgr.delete_session("github")
    assert deleted is True
    assert session_mgr.get_session("github") is None


def test_list_active_sessions(session_mgr):
    session_mgr.save_session(service_id="site1", domain="site1.com")
    session_mgr.save_session(service_id="site2", domain="site2.com")
    session_mgr.save_session(
        service_id="site3", domain="site3.com", expires_at=time.time() - 100
    )

    active = session_mgr.list_active_sessions()
    service_ids = [s["service_id"] for s in active]
    assert "site1" in service_ids
    assert "site2" in service_ids
    assert "site3" not in service_ids
