from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    pass


def test_find_server_no_file(tmp_path: Path) -> None:
    """Returns None when server.json does not exist."""
    from golem.client import find_server

    result = find_server(tmp_path)
    assert result is None


def test_find_server_stale_pid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Removes stale server.json and returns None when PID is dead."""
    from golem.client import find_server

    golem_dir = tmp_path / ".golem"
    golem_dir.mkdir()
    server_json = golem_dir / "server.json"
    server_json.write_text(
        json.dumps({"pid": 999999999, "host": "127.0.0.1", "port": 9664}),
        encoding="utf-8",
    )

    monkeypatch.setattr("golem.client._pid_alive", lambda pid: False)

    result = find_server(tmp_path)
    assert result is None
    assert not server_json.exists()


def test_find_server_valid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Returns (host, port) when server.json exists and PID is alive."""
    from golem.client import find_server

    golem_dir = tmp_path / ".golem"
    golem_dir.mkdir()
    server_json = golem_dir / "server.json"
    server_json.write_text(
        json.dumps({"pid": 12345, "host": "127.0.0.1", "port": 9664}),
        encoding="utf-8",
    )

    monkeypatch.setattr("golem.client._pid_alive", lambda pid: True)

    result = find_server(tmp_path)
    assert result == ("127.0.0.1", 9664)


@pytest.mark.asyncio
async def test_client_create_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """GolemClient.create_session posts to /api/sessions."""
    from golem.client import GolemClient

    captured: dict[str, object] = {}

    class MockResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, str]:
            return {"session_id": "test-session-1", "status": "running"}

    class MockAsyncClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> MockAsyncClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def post(self, url: str, json: object = None, **kwargs: object) -> MockResponse:
            captured["url"] = url
            captured["body"] = json
            return MockResponse()

    monkeypatch.setattr("golem.client.httpx.AsyncClient", MockAsyncClient)

    client = GolemClient("127.0.0.1", 9664)
    result = await client.create_session("/path/to/spec.md", "/project/root")

    assert captured["url"] == "http://127.0.0.1:9664/api/sessions"
    assert isinstance(captured["body"], dict)
    assert captured["body"]["spec_path"] == "/path/to/spec.md"  # type: ignore[index]
    assert result["session_id"] == "test-session-1"


@pytest.mark.asyncio
async def test_client_list_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    """GolemClient.list_sessions GETs /api/sessions."""
    from golem.client import GolemClient

    captured: dict[str, object] = {}

    class MockResponse:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> list[dict[str, str]]:
            return [{"id": "s1", "status": "running"}]

    class MockAsyncClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> MockAsyncClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def get(self, url: str, **kwargs: object) -> MockResponse:
            captured["url"] = url
            return MockResponse()

    monkeypatch.setattr("golem.client.httpx.AsyncClient", MockAsyncClient)

    client = GolemClient("127.0.0.1", 9664)
    result = await client.list_sessions()

    assert captured["url"] == "http://127.0.0.1:9664/api/sessions"
    assert len(result) == 1
    assert result[0]["id"] == "s1"


@pytest.mark.asyncio
async def test_client_stream_events(monkeypatch: pytest.MonkeyPatch) -> None:
    """GolemClient.stream_events parses SSE data lines."""
    from golem.client import GolemClient

    sse_lines = [
        'data: {"type": "log", "message": "Starting..."}',
        ": heartbeat",
        'data: {"type": "status", "state": "running"}',
        "data: not-json",
    ]

    class MockStreamResponse:
        async def __aenter__(self) -> MockStreamResponse:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        def raise_for_status(self) -> None:
            pass

        async def aiter_lines(self):  # type: ignore[return]
            for line in sse_lines:
                yield line

    class MockAsyncClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> MockAsyncClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        def stream(self, method: str, url: str) -> MockStreamResponse:
            return MockStreamResponse()

    monkeypatch.setattr("golem.client.httpx.AsyncClient", MockAsyncClient)

    client = GolemClient("127.0.0.1", 9664)
    collected: list[dict[str, object]] = []
    async for evt in client.stream_events("sess-1"):
        collected.append(evt)

    assert len(collected) == 2
    assert collected[0]["message"] == "Starting..."
    assert collected[1]["state"] == "running"
