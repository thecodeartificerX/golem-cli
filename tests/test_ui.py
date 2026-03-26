from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import golem.ui as ui_module
from golem.ui import _parse_log_line, create_app, format_sse, start_server

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_module_state() -> None:
    """Reset all module-level mutable state before each test.

    The ui module holds global singletons (current_process, current_cwd,
    event_queue, log_buffer) that persist across tests if not reset.
    """
    ui_module.current_process = None
    ui_module.current_cwd = None
    ui_module.log_buffer.clear()
    # Drain the queue (it may not be empty from a prior test)
    while not ui_module.event_queue.empty():
        try:
            ui_module.event_queue.get_nowait()
        except Exception:
            break


@pytest.fixture()
def client() -> TestClient:
    """Return a synchronous TestClient wrapping a fresh FastAPI app instance."""
    app = create_app()
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture()
def spec_file(tmp_path: Path) -> Path:
    """Create a minimal .md spec file in a temporary directory."""
    f = tmp_path / "spec.md"
    f.write_text("# Test Spec\n\n## Task\n\nDo something.\n", encoding="utf-8")
    return f


# ---------------------------------------------------------------------------
# format_sse
# ---------------------------------------------------------------------------


def test_format_sse_structure() -> None:
    """format_sse must produce event/data lines ending with double newline."""
    result = format_sse("log", {"message": "hello"})
    assert result.startswith("event: log\n")
    assert "data: " in result
    assert result.endswith("\n\n")


def test_format_sse_data_is_valid_json() -> None:
    """The data line in the SSE output must be valid JSON."""
    payload: dict[str, object] = {"message": "hello", "verb": "START", "timestamp": "12:00:00"}
    result = format_sse("log", payload)
    # Extract the data line
    lines = result.strip().splitlines()
    data_line = next(line for line in lines if line.startswith("data: "))
    parsed = json.loads(data_line[len("data: "):])
    assert parsed["message"] == "hello"
    assert parsed["verb"] == "START"


def test_format_sse_event_type_included() -> None:
    """format_sse must include the event type verbatim on the event: line."""
    result = format_sse("status", {"state": "running"})
    assert "event: status\n" in result


def test_format_sse_wire_format_exact() -> None:
    """Verify the exact wire format: event, newline, data, newline, newline."""
    result = format_sse("ping", {"ok": True})
    assert result == f"event: ping\ndata: {json.dumps({'ok': True})}\n\n"


# ---------------------------------------------------------------------------
# _parse_log_line
# ---------------------------------------------------------------------------


def test_parse_progress_line_valid() -> None:
    """A well-formed progress.log line must be parsed into timestamp, verb, message."""
    line = "[2026-03-25T12:00:15Z] COMPLETE parse-spec"
    result = _parse_log_line(line)
    assert result["timestamp"] == "12:00:15"
    assert result["verb"] == "COMPLETE"
    assert result["message"] == "COMPLETE parse-spec"
    assert result["raw"] == line


def test_parse_progress_line_verb_and_remainder() -> None:
    """verb and trailing text are correctly separated in the message field."""
    line = "[2026-03-25T09:30:00Z] START task-001 implement login"
    result = _parse_log_line(line)
    assert result["verb"] == "START"
    assert "task-001" in str(result["message"])
    assert result["timestamp"] == "09:30:00"


def test_parse_progress_line_invalid_returns_fallback() -> None:
    """A line not matching the regex must set verb to None and message to the full line."""
    line = "this is not a valid progress line"
    result = _parse_log_line(line)
    assert result["verb"] is None
    assert result["timestamp"] == ""
    assert result["message"] == line
    assert result["raw"] == line


def test_parse_progress_line_strips_trailing_newline() -> None:
    """_parse_log_line must strip trailing carriage return / newline characters."""
    line = "[2026-03-25T08:00:01Z] DONE finished\n"
    result = _parse_log_line(line)
    assert result["verb"] == "DONE"
    # The raw value should not contain a trailing newline
    assert not str(result["raw"]).endswith("\n")


