from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

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
# Reviewer integration in spawn_writer_pair (spawn_junior_dev alias)
# ---------------------------------------------------------------------------


class _PassthroughCoordinator:
    """Test double for RecoveryCoordinator that calls session_fn() directly."""

    def __init__(self, config: Any) -> None:
        pass

    async def run_with_recovery(self, session_fn: Any, **kwargs: Any) -> Any:
        return await session_fn()


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


def _make_session_result() -> Any:
    """Create a minimal ContinuationResult for testing."""
    from golem.supervisor import ContinuationResult, ToolCallRegistry
    return ContinuationResult(
        result_text="done", cost_usd=0.01, input_tokens=100, output_tokens=50,
        turns=3, duration_s=1.0, stalled=False, stall_turn=None,
        registry=ToolCallRegistry(), continuation_count=0, exhausted=False,
    )


@pytest.mark.asyncio
async def test_warning_verdict_attaches_notes() -> None:
    """When reviewer returns warning, result text should contain REVIEWER WARNINGS."""
    warning_verdict = ReviewVerdict(
        decision="warning",
        critical_issues=[],
        important_issues=["Missing null check"],
        minor_issues=[],
        summary="Minor issues.",
    )

    ok = _make_session_result()

    with (
        patch("golem.writer.continuation_supervised_session", AsyncMock(return_value=ok)),
        patch("golem.writer.run_reviewer", return_value=warning_verdict),
        patch("golem.recovery.RecoveryCoordinator", _PassthroughCoordinator),
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            golem_dir = Path(tmpdir) / ".golem"
            (golem_dir / "tickets").mkdir(parents=True)
            ticket = _make_ticket()
            result = await spawn_writer_pair(ticket, tmpdir, GolemConfig(), golem_dir=golem_dir)

    assert "REVIEWER WARNINGS" in result.result_text
    assert "Missing null check" in result.result_text


@pytest.mark.asyncio
async def test_trivial_tier_skips_reviewer() -> None:
    """When skip_review is True, the reviewer should not be called."""
    ok = _make_session_result()

    with (
        patch("golem.writer.continuation_supervised_session", AsyncMock(return_value=ok)),
        patch("golem.writer.run_reviewer") as mock_reviewer,
        patch("golem.recovery.RecoveryCoordinator", _PassthroughCoordinator),
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            golem_dir = Path(tmpdir) / ".golem"
            (golem_dir / "tickets").mkdir(parents=True)
            ticket = _make_ticket(skip_review=True)
            await spawn_writer_pair(ticket, tmpdir, GolemConfig(), golem_dir=golem_dir)

    mock_reviewer.assert_not_called()


@pytest.mark.asyncio
async def test_approve_verdict_proceeds_normally() -> None:
    """When reviewer returns approve, spawn_writer_pair returns without block text."""
    approve_verdict = ReviewVerdict(
        decision="approve",
        critical_issues=[],
        important_issues=[],
        minor_issues=[],
        summary="All good.",
    )

    ok = _make_session_result()

    with (
        patch("golem.writer.continuation_supervised_session", AsyncMock(return_value=ok)),
        patch("golem.writer.run_reviewer", return_value=approve_verdict),
        patch("golem.recovery.RecoveryCoordinator", _PassthroughCoordinator),
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            golem_dir = Path(tmpdir) / ".golem"
            (golem_dir / "tickets").mkdir(parents=True)
            ticket = _make_ticket()
            result = await spawn_writer_pair(ticket, tmpdir, GolemConfig(), golem_dir=golem_dir)

    assert "REVIEWER WARNINGS" not in result.result_text
    assert "REVIEWER BLOCKED" not in result.result_text


@pytest.mark.asyncio
async def test_reviewer_error_does_not_block_pipeline() -> None:
    """If the reviewer raises an exception, spawn_writer_pair should proceed without blocking."""
    ok = _make_session_result()

    with (
        patch("golem.writer.continuation_supervised_session", AsyncMock(return_value=ok)),
        patch("golem.writer.run_reviewer", side_effect=RuntimeError("SDK connection failed")),
        patch("golem.recovery.RecoveryCoordinator", _PassthroughCoordinator),
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            golem_dir = Path(tmpdir) / ".golem"
            (golem_dir / "tickets").mkdir(parents=True)
            ticket = _make_ticket()
            result = await spawn_writer_pair(ticket, tmpdir, GolemConfig(), golem_dir=golem_dir)

    # Should return normally despite reviewer failure
    assert result.result_text == "done"


@pytest.mark.asyncio
async def test_block_verdict_attaches_block_text() -> None:
    """When reviewer returns block, result text should contain REVIEWER BLOCKED."""
    block_verdict = ReviewVerdict(
        decision="block",
        critical_issues=["Hardcoded secret in line 10"],
        important_issues=[],
        minor_issues=[],
        summary="Security issue found.",
    )

    ok = _make_session_result()

    with (
        patch("golem.writer.continuation_supervised_session", AsyncMock(return_value=ok)),
        patch("golem.writer.run_reviewer", return_value=block_verdict),
        patch("golem.recovery.RecoveryCoordinator", _PassthroughCoordinator),
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            golem_dir = Path(tmpdir) / ".golem"
            (golem_dir / "tickets").mkdir(parents=True)
            ticket = _make_ticket()
            result = await spawn_writer_pair(ticket, tmpdir, GolemConfig(), golem_dir=golem_dir)

    assert "REVIEWER BLOCKED" in result.result_text
    assert "Hardcoded secret" in result.result_text


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
