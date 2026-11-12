"""Trusted local catalog and execution endpoints; mutations use the human task gate."""
from __future__ import annotations
import hashlib
import asyncio
import json
import time
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from jsonschema import validate, ValidationError

from .. import db
from ..hub import hub
from .discovery import MCPDiscovery, ServerSpec
from .registry import Registry
from .sanitizer import sanitize_tool_output

router = APIRouter(prefix="/api/mcp")
registry = Registry()
discovery = MCPDiscovery(registry)
_pending: dict[str, tuple[float, str, dict, str]] = {}
_expiry_tasks: set[asyncio.Task] = set()


async def _expire_gate(task_id: str):
    await asyncio.sleep(120)
    if _pending.pop(task_id, None) is not None:
        task = hub.tasks.get(task_id)
        if task and task.state == "awaiting_confirmation":
            task.state = "failed"
            task.pending_confirmation = None
            task.message = "Tool approval expired."
            await hub.broadcast_progress(task)


class ServerInput(BaseModel):
    name: str
    transport: Literal["stdio", "sse"]
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    read_only_tools: list[str] = Field(default_factory=list, alias="readOnlyTools")
    model_config = {"populate_by_name": True}


class ToolInput(BaseModel):
    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    gate_task_id: str | None = Field(default=None, alias="gateTaskId")
    model_config = {"populate_by_name": True}


@router.get("/servers")
def servers():
    return [{"name": s.name, "transport": s.transport, "url": s.url, "tools": [t.name for t in registry.tools.values() if t.server == s.name]}
            for s in discovery.servers.values()]


@router.post("/servers", status_code=201)
def add_server(body: ServerInput):
    try:
        discovery.register_server(ServerSpec(body.name, body.transport, body.command, tuple(body.args), body.url,
                                              read_only_tools=frozenset(body.read_only_tools)), trusted_user=True)
    except (PermissionError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from None
    return {"name": body.name, "status": "registered"}


@router.post("/servers/{name}/discover")
async def discover(name: str):
    if name not in discovery.servers:
        raise HTTPException(404, "unknown server")
    try:
        tools = await discovery.discover(name)
    except Exception:
        raise HTTPException(502, "MCP discovery failed") from None
    return {"tools": tools}


@router.get("/tools")
def tools():
    return [{"name": t.name, "description": t.description, "schema": t.schema, "mutating": t.mutating, "server": t.server}
            for t in registry.tools.values()]


@router.post("/prepare")
async def prepare(body: ToolInput):
    tool = registry.tools.get(body.name)
    if not tool:
        raise HTTPException(404, "unknown tool")
    if not tool.mutating:
        raise HTTPException(409, "read-only tools do not require a gate")
    try:
        validate(body.args, tool.schema)
    except ValidationError:
        raise HTTPException(422, "invalid tool arguments") from None
    task = hub.new_task(f"Approve MCP tool {body.name}")
    task.state = "awaiting_confirmation"
    safe = sanitize_tool_output(body.args, 1000)["content"]
    task.pending_confirmation = {"message": f"Approve {body.name} with these arguments? {safe}",
                                 "action": {"type": "mcp_tool", "name": body.name}}
    binding = hashlib.sha256(json.dumps([body.name, body.args], sort_keys=True, default=str).encode()).hexdigest()
    _pending[task.task_id] = (time.monotonic() + 120, body.name, body.args, binding)
    expiry = asyncio.create_task(_expire_gate(task.task_id))
    _expiry_tasks.add(expiry)
    expiry.add_done_callback(_expiry_tasks.discard)
    await hub.broadcast_progress(task)
    return {"gateTaskId": task.task_id, "message": task.pending_confirmation["message"]}


@router.post("/execute")
async def execute(body: ToolInput):
    tool = registry.tools.get(body.name)
    if not tool:
        raise HTTPException(404, "unknown tool")
    approval = None
    if tool.mutating:
        pending = _pending.pop(body.gate_task_id or "", None)
        task = hub.tasks.get(body.gate_task_id or "")
        binding = hashlib.sha256(json.dumps([body.name, body.args], sort_keys=True, default=str).encode()).hexdigest()
        if not pending or not task or pending[0] < time.monotonic() or pending[1] != body.name or pending[3] != binding or task.confirm_decision is not True:
            raise HTTPException(403, "fresh approved tool gate required")
        approval = registry.approve(body.name, body.args, task.task_id)
        task.state = "running"
        await hub.broadcast_progress(task)
    try:
        result = await registry.execute_async(body.name, body.args, approval, task_id=body.gate_task_id or "")
    except (PermissionError, ValidationError, ValueError):
        raise HTTPException(422, "tool execution rejected") from None
    except Exception:
        if tool.mutating and task:
            task.state = "failed"
            task.message = "Tool execution failed."
            await hub.broadcast_progress(task)
        raise HTTPException(502, "tool execution failed") from None
    if tool.mutating and task:
        task.state = "failed"
        task.message = "Tool call returned, but the external effect lacks authoritative confirmation. Verify it at the service."
        await hub.broadcast_progress(task)
    return {**result, "effect_verified": False} if tool.mutating else result


@router.get("/audit")
def audit():
    with db.get_connection() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM mcp_audit ORDER BY created_at DESC LIMIT 100")]
