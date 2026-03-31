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
from golem.tickets import Ticket
from golem.tools import create_writer_mcp_server

_WRITER_PROMPT_TEMPLATE = Path(__file__).parent / "prompts" / "worker.md"


def _strip_section(template: str, key: str) -> str:
    """Replace a template variable with empty string when its value is empty."""
    return template.replace("{" + key + "}", "")


def build_writer_prompt(ticket: Ticket, golem_dir: Path | None = None) -> str:
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

    # Inject reference file content inline (with caps to avoid prompt bloat)
    _MAX_PER_REF = 5_000
    _MAX_TOTAL_REF = 20_000
    if ctx.references:
        reference_sections: list[str] = []
        total_chars = 0
        for ref_path in ctx.references:
            ref_full_path = golem_dir / ref_path if golem_dir and not Path(ref_path).is_absolute() else Path(ref_path)
            if ref_full_path.exists():
                content = ref_full_path.read_text(encoding="utf-8")[:_MAX_PER_REF]
                if total_chars + len(content) > _MAX_TOTAL_REF:
                    reference_sections.append(f"### {ref_path}\n(content omitted -- read if needed)")
                    continue
                reference_sections.append(f"### {ref_path}\n{content}")
                total_chars += len(content)
            else:
                reference_sections.append(f"### {ref_path}\n(file not found)")
        references = "\n\n".join(reference_sections) if reference_sections else "(no references)"
    else:
        references = ""

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
) -> str:
    """Spawn a writer SDK session for the given ticket in the worktree.

    Returns the writer's result text.
    """
    prompt = build_writer_prompt(ticket, golem_dir=golem_dir)
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

    return result_text
