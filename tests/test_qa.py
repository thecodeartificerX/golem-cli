from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from golem.qa import _run_single_check, detect_infrastructure_checks, run_autofix, run_qa
from golem.tools import _handle_run_qa


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
        # Test infrastructure check type classification
        result = run_qa(tmpdir, [], ["ruff check ."])
        ruff_check = result.checks[0]
        assert ruff_check.type == "lint"

        # Test custom command type classification (no infra checks so it runs)
        result2 = run_qa(tmpdir, ["exit 0"], [])
        custom_check = result2.checks[0]
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


def test_two_stage_skips_spec_checks_on_infra_failure(tmp_path: Path) -> None:
    """Infra check fails -> spec checks not run -> stage='infrastructure_failed'."""
    result = run_qa(str(tmp_path), checks=["exit 0"], infrastructure_checks=["exit 1"])
    assert result.stage == "infrastructure_failed"
    # Only the infra check ran — spec check was skipped
    assert len(result.checks) == 1
    assert result.passed is False


def test_two_stage_runs_spec_checks_on_infra_pass(tmp_path: Path) -> None:
    """Infra check passes -> spec checks run -> stage='complete'."""
    result = run_qa(str(tmp_path), checks=["exit 0"], infrastructure_checks=["exit 0"])
    assert result.stage == "complete"
    assert len(result.checks) == 2
    assert result.passed is True


def test_cannot_validate_on_file_not_found(tmp_path: Path) -> None:
    """FileNotFoundError on subprocess.run sets cannot_validate=True on QACheck."""
    with patch("subprocess.run", side_effect=FileNotFoundError("binary not found")):
        check = _run_single_check("fake-binary", str(tmp_path))
    assert check.cannot_validate is True
    assert check.passed is False


def test_cannot_validate_on_os_error(tmp_path: Path) -> None:
    """OSError on subprocess.run sets cannot_validate=True on QACheck."""
    with patch("subprocess.run", side_effect=OSError("permission denied")):
        check = _run_single_check("locked-cmd", str(tmp_path))
    assert check.cannot_validate is True
    assert check.passed is False


def test_cannot_validate_propagates_to_result(tmp_path: Path) -> None:
    """QAResult.cannot_validate=True when any check has cannot_validate=True."""
    with patch("subprocess.run", side_effect=FileNotFoundError("not found")):
        result = run_qa(str(tmp_path), checks=["missing-tool"], infrastructure_checks=[])
    assert result.cannot_validate is True
    assert result.passed is False


def test_timeout_is_not_cannot_validate(tmp_path: Path) -> None:
    """TimeoutExpired -> passed=False but cannot_validate=False (real failure, not env issue)."""
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 120)):
        check = _run_single_check("slow-cmd", str(tmp_path))
    assert check.passed is False
    assert check.cannot_validate is False


def test_crash_safety_in_handle_run_qa(tmp_path: Path) -> None:
    """_handle_run_qa never raises — returns valid QAResult JSON even when run_qa crashes."""
    import asyncio

    async def run() -> None:
        with patch("golem.tools.run_qa", side_effect=RuntimeError("runner exploded")):
            result = await _handle_run_qa({"worktree_path": str(tmp_path), "checks": []})
        text = result["content"][0]["text"]
        data = json.loads(text)
        assert data["cannot_validate"] is True
        assert data["stage"] == "crashed"
        assert data["passed"] is False

    asyncio.run(run())


def test_detect_infrastructure_checks_consolidated(tmp_path: Path) -> None:
    """The consolidated detect_infrastructure_checks detects ruff, mypy, npm lint, npm test, tsc, cargo test."""
    # ruff via pyproject.toml
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 120\n", encoding="utf-8")
    checks = detect_infrastructure_checks(tmp_path)
    assert "ruff check ." in checks

    # npm lint + npm test via package.json
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"lint": "eslint src", "test": "jest"}}), encoding="utf-8"
    )
    checks = detect_infrastructure_checks(tmp_path)
    assert "npm run lint" in checks
    assert "npm test" in checks

    # tsc via tsconfig.json
    (tmp_path / "tsconfig.json").write_text("{}", encoding="utf-8")
    checks = detect_infrastructure_checks(tmp_path)
    assert "npx tsc --noEmit" in checks

    # cargo test via Cargo.toml
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "test"\n', encoding="utf-8")
    checks = detect_infrastructure_checks(tmp_path)
    assert "cargo test" in checks
