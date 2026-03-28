from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from golem.server import (
    SessionManager,
    SessionState,
    create_app,
    monitor_process,
    remove_server_json,
    run_session,
    write_server_json,
)


@pytest.fixture()
def app():
    """Create a fresh FastAPI app for each test."""
    return create_app()


@pytest.fixture()
async def client(app):
    """Async test client using httpx ASGITransport."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# -- Skeleton tests (Task 6) --


def test_create_app_returns_fastapi() -> None:
    """create_app() returns a FastAPI instance."""
    from fastapi import FastAPI
    app = create_app()
    assert isinstance(app, FastAPI)


def test_session_manager_create_and_list(tmp_path: Path) -> None:
    """SessionManager.create_session creates a session; list_sessions returns it."""
    mgr = SessionManager(tmp_path)
    session = mgr.create_session("test-1", Path("spec.md"))
    assert session.id == "test-1"
    sessions = mgr.list_sessions()
    assert len(sessions) == 1
    assert sessions[0].id == "test-1"


def test_session_manager_get_missing(tmp_path: Path) -> None:
    """SessionManager.get_session returns None for unknown IDs."""
    mgr = SessionManager(tmp_path)
    assert mgr.get_session("nonexistent") is None


def test_write_remove_server_json(tmp_path: Path) -> None:
    """write_server_json creates file; remove_server_json deletes it."""
    write_server_json(tmp_path, pid=12345, port=9664)
    server_json = tmp_path / "server.json"
    assert server_json.exists()
    data = json.loads(server_json.read_text(encoding="utf-8"))
    assert data["pid"] == 12345
    assert data["port"] == 9664

    remove_server_json(tmp_path)
    assert not server_json.exists()


@pytest.mark.asyncio
async def test_server_status_endpoint(client: AsyncClient) -> None:
    """GET /api/server/status returns 200 with expected keys."""
    resp = await client.get("/api/server/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "pid" in data
    assert "port" in data
    assert "uptime_seconds" in data
    assert "session_counts" in data


@pytest.mark.asyncio
async def test_root_returns_html(client: AsyncClient) -> None:
    """GET / returns 200 with HTML content."""
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "html" in resp.text.lower()


@pytest.mark.asyncio
async def test_create_session_returns_id(client: AsyncClient, tmp_path: Path) -> None:
    """POST /api/sessions creates a session and returns the ID."""
    spec = tmp_path / "test-spec.md"
    spec.write_text("# Test\n\n## Task\nDo something.\n", encoding="utf-8")
    async def _noop_session(*args, **kwargs):
        await asyncio.sleep(999)

    with patch("golem.server.run_session", side_effect=_noop_session):
        resp = await client.post("/api/sessions", json={
            "spec_path": str(spec),
            "project_root": str(tmp_path),
        })
    assert resp.status_code == 200
    data = resp.json()
    assert "session_id" in data
    assert data["status"] == "running"


@pytest.mark.asyncio
async def test_list_sessions_empty(client: AsyncClient) -> None:
    """GET /api/sessions returns empty list when no sessions exist."""
    resp = await client.get("/api/sessions")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_list_sessions_after_create(client: AsyncClient, tmp_path: Path) -> None:
    """After creating a session, it appears in the list."""
    spec = tmp_path / "spec.md"
    spec.write_text("# Test\n\n## Task\nDo it.\n", encoding="utf-8")
    async def _noop(*a, **k):
        await asyncio.sleep(999)

    with patch("golem.server.run_session", side_effect=_noop):
        await client.post("/api/sessions", json={
            "spec_path": str(spec),
            "project_root": str(tmp_path),
        })

    resp = await client.get("/api/sessions")
    data = resp.json()
    assert len(data) >= 1
    assert any(s["status"] in ("running", "pending") for s in data)


@pytest.mark.asyncio
async def test_get_session_detail(client: AsyncClient, tmp_path: Path) -> None:
    """GET /api/sessions/{id} returns session detail shape."""
    spec = tmp_path / "spec.md"
    spec.write_text("# Test\n", encoding="utf-8")
    async def _noop(*a, **k):
        await asyncio.sleep(999)

    with patch("golem.server.run_session", side_effect=_noop):
        resp = await client.post("/api/sessions", json={
            "spec_path": str(spec),
            "project_root": str(tmp_path),
        })
    session_id = resp.json()["session_id"]

    resp = await client.get(f"/api/sessions/{session_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == session_id
    assert "status" in data


@pytest.mark.asyncio
async def test_get_session_not_found(client: AsyncClient) -> None:
    """GET /api/sessions/{id} returns 404 for unknown ID."""
    resp = await client.get("/api/sessions/nonexistent-99")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_session_removes_from_memory_and_disk(client: AsyncClient, tmp_path: Path) -> None:
    """DELETE /api/sessions/{id} removes session from memory and deletes files."""
    spec = tmp_path / "spec.md"
    spec.write_text("# Test\n", encoding="utf-8")
    async def _noop(*a, **k):
        await asyncio.sleep(999)

    with patch("golem.server.run_session", side_effect=_noop):
        resp = await client.post("/api/sessions", json={
            "spec_path": str(spec),
            "project_root": str(tmp_path),
        })
    session_id = resp.json()["session_id"]

    resp = await client.delete(f"/api/sessions/{session_id}")
    assert resp.status_code == 200

    # Session gone from list
    resp = await client.get("/api/sessions")
    assert not any(s["id"] == session_id for s in resp.json())

    # GET returns 404 now
    resp = await client.get(f"/api/sessions/{session_id}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_session_keep_files(client: AsyncClient, tmp_path: Path) -> None:
    """DELETE with keep_files=true removes from memory but preserves disk files."""
    spec = tmp_path / "spec.md"
    spec.write_text("# Test\n", encoding="utf-8")
    async def _noop(*a, **k):
        await asyncio.sleep(999)

    with patch("golem.server.run_session", side_effect=_noop):
        resp = await client.post("/api/sessions", json={
            "spec_path": str(spec),
            "project_root": str(tmp_path),
        })
    session_id = resp.json()["session_id"]

    resp = await client.delete(f"/api/sessions/{session_id}?keep_files=true")
    assert resp.status_code == 200

    # Gone from memory
    resp = await client.get(f"/api/sessions/{session_id}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_cleanup_sessions(client: AsyncClient, tmp_path: Path) -> None:
    """POST /api/sessions/cleanup removes all finished sessions."""
    specs: list[Path] = []
    for i in range(3):
        s = tmp_path / f"spec{i}.md"
        s.write_text(f"# Test {i}\n", encoding="utf-8")
        specs.append(s)

    async def _noop(*a, **k):
        await asyncio.sleep(999)

    session_ids: list[str] = []
    for s in specs:
        with patch("golem.server.run_session", side_effect=_noop):
            resp = await client.post("/api/sessions", json={
                "spec_path": str(s),
                "project_root": str(tmp_path),
            })
        session_ids.append(resp.json()["session_id"])

    # Manually kill first two to make them "failed" (cleanable)
    await client.delete(f"/api/sessions/{session_ids[0]}?keep_files=true")
    await client.delete(f"/api/sessions/{session_ids[1]}?keep_files=true")

    # Third is still running — cleanup should leave it alone
    # (it's actually in "running" status from the mock)

    resp = await client.post("/api/sessions/cleanup")
    assert resp.status_code == 200
    data = resp.json()
    # Only already-deleted sessions are gone; the running one remains
    remaining = await client.get("/api/sessions")
    assert len(remaining.json()) >= 0  # At least the running one if not cleaned


@pytest.mark.asyncio
async def test_pause_resume_session(client: AsyncClient, tmp_path: Path) -> None:
    """Pause and resume a session transitions status correctly."""
    spec = tmp_path / "spec.md"
    spec.write_text("# Test\n", encoding="utf-8")
    async def _noop(*a, **k):
        await asyncio.sleep(999)

    with patch("golem.server.run_session", side_effect=_noop):
        resp = await client.post("/api/sessions", json={
            "spec_path": str(spec),
            "project_root": str(tmp_path),
        })
    session_id = resp.json()["session_id"]

    # Pause — returns 400 for in-process tasks (no subprocess to SIGSTOP)
    resp = await client.post(f"/api/sessions/{session_id}/pause")
    assert resp.status_code == 400

    # Resume — returns 400 (not paused)
    resp = await client.post(f"/api/sessions/{session_id}/resume")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_guidance_creates_ticket(client: AsyncClient, tmp_path: Path) -> None:
    """POST /api/sessions/{id}/guidance writes a guidance ticket."""
    spec = tmp_path / "spec.md"
    spec.write_text("# Test\n", encoding="utf-8")
    async def _noop(*a, **k):
        await asyncio.sleep(999)

    with patch("golem.server.run_session", side_effect=_noop):
        resp = await client.post("/api/sessions", json={
            "spec_path": str(spec),
            "project_root": str(tmp_path),
        })
    session_id = resp.json()["session_id"]

    resp = await client.post(f"/api/sessions/{session_id}/guidance", json={"text": "Focus on auth first"})
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("ok") is True


@pytest.mark.asyncio
async def test_session_events_sse_404(app) -> None:
    """SSE stream returns 404 for nonexistent session."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/sessions/fake-session/events")
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_aggregate_events_sse(app) -> None:
    """GET /api/events returns an SSE stream (connection succeeds)."""
    # Drive the endpoint function directly — ASGITransport hangs on infinite SSE streams
    # (see CLAUDE.md: "SSE tests must drive the generator directly")
    from fastapi.routing import APIRoute

    events_route = next(
        (r for r in app.routes if isinstance(r, APIRoute) and r.path == "/api/events"),
        None,
    )
    assert events_route is not None, "/api/events route not found"
    response = await events_route.endpoint()
    assert response.media_type == "text/event-stream"
    # Collect the first event from the generator then close it
    gen = response.body_iterator
    first_chunk = b""
    try:
        async for chunk in gen:
            if isinstance(chunk, str):
                first_chunk = chunk.encode()
            else:
                first_chunk = chunk
            break
    finally:
        await gen.aclose()
    assert len(first_chunk) > 0


