from __future__ import annotations

import asyncio
import json
import re
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

    # Auto-install dependencies in the new worktree
    _post_create_install(path)


def _post_create_install(worktree_path: Path) -> None:
    """Run dependency installation in a new worktree if project files are detected."""
    import os

    # Clear inherited VIRTUAL_ENV to avoid uv conflicts
    env = os.environ.copy()
    env.pop("VIRTUAL_ENV", None)

    if (worktree_path / "pyproject.toml").exists():
        subprocess.run(
            ["uv", "sync"], cwd=worktree_path, capture_output=True, env=env, timeout=120,
        )
    elif (worktree_path / "package.json").exists():
        # Prefer bun if available, fallback to npm
        bun_result = subprocess.run(["bun", "--version"], capture_output=True)
        if bun_result.returncode == 0:
            subprocess.run(
                ["bun", "install"], cwd=worktree_path, capture_output=True, timeout=120,
            )
        else:
            subprocess.run(
                ["npm", "install"], cwd=worktree_path, capture_output=True, timeout=120,
            )


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


async def create_pr(branch: str, title: str, body: str, draft: bool, repo_root: Path, pr_target: str = "main") -> str:
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
    pr_url = result.stdout.strip()

    # Verify PR exists (GitHub API eventual consistency)
    await verify_pr(pr_url, repo_root)

    return pr_url


async def verify_pr(pr_url: str, repo_root: Path, poll_attempts: int = 6, poll_interval: float = 5.0) -> None:
    """Verify a PR exists on GitHub by polling gh pr view.

    Raises RuntimeError if the PR cannot be verified after all attempts.
    GitHub's API is eventually consistent — a successful gh pr create does
    not guarantee the PR is immediately queryable.
    """
    match = re.search(r"/pull/(\d+)", pr_url)
    if not match:
        raise RuntimeError(f"Could not extract PR number from URL: {pr_url}")
    pr_number = match.group(1)

    for attempt in range(poll_attempts):
        result = _run(
            ["gh", "pr", "view", pr_number, "--json", "state,url,number"],
            cwd=repo_root, check=False,
        )
        if result.returncode == 0:
            return  # PR confirmed to exist

        stderr = result.stderr.lower()
        if "could not resolve" in stderr or "no pull requests" in stderr:
            if attempt < poll_attempts - 1:
                await asyncio.sleep(poll_interval)
                continue
            raise RuntimeError(
                f"PR verification failed: {pr_url} does not exist on GitHub "
                f"after {poll_attempts} attempts ({poll_attempts * poll_interval}s)"
            )

        # Other gh errors (auth, network)
        if attempt < poll_attempts - 1:
            await asyncio.sleep(poll_interval)
            continue
        raise RuntimeError(f"gh pr view failed after {poll_attempts} attempts: {result.stderr}")
