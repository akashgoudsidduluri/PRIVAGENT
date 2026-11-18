"""Encrypted local secret store (Task 2).

Replaces the old pattern of writing OAuth tokens, client secrets and LLM
API keys in plaintext into ``backend/.env`` (see
``connectors/oauth.py::_update_env_file`` in Task 1, now removed).

Design
------
* One Fernet key (AES-128-CBC + HMAC-SHA256, via ``cryptography``) encrypts
  every secret. The Fernet key itself is *derived* — never stored — with
  PBKDF2-HMAC-SHA256 (600,000 iterations, 16-byte salt) from master-key
  material.
* Master-key material, in priority order:
  1. ``PRIVAGENT_MASTER_KEY`` in the environment. It may be a Fernet key
     (44-char urlsafe base64, 32 bytes) or any passphrase string.
  2. The OS keychain via ``keyring`` (service ``"privagent"``, account
     ``"master-key"``), if the ``keyring`` package is installed *and* a
     backend answers. Best effort only: any keyring failure falls through
     silently — a headless machine must not hang waiting on a prompt.
  3. A generated 32-byte key stored in ``backend/data/.master.key``
     (mode ``0600``). This is **local-only protection, not hardware-backed
     security**: it stops casual file reads (and a stolen ``.env``/DB
     copy) but not an attacker who can read the whole data directory.
     Set ``PRIVAGENT_MASTER_KEY`` or use an OS keychain for stronger
     protection; the first use of the generated-key path logs a warning
     saying exactly that.
* The salt lives in ``backend/data/.master.salt`` (mode ``0600``). A salt
  is not secret; it only has to be stable per installation.
* Encrypted secrets live in ``backend/data/secrets.enc.json`` (mode
  ``0600``) as ``{"version": 1, "secrets": {name: <fernet token>}}``.
  Writes are atomic (temp file + ``os.replace``).
* Secret *values* and key material are never logged. Only secret *names*
  and file paths are logged.
* Under pytest (``"pytest" in sys.modules`` or ``PYTEST_CURRENT_TEST``)
  with no explicit ``PRIVAGENT_MASTER_KEY`` / ``PRIVAGENT_SECRETS_DIR``,
  the store is hermetic: a per-process temp directory and an ephemeral
  in-memory master key that is never written to disk. Tests that need a
  deterministic store set ``PRIVAGENT_SECRETS_DIR`` and
  ``PRIVAGENT_MASTER_KEY`` explicitly.

What this does NOT do
--------------------
* It does not encrypt the whole SQLite database (see ``db.py``): SQLCipher
  is not installable in this environment — ``pysqlcipher3`` fails to build
  because ``sqlcipher/sqlite3.h`` is missing (no ``libsqlcipher-dev``).
  The DB instead gets mode-``0600`` files plus column-level encryption of
  the most sensitive columns. See ``db.py`` and the README for the
  documented SQLCipher follow-up.
* It does not protect secrets from a process running as the same OS user
  that can read ``backend/data/`` — no local-file scheme can. It protects
  against plaintext copies (``.env`` in backups/screenshots, a copied
  ``.db`` file, ``git`` accidents) and against other users on the machine
  (``0600``/``0700`` modes).

Migration
---------
``migrate_env_file()`` moves known plaintext secrets out of ``backend/.env``
into this store. It encrypts + verifies the round-trip *before* blanking
the ``.env`` line, so a key is never deleted without being safely stored
first. It is only invoked from the application lifespan (never under
pytest); tests call it directly with an explicit path.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import secrets as _secrets_mod
import sys
import tempfile
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger("navigator.secrets")

# ---------------------------------------------------------------------------
# Paths / environment
# ---------------------------------------------------------------------------

SECRETS_DIR_ENV = "PRIVAGENT_SECRETS_DIR"
MASTER_KEY_ENV = "PRIVAGENT_MASTER_KEY"

KEYRING_SERVICE = "privagent"
KEYRING_ACCOUNT = "master-key"

_STORE_FILE = "secrets.enc.json"
_MASTER_KEY_FILE = ".master.key"
_SALT_FILE = ".master.salt"

_PBKDF2_ITERATIONS = 600_000
_SALT_LEN = 16

# Prefix marking a database column value as Fernet-encrypted by this module.
# Values without the prefix are treated as legacy plaintext (readable, so
# databases created before Task 2 keep working; new writes are encrypted).
ENC_PREFIX = "enc:v1:"

_lock = threading.RLock()

# In-process caches. The derived Fernet key is cached only in memory and is
# cleared by reset_for_tests(). Nothing here is ever written to a log.
_fernet_cache: dict = {}
_ephemeral_material: Optional[bytes] = None


def is_pytest() -> bool:
    return "pytest" in sys.modules or bool(os.environ.get("PYTEST_CURRENT_TEST"))


def _default_data_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "data"


def store_dir() -> Path:
    """Directory holding the store / master-key / salt files.

    ``PRIVAGENT_SECRETS_DIR`` wins. Under pytest with no override, a
    per-process temp directory is used so the suite never touches (or
    creates) real key material in ``backend/data/``.
    """
    override = os.environ.get(SECRETS_DIR_ENV, "").strip()
    if override:
        return Path(override)
    if is_pytest():
        return Path(tempfile.gettempdir()) / f"privagent-test-secrets-{os.getpid()}"
    return _default_data_dir()


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _write_private(path: Path, data: bytes) -> None:
    """Write *data* to *path* atomically with mode 0600."""
    _ensure_dir(path.parent)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".tmp.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Master-key material
# ---------------------------------------------------------------------------

def _keyring_material() -> Optional[bytes]:
    """Best-effort OS-keychain lookup. Returns None on any failure."""
    try:
        import keyring  # type: ignore
    except Exception:
        return None
    try:
        existing = keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
        if existing:
            return existing.encode("utf-8")
        material = _secrets_mod.token_bytes(32)
        keyring.set_password(
            KEYRING_SERVICE,
            KEYRING_ACCOUNT,
            base64.urlsafe_b64encode(material).decode("ascii"),
        )
        return material
    except Exception as exc:  # keyring backend missing / locked / headless
        logger.debug("OS keychain unavailable, falling back to a local key file: %s", exc)
        return None


def _master_material() -> bytes:
    """Resolve the master-key material (raw bytes, before PBKDF2)."""
    global _ephemeral_material
    env_key = os.environ.get(MASTER_KEY_ENV, "").strip()
    if env_key:
        try:
            raw = base64.urlsafe_b64decode(env_key.encode("ascii"))
            if len(raw) == 32:
                return raw
        except Exception:
            pass
        return env_key.encode("utf-8")

    if is_pytest():
        # Hermetic test runs: never create real key files.
        if _ephemeral_material is None:
            _ephemeral_material = _secrets_mod.token_bytes(32)
        return _ephemeral_material

    material = _keyring_material()
    if material is not None:
        return material

    key_file = store_dir() / _MASTER_KEY_FILE
    try:
        if key_file.exists():
            raw = base64.urlsafe_b64decode(key_file.read_bytes().strip())
            if len(raw) == 32:
                return raw
            logger.warning("Ignoring malformed master-key file %s; generating a new one.", key_file)
    except Exception as exc:
        logger.warning("Could not read master-key file %s: %s", key_file, exc)
    material = _secrets_mod.token_bytes(32)
    try:
        _write_private(key_file, base64.urlsafe_b64encode(material))
    except OSError as exc:
        logger.warning("Could not persist master-key file %s: %s", key_file, exc)
    logger.warning(
        "No %s and no OS keychain available: generated a local master key in %s "
        "(mode 0600). This is local-only protection, not hardware-backed security. "
        "Set %s or configure an OS keychain for stronger protection, and back up "
        "%s — losing it makes every stored secret unrecoverable.",
        MASTER_KEY_ENV, key_file, MASTER_KEY_ENV, key_file,
    )
    return material


def _salt() -> bytes:
    if is_pytest() and not os.environ.get(SECRETS_DIR_ENV):
        # Ephemeral salt to match the ephemeral test master key.
        key = "pytest-salt"
        cached = _fernet_cache.get(key)
        if cached is None:
            cached = _secrets_mod.token_bytes(_SALT_LEN)
            _fernet_cache[key] = cached
        return cached
    salt_file = store_dir() / _SALT_FILE
    try:
        if salt_file.exists():
            raw = salt_file.read_bytes()
            if len(raw) == _SALT_LEN:
                return raw
    except OSError as exc:
        logger.warning("Could not read salt file %s: %s", salt_file, exc)
    salt = _secrets_mod.token_bytes(_SALT_LEN)
    try:
        _write_private(salt_file, salt)
    except OSError as exc:
        logger.warning("Could not persist salt file %s: %s", salt_file, exc)
    return salt


def _fernet():
    """Build (and cache) the Fernet instance for the current master key."""
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    cache_key = "fernet"
    fernet = _fernet_cache.get(cache_key)
    if fernet is not None:
        return fernet
    material = _master_material()
    salt = _salt()
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=_PBKDF2_ITERATIONS,
    )
    key = base64.urlsafe_b64encode(kdf.derive(material))
    fernet = Fernet(key)
    _fernet_cache[cache_key] = fernet
    return fernet


def warm_key() -> None:
    """Derive (and cache) the Fernet key now, with no writes.

    Task 4 (latency): the first :func:`encrypt_text` in a process pays
    the full PBKDF2 derivation (~140-175 ms measured) inside whatever
    step happens to encrypt first — in practice the first checkpoint,
    which then looks ~50x slower than every later one (~3 ms). Deriving
    once up front (server boot, loop entry) moves that one-time cost
    out of the first task step. The iteration count is unchanged; this
    only changes *when* the derivation runs. Safe to call repeatedly:
    a cached key returns immediately.
    """
    _fernet()


def reset_for_tests() -> None:
    """Clear in-process key caches (tests only)."""
    global _ephemeral_material
    _fernet_cache.clear()
    _ephemeral_material = None


# ---------------------------------------------------------------------------
# The encrypted store: {name: fernet-token}
# ---------------------------------------------------------------------------

def _store_path() -> Path:
    return store_dir() / _STORE_FILE


def _read_store() -> dict:
    path = _store_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ValueError(f"Encrypted secret store {path} is unreadable or corrupt: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"Encrypted secret store {path} has an unexpected format")
    secrets = raw.get("secrets", {})
    if not isinstance(secrets, dict):
        raise ValueError(f"Encrypted secret store {path} has an unexpected format")
    return dict(secrets)


def _write_store(secrets: dict) -> None:
    payload = json.dumps({"version": 1, "secrets": secrets}, indent=2).encode("utf-8")
    _write_private(_store_path(), payload)


def set_secret(name: str, value: str) -> None:
    """Encrypt *value* and store it under *name*. Never logs the value."""
    if not name or not isinstance(name, str):
        raise ValueError("secret name must be a non-empty string")
    if value is None:
        raise ValueError("secret value must not be None")
    with _lock:
        fernet = _fernet()
        token = fernet.encrypt(str(value).encode("utf-8")).decode("ascii")
        secrets = _read_store()
        secrets[name] = token
        _write_store(secrets)


def get_secret(name: str) -> Optional[str]:
    """Decrypt and return the secret stored under *name*, or None."""
    if not name:
        return None
    with _lock:
        secrets = _read_store()
        token = secrets.get(name)
        if not token:
            return None
        fernet = _fernet()
        try:
            return fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except Exception as exc:
            # Wrong master key / tampered store: fail closed, never return
            # ciphertext or a guess. Callers must treat None as "no secret".
            logger.warning("Could not decrypt secret %r (wrong master key or corrupt store): %s", name, type(exc).__name__)
            return None


def delete_secret(name: str) -> bool:
    """Remove *name* from the store. Returns True if something was removed."""
    with _lock:
        secrets = _read_store()
        if name not in secrets:
            return False
        del secrets[name]
        _write_store(secrets)
        return True


def list_secret_names() -> list[str]:
    with _lock:
        return sorted(_read_store().keys())


# ---------------------------------------------------------------------------
# Column-level encryption for SQLite (see db.py)
# ---------------------------------------------------------------------------

def encrypt_text(value: Optional[str]) -> Optional[str]:
    """Encrypt a DB column value, tagged so legacy plaintext still reads.

    ``None`` and ``""`` pass through unchanged (NULL/empty stay NULL/empty,
    so ``IS NULL`` queries and NOT NULL constraints keep working).
    """
    if value is None or value == "":
        return value
    return ENC_PREFIX + _fernet().encrypt(str(value).encode("utf-8")).decode("ascii")


def decrypt_text(value: Optional[str]) -> Optional[str]:
    """Inverse of :func:`encrypt_text`; legacy plaintext passes through.

    Raises ``ValueError`` if a value carries the ``enc:v1:`` tag but cannot
    be decrypted (wrong master key / corrupt data) — fail closed rather
    than returning ciphertext as if it were the value.
    """
    if value is None or value == "":
        return value
    if not str(value).startswith(ENC_PREFIX):
        return value
    try:
        return _fernet().decrypt(str(value)[len(ENC_PREFIX):].encode("ascii")).decode("utf-8")
    except Exception as exc:
        raise ValueError("encrypted database value cannot be decrypted (wrong master key or corrupt data)") from exc


def is_encrypted(value: Optional[str]) -> bool:
    return isinstance(value, str) and value.startswith(ENC_PREFIX)


# ---------------------------------------------------------------------------
# .env migration
# ---------------------------------------------------------------------------

_OAUTH_PROVIDERS = ("google", "microsoft", "slack", "github")

#: Plaintext ``.env`` key -> encrypted-store name. Client IDs are
#: deliberately absent: they are not secrets and stay in ``.env``.
ENV_TO_STORE_KEY: dict[str, str] = {}
for _provider in _OAUTH_PROVIDERS:
    _p = _provider.upper()
    ENV_TO_STORE_KEY[f"PRIVAGENT_{_p}_ACCESS_TOKEN"] = f"oauth:{_provider}:access_token"
    ENV_TO_STORE_KEY[f"PRIVAGENT_{_p}_REFRESH_TOKEN"] = f"oauth:{_provider}:refresh_token"
    ENV_TO_STORE_KEY[f"PRIVAGENT_{_p}_OAUTH_CLIENT_SECRET"] = f"oauth:{_provider}:client_secret"
ENV_TO_STORE_KEY.update({
    "GEMINI_API_KEY": "llm:gemini_api_key",
    "GROQ_API_KEY": "llm:groq_api_key",
    "ANTSEED_API_KEY": "llm:antseed_api_key",
    "DEEPAI_GATEWAY_KEY": "llm:deepai_gateway_key",
    "PRIVAGENT_LOCAL_TOKEN": "local_auth_token",
})


def get_secret_for_env_key(env_key: str) -> Optional[str]:
    """Read the secret that used to live in ``.env`` under *env_key*."""
    store_key = ENV_TO_STORE_KEY.get(env_key)
    if not store_key:
        return None
    return get_secret(store_key)


def migrate_env_file(env_path: Optional[str] = None) -> list[str]:
    """Move plaintext secrets out of a ``.env`` file into the store.

    For every known secret key present with a non-empty value, the value is
    encrypted into the store and the round-trip is verified *before* the
    ``.env`` line is blanked, so a key is never deleted without being safely
    stored first. Blanking keeps the ``KEY=`` line (tooling that expects
    the key to exist keeps working) and appends one comment recording the
    migration. Non-secret lines are preserved byte-for-byte.

    Returns the list of migrated ``.env`` key names. Never logs values.
    """
    path = Path(env_path) if env_path else _default_data_dir().parent / ".env"
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError as exc:
        logger.warning("Could not read %s for secret migration: %s", path, exc)
        return []

    migrated: list[str] = []
    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        migrated_here = False
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in ENV_TO_STORE_KEY:
                value = stripped.split("=", 1)[1].strip().strip('"').strip("'")
                if value:
                    store_key = ENV_TO_STORE_KEY[key]
                    try:
                        set_secret(store_key, value)
                        if get_secret(store_key) != value:
                            raise ValueError("round-trip verification failed")
                    except Exception as exc:
                        # Never blank a value we could not safely store.
                        logger.warning("Could not migrate %s to the encrypted store; leaving it in %s: %s", key, path, exc)
                        new_lines.append(line)
                        continue
                    migrated.append(key)
                    new_lines.append(f"{key}=\n")
                    migrated_here = True
        if not migrated_here:
            new_lines.append(line)

    if migrated:
        new_lines.append(
            "# Task 2: migrated to the encrypted secret store "
            "(backend/data/secrets.enc.json) and removed from this file in plaintext: "
            + ", ".join(migrated) + "\n"
        )
        try:
            _write_private(path, "".join(new_lines).encode("utf-8"))
        except OSError as exc:
            logger.warning("Could not rewrite %s after secret migration: %s", path, exc)
            return []
        logger.info("Migrated %d secret(s) from %s to the encrypted store: %s", len(migrated), path, ", ".join(migrated))
    return migrated