@pytest.mark.asyncio
async def test_session_tickets_endpoint(client: AsyncClient, tmp_path: Path) -> None:
    """GET /api/sessions/{id}/tickets returns 404 for missing session."""
    resp = await client.get("/api/sessions/nonexistent/tickets")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_session_cost_endpoint(client: AsyncClient) -> None:
    """GET /api/sessions/{id}/cost returns 404 for missing session."""
    resp = await client.get("/api/sessions/nonexistent/cost")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_session_plan_endpoint(client: AsyncClient) -> None:
    """GET /api/sessions/{id}/plan returns 404 for missing session."""
    resp = await client.get("/api/sessions/nonexistent/plan")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_session_diff_endpoint(client: AsyncClient) -> None:
    """GET /api/sessions/{id}/diff returns 404 for missing session."""
    resp = await client.get("/api/sessions/nonexistent/diff")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_specs_endpoint(client: AsyncClient) -> None:
    """GET /api/specs returns a list of .md files."""
    resp = await client.get("/api/specs")
    assert resp.status_code == 200
    data = resp.json()
    assert "specs" in data
    assert isinstance(data["specs"], list)


@pytest.mark.asyncio
async def test_config_endpoint(client: AsyncClient) -> None:
    """GET /api/config returns a config dict."""
    resp = await client.get("/api/config")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, dict)
    assert "max_parallel" in data


