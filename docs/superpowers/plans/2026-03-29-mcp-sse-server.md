# MCP-over-SSE Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move MCP tool serving from in-process SDK pipes to an external SSE endpoint on the FastAPI server, so MCP tools survive SDK transport failures.

**Architecture:** New `mcp_sse.py` module handles MCP-over-SSE protocol (JSONRPC routing). Two endpoints per session on the existing FastAPI server. Session creation registers tools; session start passes `McpSSEServerConfig` to SDK instead of `McpSdkServerConfig`. In-process fallback preserved for `--no-server` CLI mode.

**Tech Stack:** FastAPI, Starlette SSE, existing `SdkMcpTool` handlers, `claude_agent_sdk` types.

---

### Task 1: McpSessionRegistry — session-scoped tool routing

**Files:**
- Create: `src/golem/mcp_sse.py`
- Create: `tests/test_mcp_sse.py`

- [ ] **Step 1: Write failing tests for McpSessionRegistry**

```python
# tests/test_mcp_sse.py
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
    from claude_agent_sdk import SdkMcpTool

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
    from claude_agent_sdk import SdkMcpTool

    registry = McpSessionRegistry()
    registry.register("sess-1", [])

    resp = await registry.handle_message("sess-1", {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "nope", "arguments": {}},
    })
    assert "error" in resp
    assert "not found" in resp["error"]["message"].lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_mcp_sse.py -v`
Expected: ImportError — `golem.mcp_sse` does not exist yet.

- [ ] **Step 3: Implement McpSessionRegistry**

```python
# src/golem/mcp_sse.py
"""MCP-over-SSE protocol for durable tool serving.

Provides McpSessionRegistry for session-scoped MCP tool routing and
SSE endpoint helpers for the FastAPI server.
"""
from __future__ import annotations

import json
from typing import Any

from claude_agent_sdk import SdkMcpTool


class McpSessionRegistry:
    """Registry of session-scoped MCP tool sets.

    Each session registers its SdkMcpTool list. JSONRPC messages are
    routed to the correct session's handlers via handle_message().
    """

    def __init__(self) -> None:
        self._sessions: dict[str, dict[str, SdkMcpTool[Any]]] = {}

    def register(self, session_id: str, tools: list[SdkMcpTool[Any]]) -> None:
        """Register MCP tools for a session."""
        self._sessions[session_id] = {t.name: t for t in tools}

    def unregister(self, session_id: str) -> None:
        """Remove a session's tools."""
        self._sessions.pop(session_id, None)

    def has_session(self, session_id: str) -> bool:
        """Check if a session is registered."""
        return session_id in self._sessions

    async def handle_message(
        self, session_id: str, message: dict[str, object],
    ) -> dict[str, object]:
        """Route a JSONRPC message to the session's tools. Returns JSONRPC response."""
        msg_id = message.get("id")
        method = str(message.get("method", ""))
        params = message.get("params", {})
        if not isinstance(params, dict):
            params = {}

        if not self.has_session(session_id):
            return _error(msg_id, -32001, f"Session '{session_id}' not registered")

        tool_map = self._sessions[session_id]

        if method == "initialize":
            return _success(msg_id, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "golem", "version": "1.0.0"},
            })

        if method == "notifications/initialized":
            return _success(msg_id, {})

        if method == "tools/list":
            tools_data = []
            for tool in tool_map.values():
                schema = tool.input_schema if isinstance(tool.input_schema, dict) else {}
                tools_data.append({
                    "name": tool.name,
                    "description": tool.description,
                    "inputSchema": schema,
                })
            return _success(msg_id, {"tools": tools_data})

        if method == "tools/call":
            tool_name = str(params.get("name", ""))
            arguments = params.get("arguments", {})
            if not isinstance(arguments, dict):
                arguments = {}
            if tool_name not in tool_map:
                return _error(msg_id, -32602, f"Tool '{tool_name}' not found")
            try:
                result = await tool_map[tool_name].handler(arguments)
                content = []
                for item in result.get("content", []):
                    if item.get("type") == "text":
                        content.append({"type": "text", "text": item["text"]})
                return _success(msg_id, {"content": content})
            except Exception as e:
                return _error(msg_id, -32603, str(e))

        return _error(msg_id, -32601, f"Method '{method}' not supported")


def _success(msg_id: object, result: object) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: object, code: int, message: str) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_mcp_sse.py -v`
Expected: All 6 pass.

