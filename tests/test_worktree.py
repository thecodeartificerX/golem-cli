from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from golem.config import GolemConfig
from golem.worktree import (
    check_main_divergence,
    commit_task,
    create_pr,
    create_worktree,
    delete_worktree,
    detect_worktree_isolation,
    list_worktrees,
    merge_group_branches,
    rebase_onto_main,
    run_post_merge_verification,
    verify_pr,
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


def test_create_worktree_cleans_up_on_failure() -> None:
    """create_worktree cleans up empty parent dir if git worktree add fails."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "repo"
        repo.mkdir()
        _init_git_repo(repo)

        wt_path = Path(tmpdir) / "worktrees" / "bad-group"
        # Use a non-existent base branch to force failure
        with pytest.raises(subprocess.CalledProcessError):
            create_worktree("bad-group", "golem/spec/bad", "nonexistent-branch", wt_path, repo)

        # The empty directory should have been cleaned up
        assert not wt_path.exists()


def test_merge_group_branches_empty_list() -> None:
    """merge_group_branches with empty list returns (True, '')."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "repo"
        repo.mkdir()
        _init_git_repo(repo)

        success, conflict_info = merge_group_branches([], "integration", repo)
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


async def test_create_pr_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """create_pr returns the PR URL on success."""
    from unittest.mock import MagicMock

    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = "https://github.com/owner/repo/pull/42\n"
    fake_result.stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: fake_result)

    url = await create_pr(
        branch="feat/my-feature",
        title="My Feature",
        body="Description here",
        draft=False,
        repo_root=Path("."),
    )
    assert url == "https://github.com/owner/repo/pull/42"


async def test_create_pr_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """create_pr raises RuntimeError when gh fails."""
    from unittest.mock import MagicMock

    fake_result = MagicMock()
    fake_result.returncode = 1
    fake_result.stdout = ""
    fake_result.stderr = "not authenticated"

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: fake_result)

    with pytest.raises(RuntimeError, match="gh pr create failed"):
        await create_pr(
            branch="feat/broken",
            title="Broken",
            body="",
            draft=False,
            repo_root=Path("."),
        )


async def test_verify_pr_success() -> None:
    """verify_pr does not raise when gh pr view returns valid JSON."""
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = json.dumps({"state": "OPEN", "url": "https://github.com/o/r/pull/1", "number": 1})

    with patch("golem.worktree._run", return_value=mock_result):
        await verify_pr("https://github.com/o/r/pull/1", Path("/tmp"))  # Should not raise


async def test_verify_pr_not_found_retries() -> None:
    """verify_pr retries and succeeds when gh fails first then returns valid JSON."""
    fail_result = MagicMock()
    fail_result.returncode = 1
    fail_result.stderr = "could not resolve to a PullRequest"

    ok_result = MagicMock()
    ok_result.returncode = 0
    ok_result.stdout = json.dumps({"state": "OPEN", "url": "https://github.com/o/r/pull/5", "number": 5})

    with patch("golem.worktree._run", side_effect=[fail_result, fail_result, ok_result]), \
         patch("asyncio.sleep", new=AsyncMock()):
        await verify_pr("https://github.com/o/r/pull/5", Path("/tmp"), poll_attempts=6, poll_interval=0)


async def test_verify_pr_not_found_all_retries() -> None:
    """verify_pr raises RuntimeError after all poll_attempts fail."""
    fail_result = MagicMock()
    fail_result.returncode = 1
    fail_result.stderr = "could not resolve to a PullRequest"

    with patch("golem.worktree._run", return_value=fail_result), \
         patch("asyncio.sleep", new=AsyncMock()):
        with pytest.raises(RuntimeError, match="PR verification failed"):
            await verify_pr("https://github.com/o/r/pull/7", Path("/tmp"), poll_attempts=3, poll_interval=0)


async def test_verify_pr_invalid_url() -> None:
    """verify_pr raises RuntimeError when URL has no /pull/NNN segment."""
    with pytest.raises(RuntimeError, match="Could not extract PR number"):
        await verify_pr("https://github.com/o/r/issues/42", Path("/tmp"))


