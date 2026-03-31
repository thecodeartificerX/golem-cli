from __future__ import annotations

import subprocess
from pathlib import Path

from golem.config import GolemConfig
from golem.qa import QAResult, detect_infrastructure_checks, run_qa


def _run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=check)


def create_worktree(group_id: str, branch: str, base_branch: str, path: Path, repo_root: Path) -> None:
    """Create a git worktree for a group at `path` on a new `branch` from `base_branch`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Check if branch already exists
    result = _run(["git", "branch", "--list", branch], cwd=repo_root, check=False)
    if branch in result.stdout:
        _run(["git", "worktree", "add", str(path), branch], cwd=repo_root)
    else:
        _run(["git", "worktree", "add", "-b", branch, str(path), base_branch], cwd=repo_root)


def delete_worktree(path: Path, repo_root: Path) -> None:
    """Remove a git worktree."""
    _run(["git", "worktree", "remove", "--force", str(path)], cwd=repo_root, check=False)
    # Also prune dangling worktree entries
    _run(["git", "worktree", "prune"], cwd=repo_root, check=False)


def list_worktrees(repo_root: Path) -> list[str]:
    """Return list of worktree paths from `git worktree list`."""
    result = _run(["git", "worktree", "list", "--porcelain"], cwd=repo_root, check=False)
    worktrees: list[str] = []
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            worktrees.append(line[len("worktree "):])
    return worktrees


def commit_task(worktree_path: Path, task_id: str, description: str) -> bool:
    """Stage all changes and commit in worktree. Returns True if commit was made."""
    # Stage all
    _run(["git", "add", "-A"], cwd=worktree_path)
    # Check if there's anything to commit
    status = _run(["git", "status", "--porcelain"], cwd=worktree_path)
    if not status.stdout.strip():
        return False
    msg = f"golem: {task_id} — {description}"
    _run(["git", "commit", "-m", msg], cwd=worktree_path)
    return True


def merge_group_branches(group_branches: list[str], target_branch: str, repo_root: Path) -> tuple[bool, str]:
    """
    Merge each group branch into target_branch sequentially.
    Returns (success, conflict_info).
    Creates target_branch from HEAD if it doesn't exist.
    """
    # Ensure target branch exists
    result = _run(["git", "branch", "--list", target_branch], cwd=repo_root, check=False)
    if target_branch not in result.stdout:
        _run(["git", "checkout", "-b", target_branch], cwd=repo_root)
    else:
        _run(["git", "checkout", target_branch], cwd=repo_root)

    conflicts: list[str] = []
    for branch in group_branches:
        # Check branch exists
        check = _run(["git", "branch", "--list", branch], cwd=repo_root, check=False)
        if branch not in check.stdout:
            continue
        result = _run(["git", "merge", "--no-ff", "-m", f"golem: merge {branch}", branch], cwd=repo_root, check=False)
        if result.returncode != 0:
            conflicts.append(f"Conflict merging {branch}: {result.stderr}")
            # Abort the conflicting merge
            _run(["git", "merge", "--abort"], cwd=repo_root, check=False)

    if conflicts:
        return False, "\n".join(conflicts)
    return True, ""


def create_pr(branch: str, title: str, body: str, draft: bool, repo_root: Path, pr_target: str = "main") -> str:
    """Create a GitHub PR using gh CLI. Returns the PR URL."""
    cmd = ["gh", "pr", "create", "--title", title, "--body", body, "--base", pr_target, "--head", branch]
    if draft:
        cmd.append("--draft")
    result = _run(cmd, cwd=repo_root, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"gh pr create failed: {result.stderr}")
    return result.stdout.strip()


def run_post_merge_verification(
    project_root: Path,
    config: GolemConfig,
    merge_commit_sha: str,
) -> QAResult:
    """Run full QA on main after a merge. Revert if it fails."""
    infrastructure_checks = config.infrastructure_checks or detect_infrastructure_checks(project_root)
    result = run_qa(
        worktree_path=str(project_root),
        checks=[],
        infrastructure_checks=infrastructure_checks,
    )
    if not result.passed:
        subprocess.run(
            ["git", "revert", "--no-edit", merge_commit_sha],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
        )
    return result


def check_main_divergence(worktree_path: Path, base_branch: str = "main") -> bool:
    """Check if main has advanced since this worktree branched.

    Returns True if main has new commits beyond the merge base (i.e. diverged).
    """
    merge_base = subprocess.run(
        ["git", "merge-base", "HEAD", base_branch],
        cwd=str(worktree_path),
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    main_head = subprocess.run(
        ["git", "rev-parse", base_branch],
        cwd=str(worktree_path),
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    return merge_base != main_head


def rebase_onto_main(worktree_path: Path, base_branch: str = "main") -> bool:
    """Rebase current branch onto base_branch. Returns True on success, False on conflict."""
    result = subprocess.run(
        ["git", "rebase", base_branch],
        cwd=str(worktree_path),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        subprocess.run(
            ["git", "rebase", "--abort"],
            cwd=str(worktree_path),
            capture_output=True,
            encoding="utf-8",
        )
        return False
    return True
