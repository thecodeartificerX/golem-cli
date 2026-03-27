from __future__ import annotations

import asyncio
import os
import random
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
from golem.tickets import Ticket
from golem.tools import create_writer_mcp_server

_WRITER_PROMPT_TEMPLATE = Path(__file__).parent / "prompts" / "worker.md"


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
    """Build writer prompt from ticket context, with optional rework context."""
    template_name = "worker_rework.md" if rework_count > 0 else "worker.md"
    template_path = Path(__file__).parent / "prompts" / template_name
    # Fall back to worker.md if rework template doesn't exist yet
    if not template_path.exists():
        template_path = _WRITER_PROMPT_TEMPLATE
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


async def spawn_writer_pair(
    ticket: Ticket,
    worktree_path: str,
    config: GolemConfig,
    golem_dir: Path | None = None,
) -> str:
    """Spawn a writer SDK session for the given ticket in the worktree.

    Returns the writer's result text.
    """
    rework_count, rework_notes = _get_rework_info(ticket)
    prompt = build_writer_prompt(ticket, rework_count=rework_count, rework_notes=rework_notes)

    # Stagger parallel writer spawns to reduce I/O contention on uv cache
    jitter = config.dispatch_jitter_max
    if jitter > 0 and os.environ.get("GOLEM_TEST_MODE") != "1":
        delay = random.uniform(0, jitter)
        print(f"[JUNIOR DEV] {ticket.id}: jitter delay {delay:.1f}s", file=sys.stderr)
        await asyncio.sleep(delay)

    result_text = ""

    writer_server = create_writer_mcp_server(golem_dir) if golem_dir else create_writer_mcp_server(Path(worktree_path))
    sources, mcps = resolve_agent_options(
        config, "writer", writer_server, golem_mcp_name="golem-writer",
    )

    last_error: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            async for message in query(
                prompt=prompt,
                options=ClaudeAgentOptions(
                    model=config.worker_model,
                    cwd=worktree_path,
                    tools={"type": "preset", "preset": "claude_code"},
                    mcp_servers=mcps,
                    setting_sources=sources,
                    max_turns=config.max_worker_turns,
                    permission_mode="bypassPermissions",
                    env=sdk_env(),
                ),
            ):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            result_text = block.text
                            preview = block.text[:120].replace("\n", " ")
                            print(f"[JUNIOR DEV] {preview}", file=sys.stderr)
                        elif isinstance(block, ToolUseBlock):
                            print(f"[JUNIOR DEV] tool: {block.name}({', '.join(f'{k}=' for k in list(block.input.keys())[:3])})", file=sys.stderr)
                elif isinstance(message, ResultMessage):
                    if message.result:
                        result_text = message.result
                        preview = message.result[:120].replace("\n", " ")
                        print(f"[JUNIOR DEV] result: {preview}", file=sys.stderr)
            break  # Success
        except CLINotFoundError:
            raise RuntimeError(
                f"Writer failed (ticket {ticket.id}): 'claude' CLI not found on PATH. Run 'claude login'."
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
                    f"Writer failed (ticket {ticket.id}) after {_MAX_RETRIES + 1} attempts. Last error: {last_error}"
                ) from None

    return result_text
