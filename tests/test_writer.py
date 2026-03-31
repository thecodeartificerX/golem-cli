from __future__ import annotations

import json
import tempfile
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import pytest

from golem.config import GolemConfig
from golem.qa import QACheck, QAResult
from golem.tickets import Ticket, TicketContext, TicketStore
from golem.writer import WriterResult, build_writer_prompt, spawn_writer_pair


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
        # Create ticket JSON on disk so forced QA / ticket update works
        _write_ticket_json(golem_dir / "tickets", ticket)
        config = GolemConfig()
        captured_cwd: list[str] = []

        async def fake_query(prompt, options=None, **kwargs):
            if options:
                captured_cwd.append(options.cwd)
            return
            yield

        # Writer never calls run_qa, so the harness forces it.
        # Mock run_qa to pass so we don't need real commands.
        passing_qa = QAResult(passed=True, checks=[], summary="0/0 checks passed.")
        with patch("golem.writer.query", side_effect=fake_query), \
             patch("golem.writer.run_qa", return_value=passing_qa):
            result = await spawn_writer_pair(ticket, tmpdir, config, golem_dir=golem_dir)

        assert captured_cwd == [tmpdir]
        assert isinstance(result, WriterResult)


def _write_ticket_json(tickets_dir: Path, ticket: Ticket) -> None:
    """Write a ticket JSON file for harness tests that need to read/update tickets."""
    tickets_dir.mkdir(parents=True, exist_ok=True)
    path = tickets_dir / f"{ticket.id}.json"
    path.write_text(json.dumps(asdict(ticket), indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Verification gate tests
# ---------------------------------------------------------------------------


def test_verification_gate_text_in_worker_prompt() -> None:
    """The verification gate text must appear in the worker prompt template."""
    prompt_path = Path(__file__).parent.parent / "src" / "golem" / "prompts" / "worker.md"
    content = prompt_path.read_text(encoding="utf-8")
    assert "Verification Gate (MANDATORY)" in content
    assert "RUN the tests. Quote the output." in content
    assert "pipeline will reject your submission" in content


def test_verification_gate_rework_text_in_worker_prompt() -> None:
    """The rework path must also mandate re-running QA."""
    prompt_path = Path(__file__).parent.parent / "src" / "golem" / "prompts" / "worker.md"
    content = prompt_path.read_text(encoding="utf-8")
    assert "re-run QA (MANDATORY" in content


@pytest.mark.asyncio
async def test_harness_detects_missing_qa_and_forces_run() -> None:
    """When writer never calls run_qa, the harness forces a QA run."""
    with tempfile.TemporaryDirectory() as tmpdir:
        golem_dir = Path(tmpdir) / ".golem"
        ticket = _make_ticket_with_context()
        _write_ticket_json(golem_dir / "tickets", ticket)
        config = GolemConfig()

        async def fake_query(prompt, options=None, **kwargs):
            # Writer session that never calls run_qa
            return
            yield

        passing_qa = QAResult(passed=True, checks=[
            QACheck(type="acceptance", tool="python -m py_compile src/main.py", passed=True, stdout="", stderr=""),
        ], summary="1/1 checks passed.")

        with patch("golem.writer.query", side_effect=fake_query), \
             patch("golem.writer.run_qa", return_value=passing_qa) as mock_qa:
            result = await spawn_writer_pair(ticket, tmpdir, config, golem_dir=golem_dir)

        assert result.qa_called is False
        assert result.qa_forced is True
        assert result.qa_passed is True
        mock_qa.assert_called_once()


@pytest.mark.asyncio
async def test_harness_forced_qa_failure_triggers_rework() -> None:
    """When forced QA fails, the harness updates the ticket to needs_work."""
    with tempfile.TemporaryDirectory() as tmpdir:
        golem_dir = Path(tmpdir) / ".golem"
        ticket = _make_ticket_with_context()
        _write_ticket_json(golem_dir / "tickets", ticket)
        config = GolemConfig()

        async def fake_query(prompt, options=None, **kwargs):
            return
            yield

        failing_qa = QAResult(passed=False, checks=[
            QACheck(type="acceptance", tool="python -m py_compile src/main.py", passed=False, stdout="", stderr="error"),
        ], summary="0/1 checks passed. Failed: ['python -m py_compile src/main.py']")

        with patch("golem.writer.query", side_effect=fake_query), \
             patch("golem.writer.run_qa", return_value=failing_qa):
            result = await spawn_writer_pair(ticket, tmpdir, config, golem_dir=golem_dir)

        assert result.qa_called is False
        assert result.qa_forced is True
        assert result.qa_passed is False

        # Verify ticket was updated to needs_work
        store = TicketStore(golem_dir / "tickets")
        updated_ticket = await store.read(ticket.id)
        assert updated_ticket.status == "needs_work"
        # Check that the harness note is in the history
        harness_events = [e for e in updated_ticket.history if e.agent == "harness"]
        assert len(harness_events) == 1
        assert "writer skipped QA" in harness_events[0].note


@pytest.mark.asyncio
async def test_harness_skips_forced_qa_when_writer_called_run_qa() -> None:
    """When writer calls run_qa during the session, the harness does not force it."""
    with tempfile.TemporaryDirectory() as tmpdir:
        golem_dir = Path(tmpdir) / ".golem"
        ticket = _make_ticket_with_context()
        _write_ticket_json(golem_dir / "tickets", ticket)
        config = GolemConfig()

        # Simulate a writer session that calls run_qa
        from claude_agent_sdk import AssistantMessage, ToolUseBlock

        async def fake_query_with_qa(prompt, options=None, **kwargs):
            yield AssistantMessage(
                content=[
                    ToolUseBlock(id="tool_1", name="mcp__golem-writer__run_qa", input={"worktree_path": tmpdir, "checks": []}),
                ],
                model="claude-sonnet-4-20250514",
            )

        with patch("golem.writer.query", side_effect=fake_query_with_qa), \
             patch("golem.writer.run_qa") as mock_qa:
            result = await spawn_writer_pair(ticket, tmpdir, config, golem_dir=golem_dir)

        assert result.qa_called is True
        assert result.qa_forced is False
        # run_qa should NOT have been called by the harness
        mock_qa.assert_not_called()
