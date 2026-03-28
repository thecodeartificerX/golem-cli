from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from golem.qa import QAResult, run_qa


# ---------------------------------------------------------------------------
# Protocols for dependency inversion (avoid circular imports with server.py)
# ---------------------------------------------------------------------------


class SessionStateProtocol(Protocol):
    id: str
    status: str


class SessionManagerProtocol(Protocol):
    def get_session(self, session_id: str) -> SessionStateProtocol | None: ...
    def list_sessions(self) -> list[SessionStateProtocol]: ...


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class ConflictInfo:
    file_path: str
    session_a: str
    session_b: str
    ticket_a: str
    ticket_b: str


@dataclass
class MergeQueueEntry:
    session_id: str
    enqueued_at: str
    pr_number: int | None = None
    status: str = "queued"


# ---------------------------------------------------------------------------
# MergeCoordinator
# ---------------------------------------------------------------------------


class MergeCoordinator:
    """FIFO merge queue, PR lifecycle, rebase cascade, and conflict detection."""

    def __init__(self, coordinator_dir: Path, session_manager: SessionManagerProtocol) -> None:
        self._coordinator_dir = coordinator_dir
        self._session_manager = session_manager
        self._queue_file = coordinator_dir / "merge-queue.json"
        self._conflict_log_path = coordinator_dir / "conflict-log.json"

    # ------------------------------------------------------------------
    # Queue management
    # ------------------------------------------------------------------

    async def enqueue(self, session_id: str) -> None:
        """Add session to the FIFO merge queue and persist."""
        entries = self._read_queue()
        # Avoid duplicates
        if any(e.session_id == session_id for e in entries):
            return
        entries.append(MergeQueueEntry(
            session_id=session_id,
            enqueued_at=datetime.now(tz=UTC).isoformat(),
        ))
        self._write_queue(entries)

    async def dequeue(self, session_id: str) -> None:
        """Remove a session from the queue and persist."""
        entries = self._read_queue()
        entries = [e for e in entries if e.session_id != session_id]
        self._write_queue(entries)

    async def process_next(self) -> None:
        """Pick the next FIFO-queued entry and create a PR for it."""
        entries = self._read_queue()
        for entry in entries:
            if entry.status == "queued":
                await self.create_pr(entry.session_id)
                return

    # ------------------------------------------------------------------
    # PR lifecycle
    # ------------------------------------------------------------------

    async def create_pr(self, session_id: str) -> str:
        """Create a GitHub PR for the session's integration branch. Returns PR URL."""
        branch = f"golem/{session_id}/integration"
        proc = await asyncio.create_subprocess_exec(
            "gh", "pr", "create",
            "--head", branch,
            "--base", "main",
            "--title", f"Session {session_id}",
            "--body", "",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await proc.communicate()
        pr_url = stdout.decode("utf-8").strip()

        # Parse PR number from URL (e.g. https://github.com/org/repo/pull/42)
        pr_number: int | None = None
        if pr_url:
            parts = pr_url.rstrip("/").split("/")
            try:
                pr_number = int(parts[-1])
            except (ValueError, IndexError):
                pr_number = None

        # Update queue entry
        entries = self._read_queue()
        for entry in entries:
            if entry.session_id == session_id:
                entry.pr_number = pr_number
                entry.status = "pr_open"
                break
        self._write_queue(entries)

        return pr_url

    async def merge_pr(self, session_id: str) -> None:
        """Merge the open PR for the session. Updates session status to merged."""
        entries = self._read_queue()
        pr_number: int | None = None
        for entry in entries:
            if entry.session_id == session_id:
                pr_number = entry.pr_number
                break

        if pr_number is not None:
            proc = await asyncio.create_subprocess_exec(
                "gh", "pr", "merge", str(pr_number),
                "--merge", "--auto",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()

        # Update queue status
        for entry in entries:
            if entry.session_id == session_id:
                entry.status = "merged"
                break
        self._write_queue(entries)

        # Update session status
        session = self._session_manager.get_session(session_id)
        if session is not None:
            session.status = "merged"

    # ------------------------------------------------------------------
    # Rebase cascade
    # ------------------------------------------------------------------

    async def rebase_queued(self, merged_session_id: str) -> None:
        """Rebase all remaining queued sessions onto updated main after a merge."""
        entries = self._read_queue()
        repo_root = self._coordinator_dir.parent

        for entry in entries:
            if entry.session_id == merged_session_id:
                continue
            if entry.status not in ("queued", "pr_open"):
                continue

            worktree_path = self._coordinator_dir / "sessions" / entry.session_id / "worktrees" / "integration"

            # git fetch origin main
            fetch_proc = await asyncio.create_subprocess_exec(
                "git", "fetch", "origin", "main",
                cwd=str(worktree_path) if worktree_path.exists() else str(repo_root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await fetch_proc.communicate()

            # git rebase origin/main
            rebase_proc = await asyncio.create_subprocess_exec(
                "git", "rebase", "origin/main",
                cwd=str(worktree_path) if worktree_path.exists() else str(repo_root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await rebase_proc.communicate()

            if rebase_proc.returncode != 0:
                # Rebase failed — mark as conflict
                session = self._session_manager.get_session(entry.session_id)
                if session is not None:
                    session.status = "conflict"
                entry.status = "conflict"
            else:
                # Re-run QA on rebased code
                try:
                    qa_result: QAResult = run_qa(str(worktree_path), [])
                    if not qa_result.passed:
                        session = self._session_manager.get_session(entry.session_id)
                        if session is not None:
                            session.status = "qa_failed"
                        entry.status = "qa_failed"
                except Exception:  # noqa: BLE001
                    session = self._session_manager.get_session(entry.session_id)
                    if session is not None:
                        session.status = "qa_failed"
                    entry.status = "qa_failed"

        self._write_queue(entries)

    # ------------------------------------------------------------------
    # Conflict detection
    # ------------------------------------------------------------------

    async def detect_conflicts(self) -> list[ConflictInfo]:
        """Detect file-level conflicts across active sessions vs main."""
        sessions = self._session_manager.list_sessions()
        repo_root = self._coordinator_dir.parent

        # Map file -> list of session_ids that touch it
        active_statuses = {"running", "awaiting_merge", "pr_open", "queued"}
        file_to_sessions: dict[str, list[str]] = {}

        for session in sessions:
            if session.status not in active_statuses:
                continue
            branch = f"golem/{session.id}/integration"
            proc = await asyncio.create_subprocess_exec(
                "git", "diff", "--name-only", f"origin/main...{branch}",
                cwd=str(repo_root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _stderr = await proc.communicate()
            if proc.returncode != 0:
                continue
            for line in stdout.decode("utf-8").splitlines():
                file_path = line.strip()
                if not file_path:
                    continue
                if file_path not in file_to_sessions:
                    file_to_sessions[file_path] = []
                file_to_sessions[file_path].append(session.id)

        conflicts: list[ConflictInfo] = []
        for file_path, session_ids in file_to_sessions.items():
            if len(session_ids) < 2:
                continue
            # Generate a ConflictInfo for each pair
            for i in range(len(session_ids)):
                for j in range(i + 1, len(session_ids)):
                    conflicts.append(ConflictInfo(
                        file_path=file_path,
                        session_a=session_ids[i],
                        session_b=session_ids[j],
                        ticket_a="",
                        ticket_b="",
                    ))

        return conflicts

    # ------------------------------------------------------------------
    # Conflict scanner
    # ------------------------------------------------------------------

    async def run_conflict_scanner(
        self,
        interval_seconds: int = 30,
        on_new_conflicts: Callable[[list[ConflictInfo]], Awaitable[None]] | None = None,
    ) -> None:
        """Run an infinite loop scanning for conflicts every interval_seconds."""
        # Load previous conflict keys from disk
        previous_keys: set[str] = set()
        if self._conflict_log_path.exists():
            try:
                raw = json.loads(self._conflict_log_path.read_text(encoding="utf-8"))
                for item in raw:
                    key = f"{item.get('file_path')}|{item.get('session_a')}|{item.get('session_b')}"
                    previous_keys.add(key)
            except (json.JSONDecodeError, OSError):
                pass

        while True:
            try:
                conflicts = await self.detect_conflicts()
            except Exception:  # noqa: BLE001
                conflicts = []

            # Persist to conflict-log.json
            self._coordinator_dir.mkdir(parents=True, exist_ok=True)
            self._conflict_log_path.write_text(
                json.dumps([dataclasses.asdict(c) for c in conflicts], indent=2),
                encoding="utf-8",
            )

            # Compute new conflicts not seen before
            current_keys: set[str] = set()
            for c in conflicts:
                current_keys.add(f"{c.file_path}|{c.session_a}|{c.session_b}")

            new_conflicts = [
                c for c in conflicts
                if f"{c.file_path}|{c.session_a}|{c.session_b}" not in previous_keys
            ]
            previous_keys = current_keys

            if new_conflicts and on_new_conflicts is not None:
                await on_new_conflicts(new_conflicts)

            await asyncio.sleep(interval_seconds)

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _read_queue(self) -> list[MergeQueueEntry]:
        """Read the merge queue from disk. Returns [] if file doesn't exist."""
        if not self._queue_file.exists():
            return []
        raw = json.loads(self._queue_file.read_text(encoding="utf-8"))
        return [MergeQueueEntry(**item) for item in raw]

    def _write_queue(self, entries: list[MergeQueueEntry]) -> None:
        """Persist the merge queue to disk."""
        self._coordinator_dir.mkdir(parents=True, exist_ok=True)
        self._queue_file.write_text(
            json.dumps([dataclasses.asdict(e) for e in entries], indent=2),
            encoding="utf-8",
        )
