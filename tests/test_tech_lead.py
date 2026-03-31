from __future__ import annotations

import asyncio
import subprocess
import tempfile
from pathlib import Path

from golem.tech_lead import (
    _cleanup_golem_worktrees,
    _ensure_merged_to_main,
    _promote_debrief_to_memory,
    _promote_gotchas_to_memory,
)
from golem.worktree import create_worktree


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


def test_ensure_merged_noop_no_branches() -> None:
    """No golem integration branches — should be a noop."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "repo"
        repo.mkdir()
        _init_repo(repo)
        # Should not raise or change anything
        _ensure_merged_to_main(repo)
        log = _git(repo, "log", "--oneline").stdout
        assert "init" in log


def test_ensure_merged_already_merged() -> None:
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
        _ensure_merged_to_main(repo)
        log = _git(repo, "log", "--oneline").stdout
        assert "integration work" in log


def test_ensure_merged_merges_unmerged_branch() -> None:
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
        _ensure_merged_to_main(repo)
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


def test_promote_debrief_to_memory_copies_file(tmp_path: Path) -> None:
    """Debrief file is copied to .golem/memory/debriefs/<edict_id>.md."""
    golem_dir = tmp_path / ".golem"
    golem_dir.mkdir()
    project_root = tmp_path

    debrief_content = "# Debrief\n\nTickets completed: 3\n"
    (golem_dir / "debrief.md").write_text(debrief_content, encoding="utf-8")

    asyncio.run(_promote_debrief_to_memory(golem_dir, project_root, "EDICT-001"))

    dest = project_root / ".golem" / "memory" / "debriefs" / "EDICT-001.md"
    assert dest.exists()
    assert dest.read_text(encoding="utf-8") == debrief_content


def test_promote_debrief_to_memory_noop_when_missing(tmp_path: Path) -> None:
    """No debrief file -- should be a noop, no crash."""
    golem_dir = tmp_path / ".golem"
    golem_dir.mkdir()
    project_root = tmp_path

    asyncio.run(_promote_debrief_to_memory(golem_dir, project_root, "EDICT-002"))

    dest = project_root / ".golem" / "memory" / "debriefs" / "EDICT-002.md"
    assert not dest.exists()


def test_promote_gotchas_creates_new_file(tmp_path: Path) -> None:
    """Gotchas file is created in project-level memory when none exists."""
    golem_dir = tmp_path / ".golem"
    golem_dir.mkdir()
    project_root = tmp_path

    gotchas_content = "# Gotchas\n\n- ruff needs explicit config\n"
    (golem_dir / "gotchas.md").write_text(gotchas_content, encoding="utf-8")

    asyncio.run(_promote_gotchas_to_memory(golem_dir, project_root))

    dest = project_root / ".golem" / "memory" / "gotchas.md"
    assert dest.exists()
    assert dest.read_text(encoding="utf-8") == gotchas_content


def test_promote_gotchas_appends_to_existing(tmp_path: Path) -> None:
    """Gotchas are appended (not overwritten) to existing project-level memory."""
    golem_dir = tmp_path / ".golem"
    golem_dir.mkdir()
    project_root = tmp_path

    # Pre-existing gotchas in memory
    memory_dir = project_root / ".golem" / "memory"
    memory_dir.mkdir(parents=True)
    existing_content = "# Gotchas\n\n- Windows encoding issues\n"
    (memory_dir / "gotchas.md").write_text(existing_content, encoding="utf-8")

    # New gotchas from this edict
    new_content = "- pytest tmp_path is required\n"
    (golem_dir / "gotchas.md").write_text(new_content, encoding="utf-8")

    asyncio.run(_promote_gotchas_to_memory(golem_dir, project_root))

    dest = memory_dir / "gotchas.md"
    result = dest.read_text(encoding="utf-8")
    # Both old and new content should be present
    assert "Windows encoding issues" in result
    assert "pytest tmp_path is required" in result
    # Existing content should come first (append, not prepend)
    assert result.index("Windows encoding issues") < result.index("pytest tmp_path is required")


def test_promote_gotchas_noop_when_missing(tmp_path: Path) -> None:
    """No gotchas file -- should be a noop, no crash."""
    golem_dir = tmp_path / ".golem"
    golem_dir.mkdir()
    project_root = tmp_path

    asyncio.run(_promote_gotchas_to_memory(golem_dir, project_root))

    dest = project_root / ".golem" / "memory" / "gotchas.md"
    assert not dest.exists()


def test_tech_lead_prompt_includes_phase_9() -> None:
    """Tech Lead prompt template contains Phase 9 -- Post-Edict Debrief."""
    prompt_path = Path(__file__).parent.parent / "src" / "golem" / "prompts" / "tech_lead.md"
    content = prompt_path.read_text(encoding="utf-8")
    assert "Phase 9" in content
    assert "Post-Edict Debrief" in content
    assert "debrief.md" in content
    assert "What was delivered" in content
    assert "What broke" in content
    assert "Lessons learned" in content
    assert "Recommendations" in content
