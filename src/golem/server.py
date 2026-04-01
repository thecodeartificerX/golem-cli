from __future__ import annotations

import asyncio
import json
import os
import sys
from collections import deque
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
import dataclasses
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel
from starlette.requests import Request

from golem.config import GolemConfig
from golem.events import EventBus, FanoutBackend, FileBackend, QueueBackend
from golem.mcp_sse import McpSessionRegistry
from golem.merge import MergeCoordinator
from golem.session import create_session_dir, generate_session_id, read_session, write_session
from golem.tickets import TicketStore

# ---------------------------------------------------------------------------
# SSE helper (moved from legacy ui.py)
# ---------------------------------------------------------------------------


def format_sse(event_type: str, data: dict[str, object]) -> str:
    """Format a Server-Sent Event string with correct wire format."""
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"

if TYPE_CHECKING:
    from golem.tickets import Ticket


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


class TicketStatusPatchRequest(BaseModel):
    status: str
    note: str = "Manual status change via board UI"


class AddRepoRequest(BaseModel):
    path: str


class RepoResponse(BaseModel):
    id: str
    path: str
    name: str
    added_at: str


class CreateEdictRequest(BaseModel):
    title: str
    body: str


# ---------------------------------------------------------------------------
# Board column constants and helpers
# ---------------------------------------------------------------------------

_BOARD_COLUMNS: list[str] = ["pending", "in_progress", "review", "rework", "done", "failed"]

_STATUS_TO_COLUMN: dict[str, str] = {
    "pending": "pending",
    "in_progress": "in_progress",
    "ready_for_review": "review",
    "needs_work": "rework",
    "blocked": "rework",
    "qa_passed": "done",
    "done": "done",
    "approved": "done",
    "failed": "failed",
}

_COLUMN_TO_STATUS: dict[str, str] = {
    "pending": "pending",
    "review": "ready_for_review",
    "rework": "needs_work",
    "done": "done",
    "failed": "failed",
}


def _ticket_to_card(t: Ticket) -> dict[str, object]:
    """Serialize a Ticket to the board card metadata shape."""
    actions = {e.action for e in t.history}
    phase_plan = bool(actions & {"created", "status_changed_to_in_progress"})
    phase_code = bool(actions & {
        "status_changed_to_ready_for_review",
        "status_changed_to_needs_work",
        "status_changed_to_qa_passed",
    })
    phase_qa = bool(actions & {
        "status_changed_to_qa_passed",
        "status_changed_to_done",
        "status_changed_to_approved",
    })
    updated_at = t.history[-1].ts if t.history else ""
    history_count = len(t.history)
    return {
        "id": t.id,
        "title": t.title,
        "status": t.status,
        "priority": t.priority,
        "type": t.type,
        "assigned_to": t.assigned_to,
        "created_by": t.created_by,
        "phase_plan": phase_plan,
        "phase_code": phase_code,
        "phase_qa": phase_qa,
        "updated_at": updated_at,
        "history_count": history_count,
        "session_id": t.session_id,
    }


def _group_by_column(cards: list[dict[str, object]]) -> dict[str, object]:
    """Group card dicts by board column key."""
    columns: dict[str, list[dict[str, object]]] = {col: [] for col in _BOARD_COLUMNS}
    for card in cards:
        status = str(card.get("status", ""))
        col = _STATUS_TO_COLUMN.get(status, "pending")
        columns[col].append(card)
    return {"columns": columns, "column_order": _BOARD_COLUMNS}


# ---------------------------------------------------------------------------
# Core types
# ---------------------------------------------------------------------------


def _make_set_event() -> asyncio.Event:
    """Create an asyncio.Event that starts in the set (unpaused) state."""
    ev = asyncio.Event()
    ev.set()
    return ev


@dataclass
class SessionState:
    id: str
    spec_path: Path
    status: str = "pending"
    created_at: str = ""
    process: asyncio.subprocess.Process | None = None  # type: ignore[name-defined]
    task: asyncio.Task[None] | None = None
    config: dict[str, object] = field(default_factory=dict)
    event_queue: asyncio.Queue[str] = field(default_factory=asyncio.Queue)
    observe_queue: asyncio.Queue[object] = field(default_factory=asyncio.Queue)
    log_buffer: deque[dict[str, str | None]] = field(default_factory=lambda: deque(maxlen=200))
    background_tasks: list[asyncio.Task[None]] = field(default_factory=list)
    resume_event: asyncio.Event = field(default_factory=_make_set_event)


@dataclass
class EdictState:
    id: str
    repo_id: str
    repo_path: str
    title: str
    body: str
    status: str = "pending"
    created_at: str = ""
    pipeline: object | None = None  # PipelineCoordinator at runtime
    task: asyncio.Task | None = None  # type: ignore[type-arg]
    tail_task: asyncio.Task | None = None  # type: ignore[type-arg]
    event_queue: asyncio.Queue[str] = field(default_factory=lambda: asyncio.Queue())
    observe_queue: asyncio.Queue[object] = field(default_factory=lambda: asyncio.Queue())
    log_buffer: deque[dict[str, str | None]] = field(default_factory=lambda: deque(maxlen=200))
    resume_event: asyncio.Event = field(default_factory=asyncio.Event)
    cost_usd: float = 0.0
    pr_url: str | None = None

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = datetime.now(tz=UTC).isoformat()
        self.resume_event.set()


class EdictManager:
    def __init__(self) -> None:
        self._edicts: dict[str, EdictState] = {}

    def create_edict(self, repo_id: str, repo_path: str, title: str, body: str) -> EdictState:
        nums = [int(k.split("-")[1]) for k in self._edicts if k.startswith("EDICT-")]
        next_num = (max(nums) + 1) if nums else 1
        edict_id = f"EDICT-{next_num:03d}"
        state = EdictState(id=edict_id, repo_id=repo_id, repo_path=repo_path, title=title, body=body)
        self._edicts[edict_id] = state
        return state

    def get_edict(self, edict_id: str) -> EdictState | None:
        return self._edicts.get(edict_id)

    def list_edicts(self, repo_id: str | None = None) -> list[EdictState]:
        edicts = list(self._edicts.values())
        if repo_id:
            edicts = [e for e in edicts if e.repo_id == repo_id]
        return edicts

    def remove_edict(self, edict_id: str) -> bool:
        return self._edicts.pop(edict_id, None) is not None


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
        """Pause a running session (subprocess via signal, or task via event)."""
        session = self._sessions.get(session_id)
        if not session:
            return False
        # In-process task path — cooperative pause via event
        if session.task and not session.task.done():
            session.resume_event.clear()
            session.status = "paused"
            return True
        # Subprocess path
        if not session.process or session.process.returncode is not None:
            return False
        import signal
        try:
            session.process.send_signal(signal.SIGSTOP if hasattr(signal, "SIGSTOP") else signal.SIGTERM)
            session.status = "paused"
            return True
        except (ProcessLookupError, OSError):
            return False

    def resume_session(self, session_id: str) -> bool:
        """Resume a paused session (subprocess via signal, or task via event)."""
        session = self._sessions.get(session_id)
        if not session or session.status != "paused":
            return False
        # In-process task path
        if session.task and not session.task.done():
            session.resume_event.set()
            session.status = "running"
            return True
        # Subprocess path
        if not session.process:
            return False
        import signal
        try:
            session.process.send_signal(signal.SIGCONT if hasattr(signal, "SIGCONT") else signal.SIGTERM)
            session.status = "running"
            return True
        except (ProcessLookupError, OSError):
            return False

    def kill_session(self, session_id: str) -> bool:
        """Kill a session's subprocess or cancel its task."""
        session = self._sessions.get(session_id)
        if not session:
            return False
        if session.task and not session.task.done():
            session.task.cancel()
        elif session.process and session.process.returncode is None:
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
    state: SessionState,
    sessions_dir: Path,
    coordinator: MergeCoordinator | None = None,
) -> None:
    """Wait for a session's subprocess to exit and update session.json status."""
    if state.process is None:
        return
    exit_code = await state.process.wait()
    await asyncio.sleep(1.0)  # Let tailing catch final lines

    session_dir = sessions_dir / state.id
    if exit_code == 0:
        state.status = "awaiting_merge"
        if coordinator is not None:
            await coordinator.enqueue(state.id)
    else:
        state.status = "failed"

    # Update session.json on disk
    if (session_dir / "session.json").exists():
        meta = read_session(session_dir)
        meta.status = state.status
        write_session(session_dir, meta)

    await state.event_queue.put(format_sse("status", {"state": state.status, "exit_code": exit_code}))


