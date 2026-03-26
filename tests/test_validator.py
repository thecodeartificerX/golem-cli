from __future__ import annotations

import sys

from golem.validator import _normalize_cmd, _subprocess_env


def test_subprocess_env_returns_dict() -> None:
    env = _subprocess_env()
    assert isinstance(env, dict)
    assert "PATH" in env


def test_normalize_cmd_converts_quotes_on_windows() -> None:
    if sys.platform == "win32":
        result = _normalize_cmd("echo 'hello'")
        assert result == 'echo "hello"'
    else:
        result = _normalize_cmd("echo 'hello'")
        assert result == "echo 'hello'"


def test_normalize_cmd_passthrough_non_windows() -> None:
    if sys.platform != "win32":
        cmd = "grep -r 'pattern' src/"
        assert _normalize_cmd(cmd) == cmd


def test_normalize_cmd_no_quotes_unchanged() -> None:
    """Commands without single quotes should pass through unchanged on any platform."""
    cmd = "ruff check . --fix"
    result = _normalize_cmd(cmd)
    assert result == cmd


def test_normalize_cmd_double_quotes_unchanged() -> None:
    """Double quotes should not be affected."""
    cmd = 'echo "hello world"'
    result = _normalize_cmd(cmd)
    assert result == cmd


def test_normalize_cmd_empty_string() -> None:
    """Empty string should pass through unchanged."""
    assert _normalize_cmd("") == ""


def test_subprocess_env_has_path_with_value() -> None:
    """PATH value should not be empty."""
    env = _subprocess_env()
    assert len(env["PATH"]) > 0
