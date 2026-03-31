from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
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

from golem.config import GolemConfig, sdk_env
from golem.qa import run_qa
from golem.tickets import Ticket, TicketStore
from golem.tools import create_writer_mcp_server

_WRITER_PROMPT_TEMPLATE = Path(__file__).parent / "prompts" / "worker.md"


@dataclass
class WriterResult:
    """Result from a writer session including QA verification status."""

    text: str
    qa_called: bool
    qa_forced: bool = False
    qa_passed: bool | None = None


def _strip_section(template: str, key: str) -> str:
    """Replace a template variable with empty string when its value is empty."""
    return template.replace("{" + key + "}", "")


def build_writer_prompt(ticket: Ticket) -> str:
    """Build writer prompt from ticket context, stripping empty sections."""
    template = _WRITER_PROMPT_TEMPLATE.read_text(encoding="utf-8")
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

    replacements = {
        "ticket_context": ticket_context,
        "plan_section": plan_section,
        "file_contents": file_contents,
        "references": references,
        "blueprint": blueprint,
        "acceptance": acceptance,
        "qa_checks": qa_checks,
        "parallelism_hints": parallelism_hints,
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
) -> WriterResult:
    """Spawn a writer SDK session for the given ticket in the worktree.

    Returns a WriterResult with session text and QA verification status.
    If the writer never called run_qa, the harness forces a QA run and
    triggers rework (via ticket update) if it fails.
    """
    prompt = build_writer_prompt(ticket)
    result_text = ""
    qa_called = False

    effective_golem_dir = golem_dir if golem_dir else Path(worktree_path)
    writer_server = create_writer_mcp_server(effective_golem_dir)

    last_error: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            async for message in query(
                prompt=prompt,
                options=ClaudeAgentOptions(
                    model=config.worker_model,
                    cwd=worktree_path,
                    tools={"type": "preset", "preset": "claude_code"},
                    mcp_servers={"golem-writer": writer_server},
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
                            print(f"[WRITER] {preview}", file=sys.stderr)
                        elif isinstance(block, ToolUseBlock):
                            if "run_qa" in block.name:
                                qa_called = True
                            print(f"[WRITER] tool: {block.name}({', '.join(f'{k}=' for k in list(block.input.keys())[:3])})", file=sys.stderr)
                elif isinstance(message, ResultMessage):
                    if message.result:
                        result_text = message.result
                        preview = message.result[:120].replace("\n", " ")
                        print(f"[WRITER] result: {preview}", file=sys.stderr)
            break  # Success
        except CLINotFoundError:
            raise RuntimeError(
                f"Writer failed (ticket {ticket.id}): 'claude' CLI not found on PATH. Run 'claude login'."
            ) from None
        except (CLIConnectionError, ClaudeSDKError) as e:
            last_error = e
            if attempt < _MAX_RETRIES:
                print(
                    f"[WRITER] Attempt {attempt + 1} failed ({type(e).__name__}), retrying in {_RETRY_DELAY_S}s...",
                    file=sys.stderr,
                )
                await asyncio.sleep(_RETRY_DELAY_S)
            else:
                raise RuntimeError(
                    f"Writer failed (ticket {ticket.id}) after {_MAX_RETRIES + 1} attempts. Last error: {last_error}"
                ) from None

    # --- Verification gate: enforce QA was actually run ---
    if qa_called:
        return WriterResult(text=result_text, qa_called=True)

    print(f"[WRITER] Verification gate: writer did not call run_qa for {ticket.id}, forcing QA run", file=sys.stderr)
    qa_checks = ticket.context.qa_checks
    infrastructure_checks: list[str] = []
    qa_result = run_qa(
        worktree_path=worktree_path,
        checks=qa_checks,
        infrastructure_checks=infrastructure_checks,
    )

    if qa_result.passed:
        print(f"[WRITER] Forced QA passed for {ticket.id}: {qa_result.summary}", file=sys.stderr)
        return WriterResult(text=result_text, qa_called=False, qa_forced=True, qa_passed=True)

    # QA failed — update ticket to needs_work to trigger rework
    print(f"[WRITER] Forced QA FAILED for {ticket.id}: {qa_result.summary}", file=sys.stderr)
    store = TicketStore(effective_golem_dir / "tickets")
    await store.update(
        ticket_id=ticket.id,
        status="needs_work",
        note=f"Harness verification gate: writer skipped QA. Forced QA failed: {qa_result.summary}",
        agent="harness",
    )
    return WriterResult(text=result_text, qa_called=False, qa_forced=True, qa_passed=False)
