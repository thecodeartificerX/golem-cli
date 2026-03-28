# Foundation + Server Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add session-scoped state namespacing and a FastAPI server that manages concurrent spec executions as isolated subprocess sessions.

**Architecture:** Each spec execution becomes a "session" with its own `.golem/sessions/<id>/` directory containing tickets, plans, progress.log, and worktrees. A FastAPI server manages sessions as subprocesses, exposes REST/SSE endpoints, and the existing `--no-server` path preserves current single-spec behavior unchanged.

**Tech Stack:** Python 3.12+, FastAPI, uvicorn, httpx (test), dataclasses, asyncio, Pydantic (module-level models)

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `src/golem/session.py` | Create | SessionMetadata dataclass, ID generation, session dir scaffolding, read/write JSON |
| `tests/test_session.py` | Create | 8 tests: ID generation (basic/increment/special/long), metadata roundtrip, dir structure, spec copy, status transitions |
| `src/golem/config.py` | Modify | Add `session_id`, `branch_prefix`, `merge_auto_rebase`, `archive_delay_minutes` fields to GolemConfig |
| `src/golem/tickets.py` | Modify | Add `session_id` field to Ticket dataclass + backward-compat in `_ticket_from_dict` |
| `tests/test_config.py` | Modify | Add tests for new fields |
| `tests/test_tickets.py` | Modify | Add test for session_id persistence + backward compat |
| `src/golem/worktree.py` | Modify | Add `branch_prefix` parameter to `create_worktree()` |
| `src/golem/tech_lead.py` | Modify | Pass `branch_prefix` through, update `_ensure_merged_to_main()` |
| `tests/test_worktree.py` | Modify | Add tests for `branch_prefix` parameter |
| `src/golem/progress.py` | Modify | Add 8 session lifecycle event methods |
| `tests/test_progress.py` | Modify | Add tests for each new event method |
| `src/golem/cli.py` | Modify | Add `--session-id`, `--golem-dir`, `--no-server` flags; add `server` sub-typer with start/stop/status |
| `tests/test_cli.py` | Modify | Add tests for new flags and server sub-commands |
| `src/golem/server.py` | Create | FastAPI app factory, SessionManager, SessionState, MergeCoordinator stub, all REST/SSE endpoints |
| `tests/test_server.py` | Create | 24 tests across skeleton, CRUD, lifecycle, SSE, data, and preserved endpoints |

---

### Task 1: Session Module

**Files:**
- Create: `src/golem/session.py`
- Create: `tests/test_session.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_session.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

import pytest

from golem.session import (
    SessionMetadata,
    create_session_dir,
    generate_session_id,
    read_session,
    write_session,
)

# Valid status transitions: from_status -> allowed next statuses
_VALID_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"running", "failed"},
    "running": {"awaiting_merge", "failed", "paused"},
    "paused": {"running", "failed"},
    "awaiting_merge": {"pr_open", "conflict", "failed"},
    "pr_open": {"merged", "conflict", "failed"},
    "conflict": {"awaiting_merge", "failed"},
    "merged": {"archived"},
    "archived": set(),
    "failed": set(),
}


def test_generate_session_id_basic(tmp_path: Path) -> None:
    """Slug from a simple spec filename."""
    spec = tmp_path / "auth-flow.md"
    spec.touch()
    sessions_dir = tmp_path / "sessions"
    result = generate_session_id(spec, sessions_dir)
    assert result == "auth-flow-1"


def test_generate_session_id_increment(tmp_path: Path) -> None:
    """Collision avoidance: existing session dirs bump the suffix."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "my-spec-1").mkdir()
    (sessions_dir / "my-spec-2").mkdir()
    spec = tmp_path / "my-spec.md"
    spec.touch()
    result = generate_session_id(spec, sessions_dir)
    assert result == "my-spec-3"


def test_generate_session_id_special_chars(tmp_path: Path) -> None:
    """Non-alphanumeric characters are stripped from the slug."""
    spec = tmp_path / "My Spec (v2)!.md"
    spec.touch()
    sessions_dir = tmp_path / "sessions"
    result = generate_session_id(spec, sessions_dir)
    # Spaces become hyphens, parens/bang stripped
    assert "(" not in result
    assert ")" not in result
    assert "!" not in result
    assert result.startswith("my-spec-v2")


def test_generate_session_id_long_name(tmp_path: Path) -> None:
    """Long spec stems are truncated to 40 characters before the suffix."""
    long_name = "a" * 80
    spec = tmp_path / f"{long_name}.md"
    spec.touch()
    sessions_dir = tmp_path / "sessions"
    result = generate_session_id(spec, sessions_dir)
    # Slug part (before -N suffix) is at most 40 chars
    slug_part = result.rsplit("-", 1)[0]
    assert len(slug_part) <= 40


def test_session_metadata_roundtrip(tmp_path: Path) -> None:
    """Write then read a SessionMetadata preserves all fields."""
    meta = SessionMetadata(
        id="auth-flow-1",
        spec_path="specs/auth-flow.md",
        status="running",
        complexity="STANDARD",
        pid=12345,
        cost_usd=1.24,
    )
    write_session(tmp_path, meta)
    loaded = read_session(tmp_path)
    assert loaded.id == "auth-flow-1"
    assert loaded.spec_path == "specs/auth-flow.md"
    assert loaded.status == "running"
    assert loaded.complexity == "STANDARD"
    assert loaded.pid == 12345
    assert loaded.cost_usd == 1.24


def test_create_session_dir_structure(tmp_path: Path) -> None:
    """create_session_dir creates all expected subdirectories."""
    sessions_dir = tmp_path / "sessions"
    spec = tmp_path / "test-spec.md"
    spec.write_text("# Test\n", encoding="utf-8")
    session_dir = create_session_dir(sessions_dir, "test-spec-1", spec)
    for subdir in ("tickets", "plans", "research", "references", "reports", "worktrees"):
        assert (session_dir / subdir).is_dir(), f"Missing subdir: {subdir}"
    assert (session_dir / "session.json").exists()


def test_create_session_dir_spec_copy(tmp_path: Path) -> None:
    """create_session_dir copies the spec as spec.md (immutable copy)."""
    sessions_dir = tmp_path / "sessions"
    spec = tmp_path / "my-feature.md"
    spec.write_text("# Feature\n\nSome content.", encoding="utf-8")
    session_dir = create_session_dir(sessions_dir, "my-feature-1", spec)
    copied = session_dir / "spec.md"
    assert copied.exists()
    assert copied.read_text(encoding="utf-8") == "# Feature\n\nSome content."


def test_status_transitions() -> None:
    """Verify the transition map covers all statuses and is internally consistent."""
    all_statuses = set(_VALID_TRANSITIONS.keys())
    # Terminal states have no outbound transitions
    assert _VALID_TRANSITIONS["archived"] == set()
    assert _VALID_TRANSITIONS["failed"] == set()
    # All target statuses are valid statuses
    for src, targets in _VALID_TRANSITIONS.items():
        for t in targets:
            assert t in all_statuses, f"Transition {src}->{t} targets unknown status"
    # Running is reachable from pending
    assert "running" in _VALID_TRANSITIONS["pending"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_session.py -v --tb=short`
