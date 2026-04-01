from __future__ import annotations

import sys

from golem.validator import _find_bash, _normalize_cmd, _subprocess_env


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


def test_find_bash_returns_none_or_string() -> None:
    """_find_bash() returns either None or a non-empty string on all platforms."""
    result = _find_bash()
    assert result is None or (isinstance(result, str) and len(result) > 0)
