"""Tests for golem.parallel — ParallelExecutor and rate limit backoff."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from golem.parallel import (
    BatchResult,
    ParallelExecutor,
    RateLimitError,
    SubtaskResult,
    _create_batches,
    _interruptible_sleep,
    _is_rate_limit_error,
)


# ---------------------------------------------------------------------------
# Unit tests for helper functions
# ---------------------------------------------------------------------------


def test_create_batches_even_split() -> None:
    """Items divide evenly into batches of the given size."""
    result = _create_batches(["a", "b", "c", "d", "e", "f"], 3)
    assert result == [["a", "b", "c"], ["d", "e", "f"]]


def test_create_batches_uneven_split() -> None:
    """Last batch is smaller when items don't divide evenly."""
    result = _create_batches(["a", "b", "c", "d", "e", "f", "g"], 3)
    assert result == [["a", "b", "c"], ["d", "e", "f"], ["g"]]


def test_create_batches_single_item() -> None:
    """Single item produces a single batch of size 1."""
    result = _create_batches(["only"], 3)
    assert result == [["only"]]


def test_create_batches_empty() -> None:
    """Empty input produces no batches."""
    result = _create_batches([], 3)
    assert result == []


def test_create_batches_batch_size_larger_than_items() -> None:
    """When batch_size > items, produces a single batch."""
    result = _create_batches(["a", "b"], 10)
    assert result == [["a", "b"]]


def test_is_rate_limit_error_429() -> None:
    """Message containing '429' is detected as a rate limit."""
    assert _is_rate_limit_error(RuntimeError("HTTP 429 Too Many Requests")) is True


def test_is_rate_limit_error_rate_limit_phrase() -> None:
    """Message containing 'rate limit' (case-insensitive) is detected."""
    assert _is_rate_limit_error(RuntimeError("Rate Limit exceeded")) is True
    assert _is_rate_limit_error(RuntimeError("API rate limit hit")) is True


def test_is_rate_limit_error_too_many_requests() -> None:
    """Message containing 'too many requests' is detected."""
    assert _is_rate_limit_error(RuntimeError("Too Many Requests from client")) is True


def test_is_rate_limit_error_no_match() -> None:
    """Regular errors are not detected as rate limits."""
    assert _is_rate_limit_error(RuntimeError("Connection refused")) is False
    assert _is_rate_limit_error(RuntimeError("Internal server error")) is False
    assert _is_rate_limit_error(RuntimeError("Timeout")) is False


@pytest.mark.asyncio
async def test_interruptible_sleep_completes_full_duration() -> None:
    """Sleep runs the full duration when cancel_event is not set."""
    cancel = asyncio.Event()
    start = time.monotonic()
    await _interruptible_sleep(0.05, cancel)
    elapsed = time.monotonic() - start
    assert elapsed >= 0.04  # Allow 10ms tolerance


@pytest.mark.asyncio
async def test_interruptible_sleep_cancelled_early() -> None:
    """Sleep returns early when cancel_event is set before timeout."""
    cancel = asyncio.Event()
    cancel.set()
    start = time.monotonic()
    await _interruptible_sleep(10.0, cancel)  # Would take 10s without cancellation
    elapsed = time.monotonic() - start
    assert elapsed < 1.0  # Returns immediately


@pytest.mark.asyncio
async def test_interruptible_sleep_cancelled_mid_sleep() -> None:
    """Sleep returns early when cancel_event is set during the sleep."""
    cancel = asyncio.Event()

    async def set_cancel_later() -> None:
        await asyncio.sleep(0.05)
        cancel.set()

    asyncio.create_task(set_cancel_later())
    start = time.monotonic()
    await _interruptible_sleep(10.0, cancel)
    elapsed = time.monotonic() - start
    assert elapsed < 1.0  # Woke up early


