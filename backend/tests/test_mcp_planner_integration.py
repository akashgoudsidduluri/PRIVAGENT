"""Milestone 2 Test Suite: MCP Integration into Shared Agent Planner.

Validates end-to-end execution of read-only queries and gated mutating operations
through PrivAgent's unified execution loop.
"""
import asyncio
import pytest
from app import db
from app.hub import hub, TaskRecord
from app.intent_router import classify_intent, TaskIntent
from app.mcp.discovery import MCPDiscovery, ServerSpec
from app.mcp.registry import Registry, Tool
from app.mcp.coordinator import (
    is_mcp_directive,
    resolve_tool_for_instruction,
    execute_mcp_directive,
)
from app.agentLoop import run_task


@pytest.mark.asyncio
async def test_mcp_read_only_query_scenario_a(tmp_path):
    """Scenario A: 'Find the tables in my local SQLite database and tell me which table contains user information.'

    PrivAgent must:
    1. Understand the request as a non-browser MCP capability directive.
    2. Discover and select the appropriate MCP tool (sqlite.list_tables).
    3. Validate arguments against schema.
    4. Execute directly without confirmation pause (read-only).
    5. Interpret result and return evidence stating 'users' table contains user info.
    """
    test_db = str(tmp_path / "scenario_a.db")
    db.init_db(test_db)

    registry = Registry(db_path=test_db)
    discovery = MCPDiscovery(registry, db_path=test_db)

    # Register local mock SQLite tool
    calls = []
    def mock_list_tables(args):
        calls.append(args)
        return {"tables": ["users", "orders", "products"], "database": "local.db"}

    tool = Tool(
        name="sqlite.list_tables",
        schema={"type": "object", "properties": {}},
        execute=mock_list_tables,
        mutating=False,
        server="sqlite",
        description="List all table names in the SQLite database",
    )
    registry.register(tool)

    instruction = "Find the tables in my local SQLite database and tell me which table contains user information."

    # 1. Routing check
    assert is_mcp_directive(instruction) is True
    classification = classify_intent(instruction)
    assert classification.intent == TaskIntent.NON_BROWSER
    assert "mcp" in classification.reason.lower()

    # 2. Tool selection
    matched_tool, args = resolve_tool_for_instruction(instruction, list(registry.tools.values()))
    assert matched_tool.name == "sqlite.list_tables"
    assert matched_tool.mutating is False

    # 3. Execution via unified coordinator
    record = hub.new_task(instruction)
    response = await execute_mcp_directive(
        instruction=instruction,
        record=record,
        db_path=test_db,
        registry=registry,
        discovery=discovery,
    )

    # 4. Verify evidence and response
    assert len(calls) == 1
    assert "users" in response
    assert "table contains user information" in response
    assert "sqlite.list_tables" in response
    assert "Verified MCP receipt" in response

    # 5. Verify audit log entry in SQLite
    with db.get_connection(test_db) as conn:
        receipts = conn.execute("SELECT * FROM mcp_audit WHERE tool_name = 'sqlite.list_tables'").fetchall()
        assert len(receipts) == 1
        assert receipts[0]["status"] == "ok"


