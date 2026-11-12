"""Schema-validated MCP tool catalog with single-use mutation gates."""
from __future__ import annotations
import inspect
import re
import time
from dataclasses import dataclass
from typing import Callable, Any
from jsonschema import validate, Draft202012Validator

from .policy import ApprovalPolicy
from .sanitizer import sanitize_tool_output
from .audit import record_receipt


@dataclass(frozen=True)
class Tool:
    name: str
    schema: dict
    execute: Callable[[dict], Any]
    mutating: bool = True  # unknown tools are never assumed read-only
    server: str = "local"
    description: str = ""


class Registry:
    def __init__(self):
        self.tools: dict[str, Tool] = {}
        self._policy = ApprovalPolicy()

    def register(self, tool: Tool):
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", tool.name) or tool.name in self.tools:
            raise ValueError("invalid or duplicate tool name")
        Draft202012Validator.check_schema(tool.schema)
        if tool.schema.get("type") != "object":
            raise ValueError("tool arguments must be an object schema")
        self.tools[tool.name] = tool

    def approve(self, name: str, args: dict, task_id: str = "") -> str:
        """Trusted approval handler only; never expose this method to a planner."""
        validate(args, self.tools[name].schema)
        return self._policy.approve(name, args, task_id)

    def _authorize(self, tool: Tool, args: dict, approval: str | None, task_id: str) -> None:
        validate(args, tool.schema)
        if tool.mutating:
            self._policy.authorize(tool.name, args, approval, task_id)

    def execute(self, name: str, args: dict, approval: str | None = None, *, task_id: str = "") -> dict:
        tool = self.tools[name]
        self._authorize(tool, args, approval, task_id)
        started = time.monotonic()
        try:
            result = tool.execute(args)
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result): result.close()
                raise RuntimeError("async MCP tool requires execute_async")
            clean = sanitize_tool_output(result)
            record_receipt(name, args, int((time.monotonic() - started) * 1000), "ok", clean["output_hash"])
            return clean
        except Exception:
            record_receipt(name, args, int((time.monotonic() - started) * 1000), "failed")
            raise

    async def execute_async(self, name: str, args: dict, approval: str | None = None, *, task_id: str = "") -> dict:
        tool = self.tools[name]
        self._authorize(tool, args, approval, task_id)
        started = time.monotonic()
        try:
            result = tool.execute(args)
            if inspect.isawaitable(result): result = await result
            clean = sanitize_tool_output(result)
            record_receipt(name, args, int((time.monotonic() - started) * 1000), "ok", clean["output_hash"])
            return clean
        except Exception:
            record_receipt(name, args, int((time.monotonic() - started) * 1000), "failed")
            raise