# ---------------------------------------------------------------------------
# 10.1 Basic Batch Execution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_succeed() -> None:
    """All subtasks complete successfully, success_count == len(subtask_ids)."""
    calls: list[str] = []

    async def runner(sid: str) -> str:
        calls.append(sid)
        return f"done-{sid}"

    executor: ParallelExecutor[str] = ParallelExecutor(max_concurrency=3, stagger_delay_s=0)
    result = await executor.run_batch(["a", "b", "c"], runner)
    assert result.success_count == 3
    assert result.failure_count == 0
    assert result.rate_limited_count == 0
    assert result.cancelled is False
    assert set(calls) == {"a", "b", "c"}
    assert all(r.success for r in result.results)
    assert all(r.result == f"done-{r.subtask_id}" for r in result.results)


@pytest.mark.asyncio
async def test_empty_batch() -> None:
    """Empty input returns BatchResult with all zeros and no results."""
    executor: ParallelExecutor[str] = ParallelExecutor()

    async def runner(sid: str) -> str:
        return "should not run"

    result = await executor.run_batch([], runner)
    assert result.success_count == 0
    assert result.failure_count == 0
    assert result.rate_limited_count == 0
    assert result.cancelled is False
    assert result.results == []


@pytest.mark.asyncio
async def test_single_subtask() -> None:
    """Single subtask completes with success_count == 1."""
    async def runner(sid: str) -> str:
        return "ok"

    executor: ParallelExecutor[str] = ParallelExecutor(stagger_delay_s=0)
    result = await executor.run_batch(["only"], runner)
    assert result.success_count == 1
    assert result.failure_count == 0


# ---------------------------------------------------------------------------
# 10.2 Failure Isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_failure_does_not_block_siblings() -> None:
    """A RuntimeError in one runner does not prevent others from completing."""
    async def runner(sid: str) -> str:
        if sid == "b":
            raise RuntimeError("exploded")
        return f"ok-{sid}"

    executor: ParallelExecutor[str] = ParallelExecutor(max_concurrency=3, stagger_delay_s=0)
    result = await executor.run_batch(["a", "b", "c"], runner)
    assert result.success_count == 2
    assert result.failure_count == 1
    failed = [r for r in result.results if not r.success]
    assert len(failed) == 1
    assert failed[0].subtask_id == "b"
    assert "exploded" in failed[0].error


@pytest.mark.asyncio
async def test_all_fail_still_returns_results() -> None:
    """Even when all tasks fail, BatchResult is returned with all failures."""
    async def runner(sid: str) -> str:
        raise ValueError(f"fail-{sid}")

    executor: ParallelExecutor[str] = ParallelExecutor(max_concurrency=3, stagger_delay_s=0)
    result = await executor.run_batch(["x", "y", "z"], runner)
    assert result.success_count == 0
    assert result.failure_count == 3
    assert len(result.results) == 3
    assert all(not r.success for r in result.results)


@pytest.mark.asyncio
async def test_multiple_batches_isolated_failure() -> None:
    """Failure in one batch does not prevent the next batch from running."""
    completed: list[str] = []

    async def runner(sid: str) -> str:
        if sid == "b":
            raise RuntimeError("batch1 failure")
        completed.append(sid)
        return "ok"

    # max_concurrency=1 forces sequential batches: [a], [b], [c]
    executor: ParallelExecutor[str] = ParallelExecutor(max_concurrency=1, stagger_delay_s=0)
    result = await executor.run_batch(["a", "b", "c"], runner)
    assert result.success_count == 2
    assert result.failure_count == 1
    assert set(completed) == {"a", "c"}


# ---------------------------------------------------------------------------
# 10.3 Rate Limit Detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limit_error_sets_flag() -> None:
    """RateLimitError is caught and result.rate_limited == True."""
    async def runner(sid: str) -> str:
        raise RateLimitError("429 Too Many Requests")

    executor: ParallelExecutor[str] = ParallelExecutor(max_concurrency=3, stagger_delay_s=0)
    result = await executor.run_batch(["x"], runner)
    assert result.rate_limited_count == 1
    assert result.results[0].rate_limited is True
    assert result.results[0].success is False


