from __future__ import annotations

import asyncio
import os
import random
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

_MAX_RETRIES = 2
_RETRY_DELAY_S = 10

from claude_agent_sdk import (
    CLIConnectionError,
    CLINotFoundError,
    ClaudeAgentOptions,
    ClaudeSDKError,
)

from typing import TYPE_CHECKING

from golem.config import GolemConfig, resolve_agent_options, sdk_env
from golem.progress import ProgressLogger
from golem.supervisor import StallConfig, SupervisedResult, build_escalated_prompt, stall_config_for_role, supervised_session
from golem.tickets import Ticket
from golem.tools import create_junior_dev_mcp_server

if TYPE_CHECKING:
    from golem.events import EventBus


@dataclass
class JuniorDevResult:
    result_text: str = ""
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    num_turns: int = 0
    duration_ms: int = 0


# Backward-compatible alias
WriterResult = JuniorDevResult

_JUNIOR_DEV_PROMPT_TEMPLATE = Path(__file__).parent / "prompts" / "junior_dev.md"


def _get_rework_info(ticket: Ticket) -> tuple[int, list[str]]:
    """Count needs_work events and extract rejection notes from ticket history."""
    rework_count = 0
    rework_notes: list[str] = []
    for event in ticket.history:
        if "needs_work" in (event.action or "").lower() or (
            event.note and "needs_work" in event.note.lower()
        ):
            rework_count += 1
            if event.note:
                rework_notes.append(event.note)
    return rework_count, rework_notes


def build_writer_prompt(ticket: Ticket, rework_count: int = 0, rework_notes: list[str] | None = None) -> str:
    """Build junior dev prompt from ticket context, with optional rework context."""
    template_name = "junior_dev_rework.md" if rework_count > 0 else "junior_dev.md"
    template_path = Path(__file__).parent / "prompts" / template_name
    # Fall back to junior_dev.md if rework template doesn't exist yet
    if not template_path.exists():
        template_path = _JUNIOR_DEV_PROMPT_TEMPLATE
    template = template_path.read_text(encoding="utf-8")
    ctx = ticket.context

    # Build substitution values
    ticket_context = f"Ticket ID: {ticket.id}\nTitle: {ticket.title}\nType: {ticket.type}\nPriority: {ticket.priority}"

    plan_section = ""
    if ctx.plan_file and Path(ctx.plan_file).exists():
        plan_section = Path(ctx.plan_file).read_text(encoding="utf-8")

    file_contents = ""
    if ctx.files:
        parts: list[str] = []
        for filename, contents in ctx.files.items():
            parts.append(f"### {filename}\n```\n{contents}\n```")
        file_contents = "\n\n".join(parts)

    references = "\n".join(ctx.references) if ctx.references else ""
    blueprint = ctx.blueprint
    acceptance = "\n".join(f"- {a}" for a in ctx.acceptance) if ctx.acceptance else ""
    qa_checks = "\n".join(f"- `{q}`" for q in ctx.qa_checks) if ctx.qa_checks else ""
    parallelism_hints = "\n".join(f"- {h}" for h in ctx.parallelism_hints) if ctx.parallelism_hints else ""

    # Build rework context string
    rework_context = ""
    if rework_count > 0 and rework_notes:
        rework_context = "## Previous Rejection Feedback\n\n"
        for i, note in enumerate(rework_notes[-3:], 1):  # Last 3 rejections
            rework_context += f"### Attempt {i} Feedback\n{note}\n\n"
        rework_context += (
            f"This is attempt {rework_count + 1}. "
            "Address ALL previous feedback before submitting.\n"
        )

    replacements = {
        "ticket_context": ticket_context,
        "plan_section": plan_section,
        "file_contents": file_contents,
        "references": references,
        "blueprint": blueprint,
        "acceptance": acceptance,
        "qa_checks": qa_checks,
        "parallelism_hints": parallelism_hints,
        "iteration": str(rework_count + 1),
        "rework_context": rework_context,
    }

    prompt = template
    for key, value in replacements.items():
        placeholder = "{" + key + "}"
        prompt = prompt.replace(placeholder, value)

    return prompt


