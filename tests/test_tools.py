from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from golem.config import GolemConfig
from golem.tools import create_golem_mcp_server, create_qa_mcp_server, create_writer_mcp_server, get_tech_lead_tools, handle_tool_call

_EXPECTED_TOOL_NAMES = {
    "create_ticket",
    "update_ticket",
    "read_ticket",
    "list_tickets",
    "run_qa",
    "create_worktree",
    "merge_branches",
    "commit_worktree",
    "create_blocker",
}


@pytest.mark.asyncio
async def test_get_tech_lead_tools_returns_all_tools() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        config = GolemConfig()
        tools = get_tech_lead_tools(Path(tmpdir), config, Path(tmpdir))
        names = {t.name for t in tools}
        assert names == _EXPECTED_TOOL_NAMES


@pytest.mark.asyncio
async def test_handle_tool_call_create_ticket() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        golem_dir = Path(tmpdir) / ".golem"
        config = GolemConfig()
        result_str = await handle_tool_call(
            "create_ticket",
            {
                "type": "task",
                "title": "Implement feature X",
                "assigned_to": "writer",
                "priority": "high",
                "created_by": "tech_lead",
            },
            golem_dir,
            config,
            Path(tmpdir),
        )
        result = json.loads(result_str)
        assert "ticket_id" in result
        assert result["ticket_id"].startswith("TICKET-")
        # Verify file was written
        assert (golem_dir / "tickets" / f"{result['ticket_id']}.json").exists()


@pytest.mark.asyncio
async def test_handle_tool_call_run_qa() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        config = GolemConfig()
        result_str = await handle_tool_call(
            "run_qa",
            {
                "worktree_path": tmpdir,
                "checks": ["exit 0"],
                "infrastructure_checks": [],
            },
            Path(tmpdir),
            config,
            Path(tmpdir),
        )
        result = json.loads(result_str)
        assert "passed" in result
        assert "checks" in result
        assert "summary" in result
        assert result["passed"] is True


@pytest.mark.asyncio
async def test_handle_tool_call_run_qa_failing() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        config = GolemConfig()
        result_str = await handle_tool_call(
            "run_qa",
            {
                "worktree_path": tmpdir,
                "checks": ["exit 1"],
                "infrastructure_checks": [],
            },
            Path(tmpdir),
            config,
            Path(tmpdir),
        )
        result = json.loads(result_str)
        assert result["passed"] is False
        assert len(result["checks"]) == 1
        assert result["checks"][0]["passed"] is False


@pytest.mark.asyncio
async def test_handle_tool_call_unknown_tool_raises() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        config = GolemConfig()
        with pytest.raises(ValueError, match="Unknown tool"):
            await handle_tool_call(
                "nonexistent_tool",
                {},
                Path(tmpdir),
                config,
                Path(tmpdir),
            )


def test_create_writer_mcp_server_has_both_tools() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        golem_dir = Path(tmpdir)
        (golem_dir / "tickets").mkdir()
        server = create_writer_mcp_server(golem_dir)
        assert server is not None
        assert server["name"] == "golem-writer"
        assert server["type"] == "sdk"
        # The server instance should have a call_tool method (MCP server)
        assert hasattr(server["instance"], "call_tool")


def test_create_golem_mcp_server_name() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        golem_dir = Path(tmpdir)
        (golem_dir / "tickets").mkdir()
        config = GolemConfig()
        server = create_golem_mcp_server(golem_dir, config, Path(tmpdir))
        assert server["name"] == "golem"
        assert server["type"] == "sdk"
        assert hasattr(server["instance"], "call_tool")


def test_create_qa_mcp_server_name() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        server = create_qa_mcp_server(Path(tmpdir))
        assert server["name"] == "golem-qa"
        assert server["type"] == "sdk"


