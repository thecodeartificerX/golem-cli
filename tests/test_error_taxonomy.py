"""Tests for error_taxonomy.py — error classification taxonomy."""

from __future__ import annotations

from golem.error_taxonomy import ClassifiedError, ErrorCategory, classify_error


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

    def test_classified_error_is_dataclass(self) -> None:
        result = classify_error("some error")
        assert isinstance(result, ClassifiedError)
        assert hasattr(result, "category")
        assert hasattr(result, "is_retryable")
        assert hasattr(result, "suggested_action")
        assert hasattr(result, "original_error")
