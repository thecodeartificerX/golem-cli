"""Error classification and graceful recovery for Golem agent sessions.

Provides FailureType taxonomy, classify_failure() with regex pattern matching
(ported from Aperant TypeScript patterns), CircularFixDetector with djb2 hashing,
recovery_delay() exponential backoff schedule, and RecoveryCoordinator which
wraps supervised_session() calls with classification-aware retry logic.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from claude_agent_sdk import CLIConnectionError, CLINotFoundError, ClaudeSDKError

from golem.config import GolemConfig

if TYPE_CHECKING:
    from golem.events import EventBus
    from golem.supervisor import SupervisedResult


# ---------------------------------------------------------------------------
# 1. Failure taxonomy
# ---------------------------------------------------------------------------


class FailureType(str, Enum):
    rate_limit = "rate_limit"
    auth_failure = "auth_failure"
    billing_failure = "billing_failure"
    context_exhausted = "context_exhausted"
    sdk_error = "sdk_error"
    infrastructure = "infrastructure"
    circular_fix = "circular_fix"
    timeout = "timeout"
    unknown = "unknown"


# ---------------------------------------------------------------------------
# 2. Detection patterns (ported from Aperant TypeScript)
# ---------------------------------------------------------------------------

# Primary Claude Code CLI output: "Limit reached · resets Dec 17 at 6am (Europe/Oslo)"
# Both middle-dot (·) and bullet (•) are handled.
_RATE_LIMIT_PRIMARY = re.compile(
    r"Limit\s+reached\s*[·•]\s*resets\s+(.+?)(?:\s*$|\n)",
    re.IGNORECASE | re.MULTILINE,
)

# Weekly vs session classification: weekly has a month name + day number
_RATE_LIMIT_WEEKLY = re.compile(r"[A-Za-z]{3}\s+\d+|week", re.IGNORECASE)

# Secondary fallback indicators (no reset time extracted)
_RATE_LIMIT_INDICATORS: list[re.Pattern[str]] = [
    re.compile(r"rate\s*limit", re.IGNORECASE),
    re.compile(r"usage\s*limit", re.IGNORECASE),
    re.compile(r"limit\s*reached", re.IGNORECASE),
    re.compile(r"exceeded.*limit", re.IGNORECASE),
    re.compile(r"too\s*many\s*requests", re.IGNORECASE),
    re.compile(r"429", re.IGNORECASE),
]

# Auth failure patterns — intentionally specific to avoid matching AI-generated
# text discussing authentication.
_AUTH_FAILURE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"""["']?type["']?\s*:\s*["']?authentication_error["']?""", re.IGNORECASE),
    re.compile(r"API\s*Error:\s*401", re.IGNORECASE),
    re.compile(r"oauth\s*token\s+has\s+expired", re.IGNORECASE),
    re.compile(r"please\s+(obtain\s+a\s+new|refresh\s+your)\s+(existing\s+)?token", re.IGNORECASE),
    re.compile(r"\[.*\]\s*authentication\s*(is\s*)?required", re.IGNORECASE),
    re.compile(r"\[.*\]\s*not\s*(yet\s*)?authenticated", re.IGNORECASE),
    re.compile(r"\[.*\]\s*login\s*(is\s*)?required", re.IGNORECASE),
    re.compile(r"status[:\s]+401", re.IGNORECASE),
    re.compile(r"HTTP\s*401", re.IGNORECASE),
    re.compile(r"·\s*Please\s+run\s+/login", re.IGNORECASE),
]

# Billing failure patterns — checked BEFORE auth patterns because some billing responses
# (e.g. HTTP 402 Payment Required) overlap with auth error message formats.
# These indicate an account/payment problem requiring human intervention — no retry.
_BILLING_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"insufficient[_\s]credits?", re.IGNORECASE),
    re.compile(r"out\s+of\s+credits?", re.IGNORECASE),
    re.compile(r"credit\s+balance\s+(?:is\s+)?too\s+low", re.IGNORECASE),
    re.compile(r"extra[_\s]usage", re.IGNORECASE),
    re.compile(r"HTTP\s*402", re.IGNORECASE),
    re.compile(r"payment[_\s]required", re.IGNORECASE),
    re.compile(r"billing[_\s]error", re.IGNORECASE),
    re.compile(r"subscription[_\s]inactive", re.IGNORECASE),
    re.compile(r"account[_\s]suspended", re.IGNORECASE),
    re.compile(r"plan[_\s]limit[_\s]exceeded", re.IGNORECASE),
    re.compile(r"usage[_\s]limit\s+(?:reached|exceeded)", re.IGNORECASE),
    re.compile(r"spending[_\s]limit", re.IGNORECASE),
    re.compile(r"quota[_\s]exceeded", re.IGNORECASE),
    re.compile(r"free[_\s]tier[_\s]limit", re.IGNORECASE),
    re.compile(r"trial[_\s]expired", re.IGNORECASE),
    re.compile(r"overdue[_\s]payment", re.IGNORECASE),
    re.compile(r"payment[_\s]declined", re.IGNORECASE),
    re.compile(r"billing[_\s]disabled", re.IGNORECASE),
    re.compile(r"status[:\s]+402", re.IGNORECASE),
    re.compile(r"""["']?type["']?\s*:\s*["']?billing_error["']?""", re.IGNORECASE),
]

