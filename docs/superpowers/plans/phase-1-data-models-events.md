# Phase 1: Data Models & Events

## Gotchas
- All file I/O must use `encoding="utf-8"` — Windows defaults to cp1252
- `TicketStore` uses `_write_json_atomic()` with `os.replace()` — new stores should follow the same pattern
- Event count is hardcoded in `test_events.py`, `test_recovery.py`, `test_parallel.py` — adding 3 new event types means updating all three
- `tickets.py` already has a `session_id` field — the new `edict_id` field is a separate parent reference (Edict > Session > Tickets)
- Pydantic models for FastAPI must be module-level, not inside factory functions

## Files
```
src/golem/
├── edict.py              # CREATE — Edict model, EdictStore, status transitions
├── repos.py              # CREATE — Repo registry (add/remove/list, repos.json persistence)
├── tickets.py            # MODIFY — add edict_id, pipeline_stage, agent_id fields
├── events.py             # MODIFY — add 3 new event types (EdictCreated, EdictUpdated, EdictNeedsAttention)
├── config.py             # MODIFY — add edict-related settings
tests/
├── test_edict.py         # CREATE — Edict model and EdictStore tests
├── test_repos.py         # CREATE — Repo registry tests
├── test_tickets.py       # MODIFY — cover new fields
├── test_events.py        # MODIFY — update event count assertion
├── test_recovery.py      # MODIFY — update event count assertion
├── test_parallel.py      # MODIFY — update event count assertion
```

---

## Task 1.1: Create Edict Model and EdictStore

**Skills to load:** `superpowers:test-driven-development`

**Architecture notes:**

Edict dataclass fields:
```
id: str              — "EDICT-001" format, zero-padded 3 digits, auto-incremented
repo_path: str       — absolute path to the repo root
title: str           — short summary
body: str            — freeform text (one-liner to full spec)
status: str          — one of: pending, planning, in_progress, needs_attention, done, failed
created_at: str      — ISO-8601 UTC
updated_at: str      — ISO-8601 UTC
pr_url: str | None   — GitHub PR URL when created
ticket_ids: list[str] — child ticket IDs, populated by tech lead
cost_usd: float      — aggregated from all agents
error: str | None    — error message on failure
```

Status constants as module-level strings (match `session.py` pattern):
```
EDICT_PENDING = "pending"
EDICT_PLANNING = "planning"
EDICT_IN_PROGRESS = "in_progress"
EDICT_NEEDS_ATTENTION = "needs_attention"
EDICT_DONE = "done"
EDICT_FAILED = "failed"
```

Valid transitions (enforce in `EdictStore.update_status()`):
```
pending -> planning
planning -> in_progress, needs_attention, failed
in_progress -> done, needs_attention, failed
needs_attention -> planning, in_progress (user re-queues)
```

`EdictStore` class — follows `TicketStore` patterns exactly:
- Constructor: `__init__(self, edicts_dir: Path)` — holds Path + asyncio.Lock
- `async create(edict: Edict) -> str` — auto-assigns next EDICT-NNN ID, writes atomically
- `async read(edict_id: str) -> Edict` — reads JSON, case-insensitive path resolution
- `async update_status(edict_id: str, status: str, error: str | None = None) -> None` — validates transition, updates `updated_at`
- `async update(edict_id: str, **kwargs) -> None` — partial update (title, body, pr_url, ticket_ids, cost_usd)
- `async list_edicts(status_filter: str | None = None) -> list[Edict]` — glob scan, optional filter
- `async delete(edict_id: str) -> bool` — removes JSON file

Storage: one JSON file per edict at `<edicts_dir>/EDICT-NNN.json`. Use `_write_json_atomic()` pattern from `tickets.py`.

**Files to create/modify:**
- `src/golem/edict.py` — Edict dataclass, status constants, EdictStore class

**Validation command:** `uv run pytest tests/test_edict.py -v`

**Tests to write (`test_edict.py`):**
- Create edict, verify auto-ID assignment (EDICT-001, EDICT-002...)
- Read edict by ID (case-insensitive)
- Update status with valid transitions
- Reject invalid status transitions (raises ValueError)
- List edicts with and without status filter
- Delete edict
- Concurrent create (two tasks creating simultaneously — lock prevents ID collision)
- Round-trip serialization (create → read → verify all fields match)

---

## Task 1.2: Create Repo Registry

**Skills to load:** `superpowers:test-driven-development`

**Architecture notes:**

Repo dataclass fields:
```
id: str         — derived from directory name (e.g., "golem-cli")
path: str       — absolute path (e.g., "F:\Tools\Projects\golem-cli")
name: str       — display name (defaults to directory name)
added_at: str   — ISO-8601 UTC
```

