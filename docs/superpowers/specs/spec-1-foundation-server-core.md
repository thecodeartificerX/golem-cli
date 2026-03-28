# Spec 1: Foundation + Server Core

> Part 1 of 5 in the Multi-Spec Orchestration series.
> Full design doc: `docs/superpowers/specs/2026-03-27-multi-spec-orchestration-design.md`
> **Depends on:** Nothing — this is the foundation.
> **Execution order:** Run FIRST. Specs 2, 3, 4 depend on this being merged.

## Context

Golem currently handles one spec per run. We're adding multi-spec concurrent execution where each spec runs as an isolated session. This spec builds the foundation: session state namespacing, a FastAPI server that manages sessions as subprocesses, and the core REST API.

### Key Architecture

- `.golem/sessions/<session-id>/` replaces the flat `.golem/` directory — each session gets its own tickets, plans, progress.log, worktrees
- `.golem/server.json` stores server PID/port for CLI discovery
- `.golem/coordinator/` stores merge queue state (stubbed here, implemented in Spec 3)
- Git branches change from `golem/<group>` to `golem/<session-id>/<group>`
- Server spawns `uv run golem run <spec> --session-id <id> --golem-dir .golem/sessions/<id>` per session
- `--no-server` flag preserves current single-spec behavior

### Session Metadata Schema (`session.json`)

```json
{
  "id": "auth-flow-1",
  "spec_path": "specs/auth-flow.md",
  "status": "running",
  "complexity": "STANDARD",
  "created_at": "2026-03-27T14:00:00Z",
  "updated_at": "2026-03-27T14:12:00Z",
  "pid": 12345,
  "pr_number": null,
  "pr_url": null,
  "merged_at": null,
  "archived_at": null,
  "cost_usd": 1.24,
  "error": null
}
```

Status values: `pending` -> `running` -> `awaiting_merge` -> `pr_open` -> `merged` -> `archived`. Terminal states: `failed`, `paused`, `conflict`.

### Coding Conventions

- **Python 3.12+**, async-first, strict typing, no `Any`
- **Always `encoding="utf-8"`** on all file I/O — Windows defaults to cp1252
- **No emoji in CLI/TUI output** — Rich crashes on Windows cp1252 console
- **Formatter:** ruff, line length 120
- **Tests:** pytest with pytest-asyncio, use `tmp_path` fixture (not `tempfile.TemporaryDirectory`)
- **Pydantic models must be module-level** — not inside `create_app()`, or FastAPI gets 422 errors
- **SSE tests:** drive generator directly with `async for` + early `break` + `aclose()` — `TestClient` hangs on infinite SSE
- **Use `monkeypatch.setattr` for module globals** in tests, never direct assignment

---

## Task 1: Session Module

**Files:**
- Create: `src/golem/session.py`

- [ ] **Step 1: Create session.py with core types**
  Create `src/golem/session.py` with:
  - `SessionMetadata` dataclass: `id`, `spec_path`, `status`, `complexity`, `created_at`, `updated_at`, `pid`, `pr_number`, `pr_url`, `merged_at`, `archived_at`, `cost_usd`, `error`
  - Status constants: `PENDING`, `RUNNING`, `AWAITING_MERGE`, `PR_OPEN`, `MERGED`, `ARCHIVED`, `FAILED`, `PAUSED`, `CONFLICT`
  - `generate_session_id(spec_path: Path, sessions_dir: Path) -> str` — slugify spec stem + incrementing suffix:
    ```python
    def generate_session_id(spec_path: Path, sessions_dir: Path) -> str:
        slug = spec_path.stem.lower().replace(" ", "-").replace("_", "-")
        slug = re.sub(r"[^a-z0-9-]", "", slug)
        slug = slug[:40]
        existing = [d.name for d in sessions_dir.iterdir() if d.is_dir()] if sessions_dir.exists() else []
        n = 1
        while f"{slug}-{n}" in existing:
            n += 1
        return f"{slug}-{n}"
    ```
  - `read_session(session_dir: Path) -> SessionMetadata` — read `session.json` with `encoding="utf-8"`
  - `write_session(session_dir: Path, meta: SessionMetadata) -> None` — write `session.json` with `encoding="utf-8"`
  - `create_session_dir(sessions_dir: Path, session_id: str, spec_path: Path) -> Path` — creates `.golem/sessions/<id>/` with subdirs: `tickets/`, `plans/`, `research/`, `references/`, `reports/`, `worktrees/`, copies spec as `spec.md`, writes initial `session.json`

