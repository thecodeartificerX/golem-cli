"""Higher-level error classification for pipeline failure triage.

Provides four broad categories (infrastructure, application, integration, timeout)
that map onto recovery decisions (retry, rework, escalate, abort).  Complements the
lower-level ``FailureType`` enum in ``recovery.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ErrorCategory(Enum):
    INFRASTRUCTURE = "infrastructure"  # Rate limit, auth, SDK crash, OOM
    APPLICATION = "application"  # Bad code, wrong approach, lint failure
    INTEGRATION = "integration"  # Merge conflict, dependency mismatch
    TIMEOUT = "timeout"  # Stall, context exhaustion


@dataclass
class ClassifiedError:
    category: ErrorCategory
    original_error: str
    suggested_action: str  # "retry" | "rework" | "escalate" | "abort"
    is_retryable: bool


def classify_error(error_msg: str, error_type: str = "") -> ClassifiedError:
    """Classify an error into a category with a suggested action."""
    lower = error_msg.lower()

    # Infrastructure
    if any(kw in lower for kw in ["rate limit", "429", "overloaded", "auth", "401", "403"]):
        return ClassifiedError(
            category=ErrorCategory.INFRASTRUCTURE,
            original_error=error_msg,
            suggested_action="retry",
            is_retryable=True,
        )

    # Timeout / Stall
    if any(kw in lower for kw in ["stall", "timeout", "context_exhausted", "max_turns"]):
        return ClassifiedError(
            category=ErrorCategory.TIMEOUT,
            original_error=error_msg,
            suggested_action="retry",
            is_retryable=True,
        )

    # Integration
    if any(kw in lower for kw in ["merge conflict", "rebase failed", "conflict in"]):
        return ClassifiedError(
            category=ErrorCategory.INTEGRATION,
            original_error=error_msg,
            suggested_action="rework",
            is_retryable=False,
        )

    # Application (default)
    return ClassifiedError(
        category=ErrorCategory.APPLICATION,
        original_error=error_msg,
        suggested_action="rework",
        is_retryable=False,
    )
