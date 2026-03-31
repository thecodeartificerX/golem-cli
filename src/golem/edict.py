from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

EDICT_PENDING = "pending"
EDICT_PLANNING = "planning"
EDICT_IN_PROGRESS = "in_progress"
EDICT_NEEDS_ATTENTION = "needs_attention"
EDICT_DONE = "done"
EDICT_FAILED = "failed"

VALID_TRANSITIONS: dict[str, list[str]] = {
    EDICT_PENDING: [EDICT_PLANNING],
    EDICT_PLANNING: [EDICT_IN_PROGRESS, EDICT_NEEDS_ATTENTION, EDICT_FAILED],
    EDICT_IN_PROGRESS: [EDICT_DONE, EDICT_NEEDS_ATTENTION, EDICT_FAILED],
    EDICT_NEEDS_ATTENTION: [EDICT_PLANNING, EDICT_IN_PROGRESS],
}


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
class Edict:
    id: str
    repo_path: str
    title: str
    body: str
    status: str = EDICT_PENDING
    created_at: str = ""
    updated_at: str = ""
    pr_url: str | None = None
    ticket_ids: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    error: str | None = None

    def to_dict(self) -> dict:  # type: ignore[type-arg]
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Edict:  # type: ignore[type-arg]
        return cls(
            id=data["id"],
            repo_path=data["repo_path"],
            title=data["title"],
            body=data["body"],
            status=data.get("status", EDICT_PENDING),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            pr_url=data.get("pr_url"),
            ticket_ids=data.get("ticket_ids", []),
            cost_usd=data.get("cost_usd", 0.0),
            error=data.get("error"),
        )


class EdictStore:
    def __init__(self, edicts_dir: Path) -> None:
        self._dir = edicts_dir
        self._lock = asyncio.Lock()

    def _resolve_path(self, edict_id: str) -> Path:
        """Resolve edict file path with case-insensitive fallback."""
        path = self._dir / f"{edict_id}.json"
        if not path.exists():
            for candidate in self._dir.glob("*.json"):
                if candidate.stem.upper() == edict_id.upper():
                    return candidate
        return path

    async def create(self, edict: Edict) -> str:
        async with self._lock:
            self._dir.mkdir(parents=True, exist_ok=True)
            existing = sorted(p for p in self._dir.glob("*.json") if p.stem.upper().startswith("EDICT-"))
            next_num = len(existing) + 1
            edict_id = f"EDICT-{next_num:03d}"
            edict.id = edict_id
            now = datetime.now(tz=UTC).isoformat()
            edict.created_at = now
            edict.updated_at = now
            path = self._dir / f"{edict_id}.json"
            _write_json_atomic(path, edict.to_dict())
            return edict_id

    async def read(self, edict_id: str) -> Edict:
        path = self._resolve_path(edict_id)
        data = json.loads(path.read_text(encoding="utf-8"))
        return Edict.from_dict(data)

    async def update_status(self, edict_id: str, status: str, error: str | None = None) -> None:
        async with self._lock:
            path = self._resolve_path(edict_id)
            data = json.loads(path.read_text(encoding="utf-8"))
            edict = Edict.from_dict(data)
            allowed = VALID_TRANSITIONS.get(edict.status, [])
            if status not in allowed:
                raise ValueError(f"Invalid transition: {edict.status!r} -> {status!r}. Allowed: {allowed}")
            edict.status = status
            edict.updated_at = datetime.now(tz=UTC).isoformat()
            if error is not None:
                edict.error = error
            _write_json_atomic(path, edict.to_dict())

    async def update(self, edict_id: str, **kwargs: object) -> None:
        async with self._lock:
            path = self._resolve_path(edict_id)
            data = json.loads(path.read_text(encoding="utf-8"))
            edict = Edict.from_dict(data)
            allowed_fields = {"title", "body", "pr_url", "ticket_ids", "cost_usd"}
            for key, value in kwargs.items():
                if key in allowed_fields:
                    setattr(edict, key, value)
            edict.updated_at = datetime.now(tz=UTC).isoformat()
            _write_json_atomic(path, edict.to_dict())

    async def list_edicts(self, status_filter: str | None = None) -> list[Edict]:
        if not self._dir.exists():
            return []
        edicts: list[Edict] = []
        for path in sorted(p for p in self._dir.glob("*.json") if p.stem.upper().startswith("EDICT-")):
            data = json.loads(path.read_text(encoding="utf-8"))
            edict = Edict.from_dict(data)
            if status_filter is not None and edict.status != status_filter:
                continue
            edicts.append(edict)
        return edicts

    async def delete(self, edict_id: str) -> bool:
        path = self._resolve_path(edict_id)
        if path.exists():
            path.unlink()
            return True
        return False
