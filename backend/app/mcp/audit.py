"""No raw arguments, credentials or tool output enter the MCP audit table."""
from __future__ import annotations
import hashlib
import json
import time
import uuid
from .. import db


def record_receipt(name: str, args: dict, latency_ms: int, status: str, output_hash: str = "", *, db_path=None, user_id=None) -> str:
    """Hash-only receipt. ``user_id`` attributes the call to its owner
    (None = operator); the audit read filters by it."""
    db.init_db(db_path)
    identifier = uuid.uuid4().hex
    input_hash = hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()
    with db.get_connection(db_path) as conn:
        conn.execute("""INSERT INTO mcp_audit(id,user_id,tool_name,input_hash,latency_ms,status,output_hash,created_at)
                        VALUES(?,?,?,?,?,?,?,?)""",
                     (identifier, user_id, name, input_hash, latency_ms, status, output_hash, time.time()))
    return identifier
