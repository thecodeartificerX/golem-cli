from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest

from golem.config import GolemConfig
from golem.worktree import (
    check_main_divergence,
    commit_task,
    create_worktree,
    delete_worktree,
    list_worktrees,
    merge_group_branches,
    rebase_onto_main,
    run_post_merge_verification,
)


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


def test_check_main_divergence_no_divergence() -> None:
    """check_main_divergence returns False when main has not advanced."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "repo"
        repo.mkdir()
        _init_git_repo(repo)

        def _git(*args: str) -> None:
            subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

        # Create a feature branch from main — main hasn't moved
        _git("checkout", "-b", "feature")
        (repo / "feature.txt").write_text("feature work", encoding="utf-8")
        _git("add", "-A")
        _git("commit", "-m", "feature commit")

        # From the feature branch, main hasn't diverged
        assert check_main_divergence(repo, base_branch="main") is False


def test_check_main_divergence_with_divergence() -> None:
    """check_main_divergence returns True when main has new commits."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "repo"
        repo.mkdir()
        _init_git_repo(repo)

        def _git(*args: str) -> None:
            subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

        # Create a feature branch
        _git("checkout", "-b", "feature")
        (repo / "feature.txt").write_text("feature work", encoding="utf-8")
        _git("add", "-A")
        _git("commit", "-m", "feature commit")

        # Go back to main and add a new commit
        _git("checkout", "main")
        (repo / "main_update.txt").write_text("main moved forward", encoding="utf-8")
        _git("add", "-A")
        _git("commit", "-m", "main advanced")

        # Switch back to feature — main has diverged
        _git("checkout", "feature")
        assert check_main_divergence(repo, base_branch="main") is True


def test_rebase_onto_main_clean() -> None:
    """rebase_onto_main succeeds on a clean rebase (no conflicts)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "repo"
        repo.mkdir()
        _init_git_repo(repo)

        def _git(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["git", *args], cwd=repo, check=True, capture_output=True, text=True, encoding="utf-8",
            )

        # Create a feature branch touching a different file
        _git("checkout", "-b", "feature")
        (repo / "feature.txt").write_text("feature work", encoding="utf-8")
        _git("add", "-A")
        _git("commit", "-m", "feature commit")

        # Advance main with a non-conflicting change
        _git("checkout", "main")
        (repo / "main_update.txt").write_text("main moved forward", encoding="utf-8")
        _git("add", "-A")
        _git("commit", "-m", "main advanced")

        # Switch to feature and rebase
        _git("checkout", "feature")
        assert rebase_onto_main(repo, base_branch="main") is True

        # Verify rebase applied — feature.txt should still exist and main_update.txt accessible
        log = _git("log", "--oneline").stdout
        assert "feature commit" in log


def test_rebase_onto_main_conflict() -> None:
    """rebase_onto_main aborts and returns False when there are conflicts."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "repo"
        repo.mkdir()
        _init_git_repo(repo)

        def _git(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["git", *args], cwd=repo, check=True, capture_output=True, text=True, encoding="utf-8",
            )

        # Create a feature branch that modifies README.md
        _git("checkout", "-b", "feature")
        (repo / "README.md").write_text("feature version", encoding="utf-8")
        _git("add", "-A")
        _git("commit", "-m", "feature changes README")

        # Advance main with a conflicting change to README.md
        _git("checkout", "main")
        (repo / "README.md").write_text("main version", encoding="utf-8")
        _git("add", "-A")
        _git("commit", "-m", "main changes README")

        # Switch to feature and try rebase — should fail
        _git("checkout", "feature")
        assert rebase_onto_main(repo, base_branch="main") is False

        # Verify rebase was aborted — we're still on feature branch, no rebase in progress
        branch = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        assert branch == "feature"


def test_run_post_merge_verification_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """run_post_merge_verification returns passed QAResult when QA passes."""
    from golem.qa import QAResult as QAR

    passing_result = QAR(passed=True, checks=[], summary="1/1 checks passed.")

    monkeypatch.setattr("golem.worktree.run_qa", lambda worktree_path, checks, infrastructure_checks: passing_result)
    monkeypatch.setattr("golem.worktree.detect_infrastructure_checks", lambda project_root: [])

    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "repo"
        repo.mkdir()
        _init_git_repo(repo)

        config = GolemConfig()
        result = run_post_merge_verification(repo, config, "abc123")
        assert result.passed is True


def test_run_post_merge_verification_fails_and_reverts(monkeypatch: pytest.MonkeyPatch) -> None:
    """run_post_merge_verification reverts the merge commit when QA fails."""
    from golem.qa import QAResult as QAR

    failing_result = QAR(passed=False, checks=[], summary="0/1 checks passed.")

    monkeypatch.setattr("golem.worktree.run_qa", lambda worktree_path, checks, infrastructure_checks: failing_result)
    monkeypatch.setattr("golem.worktree.detect_infrastructure_checks", lambda project_root: [])

    revert_calls: list[list[str]] = []
    original_run = subprocess.run

    def mock_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if cmd[0] == "git" and "revert" in cmd:
            revert_calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return original_run(cmd, **kwargs)

    monkeypatch.setattr("golem.worktree.subprocess.run", mock_run)

    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "repo"
        repo.mkdir()
        _init_git_repo(repo)

        config = GolemConfig()
        result = run_post_merge_verification(repo, config, "abc123def")
        assert result.passed is False
        # Verify git revert was called with the merge sha
        assert len(revert_calls) == 1
        assert "abc123def" in revert_calls[0]