- [ ] **Step 5: Commit**

```bash
git add src/golem/mcp_sse.py tests/test_mcp_sse.py
git commit -m "feat: McpSessionRegistry — session-scoped MCP tool routing"
```

---

### Task 2: SSE endpoints on the FastAPI server

**Files:**
- Modify: `src/golem/mcp_sse.py` (add SSE helpers)
- Modify: `src/golem/server.py` (add endpoints, wire registry)
- Test: `tests/test_mcp_sse.py` (add endpoint tests)

- [ ] **Step 1: Write failing tests for SSE endpoints**

Add to `tests/test_mcp_sse.py`:

```python
from unittest.mock import patch

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_mcp_sse.py -v -k "endpoint or message or cleaned"`
Expected: FAIL — endpoints don't exist yet.

- [ ] **Step 3: Wire McpSessionRegistry into server.py and add endpoints**

In `server.py`, add to imports:
```python
from golem.mcp_sse import McpSessionRegistry
```

In `create_app()`, after `session_mgr` creation, add:
```python
mcp_registry = McpSessionRegistry()
```

In the `create_session` endpoint, after creating session dir, register MCP tools:
```python
# Register MCP tools for this session
from golem.tools import _build_tools
tools = _build_tools(session_dir, GolemConfig(), project_root)
mcp_registry.register(session_id, tools)
```

In the `delete_session` endpoint, before removing session:
```python
mcp_registry.unregister(session_id)
```

Add two new endpoints:
```python
@app.get("/mcp/{session_id}/sse")
async def mcp_sse(session_id: str) -> StreamingResponse:
    """SSE stream for MCP-over-SSE protocol."""
    from fastapi import HTTPException
    if not mcp_registry.has_session(session_id):
        raise HTTPException(status_code=404, detail=f"MCP session not registered: {session_id}")

    async def event_stream() -> AsyncGenerator[str, None]:
        # Send endpoint event per MCP SSE spec
        yield f"event: endpoint\ndata: /mcp/{session_id}/message\n\n"
        # Keep connection alive
        while mcp_registry.has_session(session_id):
            yield ": keepalive\n\n"
            await asyncio.sleep(15)

    return StreamingResponse(event_stream(), media_type="text/event-stream")

@app.post("/mcp/{session_id}/message")
async def mcp_message(session_id: str, request: Request) -> dict[str, object]:
    """Handle JSONRPC message for MCP-over-SSE."""
    from fastapi import HTTPException, Request
    if not mcp_registry.has_session(session_id):
        raise HTTPException(status_code=404, detail=f"MCP session not registered: {session_id}")
    body = await request.json()
    return await mcp_registry.handle_message(session_id, body)
```

Note: Import `Request` from `fastapi` at the top of `create_app` or from the endpoint.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_mcp_sse.py -v`
Expected: All tests pass (registry + endpoint tests).

- [ ] **Step 5: Commit**

```bash
git add src/golem/mcp_sse.py src/golem/server.py tests/test_mcp_sse.py
git commit -m "feat: MCP SSE endpoints on FastAPI server"
```

---

### Task 3: Wire SDK sessions to use SSE MCP config

**Files:**
- Modify: `src/golem/tools.py` (add `create_golem_mcp_sse_config`)
- Modify: `src/golem/server.py` (`run_session` passes server_url, `start_session` uses it)
- Modify: `src/golem/planner.py` (accept `server_url`, use SSE config)
- Modify: `src/golem/tech_lead.py` (accept `server_url`, use SSE config)
- Modify: `src/golem/writer.py` (accept `server_url`, use SSE config)
- Test: `tests/test_mcp_sse.py` (add SSE config test)

- [ ] **Step 1: Write failing test for SSE config creation**

Add to `tests/test_mcp_sse.py`:

```python
from golem.tools import create_golem_mcp_sse_config


def test_create_golem_mcp_sse_config() -> None:
    """create_golem_mcp_sse_config returns McpSSEServerConfig."""
    config = create_golem_mcp_sse_config("sess-1", "http://127.0.0.1:7665")
    assert config["type"] == "sse"
    assert config["url"] == "http://127.0.0.1:7665/mcp/sess-1/sse"


