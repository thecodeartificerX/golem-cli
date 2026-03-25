from __future__ import annotations

from pathlib import Path

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, query

from golem.config import GolemConfig, sdk_env
from golem.tasks import Task

_WORKER_PROMPT_TEMPLATE = Path(__file__).parent / "prompts" / "worker.md"


def _build_worker_prompt(task: Task, feedback: str | None) -> str:
    template = _WORKER_PROMPT_TEMPLATE.read_text(encoding="utf-8")
    files_create = "\n".join(task.files_create) if task.files_create else "(none)"
    files_modify = "\n".join(task.files_modify) if task.files_modify else "(none)"
    acceptance = "\n".join(f"- {a}" for a in task.acceptance)
    reference_docs = "\n".join(task.reference_docs) if task.reference_docs else "(none)"

    if feedback:
        last_feedback_section = feedback
    else:
        # Remove the entire "Previous Attempt Feedback" section when no feedback
        lines = template.splitlines()
        out: list[str] = []
        skip = False
        for line in lines:
            if line.strip() == "## Previous Attempt Feedback":
                skip = True
                continue
            if skip and line.startswith("## "):
                skip = False
            if not skip:
                out.append(line)
        template = "\n".join(out)
        last_feedback_section = ""

    prompt = template.replace("{task_description}", task.description)
    prompt = prompt.replace("{files_create}", files_create)
    prompt = prompt.replace("{files_modify}", files_modify)
    prompt = prompt.replace("{acceptance}", acceptance)
    prompt = prompt.replace("{reference_docs}", reference_docs)
    if last_feedback_section:
        prompt = prompt.replace("{last_feedback}", last_feedback_section)
    return prompt


async def run_worker(
    task: Task,
    worktree_path: str,
    feedback: str | None,
    config: GolemConfig,
    dashboard_cb: object = None,
) -> str:
    prompt = _build_worker_prompt(task, feedback)
    result_text = ""

    async for message in query(
        prompt=prompt,
        options=ClaudeAgentOptions(
            model=config.worker_model,
            cwd=worktree_path,
            allowed_tools=["Bash", "Read", "Edit", "Write", "Glob", "Grep"],
            max_turns=config.max_worker_turns,
            permission_mode="bypassPermissions",
            env=sdk_env(),
        ),
    ):
        if isinstance(message, ResultMessage):
            result_text = message.result or ""
        elif isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock) and callable(dashboard_cb):
                    dashboard_cb(task.id, block.text)

    return result_text
