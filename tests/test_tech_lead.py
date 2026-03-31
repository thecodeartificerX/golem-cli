from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from golem.tech_lead import _cleanup_golem_worktrees, _ensure_merged_to_main
from golem.worktree import create_worktree


class _PassthroughCoordinator:
    """Test double for RecoveryCoordinator that calls session_fn() directly."""

    def __init__(self, config: Any) -> None:
        pass

    async def run_with_recovery(self, session_fn: Any, **kwargs: Any) -> Any:
        return await session_fn()


def _init_repo(path: Path) -> None:
    """Initialize a git repo with an initial commit."""
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True, capture_output=True)
    (path / "README.md").write_text("init", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True, encoding="utf-8")


@pytest.mark.asyncio
async def test_ensure_merged_noop_no_branches() -> None:
    """No golem integration branches — should be a noop."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "repo"
        repo.mkdir()
        _init_repo(repo)
        # Should not raise or change anything
        await _ensure_merged_to_main(repo)
        log = _git(repo, "log", "--oneline").stdout
        assert "init" in log


@pytest.mark.asyncio
async def test_ensure_merged_already_merged() -> None:
    """Integration branch already merged — should skip."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "repo"
        repo.mkdir()
        _init_repo(repo)

        # Create an integration branch with a commit, then merge it manually
        _git(repo, "checkout", "-b", "golem/test/integration")
        (repo / "new.txt").write_text("hello", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "integration work")
        _git(repo, "checkout", "main")
        _git(repo, "merge", "golem/test/integration", "--ff-only")

        # Now _ensure_merged_to_main should see it's already merged and skip
        await _ensure_merged_to_main(repo)
        log = _git(repo, "log", "--oneline").stdout
        assert "integration work" in log


@pytest.mark.asyncio
async def test_ensure_merged_merges_unmerged_branch() -> None:
    """Integration branch not merged — should merge it into main."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "repo"
        repo.mkdir()
        _init_repo(repo)

        # Create an integration branch with a commit, don't merge
        _git(repo, "checkout", "-b", "golem/test/integration")
        (repo / "feature.txt").write_text("new feature", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "unmerged integration work")
        _git(repo, "checkout", "main")

        # Before: main doesn't have the commit
        log_before = _git(repo, "log", "--oneline").stdout
        assert "unmerged integration work" not in log_before

        # After: _ensure_merged_to_main merges it
        await _ensure_merged_to_main(repo)
        log_after = _git(repo, "log", "--oneline").stdout
        assert "unmerged integration work" in log_after


def test_cleanup_golem_worktrees_noop_no_dir() -> None:
    """No worktrees dir — should not raise."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "repo"
        repo.mkdir()
        _init_repo(repo)
        golem_dir = Path(tmpdir) / ".golem"
        golem_dir.mkdir()
        # No worktrees/ subdir — should be a noop
        _cleanup_golem_worktrees(golem_dir, repo)


