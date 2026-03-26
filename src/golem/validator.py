from __future__ import annotations

import json
import os
import subprocess
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
from golem.tasks import Task, TasksFile

_VALIDATOR_PROMPT_TEMPLATE = Path(__file__).parent / "prompts" / "validator.md"
_INTEGRATION_REVIEWER_PROMPT_TEMPLATE = Path(__file__).parent / "prompts" / "integration_reviewer.md"


def _subprocess_env() -> dict[str, str]:
    """Build env for validation subprocesses with fresh user PATH from Windows registry.

    On Windows, ``subprocess.run(shell=True)`` uses cmd.exe which inherits
    the parent's PATH. If the parent (e.g. Claude Code) was started before
    a tool like ``rg`` was added to PATH, cmd.exe won't find it. We read
    the user PATH from the registry and prepend only entries that are missing
    from the inherited PATH — avoiding duplication and PATH length issues.
    """
    env = dict(os.environ)
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
                user_path, _ = winreg.QueryValueEx(key, "Path")
                current = set(env.get("PATH", "").lower().split(";"))
                new_entries = [
                    os.path.expandvars(p) for p in user_path.split(";")
                    if p and os.path.expandvars(p).lower() not in current
                ]
                if new_entries:
                    env["PATH"] = ";".join(new_entries) + ";" + env.get("PATH", "")
        except OSError:
            pass
    return env


def _normalize_cmd(cmd: str) -> str:
    """Normalize shell commands for Windows cmd.exe compatibility.

    Specs use Unix-style single quotes (e.g. ``rg -q 'pattern' file``).
    Windows cmd.exe doesn't recognize single quotes as delimiters, so
    ``rg`` receives literal ``'pattern'`` including the quotes. Replace
    single quotes with double quotes for Windows.
    """
    if sys.platform == "win32":
        return cmd.replace("'", '"')
    return cmd


def run_deterministic_checks(task: Task, worktree_path: str) -> tuple[bool, str]:
    """Run each validation_command as a subprocess. Returns (passed, feedback)."""
    env = _subprocess_env()
    for cmd in task.validation_commands:
        result = subprocess.run(
            _normalize_cmd(cmd),
            shell=True,
            cwd=worktree_path,
            capture_output=True,
            text=True,
            env=env,
        )
        if result.returncode != 0:
            feedback = f"Validation command failed: {cmd}\n"
            feedback += f"stdout: {result.stdout}\nstderr: {result.stderr}"
            return False, feedback
    return True, ""


def run_infrastructure_checks(commands: list[str], worktree_path: str) -> tuple[bool, str]:
    """Run always-on infrastructure checks. Returns (passed, feedback)."""
    if not commands:
        return True, ""
    env = _subprocess_env()
    for cmd in commands:
        result = subprocess.run(
            _normalize_cmd(cmd),
            shell=True,
            cwd=worktree_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
        )
        if result.returncode != 0:
            feedback = f"Infrastructure check failed: {cmd}\n"
            feedback += f"stdout: {result.stdout}\nstderr: {result.stderr}"
            return False, feedback
    return True, ""


async def run_ai_validator(task: Task, worktree_path: str, config: GolemConfig, blueprint: str = "") -> tuple[bool, str]:
    """Run AI validator session. Returns (passed, verdict_text)."""
    template = _VALIDATOR_PROMPT_TEMPLATE.read_text(encoding="utf-8")
    acceptance = "\n".join(f"- {a}" for a in task.acceptance)
    prompt = template.replace("{task_description}", task.description)
    prompt = prompt.replace("{acceptance}", acceptance)
    prompt = prompt.replace("{blueprint}", blueprint or "(no blueprint — no cross-cutting contracts for this spec)")

    result_text = "Validator session ended without verdict"

    tag = f"[VALIDATOR {task.id}]"
    async for message in query(
        prompt=prompt,
        options=ClaudeAgentOptions(
            model=config.validator_model,
            cwd=worktree_path,
            tools={"type": "preset", "preset": "claude_code"},
            disallowed_tools=["Write", "Edit"],
            setting_sources=config.setting_sources,
            max_turns=config.max_validator_turns,
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
                    print(f"{tag} {block.text[:300]}", file=sys.stderr, flush=True)
                elif isinstance(block, ToolUseBlock):
                    input_preview = json.dumps(block.input, default=str)[:200]
                    print(f"{tag} tool: {block.name}({input_preview})", file=sys.stderr, flush=True)
                elif isinstance(block, ToolResultBlock):
                    content_str = str(block.content or "")[:200]
                    err = " ERROR" if block.is_error else ""
                    print(f"{tag} result{err}: {content_str}", file=sys.stderr, flush=True)

    # The validator prompt asks for "PASS:" or "FAIL:" but models sometimes
    # prefix with preamble. Search for the verdict marker anywhere in the text.
    upper = result_text.upper()
    if "PASS:" in upper or upper.strip().startswith("PASS"):
        return True, result_text
    return False, result_text


async def run_validation(task: Task, worktree_path: str, config: GolemConfig, blueprint: str = "") -> tuple[bool, str]:
    """Two-tier validation: deterministic first, then AI if deterministic passes."""
    passed, feedback = run_deterministic_checks(task, worktree_path)
    if not passed:
        return False, feedback
    return await run_ai_validator(task, worktree_path, config, blueprint=blueprint)


async def run_integration_reviewer(
    tasks_file: TasksFile,
    merged_path: str,
    spec_content: str,
    config: GolemConfig,
) -> tuple[bool, str]:
    """Post-merge AI integration reviewer (Tier 2). Returns (passed, verdict_text)."""
    template = _INTEGRATION_REVIEWER_PROMPT_TEMPLATE.read_text(encoding="utf-8")
    prompt = template.replace("{spec_content}", spec_content)
    prompt = prompt.replace("{blueprint}", tasks_file.blueprint or "(no blueprint)")

    result_text = "Integration reviewer ended without verdict"

    tag = "[INTEGRATION-REVIEWER]"
    async for message in query(
        prompt=prompt,
        options=ClaudeAgentOptions(
            model=config.validator_model,
            cwd=merged_path,
            tools={"type": "preset", "preset": "claude_code"},
            disallowed_tools=["Write", "Edit"],
            setting_sources=config.setting_sources,
            max_turns=config.max_validator_turns,
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
                    print(f"{tag} {block.text[:300]}", file=sys.stderr, flush=True)
                elif isinstance(block, ToolUseBlock):
                    input_preview = json.dumps(block.input, default=str)[:200]
                    print(f"{tag} tool: {block.name}({input_preview})", file=sys.stderr, flush=True)
                elif isinstance(block, ToolResultBlock):
                    content_str = str(block.content or "")[:200]
                    err = " ERROR" if block.is_error else ""
                    print(f"{tag} result{err}: {content_str}", file=sys.stderr, flush=True)

    upper = result_text.upper()
    if "PASS:" in upper or upper.strip().startswith("PASS"):
        return True, result_text
    return False, result_text