async def test_verify_pr_gh_auth_error() -> None:
    """verify_pr raises RuntimeError after all attempts with an auth error."""
    auth_fail = MagicMock()
    auth_fail.returncode = 1
    auth_fail.stderr = "authentication required: run gh auth login"

    with patch("golem.worktree._run", return_value=auth_fail), \
         patch("asyncio.sleep", new=AsyncMock()):
        with pytest.raises(RuntimeError, match="gh pr view failed after"):
            await verify_pr("https://github.com/o/r/pull/3", Path("/tmp"), poll_attempts=2, poll_interval=0)


async def test_create_pr_draft_flag(monkeypatch: pytest.MonkeyPatch) -> None:
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

    url = await create_pr(
        branch="feat/draft",
        title="Draft PR",
        body="WIP",
        draft=True,
        repo_root=Path("."),
    )
    assert url == "https://github.com/owner/repo/pull/99"
    assert "--draft" in captured_cmd


def test_create_worktree_with_branch_prefix() -> None:
    """create_worktree with branch_prefix creates branch using the prefix."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "repo"
        repo.mkdir()
        _init_git_repo(repo)

        wt_path = Path(tmpdir) / "worktrees" / "group-a"
        create_worktree(
            "group-a",
            "golem/session-1/group-a",
            "main",
            wt_path,
            repo,
            branch_prefix="golem/session-1",
        )

        worktrees = list_worktrees(repo)
        assert any("group-a" in wt for wt in worktrees)

        # Verify the branch name uses the prefix
        result = subprocess.run(
            ["git", "branch", "--list", "golem/session-1/group-a"],
            cwd=repo, capture_output=True, text=True, check=True,
        )
        assert "golem/session-1/group-a" in result.stdout

        delete_worktree(wt_path, repo)


def test_create_worktree_default_prefix() -> None:
    """create_worktree without branch_prefix still works (backward compat)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "repo"
        repo.mkdir()
        _init_git_repo(repo)

        wt_path = Path(tmpdir) / "worktrees" / "group-b"
        # No branch_prefix argument — should still work
        create_worktree("group-b", "golem/spec/group-b", "main", wt_path, repo)

        worktrees = list_worktrees(repo)
        assert any("group-b" in wt for wt in worktrees)
        delete_worktree(wt_path, repo)


# ---------------------------------------------------------------------------
# detect_worktree_isolation tests
# ---------------------------------------------------------------------------


def test_detect_worktree_isolation_regular_repo(tmp_path: Path) -> None:
    """detect_worktree_isolation returns (False, None) for a plain git repo."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    is_worktree, parent_path = detect_worktree_isolation(repo)
    assert is_worktree is False
    assert parent_path is None


def test_detect_worktree_isolation_in_worktree(tmp_path: Path) -> None:
    """detect_worktree_isolation returns (True, parent_path) for a real git worktree."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    wt_path = tmp_path / "worktrees" / "test-wt"
    create_worktree("test-wt", "golem/spec/test-wt", "main", wt_path, repo)

    try:
        is_worktree, parent_path = detect_worktree_isolation(wt_path)
        assert is_worktree is True
        assert parent_path is not None
        # Parent path should resolve to the repo root
        assert Path(parent_path).resolve() == repo.resolve()
    finally:
        delete_worktree(wt_path, repo)


def test_detect_worktree_isolation_nonexistent_path(tmp_path: Path) -> None:
    """detect_worktree_isolation returns (False, None) for a path with no .git marker."""
    non_git = tmp_path / "not_a_repo"
    non_git.mkdir()

    is_worktree, parent_path = detect_worktree_isolation(non_git)
    assert is_worktree is False
    assert parent_path is None