def test_cleanup_golem_worktrees_removes_worktree() -> None:
    """Cleanup removes a worktree created via git worktree add."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "repo"
        repo.mkdir()
        _init_repo(repo)

        golem_dir = Path(tmpdir) / ".golem"
        wt_dir = golem_dir / "worktrees"
        wt_dir.mkdir(parents=True)

        wt_path = wt_dir / "group-test"
        create_worktree("group-test", "golem/test/group-test", "main", wt_path, repo)
        assert wt_path.exists()

        _cleanup_golem_worktrees(golem_dir, repo)
        assert not wt_path.exists()


# ---------------------------------------------------------------------------
# supervised_session stall tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tech_lead_stall_triggers_retry() -> None:
    """First continuation_supervised_session stall triggers retry with escalated prompt."""
    from golem.config import GolemConfig
    from golem.supervisor import ContinuationResult, ToolCallRegistry
    from golem.tech_lead import run_tech_lead
    from golem.tickets import Ticket, TicketContext, TicketStore

    with tempfile.TemporaryDirectory() as tmpdir:
        golem_dir = Path(tmpdir) / ".golem"
        (golem_dir / "tickets").mkdir(parents=True)
        config = GolemConfig()

        store = TicketStore(golem_dir / "tickets")
        ticket_id = await store.create(
            Ticket(
                id="", type="task", title="TL Task", status="pending",
                priority="medium", created_by="planner", assigned_to="tech_lead",
                context=TicketContext(plan_file=""),
            )
        )

        stalled = ContinuationResult(
            result_text="", cost_usd=0.0, input_tokens=0, output_tokens=0,
            turns=10, duration_s=0.1, stalled=True, stall_turn=10,
            registry=ToolCallRegistry(), continuation_count=0, exhausted=False,
        )
        ok = ContinuationResult(
            result_text="done", cost_usd=0.0, input_tokens=0, output_tokens=0,
            turns=5, duration_s=0.1, stalled=False, stall_turn=None,
            registry=ToolCallRegistry(), continuation_count=0, exhausted=False,
        )

        with patch("golem.tech_lead.continuation_supervised_session", AsyncMock(side_effect=[stalled, ok])), \
             patch("golem.tech_lead._check_integration_commits", AsyncMock(return_value=True)), \
             patch("golem.tech_lead._ensure_merged_to_main", AsyncMock()), \
             patch("golem.recovery.RecoveryCoordinator", _PassthroughCoordinator):
            result = await run_tech_lead(ticket_id, golem_dir, config, Path(tmpdir))

        assert result.num_turns == 5  # second session result


@pytest.mark.asyncio
async def test_tech_lead_double_stall_fatal() -> None:
    """Two consecutive stalls raise RuntimeError."""
    from golem.config import GolemConfig
    from golem.supervisor import ContinuationResult, ToolCallRegistry
    from golem.tech_lead import run_tech_lead
    from golem.tickets import Ticket, TicketContext, TicketStore

    with tempfile.TemporaryDirectory() as tmpdir:
        golem_dir = Path(tmpdir) / ".golem"
        (golem_dir / "tickets").mkdir(parents=True)
        config = GolemConfig()

        store = TicketStore(golem_dir / "tickets")
        ticket_id = await store.create(
            Ticket(
                id="", type="task", title="TL Task", status="pending",
                priority="medium", created_by="planner", assigned_to="tech_lead",
                context=TicketContext(plan_file=""),
            )
        )

        stalled = ContinuationResult(
            result_text="", cost_usd=0.0, input_tokens=0, output_tokens=0,
            turns=10, duration_s=0.1, stalled=True, stall_turn=10,
            registry=ToolCallRegistry(), continuation_count=0, exhausted=False,
        )

        with patch("golem.tech_lead.continuation_supervised_session", AsyncMock(return_value=stalled)), \
             patch("golem.tech_lead._cleanup_golem_worktrees"), \
             patch("golem.tech_lead._ensure_merged_to_main", AsyncMock()), \
             patch("golem.recovery.RecoveryCoordinator", _PassthroughCoordinator):
            with pytest.raises(RuntimeError, match="stall"):
                await run_tech_lead(ticket_id, golem_dir, config, Path(tmpdir))


@pytest.mark.asyncio
async def test_tech_lead_no_commits_triggers_retry() -> None:
    """No integration commits triggers a retry with escalated prompt."""
    from golem.config import GolemConfig
    from golem.supervisor import ContinuationResult, ToolCallRegistry
    from golem.tech_lead import run_tech_lead
    from golem.tickets import Ticket, TicketContext, TicketStore

    with tempfile.TemporaryDirectory() as tmpdir:
        golem_dir = Path(tmpdir) / ".golem"
        (golem_dir / "tickets").mkdir(parents=True)
        config = GolemConfig()

        store = TicketStore(golem_dir / "tickets")
        ticket_id = await store.create(
            Ticket(
                id="", type="task", title="TL Task", status="pending",
                priority="medium", created_by="planner", assigned_to="tech_lead",
                context=TicketContext(plan_file=""),
            )
        )

        ok = ContinuationResult(
            result_text="done", cost_usd=0.0, input_tokens=0, output_tokens=0,
            turns=5, duration_s=0.1, stalled=False, stall_turn=None,
            registry=ToolCallRegistry(), continuation_count=0, exhausted=False,
        )
        mock_session = AsyncMock(return_value=ok)

        with patch("golem.tech_lead.continuation_supervised_session", mock_session), \
             patch("golem.tech_lead._check_integration_commits", AsyncMock(return_value=False)), \
             patch("golem.tech_lead._ensure_merged_to_main", AsyncMock()), \
             patch("golem.recovery.RecoveryCoordinator", _PassthroughCoordinator):
            await run_tech_lead(ticket_id, golem_dir, config, Path(tmpdir))

        # Initial session + no-commits retry = 2 calls
        assert mock_session.call_count == 2


@pytest.mark.asyncio
async def test_ensure_merged_fallback_to_master() -> None:
    """_ensure_merged_to_main falls back to 'master' when 'main' doesn't exist."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "repo"
        repo.mkdir()
        # Init with 'master' instead of 'main'
        subprocess.run(["git", "init", "-b", "master"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)
        (repo / "README.md").write_text("init", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

        # Create an integration branch
        _git(repo, "checkout", "-b", "golem/test/integration")
        (repo / "feature.txt").write_text("new feature", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "integration work")
        _git(repo, "checkout", "master")

        # Should fallback to 'master' and merge successfully
        await _ensure_merged_to_main(repo)
        log = _git(repo, "log", "--oneline").stdout
        assert "integration work" in log


# ---------------------------------------------------------------------------
# Dead constant and edict_id propagation tests
# ---------------------------------------------------------------------------


def test_max_retries_constant_removed() -> None:
    """_MAX_RETRIES dead constant must not exist in tech_lead module."""
    import golem.tech_lead as tl_module
    assert not hasattr(tl_module, "_MAX_RETRIES"), "_MAX_RETRIES dead constant should be removed"


@pytest.mark.asyncio
async def test_run_tech_lead_passes_edict_id_to_recovery_coordinator() -> None:
    """run_tech_lead extracts edict_id from golem_dir path and passes it to RecoveryCoordinator."""
    from golem.config import GolemConfig
    from golem.tech_lead import run_tech_lead
    from golem.tickets import Ticket, TicketContext, TicketStore

    with tempfile.TemporaryDirectory() as tmpdir:
        # Simulate edict-style golem_dir: .golem/edicts/EDICT-042
        golem_dir = Path(tmpdir) / ".golem" / "edicts" / "EDICT-042"
        (golem_dir / "tickets").mkdir(parents=True)
        config = GolemConfig()

        store = TicketStore(golem_dir / "tickets")
        ticket_id = await store.create(
            Ticket(
                id="", type="task", title="TL Task", status="pending",
                priority="medium", created_by="planner", assigned_to="tech_lead",
                context=TicketContext(plan_file=""),
            )
        )

        from golem.supervisor import ContinuationResult, ToolCallRegistry
        ok = ContinuationResult(
            result_text="done", cost_usd=0.0, input_tokens=0, output_tokens=0,
            turns=5, duration_s=0.1, stalled=False, stall_turn=None,
            registry=ToolCallRegistry(), continuation_count=0, exhausted=False,
        )

        recorded_kwargs: list[dict[str, Any]] = []

        class _RecordingCoordinator:
            def __init__(self, _config: Any) -> None:
                pass

            async def run_with_recovery(self, session_fn: Any, **kwargs: Any) -> Any:
                recorded_kwargs.append(dict(kwargs))
                return ok

        with patch("golem.recovery.RecoveryCoordinator", _RecordingCoordinator), \
             patch("golem.tech_lead._check_integration_commits", AsyncMock(return_value=True)), \
             patch("golem.tech_lead._ensure_merged_to_main", AsyncMock()):
            await run_tech_lead(ticket_id, golem_dir, config, Path(tmpdir))

        assert len(recorded_kwargs) >= 1
        assert recorded_kwargs[0].get("edict_id") == "EDICT-042"
