from __future__ import annotations

from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, query

from golem.config import GolemConfig, sdk_env
from golem.tickets import TicketStore
from golem.tools import create_golem_mcp_server

_PLANNER_PROMPT_TEMPLATE = Path(__file__).parent / "prompts" / "planner.md"

# Monkey-patch SDK initialize timeout from 60s to 180s for long-running planner sessions.
# The SDK hardcodes this value with no public API; we patch at import time.
try:
    from claude_agent_sdk._internal.client import Query  # type: ignore[import]

    _orig_defaults = list(Query.__init__.__defaults__ or ())
    _new_defaults = tuple(180 if v == 60 else v for v in _orig_defaults)
    if _new_defaults != tuple(_orig_defaults):
        Query.__init__.__defaults__ = _new_defaults
except Exception:
    pass


async def run_planner(
    spec_path: Path,
    golem_dir: Path,
    config: GolemConfig,
    repo_root: Path | None = None,
) -> str:
    """Spawn Opus planner session that writes plans/ + references/ and creates a ticket.

    Returns the ticket_id string created by the planner.
    """
    spec_content = spec_path.read_text(encoding="utf-8")

    # Gather project context
    project_context = ""
    cwd = repo_root or spec_path.parent
    for name in ("CLAUDE.md", "README.md", "README"):
        candidate = cwd / name
        if candidate.exists():
            project_context += f"## {name}\n{candidate.read_text(encoding='utf-8')[:4000]}\n\n"
            break

    # Create required directories
    (golem_dir / "research").mkdir(parents=True, exist_ok=True)
    (golem_dir / "plans").mkdir(parents=True, exist_ok=True)
    (golem_dir / "references").mkdir(parents=True, exist_ok=True)

    template = _PLANNER_PROMPT_TEMPLATE.read_text(encoding="utf-8")
    prompt = template.replace("{spec_content}", spec_content)
    prompt = prompt.replace("{project_context}", project_context or "(none)")
    prompt = prompt.replace("{golem_dir}", str(golem_dir))

    # Build in-process MCP server with ticket tools registered
    mcp_server = create_golem_mcp_server(golem_dir, config, cwd)

    async for _message in query(
        prompt=prompt,
        options=ClaudeAgentOptions(
            model=config.planner_model,
            cwd=str(cwd),
            tools={"type": "preset", "preset": "claude_code"},
            mcp_servers={"golem": mcp_server},
            max_turns=50,
            permission_mode="bypassPermissions",
            env=sdk_env(),
        ),
    ):
        pass  # SDK routes tool calls to MCP server automatically

    # Verify plans/overview.md was created
    overview_path = golem_dir / "plans" / "overview.md"
    if not overview_path.exists():
        raise RuntimeError(
            f"Planner did not create plans/overview.md at {overview_path}. "
            "Check planner session output for errors."
        )

    # Find the most recently created ticket
    store = TicketStore(golem_dir / "tickets")
    all_tickets = await store.list_tickets()
    if not all_tickets:
        raise RuntimeError("Planner did not create any tickets via create_ticket tool.")

    # Return the last ticket created (by ID sort — TICKET-001, TICKET-002, etc.)
    last_ticket = sorted(all_tickets, key=lambda t: t.id)[-1]
    return last_ticket.id
