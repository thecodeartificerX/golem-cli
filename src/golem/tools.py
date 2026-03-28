"""MCP tool definitions and handlers for Golem orchestration.

Provides SdkMcpTool instances for ticket CRUD, QA, worktree operations,
and branch merging. Tools are injected into SDK sessions via in-process
MCP servers (golem, golem-qa, golem-junior-dev).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from claude_agent_sdk import McpSdkServerConfig, SdkMcpTool, create_sdk_mcp_server

from golem.config import GolemConfig
from golem.qa import QAResult, run_qa
from golem.tickets import Ticket, TicketContext, TicketStore
from golem.worktree import commit_task, create_worktree, merge_group_branches

if TYPE_CHECKING:
    from golem.events import EventBus
    from golem.supervisor import ToolCallRegistry

# ---------------------------------------------------------------------------
# Input schemas (JSON Schema format used by SdkMcpTool.input_schema)
# ---------------------------------------------------------------------------

_CREATE_TICKET_INPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "description": "Ticket type: task|review|merge|qa|ux-test"},
        "title": {"type": "string", "description": "Short descriptive title"},
        "assigned_to": {"type": "string", "description": "Agent role to assign this ticket to"},
        "priority": {"type": "string", "description": "Priority: low|medium|high", "default": "medium"},
        "created_by": {"type": "string", "description": "Agent creating the ticket", "default": "planner"},
        "plan_file": {"type": "string", "description": "Path to the plan file for this ticket"},
        "blueprint": {"type": "string", "description": "Architectural context for the task"},
        "acceptance": {"type": "array", "items": {"type": "string"}, "description": "Acceptance criteria"},
        "qa_checks": {"type": "array", "items": {"type": "string"}, "description": "QA check commands"},
        "references": {"type": "array", "items": {"type": "string"}, "description": "Reference file paths"},
        "parallelism_hints": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Hints for parallel sub-tasks",
        },
    },
    "required": ["type", "title", "assigned_to"],
}

_UPDATE_TICKET_INPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "ticket_id": {"type": "string", "description": "Ticket ID, e.g. TICKET-001"},
        "status": {
            "type": "string",
            "description": "New status: pending|in_progress|qa_passed|ready_for_review|needs_work|approved|done",
        },
        "note": {"type": "string", "description": "Note to append to history"},
        "agent": {"type": "string", "description": "Agent performing the update", "default": "system"},
        "attachments": {"type": "array", "items": {"type": "string"}, "description": "File paths or URLs to attach"},
    },
    "required": ["ticket_id", "status", "note"],
}

_READ_TICKET_INPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "ticket_id": {"type": "string", "description": "Ticket ID, e.g. TICKET-001"},
    },
    "required": ["ticket_id"],
}

_LIST_TICKETS_INPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "status_filter": {"type": "string", "description": "Filter by status"},
        "assigned_to_filter": {"type": "string", "description": "Filter by assigned_to"},
    },
}

_RUN_QA_INPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "worktree_path": {"type": "string", "description": "Absolute path to the worktree"},
        "checks": {"type": "array", "items": {"type": "string"}, "description": "Spec-defined check commands"},
        "infrastructure_checks": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Infrastructure checks (run first)",
        },
    },
    "required": ["worktree_path", "checks"],
}

_CREATE_WORKTREE_INPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "group_id": {"type": "string", "description": "Group identifier"},
        "branch": {"type": "string", "description": "New branch name"},
        "base_branch": {"type": "string", "description": "Base branch to branch from"},
        "path": {"type": "string", "description": "Filesystem path for the worktree"},
        "repo_root": {"type": "string", "description": "Repository root path"},
    },
    "required": ["group_id", "branch", "base_branch", "path", "repo_root"],
}

_MERGE_BRANCHES_INPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "group_branches": {
            "type": "array",
            "items": {"type": "string"},
            "description": "List of branch names to merge",
        },
        "target_branch": {"type": "string", "description": "Branch to merge into"},
        "repo_root": {"type": "string", "description": "Repository root path"},
    },
    "required": ["group_branches", "target_branch", "repo_root"],
}

_COMMIT_WORKTREE_INPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "worktree_path": {"type": "string", "description": "Path to the worktree"},
        "task_id": {"type": "string", "description": "Task ID for commit message"},
        "description": {"type": "string", "description": "Description for commit message"},
    },
    "required": ["worktree_path", "task_id", "description"],
}


# ---------------------------------------------------------------------------
# Module-level handler functions — no closures, bound to store via partial
# ---------------------------------------------------------------------------


async def _handle_create_ticket(
    store: TicketStore,
    args: dict[str, object],
    event_bus: EventBus | None = None,
) -> dict[str, object]:
    refs_raw = args.get("references") or []
    acc_raw = args.get("acceptance") or []
    qa_raw = args.get("qa_checks") or []
    hints_raw = args.get("parallelism_hints") or []
    context = TicketContext(
        plan_file=str(args.get("plan_file") or ""),
        files={str(k): str(v) for k, v in (args.get("files") or {}).items()},  # type: ignore[union-attr]
        references=[str(r) for r in refs_raw],  # type: ignore[union-attr]
        blueprint=str(args.get("blueprint") or ""),
        acceptance=[str(a) for a in acc_raw],  # type: ignore[union-attr]
        qa_checks=[str(q) for q in qa_raw],  # type: ignore[union-attr]
        parallelism_hints=[str(p) for p in hints_raw],  # type: ignore[union-attr]
    )
    ticket = Ticket(
        id="",
        type=str(args["type"]),
        title=str(args["title"]),
        status="pending",
        priority=str(args.get("priority") or "medium"),
        created_by=str(args.get("created_by") or "planner"),
        assigned_to=str(args["assigned_to"]),
        context=context,
    )
    ticket_id = await store.create(ticket)
    if event_bus:
        from golem.events import TicketCreated
        await event_bus.emit(TicketCreated(
            ticket_id=ticket_id,
            title=ticket.title,
            assignee=ticket.assigned_to,
        ))
    return {"content": [{"type": "text", "text": json.dumps({"ticket_id": ticket_id})}]}


async def _handle_update_ticket(
    store: TicketStore,
    args: dict[str, object],
    event_bus: EventBus | None = None,
) -> dict[str, object]:
    attachments_raw = args.get("attachments")
    attachments: list[str] | None = [str(a) for a in attachments_raw] if attachments_raw is not None else None  # type: ignore[union-attr]
    ticket_id = str(args["ticket_id"])
    new_status = str(args["status"])
    old_status = ""
    if event_bus:
        try:
            existing = await store.read(ticket_id)
            old_status = existing.status
        except Exception:
            pass
    await store.update(
        ticket_id=ticket_id,
        status=new_status,
        note=str(args["note"]),
        attachments=attachments,
        agent=str(args.get("agent") or "system"),
    )
    if event_bus:
        from golem.events import TicketUpdated
        await event_bus.emit(TicketUpdated(
            ticket_id=ticket_id,
            old_status=old_status,
            new_status=new_status,
        ))
    return {"content": [{"type": "text", "text": json.dumps({"ok": True})}]}


async def _handle_read_ticket(store: TicketStore, args: dict[str, object]) -> dict[str, object]:
    ticket = await store.read(str(args["ticket_id"]))
    return {"content": [{"type": "text", "text": json.dumps(asdict(ticket))}]}


async def _handle_list_tickets(store: TicketStore, args: dict[str, object]) -> dict[str, object]:
    status_raw = args.get("status_filter")
    assigned_raw = args.get("assigned_to_filter")
    tickets = await store.list_tickets(
        status_filter=str(status_raw) if status_raw is not None else None,
        assigned_to_filter=str(assigned_raw) if assigned_raw is not None else None,
    )
    return {"content": [{"type": "text", "text": json.dumps([asdict(t) for t in tickets])}]}


async def _handle_run_qa(
    args: dict[str, object],
    event_bus: EventBus | None = None,
) -> dict[str, object]:
    import sys
    try:
        checks_raw = args.get("checks") or []
        infra_raw = args.get("infrastructure_checks") or []
        result = run_qa(
            worktree_path=str(args["worktree_path"]),
            checks=[str(c) for c in checks_raw],  # type: ignore[union-attr]
            infrastructure_checks=[str(c) for c in infra_raw],  # type: ignore[union-attr]
        )
    except Exception as e:
        # Safety net: always return valid QAResult JSON — never let an exception
        # propagate as a malformed MCP response
        result = QAResult(
            passed=False, checks=[], summary=f"QA runner crashed: {e}",
            cannot_validate=True, stage="crashed",
        )

    passed = sum(1 for c in result.checks if c.passed)
    total = len(result.checks)
    status = "PASSED" if result.passed else ("CANNOT_VALIDATE" if result.cannot_validate else "FAILED")
    print(f"[QA] {status} -- {passed}/{total} checks passed", file=sys.stderr)
    if event_bus:
        from golem.events import QAResult as QAResultEvent
        ticket_id = str(args.get("ticket_id") or "")
        await event_bus.emit(QAResultEvent(
            ticket_id=ticket_id,
            passed=result.passed,
            summary=result.summary,
            checks_run=total,
        ))
    return {"content": [{"type": "text", "text": json.dumps(asdict(result))}]}


async def _handle_create_worktree(
    args: dict[str, object],
    event_bus: EventBus | None = None,
) -> dict[str, object]:
    branch = str(args["branch"])
    path = str(args["path"])
    try:
        create_worktree(
            group_id=str(args["group_id"]),
            branch=branch,
            base_branch=str(args["base_branch"]),
            path=Path(path),
            repo_root=Path(str(args["repo_root"])),
        )
    except subprocess.CalledProcessError as e:
        return {"content": [{"type": "text", "text": json.dumps({"error": f"create_worktree failed: {e.stderr or e}"})}]}
    if event_bus:
        from golem.events import WorktreeCreated
        await event_bus.emit(WorktreeCreated(branch=branch, path=path))
    return {"content": [{"type": "text", "text": json.dumps({"ok": True})}]}


async def _handle_merge_branches(
    args: dict[str, object],
    event_bus: EventBus | None = None,
) -> dict[str, object]:
    branches_raw = args.get("group_branches") or []
    group_branches = [str(b) for b in branches_raw]  # type: ignore[union-attr]
    target_branch = str(args["target_branch"])
    success, conflict_info = merge_group_branches(
        group_branches=group_branches,
        target_branch=target_branch,
        repo_root=Path(str(args["repo_root"])),
    )
    if event_bus and success:
        from golem.events import MergeComplete
        source = group_branches[0] if group_branches else ""
        await event_bus.emit(MergeComplete(source_branch=source, target_branch=target_branch))
    return {"content": [{"type": "text", "text": json.dumps({"success": success, "conflict_info": conflict_info})}]}


async def _handle_commit_worktree(args: dict[str, object]) -> dict[str, object]:
    try:
        committed = commit_task(
            worktree_path=Path(str(args["worktree_path"])),
            task_id=str(args["task_id"]),
            description=str(args["description"]),
        )
    except subprocess.CalledProcessError as e:
        return {"content": [{"type": "text", "text": json.dumps({"error": f"commit_worktree failed: {e.stderr or e}"})}]}
    return {"content": [{"type": "text", "text": json.dumps({"committed": committed})}]}


# ---------------------------------------------------------------------------
# Tool builder — creates SdkMcpTool instances bound to runtime state
# ---------------------------------------------------------------------------


def _build_tools(
    golem_dir: Path,
    config: GolemConfig,  # noqa: ARG001
    project_root: Path,
    registry: ToolCallRegistry | None = None,
    event_bus: EventBus | None = None,
) -> list[SdkMcpTool]:
    """Build SdkMcpTool instances with handlers bound to golem_dir/config/project_root.

    If registry is provided, each tool call records itself via registry.record().
    If event_bus is provided, key tool calls emit structured GolemEvents.
    """
    store = TicketStore(golem_dir / "tickets")

    def _wrap(name: str, handler: object) -> object:
        if registry is None:
            return handler

        async def _instrumented(args: dict[str, object]) -> dict[str, object]:
            registry.record(name, 0)
            return await handler(args)  # type: ignore[misc]

        return _instrumented

    return [
        SdkMcpTool(
            name="create_ticket",
            description="Create a new ticket in the ticket store.",
            input_schema=_CREATE_TICKET_INPUT_SCHEMA,
            handler=_wrap("create_ticket", partial(_handle_create_ticket, store, event_bus=event_bus)),
        ),
        SdkMcpTool(
            name="update_ticket",
            description="Update ticket status and append a history event.",
            input_schema=_UPDATE_TICKET_INPUT_SCHEMA,
            handler=_wrap("update_ticket", partial(_handle_update_ticket, store, event_bus=event_bus)),
        ),
        SdkMcpTool(
            name="read_ticket",
            description="Read a ticket by ID.",
            input_schema=_READ_TICKET_INPUT_SCHEMA,
            handler=_wrap("read_ticket", partial(_handle_read_ticket, store)),
        ),
        SdkMcpTool(
            name="list_tickets",
            description="List tickets, optionally filtered by status or assignee.",
            input_schema=_LIST_TICKETS_INPUT_SCHEMA,
            handler=_wrap("list_tickets", partial(_handle_list_tickets, store)),
        ),
        SdkMcpTool(
            name="run_qa",
            description="Run deterministic QA checks in a worktree. Returns structured QAResult.",
            input_schema=_RUN_QA_INPUT_SCHEMA,
            handler=_wrap("run_qa", partial(_handle_run_qa, event_bus=event_bus)),
        ),
        SdkMcpTool(
            name="create_worktree",
            description="Create a git worktree for a group on a new branch.",
            input_schema=_CREATE_WORKTREE_INPUT_SCHEMA,
            handler=_wrap("create_worktree", partial(_handle_create_worktree, event_bus=event_bus)),
        ),
        SdkMcpTool(
            name="merge_branches",
            description="Merge group branches into a target branch.",
            input_schema=_MERGE_BRANCHES_INPUT_SCHEMA,
            handler=_wrap("merge_branches", partial(_handle_merge_branches, event_bus=event_bus)),
        ),
        SdkMcpTool(
            name="commit_worktree",
            description="Stage and commit all changes in a worktree.",
            input_schema=_COMMIT_WORKTREE_INPUT_SCHEMA,
            handler=_wrap("commit_worktree", _handle_commit_worktree),
        ),
    ]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_tech_lead_tools(
    golem_dir: Path,
    config: GolemConfig,
    project_root: Path,
    registry: ToolCallRegistry | None = None,
    event_bus: EventBus | None = None,
) -> list[SdkMcpTool]:
    """Return all SdkMcpTool instances for the Tech Lead SDK session."""
    return _build_tools(golem_dir, config, project_root, registry=registry, event_bus=event_bus)


def create_golem_mcp_server(
    golem_dir: Path,
    config: GolemConfig,
    project_root: Path,
    registry: ToolCallRegistry | None = None,
    event_bus: EventBus | None = None,
) -> McpSdkServerConfig:
    """Create an in-process MCP server with all Golem orchestration tools.

    If registry is provided, each tool call is recorded via registry.record().
    If event_bus is provided, key tool calls emit structured GolemEvents.
    """
    tools = _build_tools(golem_dir, config, project_root, registry=registry, event_bus=event_bus)
    return create_sdk_mcp_server("golem", tools=tools)


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


def create_qa_mcp_server(project_root: Path) -> McpSdkServerConfig:  # noqa: ARG001
    """Create a minimal in-process MCP server with only the run_qa tool (for writers)."""
    qa_tool = SdkMcpTool(
        name="run_qa",
        description="Run deterministic QA checks in a worktree. Returns structured QAResult.",
        input_schema=_RUN_QA_INPUT_SCHEMA,
        handler=_handle_run_qa,
    )
    return create_sdk_mcp_server("golem-qa", tools=[qa_tool])


def create_junior_dev_mcp_server(
    golem_dir: Path,
    registry: ToolCallRegistry | None = None,
    event_bus: EventBus | None = None,
) -> McpSdkServerConfig:
    """Create an MCP server for Junior Devs with run_qa + update_ticket + read_ticket tools.

    If registry is provided, each tool call is recorded via registry.record().
    If event_bus is provided, key tool calls emit structured GolemEvents.
    """
    store = TicketStore(golem_dir / "tickets")

    async def _instrumented_run_qa(args: dict[str, object]) -> dict[str, object]:
        if registry is not None:
            registry.record("run_qa", 0)
        return await _handle_run_qa(args, event_bus=event_bus)

    async def _instrumented_update_ticket(args: dict[str, object]) -> dict[str, object]:
        if registry is not None:
            registry.record("update_ticket", 0)
        return await _handle_update_ticket(store, args, event_bus=event_bus)

    async def _instrumented_read_ticket(args: dict[str, object]) -> dict[str, object]:
        if registry is not None:
            registry.record("read_ticket", 0)
        return await _handle_read_ticket(store, args)

    tools = [
        SdkMcpTool(
            name="run_qa",
            description="Run deterministic QA checks in a worktree. Returns structured QAResult.",
            input_schema=_RUN_QA_INPUT_SCHEMA,
            handler=_instrumented_run_qa,
        ),
        SdkMcpTool(
            name="update_ticket",
            description="Update a ticket's status and append a history event.",
            input_schema=_UPDATE_TICKET_INPUT_SCHEMA,
            handler=_instrumented_update_ticket,
        ),
        SdkMcpTool(
            name="read_ticket",
            description="Read a ticket by ID. Used to poll for Tech Lead review status.",
            input_schema=_READ_TICKET_INPUT_SCHEMA,
            handler=_instrumented_read_ticket,
        ),
    ]
    return create_sdk_mcp_server("golem-junior-dev", tools=tools)


# Backward-compatible alias
create_writer_mcp_server = create_junior_dev_mcp_server


async def handle_tool_call(
    tool_name: str,
    tool_input: dict[str, object],
    golem_dir: Path,
    config: GolemConfig,
    project_root: Path,
) -> str:
    """Dispatch a tool call directly to the appropriate Python function.

    Returns JSON-encoded result string. Used for direct testing without going through MCP.
    """
    tools = _build_tools(golem_dir, config, project_root)
    tool_map = {t.name: t for t in tools}
    if tool_name not in tool_map:
        raise ValueError(f"Unknown tool: {tool_name!r}")
    result = await tool_map[tool_name].handler(tool_input)
    # Extract text content from MCP response format
    for item in result.get("content", []):
        if item.get("type") == "text":
            return item["text"]  # type: ignore[return-value]
    return json.dumps(result)
