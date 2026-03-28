"""Parallel subagent executor with rate limit backoff.

Provides ParallelExecutor for running multiple SDK subagent sessions
concurrently with:
  - asyncio.Semaphore-based concurrency control (configurable max_concurrency)
  - Per-call failure isolation (asyncio.gather with return_exceptions=True)
  - Stagger delay between launches (1s default, prevents burst 429s)
  - Exponential backoff between batches on rate limit detection (30s base, 300s cap)
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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Generic, TypeVar

if TYPE_CHECKING:
    from golem.events import EventBus

# -- Constants --
_DEFAULT_MAX_CONCURRENCY: int = 3
_RATE_LIMIT_BASE_DELAY_S: float = 30.0
_RATE_LIMIT_MAX_DELAY_S: float = 300.0
_STAGGER_DELAY_S: float = 1.0

T = TypeVar("T")

# Type alias: the function signature for a single subagent call.
# str is the subtask_id; the runner is responsible for loading ticket context etc.
SubtaskRunner = Callable[[str], Awaitable[T]]


# -- Exceptions --

class RateLimitError(RuntimeError):
    """Raised when the Claude API returns a 429 or rate limit message."""


def _is_rate_limit_error(exc: Exception) -> bool:
    """Detect rate limit from exception message."""
    msg = str(exc).lower()
    return "429" in msg or "rate limit" in msg or "too many requests" in msg


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

def _create_batches(items: list[str], batch_size: int) -> list[list[str]]:
    """Split items into non-overlapping sequential windows of batch_size."""
    return [items[i:i + batch_size] for i in range(0, len(items), batch_size)]


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
    """Batch-parallel subagent runner with rate limit backoff.

    Splits subtask_ids into fixed-size batches, runs each batch concurrently
    with asyncio.gather(return_exceptions=True), staggers launches within a
    batch by stagger_delay_s, and applies exponential backoff between batches
    when rate limits are detected.

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
        """Execute all subtasks in parallel batches with rate limit backoff.

        Splits subtask_ids into fixed-size batches of max_concurrency. Each batch
        runs concurrently via asyncio.gather(return_exceptions=True). A rate limit
        result in any batch triggers exponential backoff before the next batch starts.
        """
        if not subtask_ids:
            return BatchResult()

        batches = _create_batches(subtask_ids, self._max_concurrency)
        all_results: list[SubtaskResult[T]] = []
        rate_limit_backoff_s: float = 0.0

        for batch in batches:
            if self._cancel_event.is_set():
                break

            if rate_limit_backoff_s > 0:
                await self._emit_rate_limited(rate_limit_backoff_s)
                await _interruptible_sleep(rate_limit_backoff_s, self._cancel_event)
                rate_limit_backoff_s = 0.0

            coros = [
                self._run_single(subtask_id, runner, index * self._stagger_delay_s)
                for index, subtask_id in enumerate(batch)
            ]

            outcomes = await asyncio.gather(*coros, return_exceptions=True)

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
                    if outcome.rate_limited:
                        # Exponential backoff: base * 2^(count of rate-limited so far)
                        rl_count = sum(1 for r in all_results if r.rate_limited)
                        rate_limit_backoff_s = min(
                            self._rate_limit_base_s * (2 ** rl_count),
                            self._rate_limit_max_s,
                        )

        success_count = sum(1 for r in all_results if r.success)
        rl_count = sum(1 for r in all_results if r.rate_limited)
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