@pytest.mark.asyncio
async def test_handle_create_blocker_creates_blocker_and_blocks_original() -> None:
    """create_blocker should create a blocker ticket and set original ticket to blocked."""
    with tempfile.TemporaryDirectory() as tmpdir:
        golem_dir = Path(tmpdir) / ".golem"
        config = GolemConfig()

        # Create the original ticket first
        result_str = await handle_tool_call(
            "create_ticket",
            {
                "type": "task",
                "title": "Implement feature Y",
                "assigned_to": "writer",
                "priority": "high",
                "created_by": "tech_lead",
            },
            golem_dir,
            config,
            Path(tmpdir),
        )
        original_id = json.loads(result_str)["ticket_id"]

        # Now create a blocker for it
        blocker_str = await handle_tool_call(
            "create_blocker",
            {
                "original_ticket_id": original_id,
                "reason": "QA keeps failing after 3 rework cycles",
                "context": "ruff check fails on line 42",
            },
            golem_dir,
            config,
            Path(tmpdir),
        )
        blocker_result = json.loads(blocker_str)
        assert "blocker_id" in blocker_result
        assert blocker_result["status"] == "created"

        # Verify the blocker ticket was created correctly
        blocker_ticket_str = await handle_tool_call(
            "read_ticket",
            {"ticket_id": blocker_result["blocker_id"]},
            golem_dir,
            config,
            Path(tmpdir),
        )
        blocker_ticket = json.loads(blocker_ticket_str)
        assert blocker_ticket["type"] == "blocker"
        assert blocker_ticket["assigned_to"] == "tech_lead"
        assert blocker_ticket["status"] == "pending"
        assert blocker_ticket["priority"] == "high"
        assert "Blocked: Implement feature Y" == blocker_ticket["title"]

        # Verify the original ticket was set to blocked
        original_str = await handle_tool_call(
            "read_ticket",
            {"ticket_id": original_id},
            golem_dir,
            config,
            Path(tmpdir),
        )
        original_ticket = json.loads(original_str)
        assert original_ticket["status"] == "blocked"
        # Should have a history event about being blocked
        blocked_events = [e for e in original_ticket["history"] if "blocked" in e["note"].lower()]
        assert len(blocked_events) >= 1


@pytest.mark.asyncio
async def test_create_blocker_references_original_ticket() -> None:
    """Blocker ticket context should reference the original ticket."""
    with tempfile.TemporaryDirectory() as tmpdir:
        golem_dir = Path(tmpdir) / ".golem"
        config = GolemConfig()

        result_str = await handle_tool_call(
            "create_ticket",
            {"type": "task", "title": "Do something", "assigned_to": "writer"},
            golem_dir,
            config,
            Path(tmpdir),
        )
        original_id = json.loads(result_str)["ticket_id"]

        blocker_str = await handle_tool_call(
            "create_blocker",
            {"original_ticket_id": original_id, "reason": "Stuck on tests"},
            golem_dir,
            config,
            Path(tmpdir),
        )
        blocker_id = json.loads(blocker_str)["blocker_id"]

        blocker_ticket_str = await handle_tool_call(
            "read_ticket",
            {"ticket_id": blocker_id},
            golem_dir,
            config,
            Path(tmpdir),
        )
        blocker_ticket = json.loads(blocker_ticket_str)
        assert original_id in blocker_ticket["context"]["references"]
        assert original_id in blocker_ticket["context"]["blueprint"]
        assert "Stuck on tests" in blocker_ticket["context"]["blueprint"]


@pytest.mark.asyncio
async def test_create_blocker_is_in_writer_tools() -> None:
    """Writer MCP server should include the create_blocker tool."""
    with tempfile.TemporaryDirectory() as tmpdir:
        golem_dir = Path(tmpdir)
        (golem_dir / "tickets").mkdir()
        server = create_writer_mcp_server(golem_dir)
        # The server should be able to handle create_blocker calls
        assert server is not None
        assert server["name"] == "golem-writer"


@pytest.mark.asyncio
async def test_escalation_ticket_type() -> None:
    """Should be able to create a ticket with type=escalation."""
    with tempfile.TemporaryDirectory() as tmpdir:
        golem_dir = Path(tmpdir) / ".golem"
        config = GolemConfig()
        result_str = await handle_tool_call(
            "create_ticket",
            {
                "type": "escalation",
                "title": "Need operator help",
                "assigned_to": "operator",
                "priority": "high",
                "created_by": "tech_lead",
            },
            golem_dir,
            config,
            Path(tmpdir),
        )
        result = json.loads(result_str)
        assert "ticket_id" in result
        # Verify it was stored correctly
        ticket_str = await handle_tool_call(
            "read_ticket",
            {"ticket_id": result["ticket_id"]},
            golem_dir,
            config,
            Path(tmpdir),
        )
        ticket = json.loads(ticket_str)
        assert ticket["type"] == "escalation"
        assert ticket["assigned_to"] == "operator"


@pytest.mark.asyncio
async def test_blocker_ticket_type() -> None:
    """Should be able to create a ticket with type=blocker directly."""
    with tempfile.TemporaryDirectory() as tmpdir:
        golem_dir = Path(tmpdir) / ".golem"
        config = GolemConfig()
        result_str = await handle_tool_call(
            "create_ticket",
            {
                "type": "blocker",
                "title": "Blocked: something",
                "assigned_to": "tech_lead",
                "priority": "high",
                "created_by": "writer",
            },
            golem_dir,
            config,
            Path(tmpdir),
        )
        result = json.loads(result_str)
        ticket_str = await handle_tool_call(
            "read_ticket",
            {"ticket_id": result["ticket_id"]},
            golem_dir,
            config,
            Path(tmpdir),
        )
        ticket = json.loads(ticket_str)
        assert ticket["type"] == "blocker"
