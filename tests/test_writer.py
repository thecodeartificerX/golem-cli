from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from golem.config import GolemConfig
from golem.tickets import Ticket, TicketContext
from golem.junior_dev import build_writer_prompt, spawn_writer_pair


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

        with patch("golem.junior_dev.query", side_effect=fake_query):
            await spawn_writer_pair(ticket, tmpdir, config, golem_dir=golem_dir)

        assert captured_cwd == [tmpdir]
