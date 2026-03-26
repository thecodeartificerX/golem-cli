from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest

from golem.worktree import commit_task, create_worktree, delete_worktree, list_worktrees, merge_group_branches


def _init_git_repo(path: Path) -> None:
    """Initialize a git repo with an initial commit."""
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True, capture_output=True)
    (path / "README.md").write_text("init")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)


def test_create_and_delete_worktree() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "repo"
        repo.mkdir()
        _init_git_repo(repo)

        wt_path = Path(tmpdir) / "worktrees" / "group-a"
        create_worktree("group-a", "golem/spec/group-a", "main", wt_path, repo)

        worktrees = list_worktrees(repo)
        assert any("group-a" in wt for wt in worktrees)

        delete_worktree(wt_path, repo)

        worktrees_after = list_worktrees(repo)
        assert not any("group-a" in wt for wt in worktrees_after)


def test_commit_task() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "repo"
        repo.mkdir()
        _init_git_repo(repo)

        wt_path = Path(tmpdir) / "worktrees" / "group-b"
        create_worktree("group-b", "golem/spec/group-b", "main", wt_path, repo)

        # Create a file in the worktree
        (wt_path / "new_file.py").write_text("# hello")

        result = commit_task(wt_path, "task-001", "Create new_file.py")
        assert result is True

        # Verify commit exists
        log = subprocess.run(
            ["git", "log", "--oneline", "-1"],
            cwd=wt_path, capture_output=True, text=True, check=True,
        )
        assert "task-001" in log.stdout


def test_commit_task_no_changes() -> None:
    """commit_task returns False when there's nothing to commit."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "repo"
        repo.mkdir()
        _init_git_repo(repo)

        wt_path = Path(tmpdir) / "worktrees" / "group-c"
        create_worktree("group-c", "golem/spec/group-c", "main", wt_path, repo)

        result = commit_task(wt_path, "task-001", "Nothing changed")
        assert result is False


def test_list_worktrees_includes_main() -> None:
    """list_worktrees always returns at least the main worktree."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "repo"
        repo.mkdir()
        _init_git_repo(repo)

        worktrees = list_worktrees(repo)
        assert len(worktrees) >= 1
        repo_resolved = str(repo.resolve())
        assert any(repo_resolved in str(Path(wt).resolve()) for wt in worktrees)


def test_merge_group_branches_conflict() -> None:
    """merge_group_branches returns (False, conflict_info) on conflicts."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "repo"
        repo.mkdir()
        _init_git_repo(repo)

        # Create two branches that both modify the same file
        def _git(*args: str) -> None:
            subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

        _git("checkout", "-b", "branch-a")
        (repo / "README.md").write_text("branch A content", encoding="utf-8")
        _git("add", "-A")
        _git("commit", "-m", "branch-a change")

        _git("checkout", "main")
        _git("checkout", "-b", "branch-b")
        (repo / "README.md").write_text("branch B content", encoding="utf-8")
        _git("add", "-A")
        _git("commit", "-m", "branch-b change")

        _git("checkout", "main")

        # First merge succeeds, second conflicts
        success, conflict_info = merge_group_branches(
            ["branch-a", "branch-b"], "integration", repo,
        )
        assert success is False
        assert "branch-b" in conflict_info
