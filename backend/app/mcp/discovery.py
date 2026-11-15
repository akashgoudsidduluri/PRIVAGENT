"""User-registered MCP stdio and legacy SSE servers via the official Python SDK."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
import re
from urllib.parse import urlsplit

from mcp import ClientSession, StdioServerParameters, types as mcp_types
from mcp.client.stdio import stdio_client
from mcp.client.sse import sse_client

from .. import db
from .registry import Registry, Tool
from .sanitizer import sanitize_tool_description, resolve_tool_mutating_status


@dataclass(frozen=True)
class ServerSpec:
    name: str
    transport: str
    command: str | None = None
    args: tuple[str, ...] = ()
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    env: dict[str, str] = field(default_factory=dict, repr=False)
    read_only_tools: frozenset[str] = frozenset()


class MCPDiscovery:
    def __init__(self, registry: Registry | None = None, *, db_path: str | None = None):
        self.registry = registry or Registry()
        self.servers: dict[str, ServerSpec] = {}
        self.db_path = db_path

    def register_server(self, spec: ServerSpec, *, trusted_user: bool = False, persist: bool = True) -> None:
        if not trusted_user:
            raise PermissionError("MCP server registration requires trusted user configuration")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", spec.name) or spec.name in self.servers:
            raise ValueError("invalid or duplicate server name")
        if spec.transport == "stdio":
            path = Path(spec.command or "")
            if not path.is_absolute() or not path.is_file() or not (path.stat().st_mode & 0o111):
                raise ValueError("stdio server must be an absolute executable path")
            if any("\x00" in arg for arg in spec.args):
                raise ValueError("invalid stdio argument")
            if any("\x00" in k or "\x00" in str(v) for k, v in spec.env.items()):
                raise ValueError("invalid stdio environment variable")
        elif spec.transport == "sse":
            parts = urlsplit(spec.url or "")
            if parts.username or parts.password or parts.fragment or not parts.hostname:
                raise ValueError("invalid MCP SSE URL")
            if parts.hostname in {"localhost", "127.0.0.1", "::1"}:
                if parts.scheme not in ("http", "https"):
                    raise ValueError("local MCP requires http(s)")
            else:
                from ..website_resolver import is_safe_public_url
                if parts.scheme != "https" or not is_safe_public_url(spec.url or "")[0]:
                    raise ValueError("remote MCP requires a safe public HTTPS URL")
        else:
            raise ValueError("unsupported MCP transport")

        self.servers[spec.name] = spec

        if persist:
            db.save_mcp_server({
                "name": spec.name,
                "transport": spec.transport,
                "command": spec.command,
                "args": list(spec.args),
                "url": spec.url,
                "headers": dict(spec.headers),
                "env": dict(spec.env),
                "read_only_tools": list(spec.read_only_tools),
            }, db_path=self.db_path)

    def unregister_server(self, name: str, *, persist: bool = True) -> bool:
        """Remove a server and its catalogued tools."""
        existed = name in self.servers
        self.servers.pop(name, None)
        # Remove tools belonging to this server from registry
        for t_name in list(self.registry.tools):
            if self.registry.tools[t_name].server == name:
                self.registry.tools.pop(t_name, None)
        if persist:
            db.delete_mcp_server(name, db_path=self.db_path)
        return existed

    def rehydrate_from_db(self, *, db_path: str | None = None) -> None:
        """Load all persisted MCP servers and tools from SQLite upon startup."""
        target_db = db_path or self.db_path
        servers = db.list_mcp_servers(db_path=target_db)
        for s in servers:
            spec = ServerSpec(
                name=s["name"],
                transport=s["transport"],
                command=s.get("command"),
                args=tuple(s.get("args") or []),
                url=s.get("url"),
                headers=dict(s.get("headers") or {}),
                env=dict(s.get("env") or {}),
                read_only_tools=frozenset(s.get("read_only_tools") or []),
            )
            self.servers[spec.name] = spec

        # Re-register tools with live invoke delegates
        tools = db.list_mcp_tools(db_path=target_db)
        for t in tools:
            s_name = t["server_name"]
            raw_tool_name = t["name"].split(".", 1)[-1] if "." in t["name"] else t["name"]
            async def invoke(args, srv=s_name, t_name=raw_tool_name):
                return await self.call(srv, t_name, args)

            self.registry.replace(Tool(
                name=t["name"],
                schema=t["schema"],
                execute=invoke,
                mutating=t["mutating"],
                server=s_name,
                description=t.get("description", ""),
                discovered=t.get("discovered", True),
            ))

    @asynccontextmanager
    async def _session(self, spec: ServerSpec):
        if spec.transport == "stdio":
            env_dict = dict(spec.env) if spec.env else None
            server = StdioServerParameters(command=spec.command or "", args=list(spec.args), env=env_dict)
            transport = stdio_client(server)
        else:
            transport = sse_client(spec.url or "", headers=spec.headers, timeout=8, sse_read_timeout=15)
        async with transport as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session

    async def discover(self, name: str) -> list[str]:
        spec = self.servers[name]
        async def collect():
            names = []
            tools_to_save = []
            async with self._session(spec) as session:
                cursor = None
                for _ in range(10):
                    params = mcp_types.PaginatedRequestParams(cursor=cursor) if cursor else None
                    page = await session.list_tools(params=params) if params else await session.list_tools()
                    for item in page.tools:
                        registered = f"{name}.{item.name}"
                        clean_desc = sanitize_tool_description(item.description or "")
                        is_decl_ro = item.name in spec.read_only_tools
                        raw_schema = getattr(item, "inputSchema", None) or getattr(item, "input_schema", {})
                        item_schema = raw_schema.model_dump(mode="json") if hasattr(raw_schema, "model_dump") else raw_schema
                        is_mut = resolve_tool_mutating_status(
                            is_declared_read_only=is_decl_ro,
                            name=item.name,
                            description=clean_desc,
                            schema=item_schema,
                        )

                        async def invoke(args, server_name=name, tool_name=item.name):
                            return await self.call(server_name, tool_name, args)

                        tool_obj = Tool(
                            name=registered,
                            schema=item_schema,
                            execute=invoke,
                            mutating=is_mut,
                            server=name,
                            description=clean_desc,
                            discovered=True,
                        )
                        self.registry.replace(tool_obj)
                        tools_to_save.append({
                            "name": registered,
                            "schema": item_schema,
                            "description": clean_desc,
                            "mutating": is_mut,
                            "discovered": True,
                        })
                        names.append(registered)
                    cursor = getattr(page, "next_cursor", None) or getattr(page, "nextCursor", None)
                    if not cursor: break

            if tools_to_save:
                db.save_mcp_tools(name, tools_to_save, db_path=self.db_path)
            return names
        return await asyncio.wait_for(collect(), timeout=20)

    async def call(self, server_name: str, tool_name: str, arguments: dict):
        spec = self.servers[server_name]
        async def invoke():
            async with self._session(spec) as session:
                result = await session.call_tool(tool_name, arguments)
                return result.model_dump(mode="json")
        return await asyncio.wait_for(invoke(), timeout=30)