def test_detect_worktree_isolation_git_dir_not_file(tmp_path: Path) -> None:
    """detect_worktree_isolation returns (False, None) when .git is a directory (normal repo)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    # .git is a directory in a normal repo — should not be detected as worktree
    is_worktree, parent_path = detect_worktree_isolation(repo)
    assert is_worktree is False
    assert parent_path is None


def test_detect_worktree_isolation_malformed_git_file(tmp_path: Path) -> None:
    """detect_worktree_isolation returns (False, None) when .git file has unexpected content."""
    fake_wt = tmp_path / "fake-wt"
    fake_wt.mkdir()
    # Write a .git file without the "gitdir:" prefix
    (fake_wt / ".git").write_text("not a gitdir reference\n", encoding="utf-8")

    is_worktree, parent_path = detect_worktree_isolation(fake_wt)
    assert is_worktree is False
    assert parent_path is None


def test_build_worktree_isolation_warning_in_worktree(tmp_path: Path) -> None:
    """_build_worktree_isolation_warning returns warning text inside a real worktree."""
    from golem.writer import _build_worktree_isolation_warning

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    wt_path = tmp_path / "worktrees" / "warn-wt"
    create_worktree("warn-wt", "golem/spec/warn-wt", "main", wt_path, repo)

    try:
        warning = _build_worktree_isolation_warning(wt_path)
        assert "ISOLATED WORKTREE" in warning
        assert str(wt_path) in warning
        assert str(repo.resolve()) in warning
        assert "NEVER" in warning
        assert "parent repository" in warning
    finally:
        delete_worktree(wt_path, repo)


def test_build_worktree_isolation_warning_regular_repo(tmp_path: Path) -> None:
    """_build_worktree_isolation_warning returns empty string for a plain repo."""
    from golem.writer import _build_worktree_isolation_warning

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    warning = _build_worktree_isolation_warning(repo)
    assert warning == ""


def test_build_writer_prompt_injects_isolation_warning(tmp_path: Path) -> None:
    """build_writer_prompt injects isolation warning when worktree_path is a git worktree."""
    from golem.tickets import TicketContext
    from golem.writer import build_writer_prompt

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    wt_path = tmp_path / "worktrees" / "prompt-wt"
    create_worktree("prompt-wt", "golem/spec/prompt-wt", "main", wt_path, repo)

    try:
        ctx = TicketContext(blueprint="test", acceptance=["done"], qa_checks=["true"])
        from golem.tickets import Ticket
        ticket = Ticket(
            id="TICKET-WRN-001",
            type="task",
            title="Warning injection test",
            status="pending",
            priority="medium",
            created_by="planner",
            assigned_to="writer",
            context=ctx,
        )

        prompt = build_writer_prompt(ticket, worktree_path=wt_path)
        assert "ISOLATED WORKTREE" in prompt
        assert str(wt_path) in prompt
        assert "{worktree_isolation_warning}" not in prompt
    finally:
        delete_worktree(wt_path, repo)


def test_build_writer_prompt_no_warning_outside_worktree(tmp_path: Path) -> None:
    """build_writer_prompt does not inject warning when worktree_path is a plain repo."""
    from golem.tickets import Ticket, TicketContext
    from golem.writer import build_writer_prompt

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    ctx = TicketContext(blueprint="test", acceptance=["done"], qa_checks=["true"])
    ticket = Ticket(
        id="TICKET-WRN-002",
        type="task",
        title="No warning test",
        status="pending",
        priority="medium",
        created_by="planner",
        assigned_to="writer",
        context=ctx,
    )

    prompt = build_writer_prompt(ticket, worktree_path=repo)
    assert "ISOLATED WORKTREE" not in prompt
    assert "{worktree_isolation_warning}" not in prompt


def test_build_writer_prompt_no_warning_when_path_is_none() -> None:
    """build_writer_prompt does not inject warning when worktree_path is None."""
    from golem.tickets import Ticket, TicketContext
    from golem.writer import build_writer_prompt

    ctx = TicketContext(blueprint="test", acceptance=["done"], qa_checks=["true"])
    ticket = Ticket(
        id="TICKET-WRN-003",
        type="task",
        title="No path test",
        status="pending",
        priority="medium",
        created_by="planner",
        assigned_to="writer",
        context=ctx,
    )

    prompt = build_writer_prompt(ticket, worktree_path=None)
    assert "ISOLATED WORKTREE" not in prompt
    assert "{worktree_isolation_warning}" not in prompt


# ---------------------------------------------------------------------------
# Unit 10: Post-merge verification + conflict detection tests
# ---------------------------------------------------------------------------


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
