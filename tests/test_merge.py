from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from golem.merge import ConflictInfo, MergeCoordinator, MergeQueueEntry


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


class _FakeSession:
    def __init__(self, session_id: str, status: str = "running") -> None:
        self.id = session_id
        self.status = status


class _FakeSessionManager:
    def __init__(self, sessions: list[_FakeSession] | None = None) -> None:
        self._sessions: dict[str, _FakeSession] = {s.id: s for s in (sessions or [])}

    def get_session(self, session_id: str) -> _FakeSession | None:
        return self._sessions.get(session_id)

    def list_sessions(self) -> list[_FakeSession]:
        return list(self._sessions.values())

    def add(self, session: _FakeSession) -> None:
        self._sessions[session.id] = session


def _make_coordinator(tmp_path: Path, sessions: list[_FakeSession] | None = None) -> MergeCoordinator:
    mgr = _FakeSessionManager(sessions)
    return MergeCoordinator(tmp_path, mgr)


# ---------------------------------------------------------------------------
# Queue FIFO ordering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_adds_fifo_order(tmp_path: Path) -> None:
    coord = _make_coordinator(tmp_path)
    await coord.enqueue("a")
    await coord.enqueue("b")
    entries = coord._read_queue()
    assert len(entries) == 2
    assert entries[0].session_id == "a"
    assert entries[1].session_id == "b"


@pytest.mark.asyncio
async def test_dequeue_removes_entry(tmp_path: Path) -> None:
    coord = _make_coordinator(tmp_path)
    await coord.enqueue("a")
    await coord.dequeue("a")
    entries = coord._read_queue()
    assert entries == []


@pytest.mark.asyncio
async def test_enqueue_deduplicates(tmp_path: Path) -> None:
    coord = _make_coordinator(tmp_path)
    await coord.enqueue("a")
    await coord.enqueue("a")
    entries = coord._read_queue()
    assert len(entries) == 1


# ---------------------------------------------------------------------------
# Queue persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queue_persistence(tmp_path: Path) -> None:
    coord1 = _make_coordinator(tmp_path)
    await coord1.enqueue("a")

    # New instance reads same file
    coord2 = _make_coordinator(tmp_path)
    entries = coord2._read_queue()
    assert len(entries) == 1
    assert entries[0].session_id == "a"


def test_read_queue_missing_file(tmp_path: Path) -> None:
    coord = _make_coordinator(tmp_path)
    # queue file doesn't exist yet
    assert coord._read_queue() == []


def test_write_read_roundtrip(tmp_path: Path) -> None:
    coord = _make_coordinator(tmp_path)
    entry = MergeQueueEntry(session_id="x", enqueued_at="2026-01-01T00:00:00+00:00", pr_number=7, status="pr_open")
    coord._write_queue([entry])
    entries = coord._read_queue()
    assert len(entries) == 1
    assert entries[0].session_id == "x"
    assert entries[0].pr_number == 7
    assert entries[0].status == "pr_open"


# ---------------------------------------------------------------------------
# create_pr
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_pr_calls_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured_args: list[str] = []

    async def _fake_exec(*args: str, **kwargs: object) -> AsyncMock:
        captured_args.extend(args)
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"https://github.com/org/repo/pull/42\n", b""))
        mock_proc.returncode = 0
        return mock_proc

    monkeypatch.setattr("golem.merge.asyncio.create_subprocess_exec", _fake_exec)

    coord = _make_coordinator(tmp_path)
    await coord.enqueue("sess-1")
    url = await coord.create_pr("sess-1")

    assert "gh" in captured_args
    assert "pr" in captured_args
    assert "create" in captured_args
    assert url == "https://github.com/org/repo/pull/42"

    # Queue updated with pr_number and status
    entries = coord._read_queue()
    assert entries[0].pr_number == 42
    assert entries[0].status == "pr_open"


# ---------------------------------------------------------------------------
# merge_pr
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_pr_calls_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured_args: list[str] = []

    async def _fake_exec(*args: str, **kwargs: object) -> AsyncMock:
        captured_args.extend(args)
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0
        return mock_proc

    monkeypatch.setattr("golem.merge.asyncio.create_subprocess_exec", _fake_exec)

    sess = _FakeSession("sess-1", "awaiting_merge")
    coord = MergeCoordinator(tmp_path, _FakeSessionManager([sess]))

    # Seed queue with a pr_number
    entry = MergeQueueEntry(session_id="sess-1", enqueued_at="2026-01-01T00:00:00+00:00", pr_number=42, status="pr_open")
    coord._write_queue([entry])

    await coord.merge_pr("sess-1")

    assert "gh" in captured_args
    assert "pr" in captured_args
    assert "merge" in captured_args
    assert "42" in captured_args

    entries = coord._read_queue()
    assert entries[0].status == "merged"
    assert sess.status == "merged"


