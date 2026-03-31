"""Tests for planner handoff document creation and tech lead prompt injection."""

from __future__ import annotations

import re
import tempfile
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from golem.config import GolemConfig
from golem.tech_lead import _TECH_LEAD_PROMPT_TEMPLATE


# ---------------------------------------------------------------------------
# SDK mock helpers (mirrors test_planner.py)
# ---------------------------------------------------------------------------


def _make_mock_sdk_client(
    fake_gen_fn: Callable[..., AsyncGenerator[Any, None]],
) -> type:
    class _MockClient:
        def __init__(self, options: Any = None, **kwargs: Any) -> None:
            self._prompt: str = ""
            self._gen: AsyncGenerator[Any, None] | None = None

        async def __aenter__(self) -> "_MockClient":
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

        async def query(self, prompt: str, session_id: str = "default") -> None:
            self._prompt = prompt
            self._gen = fake_gen_fn(prompt)

        async def receive_response(self) -> AsyncGenerator[Any, None]:  # type: ignore[override]
            if self._gen is None:
                self._gen = fake_gen_fn()
            async for msg in self._gen:
                yield msg

        def interrupt(self) -> None:
            pass

    return _MockClient


class _PassthroughCoordinator:
    def __init__(self, config: GolemConfig) -> None:
        pass

    async def run_with_recovery(self, session_fn: Any, **kwargs: Any) -> Any:
        return await session_fn()


async def _fake_query(*args: Any, **kwargs: Any) -> AsyncGenerator[Any, None]:
    """Fake SDK query that writes plans/overview.md and creates a ticket."""
    prompt = args[0] if args else kwargs.get("prompt", "")

    match = re.search(r"\*\*Golem Directory:\*\*\s+`([^`]+)`", prompt)
    if match:
        golem_dir = Path(match.group(1))
        (golem_dir / "plans").mkdir(parents=True, exist_ok=True)
        (golem_dir / "plans" / "overview.md").write_text(
            "# Overview\n\n## Blueprint\nTest blueprint.\n\nMore details here.\n",
            encoding="utf-8",
        )
        (golem_dir / "plans" / "task-001.md").write_text("# Task 001\n\nDo the thing.\n", encoding="utf-8")
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

        with patch("golem.supervisor.ClaudeSDKClient", _make_mock_sdk_client(_fake_query)), \
             patch("golem.recovery.RecoveryCoordinator", _PassthroughCoordinator):
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
            "- Existing test suite uses pytest-asyncio\n"
            "- No TypeScript anywhere\n"
        )
        (handoffs_dir / "planner-to-tech-lead.md").write_text(
            handoff_content, encoding="utf-8"
        )

        prompt = _build_tech_lead_prompt(golem_dir, Path(tmpdir))

        assert "brownfield Python project" in prompt
        assert "pytest-asyncio" in prompt
        assert "No TypeScript" in prompt


def test_tech_lead_prompt_empty_handoff() -> None:
    """When no handoff file exists, the placeholder is replaced with empty string."""
    with tempfile.TemporaryDirectory() as tmpdir:
        golem_dir = Path(tmpdir) / ".golem"
        golem_dir.mkdir(parents=True)

        prompt = _build_tech_lead_prompt(golem_dir, Path(tmpdir))

        assert "{planner_handoff}" not in prompt


def test_tech_lead_prompt_has_handoff_placeholder() -> None:
    """The Tech Lead prompt template must contain the {planner_handoff} placeholder."""
    template = _TECH_LEAD_PROMPT_TEMPLATE.read_text(encoding="utf-8")
    assert "{planner_handoff}" in template