async def spawn_junior_dev(
    ticket: Ticket,
    worktree_path: str,
    config: GolemConfig,
    golem_dir: Path | None = None,
    event_bus: EventBus | None = None,
    server_url: str = "",
) -> JuniorDevResult:
    """Spawn a junior dev SDK session for the given ticket in the worktree.

    Uses supervised_session() for stall detection. On stall, retries with an
    escalated prompt. On double stall, marks ticket as failed and raises RuntimeError.
    Post-session: verifies git diff shows changed files in the worktree.

    Returns a JuniorDevResult with result text and cost/token data.
    """
    rework_count, rework_notes = _get_rework_info(ticket)
    original_prompt = build_writer_prompt(ticket, rework_count=rework_count, rework_notes=rework_notes)

    # Stagger parallel junior dev spawns to reduce I/O contention on uv cache
    jitter = config.dispatch_jitter_max
    if jitter > 0 and os.environ.get("GOLEM_TEST_MODE") != "1":
        delay = random.uniform(0, jitter)
        print(f"[JUNIOR DEV] {ticket.id}: jitter delay {delay:.1f}s", file=sys.stderr)
        await asyncio.sleep(delay)

    # SSE MCP disabled — see planner.py comment for rationale
    mcp_server = create_junior_dev_mcp_server(golem_dir, event_bus=event_bus) if golem_dir else create_junior_dev_mcp_server(Path(worktree_path), event_bus=event_bus)
    sources, mcps = resolve_agent_options(
        config, "writer", mcp_server, golem_mcp_name="golem-junior-dev",
    )

    options = ClaudeAgentOptions(
        model=config.worker_model,
        cwd=worktree_path,
        tools={"type": "preset", "preset": "claude_code"},
        mcp_servers=mcps,
        setting_sources=sources,
        max_turns=config.max_worker_turns,
        permission_mode="bypassPermissions",
        env=sdk_env(),
    )

    stall_cfg = stall_config_for_role("junior_dev", config.max_worker_turns)

    def on_text(text: str) -> None:
        preview = text[:120].replace("\n", " ")
        print(f"[JUNIOR DEV] {preview}", file=sys.stderr)

    def on_tool(name: str) -> None:
        print(f"[JUNIOR DEV] tool: {name}", file=sys.stderr)

    last_error: Exception | None = None
    session_result: SupervisedResult | None = None

    for attempt in range(_MAX_RETRIES + 1):
        try:
            prompt = original_prompt if attempt == 0 else build_writer_prompt(ticket, rework_count=rework_count, rework_notes=rework_notes)

            session_result = await supervised_session(
                prompt=prompt,
                options=options,
                role="junior_dev",
                config=config,
                stall_config=stall_cfg,
                on_text=on_text,
                on_tool=on_tool,
                golem_dir=golem_dir,
                event_bus=event_bus,
            )
            break  # Success (or stall handled internally by supervised_session)
        except CLINotFoundError:
            raise RuntimeError(
                f"Junior Dev failed (ticket {ticket.id}): 'claude' CLI not found on PATH. Run 'claude login'."
            ) from None
        except (CLIConnectionError, ClaudeSDKError) as e:
            last_error = e
            if attempt < _MAX_RETRIES:
                print(
                    f"[JUNIOR DEV] Attempt {attempt + 1} failed ({type(e).__name__}), retrying in {config.retry_delay}s...",
                    file=sys.stderr,
                )
                await asyncio.sleep(config.retry_delay)
            else:
                raise RuntimeError(
                    f"Junior Dev failed (ticket {ticket.id}) after {_MAX_RETRIES + 1} attempts. Last error: {last_error}"
                ) from None

    if session_result is None:
        raise RuntimeError(f"Junior Dev failed (ticket {ticket.id}): no session result")

    # Handle stall: retry with escalated prompt
    if session_result.stalled:
        log_dir = golem_dir if golem_dir else Path(worktree_path)
        ProgressLogger(log_dir).log_stall_retry("junior_dev")
        escalated = build_escalated_prompt(
            "junior_dev", original_prompt, session_result.turns, stall_cfg.expected_actions
        )

        # Use rework prompt as base for escalated retry
        rework_template_path = Path(__file__).parent / "prompts" / "junior_dev_rework.md"
        if rework_template_path.exists():
            rework_base = build_writer_prompt(ticket, rework_count=max(rework_count, 1), rework_notes=rework_notes)
            escalated = build_escalated_prompt(
                "junior_dev", rework_base, session_result.turns, stall_cfg.expected_actions
            )

        try:
            retry_result = await supervised_session(
                prompt=escalated,
                options=options,
                role="junior_dev",
                config=config,
                stall_config=stall_cfg,
                on_text=on_text,
                on_tool=on_tool,
                golem_dir=golem_dir,
                event_bus=event_bus,
            )
        except (CLIConnectionError, ClaudeSDKError) as e:
            raise RuntimeError(
                f"Junior Dev retry failed (ticket {ticket.id}): {e}"
            ) from None

        if retry_result.stalled:
            ProgressLogger(log_dir).log_stall_fatal("junior_dev", retry_result.turns)
            # Mark ticket as failed
            from golem.tickets import TicketStore
            if golem_dir:
                store = TicketStore(golem_dir / "tickets")
                try:
                    await store.update(
                        ticket_id=ticket.id,
                        status="failed",
                        note=f"Junior Dev stalled after {retry_result.turns} turns (retry also stalled)",
                        agent="junior_dev-supervisor",
                    )
                except Exception:
                    pass
            raise RuntimeError(
                f"Junior Dev (ticket {ticket.id}) stalled after retry — {retry_result.turns} turns with no progress"
            )
        session_result = retry_result

    # Post-session worktree verification: check that files were changed
    # Skip in test mode (GOLEM_TEST_MODE=1) to avoid spurious retries in tests
    diff_output = "skip"
    if os.environ.get("GOLEM_TEST_MODE") != "1":
        try:
            diff_proc = subprocess.run(
                ["git", "diff", "--stat", "HEAD"],
                cwd=worktree_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            diff_output = diff_proc.stdout.strip()
        except Exception:
            diff_output = ""

    if not diff_output:
        # No files changed — treat as stall and retry
        log_dir = golem_dir if golem_dir else Path(worktree_path)
        ProgressLogger(log_dir).log_stall_warning(
            "junior_dev", session_result.turns, config.max_worker_turns, session_result.registry.action_call_count()
        )
        escalated = build_escalated_prompt(
            "junior_dev", original_prompt, session_result.turns, ["file edits"]
        )
        try:
            retry_result = await supervised_session(
                prompt=escalated,
                options=options,
                role="junior_dev",
                config=config,
                stall_config=stall_cfg,
                on_text=on_text,
                on_tool=on_tool,
                golem_dir=golem_dir,
                event_bus=event_bus,
            )
        except (CLIConnectionError, ClaudeSDKError) as e:
            raise RuntimeError(
                f"Junior Dev no-diff retry failed (ticket {ticket.id}): {e}"
            ) from None

        if retry_result.stalled:
            ProgressLogger(log_dir).log_stall_fatal("junior_dev", retry_result.turns)
            from golem.tickets import TicketStore
            if golem_dir:
                store = TicketStore(golem_dir / "tickets")
                try:
                    await store.update(
                        ticket_id=ticket.id,
                        status="failed",
                        note=f"Junior Dev produced no file changes after {retry_result.turns} turns",
                        agent="junior_dev-supervisor",
                    )
                except Exception:
                    pass
            raise RuntimeError(
                f"Junior Dev (ticket {ticket.id}) produced no file changes after retry"
            )
        session_result = retry_result

    result_text = session_result.result_text
    cost_usd = session_result.cost_usd
    input_tokens = session_result.input_tokens
    output_tokens = session_result.output_tokens
    num_turns = session_result.turns
    duration_ms = int(session_result.duration_s * 1000)

    log_dir = golem_dir if golem_dir else Path(worktree_path)
    ProgressLogger(log_dir).log_agent_cost(
        role=f"junior_dev/{ticket.id}",
        cost_usd=cost_usd,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read=0,
        turns=num_turns,
        duration_s=int(session_result.duration_s),
    )
    return JuniorDevResult(
        result_text=result_text,
        cost_usd=cost_usd,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=0,
        num_turns=num_turns,
        duration_ms=duration_ms,
    )


# Backward-compatible alias
spawn_writer_pair = spawn_junior_dev
