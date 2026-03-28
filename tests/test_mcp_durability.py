"""MCP server durability tests — reproduce the "Stream closed" failure.

These tests exercise MCP tool handlers through the SDK's internal
request routing (_handle_sdk_mcp_request) to verify that:
1. Handlers survive repeated calls without state corruption
2. Handlers work correctly after simulated delays (long sub-agent runs)
3. The MCP server instance stays alive across many sequential calls
4. Concurrent handler calls don't corrupt shared TicketStore state
5. Error in one handler doesn't poison subsequent calls
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from golem.config import GolemConfig
from golem.tools import (
    create_golem_mcp_server,
    create_junior_dev_mcp_server,
    get_tech_lead_tools,
    handle_tool_call,
)


# ---------------------------------------------------------------------------
# Helper: call an MCP tool through the SDK's internal routing
# ---------------------------------------------------------------------------


async def _call_mcp_tool(server_instance: object, tool_name: str, args: dict[str, object]) -> dict[str, object]:
    """Simulate SDK's Query._handle_sdk_mcp_request for a tools/call request."""
    from mcp.types import CallToolRequest, CallToolRequestParams

    call_request = CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(name=tool_name, arguments=args),
    )
    handler = server_instance.request_handlers.get(CallToolRequest)  # type: ignore[union-attr]
    assert handler is not None, f"No call_tool handler registered on server"
    result = await handler(call_request)
    # Extract text from first content item
    content = result.root.content  # type: ignore[union-attr]
    assert len(content) > 0, "MCP response had no content"
    text = content[0].text
    return json.loads(text)


async def _list_mcp_tools(server_instance: object) -> list[str]:
    """Simulate SDK's Query._handle_sdk_mcp_request for a tools/list request."""
    from mcp.types import ListToolsRequest

    request = ListToolsRequest(method="tools/list")
    handler = server_instance.request_handlers.get(ListToolsRequest)  # type: ignore[union-attr]
    assert handler is not None
    result = await handler(request)
    return [tool.name for tool in result.root.tools]  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Test: MCP server instance stays alive through full lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_server_survives_full_lifecycle(tmp_path: Path) -> None:
    """Simulate a full Tech Lead session: create ticket, list, update, read — all via MCP."""
    golem_dir = tmp_path / ".golem"
    (golem_dir / "tickets").mkdir(parents=True)
    config = GolemConfig()

    server_config = create_golem_mcp_server(golem_dir, config, tmp_path)
    server = server_config["instance"]

    # Phase 1: list tools (the SDK does this on init)
    tools = await _list_mcp_tools(server)
    assert "create_ticket" in tools
    assert "update_ticket" in tools
    assert len(tools) == 12  # updated: 8 original + 4 new memory/progress tools

    # Phase 2: create a ticket
    result = await _call_mcp_tool(server, "create_ticket", {
        "type": "task",
        "title": "MCP durability test",
        "assigned_to": "writer",
        "priority": "high",
        "created_by": "tech_lead",
    })
    ticket_id = result["ticket_id"]
    assert ticket_id.startswith("TICKET-")

    # Phase 3: list tickets
    result = await _call_mcp_tool(server, "list_tickets", {})
    assert len(result) == 1
    assert result[0]["id"] == ticket_id

    # Phase 4: update ticket
    result = await _call_mcp_tool(server, "update_ticket", {
        "ticket_id": ticket_id,
        "status": "in_progress",
        "note": "Starting work",
        "agent": "tech_lead",
    })
    assert result["ok"] is True

    # Phase 5: read ticket back
    result = await _call_mcp_tool(server, "read_ticket", {"ticket_id": ticket_id})
    assert result["status"] == "in_progress"
    assert result["title"] == "MCP durability test"


# ---------------------------------------------------------------------------
# Test: MCP calls work after a simulated delay (sub-agent dispatch)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_survives_delay_between_calls(tmp_path: Path) -> None:
    """MCP handlers still work after asyncio.sleep simulating sub-agent execution."""
    golem_dir = tmp_path / ".golem"
    (golem_dir / "tickets").mkdir(parents=True)
    config = GolemConfig()

    server_config = create_golem_mcp_server(golem_dir, config, tmp_path)
    server = server_config["instance"]

    # Create ticket before "sub-agent" runs
    result = await _call_mcp_tool(server, "create_ticket", {
        "type": "task",
        "title": "Pre-delay ticket",
        "assigned_to": "writer",
    })
    ticket_id = result["ticket_id"]

    # Simulate sub-agent running for 2 seconds
    await asyncio.sleep(2)

    # MCP should still work after the delay
    result = await _call_mcp_tool(server, "read_ticket", {"ticket_id": ticket_id})
    assert result["title"] == "Pre-delay ticket"

    result = await _call_mcp_tool(server, "update_ticket", {
        "ticket_id": ticket_id,
        "status": "done",
        "note": "Post-delay update",
    })
    assert result["ok"] is True


