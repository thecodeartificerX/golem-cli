"""Tests for PreToolUse hook scripts."""
import json
import os
import subprocess
from pathlib import Path

HOOKS_DIR = Path(__file__).parent.parent / ".claude" / "hooks"


def run_hook(
    script_name: str,
    tool_name: str,
    tool_input: dict | None = None,
    set_sdk_session: bool = True,
) -> dict | None:
    env = {k: v for k, v in os.environ.items() if k != "GOLEM_SDK_SESSION"}
    if set_sdk_session:
        env["GOLEM_SDK_SESSION"] = "1"
    stdin_data = json.dumps({"tool_name": tool_name, "tool_input": tool_input or {}})
    result = subprocess.run(
        ["python", str(HOOKS_DIR / script_name)],
        input=stdin_data,
        capture_output=True,
        text=True,
        env=env,
    )
    if result.stdout.strip():
        return json.loads(result.stdout)
    return None


def test_block_golem_cli_blocks_clean() -> None:
    out = run_hook("block-golem-cli.py", "Bash", {"command": "uv run golem clean"})
    assert out is not None
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_block_golem_cli_allows_normal_bash() -> None:
    out = run_hook("block-golem-cli.py", "Bash", {"command": "ruff check ."})
    assert out is None


def test_block_golem_cli_passthrough_without_env() -> None:
    out = run_hook(
        "block-golem-cli.py",
        "Bash",
        {"command": "uv run golem clean"},
        set_sdk_session=False,
    )
    assert out is None


def test_block_ask_user_question_blocks() -> None:
    out = run_hook(
        "block-ask-user-question.py",
        "AskUserQuestion",
        {"question": "What should I do?"},
    )
    assert out is not None
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_block_ask_user_question_passthrough_without_env() -> None:
    out = run_hook(
        "block-ask-user-question.py",
        "AskUserQuestion",
        {"question": "What?"},
        set_sdk_session=False,
    )
    assert out is None


def test_block_dangerous_git_blocks_stash() -> None:
    out = run_hook("block-dangerous-git.py", "Bash", {"command": "git stash"})
    assert out is not None
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_block_dangerous_git_blocks_reset_hard() -> None:
    out = run_hook("block-dangerous-git.py", "Bash", {"command": "git reset --hard HEAD~1"})
    assert out is not None
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_block_dangerous_git_blocks_force_push() -> None:
    out = run_hook("block-dangerous-git.py", "Bash", {"command": "git push --force origin main"})
    assert out is not None
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_block_dangerous_git_allows_safe_ops() -> None:
    for cmd in ["git add .", "git commit -m test", "git push"]:
        out = run_hook("block-dangerous-git.py", "Bash", {"command": cmd})
        assert out is None, f"Expected pass-through for {cmd!r}, got: {out}"


def test_block_dangerous_git_allows_branch_d_lowercase() -> None:
    out = run_hook("block-dangerous-git.py", "Bash", {"command": "git branch -d feat"})
    assert out is None


def test_sdk_env_includes_golem_session() -> None:
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from golem.config import sdk_env

    env = sdk_env()
    assert env.get("GOLEM_SDK_SESSION") == "1", f"GOLEM_SDK_SESSION missing or wrong: {env}"
