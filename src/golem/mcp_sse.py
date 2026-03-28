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
