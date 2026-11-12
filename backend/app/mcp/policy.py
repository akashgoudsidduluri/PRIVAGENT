"""Single-use, short-lived, argument-bound approval for mutating MCP calls."""
from __future__ import annotations
import hashlib
import json
import secrets
import time


class ApprovalPolicy:
    def __init__(self):
        self._tokens: dict[str, tuple[float, str]] = {}

    def _binding(self, name: str, args: dict, task_id: str = "") -> str:
        return hashlib.sha256(json.dumps([task_id, name, args], sort_keys=True, default=str).encode()).hexdigest()

    def approve(self, name: str, args: dict, task_id: str = "") -> str:
        token = secrets.token_urlsafe(32)
        self._tokens[token] = (time.monotonic() + 120, self._binding(name, args, task_id))
        return token

    def authorize(self, name: str, args: dict, token: str | None, task_id: str = "") -> None:
        bound = self._tokens.pop(token or "", None)
        if not bound or bound[0] < time.monotonic() or bound[1] != self._binding(name, args, task_id):
            raise PermissionError("fresh argument-bound approval required")
