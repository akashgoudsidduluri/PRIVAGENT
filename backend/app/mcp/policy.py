"""Single-use, short-lived, argument-bound approval for mutating MCP calls."""
from __future__ import annotations
import hashlib
import json
import secrets
import time


class ApprovalPolicy:
    def __init__(self):
        self._tokens: dict[str, tuple[float, str]] = {}

    def _binding(
        self,
        name: str,
        args: dict,
        task_id: str = "",
        user_id: str = "default_user",
        server: str = "",
    ) -> str:
        s_name = server
        t_name = name
        if not s_name and "." in name:
            parts = name.split(".", 1)
            s_name = parts[0]
            t_name = parts[1]
        payload = [user_id or "default_user", task_id or "", s_name or "", t_name or "", args or {}]
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()

    def approve(
        self,
        name: str,
        args: dict,
        task_id: str = "",
        user_id: str = "default_user",
        server: str = "",
    ) -> str:
        token = secrets.token_urlsafe(32)
        binding = self._binding(name, args, task_id=task_id, user_id=user_id, server=server)
        self._tokens[token] = (time.monotonic() + 120, binding)
        return token

    def authorize(
        self,
        name: str,
        args: dict,
        token: str | None,
        task_id: str = "",
        user_id: str = "default_user",
        server: str = "",
    ) -> None:
        bound = self._tokens.pop(token or "", None)
        expected = self._binding(name, args, task_id=task_id, user_id=user_id, server=server)
        if not bound or bound[0] < time.monotonic() or bound[1] != expected:
            raise PermissionError("fresh argument-bound approval required")
