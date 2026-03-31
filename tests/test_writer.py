from __future__ import annotations

import tempfile
from pathlib import Path
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


def test_build_writer_prompt_injects_reference_content(tmp_path: Path) -> None:
    """When golem_dir is provided, reference file content is injected inline."""
    ref_file = tmp_path / "references" / "api.md"
    ref_file.parent.mkdir(parents=True)
    ref_file.write_text("# API Reference\nGET /users returns a list.", encoding="utf-8")

    ticket = _make_ticket_with_context()
    ticket.context.references = ["references/api.md"]
    prompt = build_writer_prompt(ticket, golem_dir=tmp_path)

    assert "### references/api.md" in prompt
    assert "# API Reference" in prompt
    assert "GET /users returns a list." in prompt


def test_build_writer_prompt_reference_per_file_cap(tmp_path: Path) -> None:
    """Reference content is truncated at 5,000 chars per file."""
    ref_file = tmp_path / "big.md"
    ref_file.write_text("X" * 10_000, encoding="utf-8")

    ticket = _make_ticket_with_context()
    ticket.context.references = ["big.md"]
    prompt = build_writer_prompt(ticket, golem_dir=tmp_path)

    # Content is capped at 5000 chars
    assert "### big.md" in prompt
    # The injected content should be exactly 5000 X's, not 10000
    section_start = prompt.index("### big.md\n") + len("### big.md\n")
    # Find the end of the section (next ### or end of references area)
    x_count = 0
    for ch in prompt[section_start:]:
        if ch == "X":
            x_count += 1
        else:
            break
    assert x_count == 5_000


def test_build_writer_prompt_reference_total_cap(tmp_path: Path) -> None:
    """Once total reference content exceeds 20,000 chars, remaining files show omitted message."""
    # Create 5 files, each 5000 chars — total would be 25000, exceeding 20000 cap
    for i in range(5):
        ref_file = tmp_path / f"ref{i}.md"
        ref_file.write_text(f"{'A' * 5_000}", encoding="utf-8")

    ticket = _make_ticket_with_context()
    ticket.context.references = [f"ref{i}.md" for i in range(5)]
    prompt = build_writer_prompt(ticket, golem_dir=tmp_path)

    # First 4 files fit (4 * 5000 = 20000), 5th should be omitted
    for i in range(4):
        assert f"### ref{i}.md" in prompt
    assert "### ref4.md" in prompt
    assert "(content omitted -- read if needed)" in prompt


def test_build_writer_prompt_reference_missing_file(tmp_path: Path) -> None:
    """Missing reference files get a (file not found) placeholder."""
    ticket = _make_ticket_with_context()
    ticket.context.references = ["does_not_exist.md"]
    prompt = build_writer_prompt(ticket, golem_dir=tmp_path)

    assert "### does_not_exist.md" in prompt
    assert "(file not found)" in prompt


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