@pytest.mark.asyncio
async def test_rate_limit_string_detection() -> None:
    """Generic exception containing '429' is also detected as rate limited."""
    async def runner(sid: str) -> str:
        raise RuntimeError("API error: status 429 rate limit exceeded")

    executor: ParallelExecutor[str] = ParallelExecutor(max_concurrency=3, stagger_delay_s=0)
    result = await executor.run_batch(["x"], runner)
    assert result.results[0].rate_limited is True


@pytest.mark.asyncio
async def test_rate_limit_via_too_many_requests_string() -> None:
    """Exception with 'too many requests' is detected as rate limited."""
    async def runner(sid: str) -> str:
        raise RuntimeError("Too Many Requests from client API")

    executor: ParallelExecutor[str] = ParallelExecutor(max_concurrency=3, stagger_delay_s=0)
    result = await executor.run_batch(["x"], runner)
    assert result.results[0].rate_limited is True


@pytest.mark.asyncio
async def test_non_rate_limit_error_not_flagged() -> None:
    """Standard exceptions are NOT flagged as rate limited."""
    async def runner(sid: str) -> str:
        raise RuntimeError("Connection refused by server")

    executor: ParallelExecutor[str] = ParallelExecutor(max_concurrency=3, stagger_delay_s=0)
    result = await executor.run_batch(["x"], runner)
    assert result.results[0].rate_limited is False
    assert result.rate_limited_count == 0


# ---------------------------------------------------------------------------
# 10.4 Backoff Between Batches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backoff_applied_after_rate_limited_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    """After a rate-limited result, the next batch waits the computed backoff."""
    slept: list[float] = []

    async def fake_sleep(s: float, _: asyncio.Event) -> None:
        slept.append(s)

    monkeypatch.setattr("golem.parallel._interruptible_sleep", fake_sleep)

    call_count = 0

    async def runner(sid: str) -> str:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RateLimitError("429")
        return "ok"

    executor: ParallelExecutor[str] = ParallelExecutor(
        max_concurrency=1,
        stagger_delay_s=0,
        rate_limit_base_delay_s=30.0,
    )
    result = await executor.run_batch(["a", "b"], runner)
    # "a" rate-limited (count=1), backoff = 30 * 2^1 = 60s before "b"
    assert 60.0 in slept
    assert result.success_count == 1
    assert result.rate_limited_count == 1


