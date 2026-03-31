# Phase 6: Dashboard UI

## Gotchas
- All CSS/JS must be inline — no CDN imports, no bundler, no external dependencies (existing pattern)
- No emoji in any text — Rich and Windows cp1252 console crash on emoji; the UI renders in a browser but keep consistent
- CSS custom properties are defined on `:root` — extend, don't replace the existing dark theme
- Tab pane CSS specificity: `#pane-*` ID selectors override `.tab-pane { display: none }` — use `#pane-foo.active` for display overrides
- SSE generators in tests must use `async for` with early `break` + `aclose()` — `TestClient` hangs on infinite streams
- `pollSessions` must call `updateSessionHeader` — buttons only refresh on poll if the selected item's header is refreshed
- The dashboard is served by `GET /` in `server.py` — update to serve `dashboard.html` instead of `ui_template.html`
- Native file dialog via `/api/browse/folder` returns `null` on cancel — handle gracefully in JS
- ES5-compatible JavaScript (no arrow functions, no const/let, no template literals) — match existing `ui_template.html` pattern
- Board card animations: use CSS transitions on `transform` and `opacity` — no JS animation libraries

## Files
```
src/golem/
├── dashboard.html        # CREATE — complete new dashboard UI
├── server.py             # MODIFY — serve dashboard.html from GET /
tests/
├── test_dashboard.py     # CREATE — dashboard endpoint tests (rename from test_ui.py scope)
```

---

## Task 6.1: Build Dashboard Layout Shell

**Skills to load:** `frontend-design:frontend-design`

**Architecture notes:**

Single self-contained HTML file. Structure:

```
#app (flex column, 100vh)
├── #top-bar
│   ├── "G O L E M" brand
│   ├── #repo-tabs (horizontal tab strip)
│   │   ├── .repo-tab (one per registered repo, click switches view)
│   │   └── #add-repo-btn ("+ Add Repo" — opens folder picker)
│   └── #aggregate-cost ("$12.34" total across all edicts)
│
├── #body (flex row)
│   ├── #sidebar (260px, collapsible)
│   │   ├── #new-edict-btn ("+ NEW EDICT")
│   │   └── #edict-list
│   │       ├── .edict-group "TO DO" (collapsible, pending edicts)
│   │       ├── .edict-group "IN PROGRESS" (planning + in_progress + needs_attention)
│   │       └── .edict-group "COMPLETED" (done + failed, collapsed by default)
│   │
│   └── #main-content (flex: 1)
│       ├── #empty-state ("Select an edict or create a new one")
│       └── #edict-detail (hidden until edict selected)
│           ├── #edict-header (title, status badge, Pause/Kill buttons)
│           ├── #tab-bar (5 tabs: Board, Plan, Diff, Cost, Logs)
│           └── #tab-content
│               ├── #pane-board    (default active)
│               ├── #pane-plan
│               ├── #pane-diff
│               ├── #pane-cost
│               └── #pane-logs
│
└── #status-bar ("Server running" + active edict count)

#new-edict-modal (overlay)
#card-detail-modal (overlay)
```

**CSS theme:** Extend existing dark theme variables. Keep all `--bg`, `--panel`, `--border`, `--label` etc. Add:
```css
--stage-planner: #a78bfa;     /* purple */
--stage-tech-lead: #f97316;   /* orange */
--stage-junior-dev: #3b82f6;  /* blue */
--stage-qa: #eab308;          /* yellow */
--stage-done: #22c55e;        /* green */
--stage-failed: #ef4444;      /* red */
```

**Design constraint:** The repo tabs, edict sidebar, and pipeline board must all work with real-time data updates via polling + SSE. No page reloads.

**Design constraint:** The layout must handle 0 repos, 0 edicts, and 0 tickets gracefully with appropriate empty states.

**Ordering note:** This task creates the HTML/CSS shell only. JavaScript interactivity is in Tasks 6.2-6.5.

**Files to create:**
- `src/golem/dashboard.html` — HTML + CSS layout shell

**Validation command:** Visual review — serve with `python -m http.server` and inspect layout

---

