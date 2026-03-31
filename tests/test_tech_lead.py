from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from golem.tech_lead import _cleanup_golem_worktrees, _ensure_merged_to_main
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


def test_tech_lead_prompt_includes_blocker_instructions() -> None:
    """Tech Lead prompt should contain blocker-checking instructions."""
    prompt_path = Path(__file__).parent.parent / "src" / "golem" / "prompts" / "tech_lead.md"
    content = prompt_path.read_text(encoding="utf-8")
    assert "type=blocker" in content
    assert "type=escalation" in content
    assert "assigned_to=operator" in content
    assert "Blocker Handling" in content
