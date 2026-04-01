from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from click.exceptions import Exit as ClickExit

from golem.cli import _detect_infrastructure_checks, _resolve_spec_project_root, _validate_spec


def test_validate_spec_nonexistent_exits() -> None:
    with pytest.raises(ClickExit):
        _validate_spec(Path("/nonexistent/spec.md"))


def test_validate_spec_wrong_extension_exits() -> None:
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
        f.write(b"some content here with enough length to pass size check")
    with pytest.raises(ClickExit):
        _validate_spec(Path(f.name))


def test_validate_spec_empty_exits() -> None:
    with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w", encoding="utf-8") as f:
        f.write("")
    with pytest.raises(ClickExit):
        _validate_spec(Path(f.name))


def test_validate_spec_valid_passes() -> None:
    with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w", encoding="utf-8") as f:
        f.write("# My Spec\n\n## Task 1\n\nDo the thing.\n\n## Task 2\n\nDo the other thing.\n")
    # Should not raise
    _validate_spec(Path(f.name))


def test_validate_spec_short_warns(capsys: pytest.CaptureFixture[str]) -> None:
    with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w", encoding="utf-8") as f:
        f.write("# Short\nTiny spec.")  # 19 chars, < 50
    # Should not raise, just warn
    _validate_spec(Path(f.name))
    captured = capsys.readouterr()
    assert "very short" in captured.out


def test_validate_spec_no_structure_warns(capsys: pytest.CaptureFixture[str]) -> None:
    with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w", encoding="utf-8") as f:
        f.write("This is just a paragraph of text without any headings or task markers at all and it is long enough.")
    _validate_spec(Path(f.name))
    captured = capsys.readouterr()
    assert "no headings" in captured.out


def test_detect_infrastructure_checks_finds_ruff() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        pyproject = Path(tmpdir) / "pyproject.toml"
        pyproject.write_text("[tool.ruff]\nline-length = 120\n", encoding="utf-8")
        checks = _detect_infrastructure_checks(Path(tmpdir))
        assert "ruff check ." in checks


def test_detect_infrastructure_checks_finds_npm_lint() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        pkg = Path(tmpdir) / "package.json"
        pkg.write_text(json.dumps({"scripts": {"lint": "eslint ."}}), encoding="utf-8")
        checks = _detect_infrastructure_checks(Path(tmpdir))
        assert "npm run lint" in checks


def test_detect_infrastructure_checks_finds_npm_typecheck() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        pkg = Path(tmpdir) / "package.json"
        pkg.write_text(json.dumps({"scripts": {"typecheck": "tsc --noEmit"}}), encoding="utf-8")
        checks = _detect_infrastructure_checks(Path(tmpdir))
        assert "npm run typecheck" in checks


def test_detect_infrastructure_checks_finds_tsc() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tsconfig = Path(tmpdir) / "tsconfig.json"
        tsconfig.write_text("{}", encoding="utf-8")
        checks = _detect_infrastructure_checks(Path(tmpdir))
        assert "tsc --noEmit" in checks


def test_detect_infrastructure_checks_empty_project() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        checks = _detect_infrastructure_checks(Path(tmpdir))
        assert checks == []


def test_run_cli_nonexistent_spec_exits() -> None:
    """Running golem run with a nonexistent spec should exit with error."""
    from typer.testing import CliRunner

    from golem.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["run", "/nonexistent/spec.md"])
    assert result.exit_code != 0


def test_no_command_shows_help() -> None:
    from typer.testing import CliRunner

    from golem.cli import app

    runner = CliRunner()
    result = runner.invoke(app, [])
    assert result.exit_code in (0, 2)  # typer may return 2 for help display
    assert "Usage" in result.output or "golem" in result.output


def test_run_cli_shows_version_banner() -> None:
    """golem run should print version banner even on error."""
    from typer.testing import CliRunner

    from golem.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["run", "/nonexistent/spec.md"])
    assert "Golem" in result.output
    assert "v0.2.0" in result.output or "ticket-driven" in result.output


def test_plan_cli_nonexistent_spec_exits() -> None:
    """Running golem plan with a nonexistent spec should exit with error."""
    from typer.testing import CliRunner

    from golem.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["plan", "/nonexistent/spec.md"])
    assert result.exit_code != 0


def test_detect_infrastructure_checks_finds_ruff_toml() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        ruff_toml = Path(tmpdir) / "ruff.toml"
        ruff_toml.write_text('line-length = 100\n', encoding="utf-8")
        checks = _detect_infrastructure_checks(Path(tmpdir))
        assert "ruff check ." in checks


def test_resolve_spec_project_root_finds_git() -> None:
    import subprocess

    with tempfile.TemporaryDirectory() as tmpdir:
        project = Path(tmpdir) / "project"
        project.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=project, check=True, capture_output=True)
        docs = project / "docs"
        docs.mkdir()
        spec = docs / "spec.md"
        spec.write_text("# Spec", encoding="utf-8")
        result = _resolve_spec_project_root(spec)
        assert result == project.resolve()


