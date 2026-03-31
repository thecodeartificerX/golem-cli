from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from golem.config import GolemConfig
from golem.supervisor import ToolCallRegistry
from golem.tools import create_golem_mcp_server, create_junior_dev_mcp_server, create_qa_mcp_server, create_writer_mcp_server, get_tech_lead_tools, handle_tool_call

_EXPECTED_TOOL_NAMES = {
    "create_ticket",
    "update_ticket",
    "read_ticket",
    "list_tickets",
    "run_qa",
    "create_worktree",
    "merge_branches",
    "commit_worktree",
    "get_build_progress",
    "record_discovery",
    "record_gotcha",
    "get_session_context",
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


def test_create_junior_dev_mcp_server_has_tools() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        golem_dir = Path(tmpdir)
        (golem_dir / "tickets").mkdir()
        server = create_junior_dev_mcp_server(golem_dir)
        assert server is not None
        assert server["name"] == "golem-junior-dev"
        assert server["type"] == "sdk"
        assert hasattr(server["instance"], "call_tool")


def test_create_writer_mcp_server_backward_compat() -> None:
    """create_writer_mcp_server is a backward-compatible alias."""
    with tempfile.TemporaryDirectory() as tmpdir:
        golem_dir = Path(tmpdir)
        (golem_dir / "tickets").mkdir()
        server = create_writer_mcp_server(golem_dir)
        assert server is not None
        assert server["name"] == "golem-junior-dev"


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
async def test_handle_tool_call_update_ticket() -> None:
    """update_ticket changes status and persists."""
    with tempfile.TemporaryDirectory() as tmpdir:
        golem_dir = Path(tmpdir) / ".golem"
        config = GolemConfig()

        # Create a ticket first
        result_str = await handle_tool_call(
            "create_ticket",
            {"type": "task", "title": "Test task", "assigned_to": "writer"},
            golem_dir, config, Path(tmpdir),
        )
        ticket_id = json.loads(result_str)["ticket_id"]

        # Update its status
        update_str = await handle_tool_call(
            "update_ticket",
            {"ticket_id": ticket_id, "status": "in_progress", "note": "Starting work", "agent": "tech_lead"},
            golem_dir, config, Path(tmpdir),
        )
        assert json.loads(update_str)["ok"] is True

        # Verify status persisted
        read_str = await handle_tool_call(
            "read_ticket",
            {"ticket_id": ticket_id},
            golem_dir, config, Path(tmpdir),
        )
        ticket_data = json.loads(read_str)
        assert ticket_data["status"] == "in_progress"
        assert len(ticket_data["history"]) == 2  # created + updated


@pytest.mark.asyncio
async def test_handle_tool_call_read_ticket() -> None:
    """read_ticket returns all ticket fields."""
    with tempfile.TemporaryDirectory() as tmpdir:
        golem_dir = Path(tmpdir) / ".golem"
        config = GolemConfig()

        result_str = await handle_tool_call(
            "create_ticket",
            {
                "type": "task",
                "title": "Read me",
                "assigned_to": "writer",
                "priority": "high",
                "blueprint": "Build the thing",
                "acceptance": ["It works"],
                "qa_checks": ["exit 0"],
                "references": ["doc.md"],
            },
            golem_dir, config, Path(tmpdir),
        )
        ticket_id = json.loads(result_str)["ticket_id"]

        read_str = await handle_tool_call(
            "read_ticket",
            {"ticket_id": ticket_id},
            golem_dir, config, Path(tmpdir),
        )
        data = json.loads(read_str)
        assert data["id"] == ticket_id
        assert data["title"] == "Read me"
        assert data["type"] == "task"
        assert data["priority"] == "high"
        assert data["context"]["blueprint"] == "Build the thing"
        assert data["context"]["acceptance"] == ["It works"]
        assert data["context"]["qa_checks"] == ["exit 0"]
        assert data["context"]["references"] == ["doc.md"]
        assert len(data["history"]) >= 1


@pytest.mark.asyncio
async def test_handle_tool_call_list_tickets() -> None:
    """list_tickets with status filter returns correct count."""
    with tempfile.TemporaryDirectory() as tmpdir:
        golem_dir = Path(tmpdir) / ".golem"
        config = GolemConfig()

        # Create 3 tickets
        for title in ["A", "B", "C"]:
            await handle_tool_call(
                "create_ticket",
                {"type": "task", "title": title, "assigned_to": "writer"},
                golem_dir, config, Path(tmpdir),
            )

        # Update one to in_progress
        await handle_tool_call(
            "update_ticket",
            {"ticket_id": "TICKET-001", "status": "in_progress", "note": "starting"},
            golem_dir, config, Path(tmpdir),
        )

        # List all
        all_str = await handle_tool_call(
            "list_tickets", {}, golem_dir, config, Path(tmpdir),
        )
        all_tickets = json.loads(all_str)
        assert len(all_tickets) == 3

        # List by status
        pending_str = await handle_tool_call(
            "list_tickets",
            {"status_filter": "pending"},
            golem_dir, config, Path(tmpdir),
        )
        pending = json.loads(pending_str)
        assert len(pending) == 2

        ip_str = await handle_tool_call(
            "list_tickets",
            {"status_filter": "in_progress"},
            golem_dir, config, Path(tmpdir),
        )
        assert len(json.loads(ip_str)) == 1


@pytest.mark.asyncio
async def test_handle_tool_call_create_worktree(monkeypatch: pytest.MonkeyPatch) -> None:
    """create_worktree dispatches to worktree.create_worktree with correct args."""
    from unittest.mock import MagicMock

    captured: dict[str, object] = {}

    def fake_create_worktree(group_id: str, branch: str, base_branch: str, path: Path, repo_root: Path) -> None:
        captured["group_id"] = group_id
        captured["branch"] = branch
        captured["base_branch"] = base_branch
        captured["path"] = path
        captured["repo_root"] = repo_root

    monkeypatch.setattr("golem.tools.create_worktree", fake_create_worktree)

    with tempfile.TemporaryDirectory() as tmpdir:
        golem_dir = Path(tmpdir) / ".golem"
        (golem_dir / "tickets").mkdir(parents=True)
        config = GolemConfig()

        result_str = await handle_tool_call(
            "create_worktree",
            {
                "group_id": "g1",
                "branch": "golem/spec/g1",
                "base_branch": "main",
                "path": str(Path(tmpdir) / "wt"),
                "repo_root": tmpdir,
            },
            golem_dir, config, Path(tmpdir),
        )
        assert json.loads(result_str)["ok"] is True
        assert captured["group_id"] == "g1"
        assert captured["branch"] == "golem/spec/g1"
        assert captured["base_branch"] == "main"


@pytest.mark.asyncio
async def test_handle_tool_call_merge_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    """merge_branches dispatches to worktree.merge_group_branches with correct args."""
    def fake_merge(group_branches: list[str], target_branch: str, repo_root: Path) -> tuple[bool, str]:
        return True, ""

    monkeypatch.setattr("golem.tools.merge_group_branches", fake_merge)

    with tempfile.TemporaryDirectory() as tmpdir:
        golem_dir = Path(tmpdir) / ".golem"
        (golem_dir / "tickets").mkdir(parents=True)
        config = GolemConfig()

        result_str = await handle_tool_call(
            "merge_branches",
            {
                "group_branches": ["branch-a", "branch-b"],
                "target_branch": "integration",
                "repo_root": tmpdir,
            },
            golem_dir, config, Path(tmpdir),
        )
        result = json.loads(result_str)
        assert result["success"] is True
        assert result["conflict_info"] == ""


@pytest.mark.asyncio
async def test_handle_tool_call_commit_worktree(monkeypatch: pytest.MonkeyPatch) -> None:
    """commit_worktree dispatches to worktree.commit_task with correct args."""
    captured: dict[str, object] = {}

    def fake_commit(worktree_path: Path, task_id: str, description: str) -> bool:
        captured["worktree_path"] = worktree_path
        captured["task_id"] = task_id
        captured["description"] = description
        return True

    monkeypatch.setattr("golem.tools.commit_task", fake_commit)

    with tempfile.TemporaryDirectory() as tmpdir:
        golem_dir = Path(tmpdir) / ".golem"
        (golem_dir / "tickets").mkdir(parents=True)
        config = GolemConfig()

        result_str = await handle_tool_call(
            "commit_worktree",
            {
                "worktree_path": tmpdir,
                "task_id": "TICKET-001",
                "description": "Implement auth",
            },
            golem_dir, config, Path(tmpdir),
        )
        assert json.loads(result_str)["committed"] is True
        assert captured["task_id"] == "TICKET-001"
        assert captured["description"] == "Implement auth"


@pytest.mark.asyncio
async def test_handle_tool_call_create_ticket_files_dict() -> None:
    """create_ticket preserves files dict with str(k): str(v) conversion."""
    with tempfile.TemporaryDirectory() as tmpdir:
        golem_dir = Path(tmpdir) / ".golem"
        config = GolemConfig()

        result_str = await handle_tool_call(
            "create_ticket",
            {
                "type": "task",
                "title": "With files",
                "assigned_to": "writer",
                "files": {"src/main.py": "# main module", "src/utils.py": "# utils"},
            },
            golem_dir, config, Path(tmpdir),
        )
        ticket_id = json.loads(result_str)["ticket_id"]

        read_str = await handle_tool_call(
            "read_ticket",
            {"ticket_id": ticket_id},
            golem_dir, config, Path(tmpdir),
        )
        data = json.loads(read_str)
        assert data["context"]["files"] == {"src/main.py": "# main module", "src/utils.py": "# utils"}


@pytest.mark.asyncio
async def test_tool_call_records_to_registry() -> None:
    """When registry is provided to get_tech_lead_tools, tool calls record to it."""
    with tempfile.TemporaryDirectory() as tmpdir:
        golem_dir = Path(tmpdir) / ".golem"
        (golem_dir / "tickets").mkdir(parents=True)
        config = GolemConfig()
        registry = ToolCallRegistry()

        tools = get_tech_lead_tools(Path(tmpdir), config, Path(tmpdir), registry=registry)
        create_ticket_tool = next(t for t in tools if t.name == "create_ticket")

        # Call the instrumented handler directly
        await create_ticket_tool.handler({
            "type": "task",
            "title": "Registry test ticket",
            "assigned_to": "writer",
        })

        assert registry.has_called("create_ticket")
        assert registry.action_call_count() == 1


@pytest.mark.asyncio
async def test_create_ticket_emits_event(tmp_path: Path) -> None:
    """create_golem_mcp_server accepts event_bus parameter."""
    import asyncio

    from golem.events import EventBus, QueueBackend

    golem_dir = tmp_path / ".golem"
    (golem_dir / "tickets").mkdir(parents=True)

    queue: asyncio.Queue = asyncio.Queue()
    bus = EventBus(QueueBackend(queue), session_id="test")
    config = GolemConfig()

    server = create_golem_mcp_server(golem_dir, config, tmp_path, event_bus=bus)
    assert server is not None


@pytest.mark.asyncio
async def test_no_events_without_bus(tmp_path: Path) -> None:
    """MCP server works without event_bus (backward compat)."""
    golem_dir = tmp_path / ".golem"
    (golem_dir / "tickets").mkdir(parents=True)
    config = GolemConfig()

    server = create_golem_mcp_server(golem_dir, config, tmp_path)
    assert server is not None


@pytest.mark.asyncio
async def test_create_ticket_handler_emits_ticket_created_event(tmp_path: Path) -> None:
    """create_ticket tool handler emits TicketCreated event when event_bus is set."""
    import asyncio

    from golem.events import EventBus, QueueBackend, TicketCreated

    golem_dir = tmp_path / ".golem"
    (golem_dir / "tickets").mkdir(parents=True)
    config = GolemConfig()

    queue: asyncio.Queue = asyncio.Queue()
    bus = EventBus(QueueBackend(queue), session_id="test")

    tools = get_tech_lead_tools(golem_dir, config, tmp_path, event_bus=bus)
    create_ticket_tool = next(t for t in tools if t.name == "create_ticket")

    await create_ticket_tool.handler({
        "type": "task",
        "title": "Event emission test",
        "assigned_to": "writer",
    })

    assert not queue.empty()
    event = queue.get_nowait()
    assert isinstance(event, TicketCreated)
    assert event.title == "Event emission test"
    assert event.assignee == "writer"
    assert event.ticket_id.startswith("TICKET-")
    assert event.session_id == "test"


@pytest.mark.asyncio
async def test_update_ticket_handler_emits_ticket_updated_event(tmp_path: Path) -> None:
    """update_ticket tool handler emits TicketUpdated event when event_bus is set."""
    import asyncio

    from golem.events import EventBus, QueueBackend, TicketUpdated

    golem_dir = tmp_path / ".golem"
    (golem_dir / "tickets").mkdir(parents=True)
    config = GolemConfig()

    # Create a ticket first (no bus)
    result_str = await handle_tool_call(
        "create_ticket",
        {"type": "task", "title": "Update event test", "assigned_to": "writer"},
        golem_dir, config, tmp_path,
    )
    ticket_id = json.loads(result_str)["ticket_id"]

    queue: asyncio.Queue = asyncio.Queue()
    bus = EventBus(QueueBackend(queue), session_id="test")

    tools = get_tech_lead_tools(golem_dir, config, tmp_path, event_bus=bus)
    update_ticket_tool = next(t for t in tools if t.name == "update_ticket")

    await update_ticket_tool.handler({
        "ticket_id": ticket_id,
        "status": "in_progress",
        "note": "Starting work",
    })

    assert not queue.empty()
    event = queue.get_nowait()
    assert isinstance(event, TicketUpdated)
    assert event.ticket_id == ticket_id
    assert event.old_status == "pending"
    assert event.new_status == "in_progress"


@pytest.mark.asyncio
async def test_junior_dev_mcp_server_accepts_event_bus(tmp_path: Path) -> None:
    """create_junior_dev_mcp_server accepts event_bus parameter."""
    import asyncio

    from golem.events import EventBus, QueueBackend

    golem_dir = tmp_path / ".golem"
    (golem_dir / "tickets").mkdir(parents=True)

    queue: asyncio.Queue = asyncio.Queue()
    bus = EventBus(QueueBackend(queue), session_id="test")

    server = create_junior_dev_mcp_server(golem_dir, event_bus=bus)
    assert server is not None
    assert server["name"] == "golem-junior-dev"
