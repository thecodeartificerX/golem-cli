# Multi-Spec Orchestration — Design Document

## Problem

Golem currently handles exactly one spec file per run. The entire `.golem/` state directory is flat and global — tickets auto-number from TICKET-001, worktree branches are `golem/<group>` with no qualifier, the UI enforces a single running process, and `golem clean` wipes everything. To run a second spec, you must wait for the first to finish, clean state, then start over.

Real projects have multiple independent issues to work on simultaneously. A user with five contained spec files should be able to fire all five, monitor them in parallel, and merge each independently — without them stepping on each other's state, branches, or worktrees.

## Design Decisions (Agreed)

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Concurrency model | Concurrent — multiple specs run in parallel | Maximizes throughput; each spec is self-contained |
| Architecture | Hybrid: subprocess per pipeline + in-process coordinator | Crash isolation from subprocesses; smart cross-session coordination in-process |
| Session lifecycle | Hybrid: persist until merged, then auto-archive | Matches mental model of a ticket board |
| Spec ingestion | CLI feeds server — `golem run` always routes through server | Single source of truth; CLI becomes power-user debug/steer interface |
| Session identity | Spec filename + incrementing ID (e.g. `auth-flow-1`) | Human-readable, collision-free |
| Server lifecycle | Auto-start on first `golem run`, manual stop via `golem server stop` | Zero friction to start, no surprise shutdowns |
| Merge strategy | Optimistic + rebase-on-conflict via PR status | PRs are natural conflict surface; auto-rebase handles common case |
| UI model | Full dashboard: session sidebar + detail view + merge queue + aggregate stats | Multi-session needs a project-board UI, not a single-run viewer |
| Spec decomposition | 5 ZeroShot-ready specs, sequential foundation then parallel features | Each spec is independent once foundation is merged |

---

## Architecture Overview

```
                    +-----------------------+
                    |    FastAPI Server      |
                    |    (port 9664)         |
                    |                        |
                    |  +------------------+  |
          CLI -------->| Session Manager  |  |
        (thin       |  +------------------+  |
        client)     |  | Merge Coordinator|  |
                    |  +------------------+  |
          UI --------->| API Layer (REST) |  |
        (browser)   |  | SSE Streams      |  |
                    +--+---+---+---+------+--+
                       |   |   |   |
              subprocess spawns (one per session)
                       |   |   |   |
                    +--v-+ | +-v--+|
                    |S1  | | |S3  ||
                    |auth| | |user||
                    +----+ | +----+|
                      +----v-+  +--v---+
                      |S2    |  |S4    |
                      |pay   |  |search|
                      +------+  +------+

  Each Sx = full golem pipeline (planner -> tech_lead -> writers)
            running in its own namespaced .golem/sessions/<id>/
            with git branches golem/<id>/<group>
```

### Component Responsibilities

**Server (single process):**
- Manages session lifecycle (create, pause, resume, kill, archive)
- Spawns one subprocess per session
- Tails each session's progress.log for SSE events
- Runs merge coordinator logic in-process
- Serves dashboard UI

**CLI (thin client):**
- Sends commands to server via HTTP
- Streams SSE events to terminal for real-time log output
- Falls back to direct execution with `--no-server` for CI/debugging

**Session subprocess:**
- Standard `golem run` with `--session-id` and `--golem-dir` flags
- Runs planner -> tech_lead -> writers in a namespaced state directory
- Writes progress.log, tickets, plans to its own session dir
- Creates worktrees with namespaced branches
- Exits when pipeline completes (success or failure)

**Merge coordinator (in-process in server):**
- Watches for sessions reaching `awaiting_merge` status
- Maintains FIFO merge queue
- Creates PRs via `gh` CLI
- After each merge: rebases remaining queued sessions onto updated main
- Escalates to user on rebase failure or QA failure post-rebase

---

## Session State & Namespacing

### Current State (Single-Spec)

```
.golem/
  config.json
  progress.log
  tickets/
  plans/
  research/
  references/
  reports/
  worktrees/
```

### New State (Multi-Spec)

```
.golem/
  server.json                      # Server PID, port, host, startup time
  sessions/
    auth-flow-1/
      config.json                  # Per-session config snapshot
      progress.log                 # Per-session event log
      spec.md                      # Immutable copy of original spec
      session.json                 # Session metadata (see schema below)
      tickets/
        TICKET-001.json
        TICKET-002.json
      plans/
        overview.md
        task-auth-middleware.md
      research/
      references/
      worktrees/
        group-1/                   # git worktree: branch golem/auth-flow-1/group-1
        group-2/
    payment-api-1/
      ...same structure...
  coordinator/
    merge-queue.json               # Ordered list of sessions ready to merge
    conflict-log.json              # History of detected conflicts + resolutions
```

### Session ID Generation

```python
def generate_session_id(spec_path: Path, sessions_dir: Path) -> str:
    """Generate human-readable session ID from spec filename + increment."""
    slug = spec_path.stem.lower().replace(" ", "-").replace("_", "-")
    # Strip non-alphanumeric except hyphens
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    slug = slug[:40]  # Cap length for branch name safety
    existing = [d.name for d in sessions_dir.iterdir() if d.is_dir()] if sessions_dir.exists() else []
    n = 1
    while f"{slug}-{n}" in existing:
        n += 1
    return f"{slug}-{n}"
```

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

**Status values:** `pending` -> `running` -> `awaiting_merge` -> `pr_open` -> `merged` -> `archived`
Additional terminal states: `failed`, `paused`, `conflict`

### Git Branch Namespacing

Current: `golem/<group>` (e.g. `golem/group-1`, `golem/group-1/integration`)
New: `golem/<session-id>/<group>` (e.g. `golem/auth-flow-1/group-1`, `golem/auth-flow-1/integration`)

This requires changes to:
- `worktree.create_worktree()` — accept a `branch_prefix` parameter
- `tech_lead.py` `_ensure_merged_to_main()` — filter by session-specific branch pattern
- `tech_lead.py` `_cleanup_golem_worktrees()` — scoped to session's worktree dir
- `cli.py` `clean` — iterate sessions or accept `--session` filter

### server.json Schema

```json
{
  "pid": 9876,
  "port": 9664,
  "host": "127.0.0.1",
  "started_at": "2026-03-27T13:55:00Z"
}
```

Written on server start, deleted on server stop. CLI reads this to discover the running server.

### Windows Compatibility

All new file I/O must use `encoding="utf-8"` on `read_text()`, `write_text()`, and `open()` calls — Windows defaults to cp1252. This applies to `session.json`, `server.json`, `merge-queue.json`, `conflict-log.json`, and all new code paths.

---

## Server Architecture

### Session Manager

