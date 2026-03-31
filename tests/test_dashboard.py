"""Tests for the dashboard UI endpoints."""

from __future__ import annotations

import re

import pytest
from httpx import ASGITransport, AsyncClient

from golem.server import create_app


@pytest.fixture()
def dash_app(tmp_path, monkeypatch):
    golem_dir = tmp_path / ".golem"
    golem_dir.mkdir()
    monkeypatch.setenv("GOLEM_DIR", str(golem_dir))
    monkeypatch.setenv("GOLEM_REGISTRY_PATH", str(tmp_path / "repos.json"))
    monkeypatch.setenv("GOLEM_TEST_MODE", "1")
    return create_app()


@pytest.fixture()
async def dash_client(dash_app):
    transport = ASGITransport(app=dash_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_dashboard_root_returns_html(dash_client):
    resp = await dash_client.get("/")
    assert resp.status_code == 200
    assert "G O L E M" in resp.text
    assert "pipeline-board" in resp.text
    assert "edict-list" in resp.text


@pytest.mark.asyncio
async def test_dashboard_contains_key_elements(dash_client):
    resp = await dash_client.get("/")
    assert "new-edict-btn" in resp.text
    assert "repo-tabs" in resp.text
    assert "card-detail-modal" in resp.text
    assert "pane-board" in resp.text
    assert "pane-plan" in resp.text
    assert "pane-diff" in resp.text
    assert "pane-cost" in resp.text
    assert "pane-logs" in resp.text


@pytest.mark.asyncio
async def test_dashboard_has_no_emoji(dash_client):
    resp = await dash_client.get("/")
    emoji_pattern = re.compile(r"[\U0001F300-\U0001F9FF]")
    assert not emoji_pattern.search(resp.text)


@pytest.mark.asyncio
async def test_legacy_route_serves_old_ui(dash_client):
    resp = await dash_client.get("/legacy")
    assert resp.status_code == 200
