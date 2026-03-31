from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path

from claude_agent_sdk import (
    CLIConnectionError,
    CLINotFoundError,
    ClaudeAgentOptions,
    ClaudeSDKError,
)

from typing import TYPE_CHECKING

from golem.config import GolemConfig, resolve_agent_options, sdk_env
from golem.progress import ProgressLogger
from golem.supervisor import ContinuationResult, _build_agent_hooks, build_escalated_prompt, continuation_supervised_session, stall_config_for_role
from golem.tickets import TicketStore
from golem.tools import create_golem_planner_mcp_server

if TYPE_CHECKING:
    from golem.events import EventBus


@dataclass
class PlannerResult:
    ticket_ids: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    num_turns: int = 0
    duration_ms: int = 0

    @property
    def ticket_id(self) -> str:
        """Backward-compat property: returns the last ticket ID (summary/integration ticket)."""
        return self.ticket_ids[-1] if self.ticket_ids else ""

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

# Monkey-patch SDK to prevent premature stdin closure for MCP sessions.
#
# Root cause of "Stream closed" MCP errors:
#   process_query() for string prompts calls wait_for_result_and_end_input() which
#   waits for the FIRST {"type": "result"} message, then calls transport.end_input()
#   to close stdin permanently.  In multi-agent sessions (planner with Explorer/Researcher
#   sub-agents, tech lead with Junior Dev/QA sub-agents), the CLI emits intermediate
#   {"type": "result"} messages for every sub-agent completion — not just the final one.
#   The first sub-agent result fires _first_result_event, end_input() closes stdin, but
#   the parent agent keeps running and subsequently calls MCP tools (create_ticket, etc.).
#   Those MCP control_requests arrive from CLI stdout, but _handle_control_request tries
#   to write responses back over stdin — which is already closed.  CLIConnectionError is
#   raised, the error response write also fails, and the anyio task-group dies.
#
#   The _stream_close_timeout approach (setting it to 24h) does NOT work because
#   _first_result_event is an anyio.Event — once set by any sub-agent result, await
#   returns immediately regardless of timeout.  The timeout only matters if the event
#   is never set.
#
# Fix: override wait_for_result_and_end_input to be a no-op when sdk_mcp_servers are
#   registered.  Stdin stays open for the full session and closes naturally when the
#   CLI process exits.  This is safe because the only purpose of end_input() is to
#   signal "no more user messages" — but for string prompts the user message was already
#   written, and MCP responses flow over stdin for the rest of the session.
try:
    from claude_agent_sdk._internal.client import Query as _Query  # type: ignore[import]

    _orig_wait_for_result = _Query.wait_for_result_and_end_input

    async def _patched_wait_for_result(self) -> None:  # type: ignore[no-untyped-def]
        if getattr(self, "sdk_mcp_servers", None):
            return  # Keep stdin open — MCP responses need it for the full session
        await _orig_wait_for_result(self)

    _Query.wait_for_result_and_end_input = _patched_wait_for_result  # type: ignore[method-assign]
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
) -> ContinuationResult:
    """Run a single planner supervised session."""
    mcp_server = create_golem_planner_mcp_server(golem_dir, config, cwd, event_bus=event_bus)
    sources, mcps = resolve_agent_options(config, "planner", mcp_server)

    options = ClaudeAgentOptions(
        model=config.planner_model,
        cwd=str(cwd),
        tools={"type": "preset", "preset": "claude_code"},
        mcp_servers=mcps,
        setting_sources=sources,
        max_turns=config.planner_max_turns,
        permission_mode="bypassPermissions",
        env=sdk_env(session_id=config.session_id, golem_dir=str(golem_dir)),
        max_budget_usd=config.planner_budget_usd,
        fallback_model=config.fallback_model,
        hooks=_build_agent_hooks(),
    )

    stall_cfg = stall_config_for_role("planner", config.planner_max_turns, skip_research=config.skip_research)

    def on_text(text: str) -> None:
        preview = text[:120].replace("\n", " ")
        print(f"[LEAD ARCHITECT] {preview}", file=sys.stderr)

    def on_tool(name: str) -> None:
        print(f"[LEAD ARCHITECT] tool: {name}(...)", file=sys.stderr)

    return await continuation_supervised_session(
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
    if config.skip_research:
        skip_msg = (
            "RESEARCH SUB-AGENTS DISABLED for this tier. "
            "Do NOT spawn Explorer or Researcher sub-agents. "
            "Read the codebase directly with your own tools and produce the plan in one pass."
        )
    else:
        skip_msg = ""
    original_prompt = original_prompt.replace("{skip_research_instruction}", skip_msg)

    # Extract edict_id from golem_dir path convention: .golem/edicts/EDICT-001 → "EDICT-001"
    edict_id = golem_dir.name if golem_dir.parent.name == "edicts" else ""
    original_prompt = original_prompt.replace("{edict_id}", edict_id)

    progress = ProgressLogger(golem_dir)
    session_result: ContinuationResult | None = None

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
            edict_id=edict_id,
            event_bus=event_bus,
        )
    except RecoveryExhausted as exc:
        raise RuntimeError(str(exc)) from exc

    if session_result is None:
        raise RuntimeError("Planner session produced no result")

    stall_cfg = stall_config_for_role("planner", config.planner_max_turns, skip_research=config.skip_research)

    # Handle stall: retry with escalated prompt (wrapped in RecoveryCoordinator for consistent error handling)
    if session_result.stalled:
        progress.log_stall_detected("planner", session_result.turns, config.planner_max_turns, session_result.registry.action_call_count())
        progress.log_stall_retry("planner")
        escalated = build_escalated_prompt(
            "planner", original_prompt, session_result.turns, stall_cfg.expected_actions
        )
        stall_coordinator = RecoveryCoordinator(config)
        try:
            retry_result = await stall_coordinator.run_with_recovery(
                session_fn=lambda: _run_planner_session(
                    escalated, golem_dir, config, cwd,
                    is_retry=True, event_bus=event_bus, server_url=server_url,
                ),
                role="planner",
                label="planner-stall-retry",
                golem_dir=golem_dir,
                edict_id=edict_id,
                event_bus=event_bus,
            )
        except RecoveryExhausted as e:
            raise RuntimeError(f"Planner retry failed: {e}") from None

        if retry_result.stalled:
            progress.log_stall_fatal("planner", retry_result.turns)
            # Don't raise — fall through to content verification and fallback
            # ticket creation. The planner may have written plan files even
            # though it never called create_ticket.
            print(
                f"[LEAD ARCHITECT] Planner stalled after retry ({retry_result.turns} turns) "
                "-- attempting fallback ticket creation",
                file=sys.stderr,
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
        verify_coordinator = RecoveryCoordinator(config)
        try:
            retry_result = await verify_coordinator.run_with_recovery(
                session_fn=lambda: _run_planner_session(
                    escalated, golem_dir, config, cwd,
                    is_retry=True, event_bus=event_bus, server_url=server_url,
                ),
                role="planner",
                label="planner-verify-retry",
                golem_dir=golem_dir,
                edict_id=edict_id,
                event_bus=event_bus,
            )
        except RecoveryExhausted as e:
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
        _cache_read = getattr(session_result, "cache_read_tokens", 0)
        progress.log_agent_cost(
            role="lead_architect",
            cost_usd=session_result.cost_usd,
            input_tokens=session_result.input_tokens,
            output_tokens=session_result.output_tokens,
            cache_read=_cache_read,
            turns=session_result.turns,
            duration_s=int(session_result.duration_s),
        )
        return PlannerResult(
            ticket_ids=[ticket_id],
            cost_usd=session_result.cost_usd,
            input_tokens=session_result.input_tokens,
            output_tokens=session_result.output_tokens,
            cache_read_tokens=_cache_read,
            num_turns=session_result.turns,
            duration_ms=int(session_result.duration_s * 1000),
        )

    # Return all tickets created (sorted by ID -- TICKET-001, TICKET-002, etc.)
    sorted_tickets = sorted(all_tickets, key=lambda t: t.id)
    _cache_read = getattr(session_result, "cache_read_tokens", 0)
    progress.log_agent_cost(
        role="lead_architect",
        cost_usd=session_result.cost_usd,
        input_tokens=session_result.input_tokens,
        output_tokens=session_result.output_tokens,
        cache_read=_cache_read,
        turns=session_result.turns,
        duration_s=int(session_result.duration_s),
    )
    return PlannerResult(
        ticket_ids=[t.id for t in sorted_tickets],
        cost_usd=session_result.cost_usd,
        input_tokens=session_result.input_tokens,
        output_tokens=session_result.output_tokens,
        cache_read_tokens=_cache_read,
        num_turns=session_result.turns,
        duration_ms=int(session_result.duration_s * 1000),
    )
