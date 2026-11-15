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
_pending: dict[str, tuple[float, str, dict, str, str]] = {}
_expiry_tasks: set[asyncio.Task] = set()

# Initial rehydrate on startup
try:
    discovery.rehydrate_from_db()
except Exception:
    pass


def _mask_env(env_dict: dict[str, str]) -> dict[str, str]:
    """Mask credentials and tokens in environment variables for public view."""
    masked = {}
    for k, v in (env_dict or {}).items():
        if any(sec in k.upper() for sec in ("TOKEN", "KEY", "SECRET", "PASS", "AUTH", "CRED")):
            masked[k] = "••••••••"
        else:
            masked[k] = v
    return masked


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
    env: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    read_only_tools: list[str] = Field(default_factory=list, alias="readOnlyTools")
    model_config = {"populate_by_name": True}


class ToolInput(BaseModel):
    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    gate_task_id: str | None = Field(default=None, alias="gateTaskId")
    user_id: str = "default_user"
    model_config = {"populate_by_name": True}


@router.get("/servers")
def servers():
    if not discovery.servers:
        try:
            discovery.rehydrate_from_db()
        except Exception:
            pass
    return [
        {
            "name": s.name,
            "transport": s.transport,
            "command": s.command,
            "args": list(s.args),
            "url": s.url,
            "env": _mask_env(s.env),
            "tools": [t.name for t in registry.tools.values() if t.server == s.name],
        }
        for s in discovery.servers.values()
    ]


@router.post("/servers", status_code=201)
def add_server(body: ServerInput):
    try:
        discovery.register_server(
            ServerSpec(
                name=body.name,
                transport=body.transport,
                command=body.command,
                args=tuple(body.args),
                url=body.url,
                headers=dict(body.headers),
                env=dict(body.env),
                read_only_tools=frozenset(body.read_only_tools),
            ),
            trusted_user=True,
            persist=True,
        )
    except (PermissionError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from None
    return {"name": body.name, "status": "registered"}


@router.delete("/servers/{name}")
def delete_server(name: str):
    if name not in discovery.servers:
        raise HTTPException(404, "unknown server")
    discovery.unregister_server(name, persist=True)
    return {"name": name, "status": "deleted"}


@router.post("/servers/{name}/discover")
async def discover(name: str):
    if name not in discovery.servers:
        raise HTTPException(404, "unknown server")
    try:
        tools = await discovery.discover(name)
    except Exception:
        raise HTTPException(502, "MCP discovery failed") from None
    return {"tools": tools}


@router.get("/presets")
def presets():
    import sys
    presets_list = [
        {
            "id": "sqlite_explorer",
            "name": "sqlite_explorer",
            "title": "SQLite Database Explorer",
            "description": "Inspect local SQLite databases, query schemas, and run read or mutation operations.",
            "transport": "stdio",
            "command": sys.executable,
            "args": ["-m", "backend.app.mcp.servers.sqlite_server"],
            "readOnlyTools": ["list_tables", "read_query", "describe_table"],
            "env": {},
        }
    ]
    return presets_list


@router.post("/presets/{preset_id}/install")
async def install_preset(preset_id: str):
    import sys
    if preset_id != "sqlite_explorer":
        raise HTTPException(404, f"unknown preset '{preset_id}'")
    spec = ServerSpec(
        name="sqlite_explorer",
        transport="stdio",
        command=sys.executable,
        args=("-m", "backend.app.mcp.servers.sqlite_server"),
        read_only_tools=frozenset(["list_tables", "read_query", "describe_table"]),
        env={},
    )
    if spec.name not in discovery.servers:
        try:
            discovery.register_server(spec, trusted_user=True, persist=True)
        except Exception as exc:
            raise HTTPException(422, f"Failed registering preset: {exc}") from None
    try:
        tools = await discovery.discover(spec.name)
    except Exception as exc:
        raise HTTPException(502, f"Preset discovery failed: {exc}") from None
    return {"status": "installed", "name": spec.name, "tools": tools}


@router.get("/tools")
def tools():
    return [
        {
            "name": t.name,
            "description": t.description,
            "schema": t.schema,
            "mutating": t.mutating,
            "server": t.server,
        }
        for t in registry.tools.values()
    ]


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
    task.pending_confirmation = {
        "message": f"Approve {body.name} with these arguments? {safe}",
        "action": {"type": "mcp_tool", "name": body.name},
    }

    user_id = body.user_id or "default_user"
    binding = hashlib.sha256(
        json.dumps([user_id, task.task_id, tool.server, body.name, body.args], sort_keys=True, default=str).encode()
    ).hexdigest()
    _pending[task.task_id] = (time.monotonic() + 120, body.name, body.args, binding, tool.server)
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
    task = None
    user_id = body.user_id or "default_user"
    if tool.mutating:
        pending = _pending.pop(body.gate_task_id or "", None)
        task = hub.tasks.get(body.gate_task_id or "")
        binding = hashlib.sha256(
            json.dumps([user_id, body.gate_task_id or "", tool.server, body.name, body.args], sort_keys=True, default=str).encode()
        ).hexdigest()
        if not pending or not task or pending[0] < time.monotonic() or pending[1] != body.name or pending[3] != binding or task.confirm_decision is not True:
            raise HTTPException(403, "fresh approved tool gate required")
        approval = registry.approve(body.name, body.args, task.task_id, user_id=user_id, server=tool.server)
        task.state = "running"
        await hub.broadcast_progress(task)
    try:
        result = await registry.execute_async(body.name, body.args, approval, task_id=body.gate_task_id or "", user_id=user_id)
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
    return {**result, "effect_verified": False} if tool.mutating else result


@router.get("/audit")
def audit():
    with db.get_connection() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM mcp_audit ORDER BY created_at DESC LIMIT 100")]