`RepoRegistry` class:
- Constructor: `__init__(self, registry_path: Path)` — path to `repos.json` file
- `async add(path: str, name: str | None = None) -> Repo` — validates path exists and is a directory, derives ID from dir name, deduplicates by path, appends to registry
- `async remove(repo_id: str) -> bool` — removes by ID, returns True if found
- `async list_repos() -> list[Repo]` — returns all registered repos
- `async get(repo_id: str) -> Repo | None` — lookup by ID

Storage: single `repos.json` file (JSON array of Repo dicts). Atomic write via temp file + `os.replace()`.

Registry location: configurable, default `~/.golem/repos.json` (user-level, not per-project). This allows the server to manage repos across projects.

**Files to create/modify:**
- `src/golem/repos.py` — Repo dataclass, RepoRegistry class

**Validation command:** `uv run pytest tests/test_repos.py -v`

**Tests to write (`test_repos.py`):**
- Add repo, verify ID derived from dir name
- Add repo with custom name
- Add duplicate path is idempotent (returns existing)
- Remove repo by ID
- Remove nonexistent repo returns False
- List repos returns all entries
- Get repo by ID
- Get nonexistent repo returns None
- Registry persists across RepoRegistry instances (write, recreate, read)
- Invalid path (nonexistent directory) raises ValueError

---

## Task 1.3: Extend Ticket Model

**Skills to load:** `superpowers:test-driven-development`

**Architecture notes:**

Add 3 new fields to the `Ticket` dataclass:
```
edict_id: str = ""           — parent Edict reference (e.g., "EDICT-001")
pipeline_stage: str = ""     — "planner" | "tech_lead" | "junior_dev" | "qa" | "done" | "failed"
agent_id: str = ""           — which agent instance is handling this (e.g., "junior-dev-3")
```

`pipeline_stage` determines which board column the card sits in. It is separate from `status` which tracks the ticket's own lifecycle (pending, in_progress, done, etc.).

Pipeline stage constants:
```
STAGE_PLANNER = "planner"
STAGE_TECH_LEAD = "tech_lead"
STAGE_JUNIOR_DEV = "junior_dev"
STAGE_QA = "qa"
STAGE_DONE = "done"
STAGE_FAILED = "failed"
```

Update `TicketStore.list_tickets()` to accept optional `edict_id_filter: str | None = None` and `pipeline_stage_filter: str | None = None`.

Backward compatibility: all new fields default to empty string so existing tickets deserialize without error.

**Files to modify:**
- `src/golem/tickets.py` — add fields to Ticket, update list_tickets filters

**Validation command:** `uv run pytest tests/test_tickets.py -v`

**Tests to write/update:**
- Create ticket with edict_id, pipeline_stage, agent_id
- List tickets filtered by edict_id
- List tickets filtered by pipeline_stage
- Existing tickets without new fields deserialize with defaults
- Update pipeline_stage via update() method

---

## Task 1.4: Add Edict Event Types

**Skills to load:** `superpowers:test-driven-development`

**Architecture notes:**

Add 3 new event types to `events.py`:

```
EdictCreated:
  edict_id: str
  title: str
  repo_path: str

EdictUpdated:
  edict_id: str
  old_status: str
  new_status: str

EdictNeedsAttention:
  edict_id: str
  reason: str
  ticket_id: str = ""   — which ticket triggered the escalation
```

Register all 3 in `_register_events()`. This brings the total from 41 to 44 event types.

**CRITICAL:** Update the hardcoded event count in:
- `test_events.py` — the assertion checking `len(EVENT_TYPES)`
- `test_recovery.py` — same
- `test_parallel.py` — same

All three must change from 41 to 44.

**Files to modify:**
- `src/golem/events.py` — add 3 dataclasses, register them
- `tests/test_events.py` — update count assertion
- `tests/test_recovery.py` — update count assertion
- `tests/test_parallel.py` — update count assertion

**Validation command:** `uv run pytest tests/test_events.py tests/test_recovery.py tests/test_parallel.py -v`

**Tests to write/update:**
- Each new event type serializes/deserializes correctly via `to_dict()` / `from_dict()`
- Event count assertion updated to 44

---

## Task 1.5: Add Edict Config Settings

**Skills to load:** None (minor modification)

**Architecture notes:**

Add to `GolemConfig` dataclass:
```
edict_max_retries: int = 3              — max self-heal retries before needs_attention
edict_auto_start: bool = True           — auto-start pipeline on edict creation
repo_registry_path: str = ""            — override for repos.json location (empty = ~/.golem/repos.json)
```

Add to `validate()`: check `edict_max_retries >= 0`.

**Files to modify:**
- `src/golem/config.py` — add fields, update validate()

**Validation command:** `uv run pytest tests/test_config.py -v`

**Tests to write/update:**
- New config fields have correct defaults
- Validation catches negative `edict_max_retries`
- Config round-trip (save/load) preserves new fields
