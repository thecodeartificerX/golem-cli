# Spec: External MCP Server over SSE

## Problem

SDK sessions communicate with in-process MCP servers via the `Query` object's bidirectional control protocol. When the SDK's subprocess transport pipe dies during long sessions (especially after sub-agent dispatch), all MCP tool calls fail with "Stream closed" even though the MCP server instance and handlers are perfectly healthy.

Durability tests prove our handlers survive delays, concurrency, errors, and cross-server state sharing. The failure is in the SDK transport layer, not our code.

## Solution

Move MCP tool serving from in-process (`McpSdkServerConfig`) to an external SSE endpoint (`McpSSEServerConfig`) hosted on the existing FastAPI server (port 7665). The server process is long-lived and managed by Golem.ps1 — it outlives any individual SDK session.

## Architecture

```
Before:  SDK session (subprocess) --> in-process MCP --> handlers
         (transport pipe dies = MCP dies)

After:   SDK session (subprocess) --> HTTP/SSE --> FastAPI :7665 --> handlers
         (transport pipe dies = SDK reconnects to MCP over HTTP)
```

## Components

### 1. `src/golem/mcp_sse.py` (new file)

MCP-over-SSE protocol implementation. Handles JSONRPC messages for the MCP protocol.

**Responsibilities:**
- Maintain a registry of session-scoped MCP tool sets
- Handle `initialize`, `notifications/initialized`, `tools/list`, `tools/call` JSONRPC methods
- Route `tools/call` to the correct session's tool handlers
- Return JSONRPC responses

**Key class: `McpSessionRegistry`**
```python
class McpSessionRegistry:
    def register(self, session_id: str, tools: list[SdkMcpTool], event_bus: EventBus | None = None) -> None
    def unregister(self, session_id: str) -> None
    async def handle_message(self, session_id: str, message: dict) -> dict
    def get_tools(self, session_id: str) -> list[SdkMcpTool]
```

### 2. Server endpoints (in `server.py`)

Two new endpoints per session, following the MCP SSE transport spec:

- `GET /mcp/{session_id}/sse` — SSE stream. Sends an initial `endpoint` event with the message URL. Then relays JSONRPC responses as SSE `message` events.
- `POST /mcp/{session_id}/message` — receives JSONRPC requests from the SDK client. Routes through `McpSessionRegistry.handle_message()`. Response is sent back via the SSE stream (not the HTTP response body).

**Lifecycle integration:**
- `POST /api/sessions` (create) — registers MCP tools for the session via `McpSessionRegistry.register()`
- `POST /api/sessions/{id}/start` — SDK options use `McpSSEServerConfig` pointing at the SSE endpoint
- `DELETE /api/sessions/{id}` — calls `McpSessionRegistry.unregister()`

### 3. Config change in `tools.py`

New function `create_golem_mcp_sse_config()`:
```python
def create_golem_mcp_sse_config(session_id: str, server_url: str) -> McpSSEServerConfig:
    """Return an SSE MCP server config pointing at the Golem server."""
    return {"type": "sse", "url": f"{server_url}/mcp/{session_id}/sse"}
```

`create_golem_mcp_server()` remains for `--no-server` / CLI-direct mode (backward compat).

### 4. Planner / Tech Lead / Writer changes

`resolve_agent_options()` gets an optional `server_url` parameter. When set, it uses `create_golem_mcp_sse_config()` instead of `create_golem_mcp_server()`. The server passes this URL when launching sessions.

## MCP SSE Protocol

Per the MCP specification for SSE transport:

1. Client connects to `GET /mcp/{session_id}/sse`
2. Server sends SSE event: `event: endpoint\ndata: /mcp/{session_id}/message\n\n`
3. Client sends JSONRPC requests via `POST /mcp/{session_id}/message`
4. Server processes request, sends response via SSE: `event: message\ndata: {jsonrpc response}\n\n`

The SSE connection stays open for the session lifetime. Multiple requests can be sent on the same connection.

## What Does NOT Change

- All tool handlers in `tools.py` — unchanged
- `TicketStore`, QA, worktree operations — unchanged
- `EventBus` integration — unchanged
- `_build_tools()` — unchanged (still creates `SdkMcpTool` instances)
- CLI direct mode (`golem run --no-server`) — still uses in-process MCP
- Junior Dev MCP server — also migrated to SSE (same pattern)

## Fallback Strategy

If the SSE endpoint is unreachable (server not running), the SDK session will fail to connect to MCP tools. This is preferable to the current behavior where tools appear to exist but silently fail mid-session. The error is immediate and diagnosable rather than delayed and mysterious.

## Testing

1. **Unit tests for `McpSessionRegistry`** — register, handle messages, unregister, error handling
2. **Integration tests for SSE endpoints** — connect, send JSONRPC, receive responses
3. **Full lifecycle test** — create session, register MCP, call tools via HTTP, verify ticket state
4. **Concurrent sessions** — two sessions with separate tool registrations don't cross-contaminate
5. **Unregister cleanup** — tools return errors after session is deleted

## Constraints

- No new dependencies — FastAPI + Starlette SSE already available
- No new processes — runs on the existing server
- Backward compatible — `--no-server` mode still works with in-process MCP
- Session-scoped — each session's MCP tools are isolated by URL path
