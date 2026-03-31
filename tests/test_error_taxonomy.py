"""Tests for error_taxonomy.py and its integration with recovery.py."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from golem.error_taxonomy import ClassifiedError, ErrorCategory, classify_error
from golem.recovery import (
    FailureType,
    RecoveryCoordinator,
    classify_failure,
)
from golem.tickets import Ticket, TicketContext, TicketStore


# ---------------------------------------------------------------------------
# classify_error — taxonomy classification
# ---------------------------------------------------------------------------


class TestClassifyError:
    """Tests for the higher-level error taxonomy classifier."""

    def test_rate_limit_is_infrastructure(self) -> None:
        result = classify_error("rate limit exceeded, please retry")
        assert result.category == ErrorCategory.INFRASTRUCTURE
        assert result.is_retryable is True
        assert result.suggested_action == "retry"

    def test_429_is_infrastructure(self) -> None:
        result = classify_error("HTTP 429 Too Many Requests")
        assert result.category == ErrorCategory.INFRASTRUCTURE
        assert result.is_retryable is True

    def test_overloaded_is_infrastructure(self) -> None:
        result = classify_error("Server overloaded, try again later")
        assert result.category == ErrorCategory.INFRASTRUCTURE

    def test_auth_error_is_infrastructure(self) -> None:
        result = classify_error("Authentication failed: 401 Unauthorized")
        assert result.category == ErrorCategory.INFRASTRUCTURE
        assert result.suggested_action == "retry"

    def test_403_is_infrastructure(self) -> None:
        result = classify_error("403 Forbidden — invalid token")
        assert result.category == ErrorCategory.INFRASTRUCTURE

    def test_stall_is_timeout(self) -> None:
        result = classify_error("Writer stall detected after 300s")
        assert result.category == ErrorCategory.TIMEOUT
        assert result.is_retryable is True
        assert result.suggested_action == "retry"

    def test_timeout_is_timeout(self) -> None:
        result = classify_error("Session timeout after 600s")
        assert result.category == ErrorCategory.TIMEOUT

    def test_context_exhausted_is_timeout(self) -> None:
        result = classify_error("context_exhausted: token limit reached")
        assert result.category == ErrorCategory.TIMEOUT

    def test_max_turns_is_timeout(self) -> None:
        result = classify_error("max_turns limit reached, aborting")
        assert result.category == ErrorCategory.TIMEOUT

    def test_merge_conflict_is_integration(self) -> None:
        result = classify_error("merge conflict in src/main.py")
        assert result.category == ErrorCategory.INTEGRATION
        assert result.is_retryable is False
        assert result.suggested_action == "rework"

    def test_rebase_failed_is_integration(self) -> None:
        result = classify_error("rebase failed on branch feature/x")
        assert result.category == ErrorCategory.INTEGRATION

    def test_conflict_in_is_integration(self) -> None:
        result = classify_error("CONFLICT (content): Conflict in README.md")
        assert result.category == ErrorCategory.INTEGRATION

    def test_generic_error_is_application(self) -> None:
        result = classify_error("TypeError: unsupported operand type(s)")
        assert result.category == ErrorCategory.APPLICATION
        assert result.is_retryable is False
        assert result.suggested_action == "rework"

    def test_empty_error_is_application(self) -> None:
        result = classify_error("")
        assert result.category == ErrorCategory.APPLICATION

    def test_original_error_preserved(self) -> None:
        msg = "rate limit exceeded"
        result = classify_error(msg)
        assert result.original_error == msg

    def test_error_type_param_accepted(self) -> None:
        """error_type is accepted but doesn't change behaviour (future use)."""
        result = classify_error("rate limit", error_type="sdk")
        assert result.category == ErrorCategory.INFRASTRUCTURE


# ---------------------------------------------------------------------------
# classify_failure — low-level FailureType
# ---------------------------------------------------------------------------


class TestClassifyFailure:
    """Tests for the low-level classify_failure in recovery.py."""

    def test_rate_limit(self) -> None:
        assert classify_failure("rate limit hit") == FailureType.RATE_LIMIT

    def test_429(self) -> None:
        assert classify_failure("HTTP 429") == FailureType.RATE_LIMIT

    def test_auth(self) -> None:
        assert classify_failure("auth failure 401") == FailureType.AUTH_ERROR

    def test_sdk_crash(self) -> None:
        assert classify_failure("SDK connection lost") == FailureType.SDK_CRASH

    def test_oom(self) -> None:
        assert classify_failure("Out of memory on node 3") == FailureType.OOM

    def test_lint(self) -> None:
        assert classify_failure("ruff check failed") == FailureType.LINT_FAILURE

    def test_merge_conflict(self) -> None:
        assert classify_failure("merge conflict in file.py") == FailureType.MERGE_CONFLICT

    def test_stall(self) -> None:
        assert classify_failure("stall detected") == FailureType.STALL

    def test_generic_is_bad_code(self) -> None:
        assert classify_failure("unknown error happened") == FailureType.BAD_CODE


# ---------------------------------------------------------------------------
# RecoveryCoordinator — integration tests
# ---------------------------------------------------------------------------


