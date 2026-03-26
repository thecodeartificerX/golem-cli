from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from golem.config import GolemConfig


async def _fake_query(*args, **kwargs):
    """Fake SDK query that writes plans/overview.md and creates a ticket."""
    # The query options contain cwd; we write to golem_dir based on prompt content
    # Since we can't easily extract golem_dir from options, we check kwargs/args
    # Write overview.md — the planner function will check this exists
    # We need to find golem_dir from the prompt (it contains the path)
    prompt = kwargs.get("prompt") or (args[0] if args else "")

    # Extract golem_dir from prompt (it contains the golem directory path)
    import re
    match = re.search(r"\*\*Golem Directory:\*\*\s+`([^`]+)`", prompt)
    if match:
        golem_dir = Path(match.group(1))
        (golem_dir / "plans").mkdir(parents=True, exist_ok=True)
        (golem_dir / "plans" / "overview.md").write_text(
            "# Overview\n\n## Blueprint\nTest blueprint.\n",
            encoding="utf-8",
        )
        # Also create the ticket via TicketStore
        from golem.tickets import Ticket, TicketContext, TicketStore
        store = TicketStore(golem_dir / "tickets")
        ticket = Ticket(
            id="",
            type="task",
            title="Tech Lead: Execute plans",
            status="pending",
            priority="medium",
            created_by="planner",
            assigned_to="tech_lead",
            context=TicketContext(plan_file=str(golem_dir / "plans" / "overview.md")),
        )
        await store.create(ticket)

    # Yield nothing (empty session)
    return
    yield  # make it a generator


@pytest.mark.asyncio
async def test_run_planner_creates_directories() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        spec_path = Path(tmpdir) / "spec.md"
        spec_path.write_text("# Test Spec\n\nBuild something.\n", encoding="utf-8")
        golem_dir = Path(tmpdir) / ".golem"
        config = GolemConfig()

        with patch("golem.planner.query", side_effect=_fake_query):
            await _run_planner_helper(spec_path, golem_dir, config, Path(tmpdir))

        assert (golem_dir / "research").exists()
        assert (golem_dir / "plans").exists()
        assert (golem_dir / "references").exists()


@pytest.mark.asyncio
async def test_run_planner_returns_ticket_id() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        spec_path = Path(tmpdir) / "spec.md"
        spec_path.write_text("# Test Spec\n\nBuild something.\n", encoding="utf-8")
        golem_dir = Path(tmpdir) / ".golem"
        config = GolemConfig()

        with patch("golem.planner.query", side_effect=_fake_query):
            ticket_id = await _run_planner_helper(spec_path, golem_dir, config, Path(tmpdir))

        assert ticket_id.startswith("TICKET-")
        assert (golem_dir / "tickets" / f"{ticket_id}.json").exists()


@pytest.mark.asyncio
async def test_run_planner_injects_project_context() -> None:
    """Planner prompt should include CLAUDE.md contents when present."""
    with tempfile.TemporaryDirectory() as tmpdir:
        spec_path = Path(tmpdir) / "spec.md"
        spec_path.write_text("# Test Spec\n\nBuild something.\n", encoding="utf-8")
        # Create a CLAUDE.md in the repo root
        (Path(tmpdir) / "CLAUDE.md").write_text("# Project\nThis is the project context.\n", encoding="utf-8")
        golem_dir = Path(tmpdir) / ".golem"
        config = GolemConfig()

        captured_prompts: list[str] = []

        async def _capturing_query(*args, **kwargs):
            prompt = kwargs.get("prompt") or (args[0] if args else "")
            captured_prompts.append(prompt)
            # Still do the normal fake work
            import re
            match = re.search(r"\*\*Golem Directory:\*\*\s+`([^`]+)`", prompt)
            if match:
                gd = Path(match.group(1))
                (gd / "plans").mkdir(parents=True, exist_ok=True)
                (gd / "plans" / "overview.md").write_text("# Overview\n", encoding="utf-8")
                from golem.tickets import Ticket, TicketContext, TicketStore
                store = TicketStore(gd / "tickets")
                ticket = Ticket(
                    id="", type="task", title="TL", status="pending",
                    priority="medium", created_by="planner", assigned_to="tech_lead",
                    context=TicketContext(plan_file=str(gd / "plans" / "overview.md")),
                )
                await store.create(ticket)
            return
            yield

        with patch("golem.planner.query", side_effect=_capturing_query):
            await _run_planner_helper(spec_path, golem_dir, config, Path(tmpdir))

        assert len(captured_prompts) == 1
        assert "This is the project context" in captured_prompts[0]


async def _run_planner_helper(
    spec_path: Path,
    golem_dir: Path,
    config: GolemConfig,
    repo_root: Path,
) -> str:
    from golem.planner import run_planner
    return await run_planner(spec_path, golem_dir, config, repo_root)
