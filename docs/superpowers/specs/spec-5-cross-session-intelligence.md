# Spec 5: Cross-Session Intelligence + Polish

> Part 5 of 5 in the Multi-Spec Orchestration series.
> Full design doc: `docs/superpowers/specs/2026-03-27-multi-spec-orchestration-design.md`
> **Depends on:** Specs 1-4 merged to main.
> **Execution order:** Run LAST.

## Context

Specs 1-4 built the foundation (server, sessions, namespacing), CLI client, merge coordinator, and dashboard UI. This final spec adds the cross-session intelligence layer: proactive conflict scanning, aggregate stats, unified history timeline, and edge case hardening.

### What Exists After Specs 1-4

- `src/golem/server.py` — FastAPI server with SessionManager, MergeCoordinator, all endpoints
- `src/golem/session.py` — SessionMetadata, ID generation, dir scaffolding
- `src/golem/client.py` — GolemClient HTTP wrapper
- `src/golem/merge.py` — MergeCoordinator with queue, PR management, rebase cascade, `detect_conflicts()`
- `src/golem/cli.py` — Full CLI with server routing, session commands, merge commands
- `src/golem/ui_template.html` — Multi-session dashboard with sidebar, detail view, SSE

### Coding Conventions

- **Python 3.12+**, async-first, strict typing, no `Any`
- **Always `encoding="utf-8"`** on all file I/O
- **No emoji in CLI/TUI output**
- **Formatter:** ruff, line length 120
- **Tests:** pytest with pytest-asyncio, use `tmp_path` fixture

---

## Task 1: Periodic Conflict Scanner

**Files:**
- Modify: `src/golem/merge.py`
- Modify: `src/golem/server.py`

- [ ] **Step 1: Add background scanner to MergeCoordinator**
  Add `async run_conflict_scanner(interval_seconds: int = 30)` to `MergeCoordinator`:
  - Runs in an infinite loop with `await asyncio.sleep(interval_seconds)` between scans
  - Calls `detect_conflicts()` each iteration
  - Stores results in `conflict-log.json` (with `encoding="utf-8"`)
  - Compares with previous scan to detect NEW conflicts

- [ ] **Step 2: Wire scanner into server lifespan**
  Start scanner as background `asyncio.Task` in server lifespan handler. Cancel on shutdown.

- [ ] **Step 3: Emit conflict SSE events**
  When new conflicts are detected (not previously seen), emit `conflict` SSE events on the aggregate event stream (`GET /api/events`) so the UI can show warnings in real-time without polling.

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

## Task 2: Aggregate Stats + Unified History

**Files:**
- Modify: `src/golem/server.py`
- Modify: `src/golem/cli.py`

- [ ] **Step 1: Implement /api/stats**
  Add `GET /api/stats` endpoint returning aggregate data:
  - Session counts by state (`{running: N, queued: N, merged: N, failed: N}`)
  - Total cost across all sessions
  - Pass/fail rates across all tickets (from all sessions' ticket stores)
  - Active Claude SDK session estimate (count of running sessions)

- [ ] **Step 2: Implement unified history**
  Add `GET /api/history` endpoint:
  - Read `progress.log` from each session directory
  - Merge all entries sorted by timestamp
  - Prefix each entry with session ID
  - Support optional `?session_id=X` query param to filter to one session

- [ ] **Step 3: Enrich CLI commands**
  - `golem history` — unified timeline (calls `GET /api/history`), renders as rich table
  - `golem history <session-id>` — single session timeline (calls `GET /api/history?session_id=X`)
  - `golem stats` — aggregate stats (calls `GET /api/stats`), renders as rich table
  - `golem conflicts` — enriched output with conflict details + affected tickets (calls `GET /api/conflicts`)

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

## Task 3: Edge Case Hardening + Tests

**Files:**
- Create: `tests/test_conflicts.py`
- Modify: `src/golem/server.py`
- Modify: `src/golem/session.py`

- [ ] **Step 1: Handle edge cases**
  Implement these edge case handlers:
  - **Session killed mid-merge:** coordinator detects stale entry (PID dead, session status still `pr_open`), marks as `failed`, removes from queue
  - **Server restart with active sessions:** on startup, scan `.golem/sessions/` for `session.json` files, restore `SessionState` for any non-archived sessions. Processes will be None — mark as `paused` so user can resume.
  - **Spec file deleted after session starts:** session has immutable `spec.md` copy in session dir — verify this is safe by checking `create_session_dir` always copies.

- [ ] **Step 2: Create test_conflicts.py**
  Create `tests/test_conflicts.py` with tests:
  - `test_overlap_detection_two_sessions` — two sessions modifying same file detected
  - `test_no_overlap` — sessions with different files return empty conflicts
  - `test_scanner_interval` — scanner runs periodically (mock `asyncio.sleep`, verify `detect_conflicts` called)
  - `test_stale_merge_entry` — session killed mid-merge is cleaned up from queue
  - `test_server_restart_recovery` — session state restored from disk on startup

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

## Phase 5 Completion Gate

**Phase 5 is NOT complete until every check below passes.** If any check fails, return to the responsible task, fix the issue, and re-run this entire gate.

### Gate 1: Conflict Scanner

```bash
cd F:/Tools/Projects/golem-cli
uv run python -c "
from golem.merge import MergeCoordinator
assert hasattr(MergeCoordinator, 'run_conflict_scanner')
assert hasattr(MergeCoordinator, 'detect_conflicts')
print('SCANNER: PASS')
"
```

### Gate 2: Aggregate Endpoints

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

### Gate 3: CLI Enrichments

```bash
cd F:/Tools/Projects/golem-cli
uv run golem history --help >/dev/null 2>&1 && echo "HISTORY: PASS" || echo "HISTORY: FAIL"
uv run golem stats --help >/dev/null 2>&1 && echo "STATS: PASS" || echo "STATS: FAIL"
uv run golem conflicts --help >/dev/null 2>&1 && echo "CONFLICTS: PASS" || echo "CONFLICTS: FAIL"
```

### Gate 4: Full Test Suite

```bash
cd F:/Tools/Projects/golem-cli
uv run pytest -v --tb=short 2>&1 | tail -5
```

Expected: `[N] passed, 0 failed`

### Phase 5 Verdict

| Gate | Validates Tasks |
|------|----------------|
| Gate 1 | Task 1 (conflict scanner) |
| Gate 2 | Task 2 (aggregate endpoints) |
| Gate 3 | Task 2 (CLI enrichments) |
| Gate 4 | All tasks (regression) |
