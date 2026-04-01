from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from golem.qa import _run_single_check, detect_infrastructure_checks, run_autofix, run_qa


def test_run_qa_all_pass() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        result = run_qa(tmpdir, ["exit 0", "echo ok"], [])
        assert result.passed is True
        assert all(c.passed for c in result.checks)


def test_run_qa_one_fails() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        result = run_qa(
            tmpdir,
            [
                "exit 0",
                "exit 1",
            ],
            [],
        )
        assert result.passed is False
        passed = [c for c in result.checks if c.passed]
        failed = [c for c in result.checks if not c.passed]
        assert len(passed) == 1
        assert len(failed) == 1


def test_run_qa_captures_stdout_stderr() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        # On Windows, bash routing is used so POSIX syntax works; same for Unix
        cmd = "echo hello_stdout; echo hello_stderr >&2; exit 1"
        result = run_qa(tmpdir, [cmd], [])
        check = result.checks[0]
        assert check.passed is False
        assert "hello_stdout" in check.stdout or "hello_stderr" in check.stderr


def test_run_qa_summary_format() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        result = run_qa(tmpdir, ["exit 0", "exit 1", "exit 1"], [])
        assert "1/3" in result.summary
        assert "exit 1" in result.summary or "Failed" in result.summary


def test_run_qa_summary_all_pass() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        result = run_qa(tmpdir, ["exit 0", "echo ok"], [])
        assert "2/2" in result.summary
        # Should not mention "Failed" when all pass
        assert "Failed" not in result.summary or "0" in result.summary


def test_run_autofix_runs_ruff() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            run_autofix(tmpdir, ["ruff check ."])
            calls = [str(c.args[0]) for c in mock_run.call_args_list]
            assert any("ruff check --fix" in c for c in calls)
            assert any("ruff format" in c for c in calls)


def test_run_autofix_runs_prettier() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            run_autofix(tmpdir, ["npx prettier --check ."])
            calls = [str(c.args[0]) for c in mock_run.call_args_list]
            assert any("prettier --write" in c for c in calls)


def test_run_autofix_noop_no_matching_checks() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        with patch("subprocess.run") as mock_run:
            run_autofix(tmpdir, ["echo hello"])
            # Should not call subprocess at all — no ruff or prettier
            mock_run.assert_not_called()


def test_detect_infrastructure_checks_finds_ruff() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        pyproject = Path(tmpdir) / "pyproject.toml"
        pyproject.write_text("[tool.ruff]\nline-length = 120\n", encoding="utf-8")
        checks = detect_infrastructure_checks(Path(tmpdir))
        assert "ruff check ." in checks


def test_detect_infrastructure_checks_finds_npm_lint() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        pkg = Path(tmpdir) / "package.json"
        pkg.write_text(json.dumps({"scripts": {"lint": "eslint src"}}), encoding="utf-8")
        checks = detect_infrastructure_checks(Path(tmpdir))
        assert "npm run lint" in checks


def test_infrastructure_checks_run_first() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        infra = ["exit 0"]
        spec = ["echo spec_check"]
        result = run_qa(tmpdir, spec, infra)
        # infra check should appear before spec check in results
        assert result.checks[0].tool == infra[0]
        assert result.checks[1].tool == spec[0]


def test_run_qa_empty_checks() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        result = run_qa(tmpdir, [], [])
        assert result.passed is True
        assert result.checks == []
        assert "0/0" in result.summary or result.summary == ""


def test_run_single_check_bash_routing_on_windows() -> None:
    """On Windows with bash found, _run_single_check uses shell=False with bash list command."""
    fake_bash = r"C:\Program Files\Git\bin\bash.exe"
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = ""
    mock_result.stderr = ""

    with tempfile.TemporaryDirectory() as tmpdir:
        with patch("sys.platform", "win32"):
            with patch("golem.qa._find_bash", return_value=fake_bash):
                with patch("subprocess.run", return_value=mock_result) as mock_run:
                    check = _run_single_check("test -f README.md", tmpdir)

    assert check.passed is True
    call_args = mock_run.call_args
    cmd_arg = call_args[0][0]
    assert isinstance(cmd_arg, list)
    assert cmd_arg[0] == fake_bash
    assert cmd_arg[1] == "-c"
    assert cmd_arg[2] == "test -f README.md"
    assert call_args[1]["shell"] is False


def test_run_single_check_no_bash_fallback() -> None:
    """On Windows with no bash found, _run_single_check falls back to shell=True and sets cannot_validate=True."""
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = ""
    mock_result.stderr = ""

    with tempfile.TemporaryDirectory() as tmpdir:
        with patch("sys.platform", "win32"):
            with patch("golem.qa._find_bash", return_value=None):
                with patch("subprocess.run", return_value=mock_result):
                    check = _run_single_check("test -f README.md", tmpdir)

    assert check.cannot_validate is True
    assert "WARNING" in check.stderr
    assert "bash.exe not found" in check.stderr


def test_run_single_check_unix_unchanged() -> None:
    """On Linux/macOS, _run_single_check uses shell=True with the raw command string."""
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "ok"
    mock_result.stderr = ""

    with tempfile.TemporaryDirectory() as tmpdir:
        with patch("sys.platform", "linux"):
            with patch("subprocess.run", return_value=mock_result) as mock_run:
                check = _run_single_check("echo ok", tmpdir)

    assert check.passed is True
    call_args = mock_run.call_args
    cmd_arg = call_args[0][0]
    assert cmd_arg == "echo ok"
    assert call_args[1]["shell"] is True


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only integration test")
def test_posix_test_command_passes_with_real_bash(tmp_path: Path) -> None:
    """Integration test: POSIX test -f command passes via real bash routing on Windows."""
    readme = tmp_path / "README.md"
    readme.write_text("hello", encoding="utf-8")

    result = run_qa(str(tmp_path), ["test -f README.md"], [])
    assert result.passed is True, f"Expected pass, got: {result.summary}"