## Task 6.2: Build Repo Tabs and Edict Sidebar JavaScript

**Skills to load:** `frontend-design:frontend-design`

> Read the existing `src/golem/ui_template.html` for JavaScript patterns (polling, SSE, DOM updates, ES5 style).

**Architecture notes:**

**State variables:**
```javascript
var selectedRepoId = null;
var selectedEdictId = null;
var repos = [];              // from GET /api/repos
var edicts = {};             // repo_id -> [edict, ...]
var pollTimerId = null;
```

**Repo tabs:**
- On load: `fetchRepos()` → `GET /api/repos` → render tabs
- Click tab: set `selectedRepoId`, call `fetchEdicts(repoId)`
- "+ Add Repo" button: call `GET /api/browse/folder`, then `POST /api/repos` with the path, refresh tabs
- Right-click tab: show native context menu or simple confirm → `DELETE /api/repos/{id}`

**Edict sidebar:**
- `fetchEdicts(repoId)` → `GET /api/repos/{repoId}/edicts` every 3 seconds (polling, same as existing session polling)
- Group edicts into 3 sections:
  - "TO DO": status `pending`
  - "IN PROGRESS": status `planning`, `in_progress`, `needs_attention`
  - "COMPLETED": status `done`, `failed`
- Each edict entry shows: status dot, EDICT-NNN, title (2-line clamp), ticket progress (e.g., "3/5 tickets"), cost
- `needs_attention` edicts show a warning indicator (colored border or icon)
- Section headers show count, clickable to collapse/expand
- "COMPLETED" section collapsed by default
- Click edict: call `selectEdict(edictId)` → update main content

**"+ NEW EDICT" button:**
- Opens `#new-edict-modal`
- Form: Title input + Description textarea
- Submit: `POST /api/repos/{repoId}/edicts` → close modal, select the new edict

**Files to modify:**
- `src/golem/dashboard.html` — add JavaScript for repos + sidebar

**Validation command:** `uv run pytest tests/test_server.py -k "repos or edicts" -v` (verify API integration)

---

## Task 6.3: Build Agent Pipeline Board

**Skills to load:** `frontend-design:frontend-design`

**Architecture notes:**

The Board tab is the default view when an edict is selected.

**Board layout:**
```
#pipeline-board (flex row, horizontal scroll if needed)
├── .board-col#col-planner     (special — shows activity indicator, no ticket cards)
├── .board-col#col-tech-lead
├── .board-col#col-junior-dev
├── .board-col#col-qa
└── .board-col#col-done-fail   (combined Done + Failed)
```

Each column:
- Header: agent name + ticket count badge
- Body: `.board-col-cards` container with ticket cards

**Ticket cards (.board-card):**
```
.board-card
├── .card-id ("T-002")
├── .card-title ("Connect API endpoint" — 2-line clamp)
├── .card-agent ("JD-1" — agent instance badge)
├── .card-status ("active" — colored badge)
└── .card-cost ("$0.12" — if available)
```

**Color per column** — use `--stage-*` CSS variables for column headers and card accents.

**Data flow:**
- `fetchBoard(edictId)` → `GET /api/repos/{repoId}/edicts/{edictId}/board`
- Returns columns with tickets grouped by `pipeline_stage`
- Re-fetch every 3 seconds (same poll interval as edicts)
- On SSE `ticket_updated` events, update individual cards without full re-fetch

**Card animations:**
- When a ticket moves between columns, use CSS `transition: transform 0.3s ease, opacity 0.3s ease`
- Remove card from old column with fade-out, add to new column with fade-in
- Implementation: track `boardCards` by ticket ID, compare old vs new `pipeline_stage` on each poll

**Planner column special behavior:**
- No ticket cards
- Shows activity indicator (pulsing dot + "Researching..." text) when edict status is `planning`
- Shows "Done" with checkmark when planning is complete
- Empty when edict hasn't started

**Edict-level controls above the board:**
- Pause button (visible when status is `planning` or `in_progress`)
- Kill button (visible when status is not `done` or `failed`)
- Status badge showing current edict status

