"""Tests for the orchestrator module: DAG building, wave assignment, cycle detection,
wave execution, and event emission."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from golem.config import GolemConfig
from golem.orchestrator import (
    CycleError,
    OrchestratorResult,
    TicketExecutionResult,
    TicketOutcome,
    WaveExecutor,
    WaveResult,
    _create_batches,
    _is_rate_limit_error,
    assign_waves,
    build_dag,
)
from golem.tickets import Ticket, TicketStore


# ---------------------------------------------------------------------------
# DAG construction tests
# ---------------------------------------------------------------------------


def test_build_dag_no_deps(make_ticket):
    """Tickets with no deps all go in wave 0."""
    t1 = make_ticket(id="TICKET-001")
    t2 = make_ticket(id="TICKET-002")
    nodes = build_dag([t1, t2])
    waves = assign_waves(nodes)
    assert set(waves[0]) == {"TICKET-001", "TICKET-002"}
    assert len(waves) == 1


def test_build_dag_linear_chain(make_ticket):
    """A -> B -> C produces waves 0, 1, 2."""
    t1 = make_ticket(id="TICKET-001", depends_on=[])
    t2 = make_ticket(id="TICKET-002", depends_on=["TICKET-001"])
    t3 = make_ticket(id="TICKET-003", depends_on=["TICKET-002"])
    nodes = build_dag([t1, t2, t3])
    waves = assign_waves(nodes)
    assert waves[0] == ["TICKET-001"]
    assert waves[1] == ["TICKET-002"]
    assert waves[2] == ["TICKET-003"]


def test_build_dag_diamond(make_ticket):
    """Diamond: 001 -> (002, 003) -> 004. Waves: [001], [002, 003], [004]."""
    t1 = make_ticket(id="TICKET-001", depends_on=[])
    t2 = make_ticket(id="TICKET-002", depends_on=["TICKET-001"])
    t3 = make_ticket(id="TICKET-003", depends_on=["TICKET-001"])
    t4 = make_ticket(id="TICKET-004", depends_on=["TICKET-002", "TICKET-003"])
    nodes = build_dag([t1, t2, t3, t4])
    waves = assign_waves(nodes)
    assert waves[0] == ["TICKET-001"]
    assert set(waves[1]) == {"TICKET-002", "TICKET-003"}
    assert waves[2] == ["TICKET-004"]


def test_build_dag_cycle_detection(make_ticket):
    """Circular dependency raises CycleError with member list."""
    t1 = make_ticket(id="TICKET-001", depends_on=["TICKET-002"])
    t2 = make_ticket(id="TICKET-002", depends_on=["TICKET-001"])
    with pytest.raises(CycleError) as exc_info:
        build_dag([t1, t2])
    assert "TICKET-001" in str(exc_info.value)
    assert "TICKET-002" in str(exc_info.value)


def test_build_dag_three_way_cycle(make_ticket):
    """Three-node cycle raises CycleError listing all members."""
    t1 = make_ticket(id="TICKET-001", depends_on=["TICKET-003"])
    t2 = make_ticket(id="TICKET-002", depends_on=["TICKET-001"])
    t3 = make_ticket(id="TICKET-003", depends_on=["TICKET-002"])
    with pytest.raises(CycleError) as exc_info:
        build_dag([t1, t2, t3])
    msg = str(exc_info.value)
    assert "TICKET-001" in msg
    assert "TICKET-002" in msg
    assert "TICKET-003" in msg


def test_build_dag_unknown_dep_ignored(make_ticket):
    """depends_on IDs not in the ticket set are silently filtered."""
    t1 = make_ticket(id="TICKET-001", depends_on=["TICKET-999"])
    nodes = build_dag([t1])
    assert nodes["TICKET-001"].depends_on == []
    waves = assign_waves(nodes)
    assert waves[0] == ["TICKET-001"]


def test_build_dag_reverse_edges(make_ticket):
    """Dependents are correctly populated in reverse edge map."""
    t1 = make_ticket(id="TICKET-001", depends_on=[])
    t2 = make_ticket(id="TICKET-002", depends_on=["TICKET-001"])
    t3 = make_ticket(id="TICKET-003", depends_on=["TICKET-001"])
    nodes = build_dag([t1, t2, t3])
    assert set(nodes["TICKET-001"].dependents) == {"TICKET-002", "TICKET-003"}
    assert nodes["TICKET-002"].dependents == []
    assert nodes["TICKET-003"].dependents == []


def test_assign_waves_parallel_at_same_level(make_ticket):
    """Multiple tickets at same dependency depth land in the same wave."""
    t1 = make_ticket(id="TICKET-001", depends_on=[])
    t2 = make_ticket(id="TICKET-002", depends_on=[])
    t3 = make_ticket(id="TICKET-003", depends_on=[])
    nodes = build_dag([t1, t2, t3])
    waves = assign_waves(nodes)
    assert set(waves[0]) == {"TICKET-001", "TICKET-002", "TICKET-003"}
    assert len(waves) == 1


def test_assign_waves_longest_path(make_ticket):
    """Ticket with two dependency paths takes the longer one (wave = deepest dep + 1)."""
    # 001 (wave 0) -> 003 (wave 1)
    # 001 (wave 0) -> 002 (wave 1) -> 003... but 003 depends on BOTH 001 and 002
    # So 003 must be in wave 2
    t1 = make_ticket(id="TICKET-001", depends_on=[])
    t2 = make_ticket(id="TICKET-002", depends_on=["TICKET-001"])
    t3 = make_ticket(id="TICKET-003", depends_on=["TICKET-001", "TICKET-002"])
    nodes = build_dag([t1, t2, t3])
    waves = assign_waves(nodes)
    assert waves[0] == ["TICKET-001"]
    assert waves[1] == ["TICKET-002"]
    assert waves[2] == ["TICKET-003"]


def test_assign_waves_sorted_within_wave(make_ticket):
    """Ticket IDs within a wave are sorted alphabetically for determinism."""
    t1 = make_ticket(id="TICKET-003", depends_on=[])
    t2 = make_ticket(id="TICKET-001", depends_on=[])
    t3 = make_ticket(id="TICKET-002", depends_on=[])
    nodes = build_dag([t1, t2, t3])
    waves = assign_waves(nodes)
    assert waves[0] == ["TICKET-001", "TICKET-002", "TICKET-003"]


# ---------------------------------------------------------------------------
# depends_on field persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ticket_depends_on_round_trip(tmp_path):
    """Ticket with depends_on serializes and deserializes correctly."""
    store = TicketStore(tmp_path / "tickets")
    t = Ticket(
        id="",
        type="task",
        title="Test",
        status="pending",
        priority="medium",
        created_by="planner",
        assigned_to="writer",
        context=__import__("golem.tickets", fromlist=["TicketContext"]).TicketContext(),
        depends_on=["TICKET-001", "TICKET-002"],
    )
    ticket_id = await store.create(t)
    loaded = await store.read(ticket_id)
    assert loaded.depends_on == ["TICKET-001", "TICKET-002"]


@pytest.mark.asyncio
async def test_ticket_depends_on_defaults_empty(tmp_path):
    """Ticket without depends_on defaults to empty list."""
    from golem.tickets import TicketContext
    store = TicketStore(tmp_path / "tickets")
    t = Ticket(
        id="",
        type="task",
        title="Test",
        status="pending",
        priority="medium",
        created_by="planner",
        assigned_to="writer",
        context=TicketContext(),
    )
    ticket_id = await store.create(t)
    loaded = await store.read(ticket_id)
    assert loaded.depends_on == []


# ---------------------------------------------------------------------------
# Module-level helper tests
# ---------------------------------------------------------------------------


def test_rate_limit_detection():
    """Rate limit detection covers 429, 'rate limit', and 'too many requests'."""
    assert _is_rate_limit_error("HTTP 429 Too Many Requests")
    assert _is_rate_limit_error("rate limit exceeded")
    assert _is_rate_limit_error("too many requests from this IP")
    assert not _is_rate_limit_error("connection refused")
    assert not _is_rate_limit_error("auth failure")
    assert not _is_rate_limit_error("timeout")


def test_create_batches_even(make_ticket):
    """_create_batches splits evenly."""
    tickets = [make_ticket() for _ in range(6)]
    batches = _create_batches(tickets, 3)
    assert len(batches) == 2
    assert len(batches[0]) == 3
    assert len(batches[1]) == 3


def test_create_batches_uneven(make_ticket):
    """_create_batches handles uneven splits with a smaller last batch."""
    tickets = [make_ticket() for _ in range(7)]
    batches = _create_batches(tickets, 3)
    assert len(batches) == 3
    assert len(batches[0]) == 3
    assert len(batches[1]) == 3
    assert len(batches[2]) == 1


def test_create_batches_single(make_ticket):
    """_create_batches with batch_size=1 gives one ticket per batch."""
    tickets = [make_ticket() for _ in range(3)]
    batches = _create_batches(tickets, 1)
    assert len(batches) == 3


def test_create_batches_empty():
    """_create_batches on empty list returns empty list."""
    assert _create_batches([], 3) == []


# ---------------------------------------------------------------------------
# WaveResult properties
# ---------------------------------------------------------------------------


def test_wave_result_passed_count():
    results = [
        TicketExecutionResult(ticket_id="T1", outcome=TicketOutcome.PASSED),
        TicketExecutionResult(ticket_id="T2", outcome=TicketOutcome.FAILED),
        TicketExecutionResult(ticket_id="T3", outcome=TicketOutcome.SKIPPED),
    ]
    wave = WaveResult(wave_number=0, ticket_results=results)
    assert wave.passed_count == 1
    assert wave.failed_count == 1
    assert not wave.all_failed


def test_wave_result_all_failed():
    results = [
        TicketExecutionResult(ticket_id="T1", outcome=TicketOutcome.FAILED),
        TicketExecutionResult(ticket_id="T2", outcome=TicketOutcome.FAILED),
    ]
    wave = WaveResult(wave_number=0, ticket_results=results)
    assert wave.all_failed


def test_wave_result_empty_not_all_failed():
    """Empty wave does not count as all_failed."""
    wave = WaveResult(wave_number=0, ticket_results=[])
    assert not wave.all_failed


# ---------------------------------------------------------------------------
# OrchestratorResult fields
# ---------------------------------------------------------------------------


def test_orchestrator_result_fields():
    result = OrchestratorResult(
        waves_completed=3,
        waves_total=3,
        tickets_passed=5,
        tickets_failed=1,
        tickets_skipped=0,
        total_cost_usd=0.42,
        total_duration_s=120.5,
        integration_branch="golem/wave-2-integration",
    )
    assert result.waves_completed == 3
    assert result.tickets_passed == 5
    assert not result.aborted


# ---------------------------------------------------------------------------
# WaveExecutor tests with mocked writers
# ---------------------------------------------------------------------------


def _make_git_repo(project_root: Path) -> None:
    """Initialize a bare git repo with initial commit for worktree tests."""
    subprocess.run(["git", "init", "-b", "main"], cwd=project_root, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"], cwd=project_root, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=project_root, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=project_root, check=True, capture_output=True,
    )


@pytest.mark.asyncio
async def test_wave_executor_no_pending_tickets(tmp_path):
    """WaveExecutor returns empty result when there are no pending tickets."""
    golem_dir = tmp_path / ".golem"
    golem_dir.mkdir()
    project_root = tmp_path / "project"
    project_root.mkdir()

    config = GolemConfig(max_parallel_per_wave=2)
    executor = WaveExecutor(golem_dir=golem_dir, project_root=project_root, config=config)
    result = await executor.run()
    assert result.waves_completed == 0
    assert result.tickets_passed == 0
    assert result.tickets_failed == 0


@pytest.mark.asyncio
async def test_wave_executor_all_pass(tmp_path, make_ticket, monkeypatch):
    """All tickets pass QA -> all merged to integration branch."""
    golem_dir = tmp_path / ".golem"
    golem_dir.mkdir()
    project_root = tmp_path / "project"
    project_root.mkdir()

    config = GolemConfig(max_parallel_per_wave=2)

    # Create tickets (no deps -> all wave 0)
    store = TicketStore(golem_dir / "tickets")
    t1 = make_ticket(id="TICKET-001")
    t2 = make_ticket(id="TICKET-002")
    await store.create(t1)
    await store.create(t2)

    from golem.writer import JuniorDevResult

    async def mock_writer(ticket, worktree_path, config, golem_dir, event_bus=None, **kw):
        return JuniorDevResult(result_text="done", cost_usd=0.01)

    async def mock_qa(self_ref, ticket, wt_path):
        return True

    monkeypatch.setattr("golem.orchestrator.spawn_junior_dev", mock_writer)
    monkeypatch.setattr("golem.orchestrator.create_worktree", lambda *a, **kw: None)
    monkeypatch.setattr("golem.orchestrator.delete_worktree", lambda *a, **kw: None)
    monkeypatch.setattr("golem.orchestrator.commit_task", lambda *a, **kw: True)
    monkeypatch.setattr("golem.orchestrator.merge_group_branches", lambda *a, **kw: (True, ""))

    executor = WaveExecutor(golem_dir=golem_dir, project_root=project_root, config=config)
    monkeypatch.setattr(executor, "_run_qa", mock_qa.__get__(executor, WaveExecutor))

    result = await executor.run()
    assert result.tickets_passed == 2
    assert result.tickets_failed == 0
    assert result.waves_completed == 1


@pytest.mark.asyncio
async def test_wave_executor_dependency_skip(tmp_path, make_ticket, monkeypatch):
    """Ticket in wave 1 is skipped when its wave-0 dependency fails."""
    golem_dir = tmp_path / ".golem"
    golem_dir.mkdir()
    project_root = tmp_path / "project"
    project_root.mkdir()

    config = GolemConfig(max_parallel_per_wave=2)

    store = TicketStore(golem_dir / "tickets")
    t1 = make_ticket(id="TICKET-001", depends_on=[])
    t2 = make_ticket(id="TICKET-002", depends_on=["TICKET-001"])
    await store.create(t1)
    await store.create(t2)

    async def failing_writer(ticket, worktree_path, config, golem_dir, event_bus=None, **kw):
        raise RuntimeError("writer error")

    async def mock_qa(self_ref, ticket, wt_path):
        return False

    monkeypatch.setattr("golem.orchestrator.spawn_junior_dev", failing_writer)
    monkeypatch.setattr("golem.orchestrator.create_worktree", lambda *a, **kw: None)
    monkeypatch.setattr("golem.orchestrator.delete_worktree", lambda *a, **kw: None)
    monkeypatch.setattr("golem.orchestrator.commit_task", lambda *a, **kw: True)
    monkeypatch.setattr("golem.orchestrator.merge_group_branches", lambda *a, **kw: (True, ""))

    executor = WaveExecutor(golem_dir=golem_dir, project_root=project_root, config=config)
    monkeypatch.setattr(executor, "_run_qa", mock_qa.__get__(executor, WaveExecutor))

    result = await executor.run()
    assert result.tickets_failed == 1    # TICKET-001 failed
    assert result.tickets_skipped == 1   # TICKET-002 skipped (dep failed)


@pytest.mark.asyncio
async def test_wave_executor_abort_policy(tmp_path, make_ticket, monkeypatch):
    """wave_failure_policy=abort stops execution after a fully-failed wave."""
    golem_dir = tmp_path / ".golem"
    golem_dir.mkdir()
    project_root = tmp_path / "project"
    project_root.mkdir()

    config = GolemConfig(max_parallel_per_wave=2, wave_failure_policy="abort")

    store = TicketStore(golem_dir / "tickets")
    # Wave 0: TICKET-001 (no deps) - will fail
    # Wave 1: TICKET-002 (depends on TICKET-001) - should not run
    t1 = make_ticket(id="TICKET-001", depends_on=[])
    t2 = make_ticket(id="TICKET-002", depends_on=["TICKET-001"])
    await store.create(t1)
    await store.create(t2)

    async def failing_writer(ticket, worktree_path, config, golem_dir, event_bus=None, **kw):
        raise RuntimeError("writer error")

    async def mock_qa(self_ref, ticket, wt_path):
        return False

    monkeypatch.setattr("golem.orchestrator.spawn_junior_dev", failing_writer)
    monkeypatch.setattr("golem.orchestrator.create_worktree", lambda *a, **kw: None)
    monkeypatch.setattr("golem.orchestrator.delete_worktree", lambda *a, **kw: None)
    monkeypatch.setattr("golem.orchestrator.commit_task", lambda *a, **kw: True)
    monkeypatch.setattr("golem.orchestrator.merge_group_branches", lambda *a, **kw: (True, ""))

    executor = WaveExecutor(golem_dir=golem_dir, project_root=project_root, config=config)
    monkeypatch.setattr(executor, "_run_qa", mock_qa.__get__(executor, WaveExecutor))

    result = await executor.run()
    # Wave 0 should complete (1 failed), abort fires, wave 1 skipped
    assert result.aborted
    assert result.waves_completed == 1
    # TICKET-002 was never dispatched (aborted before wave 1)
    assert result.tickets_skipped == 0  # didn't get to enqueue wave-1 tickets


@pytest.mark.asyncio
async def test_wave_executor_continue_policy(tmp_path, make_ticket, monkeypatch):
    """wave_failure_policy=continue runs remaining independent tickets despite a wave failure."""
    golem_dir = tmp_path / ".golem"
    golem_dir.mkdir()
    project_root = tmp_path / "project"
    project_root.mkdir()

    config = GolemConfig(max_parallel_per_wave=3, wave_failure_policy="continue")

    store = TicketStore(golem_dir / "tickets")
    # Wave 0: TICKET-001 (no deps) - will fail; TICKET-002 (no deps) - will pass
    # Wave 1: TICKET-003 (depends on TICKET-001) - should be skipped
    t1 = make_ticket(id="TICKET-001", depends_on=[])
    t2 = make_ticket(id="TICKET-002", depends_on=[])
    t3 = make_ticket(id="TICKET-003", depends_on=["TICKET-001"])
    await store.create(t1)
    await store.create(t2)
    await store.create(t3)

    from golem.writer import JuniorDevResult

    async def selective_writer(ticket, worktree_path, config, golem_dir, event_bus=None, **kw):
        if ticket.id == "TICKET-001":
            raise RuntimeError("forced fail")
        return JuniorDevResult(result_text="done", cost_usd=0.01)

    async def mock_qa(self_ref, ticket, wt_path):
        return ticket.id != "TICKET-001"

    monkeypatch.setattr("golem.orchestrator.spawn_junior_dev", selective_writer)
    monkeypatch.setattr("golem.orchestrator.create_worktree", lambda *a, **kw: None)
    monkeypatch.setattr("golem.orchestrator.delete_worktree", lambda *a, **kw: None)
    monkeypatch.setattr("golem.orchestrator.commit_task", lambda *a, **kw: True)
    monkeypatch.setattr("golem.orchestrator.merge_group_branches", lambda *a, **kw: (True, ""))

    executor = WaveExecutor(golem_dir=golem_dir, project_root=project_root, config=config)
    monkeypatch.setattr(executor, "_run_qa", mock_qa.__get__(executor, WaveExecutor))

    result = await executor.run()
    assert not result.aborted
    assert result.tickets_passed == 1    # TICKET-002
    assert result.tickets_failed == 1    # TICKET-001
    assert result.tickets_skipped == 1   # TICKET-003 (dep on failed TICKET-001)


@pytest.mark.asyncio
async def test_wave_executor_cycle_raises(tmp_path, monkeypatch):
    """CycleError from build_dag propagates out of WaveExecutor.run()."""
    from golem.tickets import TicketContext

    golem_dir = tmp_path / ".golem"
    golem_dir.mkdir()
    project_root = tmp_path / "project"
    project_root.mkdir()

    # Write ticket files with a cycle directly
    tickets_dir = golem_dir / "tickets"
    tickets_dir.mkdir()

    import json
    for tid, dep in [("TICKET-001", "TICKET-002"), ("TICKET-002", "TICKET-001")]:
        data = {
            "id": tid,
            "type": "task",
            "title": f"Ticket {tid}",
            "status": "pending",
            "priority": "medium",
            "created_by": "planner",
            "assigned_to": "writer",
            "context": {
                "plan_file": "",
                "files": {},
                "references": [],
                "blueprint": "",
                "acceptance": [],
                "qa_checks": [],
                "parallelism_hints": [],
            },
            "history": [],
            "depends_on": [dep],
        }
        (tickets_dir / f"{tid}.json").write_text(json.dumps(data, indent=2), encoding="utf-8")

    config = GolemConfig()
    executor = WaveExecutor(golem_dir=golem_dir, project_root=project_root, config=config)

    with pytest.raises(CycleError):
        await executor.run()


@pytest.mark.asyncio
async def test_wave_executor_abort_method(tmp_path):
    """abort() sets the internal abort event."""
    golem_dir = tmp_path / ".golem"
    golem_dir.mkdir()
    project_root = tmp_path / "project"
    project_root.mkdir()

    config = GolemConfig()
    executor = WaveExecutor(golem_dir=golem_dir, project_root=project_root, config=config)
    assert not executor._abort.is_set()
    await executor.abort("test reason")
    assert executor._abort.is_set()


@pytest.mark.asyncio
async def test_wave_executor_ticket_ids_filter(tmp_path, make_ticket, monkeypatch):
    """ticket_ids parameter restricts execution to a subset of tickets."""
    golem_dir = tmp_path / ".golem"
    golem_dir.mkdir()
    project_root = tmp_path / "project"
    project_root.mkdir()

    config = GolemConfig(max_parallel_per_wave=2)

    store = TicketStore(golem_dir / "tickets")
    t1 = make_ticket(id="TICKET-001")
    t2 = make_ticket(id="TICKET-002")
    t3 = make_ticket(id="TICKET-003")
    await store.create(t1)
    await store.create(t2)
    await store.create(t3)

    from golem.writer import JuniorDevResult

    dispatched: list[str] = []

    async def tracking_writer(ticket, worktree_path, config, golem_dir, event_bus=None, **kw):
        dispatched.append(ticket.id)
        return JuniorDevResult(result_text="done", cost_usd=0.0)

    async def mock_qa(self_ref, ticket, wt_path):
        return True

    monkeypatch.setattr("golem.orchestrator.spawn_junior_dev", tracking_writer)
    monkeypatch.setattr("golem.orchestrator.create_worktree", lambda *a, **kw: None)
    monkeypatch.setattr("golem.orchestrator.delete_worktree", lambda *a, **kw: None)
    monkeypatch.setattr("golem.orchestrator.commit_task", lambda *a, **kw: True)
    monkeypatch.setattr("golem.orchestrator.merge_group_branches", lambda *a, **kw: (True, ""))

    executor = WaveExecutor(golem_dir=golem_dir, project_root=project_root, config=config)
    monkeypatch.setattr(executor, "_run_qa", mock_qa.__get__(executor, WaveExecutor))

    # Only run TICKET-001 and TICKET-003
    result = await executor.run(ticket_ids=["TICKET-001", "TICKET-003"])
    assert result.tickets_passed == 2
    assert set(dispatched) == {"TICKET-001", "TICKET-003"}


# ---------------------------------------------------------------------------
# EventBus integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_events_emitted_on_wave(tmp_path, make_ticket, monkeypatch):
    """WaveStarted and WaveCompleted events are emitted with correct wave numbers."""
    from golem.events import EventBus, QueueBackend, WaveCompleted, WaveStarted

    golem_dir = tmp_path / ".golem"
    golem_dir.mkdir()
    project_root = tmp_path / "project"
    project_root.mkdir()

    config = GolemConfig(max_parallel_per_wave=2)

    store = TicketStore(golem_dir / "tickets")
    t1 = make_ticket(id="TICKET-001")
    await store.create(t1)

    from golem.writer import JuniorDevResult

    async def mock_writer(ticket, worktree_path, config, golem_dir, event_bus=None, **kw):
        return JuniorDevResult(result_text="done", cost_usd=0.0)

    async def mock_qa(self_ref, ticket, wt_path):
        return True

    monkeypatch.setattr("golem.orchestrator.spawn_junior_dev", mock_writer)
    monkeypatch.setattr("golem.orchestrator.create_worktree", lambda *a, **kw: None)
    monkeypatch.setattr("golem.orchestrator.delete_worktree", lambda *a, **kw: None)
    monkeypatch.setattr("golem.orchestrator.commit_task", lambda *a, **kw: True)
    monkeypatch.setattr("golem.orchestrator.merge_group_branches", lambda *a, **kw: (True, ""))

    queue: asyncio.Queue = asyncio.Queue()
    event_bus = EventBus(QueueBackend(queue))

    executor = WaveExecutor(golem_dir=golem_dir, project_root=project_root, config=config, event_bus=event_bus)
    monkeypatch.setattr(executor, "_run_qa", mock_qa.__get__(executor, WaveExecutor))

    await executor.run()

    events = []
    while not queue.empty():
        events.append(queue.get_nowait())

    event_types = {type(e).__name__ for e in events}
    assert "OrchestratorStarted" in event_types
    assert "WaveStarted" in event_types
    assert "WaveCompleted" in event_types
    assert "OrchestratorComplete" in event_types

    # Verify wave number is correct
    wave_started_events = [e for e in events if isinstance(e, WaveStarted)]
    assert any(e.wave_number == 0 for e in wave_started_events)

    wave_completed_events = [e for e in events if isinstance(e, WaveCompleted)]
    assert any(e.wave_number == 0 for e in wave_completed_events)


@pytest.mark.asyncio
async def test_orchestrator_started_event_fields(tmp_path, make_ticket, monkeypatch):
    """OrchestratorStarted has correct wave_count and ticket_count."""
    from golem.events import EventBus, OrchestratorStarted, QueueBackend

    golem_dir = tmp_path / ".golem"
    golem_dir.mkdir()
    project_root = tmp_path / "project"
    project_root.mkdir()

    config = GolemConfig(max_parallel_per_wave=2)

    store = TicketStore(golem_dir / "tickets")
    t1 = make_ticket(id="TICKET-001", depends_on=[])
    t2 = make_ticket(id="TICKET-002", depends_on=["TICKET-001"])
    await store.create(t1)
    await store.create(t2)

    from golem.writer import JuniorDevResult

    async def mock_writer(ticket, worktree_path, config, golem_dir, event_bus=None, **kw):
        return JuniorDevResult(result_text="done", cost_usd=0.0)

    async def mock_qa(self_ref, ticket, wt_path):
        return True

    monkeypatch.setattr("golem.orchestrator.spawn_junior_dev", mock_writer)
    monkeypatch.setattr("golem.orchestrator.create_worktree", lambda *a, **kw: None)
    monkeypatch.setattr("golem.orchestrator.delete_worktree", lambda *a, **kw: None)
    monkeypatch.setattr("golem.orchestrator.commit_task", lambda *a, **kw: True)
    monkeypatch.setattr("golem.orchestrator.merge_group_branches", lambda *a, **kw: (True, ""))

    queue: asyncio.Queue = asyncio.Queue()
    event_bus = EventBus(QueueBackend(queue))

    executor = WaveExecutor(golem_dir=golem_dir, project_root=project_root, config=config, event_bus=event_bus)
    monkeypatch.setattr(executor, "_run_qa", mock_qa.__get__(executor, WaveExecutor))

    await executor.run()

    events = []
    while not queue.empty():
        events.append(queue.get_nowait())

    started = next((e for e in events if isinstance(e, OrchestratorStarted)), None)
    assert started is not None
    assert started.wave_count == 2
    assert started.ticket_count == 2


# ---------------------------------------------------------------------------
# Config validation tests
# ---------------------------------------------------------------------------


def test_config_max_parallel_per_wave_validation():
    config = GolemConfig(max_parallel_per_wave=0)
    warnings = config.validate()
    assert any("max_parallel_per_wave" in w for w in warnings)


def test_config_wave_failure_policy_validation():
    config = GolemConfig(wave_failure_policy="invalid")
    warnings = config.validate()
    assert any("wave_failure_policy" in w for w in warnings)


def test_config_merge_strategy_validation():
    config = GolemConfig(merge_strategy="invalid")
    warnings = config.validate()
    assert any("merge_strategy" in w for w in warnings)


def test_config_max_rework_attempts_validation():
    config = GolemConfig(max_rework_attempts=-1)
    warnings = config.validate()
    assert any("max_rework_attempts" in w for w in warnings)


def test_config_valid_defaults():
    config = GolemConfig()
    warnings = config.validate()
    # No orchestrator-specific warnings on defaults
    orchestrator_warnings = [w for w in warnings if any(
        key in w for key in ("max_parallel_per_wave", "wave_failure_policy", "merge_strategy", "max_rework_attempts")
    )]
    assert orchestrator_warnings == []


# ---------------------------------------------------------------------------
# New event types round-trip
# ---------------------------------------------------------------------------


def test_orchestrator_events_round_trip():
    """All new event types serialize/deserialize correctly."""
    from golem.events import (
        GolemEvent,
        MergeCompleted,
        MergeConflictPredicted,
        MergeStarted,
        OrchestratorAborted,
        OrchestratorComplete,
        OrchestratorStarted,
        RateLimitBackoff,
        TicketQueued,
        WaveCompleted,
        WaveFailed,
        WaveSkipped,
        WaveStarted,
    )

    events: list[GolemEvent] = [
        OrchestratorStarted(wave_count=3, ticket_count=5, wave_sizes={"0": 2, "1": 3}),
        OrchestratorComplete(waves_completed=3, tickets_passed=4, tickets_failed=1),
        OrchestratorAborted(reason="test abort"),
        WaveStarted(wave_number=0, ticket_ids=["T1", "T2"], base_branch="main"),
        WaveCompleted(wave_number=0, passed=2, failed=0, merge_success=True),
        WaveFailed(wave_number=1, reason="all failed"),
        WaveSkipped(wave_number=2, reason="all_dependencies_failed"),
        TicketQueued(ticket_id="T1", worktree_path="/tmp/wt"),
        MergeStarted(wave_number=0, source_branches=["b1"], target_branch="integration"),
        MergeCompleted(wave_number=0, source_branches=["b1"], target_branch="integration", success=True),
        MergeConflictPredicted(filename="src/app.py", branch_a="b1", branch_b="b2"),
        RateLimitBackoff(delay_s=60.0, rate_limited_count=2),
    ]

    for event in events:
        d = event.to_dict()
        restored = GolemEvent.from_dict(d)
        assert type(restored) is type(event)
        assert restored.to_dict() == d


# ---------------------------------------------------------------------------
# Worktree info helpers
# ---------------------------------------------------------------------------


def test_worktree_info_with_session_id(tmp_path):
    """_worktree_info produces session-scoped branch names."""
    config = GolemConfig(branch_prefix="golem", session_id="sess-abc")
    executor = WaveExecutor(
        golem_dir=tmp_path / ".golem",
        project_root=tmp_path,
        config=config,
    )
    branch, path = executor._worktree_info("TICKET-001", 0)
    assert branch == "golem/sess-abc/ticket-001"
    assert "ticket-001" in str(path)


def test_worktree_info_without_session_id(tmp_path):
    """_worktree_info without session_id uses bare branch_prefix."""
    config = GolemConfig(branch_prefix="golem", session_id="")
    executor = WaveExecutor(
        golem_dir=tmp_path / ".golem",
        project_root=tmp_path,
        config=config,
    )
    branch, path = executor._worktree_info("TICKET-001", 0)
    assert branch == "golem/ticket-001"


def test_integration_branch_name_with_session(tmp_path):
    """_integration_branch_name produces session-scoped branch."""
    config = GolemConfig(branch_prefix="golem", session_id="sess-abc")
    executor = WaveExecutor(
        golem_dir=tmp_path / ".golem",
        project_root=tmp_path,
        config=config,
    )
    name = executor._integration_branch_name(2)
    assert name == "golem/sess-abc/wave-2-integration"


def test_integration_branch_name_without_session(tmp_path):
    """_integration_branch_name without session_id is bare."""
    config = GolemConfig(branch_prefix="golem", session_id="")
    executor = WaveExecutor(
        golem_dir=tmp_path / ".golem",
        project_root=tmp_path,
        config=config,
    )
    name = executor._integration_branch_name(0)
    assert name == "golem/wave-0-integration"


# ---------------------------------------------------------------------------
# Progress logger additions
# ---------------------------------------------------------------------------


def test_progress_log_wave_start(tmp_path):
    from golem.progress import ProgressLogger

    logger = ProgressLogger(tmp_path / ".golem")
    logger.log_wave_start(0, 3, ["TICKET-001", "TICKET-002"])
    content = (tmp_path / ".golem" / "progress.log").read_text(encoding="utf-8")
    assert "[WAVE 1/3] Starting" in content
    assert "TICKET-001" in content


def test_progress_log_wave_complete(tmp_path):
    from golem.progress import ProgressLogger

    logger = ProgressLogger(tmp_path / ".golem")
    logger.log_wave_complete(1, 2, 0)
    content = (tmp_path / ".golem" / "progress.log").read_text(encoding="utf-8")
    assert "[WAVE 2] Complete" in content
    assert "2 passed" in content


def test_progress_log_wave_skipped(tmp_path):
    from golem.progress import ProgressLogger

    logger = ProgressLogger(tmp_path / ".golem")
    logger.log_wave_skipped(0, "all_dependencies_failed")
    content = (tmp_path / ".golem" / "progress.log").read_text(encoding="utf-8")
    assert "[WAVE 1] Skipped" in content


def test_progress_log_error(tmp_path):
    from golem.progress import ProgressLogger

    logger = ProgressLogger(tmp_path / ".golem")
    logger.log_error("orchestrator", "something broke")
    content = (tmp_path / ".golem" / "progress.log").read_text(encoding="utf-8")
    assert "ERROR" in content
    assert "orchestrator" in content


def test_progress_log_warning(tmp_path):
    from golem.progress import ProgressLogger

    logger = ProgressLogger(tmp_path / ".golem")
    logger.log_warning("orchestrator", "watch out")
    content = (tmp_path / ".golem" / "progress.log").read_text(encoding="utf-8")
    assert "WARNING" in content