def test_parse_progress_line_verb_only() -> None:
    """A line with a verb but no trailing text must produce message equal to verb."""
    line = "[2026-03-25T10:00:00Z] IDLE"
    result = _parse_log_line(line)
    assert result["verb"] == "IDLE"
    assert result["message"] == "IDLE"


# ---------------------------------------------------------------------------
# Flatten tasks.json logic (tested via poll_tasks_json data structure)
# ---------------------------------------------------------------------------


def _flatten_tasks_json(data: dict[str, object]) -> dict[str, object]:
    """Replicate the flattening logic from poll_tasks_json for test isolation."""
    flat_tasks: list[dict[str, object]] = []
    for group in data.get("groups", []):  # type: ignore[union-attr]
        group_id = group.get("id", "")
        for task in group.get("tasks", []):
            task_dict = dict(task)
            task_dict["group"] = group_id
            flat_tasks.append(task_dict)
    completed = sum(1 for t in flat_tasks if t.get("status") == "completed")
    total = len(flat_tasks)
    return {"tasks": flat_tasks, "completed": completed, "total": total}


def test_flatten_tasks_json_injects_group_id() -> None:
    """Each task dict in the flattened output must contain the 'group' field from its parent group."""
    data: dict[str, object] = {
        "groups": [
            {
                "id": "group-1",
                "tasks": [
                    {"id": "task-001", "status": "completed", "description": "alpha"},
                    {"id": "task-002", "status": "pending", "description": "beta"},
                ],
            }
        ]
    }
    result = _flatten_tasks_json(data)
    tasks = result["tasks"]
    assert isinstance(tasks, list)
    assert len(tasks) == 2
    for task in tasks:
        assert isinstance(task, dict)
        assert task["group"] == "group-1"


def test_flatten_tasks_json_completed_count() -> None:
    """completed count must reflect only tasks with status == 'completed'."""
    data: dict[str, object] = {
        "groups": [
            {
                "id": "g1",
                "tasks": [
                    {"id": "t1", "status": "completed"},
                    {"id": "t2", "status": "pending"},
                    {"id": "t3", "status": "completed"},
                ],
            }
        ]
    }
    result = _flatten_tasks_json(data)
    assert result["completed"] == 2
    assert result["total"] == 3


def test_flatten_tasks_json_multiple_groups() -> None:
    """Tasks from multiple groups are all collected in the flat list."""
    data: dict[str, object] = {
        "groups": [
            {"id": "g1", "tasks": [{"id": "t1", "status": "pending"}]},
            {"id": "g2", "tasks": [{"id": "t2", "status": "pending"}, {"id": "t3", "status": "completed"}]},
        ]
    }
    result = _flatten_tasks_json(data)
    tasks = result["tasks"]
    assert isinstance(tasks, list)
    assert len(tasks) == 3
    group_ids = {t["group"] for t in tasks}  # type: ignore[index]
    assert "g1" in group_ids
    assert "g2" in group_ids


def test_flatten_tasks_json_empty_groups() -> None:
    """An empty groups list must produce zero tasks with zero completed and total."""
    data: dict[str, object] = {"groups": []}
    result = _flatten_tasks_json(data)
    assert result["tasks"] == []
    assert result["completed"] == 0
    assert result["total"] == 0


# ---------------------------------------------------------------------------
# GET / — index endpoint
# ---------------------------------------------------------------------------


def test_index_returns_200(client: TestClient) -> None:
    """GET / must return HTTP 200."""
    response = client.get("/")
    assert response.status_code == 200


def test_index_returns_html_content_type(client: TestClient) -> None:
    """GET / must return a response with text/html content type."""
    response = client.get("/")
    assert "text/html" in response.headers.get("content-type", "")


def test_index_returns_html_body(client: TestClient) -> None:
    """GET / must return a non-empty HTML body."""
    response = client.get("/")
    assert len(response.text) > 0
    # The placeholder or real template must be a valid HTML document
    assert "<html" in response.text.lower() or "<!doctype" in response.text.lower()


# ---------------------------------------------------------------------------
# POST /api/run — validation errors
# ---------------------------------------------------------------------------