# Context exhaustion patterns
_CONTEXT_EXHAUSTED_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"context\s*(?:window\s*)?(?:is\s*)?(?:full|exhausted|overflow)", re.IGNORECASE),
    re.compile(r"token\s*limit\s*(?:reached|exceeded)", re.IGNORECASE),
    re.compile(r"maximum\s*(?:context\s*)?length", re.IGNORECASE),
    re.compile(r"prompt\s*(?:is\s*)?too\s*long", re.IGNORECASE),
    re.compile(r"exceeds\s*(?:the\s*)?(?:maximum|max)\s*(?:context|token)", re.IGNORECASE),
]


# ---------------------------------------------------------------------------
# 3. Classification function
# ---------------------------------------------------------------------------


def classify_failure(exc: BaseException | None, result_text: str = "") -> FailureType:
    """Classify an exception (or a stall result_text) into a FailureType.

    Args:
        exc: The exception that was raised, or None if the session returned a stall result.
        result_text: The result_text from SupervisedResult (used when exc is None, or to
                     supplement the exception message).

    Returns:
        FailureType enum member. Never raises.
    """
    # Infrastructure: broken environment — fail fast, no retry
    if isinstance(exc, CLINotFoundError):
        return FailureType.infrastructure

    # Timeout
    if isinstance(exc, asyncio.TimeoutError):
        return FailureType.timeout

    # Build a combined text blob to search
    parts: list[str] = []
    if exc is not None:
        parts.append(type(exc).__name__)
        parts.append(str(exc))
    if result_text:
        parts.append(result_text[:2000])  # cap to 2 KB — no need to scan full output
    text = "\n".join(parts)

    # Billing: fail fast — human intervention required (checked before auth;
    # some billing messages embed auth-like language, e.g. 402 responses)
    for pat in _BILLING_PATTERNS:
        if pat.search(text):
            return FailureType.billing_failure

    # Auth: fail fast — human intervention required
    for pat in _AUTH_FAILURE_PATTERNS:
        if pat.search(text):
            return FailureType.auth_failure

    # Rate limit: wait + retry with backoff
    if _RATE_LIMIT_PRIMARY.search(text):
        return FailureType.rate_limit
    for pat in _RATE_LIMIT_INDICATORS:
        if pat.search(text):
            return FailureType.rate_limit

    # Context exhaustion: retry with truncated prompt
    for pat in _CONTEXT_EXHAUSTED_PATTERNS:
        if pat.search(text):
            return FailureType.context_exhausted

    # SDK / connection error: exponential backoff
    if isinstance(exc, (CLIConnectionError, ClaudeSDKError)):
        return FailureType.sdk_error

    if exc is not None:
        return FailureType.unknown

    # No exception, no text match — could be a stall result
    return FailureType.sdk_error


# ---------------------------------------------------------------------------
# 4. Circular fix detection
# ---------------------------------------------------------------------------

_CIRCULAR_FIX_WINDOW_S: float = 2 * 60 * 60  # 2 hours


@dataclass
class _AttemptRecord:
    error_hash: str
    timestamp: float  # monotonic time.time()


def _error_hash(text: str) -> str:
    """djb2-style non-cryptographic hash, returns base-36 string.

    Normalises to lowercase + stripped before hashing so minor whitespace
    differences in the same error message produce the same hash.
    """
    normalized = text.lower().strip()
    h = 0
    for ch in normalized:
        h = ((h << 5) - h + ord(ch)) & 0xFFFFFFFF
    # Produce a signed 32-bit integer (mirrors JS `| 0` behaviour)
    if h >= 0x80000000:
        h -= 0x100000000
    return str(h) if h != 0 else "0"


