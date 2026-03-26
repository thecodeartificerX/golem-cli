from __future__ import annotations

import tempfile
from unittest.mock import patch

import pytest

from golem.config import GolemConfig
from golem.tickets import Ticket, TicketContext
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


@pytest.mark.asyncio
async def test_spawn_writer_pair_uses_worktree_cwd() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        ticket = _make_ticket_with_context()
        config = GolemConfig()
        captured_cwd: list[str] = []

        async def fake_query(prompt, options=None, **kwargs):
            if options:
                captured_cwd.append(options.cwd)
            return
            yield

        with patch("golem.writer.query", side_effect=fake_query):
            await spawn_writer_pair(ticket, tmpdir, config)

        assert captured_cwd == [tmpdir]
