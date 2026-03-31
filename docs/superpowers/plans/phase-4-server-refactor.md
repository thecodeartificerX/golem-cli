# Phase 4: Server Refactor

## Gotchas
- Pydantic models MUST be module-level — defining `BaseModel` subclasses inside `create_app()` breaks FastAPI annotation resolution (422 errors instead of body binding)
- `create_app()` is a factory function — all routes are closures over locally-scoped objects. New route handlers must follow this pattern
- SSE generators must not be tested with `TestClient` — it hangs on infinite streams. Use `async for` with early `break` + `aclose()`
- `SessionManager` is used by `MergeCoordinator` via `SessionManagerProtocol` — the new `EdictManager` must satisfy a similar protocol or the merge coordinator needs updating
- The existing session system must continue working during transition — don't delete session endpoints, deprecate them
- `run_session()` is imported and called in-process via `asyncio.create_task()` — replace with `PipelineCoordinator.run()`
- `_on_session_done` writes status to `session.json` — the equivalent must write to the Edict JSON
- `FanoutBackend` must be wired for edict pipelines (SSE queue + file backend)
- Two-phase lifecycle: POST creates, POST /start launches — keep this pattern for edicts

## Files
```
src/golem/
├── server.py             # MODIFY — add EdictManager, repo endpoints, edict endpoints, board endpoint, deprecate session endpoints
├── merge.py              # MODIFY — merge queue keyed by edict_id (backward compat with session_id)
tests/
├── test_server.py        # MODIFY — add edict endpoint tests, keep session tests for backward compat
├── test_merge.py         # MODIFY — update for edict_id keying
```

---

## Task 4.1: Add Repo Management Endpoints

**Skills to load:** `superpowers:test-driven-development`

**Architecture notes:**

New Pydantic request models (module-level):
```python
class AddRepoRequest(BaseModel):
    path: str

class RepoResponse(BaseModel):
    id: str
    path: str
    name: str
    added_at: str
```

New endpoints in `create_app()`:
```
GET    /api/repos                → list registered repos (RepoRegistry.list_repos())
POST   /api/repos                → AddRepoRequest body → RepoRegistry.add()
DELETE /api/repos/{repo_id}      → RepoRegistry.remove() → 204 or 404
```

