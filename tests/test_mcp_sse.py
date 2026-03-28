from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from golem.mcp_sse import McpSessionRegistry


@pytest.mark.asyncio
async def test_register_and_list_tools(tmp_path: Path) -> None:
    """Registered session's tools are returned by tools/list."""
    from claude_agent_sdk import SdkMcpTool

    registry = McpSessionRegistry()

    async def _echo(args: dict[str, object]) -> dict[str, object]:
        return {"content": [{"type": "text", "text": "ok"}]}

    tools = [SdkMcpTool(name="my_tool", description="A tool", input_schema={"type": "object", "properties": {}}, handler=_echo)]
    registry.register("sess-1", tools)

    resp = await registry.handle_message("sess-1", {
        "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {},
    })
    assert "result" in resp
    tool_names = [t["name"] for t in resp["result"]["tools"]]
    assert "my_tool" in tool_names


@pytest.mark.asyncio
async def test_call_tool(tmp_path: Path) -> None:
    """tools/call dispatches to the registered handler."""
    from claude_agent_sdk import SdkMcpTool

    registry = McpSessionRegistry()
    called_with: dict[str, object] = {}

    async def _capture(args: dict[str, object]) -> dict[str, object]:
        called_with.update(args)
        return {"content": [{"type": "text", "text": json.dumps({"sum": args.get("a", 0)})}]}

    tools = [SdkMcpTool(name="add", description="Add", input_schema={"type": "object", "properties": {"a": {"type": "integer"}}}, handler=_capture)]
    registry.register("sess-1", tools)

    resp = await registry.handle_message("sess-1", {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "add", "arguments": {"a": 42}},
    })
    assert called_with["a"] == 42
    content = resp["result"]["content"]
    assert any("42" in c.get("text", "") for c in content)


@pytest.mark.asyncio
async def test_unregistered_session_returns_error() -> None:
    """Calling tools on an unregistered session returns JSONRPC error."""
    registry = McpSessionRegistry()
    resp = await registry.handle_message("nonexistent", {
        "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {},
    })
    assert "error" in resp
    assert resp["error"]["code"] == -32001


@pytest.mark.asyncio
async def test_unregister_cleans_up() -> None:
    """After unregister, session tools are gone."""
    from claude_agent_sdk import SdkMcpTool

    registry = McpSessionRegistry()

    async def _noop(args: dict[str, object]) -> dict[str, object]:
        return {"content": [{"type": "text", "text": "ok"}]}

    registry.register("sess-1", [SdkMcpTool(name="t", description="t", input_schema={}, handler=_noop)])
    registry.unregister("sess-1")

    resp = await registry.handle_message("sess-1", {
        "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {},
    })
    assert "error" in resp


@pytest.mark.asyncio
async def test_initialize_returns_capabilities() -> None:
    """initialize method returns server info and capabilities."""
    registry = McpSessionRegistry()
    registry.register("sess-1", [])

    resp = await registry.handle_message("sess-1", {
        "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {},
    })
    assert resp["result"]["capabilities"]["tools"] == {}
    assert resp["result"]["serverInfo"]["name"] == "golem"


@pytest.mark.asyncio
async def test_unknown_tool_returns_error() -> None:
    """Calling a nonexistent tool returns JSONRPC error."""
    registry = McpSessionRegistry()
    registry.register("sess-1", [])

    resp = await registry.handle_message("sess-1", {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "nope", "arguments": {}},
    })
    assert "error" in resp
    assert "not found" in resp["error"]["message"].lower()