# ---------------------------------------------------------------------------
# Test: many rapid sequential calls don't corrupt state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_rapid_sequential_calls(tmp_path: Path) -> None:
    """20 rapid create_ticket calls produce 20 unique tickets."""
    golem_dir = tmp_path / ".golem"
    (golem_dir / "tickets").mkdir(parents=True)
    config = GolemConfig()

    server_config = create_golem_mcp_server(golem_dir, config, tmp_path)
    server = server_config["instance"]

    ticket_ids: list[str] = []
    for i in range(20):
        result = await _call_mcp_tool(server, "create_ticket", {
            "type": "task",
            "title": f"Ticket {i}",
            "assigned_to": "writer",
        })
        ticket_ids.append(result["ticket_id"])

    # All IDs should be unique
    assert len(set(ticket_ids)) == 20

    # List should return all 20
    result = await _call_mcp_tool(server, "list_tickets", {})
    assert len(result) == 20


# ---------------------------------------------------------------------------
# Test: concurrent MCP calls don't corrupt TicketStore
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_concurrent_calls(tmp_path: Path) -> None:
    """Concurrent create_ticket calls from multiple coroutines produce unique results."""
    golem_dir = tmp_path / ".golem"
    (golem_dir / "tickets").mkdir(parents=True)
    config = GolemConfig()

    server_config = create_golem_mcp_server(golem_dir, config, tmp_path)
    server = server_config["instance"]

    async def create_one(i: int) -> str:
        result = await _call_mcp_tool(server, "create_ticket", {
            "type": "task",
            "title": f"Concurrent ticket {i}",
            "assigned_to": "writer",
        })
        return result["ticket_id"]

    # Fire 10 concurrent creates
    tasks = [create_one(i) for i in range(10)]
    ticket_ids = await asyncio.gather(*tasks)

    assert len(set(ticket_ids)) == 10

    result = await _call_mcp_tool(server, "list_tickets", {})
    assert len(result) == 10


# ---------------------------------------------------------------------------
# Test: error in one handler doesn't poison subsequent calls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_error_recovery(tmp_path: Path) -> None:
    """A failing handler call doesn't break subsequent calls."""
    golem_dir = tmp_path / ".golem"
    (golem_dir / "tickets").mkdir(parents=True)
    config = GolemConfig()

    server_config = create_golem_mcp_server(golem_dir, config, tmp_path)
    server = server_config["instance"]

    # Call read_ticket with nonexistent ID — should error
    with pytest.raises(Exception):
        await _call_mcp_tool(server, "read_ticket", {"ticket_id": "TICKET-999"})

    # Subsequent calls should still work fine
    result = await _call_mcp_tool(server, "create_ticket", {
        "type": "task",
        "title": "After error",
        "assigned_to": "writer",
    })
    assert result["ticket_id"].startswith("TICKET-")

    result = await _call_mcp_tool(server, "list_tickets", {})
    assert len(result) == 1


# ---------------------------------------------------------------------------
# Test: MCP server with event_bus survives full cycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_with_event_bus_full_cycle(tmp_path: Path) -> None:
    """MCP server with EventBus emits events and doesn't break handlers."""
    from golem.events import EventBus, GolemEvent, QueueBackend, TicketCreated, TicketUpdated

    golem_dir = tmp_path / ".golem"
    (golem_dir / "tickets").mkdir(parents=True)
    config = GolemConfig()

    queue: asyncio.Queue[GolemEvent] = asyncio.Queue()
    bus = EventBus(QueueBackend(queue), session_id="durability-test")

    server_config = create_golem_mcp_server(golem_dir, config, tmp_path, event_bus=bus)
    server = server_config["instance"]

    # Create
    result = await _call_mcp_tool(server, "create_ticket", {
        "type": "task",
        "title": "Event bus test",
        "assigned_to": "writer",
    })
    ticket_id = result["ticket_id"]

    # Update
    await _call_mcp_tool(server, "update_ticket", {
        "ticket_id": ticket_id,
        "status": "in_progress",
        "note": "Working",
    })

    # Collect events
    events: list[GolemEvent] = []
    while not queue.empty():
        events.append(queue.get_nowait())

    created_events = [e for e in events if isinstance(e, TicketCreated)]
    updated_events = [e for e in events if isinstance(e, TicketUpdated)]

    assert len(created_events) == 1
    assert created_events[0].ticket_id == ticket_id
    assert created_events[0].session_id == "durability-test"

    assert len(updated_events) == 1
    assert updated_events[0].new_status == "in_progress"


