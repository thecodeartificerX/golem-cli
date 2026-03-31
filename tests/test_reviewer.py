from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from golem.config import GolemConfig
from golem.reviewer import ReviewVerdict, _build_reviewer_prompt, _parse_verdict, run_reviewer
from golem.tickets import Ticket, TicketContext
from golem.writer import spawn_writer_pair


# ---------------------------------------------------------------------------
# _build_reviewer_prompt
# ---------------------------------------------------------------------------


def test_build_reviewer_prompt_renders_all_sections() -> None:
    prompt = _build_reviewer_prompt(
        diff_text="diff --git a/foo.py\n+print('hello')",
        ticket_title="Add greeting",
        plan_section="## Step 1\nAdd a greeting to foo.py",
        acceptance_criteria=["greeting is printed", "no errors"],
        claude_md="## Coding Conventions\n- Use utf-8",
    )
    assert "Add greeting" in prompt
    assert "diff --git a/foo.py" in prompt
    assert "Step 1" in prompt
    assert "greeting is printed" in prompt
    assert "Use utf-8" in prompt


def test_build_reviewer_prompt_empty_claude_md() -> None:
    prompt = _build_reviewer_prompt(
        diff_text="some diff",
        ticket_title="Test",
        plan_section="",
        acceptance_criteria=[],
        claude_md="",
    )
    assert "No project conventions file found" in prompt
    assert "None specified" in prompt


def test_build_reviewer_prompt_no_leftover_placeholders() -> None:
    prompt = _build_reviewer_prompt(
        diff_text="diff content",
        ticket_title="Title",
        plan_section="Plan",
        acceptance_criteria=["criterion"],
        claude_md="conventions",
    )
    import re
    leftover = re.findall(r"\{[a-z_]+\}", prompt)
    assert leftover == [], f"Unresolved placeholders: {leftover}"


# ---------------------------------------------------------------------------
# _parse_verdict — all 3 decision types
# ---------------------------------------------------------------------------


def test_parse_verdict_approve() -> None:
    text = """DECISION: approve

CRITICAL:
- None

IMPORTANT:
- None

MINOR:
- Consider renaming variable x to count

SUMMARY: Code looks good, all acceptance criteria addressed."""
    verdict = _parse_verdict(text)
    assert verdict.decision == "approve"
    assert verdict.critical_issues == []
    assert verdict.important_issues == []
    assert len(verdict.minor_issues) == 1
    assert "renaming" in verdict.minor_issues[0]
    assert "acceptance criteria" in verdict.summary


def test_parse_verdict_warning() -> None:
    text = """DECISION: warning

CRITICAL:
- None

IMPORTANT:
- Missing error handling in parse_config()
- No null check before accessing user.name

MINOR:
- None

SUMMARY: Minor issues found but no blockers."""
    verdict = _parse_verdict(text)
    assert verdict.decision == "warning"
    assert verdict.critical_issues == []
    assert len(verdict.important_issues) == 2
    assert "error handling" in verdict.important_issues[0]
    assert verdict.minor_issues == []


def test_parse_verdict_block() -> None:
    text = """DECISION: block

CRITICAL:
- Hardcoded API key in config.py line 42
- SQL injection vulnerability in query builder

IMPORTANT:
- Missing encoding="utf-8" on open() call

MINOR:
- None

SUMMARY: Security issues must be resolved before merge."""
    verdict = _parse_verdict(text)
    assert verdict.decision == "block"
    assert len(verdict.critical_issues) == 2
    assert "API key" in verdict.critical_issues[0]
    assert "SQL injection" in verdict.critical_issues[1]
    assert len(verdict.important_issues) == 1
    assert "Security" in verdict.summary


def test_parse_verdict_empty_text_defaults_to_approve() -> None:
    verdict = _parse_verdict("")
    assert verdict.decision == "approve"
    assert verdict.critical_issues == []
    assert verdict.summary == ""


def test_parse_verdict_malformed_text_defaults_to_approve() -> None:
    verdict = _parse_verdict("This is just random text with no structure.")
    assert verdict.decision == "approve"


# ---------------------------------------------------------------------------
# Reviewer integration in spawn_writer_pair — block triggers rework
# ---------------------------------------------------------------------------


def _make_ticket(skip_review: bool = False) -> Ticket:
    ctx = TicketContext(
        plan_file="",
        files={},
        references=[],
        blueprint="",
        acceptance=["it works"],
        qa_checks=[],
        parallelism_hints=[],
        skip_review=skip_review,
    )
    return Ticket(
        id="TICKET-001",
        type="task",
        title="Test ticket",
        status="pending",
        priority="medium",
        created_by="tech_lead",
        assigned_to="writer",
        context=ctx,
    )


