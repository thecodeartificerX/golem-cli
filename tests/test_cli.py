from __future__ import annotations

import json
import tempfile
from pathlib import Path

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


def _write_ticket_json(tickets_dir: Path, ticket_id: str, title: str, status: str) -> None:
    """Helper to write a ticket JSON file."""
    data = {
        "id": ticket_id,
        "type": "task",
        "title": title,
        "status": status,
        "priority": "medium",
        "created_by": "planner",
        "assigned_to": "writer",
        "context": {
            "plan_file": "",
            "files": {},
            "references": [],
            "blueprint": "",
            "acceptance": [],
            "qa_checks": [],
            "parallelism_hints": [],
        },
        "history": [
            {
                "ts": "2026-03-27T10:00:00+00:00",
                "agent": "planner",
                "action": "created",
                "note": f"Ticket created: {title}",
                "attachments": [],
            }
        ],
    }
    (tickets_dir / f"{ticket_id}.json").write_text(
        json.dumps(data, indent=2), encoding="utf-8"
    )


def test_inspect_with_real_ticket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """golem inspect shows all ticket fields: blueprint, acceptance, qa_checks, files, references."""
    from typer.testing import CliRunner

    from golem.cli import app

    tickets_dir = tmp_path / ".golem" / "tickets"
    tickets_dir.mkdir(parents=True)

    data = {
        "id": "TICKET-001",
        "type": "task",
        "title": "Build auth module",
        "status": "in_progress",
        "priority": "high",
        "created_by": "planner",
        "assigned_to": "writer-1",
        "context": {
            "plan_file": "plans/001-auth.md",
            "files": {"src/auth.py": "# auth module"},
            "references": ["docs/auth-spec.md"],
            "blueprint": "Implement JWT-based authentication",
            "acceptance": ["Login endpoint returns 200", "Token expires in 1h"],
            "qa_checks": ["uv run pytest tests/test_auth.py"],
            "parallelism_hints": [],
        },
        "history": [
            {
                "ts": "2026-03-27T10:00:00+00:00",
                "agent": "planner",
                "action": "created",
                "note": "Ticket created: Build auth module",
                "attachments": [],
            },
            {
                "ts": "2026-03-27T10:05:00+00:00",
                "agent": "tech-lead",
                "action": "status_changed_to_in_progress",
                "note": "Assigned to writer-1",
                "attachments": [],
            },
        ],
    }
    (tickets_dir / "TICKET-001.json").write_text(
        json.dumps(data, indent=2), encoding="utf-8"
    )

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["inspect", "TICKET-001"])

    assert result.exit_code == 0
    assert "TICKET-001" in result.output
    assert "Build auth module" in result.output
    assert "in_progress" in result.output
    assert "high" in result.output
    assert "planner" in result.output
    assert "writer-1" in result.output
    # Context fields
    assert "plans/001-auth.md" in result.output
    assert "JWT-based" in result.output
    assert "Login endpoint" in result.output
    assert "Token expires" in result.output
    assert "uv run pytest" in result.output
    assert "docs/auth-spec.md" in result.output
    assert "src/auth.py" in result.output
    # History
    assert "created" in result.output
    assert "status_changed_to_in_progress" in result.output


def test_status_with_real_tickets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """golem status renders a table with ticket IDs, titles, and statuses."""
    from typer.testing import CliRunner

    from golem.cli import app

    tickets_dir = tmp_path / ".golem" / "tickets"
    tickets_dir.mkdir(parents=True)

    _write_ticket_json(tickets_dir, "TICKET-001", "Auth", "in_progress")
    _write_ticket_json(tickets_dir, "TICKET-002", "Logging", "pending")

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert "TICKET-001" in result.output
    assert "TICKET-002" in result.output
    assert "Auth" in result.output
    assert "Logging" in result.output
    assert "in_progress" in result.output
    assert "pending" in result.output