# ---------------------------------------------------------------------------
# Test: Junior Dev MCP server lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_junior_dev_mcp_lifecycle(tmp_path: Path) -> None:
    """Junior Dev MCP server (limited tools) survives create → update → read cycle."""
    golem_dir = tmp_path / ".golem"
    (golem_dir / "tickets").mkdir(parents=True)
    config = GolemConfig()

    # Pre-create a ticket using the full server
    full_server_config = create_golem_mcp_server(golem_dir, config, tmp_path)
    full_server = full_server_config["instance"]
    result = await _call_mcp_tool(full_server, "create_ticket", {
        "type": "task",
        "title": "Junior dev test",
        "assigned_to": "junior_dev",
    })
    ticket_id = result["ticket_id"]

    # Now use the junior dev server (limited tools)
    jd_server_config = create_junior_dev_mcp_server(golem_dir)
    jd_server = jd_server_config["instance"]

    jd_tools = await _list_mcp_tools(jd_server)
    # Writer now gets: run_qa, update_ticket, read_ticket, commit_worktree,
    # record_discovery, record_gotcha, get_session_context, get_build_progress
    assert "run_qa" in jd_tools
    assert "update_ticket" in jd_tools
    assert "read_ticket" in jd_tools
    assert "get_session_context" in jd_tools
    assert "record_discovery" in jd_tools
    assert "record_gotcha" in jd_tools
    assert "get_build_progress" in jd_tools
    assert "commit_worktree" in jd_tools
    # Writer must NOT have create_ticket / create_worktree / merge_branches / list_tickets
    assert "create_ticket" not in jd_tools
    assert "create_worktree" not in jd_tools
    assert "merge_branches" not in jd_tools

    # Read the ticket
    result = await _call_mcp_tool(jd_server, "read_ticket", {"ticket_id": ticket_id})
    assert result["title"] == "Junior dev test"

    # Update it
    result = await _call_mcp_tool(jd_server, "update_ticket", {
        "ticket_id": ticket_id,
        "status": "in_progress",
        "note": "Junior dev working",
    })
    assert result["ok"] is True

    # Read back through full server to confirm shared state
    result = await _call_mcp_tool(full_server, "read_ticket", {"ticket_id": ticket_id})
    assert result["status"] == "in_progress"


# ---------------------------------------------------------------------------
# Test: Interleaved calls across two MCP servers sharing a TicketStore
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_servers_share_ticket_store(tmp_path: Path) -> None:
    """Tech Lead and Junior Dev MCP servers sharing the same golem_dir see each other's changes."""
    golem_dir = tmp_path / ".golem"
    (golem_dir / "tickets").mkdir(parents=True)
    config = GolemConfig()

    tl_config = create_golem_mcp_server(golem_dir, config, tmp_path)
    tl = tl_config["instance"]

    jd_config = create_junior_dev_mcp_server(golem_dir)
    jd = jd_config["instance"]

    # Tech Lead creates ticket
    result = await _call_mcp_tool(tl, "create_ticket", {
        "type": "task",
        "title": "Shared state test",
        "assigned_to": "junior_dev",
    })
    ticket_id = result["ticket_id"]

    # Junior Dev reads it
    result = await _call_mcp_tool(jd, "read_ticket", {"ticket_id": ticket_id})
    assert result["title"] == "Shared state test"
    assert result["status"] == "pending"

    # Junior Dev updates it
    await _call_mcp_tool(jd, "update_ticket", {
        "ticket_id": ticket_id,
        "status": "done",
        "note": "Finished by JD",
    })

    # Tech Lead reads the update
    result = await _call_mcp_tool(tl, "read_ticket", {"ticket_id": ticket_id})
    assert result["status"] == "done"
    assert any("Finished by JD" in h.get("note", "") for h in result["history"])
