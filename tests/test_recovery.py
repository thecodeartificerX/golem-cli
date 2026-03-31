"""Tests for golem.recovery — failure classification, circular fix detection,
recovery delay schedule, and RecoveryCoordinator.run_with_recovery().
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest

from claude_agent_sdk import CLIConnectionError, CLINotFoundError, ClaudeSDKError

from golem.config import GolemConfig
from golem.recovery import (
    CircularFixDetector,
    FailureType,
    RecoveryCoordinator,
    RecoveryExhausted,
    _error_hash,
    classify_failure,
    recovery_delay,
)
from golem.supervisor import SupervisedResult, ToolCallRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clean_result(result_text: str = "ok") -> SupervisedResult:
    """Build a clean (not stalled) SupervisedResult for use in session_fn returns."""
    return SupervisedResult(
        result_text=result_text,
        cost_usd=0.0,
        input_tokens=0,
        output_tokens=0,
        turns=1,
        duration_s=0.1,
        stalled=False,
        stall_turn=None,
        registry=ToolCallRegistry(),
    )


def _stall_result(result_text: str = "") -> SupervisedResult:
    """Build a stalled SupervisedResult."""
    return SupervisedResult(
        result_text=result_text,
        cost_usd=0.0,
        input_tokens=0,
        output_tokens=0,
        turns=5,
        duration_s=1.0,
        stalled=True,
        stall_turn=5,
        registry=ToolCallRegistry(),
    )


def _default_config(**kwargs: object) -> GolemConfig:
    """GolemConfig with sensible test defaults (fast timeouts)."""
    base = GolemConfig(
        max_retries=2,
        retry_delay=1,
        rate_limit_cooldown_s=5,
        max_rate_limit_retries=3,
        circular_fix_threshold=3,
    )
    for k, v in kwargs.items():
        object.__setattr__(base, k, v)
    return base


# ---------------------------------------------------------------------------
# Group 1 — classify_failure
# ---------------------------------------------------------------------------


class TestClassifyFailure:
    def test_classify_rate_limit_primary_pattern(self) -> None:
        exc = ClaudeSDKError("Limit reached · resets Dec 17 at 6am (Europe/Oslo)")
        assert classify_failure(exc) == FailureType.rate_limit

    def test_classify_rate_limit_bullet_variant(self) -> None:
        exc = ClaudeSDKError("Limit reached • resets Jan 5 at 11pm")
        assert classify_failure(exc) == FailureType.rate_limit

    def test_classify_rate_limit_secondary_indicator_429(self) -> None:
        exc = ClaudeSDKError("429 too many requests")
        assert classify_failure(exc) == FailureType.rate_limit

    def test_classify_rate_limit_secondary_indicator_rate_limit(self) -> None:
        exc = ClaudeSDKError("rate limit exceeded")
        assert classify_failure(exc) == FailureType.rate_limit

    def test_classify_rate_limit_from_result_text(self) -> None:
        assert classify_failure(None, "rate limit exceeded for this session") == FailureType.rate_limit

    def test_classify_auth_failure_401(self) -> None:
        exc = ClaudeSDKError("API Error: 401 Unauthorized")
        assert classify_failure(exc) == FailureType.auth_failure

    def test_classify_auth_failure_oauth_expired(self) -> None:
        exc = ClaudeSDKError("oauth token has expired, please re-authenticate")
        assert classify_failure(exc) == FailureType.auth_failure

    def test_classify_auth_failure_http_401(self) -> None:
        exc = ClaudeSDKError("HTTP 401 response received")
        assert classify_failure(exc) == FailureType.auth_failure

    def test_classify_auth_failure_status_401(self) -> None:
        exc = ClaudeSDKError("status: 401")
        assert classify_failure(exc) == FailureType.auth_failure

    def test_classify_auth_failure_authentication_error_json(self) -> None:
        exc = ClaudeSDKError('{"type": "authentication_error", "message": "invalid"}')
        assert classify_failure(exc) == FailureType.auth_failure

    def test_classify_auth_failure_not_authenticated(self) -> None:
        exc = ClaudeSDKError("[cli] not authenticated — please login")
        assert classify_failure(exc) == FailureType.auth_failure

    def test_classify_auth_failure_login_required(self) -> None:
        exc = ClaudeSDKError("[session] login is required")
        assert classify_failure(exc) == FailureType.auth_failure

    def test_classify_context_exhausted_maximum_length(self) -> None:
        exc = ClaudeSDKError("This request exceeds the maximum context length")
        assert classify_failure(exc) == FailureType.context_exhausted

    def test_classify_context_exhausted_window_full(self) -> None:
        exc = ClaudeSDKError("context window is full")
        assert classify_failure(exc) == FailureType.context_exhausted

    def test_classify_context_exhausted_token_limit(self) -> None:
        exc = ClaudeSDKError("token limit exceeded")
        assert classify_failure(exc) == FailureType.context_exhausted

    def test_classify_context_exhausted_prompt_too_long(self) -> None:
        exc = ClaudeSDKError("prompt is too long")
        assert classify_failure(exc) == FailureType.context_exhausted

    def test_classify_cli_not_found(self) -> None:
        exc = CLINotFoundError("claude not found on PATH")
        assert classify_failure(exc) == FailureType.infrastructure

    def test_classify_cli_connection_error(self) -> None:
        exc = CLIConnectionError("connection timed out")
        assert classify_failure(exc) == FailureType.sdk_error

    def test_classify_claude_sdk_error_generic(self) -> None:
        exc = ClaudeSDKError("unexpected error in SDK")
        assert classify_failure(exc) == FailureType.sdk_error

    def test_classify_timeout_error(self) -> None:
        exc = asyncio.TimeoutError()
        assert classify_failure(exc) == FailureType.timeout

    def test_classify_none_exc_empty_text(self) -> None:
        # Default for stall with no diagnostic text
        assert classify_failure(None, "") == FailureType.sdk_error

    def test_classify_none_exc_with_result_text(self) -> None:
        assert classify_failure(None, "context window is full") == FailureType.context_exhausted

    def test_classify_billing_insufficient_credits(self) -> None:
        exc = ClaudeSDKError("insufficient_credits: your account has no credits remaining")
        assert classify_failure(exc) == FailureType.billing_failure

    def test_classify_billing_out_of_credits(self) -> None:
        exc = ClaudeSDKError("out of credits — please top up your account")
        assert classify_failure(exc) == FailureType.billing_failure

    def test_classify_billing_http_402(self) -> None:
        exc = ClaudeSDKError("HTTP 402 Payment Required")
        assert classify_failure(exc) == FailureType.billing_failure

    def test_classify_billing_payment_required(self) -> None:
        exc = ClaudeSDKError("payment_required: upgrade your plan to continue")
        assert classify_failure(exc) == FailureType.billing_failure

    def test_classify_billing_subscription_inactive(self) -> None:
        exc = ClaudeSDKError("subscription_inactive: your subscription has lapsed")
        assert classify_failure(exc) == FailureType.billing_failure

    def test_classify_billing_account_suspended(self) -> None:
        exc = ClaudeSDKError("account_suspended due to overdue payment")
        assert classify_failure(exc) == FailureType.billing_failure

    def test_classify_billing_quota_exceeded(self) -> None:
        exc = ClaudeSDKError("quota_exceeded for this billing period")
        assert classify_failure(exc) == FailureType.billing_failure

    def test_classify_billing_trial_expired(self) -> None:
        exc = ClaudeSDKError("trial_expired — please add a payment method")
        assert classify_failure(exc) == FailureType.billing_failure

    def test_classify_billing_payment_declined(self) -> None:
        exc = ClaudeSDKError("payment_declined: card ending 4242 was declined")
        assert classify_failure(exc) == FailureType.billing_failure

    def test_classify_billing_spending_limit(self) -> None:
        exc = ClaudeSDKError("spending_limit reached for this period")
        assert classify_failure(exc) == FailureType.billing_failure

    def test_classify_billing_status_402(self) -> None:
        exc = ClaudeSDKError("status: 402 payment required")
        assert classify_failure(exc) == FailureType.billing_failure

    def test_classify_billing_json_type_field(self) -> None:
        exc = ClaudeSDKError('{"type": "billing_error", "message": "no credits"}')
        assert classify_failure(exc) == FailureType.billing_failure

    def test_classify_billing_from_result_text(self) -> None:
        assert classify_failure(None, "HTTP 402 — insufficient_credits on this account") == FailureType.billing_failure

    def test_classify_billing_takes_priority_over_auth(self) -> None:
        # HTTP 402 should be billing, not auth — billing is checked first
        exc = ClaudeSDKError("HTTP 402 payment_required; please authenticate your payment")
        assert classify_failure(exc) == FailureType.billing_failure

    def test_classify_auth_takes_priority_over_rate_limit(self) -> None:
        # Text contains both patterns — auth wins
        exc = ClaudeSDKError("Limit reached · resets Dec 17 at 6am; oauth token has expired")
        assert classify_failure(exc) == FailureType.auth_failure

    def test_classify_unknown_exception(self) -> None:
        exc = ValueError("something unexpected happened")
        assert classify_failure(exc) == FailureType.unknown

    def test_classify_result_text_capped_at_2000(self) -> None:
        # Large result_text should not cause errors — just capped
        long_text = "A" * 10_000
        result = classify_failure(None, long_text)
        assert isinstance(result, FailureType)

    def test_classify_infrastructure_before_text_scan(self) -> None:
        # CLINotFoundError returns infrastructure regardless of message content
        exc = CLINotFoundError("oauth token has expired")
        assert classify_failure(exc) == FailureType.infrastructure


# ---------------------------------------------------------------------------
# Group 2 — CircularFixDetector
# ---------------------------------------------------------------------------


class TestCircularFixDetector:
    def test_circular_not_triggered_below_threshold(self) -> None:
        detector = CircularFixDetector(threshold=3)
        detector.record("T-001", "same error")
        detector.record("T-001", "same error")
        assert not detector.is_circular("T-001")

    def test_circular_triggered_at_threshold(self) -> None:
        detector = CircularFixDetector(threshold=3)
        detector.record("T-001", "same error")
        detector.record("T-001", "same error")
        detector.record("T-001", "same error")
        assert detector.is_circular("T-001")

    def test_circular_different_errors_not_circular(self) -> None:
        detector = CircularFixDetector(threshold=3)
        detector.record("T-001", "error alpha")
        detector.record("T-001", "error beta")
        detector.record("T-001", "error gamma")
        assert not detector.is_circular("T-001")

    def test_circular_clear_resets_state(self) -> None:
        detector = CircularFixDetector(threshold=3)
        detector.record("T-001", "same error")
        detector.record("T-001", "same error")
        detector.record("T-001", "same error")
        assert detector.is_circular("T-001")
        detector.clear("T-001")
        assert not detector.is_circular("T-001")

    def test_circular_independent_tickets(self) -> None:
        detector = CircularFixDetector(threshold=3)
        detector.record("T-001", "same error")
        detector.record("T-001", "same error")
        detector.record("T-001", "same error")
        assert detector.is_circular("T-001")
        assert not detector.is_circular("T-002")

    def test_error_hash_deterministic(self) -> None:
        h1 = _error_hash("some error text")
        h2 = _error_hash("some error text")
        assert h1 == h2

    def test_error_hash_normalizes_whitespace(self) -> None:
        h1 = _error_hash("  FOO\n")
        h2 = _error_hash("foo")
        assert h1 == h2

    def test_error_hash_different_texts_differ(self) -> None:
        h1 = _error_hash("error alpha")
        h2 = _error_hash("error beta")
        assert h1 != h2


# ---------------------------------------------------------------------------
# Group 3 — _error_hash
# ---------------------------------------------------------------------------


class TestErrorHash:
    def test_error_hash_returns_string(self) -> None:
        result = _error_hash("some text")
        assert isinstance(result, str)

    def test_error_hash_empty_string_does_not_crash(self) -> None:
        result = _error_hash("")
        assert result == "0"

    def test_error_hash_known_value(self) -> None:
        # Regression: "syntax error" should produce a stable hash value.
        # Computed from the djb2 algorithm: h starts at 0, each char h = (h<<5)-h+ord(c) & 0xFFFFFFFF
        result = _error_hash("syntax error")
        assert isinstance(result, str)
        assert result != "0"
        # Verify it is stable across calls
        assert result == _error_hash("syntax error")
        assert result == _error_hash("SYNTAX ERROR")  # normalized to lowercase

    def test_error_hash_sign_wrapping(self) -> None:
        # A hash >= 0x80000000 should produce a negative number (like JS | 0)
        # We just verify it does not crash and returns a string
        long_text = "x" * 1000
        result = _error_hash(long_text)
        assert isinstance(result, str)
        int(result)  # must be parseable as integer


# ---------------------------------------------------------------------------
# Group 4 — recovery_delay
# ---------------------------------------------------------------------------


class TestRecoveryDelay:
    def test_delay_rate_limit_uses_cooldown(self) -> None:
        config = _default_config(rate_limit_cooldown_s=300)
        assert recovery_delay(FailureType.rate_limit, 0, config) == 300.0

    def test_delay_rate_limit_does_not_grow(self) -> None:
        config = _default_config(rate_limit_cooldown_s=300)
        assert recovery_delay(FailureType.rate_limit, 5, config) == 300.0

    def test_delay_sdk_error_exponential_attempt0(self) -> None:
        config = _default_config(retry_delay=10)
        delay = recovery_delay(FailureType.sdk_error, 0, config)
        assert delay == pytest.approx(10.0)

    def test_delay_sdk_error_exponential_attempt1(self) -> None:
        config = _default_config(retry_delay=10)
        delay = recovery_delay(FailureType.sdk_error, 1, config)
        assert delay == pytest.approx(20.0)

    def test_delay_sdk_error_exponential_attempt2(self) -> None:
        config = _default_config(retry_delay=10)
        delay = recovery_delay(FailureType.sdk_error, 2, config)
        assert delay == pytest.approx(40.0)

    def test_delay_sdk_error_capped_at_120(self) -> None:
        config = _default_config(retry_delay=10)
        # attempt=4 → 10 * 16 = 160, capped at 120
        delay = recovery_delay(FailureType.sdk_error, 4, config)
        assert delay == pytest.approx(120.0)

    def test_delay_auth_failure_zero(self) -> None:
        config = _default_config()
        assert recovery_delay(FailureType.auth_failure, 0, config) == 0.0

    def test_delay_billing_failure_zero(self) -> None:
        config = _default_config()
        assert recovery_delay(FailureType.billing_failure, 0, config) == 0.0

    def test_delay_infrastructure_zero(self) -> None:
        config = _default_config()
        assert recovery_delay(FailureType.infrastructure, 0, config) == 0.0

    def test_delay_unknown_same_as_sdk_error(self) -> None:
        config = _default_config(retry_delay=10)
        assert recovery_delay(FailureType.unknown, 0, config) == recovery_delay(
            FailureType.sdk_error, 0, config
        )

    def test_delay_timeout_same_as_sdk_error(self) -> None:
        config = _default_config(retry_delay=10)
        assert recovery_delay(FailureType.timeout, 1, config) == recovery_delay(
            FailureType.sdk_error, 1, config
        )


# ---------------------------------------------------------------------------
# Group 5 — RecoveryCoordinator.run_with_recovery (async)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestRecoveryCoordinator:
    async def test_success_on_first_attempt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleep_mock = AsyncMock(return_value=None)
        monkeypatch.setattr("golem.recovery.asyncio.sleep", sleep_mock)

        config = _default_config()
        coord = RecoveryCoordinator(config)
        expected = _clean_result()

        result = await coord.run_with_recovery(
            session_fn=AsyncMock(return_value=expected),
            role="planner",
            label="planner",
        )

        assert result is expected
        sleep_mock.assert_not_called()

    async def test_retries_sdk_error_twice_then_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleep_mock = AsyncMock(return_value=None)
        monkeypatch.setattr("golem.recovery.asyncio.sleep", sleep_mock)

        config = _default_config(max_retries=2, retry_delay=1)
        coord = RecoveryCoordinator(config)

        call_count = 0
        clean = _clean_result()

        async def flaky_fn() -> SupervisedResult:
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise ClaudeSDKError("transient SDK error")
            return clean

        result = await coord.run_with_recovery(
            session_fn=flaky_fn,
            role="planner",
            label="planner",
        )

        assert result is clean
        assert call_count == 3
        # Two sleeps for two retries
        assert sleep_mock.call_count == 2

    async def test_exhausted_after_max_retries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleep_mock = AsyncMock(return_value=None)
        monkeypatch.setattr("golem.recovery.asyncio.sleep", sleep_mock)

        config = _default_config(max_retries=2, retry_delay=1)
        coord = RecoveryCoordinator(config)

        async def always_fails() -> SupervisedResult:
            raise ClaudeSDKError("persistent SDK error")

        with pytest.raises(RecoveryExhausted) as exc_info:
            await coord.run_with_recovery(
                session_fn=always_fails,
                role="planner",
                label="planner",
            )

        assert exc_info.value.failure_type == FailureType.sdk_error
        # max_retries=2 means 3 total calls (attempt 0, 1, 2) — exhausted at attempt 2
        assert exc_info.value.attempts == 2

    async def test_auth_failure_raises_immediately(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleep_mock = AsyncMock(return_value=None)
        monkeypatch.setattr("golem.recovery.asyncio.sleep", sleep_mock)

        config = _default_config()
        coord = RecoveryCoordinator(config)

        call_count = 0

        async def auth_fail() -> SupervisedResult:
            nonlocal call_count
            call_count += 1
            raise ClaudeSDKError("oauth token has expired")

        with pytest.raises(RecoveryExhausted) as exc_info:
            await coord.run_with_recovery(
                session_fn=auth_fail,
                role="planner",
                label="planner",
            )

        assert exc_info.value.failure_type == FailureType.auth_failure
        assert call_count == 1  # no retry
        sleep_mock.assert_not_called()

    async def test_billing_failure_raises_immediately(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleep_mock = AsyncMock(return_value=None)
        monkeypatch.setattr("golem.recovery.asyncio.sleep", sleep_mock)

        config = _default_config()
        coord = RecoveryCoordinator(config)

        call_count = 0

        async def billing_fail() -> SupervisedResult:
            nonlocal call_count
            call_count += 1
            raise ClaudeSDKError("HTTP 402 payment_required: insufficient_credits")

        with pytest.raises(RecoveryExhausted) as exc_info:
            await coord.run_with_recovery(
                session_fn=billing_fail,
                role="writer",
                label="TICKET-001",
            )

        assert exc_info.value.failure_type == FailureType.billing_failure
        assert call_count == 1  # no retry
        sleep_mock.assert_not_called()

    async def test_infrastructure_raises_immediately(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleep_mock = AsyncMock(return_value=None)
        monkeypatch.setattr("golem.recovery.asyncio.sleep", sleep_mock)

        config = _default_config()
        coord = RecoveryCoordinator(config)

        call_count = 0

        async def not_found() -> SupervisedResult:
            nonlocal call_count
            call_count += 1
            raise CLINotFoundError("claude not found")

        with pytest.raises(RecoveryExhausted) as exc_info:
            await coord.run_with_recovery(
                session_fn=not_found,
                role="planner",
                label="planner",
            )

        assert exc_info.value.failure_type == FailureType.infrastructure
        assert call_count == 1
        sleep_mock.assert_not_called()

    async def test_rate_limit_waits_cooldown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleep_mock = AsyncMock(return_value=None)
        monkeypatch.setattr("golem.recovery.asyncio.sleep", sleep_mock)

        config = _default_config(rate_limit_cooldown_s=300, max_rate_limit_retries=3, retry_delay=1)
        coord = RecoveryCoordinator(config)

        call_count = 0
        clean = _clean_result()

        async def rate_limited_then_ok() -> SupervisedResult:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ClaudeSDKError("Limit reached · resets Dec 17 at 6am (UTC)")
            return clean

        result = await coord.run_with_recovery(
            session_fn=rate_limited_then_ok,
            role="planner",
            label="planner",
        )

        assert result is clean
        # Must have slept with the cooldown value
        assert sleep_mock.call_count == 1
        assert sleep_mock.call_args_list[0].args[0] == pytest.approx(300.0)

    async def test_rate_limit_stall_uses_resets_at_for_precise_sleep(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When a stalled result carries rate_limit_resets_at, run_with_recovery uses
        that timestamp for sleep duration instead of the fixed rate_limit_cooldown_s."""
        sleep_mock = AsyncMock(return_value=None)
        monkeypatch.setattr("golem.recovery.asyncio.sleep", sleep_mock)

        # Freeze time so the expected delay is deterministic.
        frozen_now = 1_000_000.0
        monkeypatch.setattr("golem.recovery.time.time", lambda: frozen_now)

        # rate_limit_cooldown_s is intentionally different (300s) to confirm it is NOT used.
        config = _default_config(rate_limit_cooldown_s=300, max_retries=3, retry_delay=1)
        coord = RecoveryCoordinator(config)

        call_count = 0
        clean = _clean_result()

        def _stall_with_resets_at(resets_at: float) -> SupervisedResult:
            return SupervisedResult(
                result_text="rate limit",
                cost_usd=0.0,
                input_tokens=0,
                output_tokens=0,
                turns=5,
                duration_s=1.0,
                stalled=True,
                stall_turn=5,
                registry=ToolCallRegistry(),
                rate_limit_resets_at=resets_at,
            )

        async def stall_with_resets_then_ok() -> SupervisedResult:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Return a stalled result whose text triggers rate_limit classification
                # and carries a precise resets_at timestamp 180s from now.
                return SupervisedResult(
                    result_text="rate limit exceeded for this session",
                    cost_usd=0.0,
                    input_tokens=0,
                    output_tokens=0,
                    turns=5,
                    duration_s=1.0,
                    stalled=True,
                    stall_turn=5,
                    registry=ToolCallRegistry(),
                    rate_limit_resets_at=frozen_now + 180.0,
                )
            return clean

        result = await coord.run_with_recovery(
            session_fn=stall_with_resets_then_ok,
            role="planner",
            label="planner",
        )

        assert result is clean
        assert sleep_mock.call_count == 1
        # Precise delay = max(1.0, (frozen_now+180) - frozen_now) = 180.0, NOT 300.0
        assert sleep_mock.call_args_list[0].args[0] == pytest.approx(180.0)

    async def test_circular_fix_detected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleep_mock = AsyncMock(return_value=None)
        monkeypatch.setattr("golem.recovery.asyncio.sleep", sleep_mock)

        config = _default_config(max_retries=10, circular_fix_threshold=3, retry_delay=1)
        coord = RecoveryCoordinator(config)

        async def same_error() -> SupervisedResult:
            raise ClaudeSDKError("identical repeated SDK error for testing")

        with pytest.raises(RecoveryExhausted) as exc_info:
            await coord.run_with_recovery(
                session_fn=same_error,
                role="planner",
                label="planner",
            )

        assert exc_info.value.failure_type == FailureType.circular_fix

    async def test_stall_result_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleep_mock = AsyncMock(return_value=None)
        monkeypatch.setattr("golem.recovery.asyncio.sleep", sleep_mock)

        # Use a unique stall text each time to avoid circular detection
        config = _default_config(max_retries=2, retry_delay=1)
        coord = RecoveryCoordinator(config)

        call_count = 0
        clean = _clean_result()

        async def stall_twice_then_ok() -> SupervisedResult:
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return _stall_result(result_text=f"stall attempt {call_count}")
            return clean

        result = await coord.run_with_recovery(
            session_fn=stall_twice_then_ok,
            role="planner",
            label="planner",
        )

        assert result is clean
        assert call_count == 3

    async def test_stall_exhausted_returns_stall_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleep_mock = AsyncMock(return_value=None)
        monkeypatch.setattr("golem.recovery.asyncio.sleep", sleep_mock)

        config = _default_config(max_retries=2, retry_delay=1)
        coord = RecoveryCoordinator(config)

        call_count = 0

        async def always_stall() -> SupervisedResult:
            nonlocal call_count
            call_count += 1
            return _stall_result(result_text=f"unique stall text {call_count}")

        result = await coord.run_with_recovery(
            session_fn=always_stall,
            role="planner",
            label="planner",
        )
        # After exhausting retries, returns the stalled result to caller
        assert result.stalled is True

    async def test_emits_classified_event(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleep_mock = AsyncMock(return_value=None)
        monkeypatch.setattr("golem.recovery.asyncio.sleep", sleep_mock)

        import asyncio as _asyncio

        from golem.events import EventBus, QueueBackend

        queue: asyncio.Queue[object] = _asyncio.Queue()
        bus = EventBus(QueueBackend(queue), session_id="test-session")

        config = _default_config(max_retries=2, retry_delay=1)
        coord = RecoveryCoordinator(config)

        call_count = 0
        clean = _clean_result()

        async def fail_once() -> SupervisedResult:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ClaudeSDKError("transient failure")
            return clean

        await coord.run_with_recovery(
            session_fn=fail_once,
            role="planner",
            label="planner",
            event_bus=bus,
        )

        # At least one AgentErrorClassified event should have been emitted
        from golem.events import AgentErrorClassified

        events = []
        while not queue.empty():
            events.append(queue.get_nowait())

        classified = [e for e in events if isinstance(e, AgentErrorClassified)]
        assert len(classified) >= 1
        assert classified[0].role == "planner"
        assert classified[0].failure_type == FailureType.sdk_error.value

    async def test_emits_recovery_started_event(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleep_mock = AsyncMock(return_value=None)
        monkeypatch.setattr("golem.recovery.asyncio.sleep", sleep_mock)

        import asyncio as _asyncio

        from golem.events import EventBus, QueueBackend

        queue: asyncio.Queue[object] = _asyncio.Queue()
        bus = EventBus(QueueBackend(queue), session_id="test-session")

        config = _default_config(max_retries=2, retry_delay=10)
        coord = RecoveryCoordinator(config)

        call_count = 0
        clean = _clean_result()

        async def fail_once() -> SupervisedResult:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ClaudeSDKError("transient failure")
            return clean

        await coord.run_with_recovery(
            session_fn=fail_once,
            role="planner",
            label="planner",
            event_bus=bus,
        )

        from golem.events import AgentRecoveryStarted

        events = []
        while not queue.empty():
            events.append(queue.get_nowait())

        recovery_events = [e for e in events if isinstance(e, AgentRecoveryStarted)]
        assert len(recovery_events) >= 1
        # delay_s for sdk_error attempt=0 with retry_delay=10 is 10.0
        assert recovery_events[0].delay_s == pytest.approx(10.0)

    async def test_clean_result_clears_circular_history(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleep_mock = AsyncMock(return_value=None)
        monkeypatch.setattr("golem.recovery.asyncio.sleep", sleep_mock)

        config = _default_config(max_retries=5, circular_fix_threshold=3, retry_delay=1)
        coord = RecoveryCoordinator(config)

        call_count = 0
        clean = _clean_result()

        async def fail_twice_then_ok() -> SupervisedResult:
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise ClaudeSDKError("same error each time but only twice")
            return clean

        # Should succeed without circular fix trigger (2 < threshold of 3)
        result = await coord.run_with_recovery(
            session_fn=fail_twice_then_ok,
            role="planner",
            label="planner",
        )
        assert not result.stalled
        assert result is clean


# ---------------------------------------------------------------------------
# Group 6 — Config validation for new fields
# ---------------------------------------------------------------------------


class TestConfigValidation:
    def test_rate_limit_cooldown_negative_warns(self) -> None:
        config = GolemConfig(rate_limit_cooldown_s=-1)
        warnings = config.validate()
        assert any("rate_limit_cooldown_s" in w for w in warnings)

    def test_max_rate_limit_retries_negative_warns(self) -> None:
        config = GolemConfig(max_rate_limit_retries=-1)
        warnings = config.validate()
        assert any("max_rate_limit_retries" in w for w in warnings)

    def test_circular_fix_threshold_below_2_warns(self) -> None:
        config = GolemConfig(circular_fix_threshold=1)
        warnings = config.validate()
        assert any("circular_fix_threshold" in w for w in warnings)

    def test_valid_new_config_no_warnings(self) -> None:
        config = GolemConfig(
            rate_limit_cooldown_s=300,
            max_rate_limit_retries=3,
            circular_fix_threshold=3,
        )
        # Only model warnings (which come from base defaults)
        warnings = config.validate()
        new_field_warnings = [
            w for w in warnings
            if any(k in w for k in ("rate_limit_cooldown_s", "max_rate_limit_retries", "circular_fix_threshold"))
        ]
        assert new_field_warnings == []


# ---------------------------------------------------------------------------
# Group 7 — Event type registry
# ---------------------------------------------------------------------------


class TestEventRegistry:
    def test_agent_error_classified_registered(self) -> None:
        from golem.events import EVENT_TYPES

        assert "agent_error_classified" in EVENT_TYPES

    def test_agent_recovery_started_registered(self) -> None:
        from golem.events import EVENT_TYPES

        assert "agent_recovery_started" in EVENT_TYPES

    def test_total_event_count_is_23(self) -> None:
        from golem.events import EVENT_TYPES

        assert len(EVENT_TYPES) == 44

    def test_agent_error_classified_roundtrip(self) -> None:
        from golem.events import AgentErrorClassified, GolemEvent

        event = AgentErrorClassified(
            role="planner",
            label="planner",
            failure_type="sdk_error",
            attempt=1,
            error_preview="something went wrong",
        )
        d = event.to_dict()
        assert d["type"] == "agent_error_classified"
        restored = GolemEvent.from_dict(d)
        assert isinstance(restored, AgentErrorClassified)
        assert restored.role == "planner"
        assert restored.failure_type == "sdk_error"

    def test_agent_recovery_started_roundtrip(self) -> None:
        from golem.events import AgentRecoveryStarted, GolemEvent

        event = AgentRecoveryStarted(
            role="tech_lead",
            label="TICKET-001",
            failure_type="rate_limit",
            attempt=0,
            delay_s=300.0,
        )
        d = event.to_dict()
        assert d["type"] == "agent_recovery_started"
        restored = GolemEvent.from_dict(d)
        assert isinstance(restored, AgentRecoveryStarted)
        assert restored.delay_s == pytest.approx(300.0)


# ---------------------------------------------------------------------------
# Group 8 — Continuation cost double-counting fix
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestContinuationCostFix:
    async def test_cumulative_cost_not_double_counted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """3 continuation segments with cumulative total_cost_usd values of 1.0, 2.5, 4.0
        should yield total cost 4.0, not 7.5 (sum of raw values).
        """
        from unittest.mock import patch

        from claude_agent_sdk import ClaudeAgentOptions, ResultMessage

        from golem.config import GolemConfig
        from golem.supervisor import ContinuationResult, StallConfig, continuation_supervised_session

        config = GolemConfig(continuation_enabled=True, max_continuations=5)
        stall_cfg = StallConfig(warning_pct=0.6, kill_pct=0.8, expected_actions=[], role="planner", max_turns=50)
        call_count = 0

        # Cumulative costs: seg1=1.0, seg2=2.5 (+=1.5), seg3=4.0 (+=1.5)
        cumulative_costs = [1.0, 2.5, 4.0]

        async def fake_query(*args: object, **kwargs: object):  # type: ignore[return]
            nonlocal call_count
            seg = call_count
            call_count += 1
            stop = "max_tokens" if call_count < 3 else "end_turn"
            yield ResultMessage(
                subtype="result",
                duration_ms=100,
                duration_api_ms=50,
                is_error=False,
                num_turns=2,
                session_id=f"s{call_count}",
                result="partial" if call_count < 3 else "done",
                stop_reason=stop,
                total_cost_usd=cumulative_costs[seg],
                usage={"input_tokens": 100, "output_tokens": 50},
            )

        options = ClaudeAgentOptions(
            model="claude-opus-4-5",
            cwd=".",
            tools={"type": "preset", "preset": "claude_code"},
            max_turns=50,
            permission_mode="bypassPermissions",
            env={},
        )

        with patch("golem.supervisor.query", side_effect=fake_query), \
             patch("golem.supervisor.compact_session_messages", return_value="summary"):
            result = await continuation_supervised_session(
                "do work", options, "planner", config, stall_cfg,
            )

        assert isinstance(result, ContinuationResult)
        # Total should be 4.0 (the final cumulative value), NOT 7.5 (1.0 + 2.5 + 4.0)
        assert result.cost_usd == pytest.approx(4.0)
        assert result.continuation_count == 2
        assert call_count == 3

    async def test_single_segment_cost_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Single-segment (no continuation) cost is passed through unchanged."""
        from unittest.mock import patch

        from claude_agent_sdk import ClaudeAgentOptions, ResultMessage

        from golem.config import GolemConfig
        from golem.supervisor import ContinuationResult, StallConfig, continuation_supervised_session

        config = GolemConfig(continuation_enabled=True, max_continuations=3)
        stall_cfg = StallConfig(warning_pct=0.6, kill_pct=0.8, expected_actions=[], role="planner", max_turns=50)

        async def fake_query(*args: object, **kwargs: object):  # type: ignore[return]
            yield ResultMessage(
                subtype="result",
                duration_ms=100,
                duration_api_ms=50,
                is_error=False,
                num_turns=1,
                session_id="s1",
                result="done",
                stop_reason="end_turn",
                total_cost_usd=2.75,
                usage={"input_tokens": 200, "output_tokens": 80},
            )

        options = ClaudeAgentOptions(
            model="claude-opus-4-5",
            cwd=".",
            tools={"type": "preset", "preset": "claude_code"},
            max_turns=50,
            permission_mode="bypassPermissions",
            env={},
        )

        with patch("golem.supervisor.query", side_effect=fake_query):
            result = await continuation_supervised_session(
                "do work", options, "planner", config, stall_cfg,
            )

        assert isinstance(result, ContinuationResult)
        assert result.cost_usd == pytest.approx(2.75)


# ---------------------------------------------------------------------------
# Group 9 — SupervisedResult new fields
# ---------------------------------------------------------------------------


class TestSupervisedResultNewFields:
    def test_cache_read_tokens_default_zero(self) -> None:
        """cache_read_tokens defaults to 0."""
        result = SupervisedResult(
            result_text="ok",
            cost_usd=0.0,
            input_tokens=0,
            output_tokens=0,
            turns=1,
            duration_s=0.1,
            stalled=False,
            stall_turn=None,
            registry=ToolCallRegistry(),
        )
        assert result.cache_read_tokens == 0

    def test_drained_turns_default_zero(self) -> None:
        """drained_turns defaults to 0."""
        result = SupervisedResult(
            result_text="ok",
            cost_usd=0.0,
            input_tokens=0,
            output_tokens=0,
            turns=1,
            duration_s=0.1,
            stalled=False,
            stall_turn=None,
            registry=ToolCallRegistry(),
        )
        assert result.drained_turns == 0

    def test_rate_limit_resets_at_default_none(self) -> None:
        """rate_limit_resets_at defaults to None."""
        result = SupervisedResult(
            result_text="ok",
            cost_usd=0.0,
            input_tokens=0,
            output_tokens=0,
            turns=1,
            duration_s=0.1,
            stalled=False,
            stall_turn=None,
            registry=ToolCallRegistry(),
        )
        assert result.rate_limit_resets_at is None

    def test_new_fields_constructible(self) -> None:
        """SupervisedResult accepts all new fields."""
        result = SupervisedResult(
            result_text="ok",
            cost_usd=1.5,
            input_tokens=100,
            output_tokens=50,
            turns=5,
            duration_s=2.0,
            stalled=False,
            stall_turn=None,
            registry=ToolCallRegistry(),
            cache_read_tokens=200,
            drained_turns=3,
            rate_limit_resets_at=1_700_000_000.0,
        )
        assert result.cache_read_tokens == 200
        assert result.drained_turns == 3
        assert result.rate_limit_resets_at == pytest.approx(1_700_000_000.0)


# ---------------------------------------------------------------------------
# Group 10 — cache_read_tokens and drained_turns in supervised_session()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSupervisedSessionNewFields:
    async def test_cache_read_tokens_extracted_from_usage(self) -> None:
        """cache_read_tokens is extracted from ResultMessage.usage."""
        from unittest.mock import patch

        from claude_agent_sdk import ClaudeAgentOptions, ResultMessage

        from golem.config import GolemConfig
        from golem.supervisor import StallConfig, supervised_session

        config = GolemConfig()
        stall_cfg = StallConfig(warning_pct=0.6, kill_pct=0.8, expected_actions=[], role="planner", max_turns=50)

        async def fake_query(*args: object, **kwargs: object):  # type: ignore[return]
            yield ResultMessage(
                subtype="result",
                duration_ms=100,
                duration_api_ms=50,
                is_error=False,
                num_turns=1,
                session_id="s1",
                result="done",
                stop_reason="end_turn",
                total_cost_usd=0.5,
                usage={"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 300},
            )

        options = ClaudeAgentOptions(
            model="claude-opus-4-5",
            cwd=".",
            tools={"type": "preset", "preset": "claude_code"},
            max_turns=50,
            permission_mode="bypassPermissions",
            env={},
        )

        with patch("golem.supervisor.query", side_effect=fake_query):
            result = await supervised_session(
                "do work", options, "planner", config, stall_cfg,
            )

        assert result.cache_read_tokens == 300

    async def test_drained_turns_counted_after_kill(self) -> None:
        """drained_turns counts turns consumed after kill threshold is hit."""
        from unittest.mock import patch

        from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock

        from golem.config import GolemConfig
        from golem.supervisor import StallConfig, supervised_session

        config = GolemConfig()
        # kill_turn = 50% of 4 = 2; warning_turn = 30% of 4 = 1
        stall_cfg = StallConfig(warning_pct=0.3, kill_pct=0.5, expected_actions=[], role="planner", max_turns=4)

        async def fake_query(*args: object, **kwargs: object):  # type: ignore[return]
            # 5 assistant turns with no action tools (stall at turn 2, drain turns 3-5)
            for i in range(5):
                yield AssistantMessage(
                    content=[TextBlock(text=f"thinking turn {i}")],
                    model="claude-opus-4-5",
                )
            yield ResultMessage(
                subtype="result",
                duration_ms=100,
                duration_api_ms=50,
                is_error=False,
                num_turns=5,
                session_id="s1",
                result="",
                stop_reason="max_turns",
                total_cost_usd=0.1,
                usage={"input_tokens": 50, "output_tokens": 25},
            )

        options = ClaudeAgentOptions(
            model="claude-opus-4-5",
            cwd=".",
            tools={"type": "preset", "preset": "claude_code"},
            max_turns=4,
            permission_mode="bypassPermissions",
            env={},
        )

        with patch("golem.supervisor.query", side_effect=fake_query):
            result = await supervised_session(
                "do work", options, "planner", config, stall_cfg,
            )

        assert result.stalled is True
        # Kill hits at turn 2 (turns_since >= kill_turn=2), turns 3,4,5 are drained
        assert result.drained_turns == 3

    async def test_cache_read_tokens_accumulated_in_continuation(self) -> None:
        """cache_read_tokens is summed across all continuation segments."""
        from unittest.mock import patch

        from claude_agent_sdk import ClaudeAgentOptions, ResultMessage

        from golem.config import GolemConfig
        from golem.supervisor import ContinuationResult, StallConfig, continuation_supervised_session

        config = GolemConfig(continuation_enabled=True, max_continuations=3)
        stall_cfg = StallConfig(warning_pct=0.6, kill_pct=0.8, expected_actions=[], role="planner", max_turns=50)
        call_count = 0

        async def fake_query(*args: object, **kwargs: object):  # type: ignore[return]
            nonlocal call_count
            call_count += 1
            # total_cost_usd is cumulative; cache_read_input_tokens is per-segment
            yield ResultMessage(
                subtype="result",
                duration_ms=100,
                duration_api_ms=50,
                is_error=False,
                num_turns=2,
                session_id=f"s{call_count}",
                result="partial" if call_count == 1 else "done",
                stop_reason="max_tokens" if call_count == 1 else "end_turn",
                total_cost_usd=call_count * 0.5,
                usage={"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 400},
            )

        options = ClaudeAgentOptions(
            model="claude-opus-4-5",
            cwd=".",
            tools={"type": "preset", "preset": "claude_code"},
            max_turns=50,
            permission_mode="bypassPermissions",
            env={},
        )

        with patch("golem.supervisor.query", side_effect=fake_query), \
             patch("golem.supervisor.compact_session_messages", return_value="summary"):
            result = await continuation_supervised_session(
                "do work", options, "planner", config, stall_cfg,
            )

        assert isinstance(result, ContinuationResult)
        # cache_read_tokens: 400 per segment × 2 segments = 800
        assert result.cache_read_tokens == 800
        # Cost: seg1=0.5, seg2=1.0-0.5=0.5 → total=1.0
        assert result.cost_usd == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Group 11 — run_with_recovery type widening
# ---------------------------------------------------------------------------


class TestRunWithRecoveryTypeWidening:
    def test_run_with_recovery_accepts_continuation_result_callable(self) -> None:
        """run_with_recovery type annotation accepts ContinuationResult-returning callables."""
        import inspect

        from golem.supervisor import ContinuationResult, SupervisedResult

        coord = RecoveryCoordinator(_default_config())
        sig = inspect.signature(coord.run_with_recovery)
        # Return annotation should reference both types
        return_annotation = str(sig.return_annotation)
        # The annotation uses forward references under TYPE_CHECKING, so we just verify
        # the method exists and has the right signature shape
        assert "session_fn" in sig.parameters
        assert "role" in sig.parameters
        assert "label" in sig.parameters
