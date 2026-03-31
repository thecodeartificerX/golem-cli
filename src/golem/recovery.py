"""Recovery coordination with failure classification.

Provides ``FailureType`` (8 low-level values) and ``classify_failure()`` for
granular failure identification, plus ``RecoveryCoordinator`` which wraps the
higher-level ``ErrorCategory`` taxonomy from ``error_taxonomy.py`` to drive
retry / escalate / abort decisions.
"""

from __future__ import annotations

import sys
from enum import Enum

from golem.error_taxonomy import ClassifiedError, classify_error
from golem.tickets import TicketStore


class FailureType(Enum):
    """Low-level failure types for granular classification."""

    RATE_LIMIT = "rate_limit"
    AUTH_ERROR = "auth_error"
    SDK_CRASH = "sdk_crash"
    OOM = "oom"
    BAD_CODE = "bad_code"
    LINT_FAILURE = "lint_failure"
    MERGE_CONFLICT = "merge_conflict"
    STALL = "stall"


def classify_failure(error_msg: str) -> FailureType:
    """Classify an error message into a low-level ``FailureType``."""
    lower = error_msg.lower()

    if "rate limit" in lower or "429" in lower:
        return FailureType.RATE_LIMIT
    if any(kw in lower for kw in ["auth", "401", "403"]):
        return FailureType.AUTH_ERROR
    if "sdk" in lower or "cli" in lower:
        return FailureType.SDK_CRASH
    if "oom" in lower or "out of memory" in lower:
        return FailureType.OOM
    if "lint" in lower or "ruff" in lower:
        return FailureType.LINT_FAILURE
    if "merge conflict" in lower or "conflict in" in lower:
        return FailureType.MERGE_CONFLICT
    if "stall" in lower or "timeout" in lower or "max_turns" in lower:
        return FailureType.STALL
    return FailureType.BAD_CODE


class RecoveryCoordinator:
    """Coordinates failure recovery using the error taxonomy.

    Given a ``TicketStore``, classifies errors at the higher taxonomy level
    and uses the classification to decide whether to retry, rework, escalate,
    or abort.  Failure events written to tickets include the error category.
    """

    def __init__(self, store: TicketStore, max_retries: int = 2) -> None:
        self._store = store
        self._max_retries = max_retries
        self._attempt_counts: dict[str, int] = {}

    def classify(self, error_msg: str, error_type: str = "") -> ClassifiedError:
        """Classify *error_msg* using the higher-level taxonomy."""
        return classify_error(error_msg, error_type)

    async def handle_failure(
        self,
        ticket_id: str,
        error_msg: str,
        agent: str = "system",
    ) -> str:
        """Handle a ticket failure: classify, record, and decide next action.

        Returns the ``suggested_action`` from the taxonomy (``"retry"``,
        ``"rework"``, ``"escalate"``, or ``"abort"``).
        """
        classified = self.classify(error_msg)

        # Track per-ticket attempts
        attempts = self._attempt_counts.get(ticket_id, 0) + 1
        self._attempt_counts[ticket_id] = attempts

        # Record failure with category prefix in the ticket history
        note = f"[{classified.category.value}] {error_msg}"

        await self._store.update(
            ticket_id,
            status="failed",
            note=note,
            agent=agent,
        )

        print(
            f"[RECOVERY] {ticket_id}: {classified.category.value} failure "
            f"(attempt {attempts}/{self._max_retries + 1}) — {classified.suggested_action}",
            file=sys.stderr,
        )

        # Decide action based on retryability and attempt budget
        if classified.is_retryable and attempts <= self._max_retries:
            return "retry"

        if attempts > self._max_retries:
            return "escalate"

        return classified.suggested_action

    def should_retry(self, error_msg: str, ticket_id: str) -> bool:
        """Quick check: is this error retryable and within budget?"""
        classified = self.classify(error_msg)
        attempts = self._attempt_counts.get(ticket_id, 0)
        return classified.is_retryable and attempts <= self._max_retries

    def reset_attempts(self, ticket_id: str) -> None:
        """Reset the attempt counter for a ticket (e.g. after successful recovery)."""
        self._attempt_counts.pop(ticket_id, None)
