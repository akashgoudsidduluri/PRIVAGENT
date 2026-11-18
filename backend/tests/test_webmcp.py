"""Task 14: WebMCP detection / validation / sensitivity tests.

WebMCP tools are page declarations — untrusted page content. The
parser validates structure (name shape, bounded description, JSON
Schema inputs/outputs) and the validator only lets the planner call
a tool this observation actually declared, with arguments matching
its input schema.
"""
from __future__ import annotations

import httpx
import pytest

from app.webmcp import (
    WebMCPTool,
    fetch_webmcp_manifest,
    format_webmcp_tools_for_prompt,
    parse_webmcp_manifest,
    parse_webmcp_tools,
    tool_requests_sensitive,
    validate_webmcp_call,
)

ORIGIN = "https://shop.example.com"


def _tool(**kw):
    d = {
        "name": "search_products",
        "description": "Search the catalogue by keyword.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    }
    d.update(kw)
    return d


def test_parse_valid_tool():
    tools, dropped = parse_webmcp_tools([_tool()], ORIGIN)
    assert len(tools) == 1 and dropped == 0
    assert tools[0].name == "search_products"
    assert tools[0].origin == ORIGIN


def test_parse_rejects_bad_names():
    tools, dropped = parse_webmcp_tools(
        [_tool(name="9bad"), _tool(name="has space"), _tool(name="ok_tool")],
        ORIGIN,
    )
    assert [t.name for t in tools] == ["ok_tool"]
    assert dropped == 2


def test_parse_rejects_bad_schemas_and_long_descriptions():
    tools, dropped = parse_webmcp_tools(
        [
            _tool(name="bad_schema", inputSchema={"type": "not-a-json-schema-type"}),
            _tool(name="long_desc", description="x" * 2001),
            _tool(name="no_schema", inputSchema=None),
            _tool(name="good"),
        ],
        ORIGIN,
    )
    assert [t.name for t in tools] == ["good"]
    assert dropped == 3


def test_parse_caps_tools_and_drops_duplicates():
    raw = [_tool(name=f"tool_{i}") for i in range(55)]
    raw.append(_tool(name="tool_0"))  # duplicate
    tools, dropped = parse_webmcp_tools(raw, ORIGIN)
    assert len(tools) == 50
    assert dropped == 6  # 5 over the cap + 1 duplicate


def test_parse_non_list_input_is_empty():
    tools, dropped = parse_webmcp_tools(None, ORIGIN)
    assert tools == [] and dropped == 0
    tools, dropped = parse_webmcp_tools({"name": "x"}, ORIGIN)
    assert tools == [] and dropped == 0


def test_manifest_parsing():
    tools, dropped, version = parse_webmcp_manifest(
        {"version": "1.0", "tools": [_tool()]}, ORIGIN
    )
    assert len(tools) == 1 and dropped == 0 and version == "1.0"


def test_manifest_rejects_non_object():
    tools, dropped, version = parse_webmcp_manifest("not json", ORIGIN)
    assert tools == [] and version == "" and dropped == 1


def test_fetch_manifest_same_origin_only():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/.well-known/webmcp.json"
        return httpx.Response(200, json={"tools": [_tool()]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    tools, dropped, version = fetch_webmcp_manifest(ORIGIN, client=client)
    assert [t.name for t in tools] == ["search_products"]


def test_fetch_manifest_raises_on_missing():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "nope"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(ValueError, match="404"):
        fetch_webmcp_manifest(ORIGIN, client=client)


def test_fetch_manifest_rejects_redirects():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example.com/webmcp.json"})

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    with pytest.raises(ValueError, match="redirect"):
        fetch_webmcp_manifest(ORIGIN, client=client)


def test_validate_call_happy_path():
    tools, _ = parse_webmcp_tools([_tool()], ORIGIN)
    assert validate_webmcp_call("search_products", {"query": "shoes"}, tools) is None


def test_validate_call_unknown_tool():
    tools, _ = parse_webmcp_tools([_tool()], ORIGIN)
    err = validate_webmcp_call("other_tool", {}, tools)
    assert err and "not declared" in err


def test_validate_call_schema_mismatch():
    tools, _ = parse_webmcp_tools([_tool()], ORIGIN)
    assert validate_webmcp_call("search_products", {}, tools) is not None  # missing required
    assert validate_webmcp_call("search_products", {"query": 42}, tools) is not None  # wrong type
    assert validate_webmcp_call("search_products", {"query": "x", "extra": 1}, tools) is None


def test_tool_requests_sensitive():
    plain, _ = parse_webmcp_tools([_tool()], ORIGIN)
    assert tool_requests_sensitive(plain[0]) is None

    creds, _ = parse_webmcp_tools(
        [_tool(name="login", description="Sign in with your password")], ORIGIN
    )
    assert tool_requests_sensitive(creds[0]) == "credentials"

    pay, _ = parse_webmcp_tools(
        [_tool(name="checkout", description="Pay for the order with your card")], ORIGIN
    )
    assert tool_requests_sensitive(pay[0]) == "payment"

    schema_sensitive, _ = parse_webmcp_tools(
        [_tool(name="vault_lookup", inputSchema={
            "type": "object",
            "properties": {"api_token": {"type": "string"}},
        })],
        ORIGIN,
    )
    assert tool_requests_sensitive(schema_sensitive[0]) == "credentials"


def test_prompt_block_is_tagged_untrusted():
    tools, _ = parse_webmcp_tools([_tool()], ORIGIN)
    block = format_webmcp_tools_for_prompt(tools)
    assert 'untrusted="true"' in block
    assert "search_products" in block
    assert "Search the catalogue" in block
    assert format_webmcp_tools_for_prompt([]) == ""


def test_tool_round_trip_through_dict():
    tools, _ = parse_webmcp_tools([_tool()], ORIGIN)
    d = tools[0].to_dict()
    back = WebMCPTool.from_dict(d)
    assert back.name == tools[0].name
    assert back.origin == ORIGIN
    assert back.input_schema == tools[0].input_schema
