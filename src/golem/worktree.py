from __future__ import annotations

import subprocess
from pathlib import Path


def _run(cmd: list[str], cwd: Path | None = None, check: bool = True, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=check, timeout=timeout)


def create_worktree(group_id: str, branch: str, base_branch: str, path: Path, repo_root: Path) -> None:
    """Create a git worktree for a group at `path` on a new `branch` from `base_branch`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Check if branch already exists
    result = _run(["git", "branch", "--list", branch], cwd=repo_root, check=False)
    try:
        if branch in result.stdout:
            _run(["git", "worktree", "add", str(path), branch], cwd=repo_root)
        else:
            _run(["git", "worktree", "add", "-b", branch, str(path), base_branch], cwd=repo_root)
    except subprocess.CalledProcessError:
        # Clean up empty directory left behind by mkdir
        if path.exists() and not any(path.iterdir()):
            path.rmdir()
        raise


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
    """Create a GitHub PR using gh CLI. Returns the PR URL.

    Requires `gh` (GitHub CLI) to be installed and authenticated.
    Raises RuntimeError if gh fails (e.g. not authenticated, repo not a GitHub remote).
    """
    cmd = ["gh", "pr", "create", "--title", title, "--body", body, "--base", pr_target, "--head", branch]
    if draft:
        cmd.append("--draft")
    result = _run(cmd, cwd=repo_root, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"gh pr create failed: {result.stderr}")
    return result.stdout.strip()
