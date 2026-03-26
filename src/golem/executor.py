from __future__ import annotations

import asyncio
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from golem.config import GolemConfig
from golem.progress import ProgressLogger
from golem.tasks import Group, Task, TasksFile, write_tasks
from golem.validator import run_infrastructure_checks, run_integration_reviewer, run_validation
from golem.worker import run_worker
from golem.worktree import commit_task, create_worktree, delete_worktree


def _run_autofix(worktree_path: str, infrastructure_checks: list[str]) -> None:
    """Run known autofix commands when infrastructure checks fail."""
    check_str = " ".join(infrastructure_checks)
    if "ruff" in check_str:
        subprocess.run(["ruff", "check", "--fix", "."], cwd=worktree_path, capture_output=True, check=False)
        subprocess.run(["ruff", "format", "."], cwd=worktree_path, capture_output=True, check=False)
    if "prettier" in check_str:
        subprocess.run(["npx", "prettier", "--write", "."], cwd=worktree_path, capture_output=True, check=False)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _deps_satisfied(task: Task, group: Group) -> bool:
    """Check all depends_on task IDs are completed within the group."""
    task_status = {t.id: t.status for t in group.tasks}
    return all(task_status.get(dep) == "completed" for dep in task.depends_on)


async def execute_group(
    group: Group,
    worktree_path: Path,
    tasks_path: Path,
    tasks_file: TasksFile,
    config: GolemConfig,
    progress: ProgressLogger,
    dashboard_cb: Callable[[str, str, str, str], None] | None = None,
) -> None:
    """Execute all tasks in a group sequentially, respecting depends_on."""
    for task in group.tasks:
        if task.status == "completed":
            continue

        # Check dependencies
        if not _deps_satisfied(task, group):
            task.status = "blocked"
            task.blocked_reason = "Dependency not satisfied"
            await write_tasks(tasks_file, tasks_path)
            progress.log_task_blocked(task.id, "Dependency not satisfied")
            continue

        task.status = "in_progress"
        await write_tasks(tasks_file, tasks_path)
        progress.log_task_start(task.id)

        if dashboard_cb:
            dashboard_cb(group.id, task.id, "in_progress", task.description[:60])

        passed = False
        for attempt in range(1, config.max_retries + 1):
            feedback = task.last_feedback if attempt > 1 else None

            # Worker
            await run_worker(
                task=task,
                worktree_path=str(worktree_path),
                feedback=feedback,
                config=config,
                blueprint=tasks_file.blueprint,
                dashboard_cb=lambda tid, text: (dashboard_cb(group.id, tid, "running", text[:60]) if dashboard_cb else None),
            )

            # Tier 1a: Infrastructure checks (always-on, agent cannot skip)
            infra_passed, infra_feedback = run_infrastructure_checks(
                config.infrastructure_checks, str(worktree_path)
            )
            if not infra_passed:
                # Attempt autofix before counting as a retry
                _run_autofix(str(worktree_path), config.infrastructure_checks)
                infra_passed, infra_feedback = run_infrastructure_checks(
                    config.infrastructure_checks, str(worktree_path)
                )
            if not infra_passed:
                task.retries = attempt
                task.last_feedback = infra_feedback
                await write_tasks(tasks_file, tasks_path)
                progress.log_task_retry(task.id, attempt, infra_feedback)
                continue

            # Tier 1b: Task validation commands + AI validator
            val_passed, val_feedback = await run_validation(
                task, str(worktree_path), config, blueprint=tasks_file.blueprint
            )
            if val_passed:
                passed = True
                break
            else:
                task.retries = attempt
                task.last_feedback = val_feedback
                await write_tasks(tasks_file, tasks_path)
                progress.log_task_retry(task.id, attempt, val_feedback)

        if passed:
            task.status = "completed"
            task.completed_at = _now_iso()
            await write_tasks(tasks_file, tasks_path)
            commit_task(worktree_path, task.id, task.description[:80])
            progress.log_task_complete(task.id)
            if dashboard_cb:
                dashboard_cb(group.id, task.id, "completed", task.description[:60])
        else:
            task.status = "blocked"
            task.blocked_reason = f"Failed after {config.max_retries} attempts. Last: {task.last_feedback}"
            await write_tasks(tasks_file, tasks_path)
            progress.log_task_blocked(task.id, task.blocked_reason or "")
            if dashboard_cb:
                dashboard_cb(group.id, task.id, "blocked", task.description[:60])

    progress.log_group_complete(group.id)