Responsibilities:
- Create sessions (copy spec, generate ID, write session.json, spawn subprocess)
- Track subprocess lifecycle (monitor exit, update status)
- Provide per-session SSE event streams (tail each session's progress.log)
- Handle pause/resume/kill (signal management on subprocesses)

```python
@dataclass
class SessionState:
    id: str
    process: asyncio.subprocess.Process | None
    status: str
    config: GolemConfig
    event_queue: asyncio.Queue[str]
    log_buffer: collections.deque[dict[str, str | None]]
    background_tasks: list[asyncio.Task[None]]

class SessionManager:
    sessions: dict[str, SessionState]
    sessions_dir: Path

    async def create_session(self, spec_path: Path, project_root: Path) -> str: ...
    async def pause_session(self, session_id: str) -> None: ...
    async def resume_session(self, session_id: str) -> None: ...
    async def kill_session(self, session_id: str) -> None: ...
    async def archive_session(self, session_id: str) -> None: ...
    def get_session(self, session_id: str) -> SessionState: ...
    def list_sessions(self, status_filter: str | None = None) -> list[SessionState]: ...
```

### Subprocess Spawning

Each session runs as:
```
uv run golem run <spec> --session-id <id> --golem-dir .golem/sessions/<id> --force
```

The existing `cli.py run()` gains two new flags:
- `--session-id TEXT` — session identifier (used for branch prefix, logging)
- `--golem-dir PATH` — override default `.golem/` location

When `--golem-dir` is provided, `run()` uses that instead of `Path.cwd() / ".golem"`. When `--session-id` is provided, it's passed through to `run_planner()` and `run_tech_lead()` for branch namespacing.

Everything downstream (planner, tech_lead, writer) receives the namespaced `golem_dir` and works exactly as before — no awareness of multi-session.

### Session Completion Signaling

The server detects session completion through two mechanisms:
1. **Process exit** — `monitor_process()` awaits the subprocess and reads its exit code (0=success, non-zero=failure)
2. **Progress log** — the server tails `progress.log` for `TECH_LEAD_COMPLETE` or `RUN_COST` events as early indicators

On completion, the server updates `session.json` status to `awaiting_merge` (on success) or `failed` (on non-zero exit), then notifies the merge coordinator if applicable.

### Merge Coordinator

```python
class MergeCoordinator:
    queue: list[str]              # Ordered session IDs
    coordinator_dir: Path         # .golem/coordinator/
    session_manager: SessionManager

    async def enqueue(self, session_id: str) -> None: ...
    async def process_next(self) -> None: ...
    async def create_pr(self, session_id: str) -> str: ...
    async def merge_pr(self, session_id: str) -> None: ...
    async def rebase_queued(self, merged_session_id: str) -> None: ...
    async def detect_conflicts(self) -> list[ConflictInfo]: ...
```

**Merge flow:**
1. Session pipeline completes -> status `awaiting_merge` -> added to queue
2. Coordinator picks next in FIFO order
3. Create PR via `gh pr create` with session's integration branch
4. Status -> `pr_open`, `session.json` updated with `pr_number` and `pr_url`
5. User approves (CLI or UI) -> coordinator merges via `gh pr merge`
6. Rebase cascade: for each remaining queued session:
   - `git fetch origin main`
   - `git rebase origin/main` on session's integration branch
   - Re-run QA on rebased code
   - If rebase fails: status -> `conflict`, alert user
   - If QA fails: status -> `qa_failed`, alert user
7. Session -> `merged` -> auto-archive after configurable delay

**Conflict detection (proactive):**
- Periodically scan active session worktrees for overlapping modified files
- `git diff --name-only` per session's integration branch vs main
- Cross-reference file lists across sessions
- Surface warnings in UI and via `golem conflicts` CLI command

### API Layer

```
# Session management
POST   /api/sessions                Create session from spec
GET    /api/sessions                List all sessions + status
GET    /api/sessions/:id            Session detail (config, tickets, cost)
POST   /api/sessions/:id/pause      Pause running session
POST   /api/sessions/:id/resume     Resume paused session
POST   /api/sessions/:id/guidance   Inject operator guidance
DELETE /api/sessions/:id            Kill + archive session

# Per-session data
GET    /api/sessions/:id/tickets    List tickets for session
GET    /api/sessions/:id/events     SSE stream for this session
GET    /api/sessions/:id/diff       Git diff from session worktrees
GET    /api/sessions/:id/cost       Token usage + cost breakdown
GET    /api/sessions/:id/plan       Rendered plan overview

# Merge operations
GET    /api/merge-queue             Current queue state
POST   /api/merge-queue/:id         Add session to merge queue (or auto on completion)
POST   /api/merge-queue/:id/approve Approve + merge PR
DELETE /api/merge-queue/:id         Remove from queue

# Cross-session
GET    /api/conflicts               Files modified by multiple active sessions
GET    /api/stats                   Aggregate stats (sessions, costs, pass rates)
GET    /api/events                  Aggregate SSE stream (all sessions)

# Server
GET    /api/server/status           Server info, uptime, session counts
POST   /api/server/stop             Graceful shutdown

# Existing (preserved)
GET    /api/specs                   Find .md spec files in project
GET    /api/browse/file             Native file picker dialog
GET    /api/browse/folder           Native folder picker dialog
GET    /api/config                  Server-level config defaults
POST   /api/preflight               Pre-run tool resolution check
GET    /                            Serve dashboard HTML
```

---

## CLI-as-Client

### Server Discovery

```python
def find_server(project_root: Path) -> tuple[str, int] | None:
    """Read .golem/server.json to find running server."""
    server_json = project_root / ".golem" / "server.json"
    if not server_json.exists():
        return None
    data = json.loads(server_json.read_text(encoding="utf-8"))
    # Verify PID is still alive
    if not _pid_alive(data["pid"]):
        server_json.unlink()
        return None
    return (data["host"], data["port"])
```

### Command Routing

When `golem run spec.md` is invoked:
1. Check for running server via `find_server()`
2. If no server: auto-start server as background process, wait for `server.json`
3. POST `/api/sessions` with `{"spec_path": "spec.md", "project_root": "."}`
4. Stream SSE from `/api/sessions/<id>/events` to terminal
5. Print session ID on startup so user can reference it

### Full CLI Surface

```
# Session management (routes through server)
golem run spec.md                        # Create session, stream logs
golem run spec1.md spec2.md              # Create multiple sessions
golem status                             # All sessions overview table
golem status <session-id>                # Single session detail
golem logs <session-id>                  # Tail session progress.log
golem logs -f <session-id>               # Follow mode
golem pause <session-id>                 # Pause running session
golem resume <session-id>                # Resume paused session
golem guidance <session-id> "message"    # Inject operator guidance
golem kill <session-id>                  # Force-stop session

# Diagnostics
golem tickets <session-id>               # List tickets for session
golem inspect <ticket-id> --session <id> # Full ticket detail
golem diff <session-id>                  # Git diff from session worktrees
golem cost <session-id>                  # Token usage + cost breakdown
golem cost                               # Aggregate costs all sessions
golem conflicts                          # Files modified by multiple sessions
golem history                            # Unified timeline all sessions
golem history <session-id>               # Timeline for one session

# Merge operations
golem merge <session-id>                 # Queue session for merge
golem merge-queue                        # Show current merge queue
golem approve <session-id>               # Approve PR + merge

# Server lifecycle
golem server start                       # Explicit start
golem server stop                        # Shutdown server + all sessions
golem server status                      # Server info, active sessions

# Housekeeping
golem clean                              # Wipe all sessions + server state
golem clean <session-id>                 # Wipe single session
golem export <session-id>                # Zip session artifacts
golem export                             # Zip all sessions

# Unchanged
golem doctor                             # Environment check
golem version                            # Version info
golem ui                                 # Open browser to dashboard
golem list-specs                         # Find .md files in project
golem config show/set/reset              # Config management
golem preflight <spec>                   # Pre-run tool check

# Fallback
golem run spec.md --no-server            # Direct execution (no server, old behavior)
```

### --no-server Fallback

When `--no-server` is passed, `golem run` executes the pipeline directly in-process (current behavior). This is useful for:
- CI environments where a persistent server doesn't make sense
- Debugging a specific pipeline issue
- Environments where background processes are restricted

In `--no-server` mode, the state goes to `.golem/` flat (not `.golem/sessions/`) to maintain backward compatibility.

---

## Dashboard UI

### Layout

```
+------------------------------------------------------------------+
| [Golem]  3 running | 1 queued | 2 done    $4.32 total           |
+------------------------------------------------------------------+
| SIDEBAR (260px)      | MAIN CONTENT                              |
|                      |                                            |
| [+ New Session]      | auth-flow-1          [RUNNING] [STANDARD]  |
|                      |           [Guidance] [Pause] [Kill]        |
| -- Running --        +--------------------------------------------+
| > auth-flow-1        | [Tickets] [Logs] [Plan] [Diff] [Cost]     |
|   Tech Lead dispatch +--------------------------------------------+
|   3/5 | $1.24 | 12m |                                            |
|                      | TICKET-001  JWT middleware     done   $0.32|
|   payment-api-1      | TICKET-002  OAuth provider    done   $0.28|
|   Planner research   | TICKET-003  Session store     wip    $0.18|
|   0/0 | $0.41 | 3m  | TICKET-004  RBAC              wip    $0.11|
|                      | TICKET-005  Auth error page   pending  --  |
|   user-settings-1    |                                            |
|   Writers in prog    | [!] Overlap: TICKET-003 touches            |
|   2/4 | $0.89 | 8m  |     src/auth/session.py also modified by    |
|                      |     user-settings-1 TICKET-002             |
| -- Merge Queue --    |                                            |
|   search-refactor-1  |                                            |
|   PR #47 - awaiting  |                                            |
|   5/5 | $2.10 | done |                                            |
|                      |                                            |
| -- Completed --      |                                            |
|   nav-redesign-1     |                                            |
|   Merged - PR #45    |                                            |
+----------------------+--------------------------------------------+
| [STATUS BAR]  Server running | 4 Claude sessions active          |
+------------------------------------------------------------------+
```

### Session Sidebar

- Grouped by lifecycle state: Running, Merge Queue, Completed
- Each entry shows: session name, current phase, ticket progress (done/total), cost, duration
- Click to select session and show detail in main area
- Color-coded status indicators (green=running, yellow=queued, gray=done, red=failed)
- `+ New Session` button opens spec file picker

### Session Detail View

**Header:** Session name, status badge, complexity badge, action buttons (Guidance, Pause, Kill)

**Sub-tabs:**
- **Tickets** — Table with ticket ID, task title, status, worktree, cost. Inline conflict alerts.
- **Logs** — Streaming log view (SSE), same format as current console panel. Filter by verb.
- **Plan** — Rendered overview.md from session's plans directory.
- **Diff** — Git diff output from session's worktrees vs main.
- **Cost** — Token usage breakdown by agent role (planner, tech lead, writers). Per-ticket costs.

### Aggregate Stats Bar

Top bar shows:
- Session counts by state (N running, N queued, N done)
- Total cost across all sessions
- Active Claude SDK session count

### Conflict Alerts

- Inline warning in ticket table when a ticket's modified files overlap with another active session
- Computed by cross-referencing `git diff --name-only` across session integration branches
- Shows which session and ticket has the overlap
- Links to the conflicting session for easy navigation

### New Session Dialog

- Triggered by `+ New Session` button or keyboard shortcut
- File picker for spec file (reuses existing native dialog on Windows)
- Project root auto-fills from spec parent directory
- Optional: complexity classification preview before launching

### SSE Event Streams

- `/api/sessions/:id/events` — per-session stream (same event types as current: `status`, `log`, `tasks`)
- `/api/events` — aggregate stream with session ID prefixed on each event
- Reconnection: log_buffer per session replays on reconnect (same as current 200-event deque)

### Technology

- Self-contained HTML file (same approach as current `ui_template.html`)
- No CDN dependencies, no build step
- Vanilla JS with SSE client
- CSS custom properties for theming (extend current dark theme)

---

## Changes to Existing Code

### `cli.py` (1,154 lines)

**New flags on `run()`:**
- `--session-id TEXT` — passed through to planner/tech_lead for branch namespacing
- `--golem-dir PATH` — override `.golem/` location
- `--no-server` — bypass server, run directly (backward compat)

**New routing in `run()`:**
- If `--no-server` not set: discover server, auto-start if needed, POST to `/api/sessions`, stream SSE
- If `--no-server` set: current direct execution behavior

**New commands:**
- `server` sub-typer with `start`, `stop`, `status`
- `merge`, `approve`, `merge-queue`
- `conflicts`
- `kill`

**Modified commands:**
- `status` — show all sessions if server running, single-session if `--session` provided
- `logs` — require session ID when server running
- `clean` — accept optional session ID, clean single or all
- `export` — accept optional session ID
- `history` — accept optional session ID, aggregate by default
- `resume` — accept session ID, route through server
- `guidance` — accept session ID as first arg

**Backward compatibility:** All commands that currently work without a server continue to work via `--no-server` or when no `server.json` exists. The CLI detects server availability and routes accordingly.

### `config.py` (318 lines)

**New fields on `GolemConfig`:**
- `session_id: str = ""` — set when running as a session
- `branch_prefix: str = "golem"` — overridden to `golem/<session-id>` when in a session
- `merge_auto_rebase: bool = True` — whether coordinator auto-rebases after merges
- `archive_delay_minutes: int = 30` — delay before auto-archiving merged sessions

### `worktree.py`

**`create_worktree()`:**
- Accept `branch_prefix: str = "golem"` parameter
- Branch names change from `golem/<group>` to `{branch_prefix}/<group>`
- When `session_id` is set, `branch_prefix = f"golem/{session_id}"`

### `tech_lead.py`

**`_ensure_merged_to_main()`:**
- Accept `branch_prefix` parameter
- Filter `git branch --list {branch_prefix}/*/integration` instead of `golem/*/integration`

**`_cleanup_golem_worktrees()`:**
- Already scoped to `golem_dir / "worktrees"` — works as-is with namespaced golem_dir

### `tickets.py` (162 lines)

**New field on `Ticket`:**
- `session_id: str = ""` — which session this ticket belongs to (for cross-session queries)

### `progress.py` (109 lines)

**New events:**
- `SESSION_START session_id=<id> spec=<path>`
- `SESSION_COMPLETE session_id=<id> status=<status>`
- `MERGE_QUEUED session_id=<id>`
- `PR_CREATED session_id=<id> pr=<number>`
- `PR_MERGED session_id=<id> pr=<number>`
- `REBASE_START session_id=<id> onto=<branch>`
- `REBASE_COMPLETE session_id=<id>` / `REBASE_FAILED session_id=<id> error=<msg>`

### `ui.py` (615 lines)

**Major rewrite.** Module-level singletons (`current_process`, `current_cwd`, `event_queue`, `log_buffer`) replaced by `SessionManager` instance. All endpoints updated to session-aware versions. New coordinator endpoints added.

### `ui_template.html` (1,486 lines)

**Full rewrite.** New layout with session sidebar, detail view, aggregate stats. Reuse CSS custom properties and dark theme from current template. New JS for multi-session SSE management.

---

## ZeroShot Spec Decomposition

Five specs, executed in dependency order. Specs 2, 3, 4 can run in parallel after Spec 1 is merged.

```
Spec 1: Foundation + Server Core          → FIRST (the foundation)
Spec 2: CLI-as-Client                     → after Spec 1 merged
Spec 3: Merge Coordinator                 → after Spec 1 merged (parallel with 2, 4)
Spec 4: Dashboard UI                      → after Spec 1 merged (parallel with 2, 3)
Spec 5: Cross-Session Intelligence        → after Specs 1-4 merged
```

---

## Phase 1: Foundation + Server Core

**Scope:** Session state namespacing, server process, session manager, subprocess spawning, core REST API, SSE per session, auto-start logic.
**Depends on:** Nothing — this is the foundation.

### Task 1: Session Module

**Files:**
- Create: `src/golem/session.py`

- [ ] **Step 1: Create session.py with core types**
  Create `src/golem/session.py` with:
  - `SessionMetadata` dataclass: `id`, `spec_path`, `status`, `complexity`, `created_at`, `updated_at`, `pid`, `pr_number`, `pr_url`, `merged_at`, `archived_at`, `cost_usd`, `error`
  - Status constants: `PENDING`, `RUNNING`, `AWAITING_MERGE`, `PR_OPEN`, `MERGED`, `ARCHIVED`, `FAILED`, `PAUSED`, `CONFLICT`
  - `generate_session_id(spec_path: Path, sessions_dir: Path) -> str` — slugify spec stem + incrementing suffix
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

### Task 2: Config & Ticket Extensions

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

### Task 3: Worktree Branch Namespacing

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

### Task 4: Progress Events

**Files:**
- Modify: `src/golem/progress.py`

- [ ] **Step 1: Add session lifecycle event methods**
  Add to `ProgressLogger`:
  - `log_session_start(session_id: str, spec_path: str)` → `SESSION_START session_id=<id> spec=<path>`
  - `log_session_complete(session_id: str, status: str)` → `SESSION_COMPLETE session_id=<id> status=<status>`
  - `log_merge_queued(session_id: str)` → `MERGE_QUEUED session_id=<id>`
  - `log_pr_created(session_id: str, pr_number: int)` → `PR_CREATED session_id=<id> pr=<number>`
  - `log_pr_merged(session_id: str, pr_number: int)` → `PR_MERGED session_id=<id> pr=<number>`
  - `log_rebase_start(session_id: str, onto: str)` → `REBASE_START session_id=<id> onto=<branch>`
  - `log_rebase_complete(session_id: str)` → `REBASE_COMPLETE session_id=<id>`
  - `log_rebase_failed(session_id: str, error: str)` → `REBASE_FAILED session_id=<id> error=<msg>`

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

### Task 5: CLI Flags + Server Sub-Typer

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
  - Call `find_server()` (from new `client.py` — stub for now, just reads `server.json`)
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

### Task 6: Server Core

**Files:**
- Create: `src/golem/server.py`

- [ ] **Step 1: Create server.py with app factory**
  Create `src/golem/server.py` with `create_app() -> FastAPI`. Include:
  - `SessionState` dataclass: `id`, `process`, `status`, `config`, `event_queue`, `log_buffer`, `background_tasks`
  - `SessionManager` class with methods: `create_session()`, `pause_session()`, `resume_session()`, `kill_session()`, `archive_session()`, `get_session()`, `list_sessions()`
  - Stub `MergeCoordinator` class (empty methods, implemented in Phase 3)
  - Server lifecycle: `write_server_json()`, `remove_server_json()`, lifespan handler

- [ ] **Step 2: Implement session CRUD endpoints**
  - `POST /api/sessions` — accepts `{"spec_path": str, "project_root": str}`, creates session dir, spawns subprocess, returns session ID
  - `GET /api/sessions` — list all sessions with status
  - `GET /api/sessions/{id}` — session detail (config, tickets, cost from progress.log)
  - `DELETE /api/sessions/{id}` — kill + archive

- [ ] **Step 3: Implement session lifecycle endpoints**
  - `POST /api/sessions/{id}/pause` — send SIGSTOP/suspend to subprocess
  - `POST /api/sessions/{id}/resume` — send SIGCONT/resume
  - `POST /api/sessions/{id}/guidance` — write guidance ticket to session's ticket dir

- [ ] **Step 4: Implement per-session SSE**
  - `GET /api/sessions/{id}/events` — SSE stream tailing session's `progress.log`
  - `GET /api/events` — aggregate SSE stream prefixing each event with session ID
  - Each session gets its own `event_queue` and `log_buffer` (deque, maxlen=200)
  - Background tasks: `tail_progress_log()`, `monitor_process()`, `stream_subprocess_output()`

- [ ] **Step 5: Implement per-session data endpoints**
  - `GET /api/sessions/{id}/tickets` — list tickets from session's ticket dir
  - `GET /api/sessions/{id}/diff` — `git diff` from session's worktree branches
  - `GET /api/sessions/{id}/cost` — parse `AGENT_COST` from session's progress.log
  - `GET /api/sessions/{id}/plan` — read session's `plans/overview.md`

- [ ] **Step 6: Preserve existing endpoints**
  - `GET /api/specs` — find .md files (same as current ui.py)
  - `GET /api/browse/file` — native file picker (same)
  - `GET /api/browse/folder` — native folder picker (same)
  - `GET /api/config` — server-level config defaults
  - `POST /api/preflight` — pre-run tool check
  - `GET /api/server/status` — server info, uptime, session counts
  - `POST /api/server/stop` — graceful shutdown

- [ ] **Step 7: Create test_server.py**
  Create `tests/test_server.py` with tests:
  - Session CRUD: create, list, get, delete
  - Session status transitions
  - SSE event stream (use `async for` with early break, same pattern as test_ui.py)
  - Subprocess spawning (mock `asyncio.create_subprocess_exec`)
  - Server.json lifecycle (write on start, remove on stop)
  - Guidance injection
  - Endpoint response shapes for tickets, diff, cost, plan
  Use `httpx.AsyncClient` with `ASGITransport` (no running server needed).

- [ ] **Step 8: Commit**
  ```bash
  git add src/golem/server.py tests/test_server.py
  git commit -m "feat: add multi-session server with session manager, SSE, and REST API"
  ```

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. Server module imports
uv run python -c "
from golem.server import create_app, SessionManager, SessionState
app = create_app()
print('SERVER_IMPORT: PASS')
"

# 2. Key endpoints registered
uv run python -c "
from golem.server import create_app
app = create_app()
routes = [r.path for r in app.routes if hasattr(r, 'path')]
required = ['/api/sessions', '/api/events', '/api/server/status']
missing = [r for r in required if r not in routes]
assert not missing, f'FAIL: missing routes {missing}'
print(f'ROUTES: PASS ({len(routes)} routes registered)')
"

# 3. Server tests pass
uv run pytest tests/test_server.py -v --tb=short 2>&1 | tail -1

# 4. Full test suite still passes
uv run pytest --tb=short -q 2>&1 | tail -1
```

Expected:
```
SERVER_IMPORT: PASS
ROUTES: PASS ([N] routes registered)
[N] passed
[N] passed
```

---

### Phase 1 Completion Gate

**Phase 1 is NOT complete until every check below passes.** If any check fails, return to the responsible task, fix the issue, and re-run this entire gate.

#### Gate 1: All New Files Exist

```bash
cd F:/Tools/Projects/golem-cli
for f in src/golem/session.py src/golem/server.py tests/test_session.py tests/test_server.py; do
  test -s "$f" && echo "$f: PASS" || echo "$f: FAIL"
done
```

Expected: all PASS

#### Gate 2: Core Imports

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
assert hasattr(c, 'session_id')
assert hasattr(c, 'branch_prefix')

t = Ticket.__dataclass_fields__
assert 'session_id' in t

import inspect
sig = inspect.signature(create_worktree)
assert 'branch_prefix' in sig.parameters

for m in ['log_session_start', 'log_session_complete', 'log_merge_queued']:
    assert hasattr(ProgressLogger, m)

print('IMPORTS: PASS')
"
```

Expected: `IMPORTS: PASS`

#### Gate 3: CLI Flags

```bash
cd F:/Tools/Projects/golem-cli
uv run golem run --help 2>&1 | grep -c "session-id\|golem-dir\|no-server" | xargs -I{} test {} -eq 3 && echo "CLI_FLAGS: PASS" || echo "CLI_FLAGS: FAIL"
uv run golem server --help 2>&1 | grep -c "start\|stop\|status" | xargs -I{} test {} -ge 3 && echo "SERVER_CMDS: PASS" || echo "SERVER_CMDS: FAIL"
```

Expected:
```
CLI_FLAGS: PASS
SERVER_CMDS: PASS
```

#### Gate 4: Full Test Suite

```bash
cd F:/Tools/Projects/golem-cli
uv run pytest -v --tb=short 2>&1 | tail -5
```

Expected: `[N] passed` (must be >= 314 existing + new tests, 0 failed)

#### Gate 5: Backward Compatibility

```bash
cd F:/Tools/Projects/golem-cli

# --no-server flag runs old behavior (will fail on missing spec, but should NOT try to contact server)
uv run golem run nonexistent.md --no-server 2>&1 | grep -q "not found\|does not exist\|No such file" && echo "NOSERVER_COMPAT: PASS" || echo "NOSERVER_COMPAT: FAIL"

# Existing commands still work without server
uv run golem version 2>&1 | grep -q "golem" && echo "VERSION_CMD: PASS" || echo "VERSION_CMD: FAIL"
uv run golem doctor 2>&1 | grep -q "git\|uv\|claude" && echo "DOCTOR_CMD: PASS" || echo "DOCTOR_CMD: FAIL"
```

Expected:
```
NOSERVER_COMPAT: PASS
VERSION_CMD: PASS
DOCTOR_CMD: PASS
```

#### Phase 1 Verdict

Run all 5 gates. If **all gates pass**, Phase 1 is complete.
If **any gate fails**, identify the responsible task from the table below, fix it, and re-run the full gate sequence.

| Gate | Validates Tasks |
|------|----------------|
| Gate 1 | Task 1 (session.py), Task 6 (server.py) |
| Gate 2 | Task 1, 2, 3, 4 (all module changes) |
| Gate 3 | Task 5 (CLI flags + server sub-typer) |
| Gate 4 | All tasks (regression) |
| Gate 5 | Task 5 (backward compat) |

---

## Phase 2: CLI-as-Client

**Scope:** Server discovery, CLI command routing through server, all new CLI commands, SSE streaming to terminal.
**Depends on:** Phase 1 merged to main.

### Task 1: Client Module

**Files:**
- Create: `src/golem/client.py`

- [ ] **Step 1: Create client.py with server discovery and HTTP client**
  Create `src/golem/client.py` with:
  - `find_server(project_root: Path) -> tuple[str, int] | None` — reads `.golem/server.json`, verifies PID alive, returns (host, port) or None
  - `_pid_alive(pid: int) -> bool` — cross-platform PID check (Windows: `ctypes.windll.kernel32.OpenProcess`, Unix: `os.kill(pid, 0)`)
  - `GolemClient` class wrapping `httpx.AsyncClient`:
    - `__init__(host: str, port: int)`
    - `create_session(spec_path: str, project_root: str) -> dict`
    - `list_sessions() -> list[dict]`
    - `get_session(session_id: str) -> dict`
    - `pause_session(session_id: str) -> None`
    - `resume_session(session_id: str) -> None`
    - `kill_session(session_id: str) -> None`
    - `send_guidance(session_id: str, text: str) -> None`
    - `stream_events(session_id: str) -> AsyncIterator[dict]` — SSE consumer
    - `get_merge_queue() -> list[dict]`
    - `approve_merge(session_id: str) -> None`
    - `get_conflicts() -> list[dict]`
    - `get_stats() -> dict`
    - `stop_server() -> None`
  All methods use `encoding="utf-8"` for file I/O.

- [ ] **Step 2: Create test_client.py**
  Create `tests/test_client.py` with tests:
  - `test_find_server_no_file` — returns None
  - `test_find_server_stale_pid` — removes stale server.json, returns None
  - `test_find_server_valid` — returns (host, port)
  - `test_client_create_session` — mock httpx, verify POST
  - `test_client_list_sessions` — mock httpx, verify GET
  - `test_client_stream_events` — mock SSE stream

- [ ] **Step 3: Commit**
  ```bash
  git add src/golem/client.py tests/test_client.py
  git commit -m "feat: add GolemClient for CLI-to-server communication"
  ```

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. Client imports
uv run python -c "from golem.client import find_server, GolemClient; print('CLIENT_IMPORT: PASS')"

# 2. Tests pass
uv run pytest tests/test_client.py -v --tb=short 2>&1 | tail -1
```

Expected:
```
CLIENT_IMPORT: PASS
[N] passed
```

---

### Task 2: CLI Command Routing

**Files:**
- Modify: `src/golem/cli.py`

- [ ] **Step 1: Implement run() server routing**
  Modify `run()`:
  - When `--no-server` is NOT set: call `find_server()`, auto-start if needed, call `client.create_session()`, stream SSE to console via `client.stream_events()`
  - Print session ID at start: `"Session: auth-flow-1 (streaming logs...)"`
  - When `--no-server` IS set: unchanged behavior

- [ ] **Step 2: Add session-aware commands**
  Add/modify commands to route through server when available:
  - `status [session_id]` — all sessions table or single session detail
  - `logs <session_id> [-f]` — stream from server SSE
  - `pause <session_id>` — POST to server
  - `resume <session_id>` — POST to server
  - `kill <session_id>` — DELETE to server
  - `guidance <session_id> <text>` — POST to server
  - `tickets <session_id>` — GET from server
  - `cost [session_id]` — aggregate or per-session
  - `diff <session_id>` — GET from server
  - `history [session_id]` — aggregate or per-session

- [ ] **Step 3: Modify existing commands for session awareness**
  - `clean [session_id]` — if session_id given, clean single session via server; else clean all
  - `export [session_id]` — if session_id given, export single session; else export all
  - `inspect <ticket_id> --session <id>` — route through server to correct session's ticket store

- [ ] **Step 4: Handle "server not running" gracefully**
  All server-dependent commands must check `find_server()` first. If None, print:
  `"Server not running. Start with 'golem server start' or use 'golem run --no-server' for direct execution."`

- [ ] **Step 5: Support multi-spec run**
  Modify `run()` to accept variadic spec paths: `golem run spec1.md spec2.md`. For each spec, create a session. Stream events from the first session by default, print IDs for all.

- [ ] **Step 6: Update CLI tests**
  In `tests/test_cli.py`: add tests for server routing (mock `find_server` and `GolemClient`), multi-spec run, session-aware commands, "server not running" error messages.

- [ ] **Step 7: Commit**
  ```bash
  git add src/golem/cli.py tests/test_cli.py
  git commit -m "feat: route CLI commands through server, add session-aware commands"
  ```

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. New commands appear in help
for cmd in pause resume kill tickets cost conflicts; do
  uv run golem $cmd --help 2>&1 | head -1 | grep -qi "usage\|error\|missing" && echo "$cmd: PASS" || echo "$cmd: FAIL"
done

# 2. Multi-spec argument accepted
uv run golem run --help 2>&1 | grep -qi "spec" && echo "MULTI_SPEC: PASS" || echo "MULTI_SPEC: FAIL"

# 3. CLI tests pass
uv run pytest tests/test_cli.py -v --tb=short 2>&1 | tail -1

# 4. Full suite passes
uv run pytest --tb=short -q 2>&1 | tail -1
```

Expected:
```
pause: PASS
resume: PASS
kill: PASS
tickets: PASS
cost: PASS
conflicts: PASS
MULTI_SPEC: PASS
[N] passed
[N] passed
```

---

### Phase 2 Completion Gate

**Phase 2 is NOT complete until every check below passes.**

#### Gate 1: Client Module

```bash
cd F:/Tools/Projects/golem-cli
uv run python -c "from golem.client import find_server, GolemClient; print('CLIENT: PASS')"
uv run pytest tests/test_client.py -v --tb=short 2>&1 | tail -1
```

Expected: `CLIENT: PASS` + `[N] passed`

#### Gate 2: CLI Commands

```bash
cd F:/Tools/Projects/golem-cli
for cmd in "server start" "server stop" "server status" pause resume kill tickets cost conflicts; do
  uv run golem $cmd --help >/dev/null 2>&1 && echo "$cmd: PASS" || echo "$cmd: FAIL"
done
```

Expected: all PASS

#### Gate 3: Server Not Running Handling

```bash
cd F:/Tools/Projects/golem-cli
# With no server.json, commands should fail gracefully
rm -f .golem/server.json 2>/dev/null
uv run golem status 2>&1 | grep -qi "not running\|no server\|start" && echo "GRACEFUL: PASS" || echo "GRACEFUL: FAIL"
```

Expected: `GRACEFUL: PASS`

#### Gate 4: Full Test Suite

```bash
cd F:/Tools/Projects/golem-cli
uv run pytest -v --tb=short 2>&1 | tail -5
```

Expected: `[N] passed, 0 failed`

#### Phase 2 Verdict

| Gate | Validates Tasks |
|------|----------------|
| Gate 1 | Task 1 (client module) |
| Gate 2 | Task 2 steps 2-3 (new commands) |
| Gate 3 | Task 2 step 4 (graceful handling) |
| Gate 4 | All tasks (regression) |

---

## Phase 3: Merge Coordinator

**Scope:** Merge queue state management, PR creation per session, post-merge rebase cascade, conflict detection and escalation, merge-related CLI commands.
**Depends on:** Phase 1 merged to main. Can run in parallel with Phases 2 and 4.

### Task 1: Merge Module

**Files:**
- Create: `src/golem/merge.py`

- [ ] **Step 1: Create merge.py with MergeCoordinator**
  Create `src/golem/merge.py` with:
  - `ConflictInfo` dataclass: `file_path`, `session_a`, `session_b`, `ticket_a`, `ticket_b`
  - `MergeQueueEntry` dataclass: `session_id`, `enqueued_at`, `pr_number`, `status`
  - `MergeCoordinator` class:
    - `__init__(coordinator_dir: Path, session_manager: SessionManager)`
    - `async enqueue(session_id: str) -> None` — add to queue, persist to `merge-queue.json`
    - `async dequeue(session_id: str) -> None` — remove from queue
    - `async process_next() -> None` — pick next FIFO entry, create PR
    - `async create_pr(session_id: str) -> str` — `gh pr create` with session's integration branch, return PR URL
    - `async merge_pr(session_id: str) -> None` — `gh pr merge`, update session status
    - `async rebase_queued(merged_session_id: str) -> None` — for each remaining queued session: fetch, rebase, re-run QA
    - `async detect_conflicts() -> list[ConflictInfo]` — `git diff --name-only` per session vs main, cross-reference
    - `_read_queue() -> list[MergeQueueEntry]` — from `merge-queue.json` with `encoding="utf-8"`
    - `_write_queue(entries: list[MergeQueueEntry]) -> None` — to `merge-queue.json` with `encoding="utf-8"`

- [ ] **Step 2: Create test_merge.py**
  Create `tests/test_merge.py` with tests:
  - `test_enqueue_dequeue` — FIFO ordering
  - `test_queue_persistence` — write/read roundtrip via JSON
  - `test_create_pr_calls_gh` — mock subprocess, verify `gh pr create` args
  - `test_merge_pr_calls_gh` — mock subprocess, verify `gh pr merge` args
  - `test_rebase_cascade_success` — mock git commands, verify rebase on remaining sessions
  - `test_rebase_cascade_conflict` — mock git rebase failure, verify status set to `conflict`
  - `test_detect_conflicts_overlap` — two sessions modifying same file
  - `test_detect_conflicts_no_overlap` — two sessions modifying different files
  - `test_detect_conflicts_empty` — no active sessions

- [ ] **Step 3: Commit**
  ```bash
  git add src/golem/merge.py tests/test_merge.py
  git commit -m "feat: add MergeCoordinator with queue, PR management, rebase cascade, conflict detection"
  ```

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. Module imports
uv run python -c "from golem.merge import MergeCoordinator, ConflictInfo, MergeQueueEntry; print('MERGE_IMPORT: PASS')"

# 2. Tests pass
uv run pytest tests/test_merge.py -v --tb=short 2>&1 | tail -1
```

Expected:
```
MERGE_IMPORT: PASS
[N] passed
```

---

### Task 2: Server Integration + Merge Endpoints

**Files:**
- Modify: `src/golem/server.py`

- [ ] **Step 1: Wire MergeCoordinator into server**
  In `create_app()`:
  - Instantiate `MergeCoordinator` alongside `SessionManager`
  - On session completion (in `monitor_process`): auto-call `coordinator.enqueue(session_id)` if exit code 0

- [ ] **Step 2: Add merge endpoints**
  - `GET /api/merge-queue` — return current queue
  - `POST /api/merge-queue/{id}` — manually enqueue session
  - `POST /api/merge-queue/{id}/approve` — merge PR + trigger rebase cascade
  - `DELETE /api/merge-queue/{id}` — remove from queue
  - `GET /api/conflicts` — return overlap info

- [ ] **Step 3: Update test_server.py**
  Add tests for merge endpoints (mock MergeCoordinator methods).

- [ ] **Step 4: Commit**
  ```bash
  git add src/golem/server.py tests/test_server.py
  git commit -m "feat: wire MergeCoordinator into server, add merge-queue endpoints"
  ```

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. Merge endpoints registered
uv run python -c "
from golem.server import create_app
app = create_app()
routes = [r.path for r in app.routes if hasattr(r, 'path')]
required = ['/api/merge-queue', '/api/conflicts']
missing = [r for r in required if r not in routes]
assert not missing, f'FAIL: missing {missing}'
print('MERGE_ROUTES: PASS')
"

# 2. Server tests pass
uv run pytest tests/test_server.py tests/test_merge.py -v --tb=short 2>&1 | tail -1
```

Expected:
```
MERGE_ROUTES: PASS
[N] passed
```

---

### Task 3: Merge CLI Commands

**Files:**
- Modify: `src/golem/cli.py`

- [ ] **Step 1: Add merge commands**
  - `golem merge <session_id>` — POST `/api/merge-queue/{id}`
  - `golem approve <session_id>` — POST `/api/merge-queue/{id}/approve`
  - `golem merge-queue` — GET `/api/merge-queue`, display as rich table
  - `golem conflicts` — GET `/api/conflicts`, display overlapping files

- [ ] **Step 2: Update CLI tests**
  In `tests/test_cli.py`: add tests for merge/approve/merge-queue/conflicts commands (mock client).

- [ ] **Step 3: Commit**
  ```bash
  git add src/golem/cli.py tests/test_cli.py
  git commit -m "feat: add merge, approve, merge-queue, conflicts CLI commands"
  ```

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. Commands registered
for cmd in merge approve merge-queue conflicts; do
  uv run golem $cmd --help >/dev/null 2>&1 && echo "$cmd: PASS" || echo "$cmd: FAIL"
done

# 2. Tests pass
uv run pytest tests/test_cli.py -v --tb=short 2>&1 | tail -1
```

Expected: all PASS + `[N] passed`

---

### Phase 3 Completion Gate

**Phase 3 is NOT complete until every check below passes.**

#### Gate 1: Merge Module

```bash
cd F:/Tools/Projects/golem-cli
uv run python -c "from golem.merge import MergeCoordinator, ConflictInfo, MergeQueueEntry; print('MERGE: PASS')"
uv run pytest tests/test_merge.py -v --tb=short 2>&1 | tail -1
```

#### Gate 2: Server Endpoints

```bash
cd F:/Tools/Projects/golem-cli
uv run python -c "
from golem.server import create_app
app = create_app()
routes = [r.path for r in app.routes if hasattr(r, 'path')]
merge_routes = [r for r in routes if 'merge' in r or 'conflict' in r]
assert len(merge_routes) >= 3, f'FAIL: only {len(merge_routes)} merge routes'
print(f'MERGE_ENDPOINTS: PASS ({len(merge_routes)} routes)')
"
```

#### Gate 3: CLI Commands

```bash
cd F:/Tools/Projects/golem-cli
for cmd in merge approve merge-queue conflicts; do
  uv run golem $cmd --help >/dev/null 2>&1 && echo "$cmd: PASS" || echo "$cmd: FAIL"
done
```

#### Gate 4: Full Test Suite

```bash
cd F:/Tools/Projects/golem-cli
uv run pytest -v --tb=short 2>&1 | tail -5
```

Expected: `[N] passed, 0 failed`

#### Phase 3 Verdict

| Gate | Validates Tasks |
|------|----------------|
| Gate 1 | Task 1 (merge module) |
| Gate 2 | Task 2 (server integration) |
| Gate 3 | Task 3 (CLI commands) |
| Gate 4 | All tasks (regression) |

---

## Phase 4: Dashboard UI

**Scope:** Full UI rewrite — session sidebar, detail view with sub-tabs, aggregate stats, conflict alerts, new session dialog, multi-session SSE.
**Depends on:** Phase 1 merged to main. Can run in parallel with Phases 2 and 3.

### Task 1: Dashboard HTML Rewrite

**Files:**
- Modify: `src/golem/ui_template.html`

- [ ] **Step 1: Session sidebar**
  Replace the current single-run control bar with a sidebar layout:
  - 260px fixed sidebar with session list grouped by state (Running, Merge Queue, Completed)
  - Each session entry: name, phase description, ticket progress, cost, duration
  - Color-coded status indicators (green=running, yellow=queued, gray=done, red=failed)
  - `+ New Session` button at top

- [ ] **Step 2: Aggregate stats bar**
  Top bar showing:
  - Session counts by state (N running, N queued, N done)
  - Total cost across all sessions
  - Connection indicator (same as current)

- [ ] **Step 3: Session detail view**
  Main content area showing selected session:
  - Header: session name, status badge, complexity badge, action buttons (Guidance, Pause, Kill)
  - Tab bar: Tickets, Logs, Plan, Diff, Cost
  - Tickets tab: table with ID, title, status, worktree, cost. Inline conflict alerts.
  - Logs tab: streaming log console (same SSE approach as current)
  - Plan tab: rendered overview.md content
  - Diff tab: git diff output
  - Cost tab: token breakdown by agent role

- [ ] **Step 4: New Session dialog**
  - Triggered by `+ New Session` button
  - Spec file input with BROWSE button (reuses `/api/browse/file`)
  - Project root auto-fills from spec parent
  - LAUNCH button calls `POST /api/sessions`

- [ ] **Step 5: Multi-session SSE management**
  JS client changes:
  - Connect to `/api/sessions/{id}/events` when session is selected
  - Disconnect previous session's SSE when switching tabs
  - Poll `/api/sessions` periodically (every 3s) to update sidebar state
  - Reconnection: replay `log_buffer` on reconnect

- [ ] **Step 6: Status bar**
  Bottom bar: server running indicator, active Claude session count

- [ ] **Step 7: Preserve self-contained nature**
  - No CDN dependencies
  - All CSS inline (extend current dark theme with sidebar/tab styles)
  - All JS inline
  - No build step

- [ ] **Step 8: Commit**
  ```bash
  git add src/golem/ui_template.html
  git commit -m "feat: full dashboard rewrite with session sidebar, detail view, and multi-session SSE"
  ```

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. Template file exists and is substantial
test -s src/golem/ui_template.html && echo "FILE: PASS" || echo "FILE: FAIL"
wc -l < src/golem/ui_template.html | xargs -I{} test {} -gt 500 && echo "SIZE: PASS" || echo "SIZE: FAIL"

# 2. No CDN references
grep -c "cdn\.\|unpkg\.\|jsdelivr\.\|cloudflare\." src/golem/ui_template.html | xargs -I{} test {} -eq 0 && echo "NO_CDN: PASS" || echo "NO_CDN: FAIL"

# 3. Key UI elements present
grep -q "sidebar\|session-list" src/golem/ui_template.html && echo "SIDEBAR: PASS" || echo "SIDEBAR: FAIL"
grep -q "api/sessions" src/golem/ui_template.html && echo "API_CALLS: PASS" || echo "API_CALLS: FAIL"
grep -q "EventSource\|text/event-stream" src/golem/ui_template.html && echo "SSE: PASS" || echo "SSE: FAIL"
grep -q "New Session\|new-session\|newSession" src/golem/ui_template.html && echo "NEW_SESSION: PASS" || echo "NEW_SESSION: FAIL"
```

Expected: all PASS

---

### Task 2: Server-Side UI Support

**Files:**
- Modify: `src/golem/server.py`

- [ ] **Step 1: Update template serving**
  Update `GET /` to serve the new `ui_template.html`. Ensure the server loads the template at startup (same pattern as current `ui.py`).

- [ ] **Step 2: Verify all UI-needed endpoints return correct shapes**
  Ensure these endpoints return data the UI JS expects:
  - `GET /api/sessions` — list with `{id, status, complexity, cost_usd, ...}`
  - `GET /api/sessions/{id}/tickets` — list of ticket objects
  - `GET /api/sessions/{id}/cost` — `{roles: [{role, cost, tokens_in, tokens_out}], total}`
  - `GET /api/sessions/{id}/plan` — `{content: "markdown string"}`
  - `GET /api/sessions/{id}/diff` — `{diff: "diff string"}`

- [ ] **Step 3: Update UI-specific tests**
  In `tests/test_ui.py`: update tests for new template content (check for sidebar elements, session-related strings). Server endpoint tests stay in `test_server.py`.

- [ ] **Step 4: Commit**
  ```bash
  git add src/golem/server.py tests/test_ui.py
  git commit -m "feat: update server template serving and UI endpoint response shapes"
  ```

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. Template loads without error
uv run python -c "
from golem.server import create_app
app = create_app()
print('TEMPLATE_LOAD: PASS')
"

# 2. UI tests pass
uv run pytest tests/test_ui.py -v --tb=short 2>&1 | tail -1

# 3. Full suite
uv run pytest --tb=short -q 2>&1 | tail -1
```

Expected:
```
TEMPLATE_LOAD: PASS
[N] passed
[N] passed
```

---

### Phase 4 Completion Gate

**Phase 4 is NOT complete until every check below passes.**

#### Gate 1: Template Integrity

```bash
cd F:/Tools/Projects/golem-cli
test -s src/golem/ui_template.html && echo "EXISTS: PASS" || echo "EXISTS: FAIL"
grep -c "cdn\.\|unpkg\.\|jsdelivr\." src/golem/ui_template.html | xargs -I{} test {} -eq 0 && echo "NO_CDN: PASS" || echo "NO_CDN: FAIL"
```

#### Gate 2: UI Elements

```bash
cd F:/Tools/Projects/golem-cli
for elem in "sidebar" "api/sessions" "EventSource" "New Session" "merge-queue\|merge.queue\|mergeQueue" "conflict"; do
  grep -qi "$elem" src/golem/ui_template.html && echo "$elem: PASS" || echo "$elem: FAIL"
done
```

#### Gate 3: Server Template Loading

```bash
cd F:/Tools/Projects/golem-cli
uv run python -c "from golem.server import create_app; create_app(); print('APP: PASS')"
```

#### Gate 4: Full Test Suite

```bash
cd F:/Tools/Projects/golem-cli
uv run pytest -v --tb=short 2>&1 | tail -5
```

Expected: `[N] passed, 0 failed`

#### Phase 4 Verdict

| Gate | Validates Tasks |
|------|----------------|
| Gate 1-2 | Task 1 (HTML rewrite) |
| Gate 3 | Task 2 (server-side support) |
| Gate 4 | All tasks (regression) |

---

## Phase 5: Cross-Session Intelligence + Polish

**Scope:** Proactive conflict scanning, aggregate cost tracking, unified history timeline, `golem conflicts` enrichment, edge case hardening.
**Depends on:** Phases 1-4 merged to main.

### Task 1: Periodic Conflict Scanner

**Files:**
- Modify: `src/golem/merge.py`
- Modify: `src/golem/server.py`

- [ ] **Step 1: Add background scanner to MergeCoordinator**
  Add `async run_conflict_scanner(interval_seconds: int = 30)` — background task that periodically calls `detect_conflicts()` and stores results in `conflict-log.json`.

- [ ] **Step 2: Wire scanner into server lifespan**
  Start scanner as background task in server lifespan handler. Cancel on shutdown.

- [ ] **Step 3: Emit conflict SSE events**
  When new conflicts are detected, emit `conflict` SSE events on the aggregate event stream so the UI can show warnings in real-time.

- [ ] **Step 4: Commit**
  ```bash
  git add src/golem/merge.py src/golem/server.py
  git commit -m "feat: add periodic conflict scanner with SSE alerts"
  ```

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. Scanner method exists
uv run python -c "
from golem.merge import MergeCoordinator
assert hasattr(MergeCoordinator, 'run_conflict_scanner')
print('SCANNER: PASS')
"

# 2. Tests pass
uv run pytest tests/test_merge.py tests/test_server.py -v --tb=short 2>&1 | tail -1
```

Expected:
```
SCANNER: PASS
[N] passed
```

---

### Task 2: Aggregate Stats + Unified History

**Files:**
- Modify: `src/golem/server.py`
- Modify: `src/golem/cli.py`

- [ ] **Step 1: Implement /api/stats**
  Return aggregate data:
  - Session counts by state
  - Total cost across all sessions
  - Pass/fail rates across all tickets
  - Active Claude SDK session estimate

- [ ] **Step 2: Implement unified history**
  `GET /api/history` — merge progress.log entries from all sessions, sorted by timestamp, with session ID prefix.

- [ ] **Step 3: Enrich CLI commands**
  - `golem history` — unified timeline (aggregate from all sessions)
  - `golem history <session-id>` — single session timeline
  - `golem stats` — aggregate stats from server
  - `golem conflicts` — enriched output with conflict details + affected tickets

- [ ] **Step 4: Commit**
  ```bash
  git add src/golem/server.py src/golem/cli.py
  git commit -m "feat: add aggregate stats, unified history, enriched conflicts CLI"
  ```

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. Stats endpoint
uv run python -c "
from golem.server import create_app
app = create_app()
routes = [r.path for r in app.routes if hasattr(r, 'path')]
assert '/api/stats' in routes, 'FAIL: /api/stats missing'
assert '/api/history' in routes or any('history' in r for r in routes), 'FAIL: history endpoint missing'
print('ENDPOINTS: PASS')
"

# 2. Full suite
uv run pytest --tb=short -q 2>&1 | tail -1
```

Expected:
```
ENDPOINTS: PASS
[N] passed
```

---

### Task 3: Edge Case Hardening + Tests

**Files:**
- Create: `tests/test_conflicts.py`
- Modify: `src/golem/server.py`
- Modify: `src/golem/session.py`

- [ ] **Step 1: Handle edge cases**
  - Session killed mid-merge: coordinator detects stale entry, marks as `failed`, removes from queue
  - Server restart with active sessions: on startup, scan `.golem/sessions/` for `session.json` files, restore `SessionState` for any non-archived sessions (processes will be None — mark as `paused`)
  - Spec file deleted after session starts: session has immutable `spec.md` copy, so this is already safe — just verify it

- [ ] **Step 2: Create test_conflicts.py**
  Create `tests/test_conflicts.py` with tests:
  - `test_overlap_detection_two_sessions` — two sessions modifying same file
  - `test_no_overlap` — different files
  - `test_scanner_interval` — scanner runs periodically (mock asyncio.sleep)
  - `test_stale_merge_entry` — session killed mid-merge is cleaned up
  - `test_server_restart_recovery` — session state restored from disk

- [ ] **Step 3: Commit**
  ```bash
  git add tests/test_conflicts.py src/golem/server.py src/golem/session.py
  git commit -m "feat: edge case hardening + conflict detection tests"
  ```

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. Conflict tests pass
uv run pytest tests/test_conflicts.py -v --tb=short 2>&1 | tail -1

# 2. Full suite
uv run pytest --tb=short -q 2>&1 | tail -1
```

Expected: `[N] passed` + `[N] passed, 0 failed`

---

### Phase 5 Completion Gate

**Phase 5 is NOT complete until every check below passes.**

#### Gate 1: Conflict Scanner

```bash
cd F:/Tools/Projects/golem-cli
uv run python -c "
from golem.merge import MergeCoordinator
assert hasattr(MergeCoordinator, 'run_conflict_scanner')
assert hasattr(MergeCoordinator, 'detect_conflicts')
print('SCANNER: PASS')
"
```

#### Gate 2: Aggregate Endpoints

```bash
cd F:/Tools/Projects/golem-cli
uv run python -c "
from golem.server import create_app
app = create_app()
routes = [r.path for r in app.routes if hasattr(r, 'path')]
for r in ['/api/stats', '/api/conflicts']:
    assert r in routes, f'FAIL: {r} missing'
print('AGG_ENDPOINTS: PASS')
"
```

#### Gate 3: CLI Enrichments

```bash
cd F:/Tools/Projects/golem-cli
uv run golem history --help >/dev/null 2>&1 && echo "HISTORY: PASS" || echo "HISTORY: FAIL"
uv run golem stats --help >/dev/null 2>&1 && echo "STATS: PASS" || echo "STATS: FAIL"
uv run golem conflicts --help >/dev/null 2>&1 && echo "CONFLICTS: PASS" || echo "CONFLICTS: FAIL"
```

#### Gate 4: Full Test Suite

```bash
cd F:/Tools/Projects/golem-cli
uv run pytest -v --tb=short 2>&1 | tail -5
```

Expected: `[N] passed, 0 failed`

#### Phase 5 Verdict

| Gate | Validates Tasks |
|------|----------------|
| Gate 1 | Task 1 (conflict scanner) |
| Gate 2 | Task 2 (aggregate endpoints) |
| Gate 3 | Task 2 (CLI enrichments) |
| Gate 4 | All tasks (regression) |

---

## Migration & Backward Compatibility

- **`--no-server` flag** preserves current single-spec direct execution behavior
- **No server running** — CLI commands that don't need the server (doctor, version, list-specs, config) work as before
- **`.golem/` without `sessions/`** — detected as legacy single-run state; `golem status` shows legacy view
- **Existing tests** — all 314 tests continue to pass; they test pipeline logic that doesn't change
- **`Golem.ps1`** — will need updates to manage server lifecycle instead of direct subprocess; deferred to post-v1

## Non-Goals (Explicitly Out of Scope)

- **Spec dependency DAG** — specs are independent; user is responsible for execution order
- **Shared context across sessions** — each session is fully isolated; no cross-session ticket references
- **Remote/distributed execution** — server runs locally only
- **Authentication/multi-user** — single user, localhost only
- **Automatic spec decomposition** — user writes separate specs manually
