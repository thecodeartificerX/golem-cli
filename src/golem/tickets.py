from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from filelock import FileLock


STAGE_PLANNER = "planner"
STAGE_TECH_LEAD = "tech_lead"
STAGE_JUNIOR_DEV = "junior_dev"
STAGE_QA = "qa"
STAGE_DONE = "done"
STAGE_FAILED = "failed"


def _write_json_atomic(path: Path, data: dict) -> None:  # type: ignore[type-arg]
    """Write JSON to path atomically via tmp+rename.

    Uses a sibling .tmp file in the same directory so rename stays on
    the same filesystem/volume (required for atomic rename on Windows).
    os.replace() is atomic on POSIX; best-effort on Windows NTFS within
    the same volume.
    """
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


@dataclass
class TicketEvent:
    ts: str
    agent: str
    action: str
    note: str
    attachments: list[str] = field(default_factory=list)


@dataclass
class TicketContext:
    plan_file: str = ""
    files: dict[str, str] = field(default_factory=dict)
    references: list[str] = field(default_factory=list)
    blueprint: str = ""
    acceptance: list[str] = field(default_factory=list)
    qa_checks: list[str] = field(default_factory=list)
    parallelism_hints: list[str] = field(default_factory=list)
    skip_review: bool = False


@dataclass
class Ticket:
    id: str
    type: str
    title: str
    status: str
    priority: str
    created_by: str
    assigned_to: str
    context: TicketContext
    history: list[TicketEvent] = field(default_factory=list)
    session_id: str = ""
    depends_on: list[str] = field(default_factory=list)  # ticket IDs this ticket blocks on
    edict_id: str = ""           # parent Edict reference (e.g., "EDICT-001")
    pipeline_stage: str = ""     # determines board column
    agent_id: str = ""           # which agent instance is handling this (e.g., "junior-dev-3")


def _ticket_to_dict(ticket: Ticket) -> dict:
    d = asdict(ticket)
    return d


def _ticket_from_dict(data: dict) -> Ticket:
    ctx_data = data.get("context", {})
    context = TicketContext(
        plan_file=ctx_data.get("plan_file", ""),
        files=ctx_data.get("files", {}),
        references=ctx_data.get("references", []),
        blueprint=ctx_data.get("blueprint", ""),
        acceptance=ctx_data.get("acceptance", []),
        qa_checks=ctx_data.get("qa_checks", []),
        parallelism_hints=ctx_data.get("parallelism_hints", []),
        skip_review=ctx_data.get("skip_review", False),
    )
    history = [
        TicketEvent(
            ts=e["ts"],
            agent=e["agent"],
            action=e["action"],
            note=e["note"],
            attachments=e.get("attachments", []),
        )
        for e in data.get("history", [])
    ]
    return Ticket(
        id=data["id"],
        type=data["type"],
        title=data["title"],
        status=data["status"],
        priority=data["priority"],
        created_by=data["created_by"],
        assigned_to=data["assigned_to"],
        context=context,
        history=history,
        session_id=data.get("session_id", ""),
        depends_on=data.get("depends_on", []),
        edict_id=data.get("edict_id", ""),
        pipeline_stage=data.get("pipeline_stage", ""),
        agent_id=data.get("agent_id", ""),
    )


def compute_waves(tickets: list[Ticket]) -> list[list[str]]:
    """Compute execution waves from ticket dependencies using Kahn's algorithm.

    Returns a list of waves, where each wave is a list of ticket IDs that can
    execute in parallel (all their dependencies are in earlier waves).

    Tickets with no dependencies go in wave 0.
    Raises ValueError if there's a dependency cycle.
    """
    from collections import deque

    if not tickets:
        return []

    ticket_ids: set[str] = {t.id for t in tickets}

    # Build adjacency structures, ignoring missing dependency references
    in_degree: dict[str, int] = {}
    dependents: dict[str, list[str]] = {}  # dep_id -> list of tickets that depend on dep_id

    for t in tickets:
        valid_deps = [d for d in t.depends_on if d in ticket_ids]
        in_degree[t.id] = len(valid_deps)
        dependents.setdefault(t.id, [])
        for d in valid_deps:
            dependents.setdefault(d, []).append(t.id)

    # Kahn's algorithm: process tickets in waves
    queue: deque[str] = deque(tid for tid, deg in in_degree.items() if deg == 0)
    wave_map: dict[str, int] = {tid: 0 for tid in queue}
    processed = 0

    while queue:
        tid = queue.popleft()
        processed += 1
        for dep_id in dependents.get(tid, []):
            wave_map[dep_id] = max(wave_map.get(dep_id, 0), wave_map[tid] + 1)
            in_degree[dep_id] -= 1
            if in_degree[dep_id] == 0:
                queue.append(dep_id)

    if processed != len(tickets):
        cycle_members = sorted(tid for tid, deg in in_degree.items() if deg > 0)
        raise ValueError(f"Dependency cycle detected involving: {', '.join(cycle_members)}")

    # Group ticket IDs by wave, sorted within each wave for determinism
    wave_buckets: dict[int, list[str]] = {}
    for tid, wave_num in wave_map.items():
        wave_buckets.setdefault(wave_num, []).append(tid)

    return [sorted(wave_buckets[i]) for i in sorted(wave_buckets)]


