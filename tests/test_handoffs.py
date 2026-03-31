from __future__ import annotations

import re
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from golem.config import GolemConfig
from golem.tech_lead import _TECH_LEAD_PROMPT_TEMPLATE


async def _fake_query(*args, **kwargs):
    """Fake SDK query that writes plans/overview.md and creates a ticket."""
    prompt = kwargs.get("prompt") or (args[0] if args else "")

    match = re.search(r"\*\*Golem Directory:\*\*\s+`([^`]+)`", prompt)
    if match:
        golem_dir = Path(match.group(1))
        (golem_dir / "plans").mkdir(parents=True, exist_ok=True)
        (golem_dir / "plans" / "overview.md").write_text(
            "# Overview\n\n## Blueprint\nTest blueprint.\n",
            encoding="utf-8",
        )
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

    return
    yield  # make it a generator


def _build_tech_lead_prompt(golem_dir: Path, project_root: Path) -> str:
    """Build a Tech Lead prompt with handoff injection, mirroring run_tech_lead logic."""
    handoff_path = golem_dir / "handoffs" / "planner-to-tech-lead.md"
    planner_handoff = handoff_path.read_text(encoding="utf-8") if handoff_path.exists() else ""

    template = _TECH_LEAD_PROMPT_TEMPLATE.read_text(encoding="utf-8")
    prompt = template.replace("{golem_dir}", str(golem_dir))
    prompt = prompt.replace("{spec_content}", "Test spec content")
    prompt = prompt.replace("{project_root}", str(project_root))
    prompt = prompt.replace("{planner_handoff}", planner_handoff)
    return prompt


@pytest.mark.asyncio
async def test_planner_creates_handoffs_directory() -> None:
    """run_planner should create the handoffs/ directory alongside other directories."""
    with tempfile.TemporaryDirectory() as tmpdir:
        spec_path = Path(tmpdir) / "spec.md"
        spec_path.write_text("# Test Spec\n\nBuild something.\n", encoding="utf-8")
        golem_dir = Path(tmpdir) / ".golem"
        config = GolemConfig()

        with patch("golem.planner.query", side_effect=_fake_query):
            from golem.planner import run_planner

            await run_planner(spec_path, golem_dir, config, Path(tmpdir))

        assert (golem_dir / "handoffs").exists()
        assert (golem_dir / "handoffs").is_dir()


def test_tech_lead_prompt_includes_planner_handoff() -> None:
    """When a planner handoff file exists, its content is injected into the Tech Lead prompt."""
    with tempfile.TemporaryDirectory() as tmpdir:
        golem_dir = Path(tmpdir) / ".golem"
        handoffs_dir = golem_dir / "handoffs"
        handoffs_dir.mkdir(parents=True)

        handoff_content = (
            "# Planner Handoff\n\n"
            "## Context\n"
            "This is a brownfield Python project using FastAPI.\n\n"
            "## Findings\n"
            "The codebase uses async patterns throughout.\n"
        )
        (handoffs_dir / "planner-to-tech-lead.md").write_text(handoff_content, encoding="utf-8")

        prompt = _build_tech_lead_prompt(golem_dir, Path(tmpdir))

        assert "brownfield Python project" in prompt
        assert "async patterns throughout" in prompt


def test_tech_lead_prompt_fallback_when_no_handoff() -> None:
    """When no planner handoff file exists, the placeholder is replaced with empty string."""
    with tempfile.TemporaryDirectory() as tmpdir:
        golem_dir = Path(tmpdir) / ".golem"
        golem_dir.mkdir(parents=True)

        prompt = _build_tech_lead_prompt(golem_dir, Path(tmpdir))

        assert "{planner_handoff}" not in prompt
        assert "Planner Handoff" in prompt


def test_cli_creates_handoffs_directory() -> None:
    """_create_golem_dirs should include handoffs/ in the created directories."""
    with tempfile.TemporaryDirectory() as tmpdir:
        golem_dir = Path(tmpdir) / ".golem"
        golem_dir.mkdir(parents=True)

        from golem.cli import _create_golem_dirs

        _create_golem_dirs(golem_dir)

        assert (golem_dir / "handoffs").exists()
        assert (golem_dir / "handoffs").is_dir()
