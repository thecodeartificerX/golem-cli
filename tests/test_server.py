from __future__ import annotations

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
    with patch("golem.server.asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_proc = MagicMock()
        mock_proc.pid = 99999
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=0)
        mock_exec.return_value = mock_proc

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
    with patch("golem.server.asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_proc = MagicMock()
        mock_proc.pid = 88888
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=0)
        mock_exec.return_value = mock_proc

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
    with patch("golem.server.asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_proc = MagicMock()
        mock_proc.pid = 77777
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=0)
        mock_exec.return_value = mock_proc

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
async def test_delete_session(client: AsyncClient, tmp_path: Path) -> None:
    """DELETE /api/sessions/{id} removes the session."""
    spec = tmp_path / "spec.md"
    spec.write_text("# Test\n", encoding="utf-8")
    with patch("golem.server.asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_proc = MagicMock()
        mock_proc.pid = 66666
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=0)
        mock_proc.terminate = MagicMock()
        mock_exec.return_value = mock_proc

        resp = await client.post("/api/sessions", json={
            "spec_path": str(spec),
            "project_root": str(tmp_path),
        })
    session_id = resp.json()["session_id"]

    resp = await client.delete(f"/api/sessions/{session_id}")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_pause_resume_session(client: AsyncClient, tmp_path: Path) -> None:
    """Pause and resume a session transitions status correctly."""
    spec = tmp_path / "spec.md"
    spec.write_text("# Test\n", encoding="utf-8")
    with patch("golem.server.asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_proc = MagicMock()
        mock_proc.pid = 55555
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=0)
        mock_proc.send_signal = MagicMock()
        mock_exec.return_value = mock_proc

        resp = await client.post("/api/sessions", json={
            "spec_path": str(spec),
            "project_root": str(tmp_path),
        })
    session_id = resp.json()["session_id"]

    # Pause
    resp = await client.post(f"/api/sessions/{session_id}/pause")
    assert resp.status_code == 200

    # Resume
    resp = await client.post(f"/api/sessions/{session_id}/resume")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_guidance_creates_ticket(client: AsyncClient, tmp_path: Path) -> None:
    """POST /api/sessions/{id}/guidance writes a guidance ticket."""
    spec = tmp_path / "spec.md"
    spec.write_text("# Test\n", encoding="utf-8")
    with patch("golem.server.asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_proc = MagicMock()
        mock_proc.pid = 44444
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=0)
        mock_exec.return_value = mock_proc

        resp = await client.post("/api/sessions", json={
            "spec_path": str(spec),
            "project_root": str(tmp_path),
        })
    session_id = resp.json()["session_id"]

    resp = await client.post(f"/api/sessions/{session_id}/guidance", json={"text": "Focus on auth first"})
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("ok") is True
