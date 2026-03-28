from __future__ import annotations

import asyncio
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

_MAX_RETRIES = 2

from claude_agent_sdk import (
    CLIConnectionError,
    CLINotFoundError,
    ClaudeAgentOptions,
    ClaudeSDKError,
)

from typing import TYPE_CHECKING

from golem.config import GolemConfig, resolve_agent_options, sdk_env
from golem.progress import ProgressLogger
from golem.supervisor import ContinuationResult, build_escalated_prompt, continuation_supervised_session, stall_config_for_role
from golem.tickets import TicketStore
from golem.tools import create_golem_mcp_server
from golem.worktree import delete_worktree, merge_group_branches

if TYPE_CHECKING:
    from golem.events import EventBus


@dataclass
class TechLeadResult:
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    num_turns: int = 0
    duration_ms: int = 0

_TECH_LEAD_PROMPT_TEMPLATE = Path(__file__).parent / "prompts" / "tech_lead.md"


def _ensure_merged_to_main(project_root: Path, branch_prefix: str = "golem") -> None:
    """Self-healing: merge any golem integration branches into main if the Tech Lead didn't."""

    def _git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args], cwd=project_root, capture_output=True, text=True, encoding="utf-8"
        )

    # Find golem integration branches
    result = _git("branch", "--list", f"{branch_prefix}/*/integration")
    integration_branches = [b.strip().lstrip("* ") for b in result.stdout.splitlines() if b.strip()]
    if not integration_branches:
        return

    # Check if main already has the integration commits
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
            print(f"[TECH LEAD] Skipping {branch} -- no changes", file=sys.stderr)
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