@pytest.mark.asyncio
async def test_mcp_mutating_query_scenario_b_approval_and_rejection(tmp_path):
    """Scenario B: 'Delete the test database.'

    PrivAgent must:
    1. Identify that deleting is a mutating operation.
    2. Pause for explicit human confirmation (task.state == 'awaiting_confirmation').
    3. If approved -> execute with single-use token and confirm.
    4. If rejected -> fail-closed without calling the tool.
    """
    test_db = str(tmp_path / "scenario_b.db")
    db.init_db(test_db)

    registry = Registry()
    discovery = MCPDiscovery(registry, db_path=test_db)

    mutation_calls = []
    def mock_delete_db(args):
        mutation_calls.append(args)
        return {"status": "deleted", "target": args.get("target")}

    tool = Tool(
        name="sqlite.delete_database",
        schema={"type": "object", "properties": {"target": {"type": "string"}}},
        execute=mock_delete_db,
        mutating=True,
        server="sqlite",
        description="Permanently delete or drop a test database",
    )
    registry.register(tool)

    instruction = "Delete the test database."

    # Part 1: Rejection test
    record_reject = hub.new_task(instruction)

    async def reject_after_pause():
        for _ in range(50):
            if record_reject.state == "awaiting_confirmation":
                record_reject.confirm_decision = False
                record_reject.confirm_event.set()
                break
            await asyncio.sleep(0.01)

    asyncio.create_task(reject_after_pause())

    resp_reject = await execute_mcp_directive(
        instruction=instruction,
        record=record_reject,
        db_path=test_db,
        registry=registry,
        discovery=discovery,
    )
    assert record_reject.state == "cancelled"
    assert "rejected" in resp_reject.lower() or "cancelled" in resp_reject.lower()
    assert len(mutation_calls) == 0, "Mutating tool must NOT be called when rejected!"

    # Part 2: Approval test
    record_approve = hub.new_task(instruction)

    async def approve_after_pause():
        for _ in range(50):
            if record_approve.state == "awaiting_confirmation":
                record_approve.confirm_decision = True
                record_approve.confirm_event.set()
                break
            await asyncio.sleep(0.01)

    asyncio.create_task(approve_after_pause())

    resp_approve = await execute_mcp_directive(
        instruction=instruction,
        record=record_approve,
        db_path=test_db,
        registry=registry,
        discovery=discovery,
    )
    assert "Approved Mutation Executed" in resp_approve
    assert "sqlite.delete_database" in resp_approve
    assert len(mutation_calls) == 1
    assert mutation_calls[0] == {"target": "test_database"}


@pytest.mark.asyncio
async def test_mcp_catalog_overview_and_chat_query(tmp_path):
    """Test 'what mcp tools do i have?' conversational inspection."""
    test_db = str(tmp_path / "mcp_catalog.db")
    db.init_db(test_db)

    registry = Registry()
    discovery = MCPDiscovery(registry, db_path=test_db)

    # Add mock server and tool
    spec = ServerSpec(name="fs_server", transport="stdio", command="mock_bin", read_only_tools=frozenset(["read_file"]))
    discovery.servers["fs_server"] = spec

    registry.register(Tool(
        name="fs_server.read_file",
        schema={"type": "object", "properties": {"path": {"type": "string"}}},
        execute=lambda a: {"content": "file data"},
        mutating=False,
        server="fs_server",
        description="Read file contents from filesystem",
    ))

    record = hub.new_task("what mcp tools do i have?")
    resp = await execute_mcp_directive(
        instruction="what mcp tools do i have?",
        record=record,
        db_path=test_db,
        registry=registry,
        discovery=discovery,
    )

    assert "fs_server" in resp
    assert "fs_server.read_file" in resp
    assert "Read-Only" in resp


@pytest.mark.asyncio
async def test_end_to_end_agent_loop_mcp_execution(tmp_path, monkeypatch):
    """Test full agent loop execution (run_task) with global discovery/registry."""
    test_db = str(tmp_path / "e2e_mcp.db")
    monkeypatch.setenv("PRIVAGENT_DB_PATH", test_db)
    db.init_db(test_db)

    from app.mcp import api as mcp_api
    mcp_api.discovery.servers.clear()
    mcp_api.registry.tools.clear()

    # Register local mock SQLite tool
    def mock_tables(args):
        return {"tables": ["users", "sessions", "settings"], "count": 3}

    mcp_api.registry.register(Tool(
        name="sqlite.list_tables",
        schema={"type": "object", "properties": {}},
        execute=mock_tables,
        mutating=False,
        server="sqlite",
        description="List all SQLite tables",
    ))

    # Run through the ONLY agent loop (run_task)
    instruction = "Find the tables in my local SQLite database and tell me which table contains user information."
    record = hub.new_task(instruction)
    await run_task(record.task_id)

    assert record.state == "completed"
    assert "users" in record.message
    assert "table contains user information" in record.message
    assert "sqlite.list_tables" in record.message


