from __future__ import annotations

import asyncio
import collections
import hashlib
import json
import logging
import os
import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from golem import dialogs as _dialogs
from pydantic import BaseModel

logger = logging.getLogger("golem.ui")

# ---------------------------------------------------------------------------
# Module-level mutable state (single-process server, no concurrency concerns)
# ---------------------------------------------------------------------------

current_process: asyncio.subprocess.Process | None = None  # type: ignore[name-defined]
current_cwd: str | None = None
event_queue: asyncio.Queue[str] = asyncio.Queue()
log_buffer: collections.deque[dict[str, str | None]] = collections.deque(maxlen=200)

# Regex for progress.log line format: [2026-03-25T12:00:15Z] VERB remainder
_LOG_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}T([\d:]+)Z?)\]\s+(\w+)\s*(.*)$")

# Cached template HTML — loaded once at startup
_template_html: str = ""


# ---------------------------------------------------------------------------
# Request models — defined at module level so FastAPI can resolve type annotations
# correctly when using `from __future__ import annotations` (lazy annotation eval
# means locally-defined classes in create_app() are not visible at annotation
# resolution time, causing FastAPI to fall back to treating them as query params).
# ---------------------------------------------------------------------------


class RunRequest(BaseModel):
    spec_path: str
    project_root: str = ""  # if empty, derive from spec's parent (backward-compatible)


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------


def format_sse(event_type: str, data: dict[str, object]) -> str:
    """Format a Server-Sent Event string with correct wire format."""
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


def _parse_log_line(line: str) -> dict[str, str | None]:
    """Parse a single progress.log line into an event dict."""
    line = line.rstrip("\n\r")
    m = _LOG_RE.match(line)
    if m:
        _full_ts, time_part, verb, remainder = m.group(1), m.group(2), m.group(3), m.group(4)
        message = f"{verb} {remainder}".strip() if remainder else verb
        return {
            "timestamp": time_part,
            "verb": verb,
            "message": message,
            "raw": line,
        }
    return {
        "timestamp": "",
        "verb": None,
        "message": line,
        "raw": line,
    }


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------


async def tail_progress_log(golem_dir: Path) -> None:
    """Tail .golem/progress.log, broadcasting new lines as SSE log events."""
    global current_process

    log_path = golem_dir / "progress.log"
    seek_pos = 0

    while True:
        await asyncio.sleep(0.5)

        # Exit condition: process finished
        if current_process is None or current_process.returncode is not None:
            break

        if not log_path.exists():
            continue

        try:
            with open(log_path, encoding="utf-8") as f:
                f.seek(seek_pos)
                new_content = f.read()
                seek_pos = f.tell()
        except OSError:
            continue

        for raw_line in new_content.splitlines():
            if not raw_line.strip():
                continue
            event_dict = _parse_log_line(raw_line)
            log_buffer.append(event_dict)
            await event_queue.put(format_sse("log", event_dict))  # type: ignore[arg-type]


async def poll_tasks_json(golem_dir: Path) -> None:
    """Poll .golem/tasks.json for changes, broadcasting SSE tasks events on change."""
    global current_process

    tasks_path = golem_dir / "tasks.json"
    last_hash = ""

    while True:
        await asyncio.sleep(1.0)

        # Exit condition: process finished
        if current_process is None or current_process.returncode is not None:
            break

        if not tasks_path.exists():
            continue

        try:
            content = tasks_path.read_text(encoding="utf-8")
        except OSError:
            continue

        content_hash = hashlib.md5(content.encode("utf-8")).hexdigest()  # noqa: S324
        if content_hash == last_hash:
            continue
        last_hash = content_hash

        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            continue

        # Flatten group→tasks structure, injecting "group" field
        flat_tasks: list[dict[str, object]] = []
        for group in data.get("groups", []):
            group_id = group.get("id", "")
            for task in group.get("tasks", []):
                task_dict = dict(task)
                task_dict["group"] = group_id
                flat_tasks.append(task_dict)

        completed = sum(1 for t in flat_tasks if t.get("status") == "completed")
        total = len(flat_tasks)

        tasks_event: dict[str, object] = {
            "tasks": flat_tasks,
            "completed": completed,
            "total": total,
        }
        await event_queue.put(format_sse("tasks", tasks_event))