# ---------------------------------------------------------------------------
# Progress log tailer coroutine (used by session lifecycle — Task 7)
# ---------------------------------------------------------------------------


async def tail_progress_log(state: SessionState, sessions_dir: Path) -> None:
    """Tail a session's progress.log, broadcasting new lines as SSE log events."""
    log_path = sessions_dir / state.id / "progress.log"
    seek_pos = 0

    while True:
        await asyncio.sleep(0.5)
        # Support both subprocess-based and in-process (asyncio.Task) sessions
        if state.process is not None:
            if state.process.returncode is not None:
                break
        elif state.task is not None:
            if state.task.done():
                break
        else:
            break  # neither process nor task — nothing to tail
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
            event_dict: dict[str, str | None] = {"message": raw_line, "raw": raw_line}
            state.log_buffer.append(event_dict)
            await state.event_queue.put(format_sse("log", event_dict))


# ---------------------------------------------------------------------------
# In-process session runner
# ---------------------------------------------------------------------------


class _TeeWriter:
    """Write to two streams simultaneously (stderr + log file)."""

    def __init__(self, primary: object, secondary: object) -> None:
        self._primary = primary
        self._secondary = secondary

    def write(self, s: str) -> int:
        self._primary.write(s)  # type: ignore[union-attr]
        try:
            self._secondary.write(s)  # type: ignore[union-attr]
            self._secondary.flush()  # type: ignore[union-attr]
        except (OSError, ValueError):
            pass  # Don't crash if log file is closed/full
        return len(s)

    def flush(self) -> None:
        self._primary.flush()  # type: ignore[union-attr]
        try:
            self._secondary.flush()  # type: ignore[union-attr]
        except (OSError, ValueError):
            pass

    def fileno(self) -> int:
        return self._primary.fileno()  # type: ignore[union-attr]

    def isatty(self) -> bool:
        return False


async def run_session(
    spec_path: Path,
    project_root: Path,
    config: GolemConfig,
    event_bus: EventBus,
    golem_dir: Path,
    server_url: str = "",
) -> None:
    """Run the full Golem pipeline in-process (replaces subprocess spawning)."""
    import sys
    import time

    from golem.conductor import classify_spec
    from golem.events import SessionComplete, SessionStart
    from golem.planner import run_planner
    from golem.progress import ProgressLogger
    from golem.tech_lead import run_tech_lead
    from golem.junior_dev import spawn_junior_dev

    # Capture stderr to session.log for post-mortem debugging
    golem_dir.mkdir(parents=True, exist_ok=True)
    session_log_path = golem_dir / "session.log"
    session_log_file = open(session_log_path, "a", encoding="utf-8")  # noqa: SIM115
    original_stderr = sys.stderr
    sys.stderr = _TeeWriter(original_stderr, session_log_file)

    start = time.monotonic()
    try:
        spec_content = spec_path.read_text(encoding="utf-8")

        await event_bus.emit(SessionStart(
            spec_path=str(spec_path),
            complexity="",
            config_snapshot=dataclasses.asdict(config),
        ))

        # Run conductor classification
        if config.conductor_enabled:
            result = classify_spec(spec_content)
            config.apply_complexity_profile(result.complexity)
            await event_bus.emit(SessionStart(
                spec_path=str(spec_path),
                complexity=result.complexity,
                config_snapshot={},
            ))

        # Create directories (already ensured above for session.log)
        (golem_dir / "tickets").mkdir(exist_ok=True)
        (golem_dir / "plans").mkdir(exist_ok=True)
        (golem_dir / "research").mkdir(exist_ok=True)
        (golem_dir / "references").mkdir(exist_ok=True)

        progress = ProgressLogger(golem_dir)
        progress.log_planner_start()

        planner_result = await run_planner(spec_path, golem_dir, config, project_root, event_bus=event_bus, server_url=server_url)
        ticket_id = planner_result.ticket_id
        progress.log_planner_complete(ticket_id)

        if not config.skip_tech_lead:
            progress.log_tech_lead_start(ticket_id)
            await run_tech_lead(ticket_id, golem_dir, config, project_root, event_bus=event_bus, server_url=server_url)
            elapsed = time.monotonic() - start
            progress.log_tech_lead_complete(elapsed_s=elapsed)
        else:
            from golem.tickets import TicketStore
            store = TicketStore(golem_dir / "tickets")
            ticket = await store.read(ticket_id)
            await spawn_junior_dev(ticket, str(project_root), config, golem_dir, event_bus=event_bus, server_url=server_url)

        total_cost = progress.sum_agent_costs()
        duration = time.monotonic() - start
        progress.log_run_cost_summary(total_cost)

        await event_bus.emit(SessionComplete(
            status="awaiting_merge", cost_usd=total_cost, duration_s=duration, error="",
        ))

    except Exception as e:
        duration = time.monotonic() - start
        await event_bus.emit(SessionComplete(
            status="failed", cost_usd=0.0, duration_s=duration, error=str(e),
        ))
        raise
    finally:
        sys.stderr = original_stderr
        session_log_file.close()


def _on_session_done(task: asyncio.Task[None], state: SessionState, mgr: SessionManager) -> None:
    """Callback when an in-process session task finishes."""
    error_msg: str | None = None
    if task.cancelled():
        state.status = "failed"
        error_msg = "Session task was cancelled"
    elif task.exception():
        state.status = "failed"
        error_msg = str(task.exception())
    else:
        state.status = "awaiting_merge"

    # Persist final status to session.json on disk
    session_dir = mgr._sessions_dir / state.id
    if (session_dir / "session.json").exists():
        try:
            meta = read_session(session_dir)
            meta.status = state.status
            if error_msg:
                meta.error = error_msg
            write_session(session_dir, meta)
        except OSError:
            pass  # Best-effort — don't crash the callback


# ---------------------------------------------------------------------------
# Cached template HTML
# ---------------------------------------------------------------------------

