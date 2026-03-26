from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from golem.config import GolemConfig
from golem.tools import create_writer_mcp_server, get_tech_lead_tools, handle_tool_call

_EXPECTED_TOOL_NAMES = {
    "create_ticket",
    "update_ticket",
    "read_ticket",
    "list_tickets",
    "run_qa",
    "create_worktree",
    "merge_branches",
    "commit_worktree",
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
