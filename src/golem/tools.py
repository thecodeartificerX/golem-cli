"""MCP tool definitions and handlers for Golem orchestration.

Provides SdkMcpTool instances for ticket CRUD, QA, worktree operations,
branch merging, and session memory. Tools are injected into SDK sessions via
in-process MCP servers (golem, golem-qa, golem-junior-dev).

The public API is built on ToolRegistry: call build_tool_registry() to get a
registry, then registry.get_tools_for_agent(agent_type, context) to get the
filtered SdkMcpTool list for a specific agent. Convenience wrappers
(create_golem_mcp_server, create_golem_planner_mcp_server,
create_junior_dev_mcp_server) delegate to this registry for backward compat.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict
from datetime import UTC, datetime
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
    from golem.tool_registry import ToolContext, ToolRegistry

# ---------------------------------------------------------------------------
# Constants for get_session_context output limits
# ---------------------------------------------------------------------------

_MAX_DISCOVERIES = 20
_MAX_MEMORY_CHARS = 1000  # per memory file
_MAX_DEBRIEFS = 5  # most recent debriefs to include from project-level memory

# ---------------------------------------------------------------------------
# Write-path containment guard
# ---------------------------------------------------------------------------


def _assert_within_worktree(path_arg: str, context: ToolContext) -> None:
    """Raise ValueError if path_arg is outside context.worktree_path.

    When context.worktree_path is None (tech lead), no restriction is applied.
    """
    if context.worktree_path is None:
        return  # tech lead has no restriction
    if not path_arg:
        return
    resolved = Path(path_arg).resolve()
    allowed = context.worktree_path.resolve()
    if not str(resolved).startswith(str(allowed)):
        raise ValueError(
            f"Write denied: {path_arg!r} is outside allowed worktree"
            f" {str(context.worktree_path)!r}"
        )


# ---------------------------------------------------------------------------
# Atomic JSON write helper (also used by tickets.py)
# ---------------------------------------------------------------------------


def _write_json_atomic(path: Path, data: dict) -> None:  # type: ignore[type-arg]
    """Write JSON to path atomically via tmp+rename.

    Uses a sibling .tmp file in the same directory so rename stays on the same
    filesystem/volume (required for atomic rename on Windows NTFS).
    """
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


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
    depends_on_raw = args.get("depends_on") or []
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
        depends_on=[str(d) for d in depends_on_raw],  # type: ignore[union-attr]
        edict_id=str(args.get("edict_id") or ""),
        pipeline_stage=str(args.get("pipeline_stage") or "planner"),
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
    golem_dir: Path | None = None,
) -> dict[str, object]:
    import asyncio
    import sys
    try:
        checks_raw = args.get("checks") or []
        infra_raw = args.get("infrastructure_checks") or []
        qa_depth_raw = args.get("qa_depth") or "standard"
        worktree_path = str(args["worktree_path"])
        checks_list = [str(c) for c in checks_raw]  # type: ignore[union-attr]
        infra_list = [str(c) for c in infra_raw]  # type: ignore[union-attr]
        qa_depth_str = str(qa_depth_raw)
        # run_qa uses subprocess.run() (synchronous, blocks up to 120 s per check).
        # Offload to a thread so the asyncio event loop stays responsive and the SDK
        # can continue processing MCP control_request messages during QA execution.
        result = await asyncio.to_thread(
            run_qa,
            worktree_path=worktree_path,
            checks=checks_list,
            infrastructure_checks=infra_list,
            qa_depth=qa_depth_str,
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
    if golem_dir is not None:
        from golem.progress import ProgressLogger as _ProgressLogger
        _logger = _ProgressLogger(golem_dir)
        _logger.log_qa_result(
            ticket_id=str(args.get("ticket_id", "unknown")),
            passed=result.passed,
            summary=result.summary,
        )
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


async def _handle_create_blocker(store: TicketStore, args: dict[str, object]) -> dict[str, object]:
    """Writer creates a blocker when stuck after max rework cycles.

    Creates a blocker ticket assigned to tech_lead and sets the original ticket to 'blocked'.
    """
    original_ticket_id = str(args["original_ticket_id"])
    reason = str(args["reason"])
    context_text = str(args.get("context") or "")

    # Read the original ticket to get its title
    original_ticket = await store.read(original_ticket_id)

    # Create blocker ticket assigned to tech_lead
    blocker = Ticket(
        id="",
        type="blocker",
        title=f"Blocked: {original_ticket.title}",
        status="pending",
        priority="high",
        created_by="writer",
        assigned_to="tech_lead",
        context=TicketContext(
            blueprint=f"Original ticket: {original_ticket_id}\nReason: {reason}\n\n{context_text}",
            references=[original_ticket_id],
        ),
    )
    blocker_id = await store.create(blocker)

    # Set the original ticket to blocked
    await store.update(
        ticket_id=original_ticket_id,
        status="blocked",
        note=f"Blocked after max rework cycles. Blocker ticket: {blocker_id}. Reason: {reason}",
        agent="writer",
    )

    return {"content": [{"type": "text", "text": json.dumps({"blocker_id": blocker_id, "status": "created"})}]}


async def _handle_get_build_progress(
    store: TicketStore,
    session_id: str,
    args: dict[str, object],
) -> dict[str, object]:
    sid = str(args.get("session_id") or session_id)
    tickets = await store.list_tickets()
    if sid:
        tickets = [t for t in tickets if not t.session_id or t.session_id == sid]

    status_counts: dict[str, int] = {}
    for t in tickets:
        status_counts[t.status] = status_counts.get(t.status, 0) + 1

    total = len(tickets)
    done = status_counts.get("done", 0) + status_counts.get("approved", 0)
    pct = int(done / total * 100) if total else 0

    # Find next pending ticket
    next_ticket = next(
        (t for t in tickets if t.status == "pending"), None
    )

    lines = [
        f"Build Progress: {done}/{total} tickets ({pct}%)",
        "",
        "Status breakdown:",
    ]
    for status in ("done", "approved", "in_progress", "qa_passed",
                   "needs_work", "pending", "ready_for_review"):
        count = status_counts.get(status, 0)
        if count:
            lines.append(f"  {status}: {count}")

    if next_ticket:
        lines += [
            "",
            "Next pending ticket:",
            f"  ID: {next_ticket.id}",
            f"  Title: {next_ticket.title}",
            f"  Assigned to: {next_ticket.assigned_to}",
        ]
    elif total > 0:
        lines.append("")
        lines.append("All tickets complete.")

    text = "\n".join(lines)
    return {"content": [{"type": "text", "text": text}]}


def _record_discovery_sync(memory_dir: Path, file_path: str, description: str, category: str) -> None:
    """Synchronous core of record_discovery — runs in a thread via asyncio.to_thread()."""
    memory_dir.mkdir(parents=True, exist_ok=True)
    map_file = memory_dir / "codebase_map.json"

    codebase_map: dict[str, object] = {"discovered_files": {}, "last_updated": None}
    if map_file.exists():
        try:
            raw = map_file.read_text(encoding="utf-8")
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                codebase_map = parsed
        except (json.JSONDecodeError, OSError):
            pass  # corrupt file: start fresh

    discovered = codebase_map.setdefault("discovered_files", {})
    if not isinstance(discovered, dict):
        discovered = {}
        codebase_map["discovered_files"] = discovered

    discovered[file_path] = {
        "description": description,
        "category": category,
        "discovered_at": datetime.now(tz=UTC).isoformat(),
    }
    codebase_map["last_updated"] = datetime.now(tz=UTC).isoformat()
    _write_json_atomic(map_file, codebase_map)


async def _handle_record_discovery(
    memory_dir: Path,
    args: dict[str, object],
) -> dict[str, object]:
    import asyncio

    file_path = str(args["file_path"])
    description = str(args["description"])
    category = str(args.get("category") or "general")
    # Offload file I/O to a thread — _write_json_atomic uses os.replace() which can
    # block on Windows when another process has the file open.
    await asyncio.to_thread(_record_discovery_sync, memory_dir, file_path, description, category)
    return {"content": [{"type": "text", "text": json.dumps({"ok": True, "file": file_path})}]}


def _record_gotcha_sync(memory_dir: Path, gotcha_text: str, context_text: str) -> None:
    """Synchronous core of record_gotcha — runs in a thread via asyncio.to_thread()."""
    memory_dir.mkdir(parents=True, exist_ok=True)
    gotchas_file = memory_dir / "gotchas.md"

    now = datetime.now(tz=UTC)
    timestamp = (
        f"{now.year}-{now.month:02d}-{now.day:02d}"
        f" {now.hour:02d}:{now.minute:02d}"
    )
    is_new = not gotchas_file.exists() or gotchas_file.stat().st_size == 0
    header = "# Gotchas & Pitfalls\n\nThings to watch out for in this codebase.\n" if is_new else ""
    entry = f"\n## [{timestamp}]\n{gotcha_text}"
    if context_text:
        entry += f"\n\n_Context: {context_text}_"
    entry += "\n"

    with open(gotchas_file, "a" if not is_new else "w", encoding="utf-8") as f:
        f.write(header + entry)


async def _handle_record_gotcha(
    memory_dir: Path,
    args: dict[str, object],
) -> dict[str, object]:
    import asyncio

    gotcha_text = str(args["gotcha"])
    context_text = str(args.get("context") or "")
    # Offload file I/O to a thread — file writes can block on Windows.
    await asyncio.to_thread(_record_gotcha_sync, memory_dir, gotcha_text, context_text)
    return {"content": [{"type": "text", "text": json.dumps({"ok": True})}]}


def _format_patterns_json(patterns_file: Path) -> str:
    """Parse patterns.json and return formatted lines, or empty string on failure."""
    if not patterns_file.exists():
        return ""
    try:
        raw = patterns_file.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (json.JSONDecodeError, OSError):
        return ""
    if not isinstance(data, dict):
        return ""

    lines: list[str] = []
    for p in data.get("patterns", []):
        if p:
            lines.append(f"- {p}")
    for r in data.get("recommendations", []):
        if r:
            lines.append(f"- [rec] {r}")
    for o in data.get("outcomes", []):
        if o:
            lines.append(f"- [outcome] {o}")
    if not lines:
        return ""
    return "\n".join(lines)[-_MAX_MEMORY_CHARS:]


def _read_memory_dir_sync(memory_dir: Path) -> str:
    """Read all memory files from a single directory and return formatted text."""
    parts: list[str] = []

    # Codebase map — cap at _MAX_DISCOVERIES entries
    map_file = memory_dir / "codebase_map.json"
    if map_file.exists():
        try:
            raw = map_file.read_text(encoding="utf-8")
            data = json.loads(raw)
            discovered: dict[str, object] = data.get("discovered_files", {})
            if discovered:
                parts.append("## Codebase Discoveries")
                for fp, info in list(discovered.items())[:_MAX_DISCOVERIES]:
                    if isinstance(info, dict):
                        desc = info.get("description", "")
                    else:
                        desc = ""
                    parts.append(f"- `{fp}`: {desc}")
        except (json.JSONDecodeError, OSError):
            pass

    # Gotchas — last _MAX_MEMORY_CHARS chars
    gotchas_file = memory_dir / "gotchas.md"
    if gotchas_file.exists():
        try:
            content = gotchas_file.read_text(encoding="utf-8")
            if content.strip():
                parts.append("\n## Gotchas")
                parts.append(content[-_MAX_MEMORY_CHARS:])
        except OSError:
            pass

    # Patterns — JSON format (written by insight_extractor as patterns.json)
    formatted = _format_patterns_json(memory_dir / "patterns.json")
    if formatted:
        parts.append("\n## Patterns")
        parts.append(formatted)

    return "\n".join(parts)


def _read_debriefs_sync(debriefs_dir: Path) -> str:
    """Read the most recent debrief files from a debriefs directory."""
    if not debriefs_dir.exists() or not debriefs_dir.is_dir():
        return ""

    try:
        debrief_files = sorted(debriefs_dir.glob("*.md"), key=lambda p: p.name, reverse=True)
    except OSError:
        return ""

    if not debrief_files:
        return ""

    parts: list[str] = []
    for df in debrief_files[:_MAX_DEBRIEFS]:
        try:
            content = df.read_text(encoding="utf-8").strip()
            if content:
                parts.append(f"### {df.stem}")
                parts.append(content[-_MAX_MEMORY_CHARS:])
        except OSError:
            pass

    return "\n".join(parts)


def _read_session_context_sync(edict_memory_dir: Path, project_root: Path | None = None) -> str:
    """Synchronous core of get_session_context — runs in a thread via asyncio.to_thread().

    Reads from both the project-level .golem/memory/ (cross-edict persistence)
    and the per-edict memory directory. Project-level memory is presented first.
    """
    prior_parts: list[str] = []
    current_parts: list[str] = []

    # --- Project-level memory (cross-edict persistence) ---
    if project_root is not None:
        project_memory_dir = project_root / ".golem" / "memory"
        if project_memory_dir.exists() and project_memory_dir.is_dir():
            # Debriefs
            debrief_text = _read_debriefs_sync(project_memory_dir / "debriefs")
            if debrief_text:
                prior_parts.append("## Debriefs")
                prior_parts.append(debrief_text)

            # Project-level gotchas
            project_gotchas = project_memory_dir / "gotchas.md"
            if project_gotchas.exists():
                try:
                    content = project_gotchas.read_text(encoding="utf-8")
                    if content.strip():
                        prior_parts.append("\n## Project Gotchas")
                        prior_parts.append(content[-_MAX_MEMORY_CHARS:])
                except OSError:
                    pass

            # Project-level patterns
            formatted = _format_patterns_json(project_memory_dir / "patterns.json")
            if formatted:
                prior_parts.append("\n## Project Patterns")
                prior_parts.append(formatted)

    # --- Per-edict memory (current session) ---
    if edict_memory_dir.exists() and edict_memory_dir.is_dir():
        edict_text = _read_memory_dir_sync(edict_memory_dir)
        if edict_text:
            current_parts.append(edict_text)

    # --- Assemble output with section headers ---
    output_parts: list[str] = []
    if prior_parts:
        output_parts.append("=== PRIOR EDICT KNOWLEDGE ===")
        output_parts.extend(prior_parts)
    if current_parts:
        output_parts.append("\n=== CURRENT SESSION ===")
        output_parts.extend(current_parts)

    return "\n".join(output_parts)


async def _handle_get_session_context(
    memory_dir: Path,
    project_root: Path | None,
    args: dict[str, object],  # noqa: ARG001
) -> dict[str, object]:
    import asyncio

    # Check if either memory source exists
    project_memory_exists = (
        project_root is not None
        and (project_root / ".golem" / "memory").exists()
    )
    if not memory_dir.exists() and not project_memory_exists:
        return {"content": [{"type": "text", "text": "No session context available yet."}]}

    # Offload all file I/O to a thread so the asyncio event loop stays responsive.
    text = await asyncio.to_thread(_read_session_context_sync, memory_dir, project_root)
    if not text:
        return {"content": [{"type": "text", "text": "No session context available yet."}]}

    return {"content": [{"type": "text", "text": text}]}


# ---------------------------------------------------------------------------
# ToolRegistry factory — replaces the three create_*_mcp_server factories
# ---------------------------------------------------------------------------


def build_tool_registry(
    golem_dir: Path,
    config: GolemConfig,
    project_root: Path,
    registry: ToolCallRegistry | None = None,
    event_bus: EventBus | None = None,
) -> ToolRegistry:
    """Build a ToolRegistry with all Golem tools registered.

    Call registry.get_tools_for_agent(agent_type, context) on the result to get
    the filtered SdkMcpTool list for a specific agent type.

    If registry is provided, each tool call records itself via registry.record().
    If event_bus is provided, key tool calls emit structured GolemEvents.
    """
    from golem.tool_registry import (
        PLANNER_TOOLS,
        TECH_LEAD_TOOLS,
        WRITER_TOOLS,
        AgentType,
        RegisteredTool,
        ToolContext,
        ToolRegistry,
    )

    store = TicketStore(golem_dir / "tickets")
    session_id = config.session_id
    memory_dir = golem_dir / "sessions" / session_id / "memory" if session_id else golem_dir / "memory"

    def _allowed(name: str) -> frozenset[AgentType]:
        result: set[AgentType] = set()
        if name in PLANNER_TOOLS:
            result.add("planner")
        if name in WRITER_TOOLS:
            result.add("writer")
        if name in TECH_LEAD_TOOLS:
            result.add("tech_lead")
        return frozenset(result)

    def _wrap_handler(name: str, handler: object) -> object:
        """Optionally instrument with registry.record()."""
        if registry is None:
            return handler

        async def _instrumented(args: dict[str, object]) -> dict[str, object]:
            registry.record(name, 0)
            return await handler(args)  # type: ignore[misc]

        return _instrumented

    # --- existing tool factories ---

    def _make_create_ticket(_ctx: ToolContext) -> SdkMcpTool:
        return SdkMcpTool(
            name="create_ticket",
            description="Create a new ticket in the ticket store.",
            input_schema={
                "type": "object",
                "properties": {
                    "type": {"type": "string", "description": "Ticket type: task|review|merge|qa|ux-test|blocker|escalation"},
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
                    "depends_on": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Ticket IDs this ticket depends on (for dependency ordering)",
                    },
                    "edict_id": {"type": "string", "description": "Parent edict ID (e.g., EDICT-001)"},
                    "pipeline_stage": {
                        "type": "string",
                        "description": "Board column: planner|tech_lead|junior_dev|qa|done|failed",
                        "default": "planner",
                    },
                },
                "required": ["type", "title", "assigned_to"],
            },
            handler=_wrap_handler(
                "create_ticket",
                partial(_handle_create_ticket, store, event_bus=event_bus),
            ),
        )

    def _make_update_ticket(_ctx: ToolContext) -> SdkMcpTool:
        return SdkMcpTool(
            name="update_ticket",
            description="Update ticket status and append a history event.",
            input_schema={
                "type": "object",
                "properties": {
                    "ticket_id": {"type": "string", "description": "Ticket ID, e.g. TICKET-001"},
                    "status": {
                        "type": "string",
                        "description": "New status: pending|in_progress|qa_passed|ready_for_review|needs_work|approved|done|blocked",
                    },
                    "note": {"type": "string", "description": "Note to append to history"},
                    "agent": {"type": "string", "description": "Agent performing the update", "default": "system"},
                    "attachments": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "File paths or URLs to attach",
                    },
                },
                "required": ["ticket_id", "status", "note"],
            },
            handler=_wrap_handler(
                "update_ticket",
                partial(_handle_update_ticket, store, event_bus=event_bus),
            ),
        )

    def _make_read_ticket(_ctx: ToolContext) -> SdkMcpTool:
        return SdkMcpTool(
            name="read_ticket",
            description="Read a ticket by ID.",
            input_schema={
                "type": "object",
                "properties": {
                    "ticket_id": {"type": "string", "description": "Ticket ID, e.g. TICKET-001"},
                },
                "required": ["ticket_id"],
            },
            handler=_wrap_handler("read_ticket", partial(_handle_read_ticket, store)),
        )

    def _make_list_tickets(_ctx: ToolContext) -> SdkMcpTool:
        return SdkMcpTool(
            name="list_tickets",
            description="List tickets, optionally filtered by status or assignee.",
            input_schema={
                "type": "object",
                "properties": {
                    "status_filter": {"type": "string", "description": "Filter by status"},
                    "assigned_to_filter": {"type": "string", "description": "Filter by assigned_to"},
                },
            },
            handler=_wrap_handler("list_tickets", partial(_handle_list_tickets, store)),
        )

    def _make_run_qa(_ctx: ToolContext) -> SdkMcpTool:
        return SdkMcpTool(
            name="run_qa",
            description="Run deterministic QA checks in a worktree. Returns structured QAResult.",
            input_schema={
                "type": "object",
                "properties": {
                    "worktree_path": {"type": "string", "description": "Absolute path to the worktree"},
                    "checks": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Spec-defined check commands",
                    },
                    "infrastructure_checks": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Infrastructure checks (run first)",
                    },
                    "qa_depth": {
                        "type": "string",
                        "description": "QA depth: minimal (infra only) | standard (infra+spec) | strict (infra+spec+recheck loop)",
                        "default": "standard",
                    },
                },
                "required": ["worktree_path", "checks"],
            },
            handler=_wrap_handler(
                "run_qa",
                partial(_handle_run_qa, event_bus=event_bus, golem_dir=golem_dir),
            ),
        )

    def _make_create_worktree(_ctx: ToolContext) -> SdkMcpTool:
        return SdkMcpTool(
            name="create_worktree",
            description="Create a git worktree for a group on a new branch.",
            input_schema={
                "type": "object",
                "properties": {
                    "group_id": {"type": "string", "description": "Group identifier"},
                    "branch": {"type": "string", "description": "New branch name"},
                    "base_branch": {"type": "string", "description": "Base branch to branch from"},
                    "path": {"type": "string", "description": "Filesystem path for the worktree"},
                    "repo_root": {"type": "string", "description": "Repository root path"},
                },
                "required": ["group_id", "branch", "base_branch", "path", "repo_root"],
            },
            handler=_wrap_handler(
                "create_worktree",
                partial(_handle_create_worktree, event_bus=event_bus),
            ),
        )

    def _make_merge_branches(_ctx: ToolContext) -> SdkMcpTool:
        return SdkMcpTool(
            name="merge_branches",
            description="Merge group branches into a target branch.",
            input_schema={
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
            },
            handler=_wrap_handler(
                "merge_branches",
                partial(_handle_merge_branches, event_bus=event_bus),
            ),
        )

    def _make_commit_worktree(ctx: ToolContext) -> SdkMcpTool:
        async def _guarded(args: dict[str, object]) -> dict[str, object]:
            try:
                _assert_within_worktree(str(args.get("worktree_path") or ""), ctx)
            except ValueError as exc:
                return {"content": [{"type": "text", "text": json.dumps({"error": str(exc)})}]}
            return await _handle_commit_worktree(args)

        return SdkMcpTool(
            name="commit_worktree",
            description="Stage and commit all changes in an assigned worktree.",
            input_schema={
                "type": "object",
                "properties": {
                    "worktree_path": {"type": "string", "description": "Path to the worktree"},
                    "task_id": {"type": "string", "description": "Task ID for commit message"},
                    "description": {"type": "string", "description": "Description for commit message"},
                },
                "required": ["worktree_path", "task_id", "description"],
            },
            handler=_wrap_handler("commit_worktree", _guarded),
        )

    # --- new tools ---

    def _make_get_build_progress(_ctx: ToolContext) -> SdkMcpTool:
        return SdkMcpTool(
            name="get_build_progress",
            description=(
                "Get current build progress: ticket counts by status, percentage done, "
                "and next pending ticket. Call at session start for orientation."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID to filter tickets. Defaults to current session.",
                    },
                },
            },
            handler=_wrap_handler(
                "get_build_progress",
                partial(_handle_get_build_progress, store, session_id),
            ),
        )

    def _make_record_discovery(_ctx: ToolContext) -> SdkMcpTool:
        return SdkMcpTool(
            name="record_discovery",
            description=(
                "Record a codebase discovery to session memory. "
                "Use when you learn something important about the codebase structure."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to the file or module being documented (relative or absolute)",
                    },
                    "description": {
                        "type": "string",
                        "description": "What was discovered about this file or module",
                    },
                    "category": {
                        "type": "string",
                        "description": 'Category: "api", "config", "ui", "test", "general"',
                    },
                },
                "required": ["file_path", "description"],
            },
            handler=_wrap_handler(
                "record_discovery",
                partial(_handle_record_discovery, memory_dir),
            ),
        )

    def _make_record_gotcha(_ctx: ToolContext) -> SdkMcpTool:
        return SdkMcpTool(
            name="record_gotcha",
            description=(
                "Record a gotcha or pitfall. "
                "Use when you encounter something future sessions should know to avoid repeating mistakes."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "gotcha": {
                        "type": "string",
                        "description": "Description of the gotcha or pitfall",
                    },
                    "context": {
                        "type": "string",
                        "description": "When this gotcha applies (optional)",
                    },
                },
                "required": ["gotcha"],
            },
            handler=_wrap_handler(
                "record_gotcha",
                partial(_handle_record_gotcha, memory_dir),
            ),
        )

    def _make_get_session_context(_ctx: ToolContext) -> SdkMcpTool:
        return SdkMcpTool(
            name="get_session_context",
            description=(
                "Get context from previous sessions and prior edicts: codebase discoveries, "
                "gotchas, patterns, and debrief summaries. "
                "Call at session start to pick up where the last session left off."
            ),
            input_schema={
                "type": "object",
                "properties": {},
            },
            handler=_wrap_handler(
                "get_session_context",
                partial(_handle_get_session_context, memory_dir, project_root),
            ),
        )

    def _make_create_blocker(_ctx: ToolContext) -> SdkMcpTool:
        return SdkMcpTool(
            name="create_blocker",
            description=(
                "Writer creates a blocker when stuck after max rework cycles. "
                "Creates a blocker ticket for Tech Lead and marks the original ticket as blocked."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "original_ticket_id": {
                        "type": "string",
                        "description": "Ticket ID that is blocked, e.g. TICKET-001",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why the writer is stuck — what failed after max rework cycles",
                    },
                    "context": {
                        "type": "string",
                        "description": "Additional context: error messages, QA output, what was tried",
                    },
                },
                "required": ["original_ticket_id", "reason"],
            },
            handler=_wrap_handler(
                "create_blocker",
                partial(_handle_create_blocker, store),
            ),
        )

    # --- register all tools ---
    tool_reg = ToolRegistry()
    for name, factory in [
        ("create_ticket", _make_create_ticket),
        ("update_ticket", _make_update_ticket),
        ("read_ticket", _make_read_ticket),
        ("list_tickets", _make_list_tickets),
        ("run_qa", _make_run_qa),
        ("create_worktree", _make_create_worktree),
        ("merge_branches", _make_merge_branches),
        ("commit_worktree", _make_commit_worktree),
        ("create_blocker", _make_create_blocker),
        ("get_build_progress", _make_get_build_progress),
        ("record_discovery", _make_record_discovery),
        ("record_gotcha", _make_record_gotcha),
        ("get_session_context", _make_get_session_context),
    ]:
        tool_reg.register(RegisteredTool(
            name=name,
            allowed_for=_allowed(name),
            factory=factory,
        ))

    return tool_reg


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
    """Return all SdkMcpTool instances for the Tech Lead SDK session.

    Delegates to build_tool_registry() — returns the full tech_lead tool set.
    If registry is provided, each tool call is recorded via registry.record().
    If event_bus is provided, key tool calls emit structured GolemEvents.
    """
    from golem.tool_registry import ToolContext

    reg = build_tool_registry(golem_dir, config, project_root, registry=registry, event_bus=event_bus)
    ctx = ToolContext(golem_dir=golem_dir, project_root=project_root, agent_type="tech_lead")
    return reg.get_tools_for_agent("tech_lead", ctx)


def create_golem_mcp_server(
    golem_dir: Path,
    config: GolemConfig,
    project_root: Path,
    registry: ToolCallRegistry | None = None,
    event_bus: EventBus | None = None,
) -> McpSdkServerConfig:
    """Create an in-process MCP server with all Golem orchestration tools (Tech Lead).

    Uses build_tool_registry() internally — tech_lead agent type gets all 12 tools.
    If registry is provided, each tool call is recorded via registry.record().
    If event_bus is provided, key tool calls emit structured GolemEvents.
    """
    from golem.tool_registry import ToolContext

    reg = build_tool_registry(golem_dir, config, project_root, registry=registry, event_bus=event_bus)
    ctx = ToolContext(golem_dir=golem_dir, project_root=project_root, agent_type="tech_lead")
    tools = reg.get_tools_for_agent("tech_lead", ctx)
    return create_sdk_mcp_server("golem", tools=tools)


def create_golem_planner_mcp_server(
    golem_dir: Path,
    config: GolemConfig,
    project_root: Path,
    event_bus: EventBus | None = None,
) -> McpSdkServerConfig:
    """Create an in-process MCP server for Planner — read-only + create_ticket + memory tools.

    Planner gets: read_ticket, list_tickets, create_ticket, get_session_context,
    record_discovery, record_gotcha, get_build_progress.
    Explicitly excluded: create_worktree, merge_branches, commit_worktree, run_qa, update_ticket.
    """
    from golem.tool_registry import ToolContext

    reg = build_tool_registry(golem_dir, config, project_root, event_bus=event_bus)
    ctx = ToolContext(golem_dir=golem_dir, project_root=project_root, agent_type="planner")
    tools = reg.get_tools_for_agent("planner", ctx)
    return create_sdk_mcp_server("golem", tools=tools)


def create_junior_dev_mcp_server(
    golem_dir: Path,
    registry: ToolCallRegistry | None = None,
    event_bus: EventBus | None = None,
    worktree_path: Path | None = None,
    config: GolemConfig | None = None,
    project_root: Path | None = None,
) -> McpSdkServerConfig:
    """Create an MCP server for Junior Devs (writer agent).

    Writer gets: run_qa, update_ticket, read_ticket, record_discovery,
    record_gotcha, get_session_context, commit_worktree (path-contained),
    get_build_progress.

    Pass worktree_path to enable write-path containment for commit_worktree.
    Pass config and project_root to correctly scope the memory directory.

    If registry is provided, each tool call is recorded via registry.record().
    If event_bus is provided, key tool calls emit structured GolemEvents.
    """
    from golem.config import GolemConfig as _GC
    from golem.tool_registry import ToolContext

    cfg = config or _GC()
    root = project_root or golem_dir.parent
    reg = build_tool_registry(golem_dir, cfg, root, registry=registry, event_bus=event_bus)
    ctx = ToolContext(
        golem_dir=golem_dir,
        project_root=root,
        worktree_path=worktree_path,
        agent_type="writer",
    )
    tools = reg.get_tools_for_agent("writer", ctx)
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
    Delegates to build_tool_registry() — exposes the full tech_lead tool set.
    """
    from golem.tool_registry import ToolContext

    reg = build_tool_registry(golem_dir, config, project_root)
    ctx = ToolContext(golem_dir=golem_dir, project_root=project_root, agent_type="tech_lead")
    tools = reg.get_tools_for_agent("tech_lead", ctx)
    tool_map = {t.name: t for t in tools}
    if tool_name not in tool_map:
        raise ValueError(f"Unknown tool: {tool_name!r}")
    result = await tool_map[tool_name].handler(tool_input)
    # Extract text content from MCP response format
    for item in result.get("content", []):
        if item.get("type") == "text":
            return item["text"]  # type: ignore[return-value]
    return json.dumps(result)
