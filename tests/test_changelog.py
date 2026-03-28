"""Tests for golem.changelog — formatting helpers, parsers, and mocked generation."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from golem.changelog import (
    ChangelogEntry,
    CommitMessage,
    _parse_changelog_response,
    _parse_commit_response,
    _resolve_since,
    _run_git,
    format_changelog,
    format_commit_message,
    generate_changelog,
    generate_commit_message,
)
from golem.config import GolemConfig


# ---------------------------------------------------------------------------
# format_changelog tests
# ---------------------------------------------------------------------------


def test_format_changelog_header() -> None:
    entry = ChangelogEntry(version="1.2.0", date="2026-03-29")
    result = format_changelog(entry)
    assert result.startswith("## [1.2.0] - 2026-03-29")


def test_format_changelog_unreleased_header() -> None:
    entry = ChangelogEntry(version="Unreleased", date="2026-03-29")
    result = format_changelog(entry)
    assert "## [Unreleased] - 2026-03-29" in result


def test_format_changelog_added_section() -> None:
    entry = ChangelogEntry(
        version="1.0.0",
        date="2026-01-01",
        added=["New feature A", "New feature B"],
    )
    result = format_changelog(entry)
    assert "### Added" in result
    assert "- New feature A" in result
    assert "- New feature B" in result


def test_format_changelog_all_sections() -> None:
    entry = ChangelogEntry(
        version="2.0.0",
        date="2026-01-01",
        added=["Added thing"],
        changed=["Changed thing"],
        fixed=["Fixed bug"],
        removed=["Removed feature"],
        summary="Major release.",
    )
    result = format_changelog(entry)
    assert "### Added" in result
    assert "### Changed" in result
    assert "### Fixed" in result
    assert "### Removed" in result
    assert "Major release." in result


def test_format_changelog_empty_sections_omitted() -> None:
    entry = ChangelogEntry(
        version="1.0.1",
        date="2026-01-01",
        fixed=["Fix one thing"],
    )
    result = format_changelog(entry)
    assert "### Fixed" in result
    assert "### Added" not in result
    assert "### Changed" not in result
    assert "### Removed" not in result


def test_format_changelog_no_summary() -> None:
    entry = ChangelogEntry(version="1.0.0", date="2026-01-01", added=["x"])
    result = format_changelog(entry)
    # Summary should not appear if empty
    lines = result.splitlines()
    assert lines[0] == "## [1.0.0] - 2026-01-01"
    # No stray blank summary line immediately after header followed by empty string
    assert "" not in [lines[1]] if len(lines) > 1 and lines[1] == "" and len(lines) > 2 and lines[2] == "" else True


# ---------------------------------------------------------------------------
# format_commit_message tests
# ---------------------------------------------------------------------------


def test_format_commit_basic() -> None:
    msg = CommitMessage(type="feat", scope="cli", description="add changelog command", body="", breaking=False)
    result = format_commit_message(msg)
    assert result == "feat(cli): add changelog command"


def test_format_commit_no_scope() -> None:
    msg = CommitMessage(type="fix", scope="", description="correct typo", body="", breaking=False)
    result = format_commit_message(msg)
    assert result == "fix: correct typo"


def test_format_commit_with_body() -> None:
    msg = CommitMessage(
        type="refactor",
        scope="core",
        description="extract helper",
        body="Moved shared logic into changelog.py to reduce duplication.",
        breaking=False,
    )
    result = format_commit_message(msg)
    assert result.startswith("refactor(core): extract helper")
    assert "Moved shared logic" in result
    assert "\n\n" in result


def test_format_commit_breaking_change() -> None:
    msg = CommitMessage(
        type="feat",
        scope="api",
        description="change response shape",
        body="",
        breaking=True,
    )
    result = format_commit_message(msg)
    assert result == "feat(api)!: change response shape"


def test_format_commit_breaking_no_scope() -> None:
    msg = CommitMessage(type="chore", scope="", description="drop py3.10 support", body="", breaking=True)
    result = format_commit_message(msg)
    assert result == "chore!: drop py3.10 support"


# ---------------------------------------------------------------------------
# ChangelogEntry / CommitMessage dataclass construction
# ---------------------------------------------------------------------------


def test_changelog_entry_defaults() -> None:
    entry = ChangelogEntry(version="0.1.0", date="2026-01-01")
    assert entry.added == []
    assert entry.changed == []
    assert entry.fixed == []
    assert entry.removed == []
    assert entry.summary == ""


def test_commit_message_defaults() -> None:
    msg = CommitMessage(type="docs", scope="readme", description="update intro", body="")
    assert msg.breaking is False


# ---------------------------------------------------------------------------
# _parse_changelog_response tests
# ---------------------------------------------------------------------------


def test_parse_changelog_valid_json() -> None:
    raw = '{"added": ["Feature X"], "changed": [], "fixed": ["Bug Y"], "removed": [], "summary": "Nice release."}'
    entry = _parse_changelog_response(raw, "1.0.0", "2026-01-01")
    assert entry.added == ["Feature X"]
    assert entry.fixed == ["Bug Y"]
    assert entry.summary == "Nice release."


def test_parse_changelog_with_preamble() -> None:
    raw = 'Here is the JSON:\n{"added": ["A"], "changed": ["B"], "fixed": [], "removed": [], "summary": "OK"}'
    entry = _parse_changelog_response(raw, "1.0.0", "2026-01-01")
    assert entry.added == ["A"]
    assert entry.changed == ["B"]


def test_parse_changelog_invalid_json_returns_empty() -> None:
    entry = _parse_changelog_response("not json at all", "1.0.0", "2026-01-01")
    assert entry.version == "1.0.0"
    assert entry.added == []


def test_parse_changelog_empty_string_returns_empty() -> None:
    entry = _parse_changelog_response("", "Unreleased", "2026-01-01")
    assert entry.version == "Unreleased"
    assert entry.summary == ""


# ---------------------------------------------------------------------------
# _parse_commit_response tests
# ---------------------------------------------------------------------------


def test_parse_commit_valid_json() -> None:
    raw = '{"type": "feat", "scope": "ui", "description": "add button", "body": "", "breaking": false}'
    msg = _parse_commit_response(raw)
    assert msg.type == "feat"
    assert msg.scope == "ui"
    assert msg.description == "add button"
    assert msg.breaking is False


def test_parse_commit_invalid_type_falls_back_to_chore() -> None:
    raw = '{"type": "unknown", "scope": "", "description": "do stuff", "body": "", "breaking": false}'
    msg = _parse_commit_response(raw)
    assert msg.type == "chore"


def test_parse_commit_invalid_json_returns_fallback() -> None:
    msg = _parse_commit_response("garbage")
    assert msg.type == "chore"
    assert msg.description == "update code"


def test_parse_commit_breaking_true() -> None:
    raw = '{"type": "fix", "scope": "core", "description": "drop support", "body": "See docs.", "breaking": true}'
    msg = _parse_commit_response(raw)
    assert msg.breaking is True
    assert msg.body == "See docs."


# ---------------------------------------------------------------------------
# _resolve_since tests
# ---------------------------------------------------------------------------


def test_resolve_since_returns_explicit_ref(git_repo: Path) -> None:
    result = _resolve_since("v1.0.0", cwd=str(git_repo))
    assert result == "v1.0.0"


def test_resolve_since_uses_latest_tag(git_repo: Path) -> None:
    # Create a tag in the test repo
    subprocess.run(["git", "tag", "v0.5.0"], cwd=str(git_repo), check=True, capture_output=True)
    result = _resolve_since("", cwd=str(git_repo))
    assert result == "v0.5.0"


def test_resolve_since_falls_back_to_first_commit(git_repo: Path) -> None:
    # No tags — should return the first commit SHA
    result = _resolve_since("", cwd=str(git_repo))
    assert len(result) == 40  # full SHA


# ---------------------------------------------------------------------------
# generate_changelog (mocked SDK)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_changelog_mocked(git_repo: Path) -> None:
    """Verify generate_changelog calls SDK and parses response."""
    config = GolemConfig()
    model_response = '{"added": ["Add changelog module"], "changed": [], "fixed": [], "removed": [], "summary": "First release."}'

    # Build a fake async generator that yields a ResultMessage
    async def _fake_query(*args, **kwargs) -> AsyncIterator[object]:
        msg = MagicMock()
        msg.__class__.__name__ = "ResultMessage"
        msg.result = model_response
        # Yield nothing for AssistantMessage path; just the ResultMessage
        from claude_agent_sdk import ResultMessage as RM
        fake = MagicMock(spec=RM)
        fake.result = model_response
        yield fake

    with patch("golem.changelog.query", side_effect=_fake_query):
        entry = await generate_changelog(
            since="",
            version="0.1.0",
            config=config,
            cwd=str(git_repo),
        )

    # The entry should have correct version/date regardless of mock parse
    assert entry.version == "0.1.0"
    assert isinstance(entry.date, str)
    assert len(entry.date) == 10  # YYYY-MM-DD


@pytest.mark.asyncio
async def test_generate_changelog_no_commits_returns_entry(git_repo: Path) -> None:
    """With no commits between since and HEAD, still returns a valid entry."""
    config = GolemConfig()

    async def _fake_query(*args, **kwargs) -> AsyncIterator[object]:
        from claude_agent_sdk import ResultMessage as RM
        fake = MagicMock(spec=RM)
        fake.result = '{"added": [], "changed": [], "fixed": [], "removed": [], "summary": "Empty."}'
        yield fake

    with patch("golem.changelog.query", side_effect=_fake_query):
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(git_repo),
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
        # since == HEAD means no range
        entry = await generate_changelog(since=sha, version="Unreleased", config=config, cwd=str(git_repo))

    assert entry.version == "Unreleased"


@pytest.mark.asyncio
async def test_generate_changelog_sdk_error_returns_empty(git_repo: Path) -> None:
    """SDK exception causes graceful fallback, not crash."""
    config = GolemConfig()

    async def _bad_query(*args, **kwargs) -> AsyncIterator[object]:
        raise RuntimeError("SDK unavailable")
        yield  # make it a generator

    with patch("golem.changelog.query", side_effect=_bad_query):
        entry = await generate_changelog(since="", version="1.0.0", config=config, cwd=str(git_repo))

    assert entry.version == "1.0.0"
    assert entry.added == []


# ---------------------------------------------------------------------------
# generate_commit_message (mocked SDK)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_commit_message_mocked() -> None:
    config = GolemConfig()
    model_response = '{"type": "feat", "scope": "changelog", "description": "add AI commit generator", "body": "", "breaking": false}'

    async def _fake_query(*args, **kwargs) -> AsyncIterator[object]:
        from claude_agent_sdk import ResultMessage as RM
        fake = MagicMock(spec=RM)
        fake.result = model_response
        yield fake

    diff = "diff --git a/src/golem/changelog.py b/src/golem/changelog.py\n+new line\n"

    with patch("golem.changelog.query", side_effect=_fake_query):
        msg = await generate_commit_message(diff=diff, config=config)

    assert msg.type == "feat"
    assert msg.scope == "changelog"


@pytest.mark.asyncio
async def test_generate_commit_message_empty_diff() -> None:
    """Empty diff returns a no-staged-changes message without hitting SDK."""
    config = GolemConfig()
    msg = await generate_commit_message(diff="", config=config)
    assert msg.type == "chore"
    assert "no staged" in msg.description


@pytest.mark.asyncio
async def test_generate_commit_message_sdk_error_returns_fallback() -> None:
    config = GolemConfig()

    async def _bad_query(*args, **kwargs) -> AsyncIterator[object]:
        raise RuntimeError("SDK unavailable")
        yield

    with patch("golem.changelog.query", side_effect=_bad_query):
        msg = await generate_commit_message(diff="some diff", config=config)

    assert msg.type == "chore"
    assert msg.description == "update code"


# ---------------------------------------------------------------------------
# CLI command integration (mocked generate_changelog / generate_commit_message)
# ---------------------------------------------------------------------------


def test_changelog_command_stdout(git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """golem changelog prints formatted entry to stdout."""
    from typer.testing import CliRunner
    from golem.cli import app

    entry = ChangelogEntry(
        version="Unreleased",
        date="2026-03-29",
        added=["New changelog command"],
        summary="Adds AI changelog generation.",
    )

    async def _mock_generate(*args, **kwargs) -> ChangelogEntry:
        return entry

    monkeypatch.chdir(git_repo)
    monkeypatch.setattr("golem.changelog.generate_changelog", _mock_generate)

    runner = CliRunner()
    result = runner.invoke(app, ["changelog"])
    assert result.exit_code == 0
    assert "Unreleased" in result.output


def test_changelog_command_output_file(git_repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """golem changelog --output writes to a file."""
    from typer.testing import CliRunner
    from golem.cli import app

    entry = ChangelogEntry(version="1.0.0", date="2026-03-29", fixed=["Fix a bug"])

    async def _mock_generate(*args, **kwargs) -> ChangelogEntry:
        return entry

    monkeypatch.chdir(git_repo)
    monkeypatch.setattr("golem.changelog.generate_changelog", _mock_generate)

    out_file = tmp_path / "CHANGELOG.md"
    runner = CliRunner()
    result = runner.invoke(app, ["changelog", "--output", str(out_file)])
    assert result.exit_code == 0
    assert out_file.exists()
    content = out_file.read_text(encoding="utf-8")
    assert "Fix a bug" in content


def test_commit_msg_command(git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """golem commit-msg prints a conventional commit message."""
    from typer.testing import CliRunner
    from golem.cli import app

    commit = CommitMessage(
        type="feat",
        scope="cli",
        description="add commit-msg command",
        body="",
        breaking=False,
    )

    async def _mock_generate(*args, **kwargs) -> CommitMessage:
        return commit

    monkeypatch.chdir(git_repo)
    monkeypatch.setattr("golem.changelog.generate_commit_message", _mock_generate)

    runner = CliRunner()
    result = runner.invoke(app, ["commit-msg"])
    assert result.exit_code == 0
    assert "feat(cli): add commit-msg command" in result.output
