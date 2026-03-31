"""Parallel subagent executor with rate limit backoff.

Provides ParallelExecutor for running multiple SDK subagent sessions
concurrently with:
  - asyncio.Semaphore-based concurrency control (configurable max_concurrency)
  - Per-call failure isolation (asyncio.gather with return_exceptions=True)
  - Stagger delay before semaphore acquire (1s default, prevents burst 429s)
  - Exponential backoff after rate-limit detection (30s base, 300s cap, exponent capped at 5)
  - Precise backoff from rate_limit_resets_at timestamp when available (supersedes geometric formula)
  - Work-stealing: all tasks launched at once, semaphore gates concurrency naturally
  - EventBus events: SubtaskStarted, SubtaskCompleted, SubtaskFailed, SubtaskBatchRateLimited
  - Cooperative cancellation via asyncio.Event

Usage:
    executor = ParallelExecutor(max_concurrency=3, event_bus=bus)
    result = await executor.run_batch(ticket_ids, runner_fn)
    for r in result.results:
        if not r.success:
            print(f"Ticket {r.subtask_id} failed: {r.error}")
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Generic, TypeVar

from golem.recovery import create_batches, is_rate_limit_exception

if TYPE_CHECKING:
    from golem.events import EventBus

# -- Constants --
_DEFAULT_MAX_CONCURRENCY: int = 3
_RATE_LIMIT_BASE_DELAY_S: float = 30.0
_RATE_LIMIT_MAX_DELAY_S: float = 300.0
_RATE_LIMIT_EXPONENT_CAP: int = 5          # max backoff = base * 2^5 = ~960s with 30s base
_STAGGER_DELAY_S: float = 1.0

T = TypeVar("T")

# Type alias: the function signature for a single subagent call.
# str is the subtask_id; the runner is responsible for loading ticket context etc.
SubtaskRunner = Callable[[str], Awaitable[T]]


# -- Exceptions --

class RateLimitError(RuntimeError):
    """Raised when the Claude API returns a 429 or rate limit message."""


# Backwards-compatibility alias for is_rate_limit_exception (renamed to avoid underscore prefix)
_is_rate_limit_error = is_rate_limit_exception


# -- Result types --

@dataclass
class SubtaskResult(Generic[T]):
    """Result of a single parallel subagent execution."""

    subtask_id: str
    success: bool
    result: T | None = None
    error: str = ""
    rate_limited: bool = False


@dataclass
class BatchResult(Generic[T]):
    """Result of the full parallel batch execution."""

    results: list[SubtaskResult[T]] = field(default_factory=list)
    success_count: int = 0
    failure_count: int = 0
    rate_limited_count: int = 0
    cancelled: bool = False


# -- Helper functions --

# Backwards-compatibility alias for create_batches (renamed to remove underscore prefix)
_create_batches = create_batches


async def _interruptible_sleep(delay_s: float, cancel_event: asyncio.Event) -> None:
    """Sleep for delay_s, but return early if cancel_event is set.

    Python equivalent of Aperant's delay(ms, signal) with abort support.
    asyncio.wait_for on the cancel event means we either wake early (cancel fires)
    or complete the full sleep (timeout = normal path).
    """
    try:
        await asyncio.wait_for(cancel_event.wait(), timeout=delay_s)
    except asyncio.TimeoutError:
        pass  # Normal path — slept the full duration without cancellation


# -- Main class --

class ParallelExecutor(Generic[T]):
    """Semaphore-gated parallel subagent runner with rate limit backoff.

    Launches ALL tasks at once, each gated by an asyncio.Semaphore limited to
    max_concurrency.  This is a work-stealing approach: when one task finishes
    the semaphore slot is released immediately and the next waiting task starts
    without waiting for an entire batch to complete.

    Stagger delay (index * stagger_delay_s) fires before semaphore acquire to
    prevent N tasks from piling up at the semaphore simultaneously.

    Rate limit backoff: when any task returns rate_limited=True, the next
    backoff period is computed as base * 2^min(rl_count, 5) (capped at max_delay
    and with exponent capped at 5 to prevent unbounded growth over long runs).

    Emits EventBus events: SubtaskStarted, SubtaskCompleted, SubtaskFailed,
    SubtaskBatchRateLimited.
    """

    def __init__(
        self,
        max_concurrency: int = _DEFAULT_MAX_CONCURRENCY,
        stagger_delay_s: float = _STAGGER_DELAY_S,
        rate_limit_base_delay_s: float = _RATE_LIMIT_BASE_DELAY_S,
        rate_limit_max_delay_s: float = _RATE_LIMIT_MAX_DELAY_S,
        event_bus: EventBus | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> None:
        self._max_concurrency = max_concurrency
        self._stagger_delay_s = stagger_delay_s
        self._rate_limit_base_s = rate_limit_base_delay_s
        self._rate_limit_max_s = rate_limit_max_delay_s
        self._event_bus = event_bus
        self._cancel_event = cancel_event or asyncio.Event()
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def run_batch(
        self,
        subtask_ids: list[str],
        runner: SubtaskRunner[T],
    ) -> BatchResult[T]:
        """Execute all subtasks with semaphore-gated work-stealing parallelism.

        All tasks are launched at once.  Each task staggers by index * stagger_delay_s
        before acquiring the semaphore, then runs under the semaphore.  When a slot
        is released the next waiting task starts immediately — no batch-wide barrier.

        After all tasks complete, if any were rate-limited, an interruptible backoff
        sleep runs before returning.  The backoff exponent is capped at
        _RATE_LIMIT_EXPONENT_CAP to prevent unbounded growth over long runs.
        """
        if not subtask_ids:
            return BatchResult()

        if self._cancel_event.is_set():
            cancelled_results: list[SubtaskResult[T]] = [
                SubtaskResult(subtask_id=sid, success=False, error="Cancelled", rate_limited=False)
                for sid in subtask_ids
            ]
            return BatchResult(
                results=cancelled_results,
                success_count=0,
                failure_count=len(cancelled_results),
                rate_limited_count=0,
                cancelled=True,
            )

        coros = [
            self._run_single(subtask_id, runner, index * self._stagger_delay_s)
            for index, subtask_id in enumerate(subtask_ids)
        ]

        outcomes = await asyncio.gather(*coros, return_exceptions=True)

        all_results: list[SubtaskResult[T]] = []
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                # Unexpected throw from _run_single itself (should not happen — it catches all)
                all_results.append(SubtaskResult(
                    subtask_id="unknown",
                    success=False,
                    error=str(outcome),
                    rate_limited=False,
                ))
            else:
                all_results.append(outcome)

        # Apply rate limit backoff once, after all tasks complete.
        # If any result carries a rate_limit_resets_at timestamp (from RateLimitEvent
        # via SupervisedResult/ContinuationResult), use it for precise sleep duration.
        # Fall back to geometric backoff when the timestamp is absent.
        rl_count = sum(1 for r in all_results if r.rate_limited)
        if rl_count > 0 and not self._cancel_event.is_set():
            # Scan all results for a rate_limit_resets_at timestamp on the T value.
            # Use the latest timestamp found (guards against multiple rate-limited tasks
            # with different window resets).  getattr is safe for generic T.
            precise_resets_at: float | None = None
            for r in all_results:
                if r.result is not None:
                    candidate = getattr(r.result, "rate_limit_resets_at", None)
                    if candidate is not None:
                        ts = float(candidate)
                        if precise_resets_at is None or ts > precise_resets_at:
                            precise_resets_at = ts

            if precise_resets_at is not None:
                backoff_s = max(0.0, precise_resets_at - time.time())
            else:
                exponent = min(rl_count, _RATE_LIMIT_EXPONENT_CAP)
                backoff_s = min(
                    self._rate_limit_base_s * (2 ** exponent),
                    self._rate_limit_max_s,
                )
            await self._emit_rate_limited(backoff_s)
            await _interruptible_sleep(backoff_s, self._cancel_event)

        success_count = sum(1 for r in all_results if r.success)
        return BatchResult(
            results=all_results,
            success_count=success_count,
            failure_count=len(all_results) - success_count,
            rate_limited_count=rl_count,
            cancelled=self._cancel_event.is_set(),
        )

    async def _run_single(
        self,
        subtask_id: str,
        runner: SubtaskRunner[T],
        stagger_delay_s: float,
    ) -> SubtaskResult[T]:
        """Run one subtask. Never raises — all errors become SubtaskResult(success=False).

        Stagger fires before semaphore acquisition to prevent piling up waiters that
        all rush in simultaneously. Semaphore ensures max_concurrency is respected.
        """
        if stagger_delay_s > 0:
            await _interruptible_sleep(stagger_delay_s, self._cancel_event)

        if self._cancel_event.is_set():
            return SubtaskResult(subtask_id=subtask_id, success=False, error="Cancelled", rate_limited=False)

        await self._emit_started(subtask_id)

        async with self._semaphore:
            try:
                result = await runner(subtask_id)
                await self._emit_completed(subtask_id)
                return SubtaskResult(subtask_id=subtask_id, success=True, result=result)
            except RateLimitError as exc:
                await self._emit_failed(subtask_id, str(exc), rate_limited=True)
                return SubtaskResult(subtask_id=subtask_id, success=False, error=str(exc), rate_limited=True)
            except Exception as exc:
                rate_limited = _is_rate_limit_error(exc)
                await self._emit_failed(subtask_id, str(exc), rate_limited=rate_limited)
                return SubtaskResult(subtask_id=subtask_id, success=False, error=str(exc), rate_limited=rate_limited)

    async def _emit_started(self, subtask_id: str) -> None:
        if self._event_bus:
            from golem.events import SubtaskStarted
            await self._event_bus.emit(SubtaskStarted(subtask_id=subtask_id))

    async def _emit_completed(self, subtask_id: str) -> None:
        if self._event_bus:
            from golem.events import SubtaskCompleted
            await self._event_bus.emit(SubtaskCompleted(subtask_id=subtask_id))

    async def _emit_failed(self, subtask_id: str, error: str, rate_limited: bool) -> None:
        if self._event_bus:
            from golem.events import SubtaskFailed
            await self._event_bus.emit(SubtaskFailed(
                subtask_id=subtask_id, error=error, rate_limited=rate_limited,
            ))

    async def _emit_rate_limited(self, backoff_s: float) -> None:
        if self._event_bus:
            from golem.events import SubtaskBatchRateLimited
            await self._event_bus.emit(SubtaskBatchRateLimited(backoff_s=backoff_s))