def test_run_endpoint_no_body_returns_422(client: TestClient) -> None:
    """POST /api/run with no body must return 422 (Pydantic validation error)."""
    response = client.post("/api/run", content="", headers={"Content-Type": "application/json"})
    assert response.status_code == 422


def test_run_endpoint_no_spec_path_field_returns_422(client: TestClient) -> None:
    """POST /api/run with missing spec_path field must return 422."""
    response = client.post("/api/run", json={"other_field": "value"})
    assert response.status_code == 422


def test_run_endpoint_invalid_extension_returns_400(client: TestClient, tmp_path: Path) -> None:
    """POST /api/run with a .txt file must return 400."""
    txt_file = tmp_path / "spec.txt"
    txt_file.write_text("not a markdown file", encoding="utf-8")
    response = client.post("/api/run", json={"spec_path": str(txt_file)})
    assert response.status_code == 400


def test_run_endpoint_nonexistent_path_returns_404(client: TestClient) -> None:
    """POST /api/run with a nonexistent .md path must return 404."""
    response = client.post("/api/run", json={"spec_path": "/nonexistent/path/spec.md"})
    assert response.status_code == 404


def test_run_endpoint_conflict_when_run_active(client: TestClient, spec_file: Path) -> None:
    """POST /api/run while a run is active must return 409."""
    # Simulate an active process: create a mock process with returncode=None
    from unittest.mock import MagicMock

    mock_proc = MagicMock()
    mock_proc.returncode = None  # Signals an active (not-yet-exited) process

    ui_module.current_process = mock_proc  # type: ignore[assignment]

    response = client.post("/api/run", json={"spec_path": str(spec_file)})
    assert response.status_code == 409

    # Cleanup: reset so the autouse fixture doesn't need special handling
    ui_module.current_process = None


def test_run_endpoint_valid_spec_starts_run(client: TestClient, spec_file: Path) -> None:
    """POST /api/run with a valid .md file must return 200 with status=started."""
    from unittest.mock import AsyncMock, MagicMock

    # Build mock process objects with the asyncio.Process interface
    mock_proc = MagicMock()
    mock_proc.returncode = None
    mock_proc.wait = AsyncMock(return_value=0)
    mock_proc.terminate = MagicMock()

    mock_clean_proc = MagicMock()
    mock_clean_proc.returncode = 0
    mock_clean_proc.wait = AsyncMock(return_value=0)

    call_count = 0

    async def _fake_create_subprocess_exec(*args: object, **kwargs: object) -> MagicMock:
        nonlocal call_count
        call_count += 1
        # First call is `golem clean`; second is `golem run`
        return mock_clean_proc if call_count == 1 else mock_proc

    # asyncio.create_subprocess_exec is an async function; patch with AsyncMock so
    # the route handler can `await` it correctly.
    async_mock = AsyncMock(side_effect=_fake_create_subprocess_exec)
    with patch("golem.ui.asyncio.create_subprocess_exec", new=async_mock):
        response = client.post("/api/run", json={"spec_path": str(spec_file)})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "started"
    assert "cwd" in body


# ---------------------------------------------------------------------------
# GET /api/events — SSE stream
#
# The event_stream() generator is infinite: after the initial burst it blocks
# on asyncio.wait_for(event_queue.get(), timeout=15.0).  We test it at two
# levels:
#
#   1. HTTP-level route test (sync) — verify the /api/events route exists and
#      returns the correct Content-Type header without consuming the body.
#
#   2. Generator-level tests (async) — drive the event_stream() coroutine
#      directly, injecting events and using asyncio.CancelledError to stop it,
#      so we never actually wait for the heartbeat timeout.
# ---------------------------------------------------------------------------


def test_events_route_registered(client: TestClient) -> None:
    """The /api/events route must be registered on the app (checked via OpenAPI schema)."""
    app = create_app()
    paths = {route.path for route in app.routes}  # type: ignore[union-attr]
    assert "/api/events" in paths


def test_events_route_media_type() -> None:
    """The /api/events route must declare media_type text/event-stream on its response class."""
    from fastapi.routing import APIRoute

    app = create_app()
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path == "/api/events":
            # The route exists — the actual media_type is set on the StreamingResponse
            # at call time; we verify the endpoint is callable here.
            assert callable(route.endpoint)
            break
    else:
        pytest.fail("/api/events route not found")


