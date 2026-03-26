from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from click.exceptions import Exit as ClickExit

from golem.cli import _create_golem_dirs, _detect_infrastructure_checks, _get_golem_dir, _get_project_root, _resolve_spec_project_root, _validate_spec


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


def test_config_show_prints_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """golem config show prints config as JSON."""
    from typer.testing import CliRunner

    from golem.cli import app

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["config", "show"])

    assert result.exit_code == 0
    assert "max_parallel" in result.output
    assert "planner_model" in result.output


def test_config_reset_removes_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """golem config reset --force deletes config.json."""
    from typer.testing import CliRunner

    from golem.cli import app

    golem_dir = tmp_path / ".golem"
    golem_dir.mkdir()
    config_path = golem_dir / "config.json"
    config_path.write_text('{"max_parallel": 5}', encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["config", "reset", "--force"])

    assert result.exit_code == 0
    assert "reset" in result.output.lower() or "defaults" in result.output.lower()
    assert not config_path.exists()


def test_config_reset_no_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """golem config reset when no config exists shows friendly message."""
    from typer.testing import CliRunner

    from golem.cli import app

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["config", "reset", "--force"])

    assert result.exit_code == 0
    assert "defaults" in result.output.lower()


def test_inspect_invalid_ticket_id_format(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """golem inspect with invalid ID format shows format error."""
    from typer.testing import CliRunner

    from golem.cli import app

    tickets_dir = tmp_path / ".golem" / "tickets"
    tickets_dir.mkdir(parents=True)

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["inspect", "foo"])

    assert result.exit_code != 0
    assert "invalid ticket id" in result.output.lower() or "TICKET-NNN" in result.output


def test_inspect_corrupt_json_exits_cleanly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """golem inspect with corrupt ticket JSON shows error, not raw traceback."""
    from typer.testing import CliRunner

    from golem.cli import app

    tickets_dir = tmp_path / ".golem" / "tickets"
    tickets_dir.mkdir(parents=True)
    (tickets_dir / "TICKET-001.json").write_text("{corrupt!!!", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["inspect", "TICKET-001"])

    assert result.exit_code != 0
    assert "corrupt" in result.output.lower()


def test_version_test_count_matches_pytest(monkeypatch: pytest.MonkeyPatch) -> None:
    """golem version test count matches actual pytest collection count."""
    import subprocess

    from typer.testing import CliRunner

    from golem.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0

    # Extract test count from version output
    for line in result.output.splitlines():
        if line.strip().startswith("Tests"):
            reported_count = int(line.strip().split()[-1])
            break
    else:
        pytest.fail("No 'Tests' line found in version output")

    # Get actual count from pytest --co -q
    proc = subprocess.run(
        ["uv", "run", "pytest", "--co", "-q"],
        capture_output=True, text=True, encoding="utf-8",
    )
    # Last non-empty line is like "205 tests collected"
    for line in reversed(proc.stdout.strip().splitlines()):
        if "test" in line and "collected" in line:
            actual_count = int(line.split()[0])
            break
    else:
        pytest.fail(f"Could not parse pytest collection count from: {proc.stdout}")

    assert reported_count == actual_count


def test_create_golem_dirs_all_subdirs(tmp_path: Path) -> None:
    """_create_golem_dirs creates all 6 expected subdirectories."""
    golem_dir = tmp_path / ".golem"
    _create_golem_dirs(golem_dir)

    expected = {"tickets", "research", "plans", "references", "reports", "worktrees"}
    actual = {d.name for d in golem_dir.iterdir() if d.is_dir()}
    assert actual == expected


def test_get_golem_dir_returns_dotgolem(tmp_path: Path) -> None:
    """_get_golem_dir returns <project_root>/.golem."""
    result = _get_golem_dir(tmp_path)
    assert result == tmp_path / ".golem"


def test_get_project_root_returns_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """_get_project_root returns Path.cwd()."""
    monkeypatch.chdir(tmp_path)
    result = _get_project_root()
    assert result == Path.cwd()


def test_run_stale_state_blocks_without_force(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """golem run exits with warning when stale .golem/tickets/ exist and no --force."""
    from typer.testing import CliRunner

    from golem.cli import app

    # Create a valid spec
    spec = tmp_path / "spec.md"
    spec.write_text("# Spec\n\n## Task 1\n\nDo the thing.\n\n## Task 2\n\nDo the other.\n", encoding="utf-8")

    # Create stale .golem/ state
    tickets_dir = tmp_path / ".golem" / "tickets"
    tickets_dir.mkdir(parents=True)
    _write_ticket_json(tickets_dir, "TICKET-001", "Old task", "done")

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["run", str(spec)])

    assert result.exit_code != 0
    assert "existing tickets" in result.output.lower() or "previous run" in result.output.lower()


