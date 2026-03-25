from __future__ import annotations

import subprocess
from pathlib import Path

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, query

from golem.config import GolemConfig
from golem.tasks import Task

_VALIDATOR_PROMPT_TEMPLATE = Path(__file__).parent / "prompts" / "validator.md"


def run_deterministic_checks(task: Task, worktree_path: str) -> tuple[bool, str]:
    """Run each validation_command as a subprocess. Returns (passed, feedback)."""
    for cmd in task.validation_commands:
        result = subprocess.run(
            cmd,
            shell=True,
            cwd=worktree_path,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            feedback = f"Validation command failed: {cmd}\n"
            feedback += f"stdout: {result.stdout}\nstderr: {result.stderr}"
            return False, feedback
    return True, ""


async def run_ai_validator(task: Task, worktree_path: str, config: GolemConfig) -> tuple[bool, str]:
    """Run AI validator session. Returns (passed, verdict_text)."""
    template = _VALIDATOR_PROMPT_TEMPLATE.read_text()
    acceptance = "\n".join(f"- {a}" for a in task.acceptance)
    prompt = template.replace("{task_description}", task.description)
    prompt = prompt.replace("{acceptance}", acceptance)

    result_text = "Validator session ended without verdict"

    async for message in query(
        prompt=prompt,
        options=ClaudeAgentOptions(
            model=config.validator_model,
            cwd=worktree_path,
            allowed_tools=["Read", "Glob", "Grep", "Bash"],
            disallowed_tools=["Write", "Edit"],
            max_turns=config.max_validator_turns,
            permission_mode="acceptEdits",
        ),
    ):
        if isinstance(message, ResultMessage):
            result_text = message.result or ""

    if result_text.startswith("PASS"):
        return True, result_text
    return False, result_text


async def run_validation(task: Task, worktree_path: str, config: GolemConfig) -> tuple[bool, str]:
    """Two-tier validation: deterministic first, then AI if deterministic passes."""
    passed, feedback = run_deterministic_checks(task, worktree_path)
    if not passed:
        return False, feedback
    return await run_ai_validator(task, worktree_path, config)