def _make_ticket(title: str = "Test Ticket") -> Ticket:
    return Ticket(
        id="",
        type="task",
        title=title,
        status="in_progress",
        priority="medium",
        created_by="planner",
        assigned_to="writer_1",
        context=TicketContext(),
    )


class TestRecoveryCoordinator:
    """Tests for RecoveryCoordinator using the error taxonomy."""

    @pytest.mark.asyncio
    async def test_retryable_error_returns_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TicketStore(Path(tmpdir) / "tickets")
            ticket = _make_ticket()
            tid = await store.create(ticket)

            coord = RecoveryCoordinator(store, max_retries=2)
            action = await coord.handle_failure(tid, "rate limit exceeded", agent="writer_1")
            assert action == "retry"

    @pytest.mark.asyncio
    async def test_non_retryable_error_returns_rework(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TicketStore(Path(tmpdir) / "tickets")
            ticket = _make_ticket()
            tid = await store.create(ticket)

            coord = RecoveryCoordinator(store, max_retries=2)
            action = await coord.handle_failure(tid, "TypeError: bad operand", agent="writer_1")
            assert action == "rework"

    @pytest.mark.asyncio
    async def test_retries_exhausted_returns_escalate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TicketStore(Path(tmpdir) / "tickets")
            ticket = _make_ticket()
            tid = await store.create(ticket)

            coord = RecoveryCoordinator(store, max_retries=1)
            # First attempt — retry
            a1 = await coord.handle_failure(tid, "rate limit exceeded")
            assert a1 == "retry"
            # Second attempt — budget exhausted
            a2 = await coord.handle_failure(tid, "rate limit exceeded")
            assert a2 == "escalate"

    @pytest.mark.asyncio
    async def test_failure_event_includes_category(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TicketStore(Path(tmpdir) / "tickets")
            ticket = _make_ticket()
            tid = await store.create(ticket)

            coord = RecoveryCoordinator(store)
            await coord.handle_failure(tid, "merge conflict in main.py", agent="writer_1")

            updated = await store.read(tid)
            last_event = updated.history[-1]
            assert "[integration]" in last_event.note
            assert "merge conflict" in last_event.note

    @pytest.mark.asyncio
    async def test_failure_event_infrastructure_category(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TicketStore(Path(tmpdir) / "tickets")
            ticket = _make_ticket()
            tid = await store.create(ticket)

            coord = RecoveryCoordinator(store)
            await coord.handle_failure(tid, "429 Too Many Requests")

            updated = await store.read(tid)
            last_event = updated.history[-1]
            assert "[infrastructure]" in last_event.note

    @pytest.mark.asyncio
    async def test_failure_event_timeout_category(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TicketStore(Path(tmpdir) / "tickets")
            ticket = _make_ticket()
            tid = await store.create(ticket)

            coord = RecoveryCoordinator(store)
            await coord.handle_failure(tid, "stall detected after 300s")

            updated = await store.read(tid)
            last_event = updated.history[-1]
            assert "[timeout]" in last_event.note

    @pytest.mark.asyncio
    async def test_failure_event_application_category(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TicketStore(Path(tmpdir) / "tickets")
            ticket = _make_ticket()
            tid = await store.create(ticket)

            coord = RecoveryCoordinator(store)
            await coord.handle_failure(tid, "NameError: undefined variable")

            updated = await store.read(tid)
            last_event = updated.history[-1]
            assert "[application]" in last_event.note

    def test_should_retry_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TicketStore(Path(tmpdir) / "tickets")
            coord = RecoveryCoordinator(store, max_retries=2)
            assert coord.should_retry("rate limit exceeded", "TICKET-001") is True

    def test_should_retry_non_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TicketStore(Path(tmpdir) / "tickets")
            coord = RecoveryCoordinator(store, max_retries=2)
            assert coord.should_retry("TypeError: bad operand", "TICKET-001") is False

    @pytest.mark.asyncio
    async def test_reset_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TicketStore(Path(tmpdir) / "tickets")
            ticket = _make_ticket()
            tid = await store.create(ticket)

            coord = RecoveryCoordinator(store, max_retries=1)
            await coord.handle_failure(tid, "rate limit exceeded")  # attempt 1
            coord.reset_attempts(tid)
            # After reset, should be able to retry again
            action = await coord.handle_failure(tid, "rate limit exceeded")
            assert action == "retry"

    def test_classify_returns_classified_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TicketStore(Path(tmpdir) / "tickets")
            coord = RecoveryCoordinator(store)
            result = coord.classify("stall detected")
            assert isinstance(result, ClassifiedError)
            assert result.category == ErrorCategory.TIMEOUT

    @pytest.mark.asyncio
    async def test_ticket_status_set_to_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TicketStore(Path(tmpdir) / "tickets")
            ticket = _make_ticket()
            tid = await store.create(ticket)

            coord = RecoveryCoordinator(store)
            await coord.handle_failure(tid, "some error")

            updated = await store.read(tid)
            assert updated.status == "failed"

    @pytest.mark.asyncio
    async def test_integration_error_returns_rework(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = TicketStore(Path(tmpdir) / "tickets")
            ticket = _make_ticket()
            tid = await store.create(ticket)

            coord = RecoveryCoordinator(store, max_retries=2)
            action = await coord.handle_failure(tid, "merge conflict in config.py")
            assert action == "rework"