def _check_integration_commits(project_root: Path) -> bool:
    """Return True if at least one golem integration branch has commits beyond main."""
    result = subprocess.run(
        ["git", "branch", "--list", "golem/*/integration"],
        cwd=project_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    branches = [b.strip().lstrip("* ") for b in result.stdout.splitlines() if b.strip()]
    if not branches:
        return False

    for branch in branches:
        log_result = subprocess.run(
            ["git", "log", "--oneline", branch, "--not", "main"],
            cwd=project_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if log_result.stdout.strip():
            return True
    return False


async def run_tech_lead(
    ticket_id: str,
    golem_dir: Path,
    config: GolemConfig,
    project_root: Path,
    event_bus: EventBus | None = None,
    server_url: str = "",
) -> TechLeadResult:
    """Spawn persistent Tech Lead session that orchestrates junior devs and creates a PR.

    The Tech Lead reads plans, creates worktrees, spawns junior dev pairs, reviews work,
    merges branches, runs integration QA, and creates a PR. Blocks until complete.
    Uses supervised_session() for stall detection and auto-retry with escalated prompts.

    Self-healing: _ensure_merged_to_main() runs after the session to merge any
    integration branches the Tech Lead didn't merge. _cleanup_golem_worktrees()
    removes orphaned worktrees on session failure.
    """
    store = TicketStore(golem_dir / "tickets")
    ticket = await store.read(ticket_id)

    # Load spec content from plan_file context if available
    spec_content = ""
    plan_file = ticket.context.plan_file
    if plan_file and Path(plan_file).exists():
        spec_content = Path(plan_file).read_text(encoding="utf-8")

    template = _TECH_LEAD_PROMPT_TEMPLATE.read_text(encoding="utf-8")
    original_prompt = template.replace("{golem_dir}", str(golem_dir))
    original_prompt = original_prompt.replace("{spec_content}", spec_content)
    original_prompt = original_prompt.replace("{project_root}", str(project_root))

    # SSE MCP disabled — see planner.py comment for rationale
    mcp_server = create_golem_mcp_server(golem_dir, config, project_root, event_bus=event_bus)
    sources, mcps = resolve_agent_options(config, "tech_lead", mcp_server)

    options = ClaudeAgentOptions(
        model=config.tech_lead_model,
        cwd=str(project_root),
        tools={"type": "preset", "preset": "claude_code"},
        mcp_servers=mcps,
        setting_sources=sources,
        max_turns=config.max_tech_lead_turns,
        permission_mode="bypassPermissions",
        env=sdk_env(),
    )

    stall_cfg = stall_config_for_role("tech_lead", config.max_tech_lead_turns)

    def on_text(text: str) -> None:
        preview = text[:120].replace("\n", " ")
        print(f"[TECH LEAD] {preview}", file=sys.stderr)

    def on_tool(name: str) -> None:
        print(f"[TECH LEAD] tool: {name}(...)", file=sys.stderr)

    progress = ProgressLogger(golem_dir)
    session_result: ContinuationResult | None = None

    from golem.recovery import RecoveryCoordinator, RecoveryExhausted

    coordinator = RecoveryCoordinator(config)
    try:
        session_result = await coordinator.run_with_recovery(
            session_fn=lambda: continuation_supervised_session(
                prompt=original_prompt,
                options=options,
                role="tech_lead",
                config=config,
                stall_config=stall_cfg,
                on_text=on_text,
                on_tool=on_tool,
                golem_dir=golem_dir,
                event_bus=event_bus,
            ),
            role="tech_lead",
            label=ticket_id,
            golem_dir=golem_dir,
            event_bus=event_bus,
        )
    except RecoveryExhausted as exc:
        _cleanup_golem_worktrees(golem_dir, project_root)
        raise RuntimeError(str(exc)) from exc

    if session_result is None:
        raise RuntimeError("Tech Lead session produced no result")

    # Handle stall: retry with escalated prompt
    if session_result.stalled:
        progress.log_stall_detected("tech_lead", session_result.turns, config.max_tech_lead_turns, session_result.registry.action_call_count())
        progress.log_stall_retry("tech_lead")
        escalated = build_escalated_prompt(
            "tech_lead", original_prompt, session_result.turns, stall_cfg.expected_actions
        )
        try:
            retry_result = await continuation_supervised_session(
                prompt=escalated,
                options=options,
                role="tech_lead",
                config=config,
                stall_config=stall_cfg,
                on_text=on_text,
                on_tool=on_tool,
                golem_dir=golem_dir,
                event_bus=event_bus,
            )
        except (CLIConnectionError, ClaudeSDKError) as e:
            _cleanup_golem_worktrees(golem_dir, project_root)
            raise RuntimeError(f"Tech Lead retry failed: {e}") from None

        if retry_result.stalled:
            progress.log_stall_fatal("tech_lead", retry_result.turns)
            _cleanup_golem_worktrees(golem_dir, project_root)
            raise RuntimeError(
                f"Tech Lead stalled after retry — {retry_result.turns} turns with no progress"
            )
        session_result = retry_result

    # Post-session verification: check for commits on integration branch beyond main
    if not _check_integration_commits(project_root):
        # No commits produced — treat as stall and retry with escalated prompt
        progress.log_stall_warning(
            "tech_lead", session_result.turns, config.max_tech_lead_turns, session_result.registry.action_call_count()
        )
        escalated = build_escalated_prompt(
            "tech_lead", original_prompt, session_result.turns, stall_cfg.expected_actions
        )
        try:
            retry_result = await continuation_supervised_session(
                prompt=escalated,
                options=options,
                role="tech_lead",
                config=config,
                stall_config=stall_cfg,
                on_text=on_text,
                on_tool=on_tool,
                golem_dir=golem_dir,
                event_bus=event_bus,
            )
        except (CLIConnectionError, ClaudeSDKError) as e:
            _cleanup_golem_worktrees(golem_dir, project_root)
            raise RuntimeError(f"Tech Lead no-commits retry failed: {e}") from None

        if retry_result.stalled:
            progress.log_stall_fatal("tech_lead", retry_result.turns)
            _cleanup_golem_worktrees(golem_dir, project_root)
            raise RuntimeError(
                "Tech Lead produced no commits after retry"
            )
        session_result = retry_result

    progress.log_agent_cost(
        role="tech_lead",
        cost_usd=session_result.cost_usd,
        input_tokens=session_result.input_tokens,
        output_tokens=session_result.output_tokens,
        cache_read=0,
        turns=session_result.turns,
        duration_s=int(session_result.duration_s),
    )

    # Self-heal: if integration branches exist but weren't merged to main, merge them
    branch_prefix = f"golem/{config.session_id}" if config.session_id else "golem"
    _ensure_merged_to_main(project_root, branch_prefix=branch_prefix)

    return TechLeadResult(
        cost_usd=session_result.cost_usd,
        input_tokens=session_result.input_tokens,
        output_tokens=session_result.output_tokens,
        cache_read_tokens=0,
        num_turns=session_result.turns,
        duration_ms=int(session_result.duration_s * 1000),
    )