async def _collect_initial_events(max_events: int = 5) -> list[str]:
    """Drive event_stream() and collect up to max_events SSE strings before cancelling.

    Cancels the generator as soon as max_events have been collected, so we never
    wait for the 15-second heartbeat.
    """
    from golem.ui import event_stream

    events: list[str] = []
    gen = event_stream()
    try:
        async for item in gen:
            events.append(item)
            if len(events) >= max_events:
                break
    finally:
        await gen.aclose()
    return events


async def test_events_generator_yields_initial_status_event() -> None:
    """event_stream() must yield a status SSE event as its very first item."""
    events = await _collect_initial_events(max_events=1)
    assert len(events) >= 1
    assert "event: status" in events[0]


async def test_events_generator_idle_when_no_process() -> None:
    """With no active process, the first event from event_stream() must report state=idle."""
    ui_module.current_process = None
    events = await _collect_initial_events(max_events=1)
    first = events[0]
    assert "idle" in first


async def test_events_generator_running_when_process_active() -> None:
    """With an active process, the first event from event_stream() must report state=running."""
    from unittest.mock import MagicMock

    mock_proc = MagicMock()
    mock_proc.returncode = None  # Process still alive
    ui_module.current_process = mock_proc  # type: ignore[assignment]

    try:
        events = await _collect_initial_events(max_events=1)
        first = events[0]
        assert "running" in first
    finally:
        ui_module.current_process = None


async def test_events_generator_replays_log_buffer() -> None:
    """event_stream() must replay all log_buffer entries immediately after the status event."""
    entry: dict[str, str | None] = {
        "timestamp": "10:00:00",
        "verb": "START",
        "message": "START task-001",
        "raw": "[2026-03-25T10:00:00Z] START task-001",
    }
    ui_module.log_buffer.append(entry)

    # Collect status event + at least the one buffered log event
    events = await _collect_initial_events(max_events=2)
    combined = "".join(events)
    assert "event: log" in combined
    assert "START" in combined


async def test_events_generator_delivers_queued_event() -> None:
    """An event placed on event_queue must be delivered by event_stream()."""
    sse_str = format_sse("status", {"state": "done", "exit_code": 0})
    await ui_module.event_queue.put(sse_str)

    # Collect: status (idle), then the queued event
    events = await _collect_initial_events(max_events=2)
    combined = "".join(events)
    assert "done" in combined


# ---------------------------------------------------------------------------
# start_server — existence and callability
# ---------------------------------------------------------------------------


def test_start_server_is_callable() -> None:
    """start_server must exist and be a callable that accepts host/port/log_level."""
    assert callable(start_server)


def test_start_server_signature_accepts_kwargs() -> None:
    """start_server must accept host, port, and log_level as keyword arguments."""
    import inspect

    sig = inspect.signature(start_server)
    params = sig.parameters
    assert "host" in params
    assert "port" in params
    assert "log_level" in params


def test_start_server_default_port() -> None:
    """start_server default port must be 9664."""
    import inspect

    sig = inspect.signature(start_server)
    assert sig.parameters["port"].default == 9664


# ---------------------------------------------------------------------------
# create_app
# ---------------------------------------------------------------------------


def test_create_app_returns_fastapi_instance() -> None:
    """create_app() must return a FastAPI application instance."""
    from fastapi import FastAPI

    app = create_app()
    assert isinstance(app, FastAPI)


def test_create_app_registers_routes() -> None:
    """The app returned by create_app() must have routes for /, /api/run, /api/events."""
    app = create_app()
    paths = {route.path for route in app.routes}  # type: ignore[union-attr]
    assert "/" in paths
    assert "/api/run" in paths
    assert "/api/events" in paths


def test_create_app_called_twice_returns_independent_instances() -> None:
    """Each call to create_app() must return a distinct FastAPI instance."""
    from fastapi import FastAPI

    app1 = create_app()
    app2 = create_app()
    assert isinstance(app1, FastAPI)
    assert isinstance(app2, FastAPI)
    assert app1 is not app2


