from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from golem.config import GolemConfig


class _PassthroughCoordinator:
    """Test double for RecoveryCoordinator that calls session_fn() directly."""

    def __init__(self, config: GolemConfig) -> None:
        self._edict_id_calls: list[str] = []

    async def run_with_recovery(
        self,
        session_fn: Any,
        **kwargs: Any,
    ) -> Any:
        self._edict_id_calls.append(kwargs.get("edict_id", ""))
        return await session_fn()


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
            "# Overview\n\n## Blueprint\nTest blueprint.\n\nMore details here.\n",
            encoding="utf-8",
        )
        (golem_dir / "plans" / "task-001.md").write_text(
            "# Task 001\n\nDo the thing.\n",
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

        with patch("golem.supervisor.query", side_effect=_fake_query), \
             patch("golem.recovery.RecoveryCoordinator", _PassthroughCoordinator):
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

        with patch("golem.supervisor.query", side_effect=_fake_query), \
             patch("golem.recovery.RecoveryCoordinator", _PassthroughCoordinator):
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
                (gd / "plans" / "overview.md").write_text(
                    "# Overview\n\n## Blueprint\nTest blueprint.\n\nMore details here.\n",
                    encoding="utf-8",
                )
                (gd / "plans" / "task-001.md").write_text("# Task 001\n\nDo the thing.\n", encoding="utf-8")
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

        with patch("golem.supervisor.query", side_effect=_capturing_query), \
             patch("golem.recovery.RecoveryCoordinator", _PassthroughCoordinator):
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
    return (await run_planner(spec_path, golem_dir, config, repo_root)).ticket_id


# ---------------------------------------------------------------------------
# Stall and verification retry tests
# ---------------------------------------------------------------------------


def _make_ok_result() -> object:
    from golem.supervisor import ContinuationResult, ToolCallRegistry
    return ContinuationResult(
        result_text="done", cost_usd=0.0, input_tokens=0, output_tokens=0,
        turns=5, duration_s=0.1, stalled=False, stall_turn=None,
        registry=ToolCallRegistry(), continuation_count=0, exhausted=False,
    )


def _make_stalled_result() -> object:
    from golem.supervisor import ContinuationResult, ToolCallRegistry
    return ContinuationResult(
        result_text="", cost_usd=0.0, input_tokens=0, output_tokens=0,
        turns=10, duration_s=0.1, stalled=True, stall_turn=10,
        registry=ToolCallRegistry(), continuation_count=0, exhausted=False,
    )


def _write_good_plans(golem_dir: Path) -> None:
    """Write valid overview.md (>3 lines) and task-001.md."""
    (golem_dir / "plans").mkdir(parents=True, exist_ok=True)
    (golem_dir / "plans" / "overview.md").write_text(
        "# Overview\n\n## Blueprint\nTest blueprint.\n\nMore details here.\n",
        encoding="utf-8",
    )
    (golem_dir / "plans" / "task-001.md").write_text("# Task 001\n\nDo the thing.\n", encoding="utf-8")


async def _make_fallback_ticket(golem_dir: Path) -> None:
    from golem.tickets import Ticket, TicketContext, TicketStore
    store = TicketStore(golem_dir / "tickets")
    await store.create(
        Ticket(
            id="", type="task", title="TL", status="pending",
            priority="medium", created_by="planner", assigned_to="tech_lead",
            context=TicketContext(plan_file=str(golem_dir / "plans" / "overview.md")),
        )
    )


@pytest.mark.asyncio
async def test_planner_stall_triggers_retry() -> None:
    """First supervised_session stall triggers retry; second completes normally."""
    with tempfile.TemporaryDirectory() as tmpdir:
        spec_path = Path(tmpdir) / "spec.md"
        spec_path.write_text("# Test Spec\n\nBuild something.\n", encoding="utf-8")
        golem_dir = Path(tmpdir) / ".golem"
        config = GolemConfig()

        _write_good_plans(golem_dir)
        await _make_fallback_ticket(golem_dir)

        mock_session = AsyncMock(side_effect=[_make_stalled_result(), _make_ok_result()])

        with patch("golem.planner.continuation_supervised_session", mock_session), \
             patch("golem.recovery.RecoveryCoordinator", _PassthroughCoordinator):
            result = await _run_planner_helper(spec_path, golem_dir, config, Path(tmpdir))

        assert mock_session.call_count == 2
        assert result.startswith("TICKET-")


@pytest.mark.asyncio
async def test_planner_empty_overview_triggers_retry() -> None:
    """Overview.md with only 2 lines fails verification and triggers retry."""
    with tempfile.TemporaryDirectory() as tmpdir:
        spec_path = Path(tmpdir) / "spec.md"
        spec_path.write_text("# Spec\n", encoding="utf-8")
        golem_dir = Path(tmpdir) / ".golem"
        config = GolemConfig()

        # Overview has only 2 lines (not >3) — triggers verification retry
        (golem_dir / "plans").mkdir(parents=True, exist_ok=True)
        (golem_dir / "plans" / "overview.md").write_text("# Short\nonly two lines\n", encoding="utf-8")
        (golem_dir / "plans" / "task-001.md").write_text("# Task\n", encoding="utf-8")
        await _make_fallback_ticket(golem_dir)

        mock_session = AsyncMock(return_value=_make_ok_result())

        with patch("golem.planner.continuation_supervised_session", mock_session), \
             patch("golem.recovery.RecoveryCoordinator", _PassthroughCoordinator):
            result = await _run_planner_helper(spec_path, golem_dir, config, Path(tmpdir))

        # Initial session + verification retry = 2 calls
        assert mock_session.call_count == 2
        assert result.startswith("TICKET-")


@pytest.mark.asyncio
async def test_planner_no_task_files_triggers_retry() -> None:
    """Missing task-*.md files fail verification and trigger retry."""
    with tempfile.TemporaryDirectory() as tmpdir:
        spec_path = Path(tmpdir) / "spec.md"
        spec_path.write_text("# Spec\n", encoding="utf-8")
        golem_dir = Path(tmpdir) / ".golem"
        config = GolemConfig()

        # Overview is fine but no task files — triggers verification retry
        (golem_dir / "plans").mkdir(parents=True, exist_ok=True)
        (golem_dir / "plans" / "overview.md").write_text(
            "# Overview\n\n## Blueprint\nTest.\n\nMore.\n", encoding="utf-8"
        )
        await _make_fallback_ticket(golem_dir)

        mock_session = AsyncMock(return_value=_make_ok_result())

        with patch("golem.planner.continuation_supervised_session", mock_session), \
             patch("golem.recovery.RecoveryCoordinator", _PassthroughCoordinator):
            result = await _run_planner_helper(spec_path, golem_dir, config, Path(tmpdir))

        # Initial session + verification retry = 2 calls
        assert mock_session.call_count == 2
        assert result.startswith("TICKET-")


@pytest.mark.asyncio
async def test_planner_fallback_ticket_logs_warning() -> None:
    """No MCP create_ticket call → fallback ticket created and STALL_WARNING logged."""
    with tempfile.TemporaryDirectory() as tmpdir:
        spec_path = Path(tmpdir) / "spec.md"
        spec_path.write_text("# Spec\n", encoding="utf-8")
        golem_dir = Path(tmpdir) / ".golem"
        config = GolemConfig()

        _write_good_plans(golem_dir)
        # No ticket in store — planner should create fallback ticket

        with patch("golem.planner.continuation_supervised_session", AsyncMock(return_value=_make_ok_result())), \
             patch("golem.recovery.RecoveryCoordinator", _PassthroughCoordinator):
            result = await _run_planner_helper(spec_path, golem_dir, config, Path(tmpdir))

        assert result.startswith("TICKET-")
        log = (golem_dir / "progress.log").read_text(encoding="utf-8")
        assert "STALL_WARNING" in log


def test_planner_result_dataclass() -> None:
    """PlannerResult has expected fields with correct defaults."""
    from golem.planner import PlannerResult
    r = PlannerResult(ticket_id="TICKET-001")
    assert r.ticket_id == "TICKET-001"
    assert r.cost_usd == 0.0
    assert r.input_tokens == 0
    assert r.output_tokens == 0
    assert r.cache_read_tokens == 0
    assert r.num_turns == 0
    assert r.duration_ms == 0


# ---------------------------------------------------------------------------
# Spec 07: skip_research_instruction injection
# ---------------------------------------------------------------------------


def test_skip_research_instruction_injected_when_true(tmp_path: "Path") -> None:
    """When skip_research=True, the planner prompt contains the skip instruction."""
    spec_path = tmp_path / "spec.md"
    spec_path.write_text("# Test Spec\n\nBuild something.\n", encoding="utf-8")
    golem_dir = tmp_path / ".golem"

    config = GolemConfig(skip_research=True)
    captured_prompts: list[str] = []

    async def _capturing_query(*args, **kwargs):
        prompt = kwargs.get("prompt") or (args[0] if args else "")
        captured_prompts.append(prompt)
        import re
        match = re.search(r"\*\*Golem Directory:\*\*\s+`([^`]+)`", prompt)
        if match:
            gd = Path(match.group(1))
            (gd / "plans").mkdir(parents=True, exist_ok=True)
            (gd / "plans" / "overview.md").write_text(
                "# Overview\n\n## Blueprint\nTest.\n\nMore details here.\n", encoding="utf-8"
            )
            (gd / "plans" / "task-001.md").write_text("# Task 001\n\nDo the thing.\n", encoding="utf-8")
            from golem.tickets import Ticket, TicketContext, TicketStore
            store = TicketStore(gd / "tickets")
            await store.create(Ticket(
                id="", type="task", title="TL", status="pending",
                priority="medium", created_by="planner", assigned_to="tech_lead",
                context=TicketContext(plan_file=str(gd / "plans" / "overview.md")),
            ))
        return
        yield

    with patch("golem.supervisor.query", side_effect=_capturing_query), \
         patch("golem.recovery.RecoveryCoordinator", _PassthroughCoordinator):
        asyncio.run(_run_planner_helper(spec_path, golem_dir, config, tmp_path))

    assert len(captured_prompts) >= 1
    assert "RESEARCH SUB-AGENTS DISABLED" in captured_prompts[0]


def test_skip_research_instruction_absent_when_false(tmp_path: "Path") -> None:
    """When skip_research=False, the skip instruction is not injected."""
    spec_path = tmp_path / "spec.md"
    spec_path.write_text("# Test Spec\n\nBuild something.\n", encoding="utf-8")
    golem_dir = tmp_path / ".golem"

    config = GolemConfig(skip_research=False)
    captured_prompts: list[str] = []

    async def _capturing_query(*args, **kwargs):
        prompt = kwargs.get("prompt") or (args[0] if args else "")
        captured_prompts.append(prompt)
        import re
        match = re.search(r"\*\*Golem Directory:\*\*\s+`([^`]+)`", prompt)
        if match:
            gd = Path(match.group(1))
            (gd / "plans").mkdir(parents=True, exist_ok=True)
            (gd / "plans" / "overview.md").write_text(
                "# Overview\n\n## Blueprint\nTest.\n\nMore details here.\n", encoding="utf-8"
            )
            (gd / "plans" / "task-001.md").write_text("# Task 001\n\nDo the thing.\n", encoding="utf-8")
            from golem.tickets import Ticket, TicketContext, TicketStore
            store = TicketStore(gd / "tickets")
            await store.create(Ticket(
                id="", type="task", title="TL", status="pending",
                priority="medium", created_by="planner", assigned_to="tech_lead",
                context=TicketContext(plan_file=str(gd / "plans" / "overview.md")),
            ))
        return
        yield

    with patch("golem.supervisor.query", side_effect=_capturing_query), \
         patch("golem.recovery.RecoveryCoordinator", _PassthroughCoordinator):
        asyncio.run(_run_planner_helper(spec_path, golem_dir, config, tmp_path))

    assert len(captured_prompts) >= 1
    assert "RESEARCH SUB-AGENTS DISABLED" not in captured_prompts[0]


def test_stall_config_expected_actions_shorter_with_skip_research() -> None:
    """stall_config_for_role with skip_research=True returns fewer expected actions."""
    from golem.supervisor import stall_config_for_role

    with_research = stall_config_for_role("planner", 50, skip_research=False)
    without_research = stall_config_for_role("planner", 50, skip_research=True)

    assert len(without_research.expected_actions) < len(with_research.expected_actions)
    # Without research: only create_ticket
    assert "create_ticket" in without_research.expected_actions
    # With research: includes spawn explorer and spawn researcher
    assert any("explorer" in a for a in with_research.expected_actions)
    assert any("researcher" in a for a in with_research.expected_actions)


# ---------------------------------------------------------------------------
# edict_id propagation test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_planner_passes_edict_id_to_recovery_coordinator() -> None:
    """run_planner extracts edict_id from golem_dir path and passes it to RecoveryCoordinator."""
    with tempfile.TemporaryDirectory() as tmpdir:
        spec_path = Path(tmpdir) / "spec.md"
        spec_path.write_text("# Test Spec\n\nBuild something.\n", encoding="utf-8")
        # Simulate edict-style golem_dir: .golem/edicts/EDICT-001
        golem_dir = Path(tmpdir) / ".golem" / "edicts" / "EDICT-001"
        golem_dir.mkdir(parents=True)
        config = GolemConfig()

        recorded_kwargs: list[dict[str, Any]] = []

        class _RecordingCoordinator:
            def __init__(self, _config: GolemConfig) -> None:
                pass

            async def run_with_recovery(self, session_fn: Any, **kwargs: Any) -> Any:
                recorded_kwargs.append(dict(kwargs))
                # Set up plan files and ticket so run_planner can complete
                _write_good_plans(golem_dir)
                await _make_fallback_ticket(golem_dir)
                return _make_ok_result()

        with patch("golem.recovery.RecoveryCoordinator", _RecordingCoordinator):
            await _run_planner_helper(spec_path, golem_dir, config, Path(tmpdir))

        assert len(recorded_kwargs) >= 1
        assert recorded_kwargs[0].get("edict_id") == "EDICT-001"
