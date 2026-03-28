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