**Files to modify:**
- `src/golem/dashboard.html` — add board rendering JavaScript

**Validation command:** Visual review with mock data

---

## Task 6.4: Build Card Detail Modal

**Skills to load:** `frontend-design:frontend-design`

**Architecture notes:**

Click any ticket card → opens `#card-detail-modal`.

**Modal layout:**
```
#card-detail-modal
├── .modal-header
│   ├── Ticket ID + Title
│   └── Close button
├── .modal-body
│   ├── .detail-meta (Status, Stage, Agent, Worktree — key-value pairs)
│   ├── .detail-section "Instructions" (ticket context blueprint)
│   ├── .detail-section "Acceptance Criteria" (checkboxes, check off as QA passes)
│   ├── .detail-section "QA Checks" (command list)
│   └── .detail-section "Live Agent Activity" (streaming event log)
```

**Live Agent Activity section:**
- Connect SSE: `GET /api/repos/{repoId}/edicts/{edictId}/tickets/{ticketId}/events`
- Each event renders as: timestamp + action description
- Tool calls show tool name + brief args
- QA results show pass/fail with output preview
- Auto-scroll to bottom, pause auto-scroll if user scrolls up

**Acceptance criteria:**
- Parse from ticket's `context.acceptance` list
- Render as checkboxes
- Check off automatically when corresponding QA checks pass (match by QAResult events)

**Data loading:**
- On open: `GET /api/repos/{repoId}/edicts/{edictId}/tickets/{ticketId}` for static data
- Connect SSE for live activity
- On close: disconnect SSE

**Files to modify:**
- `src/golem/dashboard.html` — add modal HTML + JavaScript

**Validation command:** Visual review

---

## Task 6.5: Build Remaining Tabs (Plan, Diff, Cost, Logs)

**Skills to load:** `frontend-design:frontend-design`

> Read the existing `src/golem/ui_template.html` for Plan, Diff, Cost, and Logs tab implementations — port and adapt them.

**Architecture notes:**

These tabs are simpler and largely port from the existing UI:

**Plan tab (#pane-plan):**
- `fetchPlan(edictId)` → `GET /api/repos/{repoId}/edicts/{edictId}/plan`
- Render markdown as `<pre>` (same as existing)
- Show "Planning in progress..." if edict status is `planning`

**Diff tab (#pane-diff):**
- `fetchDiff(edictId)` → `GET /api/repos/{repoId}/edicts/{edictId}/diff`
- Render colored diff (reuse existing diff colorization from `ui_template.html`)
- Show "No changes yet" if empty

**Cost tab (#pane-cost):**
- `fetchCost(edictId)` → `GET /api/repos/{repoId}/edicts/{edictId}/cost`
- Render as table with columns: Role, Input Tokens, Output Tokens, Cost
- Show total at bottom
- Group by agent role (planner, tech_lead, junior_dev instances)

**Logs tab (#pane-logs):**
- Connect SSE: `GET /api/repos/{repoId}/edicts/{edictId}/logs`
- Render log lines with timestamp + verb + message (reuse existing log rendering)
- Guidance input bar at bottom (text input + Send button)
- Send guidance: `POST /api/repos/{repoId}/edicts/{edictId}/guidance`
- Auto-scroll, pause on user scroll-up

**Files to modify:**
- `src/golem/dashboard.html` — add tab content JavaScript

**Validation command:** Visual review

---

## Task 6.6: Wire Dashboard to Server

**Skills to load:** None

**Architecture notes:**

Update `server.py:create_app()`:
- Change `GET /` to serve `dashboard.html` instead of `ui_template.html`
- Load `dashboard.html` from the package directory (same pattern as current `_template_html`)
- Keep `ui_template.html` loadable via a legacy route `GET /legacy` for migration period

**Files to modify:**
- `src/golem/server.py` — update root route to serve dashboard.html

**Validation command:** Start server, open browser, verify dashboard loads

**Tests to write (`test_dashboard.py`):**
- GET / returns 200 with HTML content
- HTML contains expected structural elements (edict-list, pipeline-board, etc.)
- GET /legacy returns old UI (if kept)
