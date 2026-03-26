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


@pytest.mark.asyncio
async def test_list_tickets_combined_filters() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = TicketStore(Path(tmpdir) / "tickets")
        t1 = _make_ticket("Task 1", assigned_to="writer")
        t2 = _make_ticket("Task 2", assigned_to="writer")
        t3 = _make_ticket("Task 3", assigned_to="tech_lead")
        await store.create(t1)
        tid2 = await store.create(t2)
        await store.create(t3)
        # Update t2 to in_progress
        await store.update(tid2, "in_progress", "started", agent="writer")
        # Filter: status=in_progress AND assigned_to=writer
        result = await store.list_tickets(status_filter="in_progress", assigned_to_filter="writer")
        assert len(result) == 1
        assert result[0].title == "Task 2"


@pytest.mark.asyncio
async def test_read_corrupt_json_raises() -> None:
    """Reading a corrupt JSON ticket file raises json.JSONDecodeError."""
    import json

    with tempfile.TemporaryDirectory() as tmpdir:
        tickets_dir = Path(tmpdir) / "tickets"
        tickets_dir.mkdir()
        corrupt_file = tickets_dir / "TICKET-001.json"
        corrupt_file.write_text("{broken json!!!", encoding="utf-8")

        store = TicketStore(tickets_dir)
        with pytest.raises(json.JSONDecodeError):
            await store.read("TICKET-001")


@pytest.mark.asyncio
async def test_update_case_insensitive() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = TicketStore(Path(tmpdir) / "tickets")
        ticket_id = await store.create(_make_ticket("Update Test"))
        # Update using lowercase ID
        await store.update(ticket_id.lower(), "in_progress", "Started work", agent="writer")
        loaded = await store.read(ticket_id)
        assert loaded.status == "in_progress"
        assert len(loaded.history) == 2  # created + status_changed
        assert loaded.history[-1].action == "status_changed_to_in_progress"


@pytest.mark.asyncio
async def test_concurrent_updates_no_corruption() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = TicketStore(Path(tmpdir) / "tickets")
        ticket_id = await store.create(_make_ticket("Concurrent Update"))

        async def _update(i: int) -> None:
            await store.update(ticket_id, "in_progress", f"Update {i}", agent=f"agent-{i}")

        await asyncio.gather(*[_update(i) for i in range(5)])
        loaded = await store.read(ticket_id)
        # 1 created event + 5 update events = 6 total
        assert len(loaded.history) == 6
        assert loaded.status == "in_progress"


@pytest.mark.asyncio
async def test_ticket_id_format() -> None:
    """Ticket IDs must be uppercase TICKET-NNN format."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = TicketStore(Path(tmpdir) / "tickets")
        tid = await store.create(_make_ticket("Format Test"))
        assert tid.startswith("TICKET-")
        num_part = tid.split("-")[1]
        assert len(num_part) == 3
        assert num_part.isdigit()


@pytest.mark.asyncio
async def test_full_context_roundtrip() -> None:
    """All TicketContext fields survive create → read roundtrip."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = TicketStore(Path(tmpdir) / "tickets")
        ctx = TicketContext(
            plan_file="/path/to/plan.md",
            files={"src/app.py": "print('hello')\n", "tests/test.py": "pass\n"},
            references=["/ref/a.md", "/ref/b.md"],
            blueprint="Full blueprint text here",
            acceptance=["Tests pass", "Lint clean"],
            qa_checks=["uv run pytest", "ruff check ."],
            parallelism_hints=["tests independent", "lint independent"],
        )
        ticket = Ticket(
            id="", type="task", title="Full Context", status="pending",
            priority="high", created_by="test", assigned_to="writer",
            context=ctx,
        )
        tid = await store.create(ticket)
        loaded = await store.read(tid)
        assert loaded.context.plan_file == "/path/to/plan.md"
        assert loaded.context.files == {"src/app.py": "print('hello')\n", "tests/test.py": "pass\n"}
        assert loaded.context.references == ["/ref/a.md", "/ref/b.md"]
        assert loaded.context.blueprint == "Full blueprint text here"
        assert loaded.context.acceptance == ["Tests pass", "Lint clean"]
        assert loaded.context.qa_checks == ["uv run pytest", "ruff check ."]
        assert loaded.context.parallelism_hints == ["tests independent", "lint independent"]
