from __future__ import annotations

import sys
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    CLIConnectionError,
    CLINotFoundError,
    ClaudeAgentOptions,
    ClaudeSDKError,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

from golem.config import GolemConfig, sdk_env
from golem.tickets import TicketStore
from golem.tools import create_golem_mcp_server
from golem.worktree import merge_group_branches

_TECH_LEAD_PROMPT_TEMPLATE = Path(__file__).parent / "prompts" / "tech_lead.md"


def _ensure_merged_to_main(project_root: Path) -> None:
    """Self-healing: merge any golem integration branches into main if the Tech Lead didn't."""
    import subprocess

    def _git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args], cwd=project_root, capture_output=True, text=True, encoding="utf-8"
        )

    # Find golem integration branches
    result = _git("branch", "--list", "golem/*/integration")
    integration_branches = [b.strip().lstrip("* ") for b in result.stdout.splitlines() if b.strip()]
    if not integration_branches:
        return

    # Check if main already has the integration commits
    current = _git("rev-parse", "HEAD")
    _git("checkout", "main")

    for branch in integration_branches:
        # Check if branch is already merged into main
        merge_check = _git("merge-base", "--is-ancestor", branch, "main")
        if merge_check.returncode == 0:
            continue  # Already merged

        print(f"[TECH LEAD] Self-healing: merging {branch} into main", file=sys.stderr)
        merge_result = _git("merge", branch, "--ff-only")
        if merge_result.returncode != 0:
            # ff-only failed, try regular merge
            merge_result = _git("merge", "--no-ff", "-m", f"feat: merge {branch} (golem self-heal)", branch)
            if merge_result.returncode != 0:
                print(f"[TECH LEAD] Warning: could not merge {branch} into main: {merge_result.stderr}", file=sys.stderr)
                _git("merge", "--abort")


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

    try:
        async for message in query(
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
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        preview = block.text[:120].replace("\n", " ")
                        print(f"[TECH LEAD] {preview}", file=sys.stderr)
                    elif isinstance(block, ToolUseBlock):
                        print(f"[TECH LEAD] tool: {block.name}({', '.join(f'{k}=' for k in list(block.input.keys())[:3])})", file=sys.stderr)
            elif isinstance(message, ResultMessage) and message.result:
                preview = message.result[:120].replace("\n", " ")
                print(f"[TECH LEAD] result: {preview}", file=sys.stderr)
    except CLINotFoundError:
        raise RuntimeError(
            "Tech Lead failed: 'claude' CLI not found on PATH. Run 'claude login' to install and authenticate."
        ) from None
    except CLIConnectionError as e:
        raise RuntimeError(
            f"Tech Lead failed: could not connect to Claude CLI. Check your auth with 'claude login'. Detail: {e}"
        ) from None
    except ClaudeSDKError as e:
        raise RuntimeError(f"Tech Lead failed: SDK error during orchestration session. Detail: {e}") from None

    # Self-heal: if integration branches exist but weren't merged to main, merge them
    _ensure_merged_to_main(project_root)
