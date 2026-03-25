from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from golem.config import GolemConfig
from golem.tasks import Task
from golem.validator import run_ai_validator, run_deterministic_checks, run_validation


def make_task(commands: list[str] | None = None) -> Task:
    return Task(
        id="task-001",
        description="Test task",
        files_create=[],
        files_modify=[],
        depends_on=[],
        acceptance=["File exists"],
        validation_commands=commands or ["true"],
        reference_docs=[],
        status="pending",
        retries=0,
        last_feedback=None,
        blocked_reason=None,
        completed_at=None,
    )


def test_deterministic_checks_pass() -> None:
    task = make_task(commands=["true"])
    passed, feedback = run_deterministic_checks(task, "/tmp")
    assert passed is True
    assert feedback == ""


def test_deterministic_checks_fail_captures_output() -> None:
    task = make_task(commands=["bash -c 'echo error_output >&2; exit 1'"])
    passed, feedback = run_deterministic_checks(task, "/tmp")
    assert passed is False
    assert "error_output" in feedback or "exit" in feedback.lower() or len(feedback) > 0


def test_deterministic_checks_first_failure_stops() -> None:
    """Only the first failing command's output is returned."""
    task = make_task(commands=["bash -c 'echo first_fail; exit 1'", "bash -c 'echo second; exit 1'"])
    passed, feedback = run_deterministic_checks(task, "/tmp")
    assert passed is False
    assert "first_fail" in feedback


def test_deterministic_checks_pass_then_fail() -> None:
    task = make_task(commands=["true", "false"])
    passed, feedback = run_deterministic_checks(task, "/tmp")
    assert passed is False


async def test_ai_validator_pass_response() -> None:
    """ResultMessage starting with PASS → (True, text)."""
    config = GolemConfig()
    task = make_task()

    mock_result = MagicMock()
    mock_result.result = "PASS: All criteria met. File exists and exports correct function."

    async def _mock_query(**kwargs):  # type: ignore[no-untyped-def]
        yield mock_result

    from claude_agent_sdk import ResultMessage as RM

    mock_result.__class__ = RM
    with patch("golem.validator.query", side_effect=_mock_query):
        with patch("golem.validator.ResultMessage", RM):
            passed, text = await run_ai_validator(task, "/tmp", config)

    assert passed is True
    assert text.startswith("PASS")


async def test_ai_validator_fail_response() -> None:
    """ResultMessage not starting with PASS → (False, text)."""
    from claude_agent_sdk import ResultMessage

    config = GolemConfig()
    task = make_task()

    result_msg = MagicMock(spec=ResultMessage)
    result_msg.result = "FAIL: criterion 1 not met — function not exported"

    async def _async_gen(*args, **kwargs):  # type: ignore[no-untyped-def]
        yield result_msg

    with patch("golem.validator.query", side_effect=_async_gen):
        passed, text = await run_ai_validator(task, "/tmp", config)

    assert passed is False
    assert "FAIL" in text


async def test_deterministic_fail_skips_ai_validator() -> None:
    """When deterministic checks fail, run_ai_validator must NOT be called."""
    task = make_task(commands=["false"])
    config = GolemConfig()

    with patch("golem.validator.run_ai_validator", new_callable=AsyncMock) as mock_ai:
        passed, feedback = await run_validation(task, "/tmp", config)
        mock_ai.assert_not_called()

    assert passed is False


async def test_deterministic_pass_triggers_ai_validator() -> None:
    """When deterministic checks pass, run_ai_validator IS called."""
    task = make_task(commands=["true"])
    config = GolemConfig()

    with patch("golem.validator.run_ai_validator", new_callable=AsyncMock, return_value=(True, "PASS")) as mock_ai:
        passed, feedback = await run_validation(task, "/tmp", config)
        mock_ai.assert_called_once()

    assert passed is True
