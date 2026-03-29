# Golem Edict System — Agent Pipeline Board Redesign

## Overview

Redesign Golem from a session-based spec executor into a persistent, ticket-driven agent pipeline service. Users post **Edicts** (freeform requests — bug reports, feature requests, full specs) into a repo, and Golem autonomously processes them through an agent pipeline: Lead Architect -> Tech Lead -> Junior Devs -> QA -> Done/PR.

The dashboard board shifts from status-based columns (Pending, In Progress, Review, Rework, Done, Failed) to **agent-based columns** where the columns represent the agents themselves and tickets flow through them visually. Each ticket card is a live observability window into the agent working on it.

**Approach: Hybrid** — new Edict model and dashboard UI, evolved server infrastructure, existing agent pipeline preserved.

---

## Terminology

| Term | Meaning |
|------|---------|
| **Edict** | A user-posted request (bug, feature, spec). Top-level work unit. Replaces "session" as user-facing concept |
| **Ticket** | A sub-task created by the Tech Lead from an Edict's plan. Internal work unit assigned to a Junior Dev |
| **Lead Architect** | Planner agent — researches, writes plans, enriches the Edict |
| **Tech Lead** | Pipeline manager — decomposes plan into Tickets, dispatches Junior Devs, reviews, merges |
| **Junior Dev** | Coding agent — executes a single Ticket in an isolated worktree. Replaces all "writer" references |
| **Pipeline Stage** | Which agent column a Ticket currently sits in (planner, tech_lead, junior_dev, qa, done, failed) |

---

## Data Model

### Edict

```
Edict:
  id:           "EDICT-001" (auto-incremented per repo)
  repo:         "F:\Tools\Projects\golem-cli"
  title:        "Add dark mode to settings"
  body:         freeform text (one-liner to full pasted spec)
  status:       pending | planning | in_progress | needs_attention | done | failed
  created_at:   timestamp
  updated_at:   timestamp
  pr_url:       null | "https://github.com/..."
  tickets:      [] (child ticket IDs, populated by tech lead)
  cost_usd:     0.0 (aggregated from all agents)
```

**Status flow:**
- `pending` — posted, waiting for a planner to pick it up
- `planning` — planner is researching and enriching it
- `in_progress` — tech lead has it, junior devs are working tickets
- `needs_attention` — self-healing exhausted, user intervention needed
- `done` — PR created, all tickets passed QA
- `failed` — unrecoverable failure

```
pending -> planning -> in_progress -> done
              |            |
              v            v
          needs_attention  failed
              |
              v
           (user re-queues or edits, back to planning/in_progress)
```

### Ticket (extended)

Existing Ticket model with additions:

```
New fields:
  edict_id:       "EDICT-001" (parent reference)
  pipeline_stage: "tech_lead" | "junior_dev" | "qa" (determines board column)
  agent_id:       "junior-dev-3" (which specific agent instance is handling this)
```

`pipeline_stage` determines which board column the card sits in, separate from `status` which tracks the ticket's own lifecycle.

### Repo Registry

```
Repo:
  id:       "golem-cli" (derived from directory name)
  path:     "F:\Tools\Projects\golem-cli"
  name:     "golem-cli" (display name)
  added_at: timestamp
```

Stored in `repos.json` in the Golem data directory.

---

## Dashboard Layout

### Top Bar

Repo tabs across the top. Clicking a tab switches the entire view to that repo's edicts and board. `+ Add Repo` opens a folder picker dialog (reuse `dialogs.py`). Aggregate cost shown on the right. Right-click a repo tab to remove it (doesn't delete the repo, just hides it from Golem).

```
┌──────────────────────────────────────────────────────────────────────┐
│  G O L E M    │ golem-cli │ my-saas-app │ + Add Repo │    $12.34   │
└──────────────────────────────────────────────────────────────────────┘
```

### Left Sidebar — Edict List

Collapsible sections: To Do, In Progress, Completed. Each section can be collapsed/expanded independently by clicking the section header. `+ NEW EDICT` button at top opens creation modal.

```
┌─────────────────────┐
│  + NEW EDICT        │
├─────────────────────┤
│ v TO DO (2)         │
│   EDICT-004         │
│   Add export to CSV │
│                     │
│   EDICT-003         │
│   Add dark mode     │
│                     │
│ v IN PROGRESS (1)   │
│   EDICT-002         │
│   Fix Safari bug    │
│   3/5 tickets       │
│                     │
│ > COMPLETED (4)     │  <-- collapsed
└─────────────────────┘
```

Edicts in "needs_attention" status show under In Progress with a visual indicator (e.g. warning icon or color).

