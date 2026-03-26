from __future__ import annotations

import json
import sys
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    query,
)

from golem.config import GolemConfig, sdk_env
from golem.tasks import Task

_WORKER_PROMPT_TEMPLATE = Path(__file__).parent / "prompts" / "worker.md"


def _strip_section(template: str, heading: str) -> str:
    """Remove a markdown ## section (heading + body) from the template."""
    lines = template.splitlines()
    out: list[str] = []
    skip = False
    for line in lines:
        if line.strip() == heading:
            skip = True
            continue
        if skip and line.startswith("## "):
            skip = False
        if not skip:
            out.append(line)
    return "\n".join(out)


def _build_worker_prompt(task: Task, feedback: str | None, blueprint: str = "") -> str:
    template = _WORKER_PROMPT_TEMPLATE.read_text(encoding="utf-8")
    files_create = "\n".join(task.files_create) if task.files_create else "(none)"
    files_modify = "\n".join(task.files_modify) if task.files_modify else "(none)"
    acceptance = "\n".join(f"- {a}" for a in task.acceptance)
    reference_docs = "\n".join(task.reference_docs) if task.reference_docs else "(none)"

    if not feedback:
        template = _strip_section(template, "## Previous Attempt Feedback")
    if not blueprint:
        template = _strip_section(template, "## Shared Blueprint")

    prompt = template.replace("{task_description}", task.description)
    prompt = prompt.replace("{files_create}", files_create)
    prompt = prompt.replace("{files_modify}", files_modify)
    prompt = prompt.replace("{acceptance}", acceptance)
    prompt = prompt.replace("{reference_docs}", reference_docs)
    if feedback:
        prompt = prompt.replace("{last_feedback}", feedback)
    if blueprint:
        prompt = prompt.replace("{blueprint}", blueprint)
    return prompt


async def run_worker(
    task: Task,
    worktree_path: str,
    feedback: str | None,
    config: GolemConfig,
    blueprint: str = "",
    dashboard_cb: object = None,
) -> str:
    prompt = _build_worker_prompt(task, feedback, blueprint=blueprint)
    result_text = ""

    tag = f"[WORKER {task.id}]"
    async for message in query(
        prompt=prompt,
        options=ClaudeAgentOptions(
            model=config.worker_model,
            cwd=worktree_path,
            tools={"type": "preset", "preset": "claude_code"},
            setting_sources=config.setting_sources,
            max_turns=config.max_worker_turns,
            permission_mode="bypassPermissions",
            env=sdk_env(),
        ),
    ):
        if isinstance(message, ResultMessage):
            result_text = message.result or ""
            print(f"{tag} result: {result_text[:200]}...", file=sys.stderr, flush=True)
        elif isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    if callable(dashboard_cb):
                        dashboard_cb(task.id, block.text)
                    print(f"{tag} {block.text[:300]}", file=sys.stderr, flush=True)
                elif isinstance(block, ToolUseBlock):
                    input_preview = json.dumps(block.input, default=str)[:200]
                    print(f"{tag} tool: {block.name}({input_preview})", file=sys.stderr, flush=True)
                elif isinstance(block, ToolResultBlock):
                    content_str = str(block.content or "")[:200]
                    err = " ERROR" if block.is_error else ""
                    print(f"{tag} result{err}: {content_str}", file=sys.stderr, flush=True)

    return result_text
