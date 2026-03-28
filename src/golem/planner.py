from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

from claude_agent_sdk import (
    CLIConnectionError,
    CLINotFoundError,
    ClaudeAgentOptions,
    ClaudeSDKError,
)

_MAX_RETRIES = 2

from typing import TYPE_CHECKING

from golem.config import GolemConfig, resolve_agent_options, sdk_env
from golem.progress import ProgressLogger
from golem.supervisor import SupervisedResult, build_escalated_prompt, stall_config_for_role, supervised_session
from golem.tickets import TicketStore
from golem.tools import create_golem_mcp_server

if TYPE_CHECKING:
    from golem.events import EventBus


@dataclass
class PlannerResult:
    ticket_id: str
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    num_turns: int = 0
    duration_ms: int = 0

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


async def _run_planner_session(
    prompt: str,
    golem_dir: Path,
    config: GolemConfig,
    cwd: Path,
    is_retry: bool = False,
    event_bus: EventBus | None = None,
    server_url: str = "",
) -> SupervisedResult:
    """Run a single planner supervised session."""
    # SSE MCP disabled for now — Claude CLI doesn't reliably connect to
    # SSE servers from --mcp-config inline JSON.  Keeping in-process MCP
    # until the SDK supports reliable SSE transport from subprocesses.
    # if server_url:
    #     from golem.tools import create_golem_mcp_sse_config
    #     mcp_server = create_golem_mcp_sse_config(config.session_id, server_url)
    # else:
    mcp_server = create_golem_mcp_server(golem_dir, config, cwd, event_bus=event_bus)
    sources, mcps = resolve_agent_options(config, "planner", mcp_server)

    options = ClaudeAgentOptions(
        model=config.planner_model,
        cwd=str(cwd),
        tools={"type": "preset", "preset": "claude_code"},
        mcp_servers=mcps,
        setting_sources=sources,
        max_turns=config.planner_max_turns,
        permission_mode="bypassPermissions",
        env=sdk_env(),
    )

    stall_cfg = stall_config_for_role("planner", config.planner_max_turns)

    def on_text(text: str) -> None:
        preview = text[:120].replace("\n", " ")
        print(f"[LEAD ARCHITECT] {preview}", file=sys.stderr)

    def on_tool(name: str) -> None:
        print(f"[LEAD ARCHITECT] tool: {name}(...)", file=sys.stderr)

    return await supervised_session(
        prompt=prompt,
        options=options,
        role="planner",
        config=config,
        stall_config=stall_cfg,
        on_text=on_text,
        on_tool=on_tool,
        golem_dir=golem_dir,
        event_bus=event_bus,
    )