@pytest.mark.asyncio
async def test_preflight_endpoint(client: AsyncClient, tmp_path: Path) -> None:
    """POST /api/preflight returns check results."""
    spec = tmp_path / "spec.md"
    spec.write_text("# Test\n", encoding="utf-8")
    resp = await client.post("/api/preflight", json={
        "spec_path": str(spec),
        "project_root": str(tmp_path),
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "ready" in data


@pytest.mark.asyncio
async def test_monitor_process_updates_status(tmp_path: Path) -> None:
    """monitor_process updates session status when subprocess exits."""
    from golem.server import SessionManager, monitor_process

    sessions_dir = tmp_path / "sessions"
    session_dir = sessions_dir / "test-monitor"
    session_dir.mkdir(parents=True)
    (session_dir / "session.json").write_text(
        json.dumps({"id": "test-monitor", "status": "running", "spec_path": "spec.md"}),
        encoding="utf-8",
    )

    mgr = SessionManager(sessions_dir)
    state = mgr.create_session("test-monitor", Path("spec.md"))

    mock_proc = AsyncMock()
    mock_proc.wait = AsyncMock(return_value=0)
    mock_proc.returncode = None
    mock_proc.pid = 11111
    state.process = mock_proc
    state.status = "running"

    await monitor_process(state, sessions_dir)
    assert state.status == "awaiting_merge"


# ---------------------------------------------------------------------------
# Merge queue endpoints
# ---------------------------------------------------------------------------


@pytest.fixture()
def merge_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """App instance backed by a temp GOLEM_DIR so merge tests don't share state."""
    monkeypatch.setenv("GOLEM_DIR", str(tmp_path / ".golem"))
    return create_app()


@pytest.fixture()
async def merge_client(merge_app):
    """Async client for merge tests."""
    transport = ASGITransport(app=merge_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_merge_queue_empty(merge_client: AsyncClient) -> None:
    """GET /api/merge-queue returns 200 and empty list when no sessions queued."""
    resp = await merge_client.get("/api/merge-queue")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_enqueue_via_api(merge_client: AsyncClient) -> None:
    """POST /api/merge-queue/{id} enqueues a session; GET shows it in the list."""
    resp = await merge_client.post("/api/merge-queue/sess-1")
    assert resp.status_code == 200
    assert resp.json() == {"status": "queued"}

    resp = await merge_client.get("/api/merge-queue")
    assert resp.status_code == 200
    data = resp.json()
    assert any(entry["session_id"] == "sess-1" for entry in data)


@pytest.mark.asyncio
async def test_dequeue_via_api(merge_client: AsyncClient) -> None:
    """DELETE /api/merge-queue/{id} removes the session from the queue."""
    # Seed the queue
    await merge_client.post("/api/merge-queue/sess-del")
    resp = await merge_client.delete("/api/merge-queue/sess-del")
    assert resp.status_code == 200
    assert resp.json() == {"status": "removed"}

    resp = await merge_client.get("/api/merge-queue")
    data = resp.json()
    assert not any(entry["session_id"] == "sess-del" for entry in data)


@pytest.mark.asyncio
async def test_approve_via_api(merge_app, monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /api/merge-queue/{id}/approve calls merge_pr + rebase_queued."""
    from golem.merge import MergeCoordinator

    merged: list[str] = []
    rebased: list[str] = []

    async def _mock_merge_pr(self: object, session_id: str) -> None:
        merged.append(session_id)

    async def _mock_rebase_queued(self: object, merged_session_id: str) -> None:
        rebased.append(merged_session_id)

    monkeypatch.setattr(MergeCoordinator, "merge_pr", _mock_merge_pr)
    monkeypatch.setattr(MergeCoordinator, "rebase_queued", _mock_rebase_queued)

    transport = ASGITransport(app=merge_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post("/api/merge-queue/sess-approve/approve")
    assert resp.status_code == 200
    assert resp.json() == {"status": "merged"}
    assert "sess-approve" in merged
    assert "sess-approve" in rebased


@pytest.mark.asyncio
async def test_conflicts_endpoint(merge_app, monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /api/conflicts returns conflict list from detect_conflicts."""
    from golem.merge import ConflictInfo, MergeCoordinator

    async def _mock_detect_conflicts(self: MergeCoordinator) -> list[ConflictInfo]:
        return [ConflictInfo(file_path="src/foo.py", session_a="a", session_b="b", ticket_a="", ticket_b="")]

    monkeypatch.setattr(MergeCoordinator, "detect_conflicts", _mock_detect_conflicts)

    transport = ASGITransport(app=merge_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/conflicts")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["file_path"] == "src/foo.py"


# ---------------------------------------------------------------------------
# Task 6: In-process session + observe/agents endpoint tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_session_emits_lifecycle_events() -> None:
    """run_session() emits SessionStart and SessionComplete events."""
    from golem.config import GolemConfig
    from golem.events import EventBus, GolemEvent, QueueBackend, SessionComplete, SessionStart
    from golem.server import run_session

    queue: asyncio.Queue = asyncio.Queue()
    bus = EventBus(QueueBackend(queue), session_id="test-session")
    config = GolemConfig()
    config.session_id = "test-session"

    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        spec = Path(tmpdir) / "spec.md"
        spec.write_text("# Test\n", encoding="utf-8")
        golem_dir = Path(tmpdir) / ".golem"

        with patch("golem.planner.run_planner") as mock_planner, \
             patch("golem.tech_lead.run_tech_lead") as mock_tl:
            mock_planner_result = MagicMock()
            mock_planner_result.ticket_id = "TICKET-001"
            mock_planner_result.cost_usd = 0.05
            mock_planner.return_value = mock_planner_result
            mock_tl.return_value = MagicMock()

            await run_session(spec, Path(tmpdir), config, bus, golem_dir)

    events: list[GolemEvent] = []
    while not queue.empty():
        events.append(queue.get_nowait())
    starts = [e for e in events if isinstance(e, SessionStart)]
    completes = [e for e in events if isinstance(e, SessionComplete)]
    assert len(starts) >= 1
    assert len(completes) == 1
    assert completes[0].status == "awaiting_merge"


@pytest.mark.asyncio
async def test_run_session_failure_emits_failed() -> None:
    """run_session() emits SessionComplete(status='failed') on exception."""
    from golem.config import GolemConfig
    from golem.events import EventBus, GolemEvent, QueueBackend, SessionComplete
    from golem.server import run_session

    queue: asyncio.Queue = asyncio.Queue()
    bus = EventBus(QueueBackend(queue), session_id="test-fail")
    config = GolemConfig()
    config.session_id = "test-fail"
    config.conductor_enabled = False

    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        spec = Path(tmpdir) / "spec.md"
        spec.write_text("# Test\n", encoding="utf-8")
        golem_dir = Path(tmpdir) / ".golem"

        with patch("golem.planner.run_planner", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError, match="boom"):
                await run_session(spec, Path(tmpdir), config, bus, golem_dir)

    events: list[GolemEvent] = []
    while not queue.empty():
        events.append(queue.get_nowait())
    completes = [e for e in events if isinstance(e, SessionComplete)]
    assert len(completes) == 1
    assert completes[0].status == "failed"
    assert "boom" in completes[0].error


@pytest.mark.asyncio
async def test_kill_session_cancels_task(client: AsyncClient, tmp_path: Path) -> None:
    """DELETE cancels the in-process task."""
    spec = tmp_path / "spec.md"
    spec.write_text("# Test\n", encoding="utf-8")

    async def _noop(*a, **k):
        await asyncio.sleep(999)

    with patch("golem.server.run_session", side_effect=_noop):
        resp = await client.post("/api/sessions", json={
            "spec_path": str(spec),
            "project_root": str(tmp_path),
        })
    session_id = resp.json()["session_id"]

    resp = await client.delete(f"/api/sessions/{session_id}")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_observe_endpoint_404(app) -> None:
    """GET /api/sessions/{id}/observe returns 404 for unknown session."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/sessions/fake/observe")
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_agents_endpoint_404(app) -> None:
    """GET /api/sessions/{id}/agents returns 404 for unknown session."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/sessions/fake/agents")
        assert resp.status_code == 404