async def stream_subprocess_output(process: asyncio.subprocess.Process) -> None:  # type: ignore[name-defined]
    """Read subprocess stdout/stderr and forward to logging + SSE."""

    async def _read_stream(
        stream: asyncio.StreamReader | None, label: str
    ) -> None:
        if stream is None:
            return
        while True:
            line = await stream.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            if not text:
                continue
            logger.info("[%s] %s", label, text)
            event: dict[str, str | None] = {
                "timestamp": "",
                "verb": label,
                "message": text,
                "raw": text,
            }
            log_buffer.append(event)
            await event_queue.put(format_sse("log", event))  # type: ignore[arg-type]

    await asyncio.gather(
        _read_stream(process.stdout, "STDOUT"),
        _read_stream(process.stderr, "STDERR"),
    )


async def monitor_process(process: asyncio.subprocess.Process, spec_cwd: str) -> None:  # type: ignore[name-defined]
    """Wait for the golem subprocess to exit and emit a final status SSE event."""
    global current_process, current_cwd

    exit_code = await process.wait()
    logger.info("Run finished: exit_code=%d", exit_code)

    # Small delay to let tailing catch final lines
    await asyncio.sleep(1.0)

    if exit_code == 0:
        status_event: dict[str, object] = {"state": "done", "exit_code": exit_code}
    else:
        logger.warning("Process exited with code %d", exit_code)
        status_event = {
            "state": "error",
            "exit_code": exit_code,
            "message": f"Process exited with code {exit_code}",
        }

    await event_queue.put(format_sse("status", status_event))

    current_process = None
    current_cwd = None


# ---------------------------------------------------------------------------
# SSE event stream generator
# ---------------------------------------------------------------------------


async def event_stream() -> AsyncGenerator[str, None]:
    """Async generator that yields SSE-formatted strings to the client."""
    global current_process, current_cwd

    # Send current status on connect
    if current_process is not None and current_process.returncode is None:
        yield format_sse("status", {"state": "running", "cwd": current_cwd or ""})
    else:
        yield format_sse("status", {"state": "idle"})

    # Replay buffered log lines
    for log_event in list(log_buffer):
        yield format_sse("log", log_event)  # type: ignore[arg-type]

    # Main event loop
    while True:
        try:
            event_str = await asyncio.wait_for(event_queue.get(), timeout=15.0)
            yield event_str
        except TimeoutError:
            yield ": heartbeat\n\n"
        except asyncio.CancelledError:
            return


