from __future__ import annotations

import asyncio
import json
import os
from collections import deque
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
import dataclasses
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from golem.merge import MergeCoordinator
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

    from golem.session import read_session, write_session

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
        if state.process is None or state.process.returncode is not None:
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
            event_dict: dict[str, str | None] = {"message": raw_line, "raw": raw_line}
            state.log_buffer.append(event_dict)
            await state.event_queue.put(format_sse("log", event_dict))


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
    merge_coordinator = MergeCoordinator(golem_dir, session_mgr)

    # Queues for aggregate SSE stream (one per connected client)
    _aggregate_queues: set[asyncio.Queue[str]] = set()

    # Restart recovery: restore non-archived sessions from disk as paused.
    # Only runs when GOLEM_DIR is explicitly set (non-empty), to avoid accidentally
    # picking up test artifacts from CWD-relative paths.
    # Only restores sessions that have a copied spec.md (written by create_session_dir).
    _golem_dir_env = os.environ.get("GOLEM_DIR", "")
    if _golem_dir_env and sessions_dir.exists():
        from golem.session import read_session
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

    # ------------------------------------------------------------------
    # Session CRUD
    # ------------------------------------------------------------------

    @app.post("/api/sessions")
    async def create_session(req: CreateSessionRequest) -> dict[str, str]:
        from golem.session import create_session_dir, generate_session_id

        spec = Path(req.spec_path)
        if not spec.exists():
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail=f"Spec not found: {req.spec_path}")

        project_root = Path(req.project_root) if req.project_root else spec.resolve().parent
        session_id = generate_session_id(spec, sessions_dir)

        # Create session directory structure
        sessions_dir.mkdir(parents=True, exist_ok=True)
        session_dir = create_session_dir(sessions_dir, session_id, spec)

        # Spawn the golem run subprocess
        process = await asyncio.create_subprocess_exec(
            "uv", "run", "golem", "run", str(spec.resolve()),
            "--force", "--no-server",
            "--session-id", session_id,
            "--golem-dir", str(session_dir),
            cwd=str(project_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        state = session_mgr.create_session(session_id, spec)
        state.process = process
        state.status = "running"

        # Start background tasks for this session
        state.background_tasks.append(asyncio.create_task(tail_progress_log(state, sessions_dir)))
        state.background_tasks.append(asyncio.create_task(monitor_process(state, sessions_dir, merge_coordinator)))

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
    async def delete_session(session_id: str) -> dict[str, str]:
        from fastapi import HTTPException

        if not session_mgr.kill_session(session_id):
            raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")
        session_mgr.archive_session(session_id)
        return {"status": "deleted", "session_id": session_id}

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
    # Per-session data endpoints
    # ------------------------------------------------------------------

    @app.get("/api/sessions/{session_id}/tickets")
    async def session_tickets(session_id: str) -> list[dict[str, object]]:
        from fastapi import HTTPException
        from golem.tickets import TicketStore

        state = session_mgr.get_session(session_id)
        if not state:
            raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")
        tickets_dir = sessions_dir / session_id / "tickets"
        if not tickets_dir.exists():
            return []
        store = TicketStore(tickets_dir)
        tickets = await store.list_tickets()
        return [{"id": t.id, "title": t.title, "status": t.status} for t in tickets]

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

    return app
