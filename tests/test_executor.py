from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from golem.config import GolemConfig
from golem.executor import execute_group
from golem.progress import ProgressLogger
from golem.tasks import FinalValidation, Group, Task, TasksFile, write_tasks


def make_task(task_id: str, status: str = "pending", depends_on: list[str] | None = None) -> Task:
    return Task(
        id=task_id,
        description=f"Task {task_id}",
        files_create=[],
        files_modify=[],
        depends_on=depends_on or [],
        acceptance=["it works"],
        validation_commands=["true"],
        reference_docs=[],
        status=status,  # type: ignore[arg-type]
        retries=0,
        last_feedback=None,
        blocked_reason=None,
        completed_at=None,
    )


def make_tasks_file(groups: list[Group]) -> TasksFile:
    return TasksFile(
        spec="spec.md",
        created="2026-03-25T00:00:00Z",
        project="test",
        branch="golem/spec",
        models={},
        config={},
        groups=groups,
        final_validation=FinalValidation(depends_on_all=True, commands=[]),
    )


async def test_dependency_ordering() -> None:
    """task-002 with depends_on=[task-001] must wait for task-001 to complete."""
    task1 = make_task("task-001")
    task2 = make_task("task-002", depends_on=["task-001"])
    group = Group(id="g", description="", worktree_branch="golem/spec/g", tasks=[task1, task2])
    tf = make_tasks_file([group])

    with tempfile.TemporaryDirectory() as tmpdir:
        tasks_path = Path(tmpdir) / "tasks.json"
        await write_tasks(tf, tasks_path)
        progress = ProgressLogger(Path(tmpdir))
        config = GolemConfig(max_retries=1)

        with (
            patch("golem.executor.run_worker", new_callable=AsyncMock, return_value="done"),
            patch("golem.executor.run_validation", new_callable=AsyncMock, return_value=(True, "PASS")),
            patch("golem.executor.commit_task", return_value=True),
        ):
            await execute_group(group, Path(tmpdir) / "wt", tasks_path, tf, config, progress)

    assert task1.status == "completed"
    assert task2.status == "completed"


async def test_retry_loop_pass_on_third() -> None:
    """Task that fails validation twice then passes on 3rd attempt."""
    task = make_task("task-001")
    group = Group(id="g", description="", worktree_branch="golem/spec/g", tasks=[task])
    tf = make_tasks_file([group])

    call_count = 0

    async def _mock_validate(t: Task, wt: str, cfg: GolemConfig, **kwargs: object) -> tuple[bool, str]:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return False, f"fail {call_count}"
        return True, "PASS"

    with tempfile.TemporaryDirectory() as tmpdir:
        tasks_path = Path(tmpdir) / "tasks.json"
        await write_tasks(tf, tasks_path)
        progress = ProgressLogger(Path(tmpdir))
        config = GolemConfig(max_retries=3)

        with (
            patch("golem.executor.run_worker", new_callable=AsyncMock, return_value="done"),
            patch("golem.executor.run_validation", side_effect=_mock_validate),
            patch("golem.executor.commit_task", return_value=True),
        ):
            await execute_group(group, Path(tmpdir) / "wt", tasks_path, tf, config, progress)

    assert task.status == "completed"
    assert call_count == 3


async def test_blocked_propagation() -> None:
    """Task that exhausts max_retries → blocked; next task continues."""
    task1 = make_task("task-001")
    task2 = make_task("task-002")  # no depends_on — should still run
    group = Group(id="g", description="", worktree_branch="golem/spec/g", tasks=[task1, task2])
    tf = make_tasks_file([group])

    with tempfile.TemporaryDirectory() as tmpdir:
        tasks_path = Path(tmpdir) / "tasks.json"
        await write_tasks(tf, tasks_path)
        progress = ProgressLogger(Path(tmpdir))
        config = GolemConfig(max_retries=2)

        validate_call_count = 0

        async def _mock_validate(t: Task, wt: str, cfg: GolemConfig, **kwargs: object) -> tuple[bool, str]:
            nonlocal validate_call_count
            validate_call_count += 1
            if t.id == "task-001":
                return False, "always fails"
            return True, "PASS"

        with (
            patch("golem.executor.run_worker", new_callable=AsyncMock, return_value="done"),
            patch("golem.executor.run_validation", side_effect=_mock_validate),
            patch("golem.executor.commit_task", return_value=True),
        ):
            await execute_group(group, Path(tmpdir) / "wt", tasks_path, tf, config, progress)

    assert task1.status == "blocked"
    assert task2.status == "completed"


async def test_completed_task_skipped() -> None:
    """Already completed task is not re-executed."""
    task = make_task("task-001", status="completed")
    group = Group(id="g", description="", worktree_branch="golem/spec/g", tasks=[task])
    tf = make_tasks_file([group])

    with tempfile.TemporaryDirectory() as tmpdir:
        tasks_path = Path(tmpdir) / "tasks.json"
        await write_tasks(tf, tasks_path)
        progress = ProgressLogger(Path(tmpdir))
        config = GolemConfig(max_retries=1)

        with (
            patch("golem.executor.run_worker", new_callable=AsyncMock, return_value="done") as mock_worker,
            patch("golem.executor.run_validation", new_callable=AsyncMock, return_value=(True, "PASS")),
            patch("golem.executor.commit_task", return_value=True),
        ):
            await execute_group(group, Path(tmpdir) / "wt", tasks_path, tf, config, progress)
            mock_worker.assert_not_called()