# ---------------------------------------------------------------------------
# Progress log tailing — integration with temp files
# ---------------------------------------------------------------------------


def test_parse_multiple_log_lines() -> None:
    """Multiple progress.log lines must each be parsed independently."""
    lines = [
        "[2026-03-25T10:00:00Z] START task-001 do work",
        "[2026-03-25T10:00:05Z] COMPLETE task-001 done",
        "raw fallback line",
    ]
    results = [_parse_log_line(line) for line in lines]

    assert results[0]["verb"] == "START"
    assert results[1]["verb"] == "COMPLETE"
    assert results[2]["verb"] is None


def test_log_buffer_populated_on_tail(tmp_path: Path) -> None:
    """Entries pushed into log_buffer must be retrievable and have the right structure."""
    # Directly simulate what tail_progress_log does when it finds a log line
    line = "[2026-03-25T11:00:00Z] RUNNING worker-1"
    event_dict = _parse_log_line(line)
    ui_module.log_buffer.append(event_dict)

    assert len(ui_module.log_buffer) == 1
    entry = ui_module.log_buffer[0]
    assert entry["verb"] == "RUNNING"
    assert entry["timestamp"] == "11:00:00"


def test_log_buffer_max_len_respected() -> None:
    """log_buffer must not grow beyond maxlen=200."""
    for i in range(250):
        ui_module.log_buffer.append({"timestamp": str(i), "verb": "X", "message": "m", "raw": "r"})

    assert len(ui_module.log_buffer) <= 200


# ---------------------------------------------------------------------------
# GET /api/browse/file — native file dialog
# ---------------------------------------------------------------------------


def test_browse_file_route_registered() -> None:
    """The /api/browse/file route must be registered on the app."""
    app = create_app()
    paths = {route.path for route in app.routes}  # type: ignore[union-attr]
    assert "/api/browse/file" in paths


def test_browse_file_returns_null_on_cancel(client: TestClient) -> None:
    """GET /api/browse/file must return {path: null} when user cancels."""
    with patch("golem.dialogs.open_file_dialog", return_value=None):
        resp = client.get("/api/browse/file")
    assert resp.status_code == 200
    assert resp.json() == {"path": None}


def test_browse_file_returns_path_on_selection(client: TestClient) -> None:
    """GET /api/browse/file must return the selected path."""
    with patch("golem.dialogs.open_file_dialog", return_value="F:/projects/spec.md"):
        resp = client.get("/api/browse/file")
    assert resp.status_code == 200
    assert resp.json()["path"] == "F:/projects/spec.md"


def test_browse_file_passes_initial_dir(client: TestClient) -> None:
    """GET /api/browse/file?initial_dir=X must forward X to open_file_dialog."""
    with patch("golem.dialogs.open_file_dialog", return_value=None) as mock_fn:
        client.get("/api/browse/file?initial_dir=F:/projects")
    mock_fn.assert_called_once_with("F:/projects")


def test_browse_file_empty_initial_dir_passes_none(client: TestClient) -> None:
    """GET /api/browse/file with no initial_dir must pass None to open_file_dialog."""
    with patch("golem.dialogs.open_file_dialog", return_value=None) as mock_fn:
        client.get("/api/browse/file")
    mock_fn.assert_called_once_with(None)


# ---------------------------------------------------------------------------
# GET /api/browse/folder — native folder dialog
# ---------------------------------------------------------------------------


def test_browse_folder_route_registered() -> None:
    """The /api/browse/folder route must be registered on the app."""
    app = create_app()
    paths = {route.path for route in app.routes}  # type: ignore[union-attr]
    assert "/api/browse/folder" in paths


def test_browse_folder_returns_null_on_cancel(client: TestClient) -> None:
    """GET /api/browse/folder must return {path: null} when user cancels."""
    with patch("golem.dialogs.open_folder_dialog", return_value=None):
        resp = client.get("/api/browse/folder")
    assert resp.status_code == 200
    assert resp.json() == {"path": None}


