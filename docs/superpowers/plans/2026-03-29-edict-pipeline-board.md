# Edict Pipeline Board — Implementation Plan

## Goal
Redesign Golem from a session-based spec executor into a persistent, ticket-driven agent pipeline service. Users post Edicts (freeform requests) and Golem processes them through an agent pipeline: Lead Architect → Tech Lead → Junior Devs → QA → Done/PR. The dashboard shifts from status-based columns to agent-based columns with live observability per ticket card.

## Architecture Overview

```
User → Edict (title + body) → Pipeline Coordinator
                                    │
                                    ├── Lead Architect (Planner)
                                    │     └── Explorer + Researcher sub-agents
                                    │     └── Output: plans/, references/, enriched Edict
                                    │
                                    ├── Tech Lead (Pipeline Manager)
                                    │     └── Creates Tickets from plan
                                    │     └── Dispatches Junior Devs in waves
                                    │     └── Reviews, reworks, merges
                                    │
                                    ├── Junior Devs (per ticket, parallel)
                                    │     └── Isolated worktrees
                                    │     └── Self-QA loop
                                    │
                                    ├── QA (deterministic subprocess)
                                    │
                                    └── Done → PR

Data flow:
  RepoRegistry (repos.json) → EdictStore (EDICT-NNN.json) → TicketStore (TICKET-NNN.json)
                                    │
                              PipelineCoordinator
                                    │
                              EventBus (SSE → Dashboard)
```

**Key entities:**
- **Repo** — registered project directory, persisted in `~/.golem/repos.json`
- **Edict** — user-posted request, top-level work unit, replaces "session" as user-facing concept
- **Ticket** — sub-task created by Tech Lead, extended with `edict_id`, `pipeline_stage`, `agent_id`
- **PipelineCoordinator** — orchestrates Edict through the full agent pipeline, replaces inline `_run_async()` in CLI

## Tech Stack
- Python 3.12+, asyncio, uv
- FastAPI + uvicorn (server)
- Claude Agent SDK (agent sessions)
- Self-contained HTML/CSS/JS (dashboard, no frameworks)
- Rich (CLI output)
- httpx (HTTP client)

## Phase Dependency Graph
```
Phase 1: Data Models + Events
    │
    ├──→ Phase 2: File Rename (writer → junior_dev)  [parallel with Phase 3]
    │
    └──→ Phase 3: Pipeline Coordinator                [parallel with Phase 2]
              │
              └──→ Phase 4: Server Refactor
                        │
                        ├──→ Phase 5: CLI + Client     [parallel with Phase 6]
                        │
                        └──→ Phase 6: Dashboard UI     [parallel with Phase 5]
                                  │
                                  └──→ Phase 7: Prompts + Integration + Cleanup
```

## Parallel Opportunities
- **Phases 2 & 3** are independent (file rename vs pipeline coordinator) — can execute in parallel
- **Phases 5 & 6** are independent (CLI vs Dashboard) — can execute in parallel
- Within Phase 1: Tasks 1.1-1.5 touch different files and can run in parallel
- Within Phase 4: Tasks 4.1-4.3 are additive endpoint additions, somewhat parallelizable
- Within Phase 6: Tasks 6.1-6.5 are sequential (HTML shell → JS interactivity)

---

### Phase 1: Data Models & Events
- **File:** phase-1-data-models-events.md
- **Tasks:** 5
- **Skills:** `superpowers:test-driven-development`
- **Creates:** `edict.py`, `repos.py`, `test_edict.py`, `test_repos.py`
- **Modifies:** `tickets.py`, `events.py`, `config.py`, `test_tickets.py`, `test_events.py`, `test_recovery.py`, `test_parallel.py`
- **Reference docs:** None
- **Ordering notes:** All 5 tasks touch different files — fully parallelizable

### Phase 2: File Rename (writer → junior_dev)
- **File:** phase-2-file-rename.md
- **Tasks:** 4
- **Skills:** `claude-md-management:revise-claude-md` (Task 2.4 only)
- **Creates:** None
- **Modifies:** Renames `writer.py` → `junior_dev.py`, `test_writer.py` → `test_junior_dev.py`, updates imports across ~10 files, updates CLAUDE.md
- **Reference docs:** None
- **Ordering notes:** Tasks 2.1-2.3 are sequential (rename file → update imports → verify prompts). Task 2.4 (CLAUDE.md) can run in parallel with 2.3

### Phase 3: Pipeline Coordinator
- **File:** phase-3-pipeline-coordinator.md
- **Tasks:** 3
- **Skills:** `superpowers:test-driven-development`
- **Creates:** `pipeline.py`, `test_pipeline.py`
- **Modifies:** `session.py`, `recovery.py`, `test_session.py`
- **Reference docs:** None
- **Ordering notes:** Task 3.1 (pipeline coordinator) depends on Phase 1 data models. Tasks 3.2 and 3.3 can run in parallel with 3.1

### Phase 4: Server Refactor
- **File:** phase-4-server-refactor.md
- **Tasks:** 5
- **Skills:** `superpowers:test-driven-development`
- **Creates:** None
- **Modifies:** `server.py`, `merge.py`, `test_server.py`, `test_merge.py`
- **Reference docs:** None
- **Ordering notes:** Task 4.1 (repos) is independent. Tasks 4.2-4.3 are sequential (CRUD before board endpoints). Task 4.4 (merge) is independent. Task 4.5 (status) depends on 4.2

### Phase 5: CLI + Client
- **File:** phase-5-cli-client.md
- **Tasks:** 3
- **Skills:** `superpowers:test-driven-development`
- **Creates:** None
- **Modifies:** `cli.py`, `client.py`, `test_cli.py`, `test_client.py`
- **Reference docs:** None
- **Ordering notes:** Task 5.1 (client methods) must come before 5.2 (CLI commands). Task 5.3 (run bridge) depends on 5.2

### Phase 6: Dashboard UI
- **File:** phase-6-dashboard-ui.md
- **Tasks:** 6
- **Skills:** `frontend-design:frontend-design`
- **Creates:** `dashboard.html`, `test_dashboard.py`
- **Modifies:** `server.py` (root route)
- **Reference docs:** None
- **Ordering notes:** Tasks are sequential: 6.1 (HTML shell) → 6.2 (repos+sidebar JS) → 6.3 (board JS) → 6.4 (card modal) → 6.5 (remaining tabs) → 6.6 (wire to server)

### Phase 7: Prompts, Integration & Cleanup
- **File:** phase-7-prompts-integration-cleanup.md
- **Tasks:** 6
- **Skills:** `agent-prompting`, `superpowers:brainstorming`, `claude-md-management:revise-claude-md`, `superpowers:verification-before-completion`
- **Creates:** `test_dashboard.py` (if not in Phase 6)
- **Modifies:** All prompt files, CLAUDE.md
- **Deletes:** `ui.py`, `ui_template.html`, `tasks.py`, `test_ui.py`, `test_tasks.py`
- **Reference docs:** None
- **Ordering notes:** Tasks 7.1-7.3 (prompts) can run in parallel. Task 7.4 (delete legacy) depends on Phase 6 being complete. Task 7.5 (docs) and 7.6 (integration test) are sequential at the end

---

## Spec File
`docs/superpowers/specs/2026-03-29-edict-pipeline-board-design.md`

## Reference Docs Produced
None — this is entirely internal architecture work using existing dependencies.