- [ ] **Step 2: Create test_session.py**
  Create `tests/test_session.py` with tests:
  - `test_generate_session_id_basic` — slug from filename
  - `test_generate_session_id_increment` — collision avoidance
  - `test_generate_session_id_special_chars` — strips non-alphanumeric
  - `test_generate_session_id_long_name` — truncates to 40 chars
  - `test_session_metadata_roundtrip` — write then read
  - `test_create_session_dir_structure` — all subdirs created
  - `test_create_session_dir_spec_copy` — spec.md is immutable copy
  - `test_status_transitions` — valid transitions from each state

- [ ] **Step 3: Commit**
  ```bash
  git add src/golem/session.py tests/test_session.py
  git commit -m "feat: add session module with ID generation, metadata I/O, and dir scaffolding"
  ```

#### Completion Gate

All checks must pass. If any fail, fix and re-run all checks before proceeding.

```bash
cd F:/Tools/Projects/golem-cli

# 1. Module imports cleanly
uv run python -c "from golem.session import SessionMetadata, generate_session_id, read_session, write_session, create_session_dir; print('IMPORT: PASS')"

# 2. Tests pass
uv run pytest tests/test_session.py -v --tb=short 2>&1 | tail -1
```

Expected:
```
IMPORT: PASS
8 passed
```

---

## Task 2: Config & Ticket Extensions

**Files:**
- Modify: `src/golem/config.py`
- Modify: `src/golem/tickets.py`

- [ ] **Step 1: Add session fields to GolemConfig**
  Add to `GolemConfig` dataclass:
  - `session_id: str = ""`
  - `branch_prefix: str = "golem"`
  - `merge_auto_rebase: bool = True`
  - `archive_delay_minutes: int = 30`

- [ ] **Step 2: Add session_id to Ticket**
  Add `session_id: str = ""` field to the `Ticket` dataclass in `tickets.py`. Default empty string for backward compat with existing ticket JSON files.

- [ ] **Step 3: Update existing tests**
  - In `tests/test_config.py`: add tests for new fields (defaults, serialization roundtrip, validation)
  - In `tests/test_tickets.py`: add test that `session_id` persists through write/read cycle, and that tickets without `session_id` load with empty default

- [ ] **Step 4: Commit**
  ```bash
  git add src/golem/config.py src/golem/tickets.py tests/test_config.py tests/test_tickets.py
  git commit -m "feat: add session_id and branch_prefix to GolemConfig and Ticket"
  ```

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. New config fields accessible
uv run python -c "
from golem.config import GolemConfig
c = GolemConfig()
assert c.session_id == '', f'FAIL: session_id={c.session_id!r}'
assert c.branch_prefix == 'golem', f'FAIL: branch_prefix={c.branch_prefix!r}'
assert c.merge_auto_rebase is True
assert c.archive_delay_minutes == 30
print('CONFIG_FIELDS: PASS')
"

# 2. Ticket session_id field
uv run python -c "
from golem.tickets import Ticket, TicketContext
t = Ticket(id='T-1', type='task', title='x', status='pending', priority='high', created_by='test', assigned_to='test', context=TicketContext(), session_id='auth-flow-1')
assert t.session_id == 'auth-flow-1'
print('TICKET_FIELD: PASS')
"

