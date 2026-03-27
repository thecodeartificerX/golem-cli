# Spec 3: Merge Coordinator

> Part 3 of 5 in the Multi-Spec Orchestration series.
> Full design doc: `docs/superpowers/specs/2026-03-27-multi-spec-orchestration-design.md`
> **Depends on:** Spec 1 (Foundation + Server Core) merged to main.
> **Can run in parallel with:** Specs 2 and 4.

## Context

Spec 1 added the server with a stub MergeCoordinator. This spec implements the full merge pipeline: FIFO merge queue, PR creation per session via `gh`, post-merge rebase cascade, and proactive conflict detection across sessions.

### Merge Flow

1. Session pipeline completes -> status `awaiting_merge` -> added to queue
2. Coordinator picks next in FIFO order
3. Create PR via `gh pr create` with session's integration branch
4. Status -> `pr_open`, `session.json` updated with `pr_number` and `pr_url`
5. User approves (CLI `golem approve` or UI) -> coordinator merges via `gh pr merge`
6. Rebase cascade: for each remaining queued session:
   - `git fetch origin main`
   - `git rebase origin/main` on session's integration branch
   - Re-run QA on rebased code
   - If rebase fails: status -> `conflict`, alert user
   - If QA fails: status -> `qa_failed`, alert user
7. Session -> `merged` -> auto-archive after configurable delay

### Conflict Detection

- Periodically scan active session worktrees for overlapping modified files
- `git diff --name-only` per session's integration branch vs main
- Cross-reference file lists across sessions
- Surface warnings in UI and via `golem conflicts` CLI command

### Coding Conventions

- **Python 3.12+**, async-first, strict typing, no `Any`
- **Always `encoding="utf-8"`** on all file I/O
- **No emoji in CLI/TUI output**
- **Formatter:** ruff, line length 120
- **Tests:** pytest with pytest-asyncio, use `tmp_path` fixture
- **`create_pr()` and `verify_pr()` are async** — use `await`; use `asyncio.sleep` for polling, not `time.sleep`

---

## Task 1: Merge Module

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

## Task 2: Server Integration + Merge Endpoints

**Files:**
- Modify: `src/golem/server.py`

- [ ] **Step 1: Wire MergeCoordinator into server**
  In `create_app()`:
  - Replace the stub `MergeCoordinator` with the real one from `golem.merge`
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

## Task 3: Merge CLI Commands

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

## Phase 3 Completion Gate

**Phase 3 is NOT complete until every check below passes.** If any check fails, return to the responsible task, fix the issue, and re-run this entire gate.

### Gate 1: Merge Module

```bash
cd F:/Tools/Projects/golem-cli
uv run python -c "from golem.merge import MergeCoordinator, ConflictInfo, MergeQueueEntry; print('MERGE: PASS')"
uv run pytest tests/test_merge.py -v --tb=short 2>&1 | tail -1
```

### Gate 2: Server Endpoints

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

### Gate 3: CLI Commands

```bash
cd F:/Tools/Projects/golem-cli
for cmd in merge approve merge-queue conflicts; do
  uv run golem $cmd --help >/dev/null 2>&1 && echo "$cmd: PASS" || echo "$cmd: FAIL"
done
```

### Gate 4: Full Test Suite

```bash
cd F:/Tools/Projects/golem-cli
uv run pytest -v --tb=short 2>&1 | tail -5
```

Expected: `[N] passed, 0 failed`

### Phase 3 Verdict

| Gate | Validates Tasks |
|------|----------------|
| Gate 1 | Task 1 (merge module) |
| Gate 2 | Task 2 (server integration) |
| Gate 3 | Task 3 (CLI commands) |
| Gate 4 | All tasks (regression) |