class CircularFixDetector:
    """Per-ticket attempt history for circular fix detection.

    Usage:
        detector = CircularFixDetector(threshold=3)
        detector.record(ticket_id, error_text)
        if detector.is_circular(ticket_id):
            raise RecoveryExhausted(...)
    """

    def __init__(self, threshold: int = 3) -> None:
        self._threshold = threshold
        self._history: dict[str, list[_AttemptRecord]] = {}

    def record(self, ticket_id: str, error_text: str) -> None:
        """Record a new error attempt for ticket_id."""
        h = _error_hash(error_text)
        now = time.time()
        records = self._history.setdefault(ticket_id, [])
        records.append(_AttemptRecord(error_hash=h, timestamp=now))
        # Trim old records outside the window
        cutoff = now - _CIRCULAR_FIX_WINDOW_S
        self._history[ticket_id] = [r for r in records if r.timestamp > cutoff]

    def is_circular(self, ticket_id: str) -> bool:
        """Return True if the same error hash appears >= threshold times in the window."""
        records = self._history.get(ticket_id, [])
        counts: dict[str, int] = {}
        for r in records:
            counts[r.error_hash] = counts.get(r.error_hash, 0) + 1
            if counts[r.error_hash] >= self._threshold:
                return True
        return False

    def clear(self, ticket_id: str) -> None:
        """Reset history for a ticket (call on success)."""
        self._history.pop(ticket_id, None)


# ---------------------------------------------------------------------------
# 5. Recovery delay schedule
# ---------------------------------------------------------------------------


def recovery_delay(
    failure_type: FailureType,
    attempt: int,
    config: GolemConfig,
) -> float:
    """Return seconds to sleep before the next retry attempt.

    Attempt 0 = first retry after the initial failure.
    Attempt 1 = second retry, and so on.

    Rate limit uses a fixed cooldown (from config); all others use
    exponential backoff starting at config.retry_delay with a 2x multiplier
    and a cap of 120 s.

    Returns 0.0 for failure types that must not retry (caller should have
    raised before calling this).
    """
    if failure_type == FailureType.rate_limit:
        return float(config.rate_limit_cooldown_s)

    if failure_type in (FailureType.auth_failure, FailureType.billing_failure, FailureType.infrastructure):
        return 0.0  # should never be called — callers raise immediately

    # sdk_error, context_exhausted, timeout, unknown: exponential backoff
    base = float(config.retry_delay)
    delay = base * (2**attempt)
    return min(delay, 120.0)


# ---------------------------------------------------------------------------
# 6. RecoveryExhausted exception
# ---------------------------------------------------------------------------


class RecoveryExhausted(RuntimeError):
    """Raised when all recovery attempts are exhausted or a hard failure occurs."""

    def __init__(
        self,
        message: str,
        failure_type: FailureType,
        attempts: int,
    ) -> None:
        super().__init__(message)
        self.failure_type = failure_type
        self.attempts = attempts


# ---------------------------------------------------------------------------
# 7. RecoveryCoordinator
# ---------------------------------------------------------------------------