def get_dependency_graph(tickets: list[Ticket]) -> dict[str, list[str]]:
    """Return {ticket_id: [depends_on_ids]} for all tickets. For board visualization."""
    return {t.id: list(t.depends_on) for t in tickets}


class TicketStore:
    def __init__(self, tickets_dir: Path) -> None:
        self._dir = tickets_dir
        self._lock = asyncio.Lock()  # In-process async lock
        self._file_lock: FileLock | None = None  # Cross-process file lock (created lazily)

    def _get_file_lock(self) -> FileLock:
        """Get or create the cross-process file lock."""
        if self._file_lock is None:
            self._dir.mkdir(parents=True, exist_ok=True)
            self._file_lock = FileLock(self._dir / ".ticket-store.lock", timeout=30)
        return self._file_lock

    def _create_sync(self, ticket: Ticket) -> str:
        """Synchronous ticket creation (called inside file lock)."""
        self._dir.mkdir(parents=True, exist_ok=True)
        # Find next ID (case-insensitive count to handle mixed-case files)
        existing = sorted(p for p in self._dir.glob("*.json") if p.stem.upper().startswith("TICKET-"))
        next_num = len(existing) + 1
        ticket_id = f"TICKET-{next_num:03d}"
        ticket.id = ticket_id
        # Append created event
        ticket.history = [
            TicketEvent(
                ts=datetime.now(tz=UTC).isoformat(),
                agent=ticket.created_by,
                action="created",
                note=f"Ticket created: {ticket.title}",
            )
        ]
        path = self._dir / f"{ticket_id}.json"
        _write_json_atomic(path, _ticket_to_dict(ticket))
        return ticket_id

    async def create(self, ticket: Ticket) -> str:
        async with self._lock:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None,
                lambda: self._with_file_lock(lambda: self._create_sync(ticket)),
            )

    def _with_file_lock(self, fn):  # type: ignore[no-untyped-def]
        """Execute fn with the cross-process file lock held."""
        with self._get_file_lock():
            return fn()

    async def read(self, ticket_id: str) -> Ticket:
        path = self._resolve_path(ticket_id)
        data = json.loads(path.read_text(encoding="utf-8"))
        return _ticket_from_dict(data)

    def _resolve_path(self, ticket_id: str) -> Path:
        """Resolve ticket file path with case-insensitive fallback."""
        path = self._dir / f"{ticket_id}.json"
        if not path.exists():
            for candidate in self._dir.glob("*.json"):
                if candidate.stem.upper() == ticket_id.upper():
                    return candidate
        return path

    def _update_sync(
        self,
        ticket_id: str,
        status: str,
        note: str,
        attachments: list[str] | None = None,
        agent: str = "system",
        pipeline_stage: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        """Synchronous ticket update (called inside file lock)."""
        path = self._resolve_path(ticket_id)
        data = json.loads(path.read_text(encoding="utf-8"))
        ticket = _ticket_from_dict(data)
        ticket.status = status
        if pipeline_stage is not None:
            ticket.pipeline_stage = pipeline_stage
        if agent_id is not None:
            ticket.agent_id = agent_id
        ticket.history.append(
            TicketEvent(
                ts=datetime.now(tz=UTC).isoformat(),
                agent=agent,
                action=f"status_changed_to_{status}",
                note=note,
                attachments=attachments or [],
            )
        )
        _write_json_atomic(path, _ticket_to_dict(ticket))

    async def update(
        self,
        ticket_id: str,
        status: str,
        note: str,
        attachments: list[str] | None = None,
        agent: str = "system",
        pipeline_stage: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                lambda: self._with_file_lock(
                    lambda: self._update_sync(
                        ticket_id, status, note, attachments, agent, pipeline_stage, agent_id
                    )
                ),
            )

    async def list_tickets(
        self,
        status_filter: str | None = None,
        assigned_to_filter: str | None = None,
        edict_id_filter: str | None = None,
        pipeline_stage_filter: str | None = None,
    ) -> list[Ticket]:
        if not self._dir.exists():
            return []
        tickets: list[Ticket] = []
        for path in sorted(p for p in self._dir.glob("*.json") if p.stem.upper().startswith("TICKET-")):
            data = json.loads(path.read_text(encoding="utf-8"))
            ticket = _ticket_from_dict(data)
            if status_filter is not None and ticket.status != status_filter:
                continue
            if assigned_to_filter is not None and ticket.assigned_to != assigned_to_filter:
                continue
            if edict_id_filter is not None and ticket.edict_id != edict_id_filter:
                continue
            if pipeline_stage_filter is not None and ticket.pipeline_stage != pipeline_stage_filter:
                continue
            tickets.append(ticket)
        return tickets