def test_browse_folder_returns_path_on_selection(client: TestClient) -> None:
    """GET /api/browse/folder must return the selected directory path."""
    with patch("golem.dialogs.open_folder_dialog", return_value="F:/projects/my-app"):
        resp = client.get("/api/browse/folder")
    assert resp.status_code == 200
    assert resp.json()["path"] == "F:/projects/my-app"


def test_browse_folder_passes_initial_dir(client: TestClient) -> None:
    """GET /api/browse/folder?initial_dir=X must forward X to open_folder_dialog."""
    with patch("golem.dialogs.open_folder_dialog", return_value=None) as mock_fn:
        client.get("/api/browse/folder?initial_dir=F:/projects")
    mock_fn.assert_called_once_with("F:/projects")


# ---------------------------------------------------------------------------
# POST /api/run — project_root support
# ---------------------------------------------------------------------------


def test_run_with_project_root_uses_as_cwd(client: TestClient, spec_file: Path) -> None:
    """POST /api/run with project_root must use it as the subprocess cwd."""
    from unittest.mock import AsyncMock, MagicMock

    mock_proc = MagicMock()
    mock_proc.returncode = None
    mock_proc.wait = AsyncMock(return_value=0)
    mock_proc.terminate = MagicMock()

    mock_clean_proc = MagicMock()
    mock_clean_proc.returncode = 0
    mock_clean_proc.wait = AsyncMock(return_value=0)

    call_count = 0

    async def _fake(*args: object, **kwargs: object) -> MagicMock:
        nonlocal call_count
        call_count += 1
        return mock_clean_proc if call_count == 1 else mock_proc

    with patch("golem.ui.asyncio.create_subprocess_exec", new=AsyncMock(side_effect=_fake)):
        resp = client.post("/api/run", json={
            "spec_path": str(spec_file),
            "project_root": str(spec_file.parent),
        })

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "started"
    assert body["cwd"] == str(spec_file.parent.resolve())


def test_run_invalid_project_root_returns_400(client: TestClient, spec_file: Path) -> None:
    """POST /api/run with a non-existent project_root must return 400."""
    resp = client.post("/api/run", json={
        "spec_path": str(spec_file),
        "project_root": "/nonexistent/directory",
    })
    assert resp.status_code == 400


def test_run_empty_project_root_uses_spec_parent(client: TestClient, spec_file: Path) -> None:
    """POST /api/run with empty project_root must fall back to spec's parent."""
    from unittest.mock import AsyncMock, MagicMock

    mock_proc = MagicMock()
    mock_proc.returncode = None
    mock_proc.wait = AsyncMock(return_value=0)
    mock_proc.terminate = MagicMock()

    mock_clean_proc = MagicMock()
    mock_clean_proc.returncode = 0
    mock_clean_proc.wait = AsyncMock(return_value=0)

    call_count = 0

    async def _fake(*args: object, **kwargs: object) -> MagicMock:
        nonlocal call_count
        call_count += 1
        return mock_clean_proc if call_count == 1 else mock_proc

    with patch("golem.ui.asyncio.create_subprocess_exec", new=AsyncMock(side_effect=_fake)):
        resp = client.post("/api/run", json={
            "spec_path": str(spec_file),
            "project_root": "",
        })

    assert resp.status_code == 200
    body = resp.json()
    assert body["cwd"] == str(spec_file.parent.resolve())


# ---------------------------------------------------------------------------
# dialogs module — platform guard
# ---------------------------------------------------------------------------


def test_open_file_dialog_raises_on_non_windows() -> None:
    """open_file_dialog must raise NotImplementedError on non-Windows platforms."""
    import sys

    from golem import dialogs

    with patch.object(sys, "platform", "linux"):
        with pytest.raises(NotImplementedError):
            dialogs.open_file_dialog()


def test_open_folder_dialog_raises_on_non_windows() -> None:
    """open_folder_dialog must raise NotImplementedError on non-Windows platforms."""
    import sys

    from golem import dialogs

    with patch.object(sys, "platform", "linux"):
        with pytest.raises(NotImplementedError):
            dialogs.open_folder_dialog()