class RecoveryCoordinator:
    """Wraps supervised_session() calls with error classification and retry logic.

    Attributes:
        config: GolemConfig — read for retry limits and delays.
        circular_detector: CircularFixDetector — shared across all calls so
            circular loops are detected across stall-retries too.

    Thread safety: not thread-safe. One coordinator per async pipeline task.
    """

    def __init__(self, config: GolemConfig) -> None:
        self._config = config
        self._circular = CircularFixDetector(threshold=config.circular_fix_threshold)

    async def run_with_recovery(
        self,
        session_fn: Callable[[], Coroutine[None, None, SupervisedResult]],
        role: str,
        label: str,
        golem_dir: Path | None = None,
        event_bus: EventBus | None = None,
    ) -> SupervisedResult:
        """Run session_fn with classification-aware retry.

        Retry budget per failure type:
          rate_limit        -> config.max_rate_limit_retries (default 3)
          sdk_error/unknown -> config.max_retries (default 2)
          timeout           -> 1 immediate retry, then exponential up to max_retries
          context_exhausted -> 1 retry only (caller must modify the prompt externally)
          auth_failure      -> 0 retries (raise RecoveryExhausted immediately)
          billing_failure   -> 0 retries (raise RecoveryExhausted immediately)
          infrastructure    -> 0 retries (raise RecoveryExhausted immediately)
          circular_fix      -> 0 retries (raise RecoveryExhausted)

        The session_fn is called fresh each iteration. Callers are responsible
        for building escalated prompts when retrying after a stall — the
        RecoveryCoordinator only handles exception-level failures.

        Returns the first successful SupervisedResult. Raises RecoveryExhausted
        on terminal failure or when all retries are exhausted.
        """
        attempt = 0
        last_failure_type = FailureType.unknown

        while True:
            try:
                result = await session_fn()
            except CLINotFoundError as exc:
                await self._emit_classified(
                    event_bus, role, label, FailureType.infrastructure, attempt, str(exc)
                )
                raise RecoveryExhausted(
                    f"[{role}/{label}] infrastructure failure: 'claude' CLI not found. "
                    "Run 'claude login' to install and authenticate.",
                    FailureType.infrastructure,
                    attempt,
                ) from exc
            except BaseException as exc:
                failure_type = classify_failure(exc)
                last_failure_type = failure_type

                await self._emit_classified(
                    event_bus, role, label, failure_type, attempt, str(exc)
                )

                if failure_type == FailureType.auth_failure:
                    raise RecoveryExhausted(
                        f"[{role}/{label}] auth failure — re-run 'claude login': {exc}",
                        failure_type,
                        attempt,
                    ) from exc

                if failure_type == FailureType.billing_failure:
                    raise RecoveryExhausted(
                        f"[{role}/{label}] billing failure — check account credits/subscription: {exc}",
                        failure_type,
                        attempt,
                    ) from exc

                max_att = self._max_attempts_for(failure_type)
                if attempt >= max_att:
                    raise RecoveryExhausted(
                        f"[{role}/{label}] {failure_type} exhausted after {attempt + 1} attempts. "
                        f"Last error: {exc}",
                        failure_type,
                        attempt,
                    ) from exc

                # Record for circular fix detection
                self._circular.record(label, str(exc))
                if self._circular.is_circular(label):
                    await self._emit_classified(
                        event_bus, role, label, FailureType.circular_fix, attempt, str(exc)
                    )
                    raise RecoveryExhausted(
                        f"[{role}/{label}] circular fix detected (same error hash >= "
                        f"{self._config.circular_fix_threshold}x in 2h window)",
                        FailureType.circular_fix,
                        attempt,
                    ) from exc

                delay = recovery_delay(failure_type, attempt, self._config)
                await self._emit_recovery_started(
                    event_bus, role, label, failure_type, attempt, delay
                )
                if delay > 0:
                    await asyncio.sleep(delay)

                attempt += 1
                continue

            # --- session_fn returned (no exception) ---
            # Check stall result — treat like sdk_error for recovery purposes
            if result.stalled:
                stall_text = result.result_text
                failure_type = classify_failure(None, stall_text)
                last_failure_type = failure_type

                await self._emit_classified(
                    event_bus, role, label, failure_type, attempt, "stall detected"
                )

                # Circular check on stall result text
                self._circular.record(label, stall_text[:500])
                if self._circular.is_circular(label):
                    await self._emit_classified(
                        event_bus, role, label, FailureType.circular_fix, attempt, stall_text[:200]
                    )
                    raise RecoveryExhausted(
                        f"[{role}/{label}] circular stall detected",
                        FailureType.circular_fix,
                        attempt,
                    )

                max_att = self._max_attempts_for(failure_type)
                if attempt >= max_att:
                    # Return the stalled result — caller decides whether to escalate prompt
                    # or raise. This preserves existing stall-escalation logic in callers.
                    return result

                delay = recovery_delay(failure_type, attempt, self._config)
                await self._emit_recovery_started(
                    event_bus, role, label, failure_type, attempt, delay
                )
                if delay > 0:
                    await asyncio.sleep(delay)

                attempt += 1
                continue

            # Clean result — clear circular history and return
            self._circular.clear(label)
            return result

    def _max_attempts_for(self, failure_type: FailureType) -> int:
        """Return max retry attempts (exclusive) for a failure type."""
        if failure_type == FailureType.rate_limit:
            return self._config.max_rate_limit_retries
        if failure_type == FailureType.context_exhausted:
            return 1
        if failure_type == FailureType.timeout:
            return self._config.max_retries
        return self._config.max_retries  # sdk_error, unknown

    async def _emit_classified(
        self,
        event_bus: EventBus | None,
        role: str,
        label: str,
        failure_type: FailureType,
        attempt: int,
        error_preview: str,
    ) -> None:
        if not event_bus:
            return
        from golem.events import AgentErrorClassified

        await event_bus.emit(
            AgentErrorClassified(
                role=role,
                label=label,
                failure_type=failure_type.value,
                attempt=attempt,
                error_preview=error_preview[:300],
            )
        )

    async def _emit_recovery_started(
        self,
        event_bus: EventBus | None,
        role: str,
        label: str,
        failure_type: FailureType,
        attempt: int,
        delay_s: float,
    ) -> None:
        if not event_bus:
            return
        from golem.events import AgentRecoveryStarted

        await event_bus.emit(
            AgentRecoveryStarted(
                role=role,
                label=label,
                failure_type=failure_type.value,
                attempt=attempt,
                delay_s=delay_s,
            )
        )
