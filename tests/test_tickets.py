from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from golem.tickets import Ticket, TicketContext, TicketStore


def _make_ticket(title: str = "Test Ticket", status: str = "pending", assigned_to: str = "tech_lead") -> Ticket:
    return Ticket(
        id="",
        type="task",
        title=title,
        status=status,
        priority="medium",
        created_by="planner",
        assigned_to=assigned_to,
        context=TicketContext(),
    )


@pytest.mark.asyncio
async def test_create_ticket_writes_json() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = TicketStore(Path(tmpdir) / "tickets")
        ticket = _make_ticket()
        ticket_id = await store.create(ticket)
        assert (Path(tmpdir) / "tickets" / f"{ticket_id}.json").exists()


@pytest.mark.asyncio
async def test_read_ticket_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = TicketStore(Path(tmpdir) / "tickets")
        original = _make_ticket("My Task", "pending", "writer_1")
        original.context.blueprint = "Build something"
        ticket_id = await store.create(original)
        loaded = await store.read(ticket_id)
        assert loaded.title == "My Task"
        assert loaded.status == "pending"
        assert loaded.assigned_to == "writer_1"
        assert loaded.context.blueprint == "Build something"


@pytest.mark.asyncio
async def test_update_appends_history() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = TicketStore(Path(tmpdir) / "tickets")
        ticket = _make_ticket()
        ticket_id = await store.create(ticket)
        await store.update(ticket_id, "in_progress", "Starting work", agent="writer_1")
        loaded = await store.read(ticket_id)
        assert loaded.status == "in_progress"
        assert len(loaded.history) == 2  # created + update
        assert loaded.history[1].action == "status_changed_to_in_progress"


@pytest.mark.asyncio
async def test_list_tickets_filters_by_status() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = TicketStore(Path(tmpdir) / "tickets")
        t1 = _make_ticket("Task 1", "pending")
        t2 = _make_ticket("Task 2", "in_progress")
        t3 = _make_ticket("Task 3", "pending")
        await store.create(t1)
        await store.create(t2)
        await store.create(t3)
        pending = await store.list_tickets(status_filter="pending")
        assert len(pending) == 2
        in_prog = await store.list_tickets(status_filter="in_progress")
        assert len(in_prog) == 1


@pytest.mark.asyncio
async def test_list_tickets_filters_by_assignee() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = TicketStore(Path(tmpdir) / "tickets")
        t1 = _make_ticket("Task 1", assigned_to="writer_1")
        t2 = _make_ticket("Task 2", assigned_to="writer_2")
        t3 = _make_ticket("Task 3", assigned_to="writer_1")
        await store.create(t1)
        await store.create(t2)
        await store.create(t3)
        w1_tickets = await store.list_tickets(assigned_to_filter="writer_1")
        assert len(w1_tickets) == 2
        w2_tickets = await store.list_tickets(assigned_to_filter="writer_2")
        assert len(w2_tickets) == 1


@pytest.mark.asyncio
async def test_ticket_id_auto_increments() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = TicketStore(Path(tmpdir) / "tickets")
        id1 = await store.create(_make_ticket("T1"))
        id2 = await store.create(_make_ticket("T2"))
        id3 = await store.create(_make_ticket("T3"))
        assert id1 == "TICKET-001"
        assert id2 == "TICKET-002"
        assert id3 == "TICKET-003"


@pytest.mark.asyncio
async def test_context_preserves_file_contents() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = TicketStore(Path(tmpdir) / "tickets")
        ticket = _make_ticket()
        ticket.context.files = {
            "src/main.py": "print('hello')\n",
            "tests/test_main.py": "def test_main(): pass\n",
        }
        ticket_id = await store.create(ticket)
        loaded = await store.read(ticket_id)
        assert loaded.context.files["src/main.py"] == "print('hello')\n"
        assert loaded.context.files["tests/test_main.py"] == "def test_main(): pass\n"


@pytest.mark.asyncio
async def test_concurrent_creates_no_corruption() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = TicketStore(Path(tmpdir) / "tickets")
        tickets = [_make_ticket(f"Task {i}") for i in range(5)]
        ids = await asyncio.gather(*[store.create(t) for t in tickets])
        assert len(set(ids)) == 5  # all unique
        # Verify all files exist
        for ticket_id in ids:
            assert (Path(tmpdir) / "tickets" / f"{ticket_id}.json").exists()


@pytest.mark.asyncio
async def test_read_case_insensitive() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = TicketStore(Path(tmpdir) / "tickets")
        ticket_id = await store.create(_make_ticket("Case Test"))
        # ticket_id is uppercase (TICKET-001), try reading with lowercase
        loaded = await store.read(ticket_id.lower())
        assert loaded.title == "Case Test"
