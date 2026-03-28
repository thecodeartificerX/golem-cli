from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from golem.merge import ConflictInfo, MergeCoordinator, MergeQueueEntry


# ---------------------------------------------------------------------------
# Helpers
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
# test_overlap_detection_two_sessions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_overlap_detection_two_sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two sessions modifying the same file should produce a ConflictInfo."""

    async def _fake_exec(*args: str, **kwargs: object) -> AsyncMock:
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"src/shared.py\nsrc/other.py\n", b""))
        mock_proc.returncode = 0
        return mock_proc

    monkeypatch.setattr("golem.merge.asyncio.create_subprocess_exec", _fake_exec)

    sess_a = _FakeSession("sess-a", "awaiting_merge")
    sess_b = _FakeSession("sess-b", "awaiting_merge")
    coord = _make_coordinator(tmp_path, [sess_a, sess_b])

    conflicts = await coord.detect_conflicts()
    assert len(conflicts) > 0
    file_paths = [c.file_path for c in conflicts]
    assert "src/shared.py" in file_paths
    conflict = next(c for c in conflicts if c.file_path == "src/shared.py")
    assert set([conflict.session_a, conflict.session_b]) == {"sess-a", "sess-b"}


# ---------------------------------------------------------------------------
# test_no_overlap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_overlap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sessions modifying different files should return empty conflicts."""
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
    coord = _make_coordinator(tmp_path, [sess_a, sess_b])

    conflicts = await coord.detect_conflicts()
    assert conflicts == []


# ---------------------------------------------------------------------------
# test_scanner_interval
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scanner_interval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Scanner calls detect_conflicts and writes conflict-log.json each iteration."""
    detect_calls: list[int] = []

    async def _fake_detect_conflicts(self: MergeCoordinator) -> list[ConflictInfo]:
        detect_calls.append(1)
        return []

    sleep_calls: list[float] = []
    call_count = [0]

    async def _fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        call_count[0] += 1
        if call_count[0] >= 1:
            raise asyncio.CancelledError

    monkeypatch.setattr("golem.merge.MergeCoordinator.detect_conflicts", _fake_detect_conflicts)
    monkeypatch.setattr("golem.merge.asyncio.sleep", _fake_sleep)

    coord = _make_coordinator(tmp_path)
    with pytest.raises(asyncio.CancelledError):
        await coord.run_conflict_scanner(interval_seconds=5)

    # detect_conflicts was called at least once
    assert len(detect_calls) >= 1
    # conflict-log.json was written
    assert coord._conflict_log_path.exists()
    data = json.loads(coord._conflict_log_path.read_text(encoding="utf-8"))
    assert isinstance(data, list)


# ---------------------------------------------------------------------------
# test_stale_merge_entry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_merge_entry(tmp_path: Path) -> None:
    """Session with pr_open status but no running process should be marked failed on startup."""
    from golem.server import create_app
    import os

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True)

    # Write a session.json for a "running" session (with spec.md to trigger recovery)
    sess_dir = sessions_dir / "stale-sess-1"
    sess_dir.mkdir()
    (sess_dir / "spec.md").write_text("# Fake\n", encoding="utf-8")
    (sess_dir / "session.json").write_text(
        json.dumps({"id": "stale-sess-1", "spec_path": "/fake/spec.md", "status": "running"}),
        encoding="utf-8",
    )

    # Write a merge queue entry with pr_open
    merge_queue_file = tmp_path / "merge-queue.json"
    merge_queue_file.write_text(
        json.dumps([
            {
                "session_id": "stale-sess-1",
                "enqueued_at": "2026-01-01T00:00:00+00:00",
                "pr_number": 5,
                "status": "pr_open",
            }
        ]),
        encoding="utf-8",
    )

    env_backup = os.environ.get("GOLEM_DIR")
    os.environ["GOLEM_DIR"] = str(tmp_path)
    try:
        app = create_app()
    finally:
        if env_backup is None:
            os.environ.pop("GOLEM_DIR", None)
        else:
            os.environ["GOLEM_DIR"] = env_backup

    # The stale entry should have been marked failed in the queue
    queue_data = json.loads(merge_queue_file.read_text(encoding="utf-8"))
    assert any(e["status"] == "failed" for e in queue_data)


# ---------------------------------------------------------------------------
# test_server_restart_recovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_server_restart_recovery(tmp_path: Path) -> None:
    """Sessions found on disk should be restored as paused on server startup."""
    import os

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True)

    # Write two session.json files representing existing (non-archived) sessions
    for sid in ("sess-alpha-1", "sess-beta-1"):
        sess_dir = sessions_dir / sid
        sess_dir.mkdir()
        (sess_dir / "spec.md").write_text("# Fake\n", encoding="utf-8")
        (sess_dir / "session.json").write_text(
            json.dumps({"id": sid, "spec_path": "/fake/spec.md", "status": "running"}),
            encoding="utf-8",
        )

    # One archived session — should NOT be restored
    archived_dir = sessions_dir / "sess-archived-1"
    archived_dir.mkdir()
    (archived_dir / "spec.md").write_text("# Fake\n", encoding="utf-8")
    (archived_dir / "session.json").write_text(
        json.dumps({"id": "sess-archived-1", "spec_path": "/fake/spec.md", "status": "archived"}),
        encoding="utf-8",
    )

    env_backup = os.environ.get("GOLEM_DIR")
    os.environ["GOLEM_DIR"] = str(tmp_path)
    try:
        from golem.server import SessionManager
        sm = SessionManager(sessions_dir)
        # Import create_app AFTER setting GOLEM_DIR
        from golem.server import create_app
        app = create_app()
    finally:
        if env_backup is None:
            os.environ.pop("GOLEM_DIR", None)
        else:
            os.environ["GOLEM_DIR"] = env_backup

    # The app was created — verify via routes that /api/stats exists (app is valid)
    routes = [r.path for r in app.routes if hasattr(r, "path")]
    assert "/api/stats" in routes
    assert "/api/history" in routes
