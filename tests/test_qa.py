from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from golem.qa import detect_infrastructure_checks, run_autofix, run_qa


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
        # Use a command that fails and produces output on Windows and Unix
        if sys.platform == "win32":
            cmd = 'cmd /c "echo hello_stdout && echo hello_stderr 1>&2 && exit 1"'
        else:
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


def test_run_autofix_both_ruff_and_prettier() -> None:
    """When both ruff and prettier are in infrastructure_checks, both autofix commands run."""
    with tempfile.TemporaryDirectory() as tmpdir:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            run_autofix(tmpdir, ["ruff check .", "npx prettier --check ."])
            calls = [str(c.args[0]) for c in mock_run.call_args_list]
            assert any("ruff check --fix" in c for c in calls)
            assert any("ruff format" in c for c in calls)
            assert any("prettier --write" in c for c in calls)


def test_qa_check_type_classification() -> None:
    """QACheck.type is lint for ruff, test for pytest, acceptance for custom commands."""
    with tempfile.TemporaryDirectory() as tmpdir:
        result = run_qa(tmpdir, ["exit 0"], ["ruff check ."])
        # Infrastructure check 'ruff check .' should be lint type
        ruff_check = result.checks[0]
        assert ruff_check.type == "lint"

        # Custom command 'exit 0' should be acceptance type
        custom_check = result.checks[1]
        assert custom_check.type == "acceptance"


def test_qa_check_type_test() -> None:
    """QACheck.type is 'test' for pytest commands."""
    with tempfile.TemporaryDirectory() as tmpdir:
        result = run_qa(tmpdir, ["echo pytest_placeholder"], [])
        # 'pytest' in command -> test type
        check = result.checks[0]
        assert check.type == "test"


def test_detect_infrastructure_checks_finds_mypy() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        pyproject = Path(tmpdir) / "pyproject.toml"
        pyproject.write_text("[tool.mypy]\nstrict = true\n", encoding="utf-8")
        checks = detect_infrastructure_checks(Path(tmpdir))
        assert "mypy ." in checks


def test_detect_infrastructure_checks_finds_cargo_test() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        cargo = Path(tmpdir) / "Cargo.toml"
        cargo.write_text('[package]\nname = "test"\n', encoding="utf-8")
        checks = detect_infrastructure_checks(Path(tmpdir))
        assert "cargo test" in checks


def test_detect_infrastructure_checks_finds_npm_test() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        pkg = Path(tmpdir) / "package.json"
        pkg.write_text(json.dumps({"scripts": {"test": "jest"}}), encoding="utf-8")
        checks = detect_infrastructure_checks(Path(tmpdir))
        assert "npm test" in checks