In `create_app()` setup:
- Determine registry path: `GOLEM_REGISTRY_PATH` env var, or `config.repo_registry_path`, or `~/.golem/repos.json`
- Create `RepoRegistry(registry_path)`
- Auto-register the current project root on startup (so the server's own repo appears)

**Files to modify:**
- `src/golem/server.py` — add 3 repo endpoints + RepoRegistry initialization

**Validation command:** `uv run pytest tests/test_server.py -k "repo" -v`

**Tests to write:**
- POST /api/repos with valid path creates repo
- POST /api/repos with invalid path returns 400
- GET /api/repos lists repos
- DELETE /api/repos/{id} removes repo
- DELETE nonexistent repo returns 404

---

## Task 4.2: Add EdictManager and Edict CRUD Endpoints

**Skills to load:** `superpowers:test-driven-development`

**Architecture notes:**

`EdictManager` class (in `server.py`, mirrors `SessionManager`):
```
class EdictManager:
    def __init__(self, golem_dir: Path):
        self._edicts: dict[str, EdictState] = {}
        self._golem_dir = golem_dir

    def create_edict(self, repo_path: str, title: str, body: str) -> EdictState: ...
    def get_edict(self, edict_id: str) -> EdictState | None: ...
    def list_edicts(self, repo_id: str | None = None) -> list[EdictState]: ...
    def remove_edict(self, edict_id: str) -> bool: ...
```

`EdictState` dataclass (in-memory state per edict, mirrors `SessionState`):
```
id: str
repo_path: str
title: str
body: str
status: str = "pending"
created_at: str
pipeline: PipelineCoordinator | None = None
task: asyncio.Task | None = None
event_queue: asyncio.Queue[str]
observe_queue: asyncio.Queue[object]
log_buffer: deque[dict] (maxlen=200)
resume_event: asyncio.Event  # starts set
cost_usd: float = 0.0
pr_url: str | None = None
```

Pydantic models (module-level):
```python
class CreateEdictRequest(BaseModel):
    title: str
    body: str

class GuidanceRequest(BaseModel):  # already exists, reuse
    text: str
```

New endpoints:
```
GET    /api/repos/{repo_id}/edicts             → list edicts for repo
POST   /api/repos/{repo_id}/edicts             → CreateEdictRequest → create edict
GET    /api/repos/{repo_id}/edicts/{id}        → edict detail
PATCH  /api/repos/{repo_id}/edicts/{id}        → update (title, body, re-queue)
DELETE /api/repos/{repo_id}/edicts/{id}        → cancel/remove
POST   /api/repos/{repo_id}/edicts/{id}/start  → kick off PipelineCoordinator
POST   /api/repos/{repo_id}/edicts/{id}/pause  → pipeline.pause()
POST   /api/repos/{repo_id}/edicts/{id}/resume → pipeline.resume()
POST   /api/repos/{repo_id}/edicts/{id}/kill   → pipeline.kill()
POST   /api/repos/{repo_id}/edicts/{id}/guidance → pipeline.send_guidance()
```

The `/start` endpoint:
1. Validates edict status is `pending` or `needs_attention`
2. Creates edict dir scaffold via `create_edict_dir()`
3. Creates `EdictStore` + `TicketStore` scoped to the edict dir
4. Creates `EventBus` with `FanoutBackend([QueueBackend(observe_queue), FileBackend(events.jsonl)])`
5. Creates `PipelineCoordinator` with all dependencies
6. Launches `pipeline.run()` as `asyncio.Task`
7. Attaches `_on_edict_done` callback

`_on_edict_done(edict_state, result)`:
- Updates in-memory state from `PipelineResult`
- Writes final status to edict JSON on disk
- Enqueues for merge if status is `done`

**Files to modify:**
- `src/golem/server.py` — add EdictManager, EdictState, 10 edict endpoints

**Validation command:** `uv run pytest tests/test_server.py -k "edict" -v`

**Tests to write:**
- POST create edict returns 201 with edict ID
- GET list edicts returns array
- GET edict detail returns full object
- PATCH update edict title/body
- DELETE edict removes it
- POST start transitions to running (mock pipeline.run)
- POST start on already-running edict returns 400
- POST pause/resume lifecycle
- POST kill cancels the pipeline
- POST guidance creates guidance ticket

---

## Task 4.3: Add Pipeline Board and Observability Endpoints

**Skills to load:** `superpowers:test-driven-development`

**Architecture notes:**

New endpoints:
```
GET /api/repos/{repo_id}/edicts/{id}/board   → tickets grouped by pipeline_stage
GET /api/repos/{repo_id}/edicts/{id}/observe → SSE event stream
GET /api/repos/{repo_id}/edicts/{id}/logs    → SSE log stream
GET /api/repos/{repo_id}/edicts/{id}/cost    → cost breakdown
GET /api/repos/{repo_id}/edicts/{id}/diff    → git diff
GET /api/repos/{repo_id}/edicts/{id}/plan    → plan files content
GET /api/repos/{repo_id}/edicts/{id}/tickets → all tickets for edict
GET /api/repos/{repo_id}/edicts/{id}/tickets/{tid} → ticket detail
GET /api/repos/{repo_id}/edicts/{id}/tickets/{tid}/events → SSE for specific ticket
```

Board endpoint response shape:
```json
{
  "edict_id": "EDICT-001",
  "columns": {
    "planner": { "count": 0, "active": false, "tickets": [] },
    "tech_lead": { "count": 1, "tickets": [{ "id": "T-003", "title": "...", "status": "queued", "agent_id": "" }] },
    "junior_dev": { "count": 2, "tickets": [{ "id": "T-002", "title": "...", "status": "active", "agent_id": "JD-1" }, ...] },
    "qa": { "count": 1, "tickets": [{ "id": "T-004", "title": "...", "status": "review", "agent_id": "JD-2" }] },
    "done": { "count": 1, "tickets": [{ "id": "T-001", "title": "...", "status": "done", "agent_id": "" }] },
    "failed": { "count": 0, "tickets": [] }
  },
  "column_order": ["planner", "tech_lead", "junior_dev", "qa", "done", "failed"]
}
```

The board endpoint reads from `TicketStore` filtered by `edict_id`, then groups by `pipeline_stage`.

The planner column is special — it has no tickets (planner enriches the edict, not ticket cards). Instead, it shows `"active": true/false` based on edict status being `planning`.

SSE endpoints follow the same pattern as existing session SSE — replay buffer then live stream.

Per-ticket events endpoint filters the observe stream by `ticket_id` field in events.

**Files to modify:**
- `src/golem/server.py` — add board + observability endpoints

**Validation command:** `uv run pytest tests/test_server.py -k "board or observe or cost or diff or plan" -v`

**Tests to write:**
- Board endpoint returns correct column grouping
- Board endpoint handles empty ticket store
- Planner column shows active=true when edict is planning
- Ticket list filtered by edict
- Cost endpoint parses agent costs
- Plan endpoint returns overview.md content
- SSE observe stream emits agent_event data (mock EventBus)

---

## Task 4.4: Update Merge Coordinator for Edict Keying

**Skills to load:** None (targeted modification)

**Architecture notes:**

`MergeQueueEntry` needs an `edict_id` field:
```
edict_id: str = ""  — optional, backward compat with session-based entries
```

`MergeCoordinator.enqueue()` should accept `edict_id: str = ""` parameter.

`create_pr()` should use edict-scoped branch naming: `golem/{edict_id}/integration` (falls back to `golem/{session_id}/integration` if `edict_id` is empty).

`detect_conflicts()` should group by edict_id when available.

This is backward-compatible — existing session-based merge queue entries continue to work.

**Files to modify:**
- `src/golem/merge.py` — add `edict_id` field, update enqueue/create_pr

**Validation command:** `uv run pytest tests/test_merge.py -v`

**Tests to write/update:**
- Enqueue with edict_id
- create_pr uses edict-scoped branch name
- Backward compat: entries without edict_id still work

---

## Task 4.5: Update Server Status Endpoint

**Skills to load:** None

**Architecture notes:**

Update `GET /api/server/status` to include:
```json
{
  "pid": 1234,
  "port": 7665,
  "uptime_seconds": 120,
  "repo_count": 2,
  "edict_counts": { "pending": 3, "planning": 1, "in_progress": 2, "done": 5, "failed": 1 },
  "session_counts": { ... }  // keep for backward compat
}
```

**Files to modify:**
- `src/golem/server.py` — update status endpoint

**Validation command:** `uv run pytest tests/test_server.py -k "status" -v`