def test_run_stale_state_force_proceeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """golem run --force overwrites stale state (fails on planner, not stale check)."""
    from unittest.mock import AsyncMock

    from typer.testing import CliRunner

    from golem.cli import app

    spec = tmp_path / "spec.md"
    spec.write_text("# Spec\n\n## Task 1\n\nDo the thing.\n\n## Task 2\n\nDo the other.\n", encoding="utf-8")

    tickets_dir = tmp_path / ".golem" / "tickets"
    tickets_dir.mkdir(parents=True)
    _write_ticket_json(tickets_dir, "TICKET-001", "Old task", "done")

    # Mock the planner so it doesn't try to start the SDK
    mock_planner = AsyncMock(side_effect=RuntimeError("mock planner"))
    monkeypatch.setattr("golem.cli.run_planner", mock_planner)

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["run", str(spec), "--force"])

    # Should NOT fail on stale state — should print overwriting message then fail on planner
    assert "overwriting" in result.output.lower()
    # Old .golem/tickets/ should have been removed
    assert not (tmp_path / ".golem" / "tickets" / "TICKET-001.json").exists()


def test_logs_with_existing_progress_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """golem logs prints progress.log entries."""
    from typer.testing import CliRunner

    from golem.cli import app

    golem_dir = tmp_path / ".golem"
    golem_dir.mkdir()
    log_path = golem_dir / "progress.log"
    log_path.write_text(
        "2026-03-27 10:00:00 | PLANNER_START | spec=test.md\n"
        "2026-03-27 10:05:00 | PLANNER_COMPLETE | tickets=3\n"
        "2026-03-27 10:06:00 | TECH_LEAD_START | ticket=TICKET-001\n",
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["logs"])

    assert result.exit_code == 0
    assert "PLANNER_START" in result.output
    assert "PLANNER_COMPLETE" in result.output
    assert "TECH_LEAD_START" in result.output


def test_clean_with_real_golem_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """golem clean --force removes .golem/ directory and golem/* branches."""
    import subprocess

    from typer.testing import CliRunner

    from golem.cli import app

    # Set up a git repo
    repo = tmp_path / "project"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("init", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

    # Create .golem/ with tickets
    golem_dir = repo / ".golem"
    tickets_dir = golem_dir / "tickets"
    tickets_dir.mkdir(parents=True)
    _write_ticket_json(tickets_dir, "TICKET-001", "Task A", "pending")
    _write_ticket_json(tickets_dir, "TICKET-002", "Task B", "done")
    (golem_dir / "plans").mkdir()
    (golem_dir / "plans" / "overview.md").write_text("# Plan", encoding="utf-8")

    # Create a golem/* branch
    subprocess.run(["git", "branch", "golem/spec/group-a"], cwd=repo, check=True, capture_output=True)

    monkeypatch.chdir(repo)
    runner = CliRunner()
    result = runner.invoke(app, ["clean", "--force"])

    assert result.exit_code == 0
    assert "Cleaned" in result.output
    assert "2 ticket(s)" in result.output
    assert "1 plan(s)" in result.output
    assert "1 golem branch" in result.output
    # .golem/ should be gone
    assert not golem_dir.exists()
    # golem/* branches should be deleted
    branch_check = subprocess.run(
        ["git", "branch", "--list", "golem/*"], cwd=repo, capture_output=True, text=True
    )
    assert branch_check.stdout.strip() == ""


def test_history_with_ticket_events(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """golem history shows events chronologically across tickets."""
    from typer.testing import CliRunner

    from golem.cli import app

    tickets_dir = tmp_path / ".golem" / "tickets"
    tickets_dir.mkdir(parents=True)

    # Ticket 1 with two events
    data1 = {
        "id": "TICKET-001",
        "type": "task",
        "title": "First task",
        "status": "done",
        "priority": "high",
        "created_by": "planner",
        "assigned_to": "writer",
        "context": {"plan_file": "", "files": {}, "references": [], "blueprint": "",
                     "acceptance": [], "qa_checks": [], "parallelism_hints": []},
        "history": [
            {"ts": "2026-03-27T08:00:00+00:00", "agent": "planner", "action": "created",
             "note": "Ticket created: First task", "attachments": []},
            {"ts": "2026-03-27T12:00:00+00:00", "agent": "writer", "action": "done",
             "note": "Work complete", "attachments": []},
        ],
    }
    # Ticket 2 with one event (timestamp between ticket 1's events)
    data2 = {
        "id": "TICKET-002",
        "type": "task",
        "title": "Second task",
        "status": "pending",
        "priority": "low",
        "created_by": "planner",
        "assigned_to": "writer",
        "context": {"plan_file": "", "files": {}, "references": [], "blueprint": "",
                     "acceptance": [], "qa_checks": [], "parallelism_hints": []},
        "history": [
            {"ts": "2026-03-27T10:00:00+00:00", "agent": "planner", "action": "created",
             "note": "Ticket created: Second task", "attachments": []},
        ],
    }
    (tickets_dir / "TICKET-001.json").write_text(json.dumps(data1, indent=2), encoding="utf-8")
    (tickets_dir / "TICKET-002.json").write_text(json.dumps(data2, indent=2), encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["history"])

    assert result.exit_code == 0
    assert "TICKET-001" in result.output
    assert "TICKET-002" in result.output
    assert "created" in result.output
    # Verify chronological ordering: 08:00 then 10:00 then 12:00
    # Rich may truncate timestamps, so check rows appear in ticket order
    first_t001 = result.output.index("TICKET-001")
    t002_pos = result.output.index("TICKET-002")
    # TICKET-001 created at 08:00 should appear before TICKET-002 created at 10:00
    assert first_t001 < t002_pos
    # Summary line
    assert "3 event(s)" in result.output
    assert "2 ticket(s)" in result.output


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
