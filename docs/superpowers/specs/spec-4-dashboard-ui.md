# Spec 4: Dashboard UI

> Part 4 of 5 in the Multi-Spec Orchestration series.
> Full design doc: `docs/superpowers/specs/2026-03-27-multi-spec-orchestration-design.md`
> **Depends on:** Spec 1 (Foundation + Server Core) merged to main.
> **Can run in parallel with:** Specs 2 and 3.

## Context

The current `ui_template.html` is a single-run viewer with one control bar and one log stream. This spec rewrites it as a multi-session dashboard with a session sidebar, detail view with sub-tabs, aggregate stats, and conflict alerts.

### Target Layout

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
| -- Merge Queue --    |                                            |
|   search-refactor-1  | [!] Overlap: TICKET-003 touches            |
|   PR #47 - awaiting  |     src/auth/session.py also modified by    |
|                      |     user-settings-1 TICKET-002             |
| -- Completed --      |                                            |
|   nav-redesign-1     |                                            |
|   Merged - PR #45    |                                            |
+----------------------+--------------------------------------------+
| [STATUS BAR]  Server running | 4 Claude sessions active          |
+------------------------------------------------------------------+
```

### Server API Endpoints (from Spec 1)

The dashboard consumes these REST + SSE endpoints:
- `GET /api/sessions` — list all sessions
- `GET /api/sessions/{id}` — session detail
- `GET /api/sessions/{id}/tickets` — tickets for session
- `GET /api/sessions/{id}/events` — SSE stream per session
- `GET /api/sessions/{id}/cost` — cost breakdown
- `GET /api/sessions/{id}/plan` — plan overview markdown
- `GET /api/sessions/{id}/diff` — git diff
- `POST /api/sessions` — create new session
- `POST /api/sessions/{id}/pause` / `resume` / `guidance`
- `DELETE /api/sessions/{id}` — kill session
- `GET /api/events` — aggregate SSE stream
- `GET /api/browse/file` — native file picker
- `GET /api/specs` — find spec files

### Coding Conventions

- **Self-contained HTML** — no CDN dependencies, no build step, all CSS/JS inline
- **No emoji** — Rich/Windows compat; use ASCII text only
- **Extend current dark theme** — CSS custom properties from existing `ui_template.html`
- **Vanilla JS** — no frameworks, SSE via `EventSource`
- **Tests:** `tests/test_ui.py` checks template content via string assertions; assert on short strings (Rich wraps in narrow terminals)

---

## Task 1: Dashboard HTML Rewrite

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
  - Diff tab: git diff output with monospace pre block
  - Cost tab: token breakdown by agent role

- [ ] **Step 4: New Session dialog**
  - Triggered by `+ New Session` button
  - Spec file input with BROWSE button (calls `GET /api/browse/file`)
  - Project root auto-fills from spec parent dir
  - LAUNCH button calls `POST /api/sessions`

- [ ] **Step 5: Multi-session SSE management**
  JS client changes:
  - Connect to `/api/sessions/{id}/events` when session is selected
  - Disconnect previous session's SSE when switching sessions
  - Poll `GET /api/sessions` periodically (every 3s) to update sidebar state
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

## Task 2: Server-Side UI Support

**Files:**
- Modify: `src/golem/server.py`

- [ ] **Step 1: Update template serving**
  Update `GET /` to serve the new `ui_template.html`. Ensure the server loads the template at startup (same pattern as current `ui.py` — read file into module-level `_template_html` string).

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

## Phase 4 Completion Gate

**Phase 4 is NOT complete until every check below passes.** If any check fails, return to the responsible task, fix the issue, and re-run this entire gate.

### Gate 1: Template Integrity

```bash
cd F:/Tools/Projects/golem-cli
test -s src/golem/ui_template.html && echo "EXISTS: PASS" || echo "EXISTS: FAIL"
grep -c "cdn\.\|unpkg\.\|jsdelivr\." src/golem/ui_template.html | xargs -I{} test {} -eq 0 && echo "NO_CDN: PASS" || echo "NO_CDN: FAIL"
```

### Gate 2: UI Elements

```bash
cd F:/Tools/Projects/golem-cli
for elem in "sidebar" "api/sessions" "EventSource" "New Session" "merge-queue\|merge.queue\|mergeQueue" "conflict"; do
  grep -qi "$elem" src/golem/ui_template.html && echo "$elem: PASS" || echo "$elem: FAIL"
done
```

### Gate 3: Server Template Loading

```bash
cd F:/Tools/Projects/golem-cli
uv run python -c "from golem.server import create_app; create_app(); print('APP: PASS')"
```

### Gate 4: Full Test Suite

```bash
cd F:/Tools/Projects/golem-cli
uv run pytest -v --tb=short 2>&1 | tail -5
```

Expected: `[N] passed, 0 failed`

### Phase 4 Verdict

| Gate | Validates Tasks |
|------|----------------|
| Gate 1-2 | Task 1 (HTML rewrite) |
| Gate 3 | Task 2 (server-side support) |
| Gate 4 | All tasks (regression) |
