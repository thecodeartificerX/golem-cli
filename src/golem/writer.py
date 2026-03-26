from __future__ import annotations

import sys
from pathlib import Path

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, ToolUseBlock, query

from golem.config import GolemConfig, sdk_env
from golem.tickets import Ticket
from golem.tools import create_qa_mcp_server

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


async def spawn_writer_pair(
    ticket: Ticket,
    worktree_path: str,
    config: GolemConfig,
) -> str:
    """Spawn a writer SDK session for the given ticket in the worktree.

    Returns the writer's result text.
    """
    prompt = build_writer_prompt(ticket)
    result_text = ""

    qa_server = create_qa_mcp_server(Path(worktree_path))

    async for message in query(
        prompt=prompt,
        options=ClaudeAgentOptions(
            model=config.worker_model,
            cwd=worktree_path,
            tools={"type": "preset", "preset": "claude_code"},
            mcp_servers={"golem-qa": qa_server},
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

    return result_text
