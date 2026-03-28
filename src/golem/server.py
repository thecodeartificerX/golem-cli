from __future__ import annotations

import asyncio
import json
import os
from collections import deque
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from golem.ui import format_sse


# ---------------------------------------------------------------------------
# Startup time for uptime tracking
# ---------------------------------------------------------------------------

_startup_time: datetime = datetime.now(tz=UTC)


# ---------------------------------------------------------------------------
# Pydantic request/response models — must be module-level for FastAPI
# ---------------------------------------------------------------------------


class CreateSessionRequest(BaseModel):
    spec_path: str
    project_root: str = ""


class GuidanceRequest(BaseModel):
    text: str


# ---------------------------------------------------------------------------
# Core types
# ---------------------------------------------------------------------------


@dataclass
class SessionState:
    id: str
    spec_path: Path
    status: str = "pending"
    created_at: str = ""
    process: asyncio.subprocess.Process | None = None  # type: ignore[name-defined]
    config: dict[str, object] = field(default_factory=dict)
    event_queue: asyncio.Queue[str] = field(default_factory=asyncio.Queue)
    log_buffer: deque[dict[str, str | None]] = field(default_factory=lambda: deque(maxlen=200))
    background_tasks: list[asyncio.Task[None]] = field(default_factory=list)


class SessionManager:
    """Manages sessions as in-memory state + on-disk metadata."""

    def __init__(self, sessions_dir: Path) -> None:
        self._sessions_dir = sessions_dir
        self._sessions: dict[str, SessionState] = {}

    def create_session(self, session_id: str, spec_path: Path) -> SessionState:
        """Create and register a new session."""
        state = SessionState(
            id=session_id, spec_path=spec_path,
            created_at=datetime.now(tz=UTC).isoformat(),
        )
        self._sessions[session_id] = state
        return state

    def get_session(self, session_id: str) -> SessionState | None:
        """Get a session by ID, or None if not found."""
        return self._sessions.get(session_id)

    def list_sessions(self) -> list[SessionState]:
        """Return all tracked sessions."""
        return list(self._sessions.values())

    def remove_session(self, session_id: str) -> bool:
        """Remove a session from tracking. Returns True if removed."""
        return self._sessions.pop(session_id, None) is not None

    def pause_session(self, session_id: str) -> bool:
        """Pause a running session's subprocess."""
        session = self._sessions.get(session_id)
        if not session or not session.process or session.process.returncode is not None:
            return False
        import signal
        try:
            session.process.send_signal(signal.SIGSTOP if hasattr(signal, "SIGSTOP") else signal.SIGTERM)
            session.status = "paused"
            return True
        except (ProcessLookupError, OSError):
            return False

    def resume_session(self, session_id: str) -> bool:
        """Resume a paused session's subprocess."""
        session = self._sessions.get(session_id)
        if not session or session.status != "paused" or not session.process:
            return False
        import signal
        try:
            session.process.send_signal(signal.SIGCONT if hasattr(signal, "SIGCONT") else signal.SIGTERM)
            session.status = "running"
            return True
        except (ProcessLookupError, OSError):
            return False

    def kill_session(self, session_id: str) -> bool:
        """Kill a session's subprocess."""
        session = self._sessions.get(session_id)
        if not session:
            return False
        if session.process and session.process.returncode is None:
            try:
                session.process.terminate()
            except (ProcessLookupError, OSError):
                pass
        session.status = "failed"
        return True

    def archive_session(self, session_id: str) -> bool:
        """Mark a session as archived."""
        session = self._sessions.get(session_id)
        if not session:
            return False
        session.status = "archived"
        return True


class MergeCoordinator:
    """Stub merge coordinator — implemented in Spec 3."""

    def __init__(self, golem_dir: Path) -> None:
        self._golem_dir = golem_dir

    async def enqueue(self, session_id: str) -> None:
        """Enqueue a session for merge. (Stub — Spec 3)"""

    async def process_queue(self) -> None:
        """Process the merge queue. (Stub — Spec 3)"""

    def get_queue_status(self) -> list[dict[str, str]]:
        """Return current queue state. (Stub — Spec 3)"""
        return []


# ---------------------------------------------------------------------------
# Server lifecycle helpers
# ---------------------------------------------------------------------------


def write_server_json(golem_dir: Path, pid: int, port: int) -> None:
    """Write server.json with PID and port info for CLI discovery."""
    golem_dir.mkdir(parents=True, exist_ok=True)
    (golem_dir / "server.json").write_text(
        json.dumps({"pid": pid, "port": port, "host": "127.0.0.1"}, indent=2),
        encoding="utf-8",
    )


def remove_server_json(golem_dir: Path) -> None:
    """Remove server.json on shutdown."""
    server_json = golem_dir / "server.json"
    if server_json.exists():
        server_json.unlink()


# ---------------------------------------------------------------------------
# Process monitor coroutine (used by session lifecycle — Task 7)
# ---------------------------------------------------------------------------


async def monitor_process(
    session: SessionState,
    on_exit: asyncio.Queue[int] | None = None,
) -> None:
    """Monitor a subprocess and update session status when it exits."""
    if session.process is None:
        return
    returncode = await session.process.wait()
    session.status = "done" if returncode == 0 else "failed"
    if on_exit is not None:
        await on_exit.put(returncode)


# ---------------------------------------------------------------------------
# Cached template HTML
# ---------------------------------------------------------------------------

_template_html: str = ""


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    """Create and return the configured FastAPI application instance."""
    global _template_html, _startup_time

    _startup_time = datetime.now(tz=UTC)

    # Load template HTML at startup
    template_path = Path(__file__).parent / "ui_template.html"
    if template_path.exists():
        _template_html = template_path.read_text(encoding="utf-8")
    else:
        _template_html = (
            "<!DOCTYPE html><html><head><title>Golem Server</title></head>"
            "<body><p>Dashboard template not found.</p></body></html>"
        )

    # Resolve golem_dir — default to cwd / .golem
    golem_dir = Path(os.environ.get("GOLEM_DIR", "")) or Path.cwd() / ".golem"
    sessions_dir = golem_dir / "sessions"
    session_mgr = SessionManager(sessions_dir)
    merge_coordinator = MergeCoordinator(golem_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        yield
        remove_server_json(golem_dir)
        # Terminate any running sessions
        for s in session_mgr.list_sessions():
            if s.process and s.process.returncode is None:  # Still running
                try:
                    s.process.terminate()
                except (ProcessLookupError, OSError):
                    pass

    app = FastAPI(title="Golem Server", lifespan=lifespan)

    # ------------------------------------------------------------------
    # Root + Server status
    # ------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(_template_html)

    @app.get("/api/server/status")
    async def server_status() -> dict[str, object]:
        uptime = (datetime.now(tz=UTC) - _startup_time).total_seconds()
        sessions = session_mgr.list_sessions()
        counts: dict[str, int] = {}
        for s in sessions:
            counts[s.status] = counts.get(s.status, 0) + 1
        return {
            "pid": os.getpid(),
            "port": int(os.environ.get("GOLEM_PORT", 9664)),
            "uptime_seconds": round(uptime, 1),
            "session_counts": counts,
        }

    @app.post("/api/server/stop")
    async def server_stop() -> dict[str, str]:
        # Signal graceful shutdown
        import signal
        os.kill(os.getpid(), signal.SIGTERM)
        return {"status": "stopping"}

    return app
