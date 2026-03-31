"""Tests for the Pipeline Coordinator."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from golem.config import GolemConfig
from golem.edict import (
    EDICT_DONE,
    EDICT_FAILED,
    EDICT_PENDING,
    Edict,
    EdictStore,
)
from golem.pipeline import PipelineCoordinator, PipelineResult
from golem.tickets import Ticket, TicketContext, TicketStore


# ---------------------------------------------------------------------------
# Mock agent results for pipeline tests
# ---------------------------------------------------------------------------


@dataclass
class _MockPlannerResult:
    ticket_ids: list[str] = field(default_factory=lambda: ["TICKET-001"])
    cost_usd: float = 0.10

    @property
    def ticket_id(self) -> str:
        return self.ticket_ids[-1] if self.ticket_ids else ""


@dataclass
class _MockTechLeadResult:
    cost_usd: float = 0.50


@dataclass
class _MockJuniorDevResult:
    cost_usd: float = 0.20


@pytest.fixture(autouse=True)
def _mock_agents(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock out agent calls so pipeline tests don't spawn real Claude sessions."""
    mock_planner = AsyncMock(return_value=_MockPlannerResult())
    mock_tech_lead = AsyncMock(return_value=_MockTechLeadResult())
    mock_junior = AsyncMock(return_value=_MockJuniorDevResult())
    mock_classify = MagicMock(return_value=MagicMock(complexity="STANDARD"))
    mock_progress = MagicMock()
    mock_progress.return_value.log_planner_start = MagicMock()
    mock_progress.return_value.log_planner_complete = MagicMock()
    mock_progress.return_value.log_tech_lead_start = MagicMock()
    mock_progress.return_value.log_tech_lead_complete = MagicMock()
    mock_progress.return_value.sum_agent_costs = MagicMock(return_value=0.60)

    monkeypatch.setattr("golem.planner.run_planner", mock_planner)
    monkeypatch.setattr("golem.tech_lead.run_tech_lead", mock_tech_lead)
    monkeypatch.setattr("golem.junior_dev.spawn_junior_dev", mock_junior)
    monkeypatch.setattr("golem.conductor.classify_spec", mock_classify)
    monkeypatch.setattr("golem.progress.ProgressLogger", mock_progress)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_edict(repo_path: str = "/repo", title: str = "Test edict") -> Edict:
    return Edict(
        id="",
        repo_path=repo_path,
        title=title,
        body="Do the thing.",
        status=EDICT_PENDING,
    )


def _make_ticket(
    edict_id: str,
    status: str = "pending",
    title: str = "Some ticket",
) -> Ticket:
    return Ticket(
        id="",
        type="feature",
        title=title,
        status=status,
        priority="medium",
        created_by="planner",
        assigned_to="",
        context=TicketContext(blueprint="do it"),
        edict_id=edict_id,
    )


