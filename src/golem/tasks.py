from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

TaskStatus = Literal["pending", "in_progress", "completed", "blocked"]

_TASKS_LOCK = asyncio.Lock()


@dataclass
class Task:
    id: str
    description: str
    files_create: list[str]
    files_modify: list[str]
    depends_on: list[str]
    acceptance: list[str]
    validation_commands: list[str]
    reference_docs: list[str]
    status: TaskStatus
    retries: int
    last_feedback: str | None
    blocked_reason: str | None
    completed_at: str | None

    @classmethod
    def from_dict(cls, d: dict) -> Task:
        return cls(
            id=d["id"],
            description=d["description"],
            files_create=d.get("files_create", []),
            files_modify=d.get("files_modify", []),
            depends_on=d.get("depends_on", []),
            acceptance=d.get("acceptance", []),
            validation_commands=d.get("validation_commands", []),
            reference_docs=d.get("reference_docs", []),
            status=d.get("status", "pending"),
            retries=d.get("retries", 0),
            last_feedback=d.get("last_feedback"),
            blocked_reason=d.get("blocked_reason"),
            completed_at=d.get("completed_at"),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "description": self.description,
            "files_create": self.files_create,
            "files_modify": self.files_modify,
            "depends_on": self.depends_on,
            "acceptance": self.acceptance,
            "validation_commands": self.validation_commands,
            "reference_docs": self.reference_docs,
            "status": self.status,
            "retries": self.retries,
            "last_feedback": self.last_feedback,
            "blocked_reason": self.blocked_reason,
            "completed_at": self.completed_at,
        }


@dataclass
class Group:
    id: str
    description: str
    worktree_branch: str
    tasks: list[Task]

    @classmethod
    def from_dict(cls, d: dict) -> Group:
        return cls(
            id=d["id"],
            description=d["description"],
            worktree_branch=d["worktree_branch"],
            tasks=[Task.from_dict(t) for t in d.get("tasks", [])],
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "description": self.description,
            "worktree_branch": self.worktree_branch,
            "tasks": [t.to_dict() for t in self.tasks],
        }


@dataclass
class FinalValidation:
    depends_on_all: bool
    commands: list[str]

    @classmethod
    def from_dict(cls, d: dict) -> FinalValidation:
        return cls(
            depends_on_all=d.get("depends_on_all", True),
            commands=d.get("commands", []),
        )

    def to_dict(self) -> dict:
        return {"depends_on_all": self.depends_on_all, "commands": self.commands}


@dataclass
class TasksFile:
    spec: str
    created: str
    project: str
    branch: str
    models: dict[str, str]
    config: dict[str, object]
    groups: list[Group]
    final_validation: FinalValidation = field(default_factory=lambda: FinalValidation(depends_on_all=True, commands=[]))

    @classmethod
    def from_dict(cls, d: dict) -> TasksFile:
        return cls(
            spec=d["spec"],
            created=d["created"],
            project=d["project"],
            branch=d["branch"],
            models=d.get("models", {}),
            config=d.get("config", {}),
            groups=[Group.from_dict(g) for g in d.get("groups", [])],
            final_validation=FinalValidation.from_dict(d.get("final_validation", {"depends_on_all": True, "commands": []})),
        )

    def to_dict(self) -> dict:
        return {
            "spec": self.spec,
            "created": self.created,
            "project": self.project,
            "branch": self.branch,
            "models": self.models,
            "config": self.config,
            "groups": [g.to_dict() for g in self.groups],
            "final_validation": self.final_validation.to_dict(),
        }


def task_by_id(tasks_file: TasksFile, task_id: str) -> Task | None:
    for group in tasks_file.groups:
        for task in group.tasks:
            if task.id == task_id:
                return task
    return None


def read_tasks(path: Path) -> TasksFile:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return TasksFile.from_dict(data)


async def write_tasks(tasks_file: TasksFile, path: Path) -> None:
    async with _TASKS_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(tasks_file.to_dict(), f, indent=2)


def write_tasks_sync(tasks_file: TasksFile, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(tasks_file.to_dict(), f, indent=2)
