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


from golem.tools import create_golem_mcp_sse_config


def test_create_golem_mcp_sse_config() -> None:
    """create_golem_mcp_sse_config returns McpSSEServerConfig."""
    config = create_golem_mcp_sse_config("sess-1", "http://127.0.0.1:7665")
    assert config["type"] == "sse"
    assert config["url"] == "http://127.0.0.1:7665/mcp/sess-1/sse"


def test_create_junior_dev_mcp_sse_config() -> None:
    """Junior dev SSE config uses session-jd path."""
    config = create_golem_mcp_sse_config("sess-1-jd", "http://127.0.0.1:7665", name="golem-junior-dev")
    assert config["type"] == "sse"
    assert config["url"] == "http://127.0.0.1:7665/mcp/sess-1-jd/sse"


from httpx import ASGITransport, AsyncClient

from golem.server import create_app


@pytest.fixture()
def app():
    return create_app()


@pytest.fixture()
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_mcp_sse_endpoint_404_without_session(client: AsyncClient) -> None:
    """GET /mcp/{id}/sse returns 404 for unregistered session."""
    resp = await client.get("/mcp/nonexistent/sse")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_mcp_message_endpoint_404_without_session(client: AsyncClient) -> None:
    """POST /mcp/{id}/message returns 404 for unregistered session."""
    resp = await client.post("/mcp/nonexistent/message", content="{}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_mcp_message_tools_list(client: AsyncClient, tmp_path: Path) -> None:
    """POST /mcp/{id}/message with tools/list returns registered tools after session creation."""
    spec = tmp_path / "spec.md"
    spec.write_text("# Test\n", encoding="utf-8")

    resp = await client.post("/api/sessions", json={
        "spec_path": str(spec),
        "project_root": str(tmp_path),
    })
    session_id = resp.json()["session_id"]

    resp = await client.post(f"/mcp/{session_id}/message", content=json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {},
    }))
    assert resp.status_code == 200
    data = resp.json()
    tool_names = [t["name"] for t in data["result"]["tools"]]
    assert "create_ticket" in tool_names
    assert "update_ticket" in tool_names


@pytest.mark.asyncio
async def test_mcp_message_create_and_read_ticket(client: AsyncClient, tmp_path: Path) -> None:
    """Full MCP lifecycle: create ticket via message endpoint, read it back."""
    spec = tmp_path / "spec.md"
    spec.write_text("# Test\n", encoding="utf-8")

    resp = await client.post("/api/sessions", json={
        "spec_path": str(spec),
        "project_root": str(tmp_path),
    })
    session_id = resp.json()["session_id"]

    # Create ticket
    resp = await client.post(f"/mcp/{session_id}/message", content=json.dumps({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "create_ticket", "arguments": {
            "type": "task", "title": "SSE test ticket", "assigned_to": "writer",
        }},
    }))
    assert resp.status_code == 200
    ticket_text = resp.json()["result"]["content"][0]["text"]
    ticket_id = json.loads(ticket_text)["ticket_id"]

    # Read it back
    resp = await client.post(f"/mcp/{session_id}/message", content=json.dumps({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "read_ticket", "arguments": {"ticket_id": ticket_id}},
    }))
    ticket_data = json.loads(resp.json()["result"]["content"][0]["text"])
    assert ticket_data["title"] == "SSE test ticket"


@pytest.mark.asyncio
async def test_mcp_junior_dev_tools_separate(client: AsyncClient, tmp_path: Path) -> None:
    """Junior Dev MCP tools are registered separately from Tech Lead tools."""
    spec = tmp_path / "spec.md"
    spec.write_text("# Test\n", encoding="utf-8")

    resp = await client.post("/api/sessions", json={
        "spec_path": str(spec),
        "project_root": str(tmp_path),
    })
    session_id = resp.json()["session_id"]

    # Tech Lead tools (full set)
    resp = await client.post(f"/mcp/{session_id}/message", content=json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {},
    }))
    tl_tools = [t["name"] for t in resp.json()["result"]["tools"]]
    assert "create_ticket" in tl_tools
    assert "create_worktree" in tl_tools
    assert len(tl_tools) == 8

    # Junior Dev tools (limited set)
    resp = await client.post(f"/mcp/{session_id}-jd/message", content=json.dumps({
        "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
    }))
    assert resp.status_code == 200
    jd_tools = [t["name"] for t in resp.json()["result"]["tools"]]
    assert set(jd_tools) == {"run_qa", "update_ticket", "read_ticket"}
    assert "create_ticket" not in jd_tools
    assert "create_worktree" not in jd_tools


@pytest.mark.asyncio
async def test_mcp_cleaned_up_after_session_delete(client: AsyncClient, tmp_path: Path) -> None:
    """MCP tools are unregistered when session is deleted."""
    spec = tmp_path / "spec.md"
    spec.write_text("# Test\n", encoding="utf-8")

    resp = await client.post("/api/sessions", json={
        "spec_path": str(spec),
        "project_root": str(tmp_path),
    })
    session_id = resp.json()["session_id"]

    # Tools work before delete
    resp = await client.post(f"/mcp/{session_id}/message", content=json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {},
    }))
    assert resp.status_code == 200

    # Delete session
    await client.delete(f"/api/sessions/{session_id}")

    # Tools gone after delete
    resp = await client.post(f"/mcp/{session_id}/message", content=json.dumps({
        "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
    }))
    assert resp.status_code == 404