@pytest.mark.asyncio
async def test_conversational_tool_discovery_and_server_removal(tmp_path):
    """Test natural language 'discover tools' and 'remove mcp server <name>' in chat."""
    test_db = str(tmp_path / "conv_mgmt.db")
    db.init_db(test_db)

    registry = Registry(db_path=test_db)
    discovery = MCPDiscovery(registry, db_path=test_db)

    # Register mock server
    spec = ServerSpec(
        name="demo_db",
        transport="stdio",
        command="mock_bin",
        read_only_tools=frozenset(["lookup"]),
    )
    discovery.servers["demo_db"] = spec

    # Mock discover method
    async def mock_discover(name):
        t = Tool(
            name=f"{name}.lookup",
            schema={"type": "object"},
            execute=lambda a: {"result": "ok"},
            mutating=False,
            server=name,
            description="Lookup read-only key",
        )
        registry.register(t)
        return [t.name]

    discovery.discover = mock_discover

    # 1. Discover tools via chat
    record = hub.new_task("discover tools for demo_db")
    resp_discover = await execute_mcp_directive(
        instruction="discover tools for demo_db",
        record=record,
        db_path=test_db,
        registry=registry,
        discovery=discovery,
    )
    assert "MCP Discovery Complete" in resp_discover
    assert "demo_db.lookup" in resp_discover
    assert "Read-Only" in resp_discover

    # 2. Remove server via chat
    record_remove = hub.new_task("remove mcp server demo_db")
    resp_remove = await execute_mcp_directive(
        instruction="remove mcp server demo_db",
        record=record_remove,
        db_path=test_db,
        registry=registry,
        discovery=discovery,
    )
    assert "MCP Server Removed" in resp_remove
    assert "demo_db" in resp_remove
    assert "demo_db" not in discovery.servers
    assert "demo_db.lookup" not in registry.tools


@pytest.mark.asyncio
async def test_conversational_sqlite_preset_activation(tmp_path):
    """Test activating built-in SQLite preset directly through conversation."""
    test_db = str(tmp_path / "preset_conv.db")
    db.init_db(test_db)

    registry = Registry(db_path=test_db)
    discovery = MCPDiscovery(registry, db_path=test_db)

    record = hub.new_task("enable sqlite preset")
    resp = await execute_mcp_directive(
        instruction="enable sqlite preset",
        record=record,
        db_path=test_db,
        registry=registry,
        discovery=discovery,
    )
    assert "SQLite Preset Activated" in resp
    assert "sqlite_explorer" in discovery.servers
    assert "sqlite_explorer.list_tables" in resp


def test_mcp_presets_and_server_api_endpoints(tmp_path, monkeypatch):
    """Test /api/mcp/presets and /api/mcp/presets/{id}/install HTTP endpoints."""
    from fastapi.testclient import TestClient
    from app.main import app

    test_db = str(tmp_path / "api_presets.db")
    monkeypatch.setenv("PRIVAGENT_DB_PATH", test_db)
    db.init_db(test_db)

    client = TestClient(app)

    # 1. Get presets
    res_presets = client.get("/api/mcp/presets")
    assert res_presets.status_code == 200
    presets = res_presets.json()
    assert any(p["id"] == "sqlite_explorer" for p in presets)

    # 2. Install preset
    res_install = client.post("/api/mcp/presets/sqlite_explorer/install")
    assert res_install.status_code == 200
    installed_data = res_install.json()
    assert installed_data["status"] == "installed"
    assert installed_data["name"] == "sqlite_explorer"
    assert "sqlite_explorer.list_tables" in installed_data["tools"]

    # 3. Check servers list
    res_servers = client.get("/api/mcp/servers")
    assert res_servers.status_code == 200
    srv_names = [s["name"] for s in res_servers.json()]
    assert "sqlite_explorer" in srv_names

    # 4. Delete server
    res_delete = client.delete("/api/mcp/servers/sqlite_explorer")
    assert res_delete.status_code == 200
    assert res_delete.json()["status"] == "deleted"


