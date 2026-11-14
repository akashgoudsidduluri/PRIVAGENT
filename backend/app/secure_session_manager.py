"""Secure Persistent Session Manager for Just-in-Time Browser Authentication.

Stores and manages encrypted web sessions, credentials, and account identities.
Guarantees fail-closed privacy: credentials are encrypted at rest using AES-GCM (Fernet)
and sanitized before passing to logs, checkpoints, or LLM prompts.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

from cryptography.fernet import Fernet, InvalidToken

from .db import (
    delete_authenticated_session_record,
    get_authenticated_session_by_domain,
    get_authenticated_session_record,
    list_authenticated_session_records,
    save_authenticated_session_record,
    update_authenticated_session_status,
)

logger = logging.getLogger("privagent.secure_session_manager")

_DATA_DIR = Path(__file__).resolve().parents[1] / "data"
_KEY_FILE = _DATA_DIR / ".session_vault.key"


def _derive_fernet_key() -> bytes:
    """Obtain or generate a 32-byte URL-safe base64 Fernet encryption key."""
    env_key = os.environ.get("PRIVAGENT_ENCRYPTION_KEY")
    if env_key:
        raw = env_key.strip().encode("utf-8")
        try:
            # Check if already a valid Fernet key
            Fernet(raw)
            return raw
        except Exception:
            # Derive deterministic 32-byte key via SHA-256
            digest = hashlib.sha256(raw).digest()
            return base64.urlsafe_b64encode(digest)

    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    if _KEY_FILE.exists():
        try:
            stored = _KEY_FILE.read_bytes().strip()
            Fernet(stored)
            return stored
        except Exception as e:
            logger.warning("Corrupted key file at %s: %s; re-generating", _KEY_FILE, e)

    new_key = Fernet.generate_key()
    try:
        _KEY_FILE.write_bytes(new_key)
        # Attempt to restrict permissions on POSIX
        if hasattr(os, "chmod"):
            try:
                os.chmod(str(_KEY_FILE), 0o600)
            except Exception:
                pass
    except Exception as e:
        logger.error("Failed to write encryption key to %s: %s", _KEY_FILE, e)
    return new_key


class SecureSessionManager:
    """Thread-safe manager for encrypted session persistence."""

    def __init__(self, key: Optional[bytes] = None, db_path: Optional[str] = None):
        self._db_path = db_path
        self._key = key or _derive_fernet_key()
        self._cipher = Fernet(self._key)

    def encrypt(self, payload: Any) -> str:
        """Encrypt payload into URL-safe base64 string."""
        if payload is None:
            return ""
        if isinstance(payload, (dict, list)):
            raw_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        elif isinstance(payload, str):
            raw_bytes = payload.encode("utf-8")
        elif isinstance(payload, bytes):
            raw_bytes = payload
        else:
            raw_bytes = str(payload).encode("utf-8")

        return self._cipher.encrypt(raw_bytes).decode("utf-8")

    def decrypt(self, encrypted_str: Optional[str]) -> Any:
        """Decrypt payload; returns parsed dict/list if JSON, else str, or None."""
        if not encrypted_str:
            return None
        try:
            decrypted_bytes = self._cipher.decrypt(encrypted_str.encode("utf-8"))
            text = decrypted_bytes.decode("utf-8")
            try:
                return json.loads(text)
            except ValueError:
                return text
        except (InvalidToken, Exception) as e:
            logger.error("Failed to decrypt session payload: %s", e)
            return None

    def save_session(
        self,
        service_id: str,
        domain: str,
        account_identifier: Optional[str] = None,
        auth_type: str = "browser_cookie",
        credentials: Optional[Any] = None,
        metadata: Optional[dict[str, Any]] = None,
        expires_at: Optional[float] = None,
    ) -> dict[str, Any]:
        """Save or update an authenticated web session."""
        service_id = service_id.strip().lower()
        domain = domain.strip().lower()
        encrypted = self.encrypt(credentials) if credentials is not None else None

        record = {
            "service_id": service_id,
            "domain": domain,
            "account_identifier": account_identifier,
            "auth_type": auth_type,
            "encrypted_credentials": encrypted,
            "status": "active",
            "metadata": metadata or {},
            "created_at": time.time(),
            "expires_at": expires_at,
            "last_verified_at": time.time(),
        }

        save_authenticated_session_record(record, db_path=self._db_path)
        logger.info(
            "Saved authenticated session for service=%s, domain=%s, account=%s",
            service_id,
            domain,
            account_identifier,
        )
        return self.to_safe_dict(record)

    def get_session(self, service_id_or_domain: str) -> Optional[dict[str, Any]]:
        """Retrieve a session by service_id or domain."""
        query = service_id_or_domain.strip().lower()
        rec = get_authenticated_session_record(query, db_path=self._db_path)
        if not rec:
            rec = get_authenticated_session_by_domain(query, db_path=self._db_path)
        if not rec:
            return None

        # Check expiration
        expires_at = rec.get("expires_at")
        if expires_at and float(expires_at) < time.time():
            if rec.get("status") == "active":
                update_authenticated_session_status(rec["service_id"], "expired", db_path=self._db_path)
                rec["status"] = "expired"

        return self.to_safe_dict(rec)

    def get_credentials(self, service_id_or_domain: str) -> Any:
        """Retrieve and decrypt credentials for a service. NEVER expose to logs/UI."""
        query = service_id_or_domain.strip().lower()
        rec = get_authenticated_session_record(query, db_path=self._db_path)
        if not rec:
            rec = get_authenticated_session_by_domain(query, db_path=self._db_path)
        if not rec or rec.get("status") != "active":
            return None

        expires_at = rec.get("expires_at")
        if expires_at and float(expires_at) < time.time():
            update_authenticated_session_status(rec["service_id"], "expired", db_path=self._db_path)
            return None

        encrypted = rec.get("encrypted_credentials")
        return self.decrypt(encrypted)

    def is_session_valid(self, service_id_or_domain: str) -> bool:
        """Check whether an active, non-expired session exists."""
        sess = self.get_session(service_id_or_domain)
        if not sess:
            return False
        return sess.get("status") == "active"

    def touch_session(
        self,
        service_id_or_domain: str,
        account_identifier: Optional[str] = None,
    ) -> bool:
        """Update last_verified_at and optional account_identifier for a session."""
        query = service_id_or_domain.strip().lower()
        raw = get_authenticated_session_record(query, db_path=self._db_path)
        if not raw:
            raw = get_authenticated_session_by_domain(query, db_path=self._db_path)
        if not raw:
            return False

        if account_identifier:
            raw["account_identifier"] = account_identifier
        raw["last_verified_at"] = time.time()
        raw["status"] = "active"

        save_authenticated_session_record(raw, db_path=self._db_path)
        return True

    def invalidate_session(self, service_id_or_domain: str, reason: str = "invalidated") -> bool:
        """Mark a session as expired or invalidated."""
        query = service_id_or_domain.strip().lower()
        raw = get_authenticated_session_record(query, db_path=self._db_path)
        if not raw:
            raw = get_authenticated_session_by_domain(query, db_path=self._db_path)
        if not raw:
            return False
        sid = raw["service_id"]
        logger.info("Invalidating session for %s (reason: %s)", sid, reason)
        return update_authenticated_session_status(
            sid,
            "expired" if reason == "expired" else "invalidated",
            db_path=self._db_path,
        )

    def delete_session(self, service_id_or_domain: str) -> bool:
        """Hard delete a session."""
        query = service_id_or_domain.strip().lower()
        raw = get_authenticated_session_record(query, db_path=self._db_path)
        if not raw:
            raw = get_authenticated_session_by_domain(query, db_path=self._db_path)
        if not raw:
            return False
        return delete_authenticated_session_record(raw["service_id"], db_path=self._db_path)

    def list_active_sessions(self) -> list[dict[str, Any]]:
        """List all active, non-expired sessions with sanitized fields."""
        records = list_authenticated_session_records(include_all=False, db_path=self._db_path)
        now = time.time()
        valid = []
        for r in records:
            exp = r.get("expires_at")
            if exp and float(exp) < now:
                update_authenticated_session_status(r["service_id"], "expired", db_path=self._db_path)
                continue
            valid.append(self.to_safe_dict(r))
        return valid

    @staticmethod
    def to_safe_dict(record: dict[str, Any]) -> dict[str, Any]:
        """Strip encrypted_credentials to prevent leakage to UI, logs, or LLM."""
        safe = {
            "service_id": record.get("service_id"),
            "domain": record.get("domain"),
            "account_identifier": record.get("account_identifier"),
            "auth_type": record.get("auth_type"),
            "status": record.get("status"),
            "created_at": record.get("created_at"),
            "expires_at": record.get("expires_at"),
            "last_verified_at": record.get("last_verified_at"),
            "metadata": record.get("metadata") or {},
            "is_authenticated": record.get("status") == "active",
        }
        return safe


# Singleton instance
_default_session_manager: Optional[SecureSessionManager] = None


def get_secure_session_manager() -> SecureSessionManager:
    """Return the global SecureSessionManager singleton."""
    global _default_session_manager
    if _default_session_manager is None:
        _default_session_manager = SecureSessionManager()
    return _default_session_manager
