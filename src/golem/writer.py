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

from golem.config import GolemConfig, sdk_env
from golem.reviewer import run_reviewer
from golem.tickets import Ticket
from golem.tools import create_writer_mcp_server

_WRITER_PROMPT_TEMPLATE = Path(__file__).parent / "prompts" / "worker.md"


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


async def _run_writer_session(
    prompt: str,
    worktree_path: str,
    config: GolemConfig,
    golem_dir: Path | None,
    ticket_id: str,
) -> str:
    """Run a single writer SDK session. Returns the writer's result text."""
    result_text = ""
    writer_server = create_writer_mcp_server(golem_dir) if golem_dir else create_writer_mcp_server(Path(worktree_path))

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
                            print(f"[WRITER] tool: {block.name}({', '.join(f'{k}=' for k in list(block.input.keys())[:3])})", file=sys.stderr)
                elif isinstance(message, ResultMessage):
                    if message.result:
                        result_text = message.result
                        preview = message.result[:120].replace("\n", " ")
                        print(f"[WRITER] result: {preview}", file=sys.stderr)
            break  # Success
        except CLINotFoundError:
            raise RuntimeError(
                f"Writer failed (ticket {ticket_id}): 'claude' CLI not found on PATH. Run 'claude login'."
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
                    f"Writer failed (ticket {ticket_id}) after {_MAX_RETRIES + 1} attempts. Last error: {last_error}"
                ) from None

    return result_text


async def spawn_writer_pair(
    ticket: Ticket,
    worktree_path: str,
    config: GolemConfig,
    golem_dir: Path | None = None,
    project_root: Path | None = None,
) -> str:
    """Spawn a writer SDK session for the given ticket in the worktree.

    After the writer finishes, runs a Reviewer sub-agent (unless skip_review is set).
    If the reviewer blocks, feeds critical issues back for rework (one retry).
    Returns the writer's result text.
    """
    prompt = build_writer_prompt(ticket)
    result_text = await _run_writer_session(prompt, worktree_path, config, golem_dir, ticket.id)

    # --- Reviewer gate (skip for trivial tickets) ---
    if ticket.context.skip_review:
        print(f"[WRITER] Skipping review for ticket {ticket.id} (skip_review=True)", file=sys.stderr)
        return result_text

    effective_root = project_root or Path(worktree_path)
    plan_section = ""
    if ticket.context.plan_file and Path(ticket.context.plan_file).exists():
        plan_section = Path(ticket.context.plan_file).read_text(encoding="utf-8")

    try:
        verdict = await run_reviewer(
            worktree_path=Path(worktree_path),
            project_root=effective_root,
            ticket_title=ticket.title,
            plan_section=plan_section,
            acceptance_criteria=ticket.context.acceptance,
            config=config,
        )
    except Exception as review_err:
        # Reviewer failure should not block the pipeline -- log and proceed
        print(f"[REVIEWER] Error during review (proceeding to QA): {review_err}", file=sys.stderr)
        return result_text

    if verdict.decision == "approve":
        print(f"[REVIEWER] Approved: {verdict.summary}", file=sys.stderr)
        return result_text

    if verdict.decision == "warning":
        warnings_text = "; ".join(verdict.important_issues) if verdict.important_issues else verdict.summary
        print(f"[REVIEWER] Warning (proceeding to QA): {warnings_text}", file=sys.stderr)
        # Attach warnings to result so Tech Lead can see them
        result_text += f"\n\n[REVIEWER WARNINGS] {warnings_text}"
        return result_text

    # decision == "block" -- feed critical issues back as rework context
    issues_text = "\n".join(f"- {i}" for i in verdict.critical_issues)
    print(f"[REVIEWER] Blocked -- requesting rework:\n{issues_text}", file=sys.stderr)

    rework_prompt = (
        f"The code reviewer has BLOCKED your changes. Fix these critical issues:\n\n"
        f"{issues_text}\n\n"
        f"After fixing, re-run QA checks.\n\n"
        f"Original task context:\n{prompt}"
    )
    result_text = await _run_writer_session(rework_prompt, worktree_path, config, golem_dir, ticket.id)
    return result_text
