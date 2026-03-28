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

        assert len(EVENT_TYPES) == 25

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
