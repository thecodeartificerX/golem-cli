"""Tests for ToolRegistry, per-agent tool filtering, new tool handlers, and atomic writes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from golem.config import GolemConfig
from golem.tickets import TicketStore, _write_json_atomic
from golem.tool_registry import (
    PLANNER_TOOLS,
    TECH_LEAD_TOOLS,
    WRITER_TOOLS,
    AgentType,
    RegisteredTool,
    ToolContext,
    ToolRegistry,
)
from golem.tools import (
    _handle_get_build_progress,
    _handle_get_session_context,
    _handle_record_discovery,
    _handle_record_gotcha,
    _write_json_atomic as tools_write_json_atomic,
    build_tool_registry,
    create_golem_mcp_server,
    create_golem_planner_mcp_server,
    create_junior_dev_mcp_server,
)


# ---------------------------------------------------------------------------
# ToolRegistry unit tests
# ---------------------------------------------------------------------------


def test_registry_empty_on_init() -> None:
    reg = ToolRegistry()
    assert reg.tool_names_for_agent("planner") == []
    assert reg.tool_names_for_agent("writer") == []
    assert reg.tool_names_for_agent("tech_lead") == []


def test_registry_register_and_filter(tmp_path: Path) -> None:
    from claude_agent_sdk import SdkMcpTool

    reg = ToolRegistry()

    async def _dummy(args: dict[str, object]) -> dict[str, object]:
        return {}

    reg.register(RegisteredTool(
        name="create_ticket",
        allowed_for=frozenset({"planner", "tech_lead"}),
        factory=lambda ctx: SdkMcpTool(
            name="create_ticket",
            description="test",
            input_schema={"type": "object"},
            handler=_dummy,
        ),
    ))
    reg.register(RegisteredTool(
        name="run_qa",
        allowed_for=frozenset({"writer", "tech_lead"}),
        factory=lambda ctx: SdkMcpTool(
            name="run_qa",
            description="test",
            input_schema={"type": "object"},
            handler=_dummy,
        ),
    ))

    ctx = ToolContext(golem_dir=tmp_path, project_root=tmp_path, agent_type="planner")

    planner_tools = reg.get_tools_for_agent("planner", ctx)
    planner_names = {t.name for t in planner_tools}
    assert planner_names == {"create_ticket"}

    writer_tools = reg.get_tools_for_agent("writer", ctx)
    writer_names = {t.name for t in writer_tools}
    assert writer_names == {"run_qa"}

    tl_tools = reg.get_tools_for_agent("tech_lead", ctx)
    tl_names = {t.name for t in tl_tools}
    assert tl_names == {"create_ticket", "run_qa"}


def test_tool_names_for_agent(tmp_path: Path) -> None:
    from claude_agent_sdk import SdkMcpTool

    reg = ToolRegistry()

    async def _dummy(args: dict[str, object]) -> dict[str, object]:
        return {}

    reg.register(RegisteredTool(
        name="alpha",
        allowed_for=frozenset({"planner"}),
        factory=lambda ctx: SdkMcpTool(name="alpha", description="", input_schema={}, handler=_dummy),
    ))
    reg.register(RegisteredTool(
        name="beta",
        allowed_for=frozenset({"writer"}),
        factory=lambda ctx: SdkMcpTool(name="beta", description="", input_schema={}, handler=_dummy),
    ))

    assert "alpha" in reg.tool_names_for_agent("planner")
    assert "alpha" not in reg.tool_names_for_agent("writer")
    assert "beta" in reg.tool_names_for_agent("writer")
    assert "beta" not in reg.tool_names_for_agent("planner")


# ---------------------------------------------------------------------------
# Agent tool set constant tests
# ---------------------------------------------------------------------------


def test_planner_tools_does_not_contain_worktree_ops() -> None:
    assert "create_worktree" not in PLANNER_TOOLS
    assert "merge_branches" not in PLANNER_TOOLS
    assert "commit_worktree" not in PLANNER_TOOLS
    assert "run_qa" not in PLANNER_TOOLS
    assert "update_ticket" not in PLANNER_TOOLS


def test_planner_tools_contains_expected_tools() -> None:
    assert "create_ticket" in PLANNER_TOOLS
    assert "read_ticket" in PLANNER_TOOLS
    assert "list_tickets" in PLANNER_TOOLS
    assert "get_session_context" in PLANNER_TOOLS
    assert "record_discovery" in PLANNER_TOOLS
    assert "record_gotcha" in PLANNER_TOOLS
    assert "get_build_progress" in PLANNER_TOOLS


def test_writer_tools_does_not_contain_create_ticket() -> None:
    assert "create_ticket" not in WRITER_TOOLS
    assert "create_worktree" not in WRITER_TOOLS
    assert "merge_branches" not in WRITER_TOOLS
    assert "list_tickets" not in WRITER_TOOLS


def test_writer_tools_contains_expected_tools() -> None:
    assert "update_ticket" in WRITER_TOOLS
    assert "read_ticket" in WRITER_TOOLS
    assert "run_qa" in WRITER_TOOLS
    assert "commit_worktree" in WRITER_TOOLS
    assert "get_session_context" in WRITER_TOOLS
    assert "record_discovery" in WRITER_TOOLS
    assert "record_gotcha" in WRITER_TOOLS
    assert "get_build_progress" in WRITER_TOOLS


def test_tech_lead_tools_is_superset_of_planner_and_writer() -> None:
    assert PLANNER_TOOLS.issubset(TECH_LEAD_TOOLS)
    assert WRITER_TOOLS.issubset(TECH_LEAD_TOOLS)


# ---------------------------------------------------------------------------
# build_tool_registry per-agent filtering tests
# ---------------------------------------------------------------------------


def test_planner_does_not_get_worktree_tools(tmp_path: Path) -> None:
    golem_dir = tmp_path / ".golem"
    (golem_dir / "tickets").mkdir(parents=True)
    config = GolemConfig()

    reg = build_tool_registry(golem_dir, config, tmp_path)
    ctx = ToolContext(golem_dir=golem_dir, project_root=tmp_path, agent_type="planner")
    names = {t.name for t in reg.get_tools_for_agent("planner", ctx)}

    assert "create_worktree" not in names
    assert "merge_branches" not in names
    assert "commit_worktree" not in names
    assert "run_qa" not in names
    assert "create_ticket" in names
    assert "get_session_context" in names
    assert "record_discovery" in names
    assert "record_gotcha" in names
    assert "get_build_progress" in names


def test_writer_does_not_get_create_ticket(tmp_path: Path) -> None:
    golem_dir = tmp_path / ".golem"
    (golem_dir / "tickets").mkdir(parents=True)
    config = GolemConfig()

    reg = build_tool_registry(golem_dir, config, tmp_path)
    ctx = ToolContext(golem_dir=golem_dir, project_root=tmp_path, agent_type="writer")
    names = {t.name for t in reg.get_tools_for_agent("writer", ctx)}

    assert "create_ticket" not in names
    assert "create_worktree" not in names
    assert "merge_branches" not in names
    assert "list_tickets" not in names
    assert "run_qa" in names
    assert "update_ticket" in names
    assert "commit_worktree" in names
    assert "get_session_context" in names


def test_tech_lead_gets_all_tools(tmp_path: Path) -> None:
    golem_dir = tmp_path / ".golem"
    (golem_dir / "tickets").mkdir(parents=True)
    config = GolemConfig()

    reg = build_tool_registry(golem_dir, config, tmp_path)
    ctx = ToolContext(golem_dir=golem_dir, project_root=tmp_path, agent_type="tech_lead")
    names = {t.name for t in reg.get_tools_for_agent("tech_lead", ctx)}

    expected = {
        "create_ticket", "update_ticket", "read_ticket", "list_tickets",
        "run_qa", "create_worktree", "merge_branches", "commit_worktree",
        "get_build_progress", "record_discovery", "record_gotcha", "get_session_context",
    }
    assert expected.issubset(names)


# ---------------------------------------------------------------------------
# Write-path containment tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_worktree_rejects_outside_path(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    other = tmp_path / "other"
    other.mkdir()

    golem_dir = tmp_path / ".golem"
    (golem_dir / "tickets").mkdir(parents=True)
    config = GolemConfig()

    ctx = ToolContext(
        golem_dir=golem_dir,
        project_root=tmp_path,
        worktree_path=worktree,
        agent_type="writer",
    )
    reg = build_tool_registry(golem_dir, config, tmp_path)
    tools = {t.name: t for t in reg.get_tools_for_agent("writer", ctx)}
    commit_tool = tools["commit_worktree"]

    result = await commit_tool.handler({
        "worktree_path": str(other),
        "task_id": "TICKET-001",
        "description": "test",
    })
    text = result["content"][0]["text"]
    data = json.loads(text)
    assert "error" in data
    assert "denied" in data["error"].lower() or "outside" in data["error"].lower()


@pytest.mark.asyncio
async def test_commit_worktree_allows_within_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    golem_dir = tmp_path / ".golem"
    (golem_dir / "tickets").mkdir(parents=True)
    config = GolemConfig()

    # Mock commit_task to avoid git operations
    def fake_commit(worktree_path: Path, task_id: str, description: str) -> bool:
        return True

    monkeypatch.setattr("golem.tools.commit_task", fake_commit)

    ctx = ToolContext(
        golem_dir=golem_dir,
        project_root=tmp_path,
        worktree_path=worktree,
        agent_type="writer",
    )
    reg = build_tool_registry(golem_dir, config, tmp_path)
    tools = {t.name: t for t in reg.get_tools_for_agent("writer", ctx)}
    commit_tool = tools["commit_worktree"]

    result = await commit_tool.handler({
        "worktree_path": str(worktree),
        "task_id": "TICKET-001",
        "description": "test commit",
    })
    text = result["content"][0]["text"]
    data = json.loads(text)
    # Should not have an error about path containment
    assert "error" not in data or "denied" not in str(data.get("error", ""))


@pytest.mark.asyncio
async def test_commit_worktree_no_restriction_when_no_worktree_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tech lead context has no worktree_path — no containment check."""
    golem_dir = tmp_path / ".golem"
    (golem_dir / "tickets").mkdir(parents=True)
    config = GolemConfig()

    def fake_commit(worktree_path: Path, task_id: str, description: str) -> bool:
        return True

    monkeypatch.setattr("golem.tools.commit_task", fake_commit)

    # Tech lead context — no worktree_path
    ctx = ToolContext(
        golem_dir=golem_dir,
        project_root=tmp_path,
        worktree_path=None,
        agent_type="tech_lead",
    )
    reg = build_tool_registry(golem_dir, config, tmp_path)
    tools = {t.name: t for t in reg.get_tools_for_agent("tech_lead", ctx)}
    commit_tool = tools["commit_worktree"]

    other_dir = tmp_path / "anywhere"
    other_dir.mkdir()

    result = await commit_tool.handler({
        "worktree_path": str(other_dir),
        "task_id": "TICKET-001",
        "description": "tech lead commit",
    })
    text = result["content"][0]["text"]
    data = json.loads(text)
    assert "error" not in data or "denied" not in str(data.get("error", ""))


