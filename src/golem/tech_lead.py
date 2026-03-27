from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_MAX_RETRIES = 2
_RETRY_DELAY_S = 10

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

from golem.config import GolemConfig, resolve_agent_options, sdk_env
from golem.tickets import TicketStore
from golem.tools import create_golem_mcp_server
from golem.worktree import delete_worktree, merge_group_branches

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
    checkout_result = _git("checkout", "main")
    if checkout_result.returncode != 0:
        # Fallback: try 'master' or detect default branch
        checkout_result = _git("checkout", "master")
        if checkout_result.returncode != 0:
            print("[TECH LEAD] Warning: could not checkout main or master branch", file=sys.stderr)
            return

    for branch in integration_branches:
        # Check if branch is already merged into main
        merge_check = _git("merge-base", "--is-ancestor", branch, "main")
        if merge_check.returncode == 0:
            continue  # Already merged

        # Skip branches with no actual changes (diff against HEAD which is now on main/master)
        diff_stat = _git("diff", "--stat", f"HEAD...{branch}")
        if not diff_stat.stdout.strip():
            print(f"[TECH LEAD] Skipping {branch} — no changes", file=sys.stderr)
            continue

        print(f"[TECH LEAD] Self-healing: merging {branch} into main", file=sys.stderr)
        merge_result = _git("merge", branch, "--ff-only")
        if merge_result.returncode != 0:
            # ff-only failed, try regular merge
            merge_result = _git("merge", "--no-ff", "-m", f"feat: merge {branch} (golem self-heal)", branch)
            if merge_result.returncode != 0:
                print(f"[TECH LEAD] Warning: could not merge {branch} into main: {merge_result.stderr}", file=sys.stderr)
                _git("merge", "--abort")


def _cleanup_golem_worktrees(golem_dir: Path, project_root: Path) -> None:
    """Remove any golem worktrees created during a failed Tech Lead session."""
    worktrees_dir = golem_dir / "worktrees"
    if not worktrees_dir.exists():
        return
    for wt in worktrees_dir.iterdir():
        if wt.is_dir():
            try:
                delete_worktree(wt, project_root)
                print(f"[TECH LEAD] Cleaned up worktree: {wt.name}", file=sys.stderr)
            except Exception as cleanup_err:
                print(f"[TECH LEAD] Warning: could not clean worktree {wt.name}: {cleanup_err}", file=sys.stderr)


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

    Self-healing: _ensure_merged_to_main() runs after the session to merge any
    integration branches the Tech Lead didn't merge. _cleanup_golem_worktrees()
    removes orphaned worktrees on session failure. Retries up to 2 times on
    CLIConnectionError/ClaudeSDKError with configurable delay.
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
    sources, mcps = resolve_agent_options(config, "tech_lead", mcp_server)
    _session_failed = False
    last_error: Exception | None = None

    for attempt in range(_MAX_RETRIES + 1):
        try:
            async for message in query(
                prompt=prompt,
                options=ClaudeAgentOptions(
                    model=config.tech_lead_model,
                    cwd=str(project_root),
                    tools={"type": "preset", "preset": "claude_code"},
                    mcp_servers=mcps,
                    setting_sources=sources,
                    max_turns=config.max_tech_lead_turns,
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
            break  # Success — exit retry loop
        except CLINotFoundError:
            _session_failed = True
            _cleanup_golem_worktrees(golem_dir, project_root)
            raise RuntimeError(
                "Tech Lead failed: 'claude' CLI not found on PATH. Run 'claude login' to install and authenticate."
            ) from None
        except (CLIConnectionError, ClaudeSDKError) as e:
            last_error = e
            if attempt < _MAX_RETRIES:
                print(
                    f"[TECH LEAD] Attempt {attempt + 1} failed ({type(e).__name__}), retrying in {config.retry_delay}s...",
                    file=sys.stderr,
                )
                await asyncio.sleep(config.retry_delay)
            else:
                _session_failed = True
                _cleanup_golem_worktrees(golem_dir, project_root)
                raise RuntimeError(
                    f"Tech Lead failed after {_MAX_RETRIES + 1} attempts. Last error: {last_error}"
                ) from None

    # Self-heal: if integration branches exist but weren't merged to main, merge them
    _ensure_merged_to_main(project_root)
