from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from golem.config import GolemConfig
from golem.tickets import Ticket, TicketContext, TicketEvent
from golem.writer import build_writer_prompt, spawn_writer_pair


def _make_ticket_with_context() -> Ticket:
    ctx = TicketContext(
        plan_file="",
        files={"src/main.py": "def main(): pass\n"},
        references=["references/api.md"],
        blueprint="Build the main entry point module.",
        acceptance=["main() function exists", "no syntax errors"],
        qa_checks=["python -m py_compile src/main.py"],
        parallelism_hints=["can be split into create + test"],
    )
    return Ticket(
        id="TICKET-001",
        type="task",
        title="Implement main module",
        status="pending",
        priority="high",
        created_by="tech_lead",
        assigned_to="writer",
        context=ctx,
    )


def test_build_writer_prompt_injects_all_fields() -> None:
    ticket = _make_ticket_with_context()
    prompt = build_writer_prompt(ticket)
    assert "TICKET-001" in prompt
    assert "Implement main module" in prompt
    assert "def main(): pass" in prompt
    assert "references/api.md" in prompt
    assert "Build the main entry point module" in prompt
    assert "main() function exists" in prompt
    assert "python -m py_compile src/main.py" in prompt
    assert "can be split into create + test" in prompt


def test_build_writer_prompt_strips_empty_sections() -> None:
    ticket = _make_ticket_with_context()
    ticket.context.parallelism_hints = []
    prompt = build_writer_prompt(ticket)
    assert "{parallelism_hints}" not in prompt


def test_build_writer_prompt_no_leftover_placeholders() -> None:
    ticket = _make_ticket_with_context()
    # Set all optional fields to empty
    ticket.context.parallelism_hints = []
    ticket.context.references = []
    ticket.context.qa_checks = []
    ticket.context.acceptance = []
    prompt = build_writer_prompt(ticket)
    assert "{" not in prompt


def test_build_writer_prompt_all_fields_populated_no_placeholders() -> None:
    """With ALL context fields populated, no {placeholder} patterns should remain."""
    ctx = TicketContext(
        plan_file="",  # empty plan file — won't read from disk
        files={"src/app.py": "print('hello')\n", "tests/test_app.py": "def test(): pass\n"},
        references=["docs/api.md", "docs/guide.md"],
        blueprint="Full architectural blueprint for the project with all details",
        acceptance=["All tests pass", "No lint errors", "Coverage > 80%"],
        qa_checks=["uv run pytest", "ruff check .", "mypy ."],
        parallelism_hints=["tests can run parallel", "lint is independent"],
    )
    ticket = Ticket(
        id="TICKET-099",
        type="task",
        title="Full context ticket",
        status="in_progress",
        priority="medium",
        created_by="tech_lead",
        assigned_to="writer",
        context=ctx,
    )
    prompt = build_writer_prompt(ticket)
    # No template placeholders should remain
    import re
    leftover = re.findall(r"\{[a-z_]+\}", prompt)
    assert leftover == [], f"Unresolved placeholders: {leftover}"
    # All content should be present
    assert "TICKET-099" in prompt
    assert "src/app.py" in prompt
    assert "docs/api.md" in prompt
    assert "All tests pass" in prompt
    assert "uv run pytest" in prompt
    assert "tests can run parallel" in prompt


def test_build_writer_prompt_reads_plan_file_from_disk() -> None:
    """When plan_file points to a real file, its contents appear in the prompt."""
    with tempfile.TemporaryDirectory() as tmpdir:
        plan_path = Path(tmpdir) / "task-001.md"
        plan_path.write_text("## Step 1: Build the widget\nCreate widget.py with Widget class.", encoding="utf-8")

        ticket = _make_ticket_with_context()
        ticket.context.plan_file = str(plan_path)
        prompt = build_writer_prompt(ticket)
        assert "Build the widget" in prompt
        assert "Widget class" in prompt


@pytest.mark.asyncio
async def test_spawn_writer_pair_uses_worktree_cwd() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        golem_dir = Path(tmpdir) / ".golem"
        (golem_dir / "tickets").mkdir(parents=True)
        ticket = _make_ticket_with_context()
        config = GolemConfig()
        captured_cwd: list[str] = []

        async def fake_query(prompt, options=None, **kwargs):
            if options:
                captured_cwd.append(options.cwd)
            return
            yield

        with patch("golem.writer.query", side_effect=fake_query):
            await spawn_writer_pair(ticket, tmpdir, config, golem_dir=golem_dir)

        assert captured_cwd == [tmpdir]