def test_resolve_spec_project_root_no_git_uses_parent() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        spec = Path(tmpdir) / "spec.md"
        spec.write_text("# Spec", encoding="utf-8")
        result = _resolve_spec_project_root(spec)
        assert result == spec.resolve().parent


def test_version_cli_shows_version() -> None:
    from typer.testing import CliRunner

    from golem.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "0.2.0" in result.output


def test_status_cli_no_golem_dir_exits_cleanly() -> None:
    from typer.testing import CliRunner

    from golem.cli import app

    runner = CliRunner()
    # Run from a temp dir with no .golem/
    result = runner.invoke(app, ["status"])
    # Should exit 0 with friendly message, not crash
    assert result.exit_code == 0
    assert "No active run" in result.output or "no active" in result.output.lower()


def test_history_cli_no_golem_dir_exits_cleanly() -> None:
    from typer.testing import CliRunner

    from golem.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["history"])
    assert result.exit_code == 0
    assert "no active" in result.output.lower()


def test_clean_cli_no_golem_dir_exits_cleanly() -> None:
    from typer.testing import CliRunner

    from golem.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["clean", "--force"])
    assert result.exit_code == 0
    assert "nothing to clean" in result.output.lower()


def test_logs_cli_no_progress_log_exits() -> None:
    from typer.testing import CliRunner

    from golem.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["logs"])
    assert result.exit_code != 0 or "no progress" in result.output.lower()


def test_inspect_cli_no_golem_dir_exits_cleanly() -> None:
    from typer.testing import CliRunner

    from golem.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["inspect", "TICKET-001"])
    assert result.exit_code == 0
    assert "no active" in result.output.lower()


def test_resume_cli_no_tickets_exits_cleanly() -> None:
    from typer.testing import CliRunner

    from golem.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["resume"])
    # Should exit with error since no tickets dir exists
    assert result.exit_code != 0 or "no tickets" in result.output.lower()


def test_run_gitinit_skipped_when_repo_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """golem run with an existing git repo must NOT reinitialize it."""
    from typer.testing import CliRunner

    from golem.cli import app

    # Set up a real git repo with one commit
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True)
    spec = tmp_path / "spec.md"
    spec.write_text("# Spec\n\n## Task\n\nDo the thing.\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)

    # Record commit count before the run
    log_before = subprocess.run(
        ["git", "log", "--oneline"], cwd=tmp_path, capture_output=True, text=True, check=True
    )
    commit_count_before = len(log_before.stdout.strip().splitlines())

    # Patch run_planner to stop execution cleanly after git-init check
    async def _noop_planner(*args: object, **kwargs: object) -> str:
        raise RuntimeError("test stop — planner not needed")

    monkeypatch.setattr("golem.cli.run_planner", _noop_planner)
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    result = runner.invoke(app, ["run", str(spec), "--force"])

    # .git must still exist and commit count must not have grown (no bootstrap commit)
    assert (tmp_path / ".git").exists()
    log_after = subprocess.run(
        ["git", "log", "--oneline"], cwd=tmp_path, capture_output=True, text=True, check=True
    )
    commit_count_after = len(log_after.stdout.strip().splitlines())
    assert commit_count_after == commit_count_before
    assert "Initializing" not in result.output


def test_run_gitinit_creates_repo_for_greenfield(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """golem run must auto-initialize a git repo when none exists."""
    from typer.testing import CliRunner

    from golem.cli import app

    spec = tmp_path / "spec.md"
    spec.write_text("# Spec\n\n## Task\n\nDo the thing.\n", encoding="utf-8")

    # Patch run_planner to stop execution after git-init block runs
    async def _noop_planner(*args: object, **kwargs: object) -> str:
        raise RuntimeError("test stop — planner not needed")

    monkeypatch.setattr("golem.cli.run_planner", _noop_planner)
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    result = runner.invoke(app, ["run", str(spec), "--force"])

    # The git repo must have been created
    assert (tmp_path / ".git").exists()
    assert "Initializing" in result.output
    assert "Git repository initialized" in result.output


def test_run_gitinit_exit_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """golem run must exit 1 with a red error when git init fails."""
    from typer.testing import CliRunner

    from golem.cli import app

    spec = tmp_path / "spec.md"
    spec.write_text("# Spec\n\n## Task\n\nDo the thing.\n", encoding="utf-8")

    # Patch subprocess.run inside the cli module to raise CalledProcessError for git init
    original_run = subprocess.run

    def _failing_subprocess(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if cmd[:3] == ["git", "init", "-b"]:
            raise subprocess.CalledProcessError(128, cmd, stderr=b"fatal: not a valid git repo")
        return original_run(cmd, **kwargs)  # type: ignore[return-value]

    monkeypatch.setattr("subprocess.run", _failing_subprocess)
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    result = runner.invoke(app, ["run", str(spec), "--force"])

    assert result.exit_code == 1
    assert "Failed to initialize git repository" in result.output