@pytest.mark.asyncio
async def test_backoff_capped_at_max(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backoff never exceeds rate_limit_max_delay_s (300s default)."""
    slept: list[float] = []

    async def fake_sleep(s: float, _: asyncio.Event) -> None:
        slept.append(s)

    monkeypatch.setattr("golem.parallel._interruptible_sleep", fake_sleep)

    call_count = 0

    async def runner(sid: str) -> str:
        nonlocal call_count
        call_count += 1
        # All calls rate-limited except the last
        if call_count <= 10:
            raise RateLimitError("429")
        return "ok"

    executor: ParallelExecutor[str] = ParallelExecutor(
        max_concurrency=1,
        stagger_delay_s=0,
        rate_limit_base_delay_s=30.0,
        rate_limit_max_delay_s=300.0,
    )
    # Run enough to exhaust the cap
    ids = [str(i) for i in range(12)]
    await executor.run_batch(ids, runner)
    # No sleep should exceed 300s
    assert all(s <= 300.0 for s in slept)


@pytest.mark.asyncio
async def test_no_backoff_without_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """No backoff sleep occurs when no rate limits are encountered."""
    slept: list[float] = []

    async def fake_sleep(s: float, _: asyncio.Event) -> None:
        slept.append(s)

    monkeypatch.setattr("golem.parallel._interruptible_sleep", fake_sleep)

    async def runner(sid: str) -> str:
        return "ok"

    # Two tasks, no stagger
    executor: ParallelExecutor[str] = ParallelExecutor(max_concurrency=1, stagger_delay_s=0)
    await executor.run_batch(["a", "b"], runner)
    # fake_sleep should not be called (no stagger, no backoff)
    assert slept == []


@pytest.mark.asyncio
async def test_backoff_exponent_capped_at_5(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backoff exponent is capped at 5 regardless of how many tasks rate-limited."""
    slept: list[float] = []

    async def fake_sleep(s: float, _: asyncio.Event) -> None:
        slept.append(s)

    monkeypatch.setattr("golem.parallel._interruptible_sleep", fake_sleep)

    async def always_rate_limit(sid: str) -> str:
        raise RateLimitError("429")

    # 10 tasks all rate-limited -- exponent should cap at 5 not grow to 10
    executor: ParallelExecutor[str] = ParallelExecutor(
        max_concurrency=10,
        stagger_delay_s=0,
        rate_limit_base_delay_s=30.0,
        rate_limit_max_delay_s=9999.0,   # disable the absolute cap to test exponent cap alone
    )
    await executor.run_batch([str(i) for i in range(10)], always_rate_limit)
    # exponent = min(10, 5) = 5 => 30 * 32 = 960s
    assert len(slept) == 1
    assert slept[0] == pytest.approx(960.0)


@pytest.mark.asyncio
async def test_backoff_exponent_cap_below_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    """When rl_count is below cap (<=5), exponent equals rl_count exactly."""
    slept: list[float] = []

    async def fake_sleep(s: float, _: asyncio.Event) -> None:
        slept.append(s)

    monkeypatch.setattr("golem.parallel._interruptible_sleep", fake_sleep)

    call_count = 0

    async def runner(sid: str) -> str:
        nonlocal call_count
        call_count += 1
        if call_count <= 3:
            raise RateLimitError("429")
        return "ok"

    executor: ParallelExecutor[str] = ParallelExecutor(
        max_concurrency=5,
        stagger_delay_s=0,
        rate_limit_base_delay_s=30.0,
        rate_limit_max_delay_s=9999.0,
    )
    # 3 rate-limited + 2 success: exponent = min(3, 5) = 3 => 30 * 8 = 240s
    await executor.run_batch([str(i) for i in range(5)], runner)
    assert len(slept) == 1
    assert slept[0] == pytest.approx(240.0)


# ---------------------------------------------------------------------------
# 10.4b Precise backoff from rate_limit_resets_at
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backoff_uses_resets_at_timestamp_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """When a task result has rate_limit_resets_at set, the executor uses that
    timestamp for backoff instead of the geometric formula."""
    from dataclasses import dataclass

    slept: list[float] = []

    async def fake_sleep(s: float, _: asyncio.Event) -> None:
        slept.append(s)

    monkeypatch.setattr("golem.parallel._interruptible_sleep", fake_sleep)

    # Freeze time.time() so the precise delay is deterministic.
    frozen_now = 1_000_000.0
    monkeypatch.setattr("golem.parallel.time.time", lambda: frozen_now)

    @dataclass
    class FakeResult:
        rate_limit_resets_at: float | None

    # Runner returns a result with resets_at set 120s from "now".
    async def runner(sid: str) -> FakeResult:
        if sid == "a":
            # Return a successful result that carries the timestamp.
            return FakeResult(rate_limit_resets_at=frozen_now + 120.0)
        return FakeResult(rate_limit_resets_at=None)

    # Mark task "a" as rate-limited by raising RateLimitError so rl_count > 0,
    # but we also need a result that carries the timestamp.
    # Since RateLimitError is an exception path (no result), test via a runner that
    # returns a successful result with rate_limit_resets_at while a *sibling* task
    # raises RateLimitError to trigger rl_count > 0.

    slept.clear()

    call_order: list[str] = []

    async def mixed_runner(sid: str) -> FakeResult:
        call_order.append(sid)
        if sid == "rl":
            raise RateLimitError("429")
        # "carrier" task returns result with resets_at so executor can read it
        return FakeResult(rate_limit_resets_at=frozen_now + 120.0)

    executor: ParallelExecutor[FakeResult] = ParallelExecutor(
        max_concurrency=2,
        stagger_delay_s=0,
        rate_limit_base_delay_s=30.0,
        rate_limit_max_delay_s=9999.0,
    )
    await executor.run_batch(["rl", "carrier"], mixed_runner)

    # Precise delay = max(0, (frozen_now+120) - frozen_now) = 120.0
    assert len(slept) == 1
    assert slept[0] == pytest.approx(120.0)


@pytest.mark.asyncio
async def test_backoff_uses_geometric_when_resets_at_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """When rate_limit_resets_at is None on all results, geometric backoff is used."""
    from dataclasses import dataclass

    slept: list[float] = []

    async def fake_sleep(s: float, _: asyncio.Event) -> None:
        slept.append(s)

    monkeypatch.setattr("golem.parallel._interruptible_sleep", fake_sleep)

    @dataclass
    class FakeResult:
        rate_limit_resets_at: float | None

    # All tasks raise RateLimitError (no result carrying resets_at).
    async def always_rate_limit(sid: str) -> FakeResult:
        raise RateLimitError("429")

    executor: ParallelExecutor[FakeResult] = ParallelExecutor(
        max_concurrency=3,
        stagger_delay_s=0,
        rate_limit_base_delay_s=30.0,
        rate_limit_max_delay_s=9999.0,
    )
    # 3 rate-limited tasks → exponent = min(3, 5) = 3 → 30 * 8 = 240s
    await executor.run_batch(["a", "b", "c"], always_rate_limit)

    assert len(slept) == 1
    assert slept[0] == pytest.approx(240.0)


# ---------------------------------------------------------------------------
# 10.5 Cancellation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_stops_pending_subtasks() -> None:
    """Setting cancel_event before run_batch causes all tasks to return cancelled."""
    cancel = asyncio.Event()
    cancel.set()  # Pre-cancelled

    async def runner(sid: str) -> str:
        return "should not run"

    executor: ParallelExecutor[str] = ParallelExecutor(stagger_delay_s=0, cancel_event=cancel)
    result = await executor.run_batch(["a", "b", "c"], runner)
    assert result.cancelled is True
    # All results are failures with error="Cancelled"
    assert all(not r.success for r in result.results)
    assert all(r.error == "Cancelled" for r in result.results)


@pytest.mark.asyncio
async def test_cancel_stops_between_batches() -> None:
    """Cancellation between batches stops further processing."""
    cancel = asyncio.Event()
    completed: list[str] = []

    async def runner(sid: str) -> str:
        completed.append(sid)
        if len(completed) >= 1:
            cancel.set()  # Cancel after first task completes
        return "ok"

    # max_concurrency=1 forces strict sequential batches
    executor: ParallelExecutor[str] = ParallelExecutor(
        max_concurrency=1, stagger_delay_s=0, cancel_event=cancel,
    )
    result = await executor.run_batch(["a", "b", "c"], runner)
    assert result.cancelled is True
    # At most one task ran (cancellation fires after first)
    assert len(completed) <= 1


# ---------------------------------------------------------------------------
# 10.6 Stagger Delay
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stagger_delay_between_launches() -> None:
    """Within a batch, each subtask is delayed by index * stagger_delay_s."""
    start_times: dict[str, float] = {}

    async def runner(sid: str) -> str:
        start_times[sid] = time.monotonic()
        return "ok"

    executor: ParallelExecutor[str] = ParallelExecutor(max_concurrency=3, stagger_delay_s=0.05)
    await executor.run_batch(["a", "b", "c"], runner)
    # "b" should start at least 0.05s after "a", "c" at least 0.10s after "a"
    assert start_times["b"] - start_times["a"] >= 0.04
    assert start_times["c"] - start_times["a"] >= 0.08


@pytest.mark.asyncio
async def test_zero_stagger_no_delay() -> None:
    """With stagger_delay_s=0, all tasks in a batch start at the same time."""
    start_times: dict[str, float] = {}

    async def runner(sid: str) -> str:
        start_times[sid] = time.monotonic()
        await asyncio.sleep(0)  # Yield to allow others to start
        return "ok"

    executor: ParallelExecutor[str] = ParallelExecutor(max_concurrency=3, stagger_delay_s=0)
    await executor.run_batch(["a", "b", "c"], runner)
    # All should start within 10ms of each other
    earliest = min(start_times.values())
    latest = max(start_times.values())
    assert latest - earliest < 0.05


# ---------------------------------------------------------------------------
# 10.7 EventBus Integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_events_emitted_for_start_and_complete(tmp_path: Path) -> None:
    """SubtaskStarted and SubtaskCompleted events are emitted on success."""
    from golem.events import EventBus, FileBackend, SubtaskCompleted, SubtaskStarted

    events_file = tmp_path / "events.jsonl"
    bus = EventBus(FileBackend(events_file))

    async def runner(sid: str) -> str:
        return "ok"

    executor: ParallelExecutor[str] = ParallelExecutor(stagger_delay_s=0, event_bus=bus)
    await executor.run_batch(["t1"], runner)

    lines = events_file.read_text(encoding="utf-8").strip().splitlines()
    event_types = [json.loads(line)["type"] for line in lines]
    assert "subtask_started" in event_types
    assert "subtask_completed" in event_types


@pytest.mark.asyncio
async def test_events_emitted_for_failure(tmp_path: Path) -> None:
    """SubtaskStarted and SubtaskFailed events are emitted on failure."""
    from golem.events import EventBus, FileBackend

    events_file = tmp_path / "events.jsonl"
    bus = EventBus(FileBackend(events_file))

    async def runner(sid: str) -> str:
        raise RuntimeError("oops")

    executor: ParallelExecutor[str] = ParallelExecutor(stagger_delay_s=0, event_bus=bus)
    await executor.run_batch(["fail1"], runner)

    lines = events_file.read_text(encoding="utf-8").strip().splitlines()
    event_types = [json.loads(line)["type"] for line in lines]
    assert "subtask_started" in event_types
    assert "subtask_failed" in event_types


@pytest.mark.asyncio
async def test_events_emitted_for_start_complete_failed(tmp_path: Path) -> None:
    """SubtaskStarted, SubtaskCompleted, SubtaskFailed events all emitted in a mixed batch."""
    from golem.events import EventBus, FileBackend

    events_file = tmp_path / "events.jsonl"
    bus = EventBus(FileBackend(events_file))

    async def runner(sid: str) -> str:
        if sid == "fail":
            raise RuntimeError("oops")
        return "ok"

    executor: ParallelExecutor[str] = ParallelExecutor(stagger_delay_s=0, event_bus=bus)
    await executor.run_batch(["ok1", "fail"], runner)

    lines = events_file.read_text(encoding="utf-8").strip().splitlines()
    event_types = {json.loads(line)["type"] for line in lines}
    assert "subtask_started" in event_types
    assert "subtask_completed" in event_types
    assert "subtask_failed" in event_types


@pytest.mark.asyncio
async def test_rate_limit_batch_event_emitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SubtaskBatchRateLimited event is emitted before backoff sleep."""
    from golem.events import EventBus, FileBackend

    events_file = tmp_path / "events.jsonl"
    bus = EventBus(FileBackend(events_file))

    # Mock sleep so the test doesn't actually wait
    async def fake_sleep(s: float, _: asyncio.Event) -> None:
        pass

    monkeypatch.setattr("golem.parallel._interruptible_sleep", fake_sleep)

    call_count = 0

    async def runner(sid: str) -> str:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RateLimitError("429")
        return "ok"

    executor: ParallelExecutor[str] = ParallelExecutor(
        max_concurrency=1, stagger_delay_s=0, event_bus=bus,
    )
    await executor.run_batch(["a", "b"], runner)

    lines = events_file.read_text(encoding="utf-8").strip().splitlines()
    event_types = {json.loads(line)["type"] for line in lines}
    assert "subtask_batch_rate_limited" in event_types


@pytest.mark.asyncio
async def test_no_events_without_event_bus() -> None:
    """Executor works normally when no EventBus is provided (no errors)."""
    async def runner(sid: str) -> str:
        return "ok"

    executor: ParallelExecutor[str] = ParallelExecutor(stagger_delay_s=0, event_bus=None)
    result = await executor.run_batch(["a", "b"], runner)
    assert result.success_count == 2


# ---------------------------------------------------------------------------
# 10.8 Concurrency Limit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrency_limit_respected() -> None:
    """Never more than max_concurrency runners active simultaneously."""
    active = 0
    max_active = 0

    async def runner(sid: str) -> str:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return "ok"

    executor: ParallelExecutor[str] = ParallelExecutor(max_concurrency=2, stagger_delay_s=0)
    await executor.run_batch(["a", "b", "c", "d", "e"], runner)
    assert max_active <= 2


@pytest.mark.asyncio
async def test_concurrency_limit_1_is_sequential() -> None:
    """max_concurrency=1 causes subtasks to run sequentially."""
    order: list[str] = []

    async def runner(sid: str) -> str:
        order.append(sid)
        return "ok"

    executor: ParallelExecutor[str] = ParallelExecutor(max_concurrency=1, stagger_delay_s=0)
    await executor.run_batch(["a", "b", "c"], runner)
    # With max_concurrency=1, order must be strictly sequential
    assert order == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_concurrency_limit_default_is_3() -> None:
    """Default max_concurrency is 3."""
    max_active = 0
    active = 0

    async def runner(sid: str) -> str:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return "ok"

    executor: ParallelExecutor[str] = ParallelExecutor(stagger_delay_s=0)
    await executor.run_batch(["a", "b", "c", "d", "e", "f"], runner)
    assert max_active <= 3


# ---------------------------------------------------------------------------
# SubtaskResult and BatchResult dataclass tests
# ---------------------------------------------------------------------------


def test_subtask_result_defaults() -> None:
    """SubtaskResult has sensible defaults for optional fields."""
    r: SubtaskResult[str] = SubtaskResult(subtask_id="t1", success=True, result="ok")
    assert r.error == ""
    assert r.rate_limited is False


def test_batch_result_defaults() -> None:
    """BatchResult() with no arguments has all-zero counts."""
    br: BatchResult[str] = BatchResult()
    assert br.success_count == 0
    assert br.failure_count == 0
    assert br.rate_limited_count == 0
    assert br.cancelled is False
    assert br.results == []


# ---------------------------------------------------------------------------
# RateLimitError class tests
# ---------------------------------------------------------------------------


def test_rate_limit_error_is_runtime_error() -> None:
    """RateLimitError inherits from RuntimeError."""
    exc = RateLimitError("429 Too Many Requests")
    assert isinstance(exc, RuntimeError)
    assert "429" in str(exc)


def test_rate_limit_error_can_be_raised_and_caught() -> None:
    """RateLimitError can be raised and caught as RuntimeError."""
    with pytest.raises(RuntimeError, match="429"):
        raise RateLimitError("429 rate limited")


# ---------------------------------------------------------------------------
# Integration: events.py registry has the new event types
# ---------------------------------------------------------------------------


def test_new_event_types_registered() -> None:
    """SubtaskStarted, SubtaskCompleted, SubtaskFailed, SubtaskBatchRateLimited are in EVENT_TYPES."""
    from golem.events import EVENT_TYPES

    assert "subtask_started" in EVENT_TYPES
    assert "subtask_completed" in EVENT_TYPES
    assert "subtask_failed" in EVENT_TYPES
    assert "subtask_batch_rate_limited" in EVENT_TYPES


def test_event_registry_count() -> None:
    """EVENT_TYPES now contains 45 event types (29 original + 16 orchestrator/edict events)."""
    from golem.events import EVENT_TYPES

    assert len(EVENT_TYPES) == 45


def test_new_event_types_roundtrip() -> None:
    """New event types can be serialized and deserialized."""
    from golem.events import (
        GolemEvent,
        SubtaskBatchRateLimited,
        SubtaskCompleted,
        SubtaskFailed,
        SubtaskStarted,
    )

    started = SubtaskStarted(subtask_id="T-001")
    d = started.to_dict()
    assert d["type"] == "subtask_started"
    assert d["subtask_id"] == "T-001"
    restored = GolemEvent.from_dict(d)
    assert isinstance(restored, SubtaskStarted)
    assert restored.subtask_id == "T-001"

    completed = SubtaskCompleted(subtask_id="T-002", duration_s=1.5, cost_usd=0.01)
    d = completed.to_dict()
    assert d["type"] == "subtask_completed"
    restored2 = GolemEvent.from_dict(d)
    assert isinstance(restored2, SubtaskCompleted)

    failed = SubtaskFailed(subtask_id="T-003", error="exploded", rate_limited=True)
    d = failed.to_dict()
    assert d["type"] == "subtask_failed"
    restored3 = GolemEvent.from_dict(d)
    assert isinstance(restored3, SubtaskFailed)
    assert restored3.rate_limited is True

    rl_event = SubtaskBatchRateLimited(backoff_s=60.0, rate_limited_count=1)
    d = rl_event.to_dict()
    assert d["type"] == "subtask_batch_rate_limited"
    restored4 = GolemEvent.from_dict(d)
    assert isinstance(restored4, SubtaskBatchRateLimited)
    assert restored4.backoff_s == 60.0


# ---------------------------------------------------------------------------
# Integration: config.py has the new keys and validation
# ---------------------------------------------------------------------------


def test_config_has_new_parallel_keys() -> None:
    """GolemConfig has the 4 new parallel executor config keys with correct defaults."""
    from golem.config import GolemConfig

    cfg = GolemConfig()
    assert cfg.max_concurrency == 3
    assert cfg.stagger_delay_s == 1.0
    assert cfg.rate_limit_base_delay_s == 30.0
    assert cfg.subagent_max_steps == 100


def test_config_validation_max_concurrency() -> None:
    """max_concurrency < 1 produces a validation warning."""
    from golem.config import GolemConfig

    cfg = GolemConfig(max_concurrency=0)
    warnings = cfg.validate()
    assert any("max_concurrency" in w for w in warnings)


def test_config_validation_stagger_delay() -> None:
    """stagger_delay_s < 0 produces a validation warning."""
    from golem.config import GolemConfig

    cfg = GolemConfig(stagger_delay_s=-1.0)
    warnings = cfg.validate()
    assert any("stagger_delay_s" in w for w in warnings)


def test_config_validation_rate_limit_base_delay() -> None:
    """rate_limit_base_delay_s < 1 produces a validation warning."""
    from golem.config import GolemConfig

    cfg = GolemConfig(rate_limit_base_delay_s=0.5)
    warnings = cfg.validate()
    assert any("rate_limit_base_delay_s" in w for w in warnings)


def test_config_validation_subagent_max_steps() -> None:
    """subagent_max_steps < 1 produces a validation warning."""
    from golem.config import GolemConfig

    cfg = GolemConfig(subagent_max_steps=0)
    warnings = cfg.validate()
    assert any("subagent_max_steps" in w for w in warnings)


def test_config_valid_defaults_produce_no_warnings() -> None:
    """Default GolemConfig produces no parallel-executor-related warnings."""
    from golem.config import GolemConfig

    cfg = GolemConfig()
    warnings = cfg.validate()
    parallel_warnings = [
        w for w in warnings
        if any(key in w for key in ["max_concurrency", "stagger_delay_s", "rate_limit_base_delay_s", "subagent_max_steps"])
    ]
    assert parallel_warnings == []