def test_create_junior_dev_mcp_sse_config() -> None:
    """Junior dev SSE config uses golem-junior-dev name."""
    config = create_golem_mcp_sse_config("sess-1", "http://127.0.0.1:7665", name="golem-junior-dev")
    assert config["type"] == "sse"
    assert config["url"] == "http://127.0.0.1:7665/mcp/sess-1/sse"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_mcp_sse.py -v -k "sse_config"`
Expected: ImportError — `create_golem_mcp_sse_config` doesn't exist.

- [ ] **Step 3: Add create_golem_mcp_sse_config to tools.py**

Add after `create_golem_mcp_server()`:

```python
def create_golem_mcp_sse_config(
    session_id: str,
    server_url: str,
    name: str = "golem",
) -> dict[str, str]:
    """Return an SSE MCP server config pointing at the Golem server.

    Used when the Golem server is running — SDK connects to MCP tools
    over HTTP/SSE instead of in-process pipes.
    """
    return {"type": "sse", "url": f"{server_url}/mcp/{session_id}/sse"}
```

- [ ] **Step 4: Thread server_url through run_session and agent functions**

In `server.py` `run_session()`, add `server_url: str = ""` parameter:

```python
async def run_session(
    spec_path: Path,
    project_root: Path,
    config: GolemConfig,
    event_bus: EventBus,
    golem_dir: Path,
    server_url: str = "",
) -> None:
```

Pass `server_url` to `run_planner` and `run_tech_lead`:

```python
planner_result = await run_planner(spec_path, golem_dir, config, project_root, event_bus=event_bus, server_url=server_url)
# ...
await run_tech_lead(ticket_id, golem_dir, config, project_root, event_bus=event_bus, server_url=server_url)
```

In `start_session` endpoint, construct the URL and pass it:

```python
server_url = f"http://127.0.0.1:{os.environ.get('GOLEM_PORT', '7665')}"
task = asyncio.create_task(
    run_session(state.spec_path, project_root, config, event_bus, session_dir, server_url=server_url)
)
```

In `planner.py` `run_planner()` and `_run_planner_session()`, add `server_url: str = ""` parameter. When `server_url` is set, use SSE config:

```python
if server_url:
    from golem.tools import create_golem_mcp_sse_config
    mcp_server = create_golem_mcp_sse_config(config.session_id, server_url)
else:
    mcp_server = create_golem_mcp_server(golem_dir, config, cwd, event_bus=event_bus)
```

Same pattern in `tech_lead.py` `run_tech_lead()` and `writer.py` `spawn_junior_dev()`:

```python
if server_url:
    from golem.tools import create_golem_mcp_sse_config
    mcp_server = create_golem_mcp_sse_config(config.session_id, server_url, name="golem-junior-dev")
else:
    junior_dev_server = create_junior_dev_mcp_server(golem_dir, event_bus=event_bus)
```

- [ ] **Step 5: Run full test suite**

Run: `uv run pytest -x -k "not history_cli_no_golem and not clean_cli_no_golem"`
Expected: All pass — in-process fallback still works for tests (server_url defaults to "").

- [ ] **Step 6: Commit**

```bash
git add src/golem/tools.py src/golem/server.py src/golem/planner.py src/golem/tech_lead.py src/golem/writer.py tests/test_mcp_sse.py
git commit -m "feat: wire SDK sessions to MCP-over-SSE when server is running"
```

---

### Task 4: Register Junior Dev tools separately

**Files:**
- Modify: `src/golem/mcp_sse.py` (support multiple tool sets per session)
- Modify: `src/golem/server.py` (register both golem + junior-dev tool sets)
- Test: `tests/test_mcp_sse.py`

The Tech Lead session uses the full `golem` tool set (8 tools). The Junior Dev sessions use the limited `golem-junior-dev` set (3 tools). Both need to be registered on the MCP SSE server for the same session, but at different endpoint paths.

- [ ] **Step 1: Write failing test**

```python
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

    # Junior Dev tools (limited set) — uses session_id + "/jd" suffix
    resp = await client.post(f"/mcp/{session_id}-jd/message", content=json.dumps({
        "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
    }))
    jd_tools = [t["name"] for t in resp.json()["result"]["tools"]]
    assert "run_qa" in jd_tools
    assert "update_ticket" in jd_tools
    assert "read_ticket" in jd_tools
    assert "create_ticket" not in jd_tools
    assert "create_worktree" not in jd_tools
