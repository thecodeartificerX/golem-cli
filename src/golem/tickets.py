from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path


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
    )


class TicketStore:
    def __init__(self, tickets_dir: Path) -> None:
        self._dir = tickets_dir
        self._lock = asyncio.Lock()

    async def create(self, ticket: Ticket) -> str:
        async with self._lock:
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

    async def update(
        self,
        ticket_id: str,
        status: str,
        note: str,
        attachments: list[str] | None = None,
        agent: str = "system",
    ) -> None:
        async with self._lock:
            path = self._resolve_path(ticket_id)
            data = json.loads(path.read_text(encoding="utf-8"))
            ticket = _ticket_from_dict(data)
            ticket.status = status
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

    async def list_tickets(
        self,
        status_filter: str | None = None,
        assigned_to_filter: str | None = None,
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
            tickets.append(ticket)
        return tickets
