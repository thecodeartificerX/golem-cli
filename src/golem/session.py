from __future__ import annotations

import json
import re
import shutil
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path


# Status constants
PENDING = "pending"
RUNNING = "running"
AWAITING_MERGE = "awaiting_merge"
PR_OPEN = "pr_open"
MERGED = "merged"
ARCHIVED = "archived"
FAILED = "failed"
PAUSED = "paused"
CONFLICT = "conflict"


@dataclass
class SessionMetadata:
    id: str = ""
    spec_path: str = ""
    status: str = PENDING
    complexity: str = "STANDARD"
    created_at: str = ""
    updated_at: str = ""
    pid: int | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    merged_at: str | None = None
    archived_at: str | None = None
    cost_usd: float = 0.0
    error: str | None = None


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def generate_session_id(spec_path: Path, sessions_dir: Path) -> str:
    """Generate a unique session ID from the spec filename."""
    slug = spec_path.stem.lower().replace(" ", "-").replace("_", "-")
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    slug = slug[:40]
    existing = [d.name for d in sessions_dir.iterdir() if d.is_dir()] if sessions_dir.exists() else []
    n = 1
    while f"{slug}-{n}" in existing:
        n += 1
    return f"{slug}-{n}"


def read_session(session_dir: Path) -> SessionMetadata:
    """Read session.json from a session directory."""
    data = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))
    return SessionMetadata(
        id=data.get("id", ""),
        spec_path=data.get("spec_path", ""),
        status=data.get("status", PENDING),
        complexity=data.get("complexity", "STANDARD"),
        created_at=data.get("created_at", ""),
        updated_at=data.get("updated_at", ""),
        pid=data.get("pid"),
        pr_number=data.get("pr_number"),
        pr_url=data.get("pr_url"),
        merged_at=data.get("merged_at"),
        archived_at=data.get("archived_at"),
        cost_usd=data.get("cost_usd", 0.0),
        error=data.get("error"),
    )


def write_session(session_dir: Path, meta: SessionMetadata) -> None:
    """Write session.json to a session directory."""
    meta.updated_at = _now_iso()
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "session.json").write_text(
        json.dumps(asdict(meta), indent=2), encoding="utf-8"
    )


def create_session_dir(sessions_dir: Path, session_id: str, spec_path: Path) -> Path:
    """Create a session directory with standard subdirectories and initial metadata."""
    session_dir = sessions_dir / session_id
    for subdir in ("tickets", "plans", "research", "references", "reports", "worktrees"):
        (session_dir / subdir).mkdir(parents=True, exist_ok=True)

    shutil.copy2(spec_path, session_dir / "spec.md")

    meta = SessionMetadata(
        id=session_id,
        spec_path=str(spec_path),
        status=PENDING,
        created_at=_now_iso(),
    )
    write_session(session_dir, meta)

    return session_dir


def delete_session_dir(sessions_dir: Path, session_id: str) -> bool:
    """Remove a session directory from disk. Returns True if removed."""
    session_dir = sessions_dir / session_id
    if session_dir.exists() and (session_dir / "session.json").exists():
        shutil.rmtree(session_dir, ignore_errors=True)
        return True
    return False