# ---------------------------------------------------------------------------
# FastAPI app factory
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    """Create and return the configured FastAPI application instance."""
    global _template_html

    # Auto-configure logging when GOLEM_DEBUG is set (e.g. from Golem.ps1)
    if os.environ.get("GOLEM_DEBUG", "").lower() in ("1", "true") and not logger.handlers:
        configure_logging(debug=True)

    # Load template HTML at startup; serve a placeholder if missing (Phase 2 creates it)
    template_path = Path(__file__).parent / "ui_template.html"
    if template_path.exists():
        _template_html = template_path.read_text(encoding="utf-8")
    else:
        _template_html = (
            "<!DOCTYPE html><html><head><title>Golem UI</title></head>"
            "<body><p>Dashboard template not found. Phase 2 creates ui_template.html.</p></body></html>"
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        yield
        # Shutdown: terminate active subprocess if alive
        if current_process is not None and current_process.returncode is None:
            try:
                current_process.terminate()
            except ProcessLookupError:
                pass

    app = FastAPI(title="Golem UI", lifespan=lifespan)

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(_template_html)

    @app.post("/api/run")
    async def api_run(req: RunRequest) -> dict[str, str]:
        global current_process, current_cwd

        spec = Path(req.spec_path)

        # Validate extension
        if not spec.suffix.lower() == ".md":
            raise HTTPException(status_code=400, detail="spec_path must be a .md file")

        # Validate existence
        if not spec.exists():
            raise HTTPException(status_code=404, detail=f"Spec file not found: {req.spec_path}")

        # Reject concurrent runs
        if current_process is not None and current_process.returncode is None:
            raise HTTPException(status_code=409, detail="A run is already in progress")

        # Resolve working directory
        resolved_spec = spec.resolve()

        if req.project_root:
            root = Path(req.project_root)
            if not root.is_dir():
                raise HTTPException(status_code=400, detail=f"project_root is not a directory: {req.project_root}")
            cwd = str(root.resolve())
            # Spec may be outside cwd — pass full resolved path
            spec_filename = str(resolved_spec)
        else:
            cwd = str(resolved_spec.parent)
            spec_filename = resolved_spec.name

        # Clear state from previous run
        log_buffer.clear()
        while not event_queue.empty():
            try:
                event_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        # Broadcast run-start status
        await event_queue.put(
            format_sse("status", {"state": "running", "spec": spec_filename, "cwd": cwd})
        )

        # Run golem clean first (await completion; ignore errors if .golem/ absent)
        try:
            clean_proc = await asyncio.create_subprocess_exec(
                "uv", "run", "golem", "clean",
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await clean_proc.wait()
        except OSError:
            pass  # uv not on PATH would be caught later on the run command

        # Spawn the golem run subprocess
        try:
            process = await asyncio.create_subprocess_exec(
                "uv", "run", "golem", "run", spec_filename, "--force",
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            raise HTTPException(status_code=500, detail="'uv' not found on PATH. Install uv: pip install uv")

        current_process = process
        current_cwd = cwd
        logger.info("Run started: spec=%s cwd=%s pid=%d", spec_filename, cwd, process.pid or 0)

        golem_dir = Path(cwd) / ".golem"

        # Start background tasks
        asyncio.create_task(tail_progress_log(golem_dir))
        asyncio.create_task(poll_tasks_json(golem_dir))
        asyncio.create_task(monitor_process(process, cwd))
        asyncio.create_task(stream_subprocess_output(process))

        return {"status": "started", "cwd": cwd}

    @app.get("/api/specs")
    async def api_specs() -> dict[str, list[str]]:
        """Return all .md files found recursively in the project."""
        project_root = Path.cwd().resolve()
        specs: list[str] = []
        skip = {".git", ".golem", ".venv", "node_modules", ".claude", "test-results", "__pycache__"}
        for p in sorted(project_root.rglob("*.md")):
            parts = p.relative_to(project_root).parts
            if any(part.startswith(".") or part in skip for part in parts):
                continue
            full = str(p.resolve()).replace("\\", "/")
            if full not in specs:
                specs.append(full)
        return {"specs": specs}

    @app.post("/api/clean")
    async def api_clean() -> dict[str, str]:
        """Remove .golem/ state directory."""
        import shutil

        golem_dir = Path.cwd().resolve() / ".golem"
        if not golem_dir.exists():
            return {"status": "nothing_to_clean"}
        shutil.rmtree(golem_dir, ignore_errors=True)
        return {"status": "cleaned"}

    @app.get("/api/config")
    async def api_config() -> dict[str, object]:
        """Return the current Golem config as JSON."""
        from dataclasses import asdict

        from golem.config import GolemConfig, load_config

        golem_dir = Path.cwd().resolve() / ".golem"
        if golem_dir.exists():
            config = load_config(golem_dir)
        else:
            config = GolemConfig()
        return asdict(config)

    @app.get("/api/browse/file")
    async def api_browse_file(initial_dir: str = "") -> dict[str, str | None]:
        """Open a native OS file picker dialog filtered to .md files."""
        try:
            path = await asyncio.to_thread(_dialogs.open_file_dialog, initial_dir or None)
        except NotImplementedError:
            raise HTTPException(status_code=501, detail="File dialogs require Windows")
        return {"path": path}

    @app.get("/api/browse/folder")
    async def api_browse_folder(initial_dir: str = "") -> dict[str, str | None]:
        """Open a native OS folder picker dialog."""
        try:
            path = await asyncio.to_thread(_dialogs.open_folder_dialog, initial_dir or None)
        except NotImplementedError:
            raise HTTPException(status_code=501, detail="Folder dialogs require Windows")
        return {"path": path}

    @app.get("/api/events")
    async def api_events() -> StreamingResponse:
        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return app


# ---------------------------------------------------------------------------
# start_server convenience function (called by CLI)
# ---------------------------------------------------------------------------


def configure_logging(debug: bool = False) -> None:
    """Configure the golem.ui logger with formatted console output."""
    level = logging.DEBUG if debug else logging.INFO
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(asctime)s] [UI] %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(handler)
    logger.setLevel(level)


def start_server(
    host: str = "127.0.0.1", port: int = 9664, log_level: str = "warning", debug: bool = False
) -> None:
    """Start the uvicorn server. Blocks until Ctrl+C."""
    import uvicorn

    configure_logging(debug=debug)
    logger.info("Golem UI server starting on %s:%d", host, port)
    app = create_app()
    uvicorn.run(app, host=host, port=port, log_level="info" if debug else log_level)