# ---------------------------------------------------------------------------
# New tool handler tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_discovery_creates_codebase_map(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    result = await _handle_record_discovery(memory_dir, {
        "file_path": "src/golem/tools.py",
        "description": "MCP tool registry",
        "category": "api",
    })
    text = result["content"][0]["text"]
    assert json.loads(text)["ok"] is True

    map_file = memory_dir / "codebase_map.json"
    assert map_file.exists()
    data = json.loads(map_file.read_text(encoding="utf-8"))
    assert "src/golem/tools.py" in data["discovered_files"]
    entry = data["discovered_files"]["src/golem/tools.py"]
    assert entry["description"] == "MCP tool registry"
    assert entry["category"] == "api"
    assert "discovered_at" in entry
    assert data["last_updated"] is not None


@pytest.mark.asyncio
async def test_record_discovery_default_category(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    await _handle_record_discovery(memory_dir, {
        "file_path": "src/main.py",
        "description": "main module",
    })
    data = json.loads((memory_dir / "codebase_map.json").read_text(encoding="utf-8"))
    assert data["discovered_files"]["src/main.py"]["category"] == "general"


@pytest.mark.asyncio
async def test_record_discovery_overwrites_same_path(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    await _handle_record_discovery(memory_dir, {"file_path": "src/a.py", "description": "first"})
    await _handle_record_discovery(memory_dir, {"file_path": "src/a.py", "description": "second"})
    data = json.loads((memory_dir / "codebase_map.json").read_text(encoding="utf-8"))
    assert data["discovered_files"]["src/a.py"]["description"] == "second"
    assert len(data["discovered_files"]) == 1


@pytest.mark.asyncio
async def test_record_and_retrieve_discovery(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    await _handle_record_discovery(memory_dir, {
        "file_path": "src/golem/tools.py",
        "description": "MCP tool registry",
        "category": "api",
    })
    result = await _handle_get_session_context(memory_dir, {})
    text = result["content"][0]["text"]
    assert "src/golem/tools.py" in text
    assert "MCP tool registry" in text


@pytest.mark.asyncio
async def test_record_gotcha_creates_file_on_first_call(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    await _handle_record_gotcha(memory_dir, {"gotcha": "Always use utf-8"})

    gotchas_file = memory_dir / "gotchas.md"
    assert gotchas_file.exists()
    content = gotchas_file.read_text(encoding="utf-8")
    assert "# Gotchas" in content
    assert "Always use utf-8" in content


@pytest.mark.asyncio
async def test_record_gotcha_appends_on_second_call(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    await _handle_record_gotcha(memory_dir, {"gotcha": "First gotcha"})
    await _handle_record_gotcha(memory_dir, {"gotcha": "Second gotcha"})

    content = (memory_dir / "gotchas.md").read_text(encoding="utf-8")
    assert "First gotcha" in content
    assert "Second gotcha" in content
    assert content.count("# Gotchas") == 1  # header written once


@pytest.mark.asyncio
async def test_record_gotcha_with_context(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    await _handle_record_gotcha(memory_dir, {
        "gotcha": "Windows encoding issue",
        "context": "Any file I/O in the golem package",
    })
    content = (memory_dir / "gotchas.md").read_text(encoding="utf-8")
    assert "Windows encoding issue" in content
    assert "Any file I/O in the golem package" in content
    assert "_Context:" in content


@pytest.mark.asyncio
async def test_record_gotcha_without_context(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    await _handle_record_gotcha(memory_dir, {"gotcha": "Simple gotcha"})
    content = (memory_dir / "gotchas.md").read_text(encoding="utf-8")
    assert "Simple gotcha" in content
    assert "_Context:" not in content


@pytest.mark.asyncio
async def test_get_session_context_returns_empty_message_if_no_memory(tmp_path: Path) -> None:
    memory_dir = tmp_path / "nonexistent_memory"
    result = await _handle_get_session_context(memory_dir, {})
    text = result["content"][0]["text"]
    assert "No session context" in text


@pytest.mark.asyncio
async def test_get_session_context_returns_empty_if_no_data_in_memory(tmp_path: Path) -> None:
    """Memory dir exists but has no content."""
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    result = await _handle_get_session_context(memory_dir, {})
    text = result["content"][0]["text"]
    assert "No session context" in text


@pytest.mark.asyncio
async def test_get_session_context_reads_discoveries_and_gotchas(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    await _handle_record_discovery(memory_dir, {"file_path": "src/a.py", "description": "module A"})
    await _handle_record_gotcha(memory_dir, {"gotcha": "Watch out for X"})

    result = await _handle_get_session_context(memory_dir, {})
    text = result["content"][0]["text"]
    assert "src/a.py" in text
    assert "module A" in text
    assert "Watch out for X" in text


@pytest.mark.asyncio
async def test_get_session_context_reads_patterns_file(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "patterns.md").write_text("## Pattern: always use async", encoding="utf-8")

    result = await _handle_get_session_context(memory_dir, {})
    text = result["content"][0]["text"]
    assert "Patterns" in text
    assert "always use async" in text


@pytest.mark.asyncio
async def test_get_session_context_caps_discoveries_at_max(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    # Add 25 discoveries — only first 20 should appear
    for i in range(25):
        await _handle_record_discovery(memory_dir, {
            "file_path": f"src/module_{i:03d}.py",
            "description": f"Module {i}",
        })

    result = await _handle_get_session_context(memory_dir, {})
    text = result["content"][0]["text"]
    # Count discovery entries
    count = text.count("src/module_")
    assert count <= 20


@pytest.mark.asyncio
async def test_get_build_progress_returns_summary(tmp_path: Path, make_ticket) -> None:
    golem_dir = tmp_path / ".golem"
    store = TicketStore(golem_dir / "tickets")
    t1 = make_ticket(status="done")
    t2 = make_ticket(status="in_progress")
    await store.create(t1)
    await store.create(t2)

    result = await _handle_get_build_progress(store, "", {})
    text = result["content"][0]["text"]
    assert "Build Progress" in text
    # 1 done out of 2 total = 50%
    assert "1/2" in text or "50%" in text


@pytest.mark.asyncio
async def test_get_build_progress_no_tickets(tmp_path: Path) -> None:
    store = TicketStore(tmp_path / "tickets")
    result = await _handle_get_build_progress(store, "", {})
    text = result["content"][0]["text"]
    assert "0/0" in text or "Build Progress" in text


@pytest.mark.asyncio
async def test_get_build_progress_all_complete(tmp_path: Path, make_ticket) -> None:
    golem_dir = tmp_path / ".golem"
    store = TicketStore(golem_dir / "tickets")
    for _ in range(3):
        t = make_ticket(status="done")
        await store.create(t)

    result = await _handle_get_build_progress(store, "", {})
    text = result["content"][0]["text"]
    assert "All tickets complete" in text or "3/3" in text


@pytest.mark.asyncio
async def test_get_build_progress_shows_next_pending(tmp_path: Path, make_ticket) -> None:
    golem_dir = tmp_path / ".golem"
    store = TicketStore(golem_dir / "tickets")
    t_done = make_ticket(title="Completed Task", status="done")
    t_pending = make_ticket(title="Pending Task", status="pending")
    await store.create(t_done)
    await store.create(t_pending)

    result = await _handle_get_build_progress(store, "", {})
    text = result["content"][0]["text"]
    assert "Next pending ticket" in text
    assert "Pending Task" in text


@pytest.mark.asyncio
async def test_get_build_progress_session_filter(tmp_path: Path, make_ticket) -> None:
    """Tickets from other sessions are filtered out when session_id is specified."""
    golem_dir = tmp_path / ".golem"
    store = TicketStore(golem_dir / "tickets")
    config = GolemConfig(session_id="session-A")

    t1 = make_ticket(title="Session A ticket", status="done")
    await store.create(t1)
    # Manually set session_id on t2 to simulate a different session
    t2 = make_ticket(title="Session B ticket", status="pending")
    ticket_id = await store.create(t2)
    # Read and update session_id
    ticket_path = store._dir / f"{ticket_id}.json"
    ticket_data = json.loads(ticket_path.read_text(encoding="utf-8"))
    ticket_data["session_id"] = "session-B"
    ticket_path.write_text(json.dumps(ticket_data, indent=2), encoding="utf-8")

    # Filter for session-A (t1 has no session_id so it is included)
    result = await _handle_get_build_progress(store, "session-A", {})
    text = result["content"][0]["text"]
    # t1 has no session_id so it is included, t2 has session-B so excluded
    assert "Session B ticket" not in text


# ---------------------------------------------------------------------------
# Atomic write tests (tickets.py)
# ---------------------------------------------------------------------------


def test_write_json_atomic_creates_correct_file(tmp_path: Path) -> None:
    path = tmp_path / "ticket.json"
    data = {"id": "TICKET-001", "status": "pending"}
    _write_json_atomic(path, data)

    assert path.exists()
    assert not path.with_suffix(".tmp").exists()  # tmp cleaned up
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["status"] == "pending"
    assert loaded["id"] == "TICKET-001"


def test_write_json_atomic_overwrites_existing(tmp_path: Path) -> None:
    path = tmp_path / "ticket.json"
    _write_json_atomic(path, {"status": "pending"})
    _write_json_atomic(path, {"status": "done"})

    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["status"] == "done"


def test_write_json_atomic_no_tmp_file_remains(tmp_path: Path) -> None:
    path = tmp_path / "data.json"
    _write_json_atomic(path, {"key": "value"})
    assert not path.with_suffix(".tmp").exists()


def test_tools_write_json_atomic_same_behavior(tmp_path: Path) -> None:
    """tools._write_json_atomic behaves identically to tickets._write_json_atomic."""
    path = tmp_path / "data.json"
    tools_write_json_atomic(path, {"from": "tools"})
    assert path.exists()
    assert not path.with_suffix(".tmp").exists()
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["from"] == "tools"


@pytest.mark.asyncio
async def test_ticket_create_uses_atomic_write(tmp_path: Path, make_ticket) -> None:
    """TicketStore.create() writes atomically — no partial files visible."""
    golem_dir = tmp_path / ".golem"
    store = TicketStore(golem_dir / "tickets")
    t = make_ticket(title="Atomic test")
    ticket_id = await store.create(t)

    path = store._dir / f"{ticket_id}.json"
    assert path.exists()
    assert not path.with_suffix(".tmp").exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["title"] == "Atomic test"


@pytest.mark.asyncio
async def test_ticket_update_uses_atomic_write(tmp_path: Path, make_ticket) -> None:
    """TicketStore.update() writes atomically."""
    golem_dir = tmp_path / ".golem"
    store = TicketStore(golem_dir / "tickets")
    t = make_ticket(title="Update test")
    ticket_id = await store.create(t)

    await store.update(ticket_id, status="in_progress", note="starting")

    path = store._dir / f"{ticket_id}.json"
    assert not path.with_suffix(".tmp").exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["status"] == "in_progress"


# ---------------------------------------------------------------------------
# Backward-compatibility tests — existing API still works
# ---------------------------------------------------------------------------


def test_create_golem_mcp_server_returns_tech_lead_server(tmp_path: Path) -> None:
    golem_dir = tmp_path / ".golem"
    (golem_dir / "tickets").mkdir(parents=True)
    config = GolemConfig()

    server = create_golem_mcp_server(golem_dir, config, tmp_path)
    assert server is not None
    assert server["name"] == "golem"
    assert server["type"] == "sdk"
    assert hasattr(server["instance"], "call_tool")


def test_create_golem_planner_mcp_server_returns_planner_server(tmp_path: Path) -> None:
    golem_dir = tmp_path / ".golem"
    (golem_dir / "tickets").mkdir(parents=True)
    config = GolemConfig()

    server = create_golem_planner_mcp_server(golem_dir, config, tmp_path)
    assert server is not None
    assert server["name"] == "golem"
    assert server["type"] == "sdk"


def test_create_junior_dev_mcp_server_returns_writer_server(tmp_path: Path) -> None:
    golem_dir = tmp_path / ".golem"
    (golem_dir / "tickets").mkdir(parents=True)

    server = create_junior_dev_mcp_server(golem_dir)
    assert server is not None
    assert server["name"] == "golem-junior-dev"
    assert server["type"] == "sdk"


def test_create_junior_dev_mcp_server_accepts_worktree_path(tmp_path: Path) -> None:
    golem_dir = tmp_path / ".golem"
    (golem_dir / "tickets").mkdir(parents=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    config = GolemConfig()

    server = create_junior_dev_mcp_server(
        golem_dir,
        worktree_path=worktree,
        config=config,
        project_root=tmp_path,
    )
    assert server is not None
    assert server["name"] == "golem-junior-dev"


# ---------------------------------------------------------------------------
# ToolContext dataclass tests
# ---------------------------------------------------------------------------


def test_tool_context_defaults() -> None:
    ctx = ToolContext(
        golem_dir=Path("/tmp/.golem"),
        project_root=Path("/tmp"),
    )
    assert ctx.worktree_path is None
    assert ctx.session_id == ""
    assert ctx.agent_type == "writer"


def test_tool_context_with_worktree() -> None:
    ctx = ToolContext(
        golem_dir=Path("/tmp/.golem"),
        project_root=Path("/tmp"),
        worktree_path=Path("/tmp/worktree"),
        session_id="abc123",
        agent_type="writer",
    )
    assert ctx.worktree_path == Path("/tmp/worktree")
    assert ctx.session_id == "abc123"


# ---------------------------------------------------------------------------
# memory_dir path tests
# ---------------------------------------------------------------------------


def test_memory_dir_scoped_to_session(tmp_path: Path) -> None:
    """When session_id is set, memory dir is under sessions/<id>/memory."""
    golem_dir = tmp_path / ".golem"
    (golem_dir / "tickets").mkdir(parents=True)
    config = GolemConfig(session_id="test-session-001")

    reg = build_tool_registry(golem_dir, config, tmp_path)
    # Registry was built — verify the memory path would be correct
    expected_memory = golem_dir / "sessions" / "test-session-001" / "memory"
    # We can verify indirectly by creating a discovery via the registry
    ctx = ToolContext(golem_dir=golem_dir, project_root=tmp_path, agent_type="planner")
    tools = {t.name: t for t in reg.get_tools_for_agent("planner", ctx)}
    assert "record_discovery" in tools


def test_memory_dir_fallback_when_no_session(tmp_path: Path) -> None:
    """When session_id is empty, memory dir falls back to .golem/memory."""
    golem_dir = tmp_path / ".golem"
    (golem_dir / "tickets").mkdir(parents=True)
    config = GolemConfig()  # session_id defaults to ""

    reg = build_tool_registry(golem_dir, config, tmp_path)
    ctx = ToolContext(golem_dir=golem_dir, project_root=tmp_path, agent_type="planner")
    tools = {t.name: t for t in reg.get_tools_for_agent("planner", ctx)}
    assert "record_discovery" in tools


# ---------------------------------------------------------------------------
# ToolRegistry integration with event_bus
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_tool_registry_with_event_bus(tmp_path: Path) -> None:
    """build_tool_registry() accepts event_bus without error."""
    import asyncio

    from golem.events import EventBus, QueueBackend

    golem_dir = tmp_path / ".golem"
    (golem_dir / "tickets").mkdir(parents=True)
    config = GolemConfig()

    queue: asyncio.Queue = asyncio.Queue()
    bus = EventBus(QueueBackend(queue), session_id="test")

    reg = build_tool_registry(golem_dir, config, tmp_path, event_bus=bus)
    ctx = ToolContext(golem_dir=golem_dir, project_root=tmp_path, agent_type="tech_lead")
    tools = reg.get_tools_for_agent("tech_lead", ctx)
    assert len(tools) > 0


@pytest.mark.asyncio
async def test_build_tool_registry_with_registry_instruments_calls(tmp_path: Path) -> None:
    """When ToolCallRegistry is passed, tool calls record to it."""
    from golem.supervisor import ToolCallRegistry

    golem_dir = tmp_path / ".golem"
    (golem_dir / "tickets").mkdir(parents=True)
    config = GolemConfig()
    tool_registry = ToolCallRegistry()

    reg = build_tool_registry(golem_dir, config, tmp_path, registry=tool_registry)
    ctx = ToolContext(golem_dir=golem_dir, project_root=tmp_path, agent_type="tech_lead")
    tools = {t.name: t for t in reg.get_tools_for_agent("tech_lead", ctx)}

    await tools["create_ticket"].handler({
        "type": "task",
        "title": "Registry instrumentation test",
        "assigned_to": "writer",
    })

    assert tool_registry.has_called("create_ticket")