### Main Area — Agent Pipeline Board

When an edict is selected, the right side shows the pipeline board. Columns represent agents, not statuses. Cards are tickets (sub-tasks) created by the Tech Lead.

```
┌───────────┐ ┌───────────┐ ┌───────────┐ ┌───────────┐ ┌───────────────┐
│  Planner  │ │ Tech Lead │ │Junior Devs│ │    QA     │ │  Done / Fail  │
│     0     │ │     1     │ │     2     │ │     1     │ │      1        │
├───────────┤ ├───────────┤ ├───────────┤ ├───────────┤ ├───────────────┤
│           │ │ ┌───────┐ │ │ ┌───────┐ │ │ ┌───────┐ │ │ ┌───────────┐ │
│           │ │ │T-003  │ │ │ │T-002  │ │ │ │T-004  │ │ │ │T-001      │ │
│           │ │ │Update │ │ │ │Connect│ │ │ │Write  │ │ │ │Setup DB   │ │
│           │ │ │docs   │ │ │ │API    │ │ │ │tests  │ │ │ │  DONE     │ │
│           │ │ │queued │ │ │ │JD-1   │ │ │ │JD-2   │ │ │ └───────────┘ │
│           │ │ └───────┘ │ │ │active │ │ │ │review │ │ │               │
│           │ │           │ │ └───────┘ │ │ └───────┘ │ │               │
│           │ │           │ │ ┌───────┐ │ │           │ │               │
│           │ │           │ │ │T-005  │ │ │           │ │               │
│           │ │           │ │ │Migrate│ │ │           │ │               │
│           │ │           │ │ │schema │ │ │           │ │               │
│           │ │           │ │ │JD-3   │ │ │           │ │               │
│           │ │           │ │ │active │ │ │           │ │               │
│           │ │           │ │ └───────┘ │ │           │ │               │
└───────────┘ └───────────┘ └───────────┘ └───────────┘ └───────────────┘
```