```

- [ ] **Step 2: Register junior dev tools in create_session**

In the `create_session` endpoint, after registering the full tool set, also register the junior dev tools:

```python
from golem.tools import _build_tools
from golem.tickets import TicketStore
from golem.tools import _handle_run_qa, _handle_update_ticket, _handle_read_ticket
from claude_agent_sdk import SdkMcpTool

# Full Tech Lead tools
tools = _build_tools(session_dir, GolemConfig(), project_root)
mcp_registry.register(session_id, tools)

# Junior Dev tools (limited)
jd_store = TicketStore(session_dir / "tickets")
jd_tools = [
    SdkMcpTool(name="run_qa", description="Run QA checks.", input_schema=_RUN_QA_INPUT_SCHEMA, handler=_handle_run_qa),
    SdkMcpTool(name="update_ticket", description="Update ticket.", input_schema=_UPDATE_TICKET_INPUT_SCHEMA, handler=partial(_handle_update_ticket, jd_store)),
    SdkMcpTool(name="read_ticket", description="Read ticket.", input_schema=_READ_TICKET_INPUT_SCHEMA, handler=partial(_handle_read_ticket, jd_store)),
]
mcp_registry.register(f"{session_id}-jd", jd_tools)
```

In `writer.py`, when `server_url` is set:

```python
mcp_server = create_golem_mcp_sse_config(f"{config.session_id}-jd", server_url, name="golem-junior-dev")
```

- [ ] **Step 3: Run tests**

Run: `uv run pytest tests/test_mcp_sse.py -v`
Expected: All pass.

- [ ] **Step 4: Commit**

```bash
git add src/golem/mcp_sse.py src/golem/server.py src/golem/writer.py tests/test_mcp_sse.py
git commit -m "feat: separate Junior Dev MCP tool registration on SSE"
```

---

### Task 5: Full integration test and cleanup

**Files:**
- Modify: `tests/test_mcp_sse.py` (full lifecycle integration test)
- Modify: `tests/test_mcp_durability.py` (add SSE variant of durability tests)

- [ ] **Step 1: Write full lifecycle integration test**

```python
@pytest.mark.asyncio
async def test_full_mcp_sse_lifecycle(client: AsyncClient, tmp_path: Path) -> None:
    """Full lifecycle: create session, call MCP tools over HTTP, delete session."""
    spec = tmp_path / "spec.md"
    spec.write_text("# Test\n", encoding="utf-8")

    # Create session (registers MCP tools)
    resp = await client.post("/api/sessions", json={
        "spec_path": str(spec),
        "project_root": str(tmp_path),
    })
    session_id = resp.json()["session_id"]

    # Initialize (MCP handshake)
    resp = await client.post(f"/mcp/{session_id}/message", content=json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {},
    }))
    assert "serverInfo" in resp.json()["result"]

    # Create ticket
    resp = await client.post(f"/mcp/{session_id}/message", content=json.dumps({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "create_ticket", "arguments": {
            "type": "task", "title": "Full lifecycle", "assigned_to": "writer",
        }},
    }))
    ticket_id = json.loads(resp.json()["result"]["content"][0]["text"])["ticket_id"]

    # Update ticket
    resp = await client.post(f"/mcp/{session_id}/message", content=json.dumps({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "update_ticket", "arguments": {
            "ticket_id": ticket_id, "status": "in_progress", "note": "Working",
        }},
    }))
    assert json.loads(resp.json()["result"]["content"][0]["text"])["ok"] is True

    # List tickets
    resp = await client.post(f"/mcp/{session_id}/message", content=json.dumps({
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "list_tickets", "arguments": {}},
    }))
    tickets = json.loads(resp.json()["result"]["content"][0]["text"])
    assert len(tickets) == 1
    assert tickets[0]["status"] == "in_progress"

    # Delete session — MCP cleaned up
    await client.delete(f"/api/sessions/{session_id}")

    resp = await client.post(f"/mcp/{session_id}/message", content=json.dumps({
        "jsonrpc": "2.0", "id": 5, "method": "tools/list", "params": {},
    }))
    assert resp.status_code == 404
```

- [ ] **Step 2: Run full test suite**

Run: `uv run pytest -x -k "not history_cli_no_golem and not clean_cli_no_golem"`
Expected: All pass.

- [ ] **Step 3: Commit**

```bash
git add tests/test_mcp_sse.py tests/test_mcp_durability.py
git commit -m "test: full MCP SSE lifecycle integration tests"
```

---
