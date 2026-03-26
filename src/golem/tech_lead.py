from __future__ import annotations

from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, query

from golem.config import GolemConfig, sdk_env
from golem.tickets import TicketStore
from golem.tools import create_golem_mcp_server

_TECH_LEAD_PROMPT_TEMPLATE = Path(__file__).parent / "prompts" / "tech_lead.md"


async def run_tech_lead(
    ticket_id: str,
    golem_dir: Path,
    config: GolemConfig,
    project_root: Path,
) -> None:
    """Spawn persistent Tech Lead session that orchestrates writers and creates a PR.

    The Tech Lead reads plans, creates worktrees, spawns writer pairs, reviews work,
    merges branches, runs integration QA, and creates a PR. Blocks until complete.
    The SDK automatically routes all tool calls to the registered MCP server.
    """
    store = TicketStore(golem_dir / "tickets")
    ticket = await store.read(ticket_id)

    # Load spec content from plan_file context if available
    spec_content = ""
    plan_file = ticket.context.plan_file
    if plan_file and Path(plan_file).exists():
        spec_content = Path(plan_file).read_text(encoding="utf-8")

    template = _TECH_LEAD_PROMPT_TEMPLATE.read_text(encoding="utf-8")
    prompt = template.replace("{golem_dir}", str(golem_dir))
    prompt = prompt.replace("{spec_content}", spec_content)
    prompt = prompt.replace("{project_root}", str(project_root))

    # Build in-process MCP server with all orchestration tools registered
    mcp_server = create_golem_mcp_server(golem_dir, config, project_root)

    async for _message in query(
        prompt=prompt,
        options=ClaudeAgentOptions(
            model=config.tech_lead_model,
            cwd=str(project_root),
            tools={"type": "preset", "preset": "claude_code"},
            mcp_servers={"golem": mcp_server},
            max_turns=100,
            permission_mode="bypassPermissions",
            env=sdk_env(),
        ),
    ):
        pass  # SDK routes tool calls to MCP server automatically