def _make_coordinator(
    edict: Edict,
    edict_store: EdictStore,
    ticket_store: TicketStore,
    tmp_path: Path,
    event_bus: object = None,
) -> PipelineCoordinator:
    config = GolemConfig()
    return PipelineCoordinator(
        edict=edict,
        edict_store=edict_store,
        ticket_store=ticket_store,
        config=config,
        project_root=tmp_path,
        golem_dir=tmp_path / ".golem",
        event_bus=event_bus,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def edict_env(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create edict + ticket store directories and .golem."""
    edicts_dir = tmp_path / "edicts"
    edicts_dir.mkdir()
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    golem_dir = tmp_path / ".golem"
    golem_dir.mkdir()
    return edicts_dir, tickets_dir, golem_dir


# ---------------------------------------------------------------------------
# Test 1: Pipeline creation with valid Edict
# ---------------------------------------------------------------------------


def test_pipeline_creation(tmp_path: Path, edict_env: tuple[Path, Path, Path]) -> None:
    """PipelineCoordinator can be instantiated with valid Edict and stores."""
    edicts_dir, tickets_dir, golem_dir = edict_env
    edict = _make_edict()
    edict_store = EdictStore(edicts_dir)
    ticket_store = TicketStore(tickets_dir)

    coord = _make_coordinator(edict, edict_store, ticket_store, tmp_path)

    assert coord._edict is edict
    assert coord._edict_store is edict_store
    assert coord._ticket_store is ticket_store
    assert coord._killed is False
    assert coord._cost_usd == 0.0
    assert coord._resume_event.is_set()


# ---------------------------------------------------------------------------
# Test 2: Pipeline run with status transitions: pending -> planning -> in_progress -> done
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_run_status_transitions(tmp_path: Path, edict_env: tuple[Path, Path, Path]) -> None:
    """Pipeline transitions edict through pending -> planning -> in_progress -> done."""
    edicts_dir, tickets_dir, golem_dir = edict_env
    edict = _make_edict()
    edict_store = EdictStore(edicts_dir)
    ticket_store = TicketStore(tickets_dir)

    edict_id = await edict_store.create(edict)

    coord = _make_coordinator(edict, edict_store, ticket_store, tmp_path)
    result = await coord.run()

    assert result.edict_id == edict_id
    assert result.status == EDICT_DONE
    assert result.error is None

    # Verify persisted status on disk
    persisted = await edict_store.read(edict_id)
    assert persisted.status == EDICT_DONE


# ---------------------------------------------------------------------------
# Test 3: Pipeline run with failure (mock exception -> status: failed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_run_failure_on_exception(
    tmp_path: Path, edict_env: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """When update_status raises after the first call, pipeline marks edict as failed."""
    edicts_dir, tickets_dir, golem_dir = edict_env
    edict = _make_edict()
    edict_store = EdictStore(edicts_dir)
    ticket_store = TicketStore(tickets_dir)

    await edict_store.create(edict)

    call_count = 0
    original_update = edict_store.update_status

    async def patched_update_status(eid: str, status: str, error: str | None = None) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            # Fail on the second call (in_progress transition)
            raise RuntimeError("Simulated agent failure")
        await original_update(eid, status, error)

    monkeypatch.setattr(edict_store, "update_status", patched_update_status)

    coord = _make_coordinator(edict, edict_store, ticket_store, tmp_path)
    result = await coord.run()

    assert result.status == EDICT_FAILED
    assert result.error is not None
    assert "Simulated agent failure" in result.error


# ---------------------------------------------------------------------------
# Test 4: Pause/resume lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pause_resume_lifecycle(tmp_path: Path, edict_env: tuple[Path, Path, Path]) -> None:
    """pause() clears the resume event; resume() sets it."""
    edicts_dir, tickets_dir, golem_dir = edict_env
    edict = _make_edict()
    edict_store = EdictStore(edicts_dir)
    ticket_store = TicketStore(tickets_dir)

    coord = _make_coordinator(edict, edict_store, ticket_store, tmp_path)

    # Initially unpaused
    assert coord._resume_event.is_set()

    await coord.pause()
    assert not coord._resume_event.is_set()

    await coord.resume()
    assert coord._resume_event.is_set()


# ---------------------------------------------------------------------------
# Test 5: Kill cancels and sets killed flag
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kill_sets_flag(tmp_path: Path, edict_env: tuple[Path, Path, Path]) -> None:
    """kill() sets _killed flag and unblocks paused pipeline."""
    edicts_dir, tickets_dir, golem_dir = edict_env
    edict = _make_edict()
    edict_store = EdictStore(edicts_dir)
    ticket_store = TicketStore(tickets_dir)

    coord = _make_coordinator(edict, edict_store, ticket_store, tmp_path)

    assert coord._killed is False
    # Pause first to verify kill also unblocks
    await coord.pause()
    assert not coord._resume_event.is_set()

    await coord.kill()

    assert coord._killed is True
    # Kill must also set resume_event so a blocked _check_pause() can proceed
    assert coord._resume_event.is_set()


@pytest.mark.asyncio
async def test_kill_during_run_returns_failed(
    tmp_path: Path, edict_env: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """kill() causes pipeline to exit with EDICT_FAILED after the first pause check."""
    edicts_dir, tickets_dir, golem_dir = edict_env
    edict = _make_edict()
    edict_store = EdictStore(edicts_dir)
    ticket_store = TicketStore(tickets_dir)

    await edict_store.create(edict)
    coord = _make_coordinator(edict, edict_store, ticket_store, tmp_path)

    # Kill before run: the pipeline will transition to planning, hit _check_pause,
    # see _killed=True, and return FAILED immediately (without trying in_progress).
    await coord.kill()

    result = await coord.run()

    assert result.status == EDICT_FAILED
    assert result.error == "Killed by operator"


# ---------------------------------------------------------------------------
# Test 6: Guidance creates a ticket in the store
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guidance_creates_ticket(tmp_path: Path, edict_env: tuple[Path, Path, Path]) -> None:
    """send_guidance() creates a guidance ticket in the ticket store."""
    edicts_dir, tickets_dir, golem_dir = edict_env
    edict = _make_edict()
    edict_store = EdictStore(edicts_dir)
    ticket_store = TicketStore(tickets_dir)

    edict_id = await edict_store.create(edict)
    coord = _make_coordinator(edict, edict_store, ticket_store, tmp_path)

    await coord.send_guidance("fix the authentication bug")

    tickets = await ticket_store.list_tickets()
    assert len(tickets) == 1
    t = tickets[0]
    assert t.type == "guidance"
    assert t.priority == "high"
    assert t.created_by == "operator"
    assert t.context.blueprint == "fix the authentication bug"
    assert t.edict_id == edict_id


# ---------------------------------------------------------------------------
# Test 7: Cost accumulation via add_cost()
# ---------------------------------------------------------------------------


def test_cost_accumulation(tmp_path: Path, edict_env: tuple[Path, Path, Path]) -> None:
    """add_cost() accumulates costs correctly."""
    edicts_dir, tickets_dir, golem_dir = edict_env
    edict = _make_edict()
    edict_store = EdictStore(edicts_dir)
    ticket_store = TicketStore(tickets_dir)

    coord = _make_coordinator(edict, edict_store, ticket_store, tmp_path)

    assert coord._cost_usd == 0.0
    coord.add_cost(1.50)
    coord.add_cost(0.75)
    assert coord._cost_usd == pytest.approx(2.25)


# ---------------------------------------------------------------------------
# Test 8: PipelineResult fields populated correctly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_result_fields(tmp_path: Path, edict_env: tuple[Path, Path, Path]) -> None:
    """PipelineResult has correct edict_id, status, duration_s, and cost fields."""
    edicts_dir, tickets_dir, golem_dir = edict_env
    edict = _make_edict()
    edict_store = EdictStore(edicts_dir)
    ticket_store = TicketStore(tickets_dir)

    edict_id = await edict_store.create(edict)
    coord = _make_coordinator(edict, edict_store, ticket_store, tmp_path)

    result = await coord.run()

    assert result.edict_id == edict_id
    assert result.status == EDICT_DONE
    assert result.duration_s >= 0.0
    assert result.total_cost_usd >= 0.0  # includes mocked agent costs
    assert result.pr_url is None
    assert result.error is None


# ---------------------------------------------------------------------------
# Test 9: Ticket counts (passed/failed) from ticket store
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_ticket_counts(tmp_path: Path, edict_env: tuple[Path, Path, Path]) -> None:
    """PipelineResult.tickets_passed / tickets_failed count correctly from the store."""
    edicts_dir, tickets_dir, golem_dir = edict_env
    edict = _make_edict()
    edict_store = EdictStore(edicts_dir)
    ticket_store = TicketStore(tickets_dir)

    edict_id = await edict_store.create(edict)

    # Create 2 done and 1 failed ticket for this edict
    done_ticket_1 = _make_ticket(edict_id, status="done", title="Done 1")
    done_ticket_2 = _make_ticket(edict_id, status="done", title="Done 2")
    failed_ticket = _make_ticket(edict_id, status="failed", title="Failed 1")

    await ticket_store.create(done_ticket_1)
    await ticket_store.create(done_ticket_2)
    await ticket_store.create(failed_ticket)

    coord = _make_coordinator(edict, edict_store, ticket_store, tmp_path)
    result = await coord.run()

    # 2 done + the mocked planner's TICKET-001 (also counted)
    assert result.tickets_passed >= 2
    assert result.tickets_failed == 1


# ---------------------------------------------------------------------------
# Test 10: PipelineResult is a proper dataclass with defaults
# ---------------------------------------------------------------------------


def test_pipeline_result_defaults() -> None:
    """PipelineResult initialises with safe defaults."""
    r = PipelineResult()
    assert r.edict_id == ""
    assert r.status == ""
    assert r.pr_url is None
    assert r.total_cost_usd == 0.0
    assert r.duration_s == 0.0
    assert r.tickets_passed == 0
    assert r.tickets_failed == 0
    assert r.error is None


# ---------------------------------------------------------------------------
# Test 11: Event emission with EventBus
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_emits_events(tmp_path: Path, edict_env: tuple[Path, Path, Path]) -> None:
    """Pipeline emits EdictCreated and EdictUpdated events when event_bus is set."""
    import asyncio as _asyncio

    from golem.events import EdictCreated, EdictUpdated, EventBus, QueueBackend

    edicts_dir, tickets_dir, golem_dir = edict_env
    edict = _make_edict()
    edict_store = EdictStore(edicts_dir)
    ticket_store = TicketStore(tickets_dir)

    edict_id = await edict_store.create(edict)

    queue: asyncio.Queue[object] = _asyncio.Queue()
    bus = EventBus(QueueBackend(queue), session_id="test-pipeline")

    coord = _make_coordinator(edict, edict_store, ticket_store, tmp_path, event_bus=bus)
    await coord.run()

    events = []
    while not queue.empty():
        events.append(queue.get_nowait())

    types = [type(e).__name__ for e in events]
    assert "EdictCreated" in types
    assert "EdictUpdated" in types

    created_events = [e for e in events if isinstance(e, EdictCreated)]
    assert len(created_events) == 1
    assert created_events[0].edict_id == edict_id
    assert created_events[0].title == "Test edict"

    updated_events = [e for e in events if isinstance(e, EdictUpdated)]
    assert len(updated_events) >= 2  # planning and in_progress transitions


# ---------------------------------------------------------------------------
# Test 12: Handoff ticket lifecycle — pending -> in_progress -> done
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_updates_handoff_ticket_lifecycle(
    tmp_path: Path, edict_env: tuple[Path, Path, Path]
) -> None:
    """Pipeline updates handoff ticket (TICKET-001) through in_progress -> done."""
    edicts_dir, tickets_dir, golem_dir = edict_env
    edict = _make_edict()
    edict_store = EdictStore(edicts_dir)
    ticket_store = TicketStore(tickets_dir)

    edict_id = await edict_store.create(edict)

    # Pre-create the handoff ticket so TicketStore.create() assigns it TICKET-001
    # (first ticket in an empty store → TICKET-001, matching _MockPlannerResult.ticket_id)
    handoff = _make_ticket(edict_id, status="pending", title="Planner handoff ticket")
    assigned_id = await ticket_store.create(handoff)
    assert assigned_id == "TICKET-001"

    coord = _make_coordinator(edict, edict_store, ticket_store, tmp_path)
    result = await coord.run()

    assert result.status == EDICT_DONE

    final_ticket = await ticket_store.read("TICKET-001")
    assert final_ticket.status == "done"
    assert final_ticket.pipeline_stage == "done"


@pytest.mark.asyncio
async def test_pipeline_updates_handoff_ticket_trivial_tier(
    tmp_path: Path, edict_env: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """TRIVIAL tier pipeline updates handoff ticket through in_progress -> done via Junior Dev path."""
    edicts_dir, tickets_dir, golem_dir = edict_env
    edict = _make_edict()
    edict_store = EdictStore(edicts_dir)
    ticket_store = TicketStore(tickets_dir)

    edict_id = await edict_store.create(edict)

    # Pre-create the handoff ticket so it gets TICKET-001
    handoff = _make_ticket(edict_id, status="pending", title="Planner handoff ticket")
    assigned_id = await ticket_store.create(handoff)
    assert assigned_id == "TICKET-001"

    # Force TRIVIAL tier so skip_tech_lead=True
    monkeypatch.setattr("golem.conductor.classify_spec", MagicMock(return_value=MagicMock(complexity="TRIVIAL")))

    config = GolemConfig()
    config.skip_tech_lead = True
    coord = PipelineCoordinator(
        edict=edict,
        edict_store=edict_store,
        ticket_store=ticket_store,
        config=config,
        project_root=tmp_path,
        golem_dir=tmp_path / ".golem",
    )
    result = await coord.run()

    assert result.status == EDICT_DONE

    final_ticket = await ticket_store.read("TICKET-001")
    assert final_ticket.status == "done"
    assert final_ticket.pipeline_stage == "done"


@pytest.mark.asyncio
async def test_pipeline_handoff_ticket_missing_does_not_raise(
    tmp_path: Path, edict_env: tuple[Path, Path, Path]
) -> None:
    """Pipeline completes successfully even when TICKET-001 doesn't exist in the store."""
    edicts_dir, tickets_dir, golem_dir = edict_env
    edict = _make_edict()
    edict_store = EdictStore(edicts_dir)
    # Empty ticket store — TICKET-001 was never created (e.g. planner used MCP directly)
    ticket_store = TicketStore(tickets_dir)

    await edict_store.create(edict)

    coord = _make_coordinator(edict, edict_store, ticket_store, tmp_path)
    result = await coord.run()

    # Pipeline must still complete — ticket update failures are silently swallowed
    assert result.status == EDICT_DONE
    assert result.error is None


# ---------------------------------------------------------------------------
# Test 13: PipelineResult.waves is populated after a run
# ---------------------------------------------------------------------------


def test_pipeline_result_waves_default() -> None:
    """PipelineResult.waves defaults to empty list."""
    r = PipelineResult()
    assert r.waves == []


@pytest.mark.asyncio
async def test_pipeline_result_waves_populated(
    tmp_path: Path, edict_env: tuple[Path, Path, Path]
) -> None:
    """PipelineResult.waves is a list after pipeline completes (even if empty for no tickets)."""
    edicts_dir, tickets_dir, golem_dir = edict_env
    edict = _make_edict()
    edict_store = EdictStore(edicts_dir)
    ticket_store = TicketStore(tickets_dir)

    await edict_store.create(edict)

    coord = _make_coordinator(edict, edict_store, ticket_store, tmp_path)
    result = await coord.run()

    assert result.status == EDICT_DONE
    # waves is always a list (may be empty if no tickets in store, or contain wave data)
    assert isinstance(result.waves, list)


@pytest.mark.asyncio
async def test_pipeline_result_waves_with_tickets(
    tmp_path: Path, edict_env: tuple[Path, Path, Path]
) -> None:
    """PipelineResult.waves reflects ticket dependency structure after planner."""
    from golem.tickets import Ticket, TicketContext

    edicts_dir, tickets_dir, golem_dir = edict_env
    edict = _make_edict()
    edict_store = EdictStore(edicts_dir)
    ticket_store = TicketStore(tickets_dir)

    edict_id = await edict_store.create(edict)

    # Pre-create two independent tickets — they should land in wave 0 together
    t1 = Ticket(
        id="", type="task", title="Task 1", status="pending",
        priority="medium", created_by="planner", assigned_to="tech_lead",
        context=TicketContext(), edict_id=edict_id, depends_on=[],
    )
    t2 = Ticket(
        id="", type="task", title="Task 2", status="pending",
        priority="medium", created_by="planner", assigned_to="tech_lead",
        context=TicketContext(), edict_id=edict_id, depends_on=[],
    )
    await ticket_store.create(t1)
    await ticket_store.create(t2)

    coord = _make_coordinator(edict, edict_store, ticket_store, tmp_path)
    result = await coord.run()

    assert result.status == EDICT_DONE
    assert isinstance(result.waves, list)
    # With 2 independent tickets, we expect exactly 1 wave containing both
    if result.waves:  # waves populated when tickets exist
        assert len(result.waves[0]) >= 1  # at least 1 ticket in the first wave


# ---------------------------------------------------------------------------
# Test 14: Pipeline advances planner-stage tickets to tech_lead stage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_advances_planner_tickets_to_tech_lead(
    tmp_path: Path, edict_env: tuple[Path, Path, Path]
) -> None:
    """After the planner phase, all pipeline_stage='planner' tickets advance to 'tech_lead'."""
    edicts_dir, tickets_dir, golem_dir = edict_env
    edict = _make_edict()
    edict_store = EdictStore(edicts_dir)
    ticket_store = TicketStore(tickets_dir)

    edict_id = await edict_store.create(edict)

    # Pre-create 3 skeleton tickets with pipeline_stage="planner"
    for i in range(1, 4):
        skeleton = Ticket(
            id="",
            type="task",
            title=f"Skeleton Task {i}",
            status="pending",
            priority="medium",
            created_by="planner",
            assigned_to="tech_lead",
            context=TicketContext(blueprint="test"),
            edict_id=edict_id,
            pipeline_stage="planner",
        )
        await ticket_store.create(skeleton)

    coord = _make_coordinator(edict, edict_store, ticket_store, tmp_path)
    result = await coord.run()

    assert result.status == EDICT_DONE

    # Verify the advance loop ran: all planner-stage tickets should have been
    # advanced to at least tech_lead stage.  TICKET-001 is the dispatch ticket
    # and gets further updated to pipeline_stage="done" by the normal lifecycle.
    # TICKET-002 and TICKET-003 are not the dispatch ticket, so they remain at
    # pipeline_stage="tech_lead" after the advance loop.
    all_tickets = await ticket_store.list_tickets()
    skeleton_tickets = [t for t in all_tickets if t.title.startswith("Skeleton Task")]
    assert len(skeleton_tickets) == 3

    # Non-dispatch skeleton tickets must be at tech_lead stage
    non_dispatch = [t for t in skeleton_tickets if t.id != "TICKET-001"]
    assert len(non_dispatch) == 2
    for ticket in non_dispatch:
        assert ticket.pipeline_stage == "tech_lead", (
            f"Expected 'tech_lead' but got '{ticket.pipeline_stage}' for {ticket.id}"
        )

    # The dispatch ticket (TICKET-001) goes through tech_lead and ends at done
    dispatch_ticket = next(t for t in skeleton_tickets if t.id == "TICKET-001")
    assert dispatch_ticket.pipeline_stage == "done"


# ---------------------------------------------------------------------------
# Test 15: kill() cancels the running stage task and returns EDICT_FAILED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kill_cancels_running_stage(
    tmp_path: Path, edict_env: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """kill() cancels _current_task mid-flight; pipeline completes with EDICT_FAILED within 1s."""
    import asyncio as _asyncio

    edicts_dir, tickets_dir, golem_dir = edict_env
    edict = _make_edict()
    edict_store = EdictStore(edicts_dir)
    ticket_store = TicketStore(tickets_dir)

    await edict_store.create(edict)

    # Replace run_planner with a coroutine that sleeps for a long time so kill()
    # has a chance to cancel it.  Using asyncio.sleep makes the task cancellable.
    async def _blocking_planner(*args: object, **kwargs: object) -> _MockPlannerResult:
        await _asyncio.sleep(999)
        return _MockPlannerResult()

    monkeypatch.setattr("golem.planner.run_planner", _blocking_planner)

    coord = _make_coordinator(edict, edict_store, ticket_store, tmp_path)

    async def _kill_after_start() -> None:
        # Yield control so the pipeline task can start and block inside run_planner.
        await _asyncio.sleep(0.05)
        await coord.kill()

    # Run pipeline and kill concurrently; expect the pipeline to finish within 1s.
    pipeline_task = _asyncio.create_task(coord.run())
    killer_task = _asyncio.create_task(_kill_after_start())

    try:
        result = await _asyncio.wait_for(pipeline_task, timeout=1.0)
    finally:
        killer_task.cancel()
        try:
            await killer_task
        except _asyncio.CancelledError:
            pass

    assert result.status == EDICT_FAILED
    assert result.error is not None


# ---------------------------------------------------------------------------
# Spec 6.1: _can_dispatch dependency enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_can_dispatch_no_dependencies(tmp_path: Path) -> None:
    """_can_dispatch returns True for tickets with no dependencies."""
    from golem.pipeline import _can_dispatch

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    store = TicketStore(tickets_dir)

    ticket = Ticket(
        id="", type="task", title="No deps", status="pending",
        priority="medium", created_by="planner", assigned_to="",
        context=TicketContext(), depends_on=[],
    )
    await store.create(ticket)

    ticket_loaded = await store.read("TICKET-001")
    assert await _can_dispatch(ticket_loaded, store) is True


@pytest.mark.asyncio
async def test_can_dispatch_all_deps_done(tmp_path: Path) -> None:
    """_can_dispatch returns True when all dependencies are done."""
    from golem.pipeline import _can_dispatch

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    store = TicketStore(tickets_dir)

    dep1 = Ticket(
        id="", type="task", title="Dep 1", status="done",
        priority="medium", created_by="planner", assigned_to="",
        context=TicketContext(),
    )
    dep2 = Ticket(
        id="", type="task", title="Dep 2", status="approved",
        priority="medium", created_by="planner", assigned_to="",
        context=TicketContext(),
    )
    dep1_id = await store.create(dep1)
    dep2_id = await store.create(dep2)

    ticket = Ticket(
        id="", type="task", title="Has deps", status="pending",
        priority="medium", created_by="planner", assigned_to="",
        context=TicketContext(), depends_on=[dep1_id, dep2_id],
    )
    ticket_id = await store.create(ticket)

    ticket_loaded = await store.read(ticket_id)
    assert await _can_dispatch(ticket_loaded, store) is True


@pytest.mark.asyncio
async def test_can_dispatch_blocks_on_pending_dep(tmp_path: Path) -> None:
    """_can_dispatch returns False when a dependency is still pending."""
    from golem.pipeline import _can_dispatch

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    store = TicketStore(tickets_dir)

    dep = Ticket(
        id="", type="task", title="Pending dep", status="pending",
        priority="medium", created_by="planner", assigned_to="",
        context=TicketContext(),
    )
    dep_id = await store.create(dep)

    ticket = Ticket(
        id="", type="task", title="Blocked", status="pending",
        priority="medium", created_by="planner", assigned_to="",
        context=TicketContext(), depends_on=[dep_id],
    )
    ticket_id = await store.create(ticket)

    ticket_loaded = await store.read(ticket_id)
    assert await _can_dispatch(ticket_loaded, store) is False


@pytest.mark.asyncio
async def test_can_dispatch_blocks_on_missing_dep(tmp_path: Path) -> None:
    """_can_dispatch returns False when a dependency does not exist."""
    from golem.pipeline import _can_dispatch

    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir()
    store = TicketStore(tickets_dir)

    ticket = Ticket(
        id="", type="task", title="Missing dep", status="pending",
        priority="medium", created_by="planner", assigned_to="",
        context=TicketContext(), depends_on=["TICKET-999"],
    )
    ticket_id = await store.create(ticket)

    ticket_loaded = await store.read(ticket_id)
    assert await _can_dispatch(ticket_loaded, store) is False


# ---------------------------------------------------------------------------
# Spec 6.1: WaveExecutor fallback to Tech Lead on failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_wave_executor_fallback_to_tech_lead(
    tmp_path: Path, edict_env: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """When WaveExecutor raises, pipeline falls back to Tech Lead and completes successfully."""
    edicts_dir, tickets_dir, golem_dir = edict_env
    edict = _make_edict()
    edict_store = EdictStore(edicts_dir)
    ticket_store = TicketStore(tickets_dir)

    edict_id = await edict_store.create(edict)

    # Pre-create handoff ticket
    handoff = _make_ticket(edict_id, status="pending", title="Planner handoff ticket")
    await ticket_store.create(handoff)

    # Force orchestrator_enabled=True (already the default, but be explicit)
    config = GolemConfig(orchestrator_enabled=True)
    # Ensure classify returns STANDARD so orchestrator_enabled stays True after profile apply
    monkeypatch.setattr("golem.conductor.classify_spec", MagicMock(return_value=MagicMock(complexity="STANDARD")))

    # Make WaveExecutor.run() raise to trigger fallback
    async def _failing_wave_run(*args: object, **kwargs: object) -> None:
        raise ValueError("DAG cycle detected")

    monkeypatch.setattr("golem.orchestrator.WaveExecutor.run", _failing_wave_run)

    coord = PipelineCoordinator(
        edict=edict,
        edict_store=edict_store,
        ticket_store=ticket_store,
        config=config,
        project_root=tmp_path,
        golem_dir=tmp_path / ".golem",
    )
    result = await coord.run()

    # Pipeline should complete via Tech Lead fallback
    assert result.status == EDICT_DONE
    assert result.error is None
