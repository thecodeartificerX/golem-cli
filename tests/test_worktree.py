from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest

from golem.worktree import commit_task, create_pr, create_worktree, delete_worktree, list_worktrees, merge_group_branches


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


def test_merge_group_branches_clean() -> None:
    """merge_group_branches returns (True, '') when branches don't conflict."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "repo"
        repo.mkdir()
        _init_git_repo(repo)

        def _git(*args: str) -> None:
            subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

        # Two branches touching different files
        _git("checkout", "-b", "branch-x")
        (repo / "file_x.txt").write_text("content x", encoding="utf-8")
        _git("add", "-A")
        _git("commit", "-m", "add file_x")

        _git("checkout", "main")
        _git("checkout", "-b", "branch-y")
        (repo / "file_y.txt").write_text("content y", encoding="utf-8")
        _git("add", "-A")
        _git("commit", "-m", "add file_y")

        _git("checkout", "main")

        success, conflict_info = merge_group_branches(
            ["branch-x", "branch-y"], "integration", repo,
        )
        assert success is True
        assert conflict_info == ""


def test_merge_group_branches_skips_nonexistent() -> None:
    """merge_group_branches skips branches that don't exist."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "repo"
        repo.mkdir()
        _init_git_repo(repo)

        def _git(*args: str) -> None:
            subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

        # Create one real branch, reference one that doesn't exist
        _git("checkout", "-b", "branch-real")
        (repo / "real.txt").write_text("real", encoding="utf-8")
        _git("add", "-A")
        _git("commit", "-m", "real branch")
        _git("checkout", "main")

        success, conflict_info = merge_group_branches(
            ["branch-real", "branch-nonexistent"], "integration", repo,
        )
        # Should succeed — nonexistent branch is skipped
        assert success is True
        assert conflict_info == ""


def test_create_worktree_branch_already_exists() -> None:
    """create_worktree uses existing branch when it already exists (no -b flag)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "repo"
        repo.mkdir()
        _init_git_repo(repo)

        # Create branch first
        subprocess.run(["git", "branch", "golem/spec/existing"], cwd=repo, check=True, capture_output=True)

        wt_path = Path(tmpdir) / "worktrees" / "existing"
        create_worktree("existing", "golem/spec/existing", "main", wt_path, repo)

        # Worktree should exist and be on the correct branch
        worktrees = list_worktrees(repo)
        assert any("existing" in wt for wt in worktrees)

        # Check the worktree is on the expected branch
        result = subprocess.run(
            ["git", "branch", "--show-current"], cwd=wt_path,
            capture_output=True, text=True, check=True,
        )
        assert result.stdout.strip() == "golem/spec/existing"

        # Cleanup
        delete_worktree(wt_path, repo)


def test_create_pr_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """create_pr returns the PR URL on success."""
    from unittest.mock import MagicMock

    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = "https://github.com/owner/repo/pull/42\n"
    fake_result.stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: fake_result)

    url = create_pr(
        branch="feat/my-feature",
        title="My Feature",
        body="Description here",
        draft=False,
        repo_root=Path("."),
    )
    assert url == "https://github.com/owner/repo/pull/42"


def test_create_pr_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """create_pr raises RuntimeError when gh fails."""
    from unittest.mock import MagicMock

    fake_result = MagicMock()
    fake_result.returncode = 1
    fake_result.stdout = ""
    fake_result.stderr = "not authenticated"

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: fake_result)

    with pytest.raises(RuntimeError, match="gh pr create failed"):
        create_pr(
            branch="feat/broken",
            title="Broken",
            body="",
            draft=False,
            repo_root=Path("."),
        )


def test_create_pr_draft_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """create_pr passes --draft when draft=True."""
    from unittest.mock import MagicMock

    captured_cmd: list[str] = []

    def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
        captured_cmd.extend(cmd)
        result = MagicMock()
        result.returncode = 0
        result.stdout = "https://github.com/owner/repo/pull/99\n"
        result.stderr = ""
        return result

    monkeypatch.setattr(subprocess, "run", fake_run)

    url = create_pr(
        branch="feat/draft",
        title="Draft PR",
        body="WIP",
        draft=True,
        repo_root=Path("."),
    )
    assert url == "https://github.com/owner/repo/pull/99"
    assert "--draft" in captured_cmd