Expected: FAIL (ModuleNotFoundError — `golem.session` doesn't exist yet)

- [ ] **Step 3: Implement session.py**

Create `src/golem/session.py`:

```python
from __future__ import annotations

import json
import re
import shutil
from dataclasses import asdict, dataclass, field
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
    """Generate a unique session ID from the spec filename.

    Slugifies the spec stem, then appends an incrementing suffix to avoid collisions.
    """
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
    """Create a session directory with standard subdirectories and initial metadata.

    Returns the path to the created session directory.
    """
    session_dir = sessions_dir / session_id
    for subdir in ("tickets", "plans", "research", "references", "reports", "worktrees"):
        (session_dir / subdir).mkdir(parents=True, exist_ok=True)

    # Copy spec as immutable snapshot
    shutil.copy2(spec_path, session_dir / "spec.md")

    # Write initial session.json
    meta = SessionMetadata(
        id=session_id,
        spec_path=str(spec_path),
        status=PENDING,
        created_at=_now_iso(),
    )
    write_session(session_dir, meta)

    return session_dir
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_session.py -v --tb=short`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add src/golem/session.py tests/test_session.py
git commit -m "feat: add session module with ID generation, metadata I/O, and dir scaffolding"
```

---

### Task 2: Config and Ticket Extensions

**Files:**
- Modify: `src/golem/config.py`
- Modify: `src/golem/tickets.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_tickets.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_config.py`:

```python
def test_session_id_default() -> None:
    """GolemConfig.session_id defaults to empty string."""
    config = GolemConfig()
    assert config.session_id == ""


def test_branch_prefix_default() -> None:
    """GolemConfig.branch_prefix defaults to 'golem'."""
    config = GolemConfig()
    assert config.branch_prefix == "golem"


def test_merge_auto_rebase_default() -> None:
    """GolemConfig.merge_auto_rebase defaults to True."""
    config = GolemConfig()
    assert config.merge_auto_rebase is True


def test_archive_delay_minutes_default() -> None:
    """GolemConfig.archive_delay_minutes defaults to 30."""
    config = GolemConfig()
    assert config.archive_delay_minutes == 30


def test_session_fields_roundtrip() -> None:
    """Session-related config fields survive save/load cycle."""
    with tempfile.TemporaryDirectory() as tmpdir:
        golem_dir = Path(tmpdir)
        config = GolemConfig(session_id="auth-flow-1", branch_prefix="golem/auth-flow-1")
        save_config(config, golem_dir)
        loaded = load_config(golem_dir)
        assert loaded.session_id == "auth-flow-1"
        assert loaded.branch_prefix == "golem/auth-flow-1"
```

Add to `tests/test_tickets.py`:

```python
async def test_ticket_session_id_roundtrip(tmp_path: Path) -> None:
    """session_id persists through create/read cycle."""
    store = TicketStore(tmp_path / "tickets")
    (tmp_path / "tickets").mkdir(parents=True, exist_ok=True)
    ticket = Ticket(
        id="",
        type="task",
        title="Test",
        status="pending",
        priority="high",
        created_by="test",
        assigned_to="writer",
        context=TicketContext(),
        session_id="auth-flow-1",
    )
    ticket_id = await store.create(ticket)
    loaded = await store.read(ticket_id)
    assert loaded.session_id == "auth-flow-1"


async def test_ticket_without_session_id_loads_default(tmp_path: Path) -> None:
    """Legacy tickets without session_id field load with empty default."""
    tickets_dir = tmp_path / "tickets"
    tickets_dir.mkdir(parents=True, exist_ok=True)
    # Write a ticket JSON without session_id
    data = {
        "id": "TICKET-001",
        "type": "task",
        "title": "Legacy",
        "status": "pending",
        "priority": "medium",
        "created_by": "planner",
        "assigned_to": "writer",
        "context": {"plan_file": "", "files": {}, "references": [], "blueprint": "",
                     "acceptance": [], "qa_checks": [], "parallelism_hints": []},
        "history": [],
    }
    (tickets_dir / "TICKET-001.json").write_text(
        json.dumps(data, indent=2), encoding="utf-8"
    )
    store = TicketStore(tickets_dir)
    loaded = await store.read("TICKET-001")
    assert loaded.session_id == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config.py::test_session_id_default tests/test_tickets.py::test_ticket_session_id_roundtrip -v --tb=short`
Expected: FAIL (AttributeError — fields don't exist yet)

- [ ] **Step 3: Add session fields to GolemConfig**

In `src/golem/config.py`, add these fields to the `GolemConfig` dataclass (after `pr_target`):

```python
    session_id: str = ""
    branch_prefix: str = "golem"
    merge_auto_rebase: bool = True
    archive_delay_minutes: int = 30
```

- [ ] **Step 4: Add session_id to Ticket**

In `src/golem/tickets.py`, add to the `Ticket` dataclass (after `history`):

```python
    session_id: str = ""
```

In `_ticket_from_dict`, add after the history parsing:

```python
    session_id=data.get("session_id", ""),
```

And pass it to the `Ticket(...)` constructor call in `_ticket_from_dict`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py tests/test_tickets.py -v --tb=short`
Expected: all passed (existing + new)

- [ ] **Step 6: Run full suite for regression**

Run: `uv run pytest --tb=short -q`
Expected: all passed, 0 failed

- [ ] **Step 7: Commit**

```bash
git add src/golem/config.py src/golem/tickets.py tests/test_config.py tests/test_tickets.py
git commit -m "feat: add session_id and branch_prefix to GolemConfig and Ticket"
```

---

### Task 3: Worktree Branch Namespacing

**Files:**
- Modify: `src/golem/worktree.py`
- Modify: `src/golem/tech_lead.py`
- Modify: `tests/test_worktree.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_worktree.py`:

```python
def test_create_worktree_with_branch_prefix() -> None:
    """create_worktree with branch_prefix creates branch using the prefix."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "repo"
        repo.mkdir()
        _init_git_repo(repo)

        wt_path = Path(tmpdir) / "worktrees" / "group-a"
        create_worktree(
            "group-a",
            "golem/session-1/group-a",
            "main",
            wt_path,
            repo,
            branch_prefix="golem/session-1",
        )

        worktrees = list_worktrees(repo)
        assert any("group-a" in wt for wt in worktrees)

        # Verify the branch name uses the prefix
        result = subprocess.run(
            ["git", "branch", "--list", "golem/session-1/group-a"],
            cwd=repo, capture_output=True, text=True, check=True,
        )
        assert "golem/session-1/group-a" in result.stdout

        delete_worktree(wt_path, repo)


def test_create_worktree_default_prefix() -> None:
    """create_worktree without branch_prefix still works (backward compat)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "repo"
        repo.mkdir()
        _init_git_repo(repo)

        wt_path = Path(tmpdir) / "worktrees" / "group-b"
        # No branch_prefix argument — should still work
        create_worktree("group-b", "golem/spec/group-b", "main", wt_path, repo)

        worktrees = list_worktrees(repo)
        assert any("group-b" in wt for wt in worktrees)
        delete_worktree(wt_path, repo)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_worktree.py::test_create_worktree_with_branch_prefix -v --tb=short`
Expected: FAIL (TypeError — unexpected keyword argument `branch_prefix`)

- [ ] **Step 3: Add branch_prefix parameter to create_worktree**

In `src/golem/worktree.py`, update the `create_worktree` signature:

```python
def create_worktree(
    group_id: str, branch: str, base_branch: str, path: Path, repo_root: Path,
    branch_prefix: str = "golem",
) -> None:
```

The existing implementation already uses the `branch` parameter directly (not constructing it from a hardcoded prefix), so the parameter is accepted but the caller is responsible for passing the correct branch name. This makes it backward compatible.

- [ ] **Step 4: Update _ensure_merged_to_main in tech_lead.py**

In `src/golem/tech_lead.py`, update `_ensure_merged_to_main`:

```python
def _ensure_merged_to_main(project_root: Path, branch_prefix: str = "golem") -> None:
```

And change the branch listing pattern from:
```python
    result = _git("branch", "--list", "golem/*/integration")
```
to:
```python
    result = _git("branch", "--list", f"{branch_prefix}/*/integration")
```

Also update `run_tech_lead()` to compute and pass `branch_prefix`. Find the call to `_ensure_merged_to_main(project_root)` and add the prefix derivation above it:

```python
    branch_prefix = f"golem/{config.session_id}" if config.session_id else "golem"
```

Then pass it:
```python
    _ensure_merged_to_main(project_root, branch_prefix=branch_prefix)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_worktree.py tests/test_tech_lead.py -v --tb=short`
Expected: all passed

- [ ] **Step 6: Run full suite for regression**

Run: `uv run pytest --tb=short -q`
Expected: all passed, 0 failed

- [ ] **Step 7: Commit**

```bash
git add src/golem/worktree.py src/golem/tech_lead.py tests/test_worktree.py
git commit -m "feat: add branch_prefix to worktree/tech_lead for session-scoped branches"
```

---

### Task 4: Progress Events

**Files:**
- Modify: `src/golem/progress.py`
- Modify: `tests/test_progress.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_progress.py`:

```python
def test_log_session_start() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        logger = ProgressLogger(Path(tmpdir))
        logger.log_session_start("auth-flow-1", "specs/auth.md")
        content = (Path(tmpdir) / "progress.log").read_text(encoding="utf-8")
        assert "SESSION_START" in content
        assert "session_id=auth-flow-1" in content
        assert "spec=specs/auth.md" in content


def test_log_session_complete() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        logger = ProgressLogger(Path(tmpdir))
        logger.log_session_complete("auth-flow-1", "merged")
        content = (Path(tmpdir) / "progress.log").read_text(encoding="utf-8")
        assert "SESSION_COMPLETE" in content
        assert "session_id=auth-flow-1" in content
        assert "status=merged" in content


def test_log_merge_queued() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        logger = ProgressLogger(Path(tmpdir))
        logger.log_merge_queued("auth-flow-1")
        content = (Path(tmpdir) / "progress.log").read_text(encoding="utf-8")
        assert "MERGE_QUEUED" in content
        assert "session_id=auth-flow-1" in content


def test_log_pr_created() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        logger = ProgressLogger(Path(tmpdir))
        logger.log_pr_created("auth-flow-1", 42)
        content = (Path(tmpdir) / "progress.log").read_text(encoding="utf-8")
        assert "PR_CREATED" in content
        assert "session_id=auth-flow-1" in content
        assert "pr=42" in content


def test_log_pr_merged() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        logger = ProgressLogger(Path(tmpdir))
        logger.log_pr_merged("auth-flow-1", 42)
        content = (Path(tmpdir) / "progress.log").read_text(encoding="utf-8")
        assert "PR_MERGED" in content
        assert "pr=42" in content


def test_log_rebase_start() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        logger = ProgressLogger(Path(tmpdir))
        logger.log_rebase_start("auth-flow-1", "main")
        content = (Path(tmpdir) / "progress.log").read_text(encoding="utf-8")
        assert "REBASE_START" in content
        assert "onto=main" in content


def test_log_rebase_complete() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        logger = ProgressLogger(Path(tmpdir))
        logger.log_rebase_complete("auth-flow-1")
        content = (Path(tmpdir) / "progress.log").read_text(encoding="utf-8")
        assert "REBASE_COMPLETE" in content
        assert "session_id=auth-flow-1" in content


def test_log_rebase_failed() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        logger = ProgressLogger(Path(tmpdir))
        logger.log_rebase_failed("auth-flow-1", "conflict in server.py")
        content = (Path(tmpdir) / "progress.log").read_text(encoding="utf-8")
        assert "REBASE_FAILED" in content
        assert "error=conflict in server.py" in content
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_progress.py::test_log_session_start -v --tb=short`
Expected: FAIL (AttributeError — method doesn't exist)

- [ ] **Step 3: Add session lifecycle methods to ProgressLogger**

Add to `src/golem/progress.py` (after the stall methods, before `sum_agent_costs`):

```python
    # -- Session lifecycle events --

    def log_session_start(self, session_id: str, spec_path: str) -> None:
        self._write(f"SESSION_START session_id={session_id} spec={spec_path}")

    def log_session_complete(self, session_id: str, status: str) -> None:
        self._write(f"SESSION_COMPLETE session_id={session_id} status={status}")

    def log_merge_queued(self, session_id: str) -> None:
        self._write(f"MERGE_QUEUED session_id={session_id}")

    def log_pr_created(self, session_id: str, pr_number: int) -> None:
        self._write(f"PR_CREATED session_id={session_id} pr={pr_number}")

    def log_pr_merged(self, session_id: str, pr_number: int) -> None:
        self._write(f"PR_MERGED session_id={session_id} pr={pr_number}")

    def log_rebase_start(self, session_id: str, onto: str) -> None:
        self._write(f"REBASE_START session_id={session_id} onto={onto}")

    def log_rebase_complete(self, session_id: str) -> None:
        self._write(f"REBASE_COMPLETE session_id={session_id}")

    def log_rebase_failed(self, session_id: str, error: str) -> None:
        self._write(f"REBASE_FAILED session_id={session_id} error={error}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_progress.py -v --tb=short`
Expected: all passed (existing + 8 new)

- [ ] **Step 5: Commit**

```bash
git add src/golem/progress.py tests/test_progress.py
git commit -m "feat: add session lifecycle events to ProgressLogger"
```

---

### Task 5: CLI Flags + Server Sub-Typer

**Files:**
- Modify: `src/golem/cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cli.py`:

```python
from click.testing import CliRunner
from golem.cli import app

# Note: typer apps use click's CliRunner under the hood
_runner = CliRunner()


def test_run_help_has_session_id() -> None:
    """--session-id flag appears in run --help."""
    result = _runner.invoke(app, ["run", "--help"])
    assert "session-id" in result.output


def test_run_help_has_golem_dir() -> None:
    """--golem-dir flag appears in run --help."""
    result = _runner.invoke(app, ["run", "--help"])
    assert "golem-dir" in result.output


def test_run_help_has_no_server() -> None:
    """--no-server flag appears in run --help."""
    result = _runner.invoke(app, ["run", "--help"])
    assert "no-server" in result.output


def test_server_start_in_help() -> None:
    """server start sub-command appears in server --help."""
    result = _runner.invoke(app, ["server", "--help"])
    assert "start" in result.output


def test_server_stop_in_help() -> None:
    """server stop sub-command appears in server --help."""
    result = _runner.invoke(app, ["server", "--help"])
    assert "stop" in result.output


def test_server_status_in_help() -> None:
    """server status sub-command appears in server --help."""
    result = _runner.invoke(app, ["server", "--help"])
    assert "status" in result.output


def test_server_status_no_running(tmp_path: Path) -> None:
    """server status with no server.json reports not running."""
    import os
    old_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        result = _runner.invoke(app, ["server", "status"])
        assert result.exit_code == 0
        assert "not running" in result.output.lower() or "no server" in result.output.lower()
    finally:
        os.chdir(old_cwd)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli.py::test_run_help_has_session_id -v --tb=short`
Expected: FAIL (assertion error — flag not in help output)

- [ ] **Step 3: Add --session-id, --golem-dir, --no-server flags to run()**

In `src/golem/cli.py`, update the `run()` command signature:

```python
@app.command()
def run(
    spec: Path = typer.Argument(..., help="Path to spec markdown file"),
    force: bool = typer.Option(False, "--force", help="Skip confirmation prompts (for CI/non-interactive)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Run planner only, skip Tech Lead execution"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose debug output"),
    no_classify: bool = typer.Option(False, "--no-classify", help="Skip complexity classification, run STANDARD pipeline"),
    session_id: str = typer.Option("", "--session-id", help="Session ID for multi-spec execution"),
    golem_dir_override: str = typer.Option("", "--golem-dir", help="Override .golem directory path"),
    no_server: bool = typer.Option(False, "--no-server", help="Run directly without server (current behavior)"),
) -> None:
```

After `_validate_spec(spec)`, add:

```python
    if golem_dir_override:
        golem_dir = Path(golem_dir_override)
    else:
        golem_dir = _get_golem_dir(project_root)
```

After `config = load_config(golem_dir)`, add:

```python
    if session_id:
        config.session_id = session_id
        config.branch_prefix = f"golem/{session_id}"
```

- [ ] **Step 4: Add server sub-typer**

In `src/golem/cli.py`, add after the `app` definition:

```python
server_app = typer.Typer(name="server", help="Manage the Golem server.")
app.add_typer(server_app, name="server")


@server_app.command()
def start(
    port: int = typer.Option(9664, "--port", help="Server port"),
    host: str = typer.Option("127.0.0.1", "--host", help="Server host"),
) -> None:
    """Start the Golem server as a background process."""
    import subprocess as sp

    project_root = _get_project_root()
    golem_dir = _get_golem_dir(project_root)
    golem_dir.mkdir(parents=True, exist_ok=True)
    server_json = golem_dir / "server.json"

    if server_json.exists():
        import json
        info = json.loads(server_json.read_text(encoding="utf-8"))
        console.print(f"Server already running (PID {info.get('pid')}, port {info.get('port')})")
        return

    proc = sp.Popen(
        ["uv", "run", "python", "-m", "uvicorn", "golem.server:create_app",
         "--factory", "--host", host, "--port", str(port)],
        cwd=str(project_root),
    )

    import json
    server_json.write_text(json.dumps({
        "pid": proc.pid,
        "port": port,
        "host": host,
    }, indent=2), encoding="utf-8")
    console.print(f"Server started (PID {proc.pid}, port {port})")


@server_app.command()
def stop() -> None:
    """Stop the running Golem server."""
    import json
    import signal

    project_root = _get_project_root()
    golem_dir = _get_golem_dir(project_root)
    server_json = golem_dir / "server.json"

    if not server_json.exists():
        console.print("No server running.")
        return

    info = json.loads(server_json.read_text(encoding="utf-8"))
    pid = info.get("pid")

    import os
    try:
        os.kill(pid, signal.SIGTERM)
        console.print(f"Server stopped (PID {pid})")
    except (ProcessLookupError, OSError):
        console.print(f"Server process {pid} not found (already stopped?)")

    server_json.unlink(missing_ok=True)


@server_app.command(name="status")
def server_status() -> None:
    """Show server status."""
    import json

    project_root = _get_project_root()
    golem_dir = _get_golem_dir(project_root)
    server_json = golem_dir / "server.json"

    if not server_json.exists():
        console.print("No server running.")
        return

    info = json.loads(server_json.read_text(encoding="utf-8"))
    console.print(f"Server running: PID={info.get('pid')}, port={info.get('port')}, host={info.get('host')}")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -v --tb=short`
Expected: all passed (existing + new)

- [ ] **Step 6: Run full suite for regression**

Run: `uv run pytest --tb=short -q`
Expected: all passed, 0 failed

- [ ] **Step 7: Commit**

```bash
git add src/golem/cli.py tests/test_cli.py
git commit -m "feat: add --session-id, --golem-dir, --no-server flags and server sub-typer"
```

---

### Task 6: Server Skeleton + Session Manager

**Files:**
- Create: `src/golem/server.py`
- Create: `tests/test_server.py`

- [ ] **Step 1: Write the failing tests for server skeleton**

Create `tests/test_server.py`:

```python
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from golem.server import (
    SessionManager,
    SessionState,
    create_app,
    monitor_process,
    remove_server_json,
    write_server_json,
)


@pytest.fixture()
def app():
    """Create a fresh FastAPI app for each test."""
    return create_app()


@pytest.fixture()
async def client(app):
    """Async test client using httpx ASGITransport."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# -- Skeleton tests (Task 6) --


def test_create_app_returns_fastapi() -> None:
    """create_app() returns a FastAPI instance."""
    from fastapi import FastAPI
    app = create_app()
    assert isinstance(app, FastAPI)


def test_session_manager_create_and_list(tmp_path: Path) -> None:
    """SessionManager.create_session creates a session; list_sessions returns it."""
    mgr = SessionManager(tmp_path)
    session = mgr.create_session("test-1", Path("spec.md"))
    assert session.id == "test-1"
    sessions = mgr.list_sessions()
    assert len(sessions) == 1
    assert sessions[0].id == "test-1"


def test_session_manager_get_missing(tmp_path: Path) -> None:
    """SessionManager.get_session returns None for unknown IDs."""
    mgr = SessionManager(tmp_path)
    assert mgr.get_session("nonexistent") is None


def test_write_remove_server_json(tmp_path: Path) -> None:
    """write_server_json creates file; remove_server_json deletes it."""
    write_server_json(tmp_path, pid=12345, port=9664)
    server_json = tmp_path / "server.json"
    assert server_json.exists()
    data = json.loads(server_json.read_text(encoding="utf-8"))
    assert data["pid"] == 12345
    assert data["port"] == 9664

    remove_server_json(tmp_path)
    assert not server_json.exists()


async def test_server_status_endpoint(client: AsyncClient) -> None:
    """GET /api/server/status returns 200 with expected keys."""
    resp = await client.get("/api/server/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "pid" in data
    assert "port" in data
    assert "uptime_seconds" in data
    assert "session_counts" in data


async def test_root_returns_html(client: AsyncClient) -> None:
    """GET / returns 200 with HTML content."""
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "html" in resp.text.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_server.py -v --tb=short`
Expected: FAIL (ModuleNotFoundError — `golem.server` doesn't exist yet)

- [ ] **Step 3: Create server.py with core types and app factory**

Create `src/golem/server.py`:

```python
from __future__ import annotations

import asyncio
import json
import os
from collections import deque
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator
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
            if s.process and s.process.returncode is None:
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_server.py -v --tb=short`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add src/golem/server.py tests/test_server.py
git commit -m "feat: add server skeleton with SessionManager, app factory, and server status"
```

---

### Task 7: Session CRUD + Lifecycle Endpoints

**Files:**
- Modify: `src/golem/server.py`
- Modify: `tests/test_server.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_server.py`:

```python
async def test_create_session_returns_id(client: AsyncClient, tmp_path: Path) -> None:
    """POST /api/sessions creates a session and returns the ID."""
    spec = tmp_path / "test-spec.md"
    spec.write_text("# Test\n\n## Task\nDo something.\n", encoding="utf-8")
    with patch("golem.server.asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_proc = MagicMock()
        mock_proc.pid = 99999
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=0)
        mock_exec.return_value = mock_proc

        resp = await client.post("/api/sessions", json={
            "spec_path": str(spec),
            "project_root": str(tmp_path),
        })
    assert resp.status_code == 200
    data = resp.json()
    assert "session_id" in data
    assert data["status"] == "running"


async def test_list_sessions_empty(client: AsyncClient) -> None:
    """GET /api/sessions returns empty list when no sessions exist."""
    resp = await client.get("/api/sessions")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_list_sessions_after_create(client: AsyncClient, tmp_path: Path) -> None:
    """After creating a session, it appears in the list."""
    spec = tmp_path / "spec.md"
    spec.write_text("# Test\n\n## Task\nDo it.\n", encoding="utf-8")
    with patch("golem.server.asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_proc = MagicMock()
        mock_proc.pid = 88888
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=0)
        mock_exec.return_value = mock_proc

        await client.post("/api/sessions", json={
            "spec_path": str(spec),
            "project_root": str(tmp_path),
        })

    resp = await client.get("/api/sessions")
    data = resp.json()
    assert len(data) >= 1
    assert any(s["status"] in ("running", "pending") for s in data)


async def test_get_session_detail(client: AsyncClient, tmp_path: Path) -> None:
    """GET /api/sessions/{id} returns session detail shape."""
    spec = tmp_path / "spec.md"
    spec.write_text("# Test\n", encoding="utf-8")
    with patch("golem.server.asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_proc = MagicMock()
        mock_proc.pid = 77777
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=0)
        mock_exec.return_value = mock_proc

        resp = await client.post("/api/sessions", json={
            "spec_path": str(spec),
            "project_root": str(tmp_path),
        })
    session_id = resp.json()["session_id"]

    resp = await client.get(f"/api/sessions/{session_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == session_id
    assert "status" in data


async def test_get_session_not_found(client: AsyncClient) -> None:
    """GET /api/sessions/{id} returns 404 for unknown ID."""
    resp = await client.get("/api/sessions/nonexistent-99")
    assert resp.status_code == 404


async def test_delete_session(client: AsyncClient, tmp_path: Path) -> None:
    """DELETE /api/sessions/{id} removes the session."""
    spec = tmp_path / "spec.md"
    spec.write_text("# Test\n", encoding="utf-8")
    with patch("golem.server.asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_proc = MagicMock()
        mock_proc.pid = 66666
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=0)
        mock_proc.terminate = MagicMock()
        mock_exec.return_value = mock_proc

        resp = await client.post("/api/sessions", json={
            "spec_path": str(spec),
            "project_root": str(tmp_path),
        })
    session_id = resp.json()["session_id"]

    resp = await client.delete(f"/api/sessions/{session_id}")
    assert resp.status_code == 200


async def test_pause_resume_session(client: AsyncClient, tmp_path: Path) -> None:
    """Pause and resume a session transitions status correctly."""
    spec = tmp_path / "spec.md"
    spec.write_text("# Test\n", encoding="utf-8")
    with patch("golem.server.asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_proc = MagicMock()
        mock_proc.pid = 55555
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=0)
        mock_proc.send_signal = MagicMock()
        mock_exec.return_value = mock_proc

        resp = await client.post("/api/sessions", json={
            "spec_path": str(spec),
            "project_root": str(tmp_path),
        })
    session_id = resp.json()["session_id"]

    # Pause
    resp = await client.post(f"/api/sessions/{session_id}/pause")
    assert resp.status_code == 200

    # Resume
    resp = await client.post(f"/api/sessions/{session_id}/resume")
    assert resp.status_code == 200


async def test_guidance_creates_ticket(client: AsyncClient, tmp_path: Path) -> None:
    """POST /api/sessions/{id}/guidance writes a guidance ticket."""
    spec = tmp_path / "spec.md"
    spec.write_text("# Test\n", encoding="utf-8")
    with patch("golem.server.asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_proc = MagicMock()
        mock_proc.pid = 44444
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=0)
        mock_exec.return_value = mock_proc

        resp = await client.post("/api/sessions", json={
            "spec_path": str(spec),
            "project_root": str(tmp_path),
        })
    session_id = resp.json()["session_id"]

    resp = await client.post(f"/api/sessions/{session_id}/guidance", json={"text": "Focus on auth first"})
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("ok") is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_server.py::test_create_session_returns_id -v --tb=short`
Expected: FAIL (404 or missing endpoint)

- [ ] **Step 3: Implement session CRUD endpoints**

Add to the `create_app()` function in `src/golem/server.py`, after the server status endpoints:

```python
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
        state.background_tasks.append(asyncio.create_task(monitor_process(state, sessions_dir)))

        return {"session_id": session_id, "status": "running"}

    @app.get("/api/sessions")
    async def list_sessions() -> list[dict[str, str]]:
        sessions = session_mgr.list_sessions()
        return [
            {
                "id": s.id,
                "status": s.status,
                "spec_path": str(s.spec_path),
                "created_at": s.created_at,
            }
            for s in sessions
        ]

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
```

- [ ] **Step 4: Implement session lifecycle endpoints**

Add to `create_app()`:

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_server.py -v --tb=short`
Expected: 14 passed

- [ ] **Step 6: Run full suite for regression**

Run: `uv run pytest --tb=short -q`
Expected: all passed, 0 failed

- [ ] **Step 7: Commit**

```bash
git add src/golem/server.py tests/test_server.py
git commit -m "feat: add session CRUD and lifecycle endpoints"
```

---

### Task 8: SSE Streams, Data Endpoints + Preserved Endpoints

**Files:**
- Modify: `src/golem/server.py`
- Modify: `tests/test_server.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_server.py`:

```python
import asyncio


async def test_session_events_sse_404(app) -> None:
    """SSE stream returns 404 for nonexistent session."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/sessions/fake-session/events")
        assert resp.status_code == 404


async def test_aggregate_events_sse(app) -> None:
    """GET /api/events returns an SSE stream (connection succeeds)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Use stream to test SSE connectivity
        async with ac.stream("GET", "/api/events") as resp:
            assert resp.status_code == 200
            # Read first chunk (should be a heartbeat or status event)
            first_chunk = b""
            async for chunk in resp.aiter_bytes():
                first_chunk = chunk
                break  # Just verify we can read one chunk
            assert len(first_chunk) > 0


async def test_session_tickets_endpoint(client: AsyncClient, tmp_path: Path) -> None:
    """GET /api/sessions/{id}/tickets returns ticket list (or 404)."""
    resp = await client.get("/api/sessions/nonexistent/tickets")
    assert resp.status_code == 404


async def test_session_cost_endpoint(client: AsyncClient) -> None:
    """GET /api/sessions/{id}/cost returns 404 for missing session."""
    resp = await client.get("/api/sessions/nonexistent/cost")
    assert resp.status_code == 404


async def test_session_plan_endpoint(client: AsyncClient) -> None:
    """GET /api/sessions/{id}/plan returns 404 for missing session."""
    resp = await client.get("/api/sessions/nonexistent/plan")
    assert resp.status_code == 404


async def test_session_diff_endpoint(client: AsyncClient) -> None:
    """GET /api/sessions/{id}/diff returns 404 for missing session."""
    resp = await client.get("/api/sessions/nonexistent/diff")
    assert resp.status_code == 404


async def test_specs_endpoint(client: AsyncClient) -> None:
    """GET /api/specs returns a list of .md files."""
    resp = await client.get("/api/specs")
    assert resp.status_code == 200
    data = resp.json()
    assert "specs" in data
    assert isinstance(data["specs"], list)


async def test_config_endpoint(client: AsyncClient) -> None:
    """GET /api/config returns a config dict."""
    resp = await client.get("/api/config")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, dict)
    assert "max_parallel" in data


async def test_preflight_endpoint(client: AsyncClient, tmp_path: Path) -> None:
    """POST /api/preflight returns check results."""
    spec = tmp_path / "spec.md"
    spec.write_text("# Test\n", encoding="utf-8")
    resp = await client.post("/api/preflight", json={
        "spec_path": str(spec),
        "project_root": str(tmp_path),
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "ready" in data


async def test_monitor_process_updates_status(tmp_path: Path) -> None:
    """monitor_process updates session status when subprocess exits."""
    from golem.server import SessionManager, monitor_process

    # Create session dir structure so monitor_process can write session.json
    sessions_dir = tmp_path / "sessions"
    session_dir = sessions_dir / "test-monitor"
    session_dir.mkdir(parents=True)
    (session_dir / "session.json").write_text(
        json.dumps({"id": "test-monitor", "status": "running", "spec_path": "spec.md"}),
        encoding="utf-8",
    )

    mgr = SessionManager(sessions_dir)
    state = mgr.create_session("test-monitor", Path("spec.md"))

    # Simulate a process that exits immediately with code 0
    mock_proc = AsyncMock()
    mock_proc.wait = AsyncMock(return_value=0)
    mock_proc.returncode = None
    mock_proc.pid = 11111
    state.process = mock_proc
    state.status = "running"

    await monitor_process(state, sessions_dir)
    assert state.status == "awaiting_merge"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_server.py::test_session_events_sse -v --tb=short`
Expected: FAIL (404 or missing endpoint)

- [ ] **Step 3: Implement per-session SSE endpoints and background tasks**

Add these background task functions **before** `create_app()` in `src/golem/server.py` (module-level):

```python
async def tail_progress_log(state: SessionState, sessions_dir: Path) -> None:
    """Tail a session's progress.log, broadcasting new lines as SSE log events."""
    log_path = sessions_dir / state.id / "progress.log"
    seek_pos = 0

    while True:
        await asyncio.sleep(0.5)
        # Exit when process has finished
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


async def monitor_process(state: SessionState, sessions_dir: Path) -> None:
    """Wait for a session's subprocess to exit and update session.json status."""
    if state.process is None:
        return
    exit_code = await state.process.wait()
    await asyncio.sleep(1.0)  # Let tailing catch final lines

    from golem.session import read_session, write_session

    session_dir = sessions_dir / state.id
    if exit_code == 0:
        state.status = "awaiting_merge"
    else:
        state.status = "failed"

    # Update session.json on disk
    if (session_dir / "session.json").exists():
        meta = read_session(session_dir)
        meta.status = state.status
        write_session(session_dir, meta)

    await state.event_queue.put(format_sse("status", {"state": state.status, "exit_code": exit_code}))
```

Then add to `create_app()`:

```python
    # ------------------------------------------------------------------
    # SSE event streams
    # ------------------------------------------------------------------

    @app.get("/api/sessions/{session_id}/events")
    async def session_events(session_id: str) -> StreamingResponse:
        from fastapi import HTTPException
        from fastapi.responses import StreamingResponse

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
        from fastapi.responses import StreamingResponse

        aggregate_queue: asyncio.Queue[str] = asyncio.Queue()

        async def _stream() -> AsyncGenerator[str, None]:
            yield format_sse("status", {"state": "connected"})
            while True:
                try:
                    event_str = await asyncio.wait_for(aggregate_queue.get(), timeout=15.0)
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
```

- [ ] **Step 4: Implement per-session data endpoints**

Add to `create_app()`:

```python
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
        # Run git diff for session's worktree branches
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
        from fastapi import HTTPException
        import re

        state = session_mgr.get_session(session_id)
        if not state:
            raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")
        log_path = sessions_dir / session_id / "progress.log"
        total = 0.0
        if log_path.exists():
            for line in log_path.read_text(encoding="utf-8").splitlines():
                m = re.search(r"AGENT_COST.*cost=\$([0-9.]+)", line)
                if m:
                    total += float(m.group(1))
        return {"session_id": session_id, "total_cost_usd": round(total, 6)}

    @app.get("/api/sessions/{session_id}/plan")
    async def session_plan(session_id: str) -> dict[str, str]:
        from fastapi import HTTPException

        state = session_mgr.get_session(session_id)
        if not state:
            raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")
        plan_path = sessions_dir / session_id / "plans" / "overview.md"
        if not plan_path.exists():
            return {"session_id": session_id, "plan": ""}
        return {"session_id": session_id, "plan": plan_path.read_text(encoding="utf-8")}
```

- [ ] **Step 5: Implement preserved endpoints from ui.py**

Add to `create_app()`:

```python
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
```

Note: `format_sse` and `StreamingResponse` are already imported at the top of `server.py` (from Step 3 of Task 6).

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_server.py -v --tb=short`
Expected: 24 passed

- [ ] **Step 7: Run full suite for regression**

Run: `uv run pytest --tb=short -q`
Expected: all passed, 0 failed

- [ ] **Step 8: Commit**

```bash
git add src/golem/server.py tests/test_server.py
git commit -m "feat: add SSE streams, data endpoints, and preserved endpoints to server"
```

---

## Phase 1 Completion Gate

**Phase 1 is NOT complete until every check below passes.** If any check fails, return to the responsible task, fix the issue, and re-run this entire gate.

### Gate 1: All New Files Exist and Are Non-Empty

```bash
cd F:/Tools/Projects/golem-cli
for f in src/golem/session.py src/golem/server.py tests/test_session.py tests/test_server.py; do
  test -s "$f" && echo "$f: PASS" || echo "$f: FAIL"
done
```

Expected: all PASS

### Gate 2: Core Imports + Field Checks

```bash
cd F:/Tools/Projects/golem-cli
uv run python -c "
from golem.session import SessionMetadata, generate_session_id, create_session_dir
from golem.server import create_app, SessionManager, SessionState
from golem.config import GolemConfig
from golem.tickets import Ticket
from golem.worktree import create_worktree
from golem.progress import ProgressLogger

c = GolemConfig()
assert hasattr(c, 'session_id'), 'FAIL: GolemConfig missing session_id'
assert hasattr(c, 'branch_prefix'), 'FAIL: GolemConfig missing branch_prefix'

t = Ticket.__dataclass_fields__
assert 'session_id' in t, 'FAIL: Ticket missing session_id field'

import inspect
sig = inspect.signature(create_worktree)
assert 'branch_prefix' in sig.parameters, 'FAIL: create_worktree missing branch_prefix param'

for m in ['log_session_start', 'log_session_complete', 'log_merge_queued', 'log_pr_created', 'log_pr_merged', 'log_rebase_start', 'log_rebase_complete', 'log_rebase_failed']:
    assert hasattr(ProgressLogger, m), f'FAIL: ProgressLogger missing {m}'

print('IMPORTS: PASS')
"
```

Expected: `IMPORTS: PASS`

### Gate 3: CLI Flags

```bash
cd F:/Tools/Projects/golem-cli
uv run golem run --help 2>&1 | grep -q "session-id" && echo "FLAG_SESSION: PASS" || echo "FLAG_SESSION: FAIL"
uv run golem run --help 2>&1 | grep -q "golem-dir" && echo "FLAG_DIR: PASS" || echo "FLAG_DIR: FAIL"
uv run golem run --help 2>&1 | grep -q "no-server" && echo "FLAG_NOSERVER: PASS" || echo "FLAG_NOSERVER: FAIL"
uv run golem server --help 2>&1 | grep -q "start" && echo "SERVER_START: PASS" || echo "SERVER_START: FAIL"
uv run golem server --help 2>&1 | grep -q "stop" && echo "SERVER_STOP: PASS" || echo "SERVER_STOP: FAIL"
uv run golem server --help 2>&1 | grep -q "status" && echo "SERVER_STATUS: PASS" || echo "SERVER_STATUS: FAIL"
```

Expected: all 6 PASS

### Gate 4: Server Route Coverage

```bash
cd F:/Tools/Projects/golem-cli
uv run python -c "
from golem.server import create_app
app = create_app()
routes = [r.path for r in app.routes if hasattr(r, 'path')]
required = [
    '/',
    '/api/sessions',
    '/api/sessions/{session_id}',
    '/api/events',
    '/api/server/status',
    '/api/specs',
    '/api/config',
]
missing = [r for r in required if r not in routes]
if missing:
    print(f'ROUTE_COVERAGE: FAIL -- missing: {missing}')
else:
    print(f'ROUTE_COVERAGE: PASS ({len(routes)} routes)')
"
```

Expected: `ROUTE_COVERAGE: PASS`

### Gate 5: Full Test Suite

```bash
cd F:/Tools/Projects/golem-cli
uv run pytest -v --tb=short 2>&1 | tail -5
```

Expected: `[N] passed` (must be >= 382 -- 350 existing + ~8 session + ~7 config/ticket/worktree/progress + ~7 cli + ~24 server, 0 failed)

### Gate 6: Backward Compatibility

```bash
cd F:/Tools/Projects/golem-cli
uv run golem run nonexistent.md --no-server 2>&1 | grep -q "not found\|does not exist\|No such file" && echo "NOSERVER_COMPAT: PASS" || echo "NOSERVER_COMPAT: FAIL"
uv run golem version 2>&1 | grep -q "golem" && echo "VERSION_CMD: PASS" || echo "VERSION_CMD: FAIL"
uv run golem doctor 2>&1 | grep -q "git\|uv\|claude" && echo "DOCTOR_CMD: PASS" || echo "DOCTOR_CMD: FAIL"
```

Expected: all 3 PASS

### Phase 1 Verdict

Run all 6 gates. If **all gates pass**, Phase 1 is complete.
If **any gate fails**, identify the responsible task from the table below, fix it, and re-run the full gate sequence.

| Gate | Validates Tasks |
|------|----------------|
| Gate 1 | Task 1 (session.py), Tasks 6-8 (server.py) |
| Gate 2 | Tasks 1-4 (all module changes) |
| Gate 3 | Task 5 (CLI flags + server sub-typer) |
| Gate 4 | Tasks 6-8 (server endpoints) |
| Gate 5 | All tasks (regression + new test count) |
| Gate 6 | Task 5 (backward compat) |