@pytest.mark.asyncio
async def test_block_verdict_triggers_rework() -> None:
    """When reviewer returns block, spawn_writer_pair should call the writer a second time with rework context."""
    call_count = 0

    async def fake_query(prompt, options=None, **kwargs):
        nonlocal call_count
        call_count += 1
        return
        yield  # noqa: unreachable — makes this an async generator

    block_verdict = ReviewVerdict(
        decision="block",
        critical_issues=["Hardcoded secret in line 10"],
        important_issues=[],
        minor_issues=[],
        summary="Security issue found.",
    )

    with (
        patch("golem.writer.query", side_effect=fake_query),
        patch("golem.writer.run_reviewer", return_value=block_verdict),
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            golem_dir = Path(tmpdir) / ".golem"
            (golem_dir / "tickets").mkdir(parents=True)
            ticket = _make_ticket()
            await spawn_writer_pair(ticket, tmpdir, GolemConfig(), golem_dir=golem_dir)

    # Writer should be called twice: initial + rework
    assert call_count == 2


@pytest.mark.asyncio
async def test_warning_verdict_proceeds_with_notes() -> None:
    """When reviewer returns warning, spawn_writer_pair should proceed and attach warnings."""
    async def fake_query(prompt, options=None, **kwargs):
        return
        yield  # noqa: unreachable

    warning_verdict = ReviewVerdict(
        decision="warning",
        critical_issues=[],
        important_issues=["Missing null check"],
        minor_issues=[],
        summary="Minor issues.",
    )

    with (
        patch("golem.writer.query", side_effect=fake_query),
        patch("golem.writer.run_reviewer", return_value=warning_verdict),
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            golem_dir = Path(tmpdir) / ".golem"
            (golem_dir / "tickets").mkdir(parents=True)
            ticket = _make_ticket()
            result = await spawn_writer_pair(ticket, tmpdir, GolemConfig(), golem_dir=golem_dir)

    assert "REVIEWER WARNINGS" in result
    assert "Missing null check" in result


@pytest.mark.asyncio
async def test_trivial_tier_skips_reviewer() -> None:
    """When skip_review is True, the reviewer should not be called."""
    async def fake_query(prompt, options=None, **kwargs):
        return
        yield  # noqa: unreachable

    with (
        patch("golem.writer.query", side_effect=fake_query),
        patch("golem.writer.run_reviewer") as mock_reviewer,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            golem_dir = Path(tmpdir) / ".golem"
            (golem_dir / "tickets").mkdir(parents=True)
            ticket = _make_ticket(skip_review=True)
            await spawn_writer_pair(ticket, tmpdir, GolemConfig(), golem_dir=golem_dir)

    mock_reviewer.assert_not_called()


@pytest.mark.asyncio
async def test_approve_verdict_proceeds_normally() -> None:
    """When reviewer returns approve, spawn_writer_pair returns without rework."""
    call_count = 0

    async def fake_query(prompt, options=None, **kwargs):
        nonlocal call_count
        call_count += 1
        return
        yield  # noqa: unreachable

    approve_verdict = ReviewVerdict(
        decision="approve",
        critical_issues=[],
        important_issues=[],
        minor_issues=[],
        summary="All good.",
    )

    with (
        patch("golem.writer.query", side_effect=fake_query),
        patch("golem.writer.run_reviewer", return_value=approve_verdict),
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            golem_dir = Path(tmpdir) / ".golem"
            (golem_dir / "tickets").mkdir(parents=True)
            ticket = _make_ticket()
            result = await spawn_writer_pair(ticket, tmpdir, GolemConfig(), golem_dir=golem_dir)

    # Writer should be called only once (no rework)
    assert call_count == 1
    assert "REVIEWER WARNINGS" not in result


@pytest.mark.asyncio
async def test_reviewer_error_does_not_block_pipeline() -> None:
    """If the reviewer raises an exception, spawn_writer_pair should proceed without blocking."""
    async def fake_query(prompt, options=None, **kwargs):
        return
        yield  # noqa: unreachable

    with (
        patch("golem.writer.query", side_effect=fake_query),
        patch("golem.writer.run_reviewer", side_effect=RuntimeError("SDK connection failed")),
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            golem_dir = Path(tmpdir) / ".golem"
            (golem_dir / "tickets").mkdir(parents=True)
            ticket = _make_ticket()
            # Should not raise
            result = await spawn_writer_pair(ticket, tmpdir, GolemConfig(), golem_dir=golem_dir)

    # Should return normally despite reviewer failure
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Config fields
# ---------------------------------------------------------------------------


def test_config_has_reviewer_fields() -> None:
    config = GolemConfig()
    assert config.reviewer_model == "claude-sonnet-4-6"
    assert config.reviewer_budget_usd == 0.25


def test_config_validates_reviewer_model() -> None:
    config = GolemConfig(reviewer_model="gpt-4o")
    warnings = config.validate()
    assert any("reviewer_model" in w for w in warnings)


# ---------------------------------------------------------------------------
# TicketContext skip_review field
# ---------------------------------------------------------------------------


def test_ticket_context_skip_review_default_false() -> None:
    ctx = TicketContext()
    assert ctx.skip_review is False


def test_ticket_context_skip_review_true() -> None:
    ctx = TicketContext(skip_review=True)
    assert ctx.skip_review is True
