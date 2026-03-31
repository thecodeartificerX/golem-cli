"""ToolRegistry — central registry of Golem MCP tools with per-agent filtering.

Mirrors Aperant's ToolRegistry pattern: tools are registered once with metadata
(name, allowed_for set, factory callable). At session startup, get_tools_for_agent()
filters to only the tools the given agent type may call, then binds them to a
ToolContext (golem_dir, project_root, worktree_path, session_id).

Write-path containment is enforced inside the commit_worktree factory via
_assert_within_worktree() in tools.py — not at call-site.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Literal

from claude_agent_sdk import SdkMcpTool

if TYPE_CHECKING:
    pass

AgentType = Literal["planner", "writer", "tech_lead"]

# ---------------------------------------------------------------------------
# Per-agent tool allowlists — module-level constants, single source of truth
# ---------------------------------------------------------------------------

PLANNER_TOOLS: frozenset[str] = frozenset({
    # Read-only ticket access — planner needs to check for duplicates
    "read_ticket",
    "list_tickets",
    # Planner's primary write capability
    "create_ticket",
    # Cross-session knowledge (read)
    "get_session_context",
    # Planner writes discoveries as it reads the spec + codebase
    "record_discovery",
    "record_gotcha",
    # Progress view (read-only)
    "get_build_progress",
})

WRITER_TOOLS: frozenset[str] = frozenset({
    # Ticket lifecycle for the assigned ticket only
    "update_ticket",
    "read_ticket",
    # QA runner — writers must validate their own work
    "run_qa",
    # Session memory — writers log what they learn
    "record_discovery",
    "record_gotcha",
    "get_session_context",
    # Commit their worktree changes (scoped to their worktree)
    "commit_worktree",
    # Progress awareness — writers may check overall status
    "get_build_progress",
})

TECH_LEAD_TOOLS: frozenset[str] = frozenset({
    # Full ticket CRUD
    "create_ticket",
    "update_ticket",
    "read_ticket",
    "list_tickets",
    # QA runner
    "run_qa",
    # Git operations — tech lead coordinates all worktrees
    "create_worktree",
    "merge_branches",
    "commit_worktree",
    # Session memory
    "record_discovery",
    "record_gotcha",
    "get_session_context",
    # Progress and build state
    "get_build_progress",
})


@dataclass
class ToolContext:
    """Runtime context passed to tool factories at bind time.

    Mirrors Aperant's ToolContext. Created fresh for each agent session —
    never stored globally. worktree_path is only set for writer sessions
    to enforce write-path containment in commit_worktree.
    """

    golem_dir: Path
    project_root: Path
    worktree_path: Path | None = None  # only set for writer sessions
    session_id: str = ""
    agent_type: AgentType = "writer"


@dataclass
class RegisteredTool:
    """A tool definition before it is bound to a context.

    name: the bare tool name (SDK exposes it as mcp__golem__<name>)
    allowed_for: frozenset of agent types that may call this tool
    factory: callable(context) -> SdkMcpTool
    """

    name: str
    allowed_for: frozenset[AgentType]
    factory: Callable[[ToolContext], SdkMcpTool]


class ToolRegistry:
    """Central registry of all Golem MCP tools.

    Usage::

        registry = build_tool_registry(golem_dir, config, project_root, ...)
        ctx = ToolContext(golem_dir=golem_dir, project_root=project_root, agent_type="writer")
        tools = registry.get_tools_for_agent("writer", ctx)
        mcp_server = create_sdk_mcp_server("golem-junior-dev", tools=tools)
    """

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(self, tool: RegisteredTool) -> None:
        """Add a tool to the registry."""
        self._tools[tool.name] = tool

    def get_tools_for_agent(
        self,
        agent_type: AgentType,
        context: ToolContext,
    ) -> list[SdkMcpTool]:
        """Return bound SdkMcpTool instances for the given agent type.

        Only tools whose allowed_for set contains agent_type are included.
        Each tool factory is called with context at this point.
        """
        return [
            registered.factory(context)
            for registered in self._tools.values()
            if agent_type in registered.allowed_for
        ]

    def tool_names_for_agent(self, agent_type: AgentType) -> list[str]:
        """Return bare tool names available to an agent (for logging/docs)."""
        return [
            name
            for name, t in self._tools.items()
            if agent_type in t.allowed_for
        ]
