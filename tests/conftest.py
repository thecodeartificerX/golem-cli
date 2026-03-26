"""Shared test fixtures for the Golem test suite."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from golem.tickets import Ticket, TicketContext


@pytest.fixture()
def make_ticket():
    """Factory fixture for creating Ticket instances with sensible defaults."""

    def _make(
        title: str = "Test Ticket",
        status: str = "pending",
        assigned_to: str = "writer",
        ticket_type: str = "task",
        priority: str = "medium",
        created_by: str = "planner",
        blueprint: str = "",
        acceptance: list[str] | None = None,
        qa_checks: list[str] | None = None,
    ) -> Ticket:
        return Ticket(
            id="",
            type=ticket_type,
            title=title,
            status=status,
            priority=priority,
            created_by=created_by,
            assigned_to=assigned_to,
            context=TicketContext(
                blueprint=blueprint,
                acceptance=acceptance or [],
                qa_checks=qa_checks or [],
            ),
        )

    return _make


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    """Create an initialized git repo with an initial commit. Returns repo path."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("init", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return repo


@pytest.fixture()
def golem_dir(tmp_path: Path) -> Path:
    """Create a complete .golem/ directory structure for CLI integration tests."""
    gd = tmp_path / ".golem"
    for subdir in ("tickets", "research", "plans", "references", "reports", "worktrees"):
        (gd / subdir).mkdir(parents=True)
    return gd


def write_ticket_json(tickets_dir: Path, ticket_id: str, title: str, status: str) -> None:
    """Helper to write a ticket JSON file directly (for tests that need files on disk)."""
    data = {
        "id": ticket_id,
        "type": "task",
        "title": title,
        "status": status,
        "priority": "medium",
        "created_by": "planner",
        "assigned_to": "writer",
        "context": {
            "plan_file": "",
            "files": {},
            "references": [],
            "blueprint": "",
            "acceptance": [],
            "qa_checks": [],
            "parallelism_hints": [],
        },
        "history": [
            {
                "ts": "2026-03-27T10:00:00+00:00",
                "agent": "planner",
                "action": "created",
                "note": f"Ticket created: {title}",
                "attachments": [],
            }
        ],
    }
    (tickets_dir / f"{ticket_id}.json").write_text(
        json.dumps(data, indent=2), encoding="utf-8"
    )