async def run_planner(
    spec_path: Path,
    golem_dir: Path,
    config: GolemConfig,
    repo_root: Path | None = None,
    event_bus: EventBus | None = None,
    server_url: str = "",
) -> PlannerResult:
    """Spawn Opus planner session that writes plans/ + references/ and creates a ticket.

    Uses supervised_session() for stall detection. Retries up to 2 times on
    CLIConnectionError/ClaudeSDKError. On stall, retries with escalated prompt.
    Post-session: verifies overview.md exists with >3 lines and task-*.md files exist.
    If the planner doesn't call create_ticket via MCP, a self-healing fallback creates
    a ticket programmatically.

    Returns a PlannerResult with ticket_id and cost/token data.
    """
    try:
        spec_content = spec_path.read_text(encoding="utf-8")
    except PermissionError:
        raise RuntimeError(f"Cannot read spec file (permission denied): {spec_path}") from None
    except OSError as e:
        raise RuntimeError(f"Cannot read spec file: {spec_path} ({e})") from None

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
    original_prompt = template.replace("{spec_content}", spec_content)
    original_prompt = original_prompt.replace("{project_context}", project_context or "(none)")
    original_prompt = original_prompt.replace("{golem_dir}", str(golem_dir))
    infra_checks_str = "\n".join(f"- `{c}`" for c in config.infrastructure_checks) if config.infrastructure_checks else "(none detected)"
    original_prompt = original_prompt.replace("{infrastructure_checks}", infra_checks_str)

    progress = ProgressLogger(golem_dir)
    session_result: SupervisedResult | None = None

    from golem.recovery import RecoveryCoordinator, RecoveryExhausted

    coordinator = RecoveryCoordinator(config)
    try:
        session_result = await coordinator.run_with_recovery(
            session_fn=lambda: _run_planner_session(
                original_prompt, golem_dir, config, cwd,
                event_bus=event_bus, server_url=server_url,
            ),
            role="planner",
            label="planner",
            golem_dir=golem_dir,
            event_bus=event_bus,
        )
    except RecoveryExhausted as exc:
        raise RuntimeError(str(exc)) from exc

    if session_result is None:
        raise RuntimeError("Planner session produced no result")

    stall_cfg = stall_config_for_role("planner", config.planner_max_turns)

    # Handle stall: retry with escalated prompt
    if session_result.stalled:
        progress.log_stall_detected("planner", session_result.turns, config.planner_max_turns, session_result.registry.action_call_count())
        progress.log_stall_retry("planner")
        escalated = build_escalated_prompt(
            "planner", original_prompt, session_result.turns, stall_cfg.expected_actions
        )
        try:
            retry_result = await _run_planner_session(escalated, golem_dir, config, cwd, is_retry=True, event_bus=event_bus, server_url=server_url)
        except (CLIConnectionError, ClaudeSDKError) as e:
            raise RuntimeError(f"Planner retry failed: {e}") from None

        if retry_result.stalled:
            progress.log_stall_fatal("planner", retry_result.turns)
            raise RuntimeError(
                f"Planner stalled after retry -- {retry_result.turns} turns with no progress"
            )
        session_result = retry_result

    # Post-session content verification
    overview_path = golem_dir / "plans" / "overview.md"
    plans_dir = golem_dir / "plans"
    overview_ok = (
        overview_path.exists()
        and len(overview_path.read_text(encoding="utf-8").splitlines()) > 3
    )
    task_files = list(plans_dir.glob("task-*.md")) if plans_dir.exists() else []

    if not overview_ok or not task_files:
        # Verification failed: treat as stall and retry
        progress.log_stall_warning(
            "planner", session_result.turns, config.planner_max_turns, session_result.registry.action_call_count()
        )
        escalated = build_escalated_prompt(
            "planner", original_prompt, session_result.turns, stall_cfg.expected_actions
        )
        try:
            retry_result = await _run_planner_session(escalated, golem_dir, config, cwd, is_retry=True, event_bus=event_bus, server_url=server_url)
        except (CLIConnectionError, ClaudeSDKError) as e:
            raise RuntimeError(f"Planner verification-retry failed: {e}") from None

        if retry_result.stalled:
            progress.log_stall_fatal("planner", retry_result.turns)
            raise RuntimeError("Planner produced no valid plan after retry")
        session_result = retry_result

    # Verify overview.md was created (original check preserved)
    if not overview_path.exists():
        raise RuntimeError(
            f"Planner did not create plans/overview.md at {overview_path}. "
            "Check planner session output for errors."
        )

    # Find the most recently created ticket, or self-heal by creating one
    store = TicketStore(golem_dir / "tickets")
    all_tickets = await store.list_tickets()
    if not all_tickets:
        print("[LEAD ARCHITECT] Warning: planner did not call create_ticket -- creating fallback ticket", file=sys.stderr)
        progress.log_stall_warning(
            "planner", session_result.turns, config.planner_max_turns, session_result.registry.action_call_count()
        )
        # Self-heal: create the ticket the planner should have created
        overview = golem_dir / "plans" / "overview.md"
        blueprint = overview.read_text(encoding="utf-8")[:500] if overview.exists() else ""
        plan_files = sorted((golem_dir / "plans").glob("task-*.md"))
        from golem.tickets import Ticket, TicketContext

        ticket = Ticket(
            id="",
            type="task",
            title=f"Tech Lead: Execute {overview}",
            status="pending",
            priority="high",
            created_by="planner-fallback",
            assigned_to="tech_lead",
            context=TicketContext(
                plan_file=str(overview),
                references=[str(p) for p in plan_files],
                blueprint=blueprint,
                acceptance=[
                    "All tasks completed",
                    "All QA checks pass",
                    "PR created",
                    f"Task files: {len(plan_files)}",
                ],
            ),
            history=[],
        )
        ticket_id = await store.create(ticket)
        progress.log_agent_cost(
            role="lead_architect",
            cost_usd=session_result.cost_usd,
            input_tokens=session_result.input_tokens,
            output_tokens=session_result.output_tokens,
            cache_read=0,
            turns=session_result.turns,
            duration_s=int(session_result.duration_s),
        )
        return PlannerResult(
            ticket_id=ticket_id,
            cost_usd=session_result.cost_usd,
            input_tokens=session_result.input_tokens,
            output_tokens=session_result.output_tokens,
            cache_read_tokens=0,
            num_turns=session_result.turns,
            duration_ms=int(session_result.duration_s * 1000),
        )

    # Return the last ticket created (by ID sort -- TICKET-001, TICKET-002, etc.)
    last_ticket = sorted(all_tickets, key=lambda t: t.id)[-1]
    progress.log_agent_cost(
        role="lead_architect",
        cost_usd=session_result.cost_usd,
        input_tokens=session_result.input_tokens,
        output_tokens=session_result.output_tokens,
        cache_read=0,
        turns=session_result.turns,
        duration_s=int(session_result.duration_s),
    )
    return PlannerResult(
        ticket_id=last_ticket.id,
        cost_usd=session_result.cost_usd,
        input_tokens=session_result.input_tokens,
        output_tokens=session_result.output_tokens,
        cache_read_tokens=0,
        num_turns=session_result.turns,
        duration_ms=int(session_result.duration_s * 1000),
    )