def test_build_prompt_first_attempt() -> None:
    """rework_count=0: iteration placeholder resolved to '1', rework_context is empty."""
    ticket = _make_ticket_with_context()
    prompt = build_writer_prompt(ticket, rework_count=0)
    assert "{iteration}" not in prompt
    assert "{rework_context}" not in prompt
    assert "Previous Rejection" not in prompt


def test_build_prompt_rework() -> None:
    """rework_count=2, notes=['fix lint', 'wrong file'] -> iteration=3, both notes in prompt."""
    ticket = _make_ticket_with_context()
    notes = ["fix lint errors", "wrong file modified"]
    prompt = build_writer_prompt(ticket, rework_count=2, rework_notes=notes)
    assert "{iteration}" not in prompt
    assert "{rework_context}" not in prompt
    assert "fix lint errors" in prompt
    assert "wrong file modified" in prompt


def test_build_prompt_rework_limits_notes() -> None:
    """With 5 rework notes, only the last 3 appear in the prompt."""
    ticket = _make_ticket_with_context()
    notes = ["n1", "n2", "n3", "n4", "n5"]
    prompt = build_writer_prompt(ticket, rework_count=5, rework_notes=notes)
    assert "n3" in prompt
    assert "n4" in prompt
    assert "n5" in prompt
    assert "n1" not in prompt
    assert "n2" not in prompt


def test_get_rework_info_counts_needs_work() -> None:
    """_get_rework_info counts needs_work events and extracts notes."""
    from golem.writer import _get_rework_info

    ticket = _make_ticket_with_context()
    ticket.history = [
        TicketEvent(ts="2026-01-01T00:00:00+00:00", agent="tl", action="status_changed_to_needs_work", note="fix lint"),
        TicketEvent(ts="2026-01-01T00:01:00+00:00", agent="tl", action="approved", note=""),
        TicketEvent(ts="2026-01-01T00:02:00+00:00", agent="tl", action="status_changed_to_needs_work", note="wrong file"),
    ]
    count, notes = _get_rework_info(ticket)
    assert count == 2
    assert notes == ["fix lint", "wrong file"]


def test_get_rework_info_empty_history() -> None:
    """_get_rework_info returns (0, []) for a ticket with no history."""
    from golem.writer import _get_rework_info

    ticket = _make_ticket_with_context()
    # Ticket from _make_ticket_with_context has empty history by default
    count, notes = _get_rework_info(ticket)
    assert count == 0
    assert notes == []


@pytest.mark.asyncio
async def test_jitter_skip_in_test_mode() -> None:
    """With GOLEM_TEST_MODE=1, no asyncio.sleep is called for jitter."""
    import os

    slept_durations: list[float] = []

    async def fake_sleep(n: float) -> None:
        slept_durations.append(n)

    async def fake_query(*args, **kwargs):  # type: ignore[misc]
        return
        yield

    ticket = _make_ticket_with_context()
    config = GolemConfig(dispatch_jitter_max=10.0)

    with patch("golem.writer.query", side_effect=fake_query), \
         patch("asyncio.sleep", side_effect=fake_sleep), \
         patch.dict(os.environ, {"GOLEM_TEST_MODE": "1"}):
        with tempfile.TemporaryDirectory() as tmpdir:
            golem_dir = Path(tmpdir) / ".golem"
            (golem_dir / "tickets").mkdir(parents=True)
            await spawn_writer_pair(ticket, tmpdir, config, golem_dir=golem_dir)

    # asyncio.sleep may be called for retry_delay but NOT for jitter (which is 10.0s max)
    jitter_sleeps = [d for d in slept_durations if d >= 1.0]
    assert len(jitter_sleeps) == 0, f"Jitter sleep called in test mode: {slept_durations}"


@pytest.mark.asyncio
async def test_spawn_writer_pair_golem_dir_none_fallback() -> None:
    """When golem_dir is None, create_writer_mcp_server uses Path(worktree_path)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "tickets").mkdir()
        ticket = _make_ticket_with_context()
        config = GolemConfig()
        captured_mcp_dir: list[Path] = []

        original_create = spawn_writer_pair.__module__

        def fake_create_writer_mcp(golem_dir: Path):
            captured_mcp_dir.append(golem_dir)
            # Return a minimal server config
            from unittest.mock import MagicMock
            return {"name": "golem-writer", "type": "sdk", "instance": MagicMock()}

        async def fake_query(prompt, options=None, **kwargs):
            return
            yield

        with patch("golem.writer.create_writer_mcp_server", side_effect=fake_create_writer_mcp), \
             patch("golem.writer.query", side_effect=fake_query):
            await spawn_writer_pair(ticket, tmpdir, config, golem_dir=None)

        assert len(captured_mcp_dir) == 1
        assert captured_mcp_dir[0] == Path(tmpdir)
