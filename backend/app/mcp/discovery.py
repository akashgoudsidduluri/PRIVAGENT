"""User-registered MCP stdio and legacy SSE servers via the official Python SDK."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
import re
from urllib.parse import urlsplit

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.sse import sse_client

from .registry import Registry, Tool


@dataclass(frozen=True)
class ServerSpec:
    name: str
    transport: str
    command: str | None = None
    args: tuple[str, ...] = ()
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    read_only_tools: frozenset[str] = frozenset()


class MCPDiscovery:
    def __init__(self, registry: Registry | None = None):
        self.registry = registry or Registry()
        self.servers: dict[str, ServerSpec] = {}

    def register_server(self, spec: ServerSpec, *, trusted_user: bool = False) -> None:
        if not trusted_user:
            raise PermissionError("MCP server registration requires trusted user configuration")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", spec.name) or spec.name in self.servers:
            raise ValueError("invalid or duplicate server name")
        if spec.transport == "stdio":
            path = Path(spec.command or "")
            if not path.is_absolute() or not path.is_file() or not path.stat().st_mode & 0o111:
                raise ValueError("stdio server must be an absolute executable path")
            if any("\x00" in arg for arg in spec.args):
                raise ValueError("invalid stdio argument")
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

    @asynccontextmanager
    async def _session(self, spec: ServerSpec):
        if spec.transport == "stdio":
            server = StdioServerParameters(command=spec.command or "", args=list(spec.args))
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
            async with self._session(spec) as session:
                cursor = None
                for _ in range(10):
                    page = await session.list_tools(cursor=cursor)
                    for item in page.tools:
                        registered = f"{name}.{item.name}"
                        async def invoke(args, server_name=name, tool_name=item.name):
                            return await self.call(server_name, tool_name, args)
                        self.registry.register(Tool(registered, item.inputSchema, invoke,
                                                    mutating=item.name not in spec.read_only_tools,
                                                    server=name, description=item.description or ""))
                        names.append(registered)
                    cursor = page.nextCursor
                    if not cursor: break
            return names
        return await asyncio.wait_for(collect(), timeout=20)

    async def call(self, server_name: str, tool_name: str, arguments: dict):
        spec = self.servers[server_name]
        async def invoke():
            async with self._session(spec) as session:
                result = await session.call_tool(tool_name, arguments)
                return result.model_dump(mode="json")
        return await asyncio.wait_for(invoke(), timeout=30)
