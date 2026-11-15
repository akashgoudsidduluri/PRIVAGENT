"""Schema-validated MCP tool catalog with single-use mutation gates."""
from __future__ import annotations
import inspect
import re
import time
from dataclasses import dataclass
from typing import Callable, Any
from jsonschema import validate, Draft202012Validator

from .policy import ApprovalPolicy
from .sanitizer import sanitize_tool_output, sanitize_tool_description, resolve_tool_mutating_status
from .audit import record_receipt


@dataclass(frozen=True)
class Tool:
    name: str
    schema: dict
    execute: Callable[[dict], Any]
    mutating: bool = True  # unknown tools are never assumed read-only
    server: str = "local"
    description: str = ""
    discovered: bool = False  # true only for tools learned from an MCP server


class Registry:
    def __init__(self, db_path: str | None = None):
        self.tools: dict[str, Tool] = {}
        self._policy = ApprovalPolicy()
        self.db_path = db_path

    def _sanitize_and_validate_tool(self, tool: Tool) -> Tool:
        Draft202012Validator.check_schema(tool.schema)
        if tool.schema.get("type") != "object":
            raise ValueError("tool arguments must be an object schema")

        clean_desc = sanitize_tool_description(tool.description)
        # Fail-closed semantic mutation verification:
        # If tool claims to be read-only, check if name/desc/schema reveal destructive operations
        declared_read_only = not tool.mutating
        actual_mutating = resolve_tool_mutating_status(
            is_declared_read_only=declared_read_only,
            name=tool.name,
            description=clean_desc,
            schema=tool.schema,
        )

        if clean_desc != tool.description or actual_mutating != tool.mutating:
            return Tool(
                name=tool.name,
                schema=tool.schema,
                execute=tool.execute,
                mutating=actual_mutating,
                server=tool.server,
                description=clean_desc,
                discovered=tool.discovered,
            )
        return tool

    def register(self, tool: Tool):
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", tool.name) or tool.name in self.tools:
            raise ValueError("invalid or duplicate tool name")
        validated = self._sanitize_and_validate_tool(tool)
        self.tools[validated.name] = validated

    def replace(self, tool: Tool):
        """Refresh a tool discovered again from the same server.

        Re-running discovery must not fail just because the tool was already
        catalogued. A name owned by a different server remains a collision.
        """
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", tool.name):
            raise ValueError("invalid tool name")
        existing = self.tools.get(tool.name)
        if existing is not None and (not existing.discovered or existing.server != tool.server):
            raise ValueError("tool name belongs to another source")
        validated = self._sanitize_and_validate_tool(tool)
        self.tools[validated.name] = validated

    def approve(self, name: str, args: dict, task_id: str = "", user_id: str = "default_user", server: str = "") -> str:
        """Trusted approval handler only; never expose this method to a planner."""
        validate(args, self.tools[name].schema)
        s_name = server or self.tools[name].server
        return self._policy.approve(name, args, task_id=task_id, user_id=user_id, server=s_name)

    def _authorize(self, tool: Tool, args: dict, approval: str | None, task_id: str = "", user_id: str = "default_user") -> None:
        validate(args, tool.schema)
        if tool.mutating:
            self._policy.authorize(tool.name, args, approval, task_id=task_id, user_id=user_id, server=tool.server)

    def execute(self, name: str, args: dict, approval: str | None = None, *, task_id: str = "", user_id: str = "default_user") -> dict:
        tool = self.tools[name]
        self._authorize(tool, args, approval, task_id=task_id, user_id=user_id)
        started = time.monotonic()
        try:
            result = tool.execute(args)
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result): result.close()
                raise RuntimeError("async MCP tool requires execute_async")
            clean = sanitize_tool_output(result)
            record_receipt(name, args, int((time.monotonic() - started) * 1000), "ok", clean["output_hash"], db_path=self.db_path)
            return clean
        except Exception:
            record_receipt(name, args, int((time.monotonic() - started) * 1000), "failed", db_path=self.db_path)
            raise

    async def execute_async(self, name: str, args: dict, approval: str | None = None, *, task_id: str = "", user_id: str = "default_user") -> dict:
        tool = self.tools[name]
        self._authorize(tool, args, approval, task_id=task_id, user_id=user_id)
        started = time.monotonic()
        try:
            result = tool.execute(args)
            if inspect.isawaitable(result): result = await result
            clean = sanitize_tool_output(result)
            record_receipt(name, args, int((time.monotonic() - started) * 1000), "ok", clean["output_hash"], db_path=self.db_path)
            return clean
        except Exception:
            record_receipt(name, args, int((time.monotonic() - started) * 1000), "failed", db_path=self.db_path)
            raise