_template_html: str = ""
_dashboard_html: str = ""


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    """Create and return the configured FastAPI application instance."""
    global _template_html, _dashboard_html, _startup_time

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

    # Load dashboard template
    dashboard_path = Path(__file__).parent / "dashboard.html"
    if dashboard_path.exists():
        _dashboard_html = dashboard_path.read_text(encoding="utf-8")

    # Resolve golem_dir — default to cwd / .golem
    golem_dir = Path(os.environ.get("GOLEM_DIR", "")) or Path.cwd() / ".golem"
    sessions_dir = golem_dir / "sessions"
    session_mgr = SessionManager(sessions_dir)
    mcp_registry = McpSessionRegistry()
    merge_coordinator = MergeCoordinator(golem_dir, session_mgr)

    # Determine registry path for repo management
    from golem.repos import RepoRegistry
    registry_path_str = os.environ.get("GOLEM_REGISTRY_PATH", "")
    if not registry_path_str:
        registry_path_str = str(Path.home() / ".golem" / "repos.json")
    registry_path = Path(registry_path_str)
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    repo_registry = RepoRegistry(registry_path)

    edict_mgr = EdictManager()

    # Queues for aggregate SSE stream (one per connected client)
    _aggregate_queues: set[asyncio.Queue[str]] = set()

    # Restart recovery: restore non-archived sessions from disk as paused.
    # Only runs when GOLEM_DIR is explicitly set (non-empty), to avoid accidentally
    # picking up test artifacts from CWD-relative paths.
    # Only restores sessions that have a copied spec.md (written by create_session_dir).
    _golem_dir_env = os.environ.get("GOLEM_DIR", "")
    if _golem_dir_env and sessions_dir.exists():
        for session_json in sessions_dir.glob("*/session.json"):
            session_dir = session_json.parent
            spec_copy = session_dir / "spec.md"
            if not spec_copy.exists():
                # Not a real session dir — skip
                continue
            try:
                meta = read_session(session_dir)
                if meta.status not in ("archived",) and meta.id:
                    existing = session_mgr.get_session(meta.id)
                    if existing is None:
                        restored = session_mgr.create_session(meta.id, Path(meta.spec_path))
                        restored.status = "paused"
            except Exception:  # noqa: BLE001
                pass

    # Stale mid-merge cleanup: entries with pr_open but no running process → failed
    try:
        entries = merge_coordinator._read_queue()
        changed = False
        for entry in entries:
            if entry.status == "pr_open":
                sess = session_mgr.get_session(entry.session_id)
                if sess is None or sess.process is None:
                    entry.status = "failed"
                    if sess is not None:
                        sess.status = "failed"
                    changed = True
        if changed:
            merge_coordinator._write_queue(entries)
    except Exception:  # noqa: BLE001
        pass

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        # Edict startup recovery: scan all registered repos for persisted edicts so that
        # IDs don't restart from EDICT-001 after a server restart and old edicts remain
        # accessible via the API.
        try:
            repos = await repo_registry.list_repos()
            for repo in repos:
                repo_edicts_dir = Path(repo.path) / ".golem" / "edicts"
                if not repo_edicts_dir.exists():
                    continue
                for edict_json in sorted(repo_edicts_dir.glob("EDICT-*.json")):
                    try:
                        data = json.loads(edict_json.read_text(encoding="utf-8"))
                        edict_id = data.get("id", "")
                        if not edict_id or edict_mgr.get_edict(edict_id) is not None:
                            continue
                        state = EdictState(
                            id=edict_id,
                            repo_id=repo.id,
                            repo_path=repo.path,
                            title=data.get("title", ""),
                            body=data.get("body", ""),
                            status=data.get("status", "pending"),
                            created_at=data.get("created_at", ""),
                        )
                        edict_mgr._edicts[edict_id] = state
                    except Exception:  # noqa: BLE001
                        pass
        except Exception:  # noqa: BLE001
            pass

        # Start conflict scanner background task
        async def _on_new_conflicts(conflicts: list) -> None:
            for c in conflicts:
                event_str = format_sse("conflict", dataclasses.asdict(c) if hasattr(c, "__dataclass_fields__") else c)
                for q in list(_aggregate_queues):
                    await q.put(event_str)

        _scanner_task = asyncio.create_task(
            merge_coordinator.run_conflict_scanner(on_new_conflicts=_on_new_conflicts)
        )

        try:
            yield
        finally:
            _scanner_task.cancel()
            remove_server_json(golem_dir)
            # Terminate any running sessions
            for s in session_mgr.list_sessions():
                if s.task and not s.task.done():
                    s.task.cancel()
                elif s.process and s.process.returncode is None:  # Still running
                    try:
                        s.process.terminate()
                    except (ProcessLookupError, OSError):
                        pass
            # Cancel any running edict pipelines and their log tailers
            for state in edict_mgr.list_edicts():
                if state.task and not state.task.done():
                    state.task.cancel()
                if state.tail_task and not state.tail_task.done():
                    state.tail_task.cancel()

    app = FastAPI(title="Golem Server", lifespan=lifespan)

    # ------------------------------------------------------------------
    # Root + Server status
    # ------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(_dashboard_html or _template_html)

    @app.get("/legacy", response_class=HTMLResponse)
    async def legacy_ui() -> HTMLResponse:
        return HTMLResponse(_template_html)

    @app.get("/api/server/status")
    async def server_status() -> dict[str, object]:
        uptime = (datetime.now(tz=UTC) - _startup_time).total_seconds()
        sessions = session_mgr.list_sessions()
        session_counts: dict[str, int] = {}
        for s in sessions:
            session_counts[s.status] = session_counts.get(s.status, 0) + 1

        edict_counts: dict[str, int] = {}
        for e in edict_mgr.list_edicts():
            edict_counts[e.status] = edict_counts.get(e.status, 0) + 1

        repos = await repo_registry.list_repos()

        return {
            "pid": os.getpid(),
            "port": int(os.environ.get("GOLEM_PORT", 7665)),
            "uptime_seconds": round(uptime, 1),
            "repo_count": len(repos),
            "edict_counts": edict_counts,
            "session_counts": session_counts,
        }

    @app.post("/api/server/stop")
    async def server_stop() -> dict[str, str]:
        # Signal graceful shutdown
        import signal
        os.kill(os.getpid(), signal.SIGTERM)
        return {"status": "stopping"}

    # ------------------------------------------------------------------
    # Session CRUD
    # ------------------------------------------------------------------

    @app.post("/api/sessions")
    async def create_session(req: CreateSessionRequest) -> dict[str, str]:
        """Create a session (directory + metadata) without starting the pipeline."""
        spec = Path(req.spec_path)
        if not spec.exists():
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail=f"Spec not found: {req.spec_path}")

        project_root = Path(req.project_root) if req.project_root else spec.resolve().parent
        session_id = generate_session_id(spec, sessions_dir)

        # Create session directory structure
        sessions_dir.mkdir(parents=True, exist_ok=True)
        session_dir = create_session_dir(sessions_dir, session_id, spec)

        state = session_mgr.create_session(session_id, spec)
        state.status = "created"
        state.config["project_root"] = str(project_root)

        # Persist "created" status to session.json on disk
        if (session_dir / "session.json").exists():
            meta = read_session(session_dir)
            meta.status = "created"
            write_session(session_dir, meta)

        # Register MCP tools for this session using the canonical factory functions.
        from golem.tools import build_tool_registry
        from golem.tool_registry import ToolContext
        session_config = GolemConfig()
        session_config.session_id = session_id
        tool_reg = build_tool_registry(session_dir, session_config, project_root)

        # Tech lead gets the full tool set (all 12 tools).
        tl_ctx = ToolContext(golem_dir=session_dir, project_root=project_root, agent_type="tech_lead")
        mcp_registry.register(session_id, tool_reg.get_tools_for_agent("tech_lead", tl_ctx))

        # Junior dev gets the writer tool set (run_qa, update_ticket, read_ticket, memory tools).
        jd_ctx = ToolContext(golem_dir=session_dir, project_root=project_root, agent_type="writer")
        mcp_registry.register(f"{session_id}-jd", tool_reg.get_tools_for_agent("writer", jd_ctx))

        return {"session_id": session_id, "status": "created"}

    @app.post("/api/sessions/{session_id}/start")
    async def start_session(session_id: str) -> dict[str, str]:
        """Start the pipeline for a previously created session."""
        from fastapi import HTTPException
        from golem.config import load_config

        state = session_mgr.get_session(session_id)
        if state is None:
            raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")
        if state.status not in ("created", "pending"):
            raise HTTPException(status_code=400, detail=f"Session '{session_id}' is already {state.status}")

        session_dir = sessions_dir / session_id
        project_root = Path(str(state.config.get("project_root", ""))) if state.config.get("project_root") else state.spec_path.resolve().parent

        state.status = "running"

        # Persist "running" status to session.json on disk
        if (session_dir / "session.json").exists():
            meta = read_session(session_dir)
            meta.status = "running"
            write_session(session_dir, meta)

        # Create EventBus with dual backend: QueueBackend for SSE + FileBackend for events.jsonl
        events_jsonl = session_dir / "events.jsonl"
        event_bus = EventBus(
            FanoutBackend([QueueBackend(state.observe_queue), FileBackend(events_jsonl)]),
            session_id=session_id,
        )

        # Load config for this session
        config = load_config(session_dir) if (session_dir / "config.json").exists() else GolemConfig()
        config.session_id = session_id

        # Write security_allowlist.json so hooks can read it
        if config.security_allowlist:
            allowlist_path = session_dir / "security_allowlist.json"
            with open(allowlist_path, "w", encoding="utf-8") as f:
                json.dump({"commands": config.security_allowlist}, f)

        # Run session in-process as async task
        server_url = f"http://127.0.0.1:{os.environ.get('GOLEM_PORT', '7665')}"
        task = asyncio.create_task(
            run_session(state.spec_path, project_root, config, event_bus, session_dir, server_url=server_url)
        )
        state.task = task
        task.add_done_callback(lambda t: _on_session_done(t, state, session_mgr))
        state.background_tasks.append(task)

        # Still tail progress.log for legacy SSE events endpoint
        state.background_tasks.append(asyncio.create_task(tail_progress_log(state, sessions_dir)))

        return {"session_id": session_id, "status": "running"}

    @app.get("/api/sessions")
    async def list_sessions() -> list[dict[str, object]]:
        sessions = session_mgr.list_sessions()
        result: list[dict[str, object]] = []
        for s in sessions:
            entry: dict[str, object] = {
                "id": s.id,
                "status": s.status,
                "spec_path": str(s.spec_path),
                "created_at": s.created_at,
                "complexity": s.config.get("complexity", "") if s.config else "",
                "cost_usd": s.config.get("cost_usd", None) if s.config else None,
            }
            result.append(entry)
        return result

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, object]:
        from fastapi import HTTPException

        state = session_mgr.get_session(session_id)
        if not state:
            raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")
        return {
            "id": state.id,
            "status": state.status,
            "spec_path": str(state.spec_path),
            "pid": state.process.pid if state.process else None,
        }

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str, keep_files: bool = False) -> dict[str, str]:
        """Kill, archive, remove from memory, and optionally delete files."""
        from fastapi import HTTPException
        from golem.session import delete_session_dir

        if not session_mgr.get_session(session_id):
            raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")
        session_mgr.kill_session(session_id)
        if not keep_files:
            delete_session_dir(sessions_dir, session_id)
        mcp_registry.unregister(session_id)
        mcp_registry.unregister(f"{session_id}-jd")
        session_mgr.remove_session(session_id)
        return {"status": "deleted", "session_id": session_id}

    @app.post("/api/sessions/cleanup")
    async def cleanup_sessions() -> dict[str, object]:
        """Bulk-delete all finished sessions (archived, failed, merged, awaiting_merge)."""
        from golem.session import delete_session_dir

        cleanable = {"archived", "failed", "merged", "awaiting_merge"}
        removed: list[str] = []
        for s in list(session_mgr.list_sessions()):
            if s.status in cleanable:
                session_mgr.kill_session(s.id)
                delete_session_dir(sessions_dir, s.id)
                mcp_registry.unregister(s.id)
                mcp_registry.unregister(f"{s.id}-jd")
                session_mgr.remove_session(s.id)
                removed.append(s.id)
        return {"removed": removed, "count": len(removed)}

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    @app.post("/api/sessions/{session_id}/pause")
    async def pause_session(session_id: str) -> dict[str, str]:
        from fastapi import HTTPException

        if not session_mgr.pause_session(session_id):
            raise HTTPException(status_code=400, detail="Cannot pause session")
        return {"status": "paused", "session_id": session_id}

    @app.post("/api/sessions/{session_id}/resume")
    async def resume_session(session_id: str) -> dict[str, str]:
        from fastapi import HTTPException

        if not session_mgr.resume_session(session_id):
            raise HTTPException(status_code=400, detail="Cannot resume session")
        return {"status": "running", "session_id": session_id}

    @app.post("/api/sessions/{session_id}/guidance")
    async def send_guidance(session_id: str, req: GuidanceRequest) -> dict[str, object]:
        from fastapi import HTTPException
        from golem.tickets import Ticket, TicketContext, TicketStore

        state = session_mgr.get_session(session_id)
        if not state:
            raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

        session_dir = sessions_dir / session_id
        tickets_dir = session_dir / "tickets"
        tickets_dir.mkdir(parents=True, exist_ok=True)
        store = TicketStore(tickets_dir)

        ticket = Ticket(
            id="",
            type="guidance",
            title="Operator Guidance",
            status="pending",
            priority="high",
            created_by="operator",
            assigned_to="tech_lead",
            context=TicketContext(),
            session_id=session_id,
        )
        ticket_id = await store.create(ticket)
        await store.update(
            ticket_id=ticket_id,
            status="pending",
            note=req.text,
            agent="operator",
        )
        return {"ok": True, "ticket_id": ticket_id}

    # ------------------------------------------------------------------
    # SSE event streams
    # ------------------------------------------------------------------

    @app.get("/api/sessions/{session_id}/events")
    async def session_events(session_id: str) -> StreamingResponse:
        from fastapi import HTTPException

        state = session_mgr.get_session(session_id)
        if not state:
            raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

        async def _stream() -> AsyncGenerator[str, None]:
            # Replay buffer
            for event in list(state.log_buffer):
                yield format_sse("log", event)
            # Stream new events
            while True:
                try:
                    event_str = await asyncio.wait_for(state.event_queue.get(), timeout=15.0)
                    yield event_str
                except TimeoutError:
                    yield ": heartbeat\n\n"
                except asyncio.CancelledError:
                    return

        return StreamingResponse(
            _stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    @app.get("/api/sessions/{session_id}/events/snapshot")
    async def get_events_snapshot(session_id: str) -> JSONResponse:
        """Return all recorded events for a session as a JSON array."""
        state = session_mgr.get_session(session_id)
        if state is None:
            return JSONResponse({"error": "session not found"}, status_code=404)
        events_path = sessions_dir / session_id / "events.jsonl"
        if not events_path.exists():
            return JSONResponse([], status_code=200)
        events: list[dict[str, object]] = []
        try:
            with open(events_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        events.append(json.loads(line))
        except (OSError, json.JSONDecodeError):
            return JSONResponse({"error": "failed to read events"}, status_code=500)
        return JSONResponse(events, status_code=200)

    @app.get("/api/events")
    async def aggregate_events() -> StreamingResponse:
        aggregate_queue: asyncio.Queue[str] = asyncio.Queue()
        _aggregate_queues.add(aggregate_queue)

        async def _stream() -> AsyncGenerator[str, None]:
            try:
                yield format_sse("status", {"state": "connected"})
                while True:
                    try:
                        event_str = await asyncio.wait_for(aggregate_queue.get(), timeout=15.0)
                        yield event_str
                    except TimeoutError:
                        yield ": heartbeat\n\n"
                    except asyncio.CancelledError:
                        return
            finally:
                _aggregate_queues.discard(aggregate_queue)

        return StreamingResponse(
            _stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    # ------------------------------------------------------------------
    # Typed event streams (observe)
    # ------------------------------------------------------------------

    @app.get("/api/sessions/{session_id}/observe")
    async def session_observe(session_id: str) -> StreamingResponse:
        """SSE stream of typed GolemEvents for a session."""
        from fastapi import HTTPException
        from golem.events import GolemEvent

        state = session_mgr.get_session(session_id)
        if not state:
            raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

        async def _stream() -> AsyncGenerator[str, None]:
            while True:
                try:
                    event = await asyncio.wait_for(state.observe_queue.get(), timeout=15.0)
                    if isinstance(event, GolemEvent):
                        yield format_sse("agent_event", event.to_dict())
                    else:
                        yield format_sse("agent_event", {"raw": str(event)})
                except TimeoutError:
                    yield ": heartbeat\n\n"
                except asyncio.CancelledError:
                    return

        return StreamingResponse(
            _stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    @app.get("/api/sessions/{session_id}/agents")
    async def session_agents(session_id: str) -> dict[str, object]:
        """Return current agent tree state for a session."""
        from fastapi import HTTPException

        state = session_mgr.get_session(session_id)
        if not state:
            raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

        return {
            "session_id": session_id,
            "status": state.status,
            "has_task": state.task is not None,
            "task_done": state.task.done() if state.task else None,
        }

    # ------------------------------------------------------------------
    # Per-session data endpoints
    # ------------------------------------------------------------------

    @app.get("/api/sessions/{session_id}/tickets")
    async def session_tickets(
        session_id: str,
        view: str = "",
    ) -> list[dict[str, object]] | dict[str, object]:
        from fastapi import HTTPException
        from golem.tickets import TicketStore

        state = session_mgr.get_session(session_id)
        if not state:
            raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")
        tickets_dir = sessions_dir / session_id / "tickets"
        if not tickets_dir.exists():
            if view == "board":
                return _group_by_column([])
            return []
        store = TicketStore(tickets_dir)
        tickets = await store.list_tickets()
        cards = [_ticket_to_card(t) for t in tickets]
        if view == "board":
            return _group_by_column(cards)
        return cards

    @app.patch("/api/sessions/{session_id}/tickets/{ticket_id}/status")
    async def patch_ticket_status(
        session_id: str,
        ticket_id: str,
        req: TicketStatusPatchRequest,
    ) -> dict[str, object]:
        from fastapi import HTTPException
        from golem.tickets import TicketStore

        state = session_mgr.get_session(session_id)
        if not state:
            raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")
        if req.status == "in_progress":
            raise HTTPException(status_code=400, detail="Cannot set in_progress via manual update")
        known = set(_STATUS_TO_COLUMN.keys())
        if req.status not in known:
            raise HTTPException(status_code=400, detail=f"Unknown status '{req.status}'")
        tickets_dir = sessions_dir / session_id / "tickets"
        if not tickets_dir.exists():
            raise HTTPException(status_code=404, detail=f"No tickets for session {session_id}")
        store = TicketStore(tickets_dir)
        try:
            await store.update(ticket_id=ticket_id, status=req.status, note=req.note, agent="operator")
            ticket = await store.read(ticket_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Ticket not found: {ticket_id}")
        return _ticket_to_card(ticket)

    @app.get("/api/sessions/{session_id}/diff")
    async def session_diff(session_id: str) -> dict[str, str]:
        import subprocess
        from fastapi import HTTPException

        state = session_mgr.get_session(session_id)
        if not state:
            raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")
        branch_prefix = f"golem/{session_id}"
        try:
            result = subprocess.run(
                ["git", "diff", f"main...{branch_prefix}/group-a/integration"],
                capture_output=True, text=True, timeout=30, check=False,
            )
            diff_text = result.stdout if result.returncode == 0 else ""
        except (FileNotFoundError, subprocess.TimeoutExpired):
            diff_text = ""
        return {"diff": diff_text, "session_id": session_id}

    @app.get("/api/sessions/{session_id}/cost")
    async def session_cost(session_id: str) -> dict[str, object]:
        import re
        from fastapi import HTTPException

        state = session_mgr.get_session(session_id)
        if not state:
            raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")
        log_path = sessions_dir / session_id / "progress.log"
        total = 0.0
        roles: list[dict[str, object]] = []
        if log_path.exists():
            for line in log_path.read_text(encoding="utf-8").splitlines():
                m = re.search(r"AGENT_COST\s+role=(\S+).*cost=\$([0-9.]+).*tokens_in=([0-9]+).*tokens_out=([0-9]+)", line)
                if m:
                    cost_val = float(m.group(2))
                    total += cost_val
                    roles.append({
                        "role": m.group(1),
                        "cost": round(cost_val, 6),
                        "tokens_in": int(m.group(3)),
                        "tokens_out": int(m.group(4)),
                    })
                else:
                    m2 = re.search(r"AGENT_COST.*cost=\$([0-9.]+)", line)
                    if m2:
                        cost_val = float(m2.group(1))
                        total += cost_val
        return {"session_id": session_id, "roles": roles, "total": round(total, 6)}

    @app.get("/api/sessions/{session_id}/plan")
    async def session_plan(session_id: str) -> dict[str, str]:
        from fastapi import HTTPException

        state = session_mgr.get_session(session_id)
        if not state:
            raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")
        plan_path = sessions_dir / session_id / "plans" / "overview.md"
        if not plan_path.exists():
            return {"session_id": session_id, "content": ""}
        return {"session_id": session_id, "content": plan_path.read_text(encoding="utf-8")}

    # ------------------------------------------------------------------
    # Preserved endpoints (ported from ui.py)
    # ------------------------------------------------------------------

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

    @app.get("/api/browse/file")
    async def api_browse_file(initial_dir: str = "") -> dict[str, str | None]:
        from golem import dialogs as _dialogs
        try:
            path = await asyncio.to_thread(_dialogs.open_file_dialog, initial_dir or None)
        except NotImplementedError:
            from fastapi import HTTPException
            raise HTTPException(status_code=501, detail="File dialogs require Windows")
        return {"path": path}

    @app.get("/api/browse/folder")
    async def api_browse_folder(initial_dir: str = "") -> dict[str, str | None]:
        from golem import dialogs as _dialogs
        try:
            path = await asyncio.to_thread(_dialogs.open_folder_dialog, initial_dir or None)
        except NotImplementedError:
            from fastapi import HTTPException
            raise HTTPException(status_code=501, detail="Folder dialogs require Windows")
        return {"path": path}

    @app.get("/api/config")
    async def api_config() -> dict[str, object]:
        from dataclasses import asdict
        from golem.config import GolemConfig, load_config

        if golem_dir.exists():
            config = load_config(golem_dir)
        else:
            config = GolemConfig()
        return asdict(config)

    @app.post("/api/preflight")
    async def api_preflight(req: CreateSessionRequest) -> dict[str, object]:
        from golem.config import GolemConfig, load_config, resolve_plugins_for_role, run_preflight_checks

        spec = Path(req.spec_path)
        if not spec.exists():
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail=f"Spec not found: {req.spec_path}")

        project_root = Path(req.project_root).resolve() if req.project_root else spec.resolve().parent
        config_dir = project_root / ".golem"
        config = load_config(config_dir) if (config_dir / "config.json").exists() else GolemConfig()

        golem_tools: dict[str, list[str]] = {
            "planner": ["create_ticket", "update_ticket", "read_ticket", "list_tickets"],
            "tech_lead": ["create_ticket", "update_ticket", "read_ticket", "list_tickets",
                         "run_qa", "create_worktree", "merge_branches", "commit_worktree"],
            "writer": ["run_qa", "update_ticket"],
        }

        roles: dict[str, dict[str, object]] = {}
        for role in ("planner", "tech_lead", "writer"):
            sources = config.agent_setting_sources.get(role, config.setting_sources)
            extras = config.extra_mcp_servers.get(role, {})
            proj_plugins, usr_plugins = resolve_plugins_for_role(config, role, project_root)
            roles[role] = {
                "setting_sources": sources,
                "golem_tools": golem_tools[role],
                "extra_mcps": {n: s for n, s in extras.items()},
                "project_plugins": proj_plugins,
                "user_plugins": usr_plugins,
            }

        errors, warnings_list, infos = run_preflight_checks(config, project_root)
        return {
            "spec": str(spec),
            "project_root": str(project_root),
            "roles": roles,
            "pitfalls": {"errors": errors, "warnings": warnings_list, "infos": infos},
            "ready": len(errors) == 0,
        }

    # ------------------------------------------------------------------
    # MCP-over-SSE endpoints
    # ------------------------------------------------------------------

    @app.get("/mcp/{session_id}/sse")
    async def mcp_sse_endpoint(session_id: str, request: Request) -> StreamingResponse:
        """SSE stream for MCP-over-SSE protocol."""
        from fastapi import HTTPException
        if not mcp_registry.has_session(session_id):
            raise HTTPException(status_code=404, detail=f"MCP session not registered: {session_id}")

        # Build absolute message URL from the request's base
        base_url = str(request.base_url).rstrip("/")
        message_url = f"{base_url}/mcp/{session_id}/message"

        async def event_stream() -> AsyncGenerator[str, None]:
            yield f"event: endpoint\ndata: {message_url}\n\n"
            while mcp_registry.has_session(session_id):
                yield ": keepalive\n\n"
                await asyncio.sleep(15)

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @app.post("/mcp/{session_id}/message")
    async def mcp_message_endpoint(session_id: str, request: Request) -> dict[str, object]:
        """Handle JSONRPC message for MCP-over-SSE."""
        from fastapi import HTTPException
        if not mcp_registry.has_session(session_id):
            raise HTTPException(status_code=404, detail=f"MCP session not registered: {session_id}")
        body = await request.json()
        return await mcp_registry.handle_message(session_id, body)

    # ------------------------------------------------------------------
    # Merge queue endpoints
    # ------------------------------------------------------------------

    @app.get("/api/merge-queue")
    async def get_merge_queue() -> list[dict[str, object]]:
        entries = merge_coordinator._read_queue()
        return [dataclasses.asdict(e) for e in entries]

    @app.post("/api/merge-queue/{session_id}")
    async def enqueue_session(session_id: str) -> dict[str, str]:
        await merge_coordinator.enqueue(session_id)
        return {"status": "queued"}

    @app.post("/api/merge-queue/{session_id}/approve")
    async def approve_session(session_id: str) -> dict[str, str]:
        await merge_coordinator.merge_pr(session_id)
        await merge_coordinator.rebase_queued(session_id)
        return {"status": "merged"}

    @app.delete("/api/merge-queue/{session_id}")
    async def dequeue_session(session_id: str) -> dict[str, str]:
        await merge_coordinator.dequeue(session_id)
        return {"status": "removed"}

    @app.get("/api/conflicts")
    async def get_conflicts() -> list[dict[str, object]]:
        conflicts = await merge_coordinator.detect_conflicts()
        return [dataclasses.asdict(c) for c in conflicts]

    # ------------------------------------------------------------------
    # Aggregate stats + unified history
    # ------------------------------------------------------------------

    @app.get("/api/stats")
    async def aggregate_stats() -> dict[str, object]:
        import re
        from golem.tickets import TicketStore

        sessions = session_mgr.list_sessions()
        session_counts: dict[str, int] = {}
        for s in sessions:
            session_counts[s.status] = session_counts.get(s.status, 0) + 1

        total_cost = 0.0
        ticket_done = 0
        ticket_failed = 0
        ticket_total = 0

        for s in sessions:
            # Cost from progress.log
            log_path = sessions_dir / s.id / "progress.log"
            if log_path.exists():
                for line in log_path.read_text(encoding="utf-8").splitlines():
                    m = re.search(r"AGENT_COST.*cost=\$([0-9.]+)", line)
                    if m:
                        total_cost += float(m.group(1))
            # Tickets
            tickets_dir_s = sessions_dir / s.id / "tickets"
            if tickets_dir_s.exists():
                store = TicketStore(tickets_dir_s)
                try:
                    tickets = await store.list_tickets()
                    ticket_total += len(tickets)
                    for t in tickets:
                        if t.status in ("done", "approved", "qa_passed"):
                            ticket_done += 1
                        elif t.status in ("needs_work", "blocked", "failed"):
                            ticket_failed += 1
                except Exception:  # noqa: BLE001
                    pass

        pass_rate = (ticket_done / ticket_total) if ticket_total > 0 else 0.0
        active_sessions = sum(1 for s in sessions if s.status == "running")

        return {
            "session_counts": session_counts,
            "total_cost": round(total_cost, 6),
            "ticket_pass_rate": round(pass_rate, 4),
            "ticket_counts": {"done": ticket_done, "failed": ticket_failed, "total": ticket_total},
            "active_sessions": active_sessions,
        }

    @app.get("/api/history")
    async def aggregate_history(session_id: str = "") -> list[dict[str, str]]:
        entries: list[dict[str, str]] = []

        target_sessions = (
            [session_mgr.get_session(session_id)] if session_id else session_mgr.list_sessions()
        )

        for s in target_sessions:
            if s is None:
                continue
            log_path = sessions_dir / s.id / "progress.log"
            if not log_path.exists():
                continue
            try:
                for line in log_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    # Lines may be prefixed with timestamp like "[2026-03-28T09:39:35] message"
                    # Parse out timestamp if present
                    timestamp = ""
                    message = line
                    import re
                    m = re.match(r"^\[([^\]]+)\]\s*(.*)", line)
                    if m:
                        timestamp = m.group(1)
                        message = m.group(2)
                    entries.append({"session_id": s.id, "timestamp": timestamp, "message": message})
            except OSError:
                pass

        entries.sort(key=lambda e: e["timestamp"])
        return entries

    # ------------------------------------------------------------------
    # Task 4.1: Repo management endpoints
    # ------------------------------------------------------------------

    @app.get("/api/repos")
    async def list_repos() -> list[dict[str, str]]:
        repos = await repo_registry.list_repos()
        return [r.to_dict() for r in repos]

    @app.post("/api/repos", status_code=201)
    async def add_repo(req: AddRepoRequest) -> dict[str, str]:
        from fastapi import HTTPException
        try:
            repo = await repo_registry.add(req.path)
            return repo.to_dict()
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.delete("/api/repos/{repo_id}", status_code=204)
    async def remove_repo(repo_id: str) -> None:
        from fastapi import HTTPException
        removed = await repo_registry.remove(repo_id)
        if not removed:
            raise HTTPException(status_code=404, detail="Repo not found")

    # ------------------------------------------------------------------
    # Task 4.2: Edict CRUD endpoints
    # ------------------------------------------------------------------

    @app.get("/api/repos/{repo_id}/edicts")
    async def list_edicts(repo_id: str) -> list[dict[str, object]]:
        return [
            {
                "id": e.id, "repo_id": e.repo_id, "title": e.title, "body": e.body,
                "status": e.status, "created_at": e.created_at, "cost_usd": e.cost_usd,
                "pr_url": e.pr_url,
            }
            for e in edict_mgr.list_edicts(repo_id)
        ]

    async def _auto_start_edict(state: EdictState, _server_golem_dir: Path) -> None:
        """Launch the pipeline for a newly created edict."""
        from golem.session import create_edict_dir
        from golem.edict import Edict, EdictStore
        from golem.pipeline import PipelineCoordinator

        # Use the TARGET repo's .golem/, not the server's own
        repo_golem_dir = Path(state.repo_path) / ".golem"
        repo_golem_dir.mkdir(parents=True, exist_ok=True)

        edict_dir = create_edict_dir(repo_golem_dir / "edicts", state.id)
        edict_store = EdictStore(repo_golem_dir / "edicts")
        edict_obj = Edict(
            id=state.id, repo_path=state.repo_path,
            title=state.title, body=state.body,
        )
        await edict_store.create(edict_obj)

        ticket_store_obj = TicketStore(edict_dir / "tickets")
        config = GolemConfig()

        observe_backend = QueueBackend(state.observe_queue)
        file_backend = FileBackend(edict_dir / "events.jsonl")
        event_bus_obj = EventBus(FanoutBackend([observe_backend, file_backend]))

        srv_url = f"http://127.0.0.1:{os.environ.get('GOLEM_PORT', '7665')}"
        pipeline = PipelineCoordinator(
            edict=edict_obj, edict_store=edict_store,
            ticket_store=ticket_store_obj, config=config,
            project_root=Path(state.repo_path), golem_dir=repo_golem_dir,
            event_bus=event_bus_obj, server_url=srv_url,
        )
        state.pipeline = pipeline
        state.status = "running"

        async def _run_pipeline() -> None:
            pipeline_result = await pipeline.run()
            state.status = pipeline_result.status
            state.cost_usd = pipeline_result.total_cost_usd
            state.pr_url = pipeline_result.pr_url

        task = asyncio.create_task(_run_pipeline())
        state.task = task

        def _on_task_done(t: asyncio.Task) -> None:  # type: ignore[type-arg]
            if t.cancelled():
                state.status = "failed"
                return
            exc = t.exception()
            if exc:
                import traceback
                print(f"[PIPELINE] Background task exception: {exc}", file=sys.stderr)
                traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)
                state.status = "failed"

        task.add_done_callback(_on_task_done)

        # Tail progress.log AND session.log into the SSE event_queue for the Logs tab
        async def _tail_edict_log() -> None:
            log_files = {
                edict_dir / "progress.log": 0,
                edict_dir / "session.log": 0,
            }
            while state.task and not state.task.done():
                await asyncio.sleep(0.5)
                for log_path in list(log_files):
                    if not log_path.exists():
                        continue
                    try:
                        with open(log_path, encoding="utf-8") as f:
                            f.seek(log_files[log_path])
                            new_content = f.read()
                            log_files[log_path] = f.tell()
                    except OSError:
                        continue
                    for raw_line in new_content.splitlines():
                        if not raw_line.strip():
                            continue
                        entry: dict[str, str | None] = {"message": raw_line, "raw": raw_line}
                        state.log_buffer.append(entry)
                        await state.event_queue.put(format_sse("log", entry))

        state.tail_task = asyncio.create_task(_tail_edict_log())

    @app.post("/api/repos/{repo_id}/edicts", status_code=201)
    async def create_edict(repo_id: str, req: CreateEdictRequest) -> dict[str, object]:
        from fastapi import HTTPException
        repo = await repo_registry.get(repo_id)
        if not repo:
            raise HTTPException(status_code=404, detail="Repo not found")
        state = edict_mgr.create_edict(repo_id, repo.path, req.title, req.body)

        # Auto-start the pipeline if configured (skip in test mode)
        config = GolemConfig()
        if config.edict_auto_start and not os.environ.get("GOLEM_TEST_MODE"):
            await _auto_start_edict(state, golem_dir)

        return {
            "id": state.id, "repo_id": state.repo_id, "title": state.title,
            "body": state.body, "status": state.status, "created_at": state.created_at,
        }

    @app.get("/api/repos/{repo_id}/edicts/{edict_id}")
    async def get_edict(repo_id: str, edict_id: str) -> dict[str, object]:
        from fastapi import HTTPException
        state = edict_mgr.get_edict(edict_id)
        if not state or state.repo_id != repo_id:
            raise HTTPException(status_code=404, detail="Edict not found")
        return {
            "id": state.id, "repo_id": state.repo_id, "title": state.title,
            "body": state.body, "status": state.status, "created_at": state.created_at,
            "cost_usd": state.cost_usd, "pr_url": state.pr_url,
        }

    @app.patch("/api/repos/{repo_id}/edicts/{edict_id}")
    async def update_edict(repo_id: str, edict_id: str, req: Request) -> dict[str, object]:
        from fastapi import HTTPException
        state = edict_mgr.get_edict(edict_id)
        if not state or state.repo_id != repo_id:
            raise HTTPException(status_code=404, detail="Edict not found")
        body = await req.json()
        if "title" in body:
            state.title = body["title"]
        if "body" in body:
            state.body = body["body"]
        return {"id": state.id, "title": state.title, "body": state.body, "status": state.status}

    @app.delete("/api/repos/{repo_id}/edicts/{edict_id}", status_code=204)
    async def delete_edict(repo_id: str, edict_id: str) -> None:
        from fastapi import HTTPException
        state = edict_mgr.get_edict(edict_id)
        if not state or state.repo_id != repo_id:
            raise HTTPException(status_code=404, detail="Edict not found")
        edict_mgr.remove_edict(edict_id)

    @app.post("/api/repos/{repo_id}/edicts/{edict_id}/start")
    async def start_edict(repo_id: str, edict_id: str) -> dict[str, object]:
        from fastapi import HTTPException
        state = edict_mgr.get_edict(edict_id)
        if not state or state.repo_id != repo_id:
            raise HTTPException(status_code=404, detail="Edict not found")
        if state.status not in ("pending", "needs_attention"):
            raise HTTPException(
                status_code=400,
                detail=f"Cannot start edict in '{state.status}' status",
            )

        from golem.session import create_edict_dir
        from golem.edict import Edict, EdictStore
        from golem.pipeline import PipelineCoordinator

        # Use the TARGET repo's .golem/, not the server's own
        repo_golem_dir = Path(state.repo_path) / ".golem"
        repo_golem_dir.mkdir(parents=True, exist_ok=True)

        edict_dir = create_edict_dir(repo_golem_dir / "edicts", edict_id)
        edict_store = EdictStore(repo_golem_dir / "edicts")
        edict_obj = Edict(id=edict_id, repo_path=state.repo_path, title=state.title, body=state.body)
        await edict_store.create(edict_obj)

        ticket_store_obj = TicketStore(edict_dir / "tickets")
        config = GolemConfig()

        observe_backend = QueueBackend(state.observe_queue)
        file_backend = FileBackend(edict_dir / "events.jsonl")
        event_bus = EventBus(FanoutBackend([observe_backend, file_backend]))

        srv_url = f"http://127.0.0.1:{os.environ.get('GOLEM_PORT', '7665')}"
        pipeline = PipelineCoordinator(
            edict=edict_obj,
            edict_store=edict_store,
            ticket_store=ticket_store_obj,
            config=config,
            project_root=Path(state.repo_path),
            golem_dir=repo_golem_dir,
            event_bus=event_bus,
            server_url=srv_url,
        )
        state.pipeline = pipeline
        state.status = "running"

        async def _run_pipeline() -> None:
            result = await pipeline.run()
            state.status = result.status
            state.cost_usd = result.total_cost_usd
            state.pr_url = result.pr_url

        task = asyncio.create_task(_run_pipeline())
        state.task = task

        def _on_task_done(t: asyncio.Task) -> None:  # type: ignore[type-arg]
            if t.cancelled():
                state.status = "failed"
                return
            exc = t.exception()
            if exc:
                import traceback
                print(f"[PIPELINE] Background task exception: {exc}", file=sys.stderr)
                traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)
                state.status = "failed"

        task.add_done_callback(_on_task_done)

        async def _tail_edict_log() -> None:
            log_files = {
                edict_dir / "progress.log": 0,
                edict_dir / "session.log": 0,
            }
            while state.task and not state.task.done():
                await asyncio.sleep(0.5)
                for log_path in list(log_files):
                    if not log_path.exists():
                        continue
                    try:
                        with open(log_path, encoding="utf-8") as f:
                            f.seek(log_files[log_path])
                            new_content = f.read()
                            log_files[log_path] = f.tell()
                    except OSError:
                        continue
                    for raw_line in new_content.splitlines():
                        if not raw_line.strip():
                            continue
                        entry: dict[str, str | None] = {"message": raw_line, "raw": raw_line}
                        state.log_buffer.append(entry)
                        await state.event_queue.put(format_sse("log", entry))

        state.tail_task = asyncio.create_task(_tail_edict_log())
        return {"id": state.id, "status": state.status}

    @app.post("/api/repos/{repo_id}/edicts/{edict_id}/pause")
    async def pause_edict(repo_id: str, edict_id: str) -> dict[str, object]:
        from fastapi import HTTPException
        state = edict_mgr.get_edict(edict_id)
        if not state or state.repo_id != repo_id:
            raise HTTPException(status_code=404, detail="Edict not found")
        if state.pipeline:
            await state.pipeline.pause()  # type: ignore[union-attr]
            state.status = "paused"
        return {"id": state.id, "status": state.status}

    @app.post("/api/repos/{repo_id}/edicts/{edict_id}/resume")
    async def resume_edict(repo_id: str, edict_id: str) -> dict[str, object]:
        from fastapi import HTTPException
        state = edict_mgr.get_edict(edict_id)
        if not state or state.repo_id != repo_id:
            raise HTTPException(status_code=404, detail="Edict not found")
        if state.pipeline:
            await state.pipeline.resume()  # type: ignore[union-attr]
            state.status = "running"
        return {"id": state.id, "status": state.status}

    @app.post("/api/repos/{repo_id}/edicts/{edict_id}/kill")
    async def kill_edict(repo_id: str, edict_id: str) -> dict[str, object]:
        from fastapi import HTTPException
        state = edict_mgr.get_edict(edict_id)
        if not state or state.repo_id != repo_id:
            raise HTTPException(status_code=404, detail="Edict not found")
        if state.pipeline:
            await state.pipeline.kill()  # type: ignore[union-attr]
        if state.task and not state.task.done():
            state.task.cancel()
        state.status = "failed"
        return {"id": state.id, "status": state.status}

    @app.post("/api/repos/{repo_id}/edicts/{edict_id}/guidance")
    async def send_edict_guidance(
        repo_id: str, edict_id: str, req: GuidanceRequest
    ) -> dict[str, object]:
        from fastapi import HTTPException
        state = edict_mgr.get_edict(edict_id)
        if not state or state.repo_id != repo_id:
            raise HTTPException(status_code=404, detail="Edict not found")
        if state.pipeline:
            await state.pipeline.send_guidance(req.text)  # type: ignore[union-attr]
        return {"id": state.id, "status": "guidance_sent"}

    # ------------------------------------------------------------------
    # Task 4.3: Pipeline board and observability endpoints
    # ------------------------------------------------------------------

    @app.get("/api/repos/{repo_id}/edicts/{edict_id}/board")
    async def get_edict_board(repo_id: str, edict_id: str) -> dict[str, object]:
        from fastapi import HTTPException
        state = edict_mgr.get_edict(edict_id)
        if not state or state.repo_id != repo_id:
            raise HTTPException(status_code=404, detail="Edict not found")

        edict_dir = Path(state.repo_path) / ".golem" / "edicts" / edict_id
        tickets_dir = edict_dir / "tickets"
        columns: dict[str, dict[str, object]] = {
            "planner": {"count": 0, "active": state.status == "planning", "tickets": []},
            "tech_lead": {"count": 0, "tickets": []},
            "junior_dev": {"count": 0, "tickets": []},
            "qa": {"count": 0, "tickets": []},
            "done": {"count": 0, "tickets": []},
            "failed": {"count": 0, "tickets": []},
        }

        if tickets_dir.exists():
            ticket_store = TicketStore(tickets_dir)
            tickets = await ticket_store.list_tickets(edict_id_filter=edict_id)
            for t in tickets:
                stage = t.pipeline_stage or "tech_lead"
                if stage in columns:
                    col_tickets = columns[stage]["tickets"]
                    assert isinstance(col_tickets, list)
                    col_tickets.append({
                        "id": t.id, "title": t.title, "status": t.status,
                        "agent_id": t.agent_id, "pipeline_stage": stage,
                    })
                    columns[stage]["count"] = len(col_tickets)

        return {
            "edict_id": edict_id,
            "columns": columns,
            "column_order": ["planner", "tech_lead", "junior_dev", "qa", "done", "failed"],
        }

    @app.get("/api/repos/{repo_id}/edicts/{edict_id}/tickets")
    async def get_edict_tickets(repo_id: str, edict_id: str) -> list[dict[str, object]]:
        from fastapi import HTTPException
        state = edict_mgr.get_edict(edict_id)
        if not state or state.repo_id != repo_id:
            raise HTTPException(status_code=404, detail="Edict not found")
        edict_dir = Path(state.repo_path) / ".golem" / "edicts" / edict_id / "tickets"
        if not edict_dir.exists():
            return []
        ticket_store = TicketStore(edict_dir)
        tickets = await ticket_store.list_tickets(edict_id_filter=edict_id)
        return [
            {
                "id": t.id, "title": t.title, "status": t.status,
                "pipeline_stage": t.pipeline_stage, "agent_id": t.agent_id,
            }
            for t in tickets
        ]

    @app.get("/api/repos/{repo_id}/edicts/{edict_id}/tickets/{ticket_id}")
    async def get_edict_ticket_detail(
        repo_id: str, edict_id: str, ticket_id: str
    ) -> dict[str, object]:
        from fastapi import HTTPException
        state = edict_mgr.get_edict(edict_id)
        if not state or state.repo_id != repo_id:
            raise HTTPException(status_code=404, detail="Edict not found")
        edict_dir = Path(state.repo_path) / ".golem" / "edicts" / edict_id / "tickets"
        if not edict_dir.exists():
            raise HTTPException(status_code=404, detail="Ticket not found")
        ticket_store = TicketStore(edict_dir)
        try:
            ticket = await ticket_store.read(ticket_id)
            import dataclasses
            return dataclasses.asdict(ticket)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="Ticket not found")

    @app.get("/api/repos/{repo_id}/edicts/{edict_id}/dependencies")
    async def get_edict_dependencies(repo_id: str, edict_id: str) -> dict[str, object]:
        from fastapi import HTTPException
        from golem.tickets import compute_waves, get_dependency_graph

        state = edict_mgr.get_edict(edict_id)
        if not state or state.repo_id != repo_id:
            raise HTTPException(status_code=404, detail="Edict not found")

        tickets_dir = Path(state.repo_path) / ".golem" / "edicts" / edict_id / "tickets"
        tickets: list[Ticket] = []
        if tickets_dir.exists():
            ticket_store = TicketStore(tickets_dir)
            tickets = await ticket_store.list_tickets(edict_id_filter=edict_id)

        graph = get_dependency_graph(tickets)
        cycle_detected = False
        waves: list[list[str]] = []
        try:
            waves = compute_waves(tickets)
        except ValueError:
            cycle_detected = True

        return {"graph": graph, "waves": waves, "cycle_detected": cycle_detected}

    @app.get("/api/repos/{repo_id}/edicts/{edict_id}/observe")
    async def observe_edict(repo_id: str, edict_id: str) -> StreamingResponse:
        from fastapi import HTTPException
        state = edict_mgr.get_edict(edict_id)
        if not state or state.repo_id != repo_id:
            raise HTTPException(status_code=404, detail="Edict not found")

        async def _stream() -> AsyncGenerator[str, None]:
            while True:
                try:
                    event = await asyncio.wait_for(state.observe_queue.get(), timeout=30.0)
                    yield f"data: {json.dumps(event) if isinstance(event, dict) else str(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"

        return StreamingResponse(_stream(), media_type="text/event-stream")

    @app.get("/api/repos/{repo_id}/edicts/{edict_id}/logs")
    async def edict_logs(repo_id: str, edict_id: str) -> StreamingResponse:
        from fastapi import HTTPException
        state = edict_mgr.get_edict(edict_id)
        if not state or state.repo_id != repo_id:
            raise HTTPException(status_code=404, detail="Edict not found")

        async def _stream() -> AsyncGenerator[str, None]:
            for entry in list(state.log_buffer):
                yield format_sse("log", entry)
            while True:
                try:
                    event = await asyncio.wait_for(state.event_queue.get(), timeout=30.0)
                    yield event
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"

        return StreamingResponse(_stream(), media_type="text/event-stream")

    @app.get("/api/repos/{repo_id}/edicts/{edict_id}/cost")
    async def edict_cost(repo_id: str, edict_id: str) -> dict[str, object]:
        from fastapi import HTTPException
        state = edict_mgr.get_edict(edict_id)
        if not state or state.repo_id != repo_id:
            raise HTTPException(status_code=404, detail="Edict not found")
        return {"edict_id": edict_id, "total_cost_usd": state.cost_usd, "breakdown": []}

    @app.get("/api/repos/{repo_id}/edicts/{edict_id}/diff")
    async def edict_diff(repo_id: str, edict_id: str) -> dict[str, object]:
        from fastapi import HTTPException
        state = edict_mgr.get_edict(edict_id)
        if not state or state.repo_id != repo_id:
            raise HTTPException(status_code=404, detail="Edict not found")
        return {"edict_id": edict_id, "diff": ""}

    @app.get("/api/repos/{repo_id}/edicts/{edict_id}/plan")
    async def edict_plan(repo_id: str, edict_id: str) -> dict[str, object]:
        from fastapi import HTTPException
        state = edict_mgr.get_edict(edict_id)
        if not state or state.repo_id != repo_id:
            raise HTTPException(status_code=404, detail="Edict not found")
        edict_dir = Path(state.repo_path) / ".golem" / "edicts" / edict_id
        plan_path = edict_dir / "plans" / "overview.md"
        content = ""
        if plan_path.exists():
            content = plan_path.read_text(encoding="utf-8")
        return {"edict_id": edict_id, "plan": content}

    return app


# ---------------------------------------------------------------------------
# Server lifecycle helpers (moved from legacy ui.py, used by CLI `ui` command)
# ---------------------------------------------------------------------------

import logging as _logging

_ui_logger = _logging.getLogger("golem.ui")


def configure_logging(debug: bool = False) -> None:
    """Configure the golem.ui logger with formatted console output."""
    level = _logging.DEBUG if debug else _logging.INFO
    handler = _logging.StreamHandler()
    handler.setFormatter(_logging.Formatter("[%(asctime)s] [UI] %(message)s", datefmt="%H:%M:%S"))
    _ui_logger.addHandler(handler)
    _ui_logger.setLevel(level)


def start_server(
    host: str = "127.0.0.1", port: int = 7665, log_level: str = "warning", debug: bool = False
) -> None:
    """Start the uvicorn server for the legacy single-session UI. Blocks until Ctrl+C."""
    import uvicorn

    configure_logging(debug=debug)
    _ui_logger.info("Golem UI server starting on %s:%d", host, port)
    app = create_app()
    uvicorn.run(app, host=host, port=port, log_level="info" if debug else log_level)