- Column headers show agent name + count of cards
- Cards show: ticket ID, title (2-line clamp), assigned agent instance (JD-1, JD-2...), status
- Cards animate between columns as tickets progress through the pipeline
- Planner column shows activity indicator while researching, empties when done (planner enriches the edict, doesn't produce ticket cards)
- The board has Pause and Kill buttons for the edict at the top right

### Card Detail Modal

Click any ticket card to open a detail modal with live agent observability:

```
┌──────────────────────────────────────────────────────┐
│  T-002: Connect API endpoint              [x close]  │
├──────────────────────────────────────────────────────┤
│  Status:    active                                    │
│  Stage:     Junior Dev (JD-1)                         │
│  Worktree:  golem/edict-002/t-002                     │
│                                                       │
│  -- Instructions --                                   │
│  Connect the settings API to the new dark mode        │
│  toggle. Use the existing /api/settings PATCH         │
│  endpoint. Add the `theme` field to the schema.       │
│                                                       │
│  -- Acceptance Criteria --                            │
│  [x] PATCH /api/settings accepts theme field          │
│  [ ] Frontend toggle calls the endpoint               │
│  [ ] Tests cover light/dark round-trip                 │
│                                                       │
│  -- QA Checks --                                      │
│  ruff check src/                                      │
│  pytest tests/test_settings.py                        │
│                                                       │
│  -- Live Agent Activity --                            │
│  14:32:01  Reading src/api/settings.py                │
│  14:32:03  Edit: added theme field to SettingsSchema  │
│  14:32:05  Reading tests/test_settings.py             │
│  14:32:08  Write: test_dark_mode_toggle               │
│  14:32:10  Tool: run_qa -> running ruff...            │
│  14:32:12  ruff: PASSED                               │
│  14:32:13  Tool: run_qa -> running pytest...          │
└──────────────────────────────────────────────────────┘
```

- Static info (instructions, acceptance criteria, QA checks) from the ticket
- Live agent activity streams in real-time at the bottom
- Acceptance criteria check off as QA passes them

### New Edict Modal

Minimal form — title and freeform description. Repo is auto-selected from the active tab.

```
┌──────────────────────────────────────────────────────┐
│  New Edict                                [x close]   │
├──────────────────────────────────────────────────────┤
│  Title:    [                                     ]    │
│                                                       │
│  Description:                                         │
│  ┌──────────────────────────────────────────────┐    │
│  │                                              │    │
│  │  (paste spec, write a bug report, whatever)  │    │
│  │                                              │    │
│  └──────────────────────────────────────────────┘    │
│                                                       │
│              [POST EDICT]                             │
└──────────────────────────────────────────────────────┘
```

---

## Agent Operational Blueprints

### Lead Architect (Planner)

| Aspect | Detail |
|--------|--------|
| **Persona** | Senior architect who owns research and planning |
| **Input** | Raw Edict (user's freeform text) |
| **Output** | Enriched Edict with plan files and reference docs on disk |
| **Sub-agents** | Explorer (Haiku — codebase structure), Researcher (Sonnet — deep analysis), dispatched in parallel |
| **Documentation** | Context7 MCP for library/framework docs. Auto-heal: if MCP unavailable, fall back to web search |
| **Writes to disk** | `plans/overview.md`, `plans/task-*.md`, `references/*.md` (curated syntax/API docs for downstream agents) |
| **Key behavior** | Reference files are specifically curated so Junior Devs have correct, latest syntax — not just "go look it up" |
| **Completion** | Marks edict as `planned`, all artifacts on disk |

### Tech Lead (Pipeline Manager)

| Aspect | Detail |
|--------|--------|
| **Persona** | Pipeline manager who owns ticket decomposition and flow |
| **Input** | Enriched Edict + plan files + references |
| **Output** | Tickets with dependencies, worktrees, dispatched Junior Devs |
| **Responsibilities** | Create tickets, set dependency order (DAG), create worktrees, dispatch Junior Devs in parallel waves, review results, request rework, merge worktrees, create PR |
| **Key behavior** | Doesn't write code — manages the pipeline. Reviews Junior Dev output, runs QA, handles rework loops |
| **Self-healing** | If a Junior Dev stalls or fails, escalates the ticket prompt and re-dispatches. After N failures, marks ticket `needs_attention` for user |
| **Completion** | All tickets pass QA, worktrees merged, PR created, edict marked `done` |

### Junior Dev (formerly "Writer")

| Aspect | Detail |
|--------|--------|
| **Persona** | Developer who executes a single ticket |
| **Input** | One Ticket with full context + reference docs from planner |
| **Output** | Code changes in worktree, updated ticket status |
| **Sub-agents** | Fullstack Developer sub-agent (Sonnet) for parallel file writing when ticket touches multiple files |
| **Key behavior** | ALWAYS reads reference docs and research before writing. Uses curated syntax/API docs, not memory |
| **QA** | Self-runs QA before marking ticket as review. Fix-retry loop up to 3 attempts before escalating |
| **Scope** | Worktree only. No files outside ticket scope. No worktree deletion |

### QA (Deterministic)

| Aspect | Detail |
|--------|--------|
| **No AI prompt** | Subprocess-based checks, not an AI agent |
| **Infra checks** | ruff, mypy, tsc, cargo test (auto-detected from project) |
| **Spec checks** | Commands from ticket's `qa_checks` field |
| **Invoked by** | Tech Lead after Junior Dev marks ticket as review |

### Prompt Engineering

All agent system prompts will be crafted during implementation using the `agent-prompting` skill:
- Identity-first persona (not rule lists)
- Principles over procedures
- Tool usage patterns baked into identity
- Auto-heal fallback behaviors as natural problem-solving
- Reference doc consumption as a core habit

---

## Dashboard Tabs (per Edict)

5 tabs when viewing an edict, reduced from 8:

| Tab | Content |
|-----|---------|
| **Board** | Default. Agent pipeline board (Planner -> Tech Lead -> Junior Devs -> QA -> Done/Fail) |
| **Plan** | Planner's output — `overview.md` + task files. Available once planning completes |
| **Diff** | Aggregate git diff for the whole edict (all merged worktrees) |
| **Cost** | Cost breakdown by agent role — planner, tech lead, each junior dev instance |
| **Logs** | Progress log stream with guidance input bar at bottom |

**Absorbed into card modals:** Tickets table (board IS the tickets), Observe (per-card live agent activity), Preflight (runs automatically, brief status bar above board).

---

## API Endpoints

### Repo Management
```
GET    /api/repos                    -> list registered repos
POST   /api/repos                    -> { path }  register a repo
DELETE /api/repos/{repo_id}          -> remove repo from Golem (doesn't delete files)
```

### Edict CRUD
```
GET    /api/repos/{repo_id}/edicts            -> list edicts for repo
POST   /api/repos/{repo_id}/edicts            -> { title, body }  create edict
GET    /api/repos/{repo_id}/edicts/{id}       -> edict detail
PATCH  /api/repos/{repo_id}/edicts/{id}       -> update (edit body, re-queue)
DELETE /api/repos/{repo_id}/edicts/{id}       -> cancel/remove edict
POST   /api/repos/{repo_id}/edicts/{id}/start -> kick off pipeline
POST   /api/repos/{repo_id}/edicts/{id}/pause -> pause execution
POST   /api/repos/{repo_id}/edicts/{id}/resume -> resume
POST   /api/repos/{repo_id}/edicts/{id}/kill  -> kill execution
POST   /api/repos/{repo_id}/edicts/{id}/guidance -> send operator guidance
```

### Tickets (scoped under edict)
```
GET    /api/repos/{repo_id}/edicts/{id}/tickets          -> all tickets
GET    /api/repos/{repo_id}/edicts/{id}/tickets/{tid}    -> ticket detail
GET    /api/repos/{repo_id}/edicts/{id}/tickets/{tid}/events -> live event stream for specific ticket/agent
```

### Pipeline Board
```
GET    /api/repos/{repo_id}/edicts/{id}/board -> tickets grouped by pipeline_stage
```

### Observability
```
GET    /api/repos/{repo_id}/edicts/{id}/observe  -> SSE event stream
GET    /api/repos/{repo_id}/edicts/{id}/logs     -> SSE log stream
GET    /api/repos/{repo_id}/edicts/{id}/cost     -> cost breakdown
GET    /api/repos/{repo_id}/edicts/{id}/diff     -> git diff
GET    /api/repos/{repo_id}/edicts/{id}/plan     -> plan files
```

### Server
```
GET    /api/server/status -> health + active repo count + edict counts
```

---

## Codebase Mapping

### Stays As-Is
- `planner.py` — core pipeline logic, entry point changes from session to edict
- `tech_lead.py` — orchestration logic, minimal changes
- `qa.py` — deterministic, no changes
- `orchestrator.py` — DAG + wave execution
- `recovery.py` — adds `needs_attention` escalation path
- `supervisor.py` — stall detection, circuit breakers
- `security.py` — bash validation, secret scanning
- `parallel.py` — parallel sub-agent executor
- `worktree.py` — git worktree operations
- `merge_strategies.py` — deterministic merge strategies
- `tool_registry.py` — per-agent tool filtering
- `tools.py` — MCP tools
- `dialogs.py` — reuse for Add Repo folder picker

### New Files
- `edict.py` — Edict data model, EdictStore, status transitions
- `repos.py` — Repo registry (add/remove/list, persist to `repos.json`)
- `pipeline.py` — Pipeline coordinator (Edict through full agent pipeline, replaces `run_session()`)
- `dashboard.html` — New UI (agent pipeline board, edict sidebar, repo tabs, card modals)

### Refactored
- `server.py` — `SessionManager` -> `EdictManager`, repo management endpoints, pipeline-stage-grouped board endpoints. Keep SSE, event bus, pause/resume/kill
- `session.py` — `create_session_dir()` -> `create_edict_dir()`, dirs still exist for isolation
- `cli.py` — new commands (`golem edict create`, `golem repo add`, etc.), old session commands deprecated
- `client.py` — HTTP client updated for new endpoints
- `merge.py` — merge queue keyed by edict_id
- `events.py` — add EdictCreated, EdictUpdated, EdictNeedsAttention event types
- `tickets.py` — add `edict_id`, `pipeline_stage`, `agent_id` fields
- `config.py` — add repo registry config, edict-related settings
- `prompts/*.md` — full rewrite using agent-prompting skill

### Renamed
- `writer.py` -> `junior_dev.py`
- `prompts/worker.md` -> `prompts/junior_dev.md`
- `prompts/worker_rework.md` -> `prompts/junior_dev_rework.md`
- All "writer" references in codebase -> "junior_dev"

### Deleted (after new system is stable)
- `ui_template.html` — replaced by `dashboard.html`
- `ui.py` — legacy single-session dashboard
- `tasks.py` — v1 legacy, already unused

### Test Impact
- `test_writer.py` -> `test_junior_dev.py`
- `test_server.py` — significant updates for edict endpoints
- `test_ui.py` -> `test_dashboard.py`
- New: `test_edict.py`, `test_repos.py`, `test_pipeline.py`
- Most other test files: minor updates (field renames, edict_id)

---

## Design Constraints

- **1 Edict = 1 PR** — no batching of edicts into shared PRs
- **Self-heal first, escalate if stuck** — configurable retry count before `needs_attention`
- **Real-time observability** — card modals stream live agent activity via SSE
- **No Jira terminology** — no epics, stories, or sprints. Edicts and Tickets only
- **Windows compatible** — all existing Windows gotchas apply (encoding, no emoji, PATH refresh)
- **Existing agent pipeline preserved** — planner, tech lead, junior dev, QA, orchestrator, recovery, supervisor all keep working
