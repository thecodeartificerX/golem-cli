from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from golem.tasks import FinalValidation, Group, Task, TasksFile, read_tasks, task_by_id, write_tasks, write_tasks_sync


def make_task(task_id: str = "task-001", status: str = "pending") -> Task:
    return Task(
        id=task_id,
        description="Test task",
        files_create=["src/new.py"],
        files_modify=["src/existing.py"],
        depends_on=[],
        acceptance=["File exists"],
        validation_commands=["test -f src/new.py"],
        reference_docs=[],
        status=status,  # type: ignore[arg-type]
        retries=0,
        last_feedback=None,
        blocked_reason=None,
        completed_at=None,
    )


def make_group(group_id: str = "group-a", tasks: list[Task] | None = None) -> Group:
    return Group(
        id=group_id,
        description="Test group",
        worktree_branch=f"golem/spec/{group_id}",
        tasks=tasks or [make_task()],
    )


def make_tasks_file(groups: list[Group] | None = None) -> TasksFile:
    return TasksFile(
        spec="spec.md",
        created="2026-03-25T00:00:00Z",
        project="test",
        branch="golem/spec",
        models={"planner": "opus", "worker": "opus", "validator": "sonnet"},
        config={"max_retries": 3, "max_parallel": 3},
        groups=groups or [make_group()],
        final_validation=FinalValidation(depends_on_all=True, commands=[]),
    )


def test_task_from_dict() -> None:
    d = {
        "id": "task-001",
        "description": "desc",
        "files_create": ["f.py"],
        "files_modify": [],
        "depends_on": [],
        "acceptance": ["passes"],
        "validation_commands": ["echo ok"],
        "reference_docs": [],
        "status": "pending",
        "retries": 0,
        "last_feedback": None,
        "blocked_reason": None,
        "completed_at": None,
    }
    task = Task.from_dict(d)
    assert task.id == "task-001"
    assert task.status == "pending"
    assert task.acceptance == ["passes"]


def test_group_from_dict() -> None:
    d = {
        "id": "group-a",
        "description": "desc",
        "worktree_branch": "golem/spec/group-a",
        "tasks": [
            {
                "id": "task-001", "description": "d", "files_create": [], "files_modify": [],
                "depends_on": [], "acceptance": ["a"], "validation_commands": ["true"],
                "reference_docs": [], "status": "pending", "retries": 0,
                "last_feedback": None, "blocked_reason": None, "completed_at": None,
            }
        ],
    }
    group = Group.from_dict(d)
    assert group.id == "group-a"
    assert len(group.tasks) == 1
    assert group.tasks[0].id == "task-001"


def test_tasks_file_roundtrip() -> None:
    tf = make_tasks_file()
    d = tf.to_dict()
    tf2 = TasksFile.from_dict(d)
    assert tf2.spec == tf.spec
    assert len(tf2.groups) == len(tf.groups)
    assert tf2.groups[0].tasks[0].id == tf.groups[0].tasks[0].id


def test_status_transition_pending_to_in_progress() -> None:
    task = make_task(status="pending")
    task.status = "in_progress"
    assert task.status == "in_progress"


def test_status_transition_in_progress_to_completed() -> None:
    task = make_task(status="in_progress")
    task.status = "completed"
    task.completed_at = "2026-03-25T00:00:00Z"
    assert task.status == "completed"
    assert task.completed_at is not None


def test_status_transition_in_progress_to_blocked() -> None:
    task = make_task(status="in_progress")
    task.status = "blocked"
    task.blocked_reason = "max retries"
    assert task.status == "blocked"
    assert task.blocked_reason == "max retries"


def test_task_by_id_found() -> None:
    tf = make_tasks_file()
    found = task_by_id(tf, "task-001")
    assert found is not None
    assert found.id == "task-001"


def test_task_by_id_not_found() -> None:
    tf = make_tasks_file()
    assert task_by_id(tf, "nonexistent") is None


async def test_write_and_read_tasks_roundtrip() -> None:
    tf = make_tasks_file()
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "tasks.json"
        await write_tasks(tf, path)
        assert path.exists()
        tf2 = read_tasks(path)
        assert tf2.spec == tf.spec
        assert tf2.groups[0].tasks[0].id == tf.groups[0].tasks[0].id


async def test_concurrent_writes_no_corruption() -> None:
    """10 concurrent writes must not corrupt tasks.json."""
    tf = make_tasks_file()
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "tasks.json"

        async def _write(i: int) -> None:
            tf_copy = make_tasks_file()
            tf_copy.project = f"project-{i}"
            await write_tasks(tf_copy, path)

        await asyncio.gather(*[_write(i) for i in range(10)])

        # File should be valid JSON
        with open(path) as f:
            data = json.load(f)
        assert "spec" in data
        assert "groups" in data


def test_write_tasks_sync_writes_to_disk() -> None:
    """write_tasks_sync writes a valid JSON file that can be read back."""
    tf = make_tasks_file()
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "subdir" / "tasks.json"
        write_tasks_sync(tf, path)
        assert path.exists()
        tf2 = read_tasks(path)
        assert tf2.spec == tf.spec
        assert len(tf2.groups) == len(tf.groups)
        assert tf2.groups[0].tasks[0].id == tf.groups[0].tasks[0].id