async def execute_all_groups(
    tasks_file: TasksFile,
    golem_dir: Path,
    repo_root: Path,
    config: GolemConfig,
    progress: ProgressLogger,
    dashboard_cb: Callable[[str, str, str, str], None] | None = None,
) -> None:
    """Create worktrees and run all groups concurrently via asyncio.gather."""
    tasks_path = golem_dir / "tasks.json"
    worktrees_dir = golem_dir / "worktrees"
    worktrees_dir.mkdir(parents=True, exist_ok=True)

    # Determine base branch
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo_root, capture_output=True, text=True, check=False,
    )
    base_branch = result.stdout.strip() or "main"

    # Create worktrees for all groups
    group_worktree_paths: dict[str, Path] = {}
    for group in tasks_file.groups:
        wt_path = worktrees_dir / group.id
        group_worktree_paths[group.id] = wt_path
        if not wt_path.exists():
            create_worktree(group.id, group.worktree_branch, base_branch, wt_path, repo_root)
            # Install package in worktree so validation commands can import modules
            subprocess.run(
                ["uv", "sync"], cwd=wt_path, capture_output=True, text=True, check=False,
            )

    # Run all groups concurrently
    coroutines = [
        execute_group(
            group=group,
            worktree_path=group_worktree_paths[group.id],
            tasks_path=tasks_path,
            tasks_file=tasks_file,
            config=config,
            progress=progress,
            dashboard_cb=dashboard_cb,
        )
        for group in tasks_file.groups
    ]
    await asyncio.gather(*coroutines)


def run_final_validation(
    tasks_file: TasksFile,
    merged_branch_path: Path,
    infrastructure_checks: list[str] | None = None,
) -> tuple[bool, list[str]]:
    """Run infrastructure checks then final_validation.commands on the merged branch. Returns (passed, results)."""
    results: list[str] = []
    all_passed = True

    # Tier 3a: infrastructure checks (always-on)
    for cmd in infrastructure_checks or []:
        result = subprocess.run(
            cmd, shell=True, cwd=merged_branch_path,
            capture_output=True, text=True, encoding="utf-8",
        )
        marker = "PASS" if result.returncode == 0 else "FAIL"
        results.append(f"[infra] {marker}: {cmd}")
        if result.returncode != 0:
            results.append(f"  {result.stderr.strip()}")
            all_passed = False

    # Tier 3b: spec-defined final_validation commands
    for cmd in tasks_file.final_validation.commands:
        result = subprocess.run(
            cmd, shell=True, cwd=merged_branch_path,
            capture_output=True, text=True, encoding="utf-8",
        )
        marker = "PASS" if result.returncode == 0 else "FAIL"
        results.append(f"[spec] {marker}: {cmd}")
        if result.returncode != 0:
            results.append(f"  {result.stderr.strip()}")
            all_passed = False

    return all_passed, results


async def run_integration_review(
    tasks_file: TasksFile,
    merged_path: Path,
    spec_content: str,
    config: GolemConfig,
    progress: "ProgressLogger",
) -> tuple[bool, str]:
    """Post-merge integration review (Tier 2): infra checks + AI reviewer."""
    # Infrastructure checks on merged code
    infra_passed, infra_feedback = run_infrastructure_checks(
        config.infrastructure_checks, str(merged_path)
    )
    if not infra_passed:
        return False, f"Infrastructure checks failed on merged code:\n{infra_feedback}"

    # AI integration reviewer
    return await run_integration_reviewer(
        tasks_file=tasks_file,
        merged_path=str(merged_path),
        spec_content=spec_content,
        config=config,
    )