# 3. All existing + new tests pass
uv run pytest tests/test_config.py tests/test_tickets.py -v --tb=short 2>&1 | tail -1
```

Expected:
```
CONFIG_FIELDS: PASS
TICKET_FIELD: PASS
[N] passed
```

---

## Task 3: Worktree Branch Namespacing

**Files:**
- Modify: `src/golem/worktree.py`
- Modify: `src/golem/tech_lead.py`

- [ ] **Step 1: Add branch_prefix to create_worktree()**
  Add `branch_prefix: str = "golem"` parameter to `create_worktree()`. Change branch name construction from hardcoded `golem/<group>` to `{branch_prefix}/<group>`.

- [ ] **Step 2: Update tech_lead.py**
  - `_ensure_merged_to_main()`: accept `branch_prefix` parameter, filter `git branch --list {branch_prefix}/*/integration`
  - `run_tech_lead()`: compute `branch_prefix = f"golem/{config.session_id}"` if `config.session_id` is set, else `"golem"`. Pass to worktree/merge calls.

- [ ] **Step 3: Update worktree tests**
  In `tests/test_worktree.py`: add tests for `branch_prefix` parameter — verify branch names include prefix, default still works.

- [ ] **Step 4: Commit**
  ```bash
  git add src/golem/worktree.py src/golem/tech_lead.py tests/test_worktree.py
  git commit -m "feat: add branch_prefix to worktree/tech_lead for session-scoped branches"
  ```

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. create_worktree accepts branch_prefix
uv run python -c "
import inspect
from golem.worktree import create_worktree
sig = inspect.signature(create_worktree)
assert 'branch_prefix' in sig.parameters, 'FAIL: branch_prefix not in signature'
print('WORKTREE_SIG: PASS')
"

# 2. All worktree + tech_lead tests pass
uv run pytest tests/test_worktree.py tests/test_tech_lead.py -v --tb=short 2>&1 | tail -1

# 3. Full existing suite still passes
uv run pytest --tb=short -q 2>&1 | tail -1
```

Expected:
```
WORKTREE_SIG: PASS
[N] passed
[N] passed
```

---

## Task 4: Progress Events

**Files:**
- Modify: `src/golem/progress.py`

- [ ] **Step 1: Add session lifecycle event methods**
  Add to `ProgressLogger`:
  - `log_session_start(session_id: str, spec_path: str)` -> `SESSION_START session_id=<id> spec=<path>`
  - `log_session_complete(session_id: str, status: str)` -> `SESSION_COMPLETE session_id=<id> status=<status>`
  - `log_merge_queued(session_id: str)` -> `MERGE_QUEUED session_id=<id>`
  - `log_pr_created(session_id: str, pr_number: int)` -> `PR_CREATED session_id=<id> pr=<number>`
  - `log_pr_merged(session_id: str, pr_number: int)` -> `PR_MERGED session_id=<id> pr=<number>`
  - `log_rebase_start(session_id: str, onto: str)` -> `REBASE_START session_id=<id> onto=<branch>`
  - `log_rebase_complete(session_id: str)` -> `REBASE_COMPLETE session_id=<id>`
  - `log_rebase_failed(session_id: str, error: str)` -> `REBASE_FAILED session_id=<id> error=<msg>`

- [ ] **Step 2: Add tests**
  In `tests/test_progress.py`: add tests for each new event method — verify format and key=value parsing.

- [ ] **Step 3: Commit**
  ```bash
  git add src/golem/progress.py tests/test_progress.py
  git commit -m "feat: add session lifecycle events to ProgressLogger"
  ```

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. New methods exist
uv run python -c "
from golem.progress import ProgressLogger
methods = ['log_session_start', 'log_session_complete', 'log_merge_queued', 'log_pr_created', 'log_pr_merged', 'log_rebase_start', 'log_rebase_complete', 'log_rebase_failed']
for m in methods:
    assert hasattr(ProgressLogger, m), f'FAIL: missing {m}'
print(f'PROGRESS_METHODS: PASS ({len(methods)} methods)')
"

# 2. Tests pass
uv run pytest tests/test_progress.py -v --tb=short 2>&1 | tail -1
```

Expected:
```
PROGRESS_METHODS: PASS (8 methods)
[N] passed
```

---

## Task 5: CLI Flags + Server Sub-Typer

**Files:**
- Modify: `src/golem/cli.py`

- [ ] **Step 1: Add --session-id and --golem-dir flags to run()**
  Add to `run()` command:
  - `--session-id TEXT` — optional, passed to planner/tech_lead
  - `--golem-dir PATH` — optional, overrides `Path.cwd() / ".golem"`
  - `--no-server` — boolean flag, when set uses current direct execution behavior
  Modify `_get_golem_dir()` to accept optional override path.
  When `--session-id` is set, populate `config.session_id` and `config.branch_prefix`.

- [ ] **Step 2: Add server sub-typer**
  Create `server_app = typer.Typer(name="server")` with commands:
  - `start` — launches server via `uvicorn` as background subprocess, writes `.golem/server.json`
  - `stop` — reads `server.json`, sends stop signal, removes `server.json`
  - `status` — reads `server.json`, prints server info or "not running"
  Register with `app.add_typer(server_app, name="server")`.

- [ ] **Step 3: Add CLI routing in run()**
  When `--no-server` is NOT set:
  - Call `find_server()` (stub for now, reads `server.json`)
  - If server not found, call `server start` logic to auto-start
  - POST to `/api/sessions` with spec path
  - Stream SSE from `/api/sessions/<id>/events` to console
  When `--no-server` IS set: current behavior unchanged.

- [ ] **Step 4: Update CLI tests**
  In `tests/test_cli.py`: add tests for new flags (`--session-id`, `--golem-dir`, `--no-server`), `server start/stop/status` commands (mock subprocess).

- [ ] **Step 5: Commit**
  ```bash
  git add src/golem/cli.py tests/test_cli.py
  git commit -m "feat: add --session-id, --golem-dir, --no-server flags and server sub-typer"
  ```

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. New flags appear in help
uv run golem run --help 2>&1 | grep -q "session-id" && echo "FLAG_SESSION: PASS" || echo "FLAG_SESSION: FAIL"
uv run golem run --help 2>&1 | grep -q "golem-dir" && echo "FLAG_DIR: PASS" || echo "FLAG_DIR: FAIL"
uv run golem run --help 2>&1 | grep -q "no-server" && echo "FLAG_NOSERVER: PASS" || echo "FLAG_NOSERVER: FAIL"

# 2. Server sub-typer registered
uv run golem server --help 2>&1 | grep -q "start" && echo "SERVER_START: PASS" || echo "SERVER_START: FAIL"
uv run golem server --help 2>&1 | grep -q "stop" && echo "SERVER_STOP: PASS" || echo "SERVER_STOP: FAIL"
uv run golem server --help 2>&1 | grep -q "status" && echo "SERVER_STATUS: PASS" || echo "SERVER_STATUS: FAIL"

# 3. CLI tests pass
uv run pytest tests/test_cli.py -v --tb=short 2>&1 | tail -1
```

Expected:
```
FLAG_SESSION: PASS
FLAG_DIR: PASS
FLAG_NOSERVER: PASS
SERVER_START: PASS
SERVER_STOP: PASS
SERVER_STATUS: PASS
[N] passed
```

---

## Task 6: Server Skeleton + Session Manager

**Files:**
- Create: `src/golem/server.py`

This task creates the server module with its core infrastructure. Do NOT implement endpoints yet — those come in Tasks 7 and 8.

- [ ] **Step 1: Create server.py with core types and app factory**
  Create `src/golem/server.py` with `create_app() -> FastAPI`. Include:
  - `SessionState` dataclass: `id`, `process`, `status`, `config`, `event_queue`, `log_buffer`, `background_tasks`
  - `SessionManager` class with methods: `create_session()`, `pause_session()`, `resume_session()`, `kill_session()`, `archive_session()`, `get_session()`, `list_sessions()`
  - Stub `MergeCoordinator` class (empty methods, implemented in Spec 3)
  - Server lifecycle helpers: `write_server_json(golem_dir, pid, port)`, `remove_server_json(golem_dir)`, lifespan handler that calls remove on shutdown
  - **Pydantic request models must be module-level** (not inside `create_app()`)
  - Module-level `CreateSessionRequest(BaseModel)`: `spec_path: str`, `project_root: str = ""`
  - `_startup_time` module-level datetime for uptime tracking
  - Use `datetime.UTC` (not `timezone.utc`) per ruff UP017

- [ ] **Step 2: Implement the root endpoint and server status**
  Inside `create_app()`, register:
  - `GET /` — serve dashboard HTML from `ui_template.html` (same as current `ui.py`)
  - `GET /api/server/status` — return `{"pid", "port", "uptime_seconds", "session_counts": {by_status}}`
  - `POST /api/server/stop` — graceful shutdown

- [ ] **Step 3: Write initial tests for server skeleton**
  Create `tests/test_server.py` with tests:
  - `test_create_app_returns_fastapi` — app factory returns FastAPI instance
  - `test_session_manager_create_and_list` — create session, verify it appears in list
  - `test_session_manager_get_missing` — returns None for unknown ID
  - `test_write_remove_server_json` — write creates file, remove deletes it
  - `test_server_status_endpoint` — GET `/api/server/status` returns 200 with expected keys
  - `test_root_returns_html` — GET `/` returns 200 with HTML content
  Use `httpx.AsyncClient` with `ASGITransport` (no running server needed).

- [ ] **Step 4: Commit**
  ```bash
  git add src/golem/server.py tests/test_server.py
  git commit -m "feat: add server skeleton with SessionManager, app factory, and server status"
  ```

#### Completion Gate

All checks must pass. If any fail, fix and re-run all checks before proceeding.

```bash
cd F:/Tools/Projects/golem-cli

# 1. Core types import
uv run python -c "
from golem.server import create_app, SessionManager, SessionState, write_server_json, remove_server_json
app = create_app()
print('SERVER_SKELETON: PASS')
"

# 2. Minimum routes registered (root + server status + stop = 3)
uv run python -c "
from golem.server import create_app
app = create_app()
routes = [r.path for r in app.routes if hasattr(r, 'path')]
assert '/' in routes, f'FAIL: / missing from {routes}'
assert '/api/server/status' in routes, f'FAIL: /api/server/status missing'
print(f'SKELETON_ROUTES: PASS ({len(routes)} routes)')
"

# 3. Skeleton tests pass (exactly 6)
uv run pytest tests/test_server.py -v --tb=short 2>&1 | tail -1
```

Expected:
```
SERVER_SKELETON: PASS
SKELETON_ROUTES: PASS ([N] routes)
6 passed
```

---

## Task 7: Session CRUD + Lifecycle Endpoints

**Files:**
- Modify: `src/golem/server.py`
- Modify: `tests/test_server.py`

This task adds all session management endpoints. Each endpoint must be tested before moving on.

- [ ] **Step 1: Implement session CRUD endpoints**
  Add to `create_app()`:
  - `POST /api/sessions` — accepts `CreateSessionRequest`, creates session dir via `create_session_dir()`, spawns subprocess via `asyncio.create_subprocess_exec`, returns `{"session_id": str, "status": "running"}`
  - `GET /api/sessions` — list all sessions with status, returns `[{"id", "status", "spec_path", "created_at"}]`
  - `GET /api/sessions/{session_id}` — session detail (config, tickets, cost from progress.log)
  - `DELETE /api/sessions/{session_id}` — kill subprocess + archive session

- [ ] **Step 2: Implement session lifecycle endpoints**
  - `POST /api/sessions/{session_id}/pause` — send SIGSTOP/suspend to subprocess, update status
  - `POST /api/sessions/{session_id}/resume` — send SIGCONT/resume, update status
  - `POST /api/sessions/{session_id}/guidance` — write operator guidance ticket to session's ticket dir using `TicketStore`

- [ ] **Step 3: Add CRUD + lifecycle tests**
  Add to `tests/test_server.py`:
  - `test_create_session_returns_id` — POST `/api/sessions` returns session_id (mock subprocess)
  - `test_list_sessions_empty` — GET `/api/sessions` returns empty list initially
  - `test_list_sessions_after_create` — session appears after creation
  - `test_get_session_detail` — GET `/api/sessions/{id}` returns expected shape
  - `test_get_session_not_found` — returns 404 for unknown ID
  - `test_delete_session` — DELETE removes session, returns success
  - `test_pause_resume_session` — status transitions correctly
  - `test_guidance_creates_ticket` — POST guidance writes ticket JSON to session dir
  Mock `asyncio.create_subprocess_exec` in all tests that spawn processes.

- [ ] **Step 4: Run tests and commit**
  ```bash
  uv run pytest tests/test_server.py -v --tb=short
  git add src/golem/server.py tests/test_server.py
  git commit -m "feat: add session CRUD and lifecycle endpoints"
  ```

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. Session endpoints registered
uv run python -c "
from golem.server import create_app
app = create_app()
routes = [r.path for r in app.routes if hasattr(r, 'path')]
required = ['/api/sessions', '/api/sessions/{session_id}']
missing = [r for r in required if r not in routes]
assert not missing, f'FAIL: missing {missing}'
print(f'CRUD_ROUTES: PASS')
"

# 2. Server tests pass (skeleton 6 + CRUD/lifecycle 8 = 14)
uv run pytest tests/test_server.py -v --tb=short 2>&1 | tail -1

# 3. Full suite still passes
uv run pytest --tb=short -q 2>&1 | tail -1
```

Expected:
```
CRUD_ROUTES: PASS
14 passed
[N] passed, 0 failed
```

---

## Task 8: SSE Streams, Data Endpoints + Preserved Endpoints

**Files:**
- Modify: `src/golem/server.py`
- Modify: `tests/test_server.py`

This task adds real-time event streaming, per-session data access, and ports over endpoints from the existing `ui.py`.

- [ ] **Step 1: Implement per-session SSE**
  - `GET /api/sessions/{session_id}/events` — SSE stream tailing session's `progress.log`
  - `GET /api/events` — aggregate SSE stream prefixing each event with session ID
  - Each session gets its own `event_queue` (`asyncio.Queue`) and `log_buffer` (`deque`, maxlen=200)
  - Background tasks: `tail_progress_log()` reads new lines from progress.log, `monitor_process()` awaits subprocess exit and updates `session.json` to `awaiting_merge` (exit 0) or `failed` (non-zero)

- [ ] **Step 2: Implement per-session data endpoints**
  - `GET /api/sessions/{session_id}/tickets` — list tickets from session's ticket dir via `TicketStore`
  - `GET /api/sessions/{session_id}/diff` — run `git diff` for session's worktree branches
  - `GET /api/sessions/{session_id}/cost` — parse `AGENT_COST` lines from session's `progress.log`
  - `GET /api/sessions/{session_id}/plan` — read session's `plans/overview.md`

- [ ] **Step 3: Preserve existing endpoints from ui.py**
  Port these from the existing `ui.py` module:
  - `GET /api/specs` — find .md files in project
  - `GET /api/browse/file` — native file picker dialog
  - `GET /api/browse/folder` — native folder picker dialog
  - `GET /api/config` — server-level config defaults
  - `POST /api/preflight` — pre-run tool/environment check

- [ ] **Step 4: Add SSE + data + preserved endpoint tests**
  Add to `tests/test_server.py`:
  - `test_session_events_sse` — SSE stream yields events (use `async for` with early `break` + `aclose()`, NOT `TestClient`)
  - `test_aggregate_events_sse` — aggregate stream prefixes session ID
  - `test_session_tickets_endpoint` — returns ticket list from session dir
  - `test_session_cost_endpoint` — parses AGENT_COST from progress.log
  - `test_session_plan_endpoint` — returns plan content or 404
  - `test_session_diff_endpoint` — returns diff output
  - `test_specs_endpoint` — lists .md files
  - `test_config_endpoint` — returns config dict
  - `test_preflight_endpoint` — runs checks and returns results
  - `test_monitor_process_updates_status` — subprocess exit updates session.json

- [ ] **Step 5: Run tests and commit**
  ```bash
  uv run pytest tests/test_server.py -v --tb=short
  uv run pytest --tb=short -q
  git add src/golem/server.py tests/test_server.py
  git commit -m "feat: add SSE streams, data endpoints, and preserved endpoints to server"
  ```

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. All endpoint categories registered
uv run python -c "
from golem.server import create_app
app = create_app()
routes = [r.path for r in app.routes if hasattr(r, 'path')]
required = [
    '/api/sessions', '/api/events', '/api/server/status',
    '/api/specs', '/api/config',
]
missing = [r for r in required if r not in routes]
assert not missing, f'FAIL: missing {missing}'
# Verify parameterized routes exist
param_routes = [r for r in routes if '{session_id}' in r]
assert len(param_routes) >= 5, f'FAIL: only {len(param_routes)} session param routes'
print(f'ALL_ROUTES: PASS ({len(routes)} total routes)')
"

# 2. Server tests pass (skeleton 6 + CRUD 8 + SSE/data/preserved 10 = 24)
uv run pytest tests/test_server.py -v --tb=short 2>&1 | tail -1

# 3. Full suite still passes
uv run pytest --tb=short -q 2>&1 | tail -1
```

Expected:
```
ALL_ROUTES: PASS ([N] total routes)
24 passed
[N] passed, 0 failed
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

Expected: `[N] passed` (must be >= 382 — 350 existing + ~8 session + ~6 config/ticket/worktree/progress + ~24 server, 0 failed)

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