# ---------------------------------------------------------------------------
# rebase_queued — success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rebase_cascade_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_exec(*args: str, **kwargs: object) -> AsyncMock:
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0
        return mock_proc

    monkeypatch.setattr("golem.merge.asyncio.create_subprocess_exec", _fake_exec)

    # Mock run_qa to return passing result
    from golem.qa import QAResult
    monkeypatch.setattr("golem.merge.run_qa", lambda *a, **kw: QAResult(passed=True))

    sess2 = _FakeSession("sess-2", "queued")
    coord = MergeCoordinator(tmp_path, _FakeSessionManager([sess2]))
    await coord.enqueue("sess-2")

    await coord.rebase_queued("sess-1")

    # sess-2 should NOT be marked conflict or qa_failed
    assert sess2.status not in ("conflict", "qa_failed")


# ---------------------------------------------------------------------------
# rebase_queued — conflict
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rebase_cascade_conflict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    call_count = [0]

    async def _fake_exec(*args: str, **kwargs: object) -> AsyncMock:
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"CONFLICT"))
        # First call (fetch) succeeds, second call (rebase) fails
        call_count[0] += 1
        mock_proc.returncode = 0 if call_count[0] % 2 == 1 else 1
        return mock_proc

    monkeypatch.setattr("golem.merge.asyncio.create_subprocess_exec", _fake_exec)

    sess2 = _FakeSession("sess-2", "queued")
    coord = MergeCoordinator(tmp_path, _FakeSessionManager([sess2]))
    await coord.enqueue("sess-2")

    await coord.rebase_queued("sess-1")

    assert sess2.status == "conflict"


# ---------------------------------------------------------------------------
# detect_conflicts — overlap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detect_conflicts_overlap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    call_count = [0]

    async def _fake_exec(*args: str, **kwargs: object) -> AsyncMock:
        mock_proc = AsyncMock()
        # Both sessions touch src/foo.py
        mock_proc.communicate = AsyncMock(return_value=(b"src/foo.py\nsrc/bar.py\n", b""))
        mock_proc.returncode = 0
        return mock_proc

    monkeypatch.setattr("golem.merge.asyncio.create_subprocess_exec", _fake_exec)

    sess_a = _FakeSession("sess-a", "awaiting_merge")
    sess_b = _FakeSession("sess-b", "awaiting_merge")
    coord = MergeCoordinator(tmp_path, _FakeSessionManager([sess_a, sess_b]))

    conflicts = await coord.detect_conflicts()
    file_paths = [c.file_path for c in conflicts]
    assert "src/foo.py" in file_paths
    assert any(c.session_a in ("sess-a", "sess-b") and c.session_b in ("sess-a", "sess-b") for c in conflicts)


# ---------------------------------------------------------------------------
# detect_conflicts — no overlap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detect_conflicts_no_overlap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    responses = [b"src/a.py\n", b"src/b.py\n"]
    call_index = [0]

    async def _fake_exec(*args: str, **kwargs: object) -> AsyncMock:
        mock_proc = AsyncMock()
        idx = call_index[0] % len(responses)
        call_index[0] += 1
        mock_proc.communicate = AsyncMock(return_value=(responses[idx], b""))
        mock_proc.returncode = 0
        return mock_proc

    monkeypatch.setattr("golem.merge.asyncio.create_subprocess_exec", _fake_exec)

    sess_a = _FakeSession("sess-a", "awaiting_merge")
    sess_b = _FakeSession("sess-b", "awaiting_merge")
    coord = MergeCoordinator(tmp_path, _FakeSessionManager([sess_a, sess_b]))

    conflicts = await coord.detect_conflicts()
    assert conflicts == []


# ---------------------------------------------------------------------------
# detect_conflicts — empty
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detect_conflicts_empty(tmp_path: Path) -> None:
    coord = MergeCoordinator(tmp_path, _FakeSessionManager([]))
    conflicts = await coord.detect_conflicts()
    assert conflicts == []
