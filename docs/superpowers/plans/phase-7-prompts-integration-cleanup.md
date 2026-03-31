# Phase 7: Prompts, Integration & Cleanup

## Gotchas
- Agent prompts must use `agent-prompting` skill — identity-first persona, principles over procedures
- Prompt placeholders (`{golem_dir}`, `{spec_content}`, etc.) are substituted by Python code — don't break the template interface
- The planner prompt has specific completion signals (`=== PLANNER: DONE ===`) that `run_planner()` may check for — preserve or update signal detection code
- MCP tool names in prompts must use full prefixed names: `mcp__golem__create_ticket`, not `create_ticket`
- Removing `ui.py` and `tasks.py` will break any existing imports — search for all references first
- `test_ui.py` tests the legacy UI endpoints — either delete or port relevant tests to `test_dashboard.py`
- `tasks.py` is "v1 legacy, already unused" per CLAUDE.md — safe to delete, but verify no remaining imports
- Event count may have changed by this point — verify current count before updating test assertions

## Files
```
src/golem/
├── prompts/planner.md           # MODIFY — full rewrite with agent-prompting skill
├── prompts/tech_lead.md         # MODIFY — full rewrite
├── prompts/junior_dev.md        # MODIFY — full rewrite
├── prompts/junior_dev_rework.md # MODIFY — full rewrite
├── ui.py                        # DELETE — legacy single-session dashboard
├── ui_template.html             # DELETE — replaced by dashboard.html
├── tasks.py                     # DELETE — v1 legacy, unused
tests/
├── test_ui.py                   # DELETE or RENAME → test_dashboard.py (port relevant tests)
├── test_tasks.py                # DELETE — tests for deleted module
├── test_dashboard.py            # CREATE (if not created in Phase 6) — dashboard endpoint tests
```

---

## Task 7.1: Rewrite Planner System Prompt

**Skills to load:** `agent-prompting`, `superpowers:brainstorming`

**Architecture notes:**

The planner prompt (`prompts/planner.md`) must be rewritten with identity-first persona as the **Lead Architect**.

Key changes from current prompt:
- Replace "session" references with "edict" terminology
- The planner now receives an Edict (title + body) instead of a raw spec file path
- Context7 MCP for documentation (auto-heal: fall back to web search if unavailable)
- Sub-agent dispatch patterns remain the same (Explorer + Researcher)
- Must still write `plans/overview.md`, `plans/task-NNN.md`, `references/*.md`
- Must still call `mcp__golem__create_ticket` at the end
- Completion signals must be preserved (or updated if pipeline.py changes detection logic)

Preserve template placeholders:
- `{spec_content}` → rename to `{edict_body}` (update `planner.py` substitution)
- `{project_context}`, `{golem_dir}`, `{infrastructure_checks}`, `{skip_research_instruction}` — keep as-is

**Files to modify:**
- `src/golem/prompts/planner.md` — full rewrite
- `src/golem/planner.py` — update placeholder name if changed

**Validation command:** `uv run pytest tests/test_planner.py -v`

---

## Task 7.2: Rewrite Tech Lead System Prompt

**Skills to load:** `agent-prompting`, `superpowers:brainstorming`

**Architecture notes:**

The Tech Lead prompt (`prompts/tech_lead.md`) must be rewritten with identity-first persona as the **Pipeline Manager**.

Key changes:
- Replace "session" with "edict" terminology
- Tech Lead now sets `pipeline_stage` on tickets as they move through the pipeline
- When creating tickets: set `pipeline_stage = "tech_lead"` initially
- When dispatching to Junior Devs: update `pipeline_stage = "junior_dev"` and set `agent_id`
- When sending to QA: update `pipeline_stage = "qa"`
- On completion: update `pipeline_stage = "done"` or `"failed"`
- All MCP tool calls for ticket updates should include pipeline_stage changes
- Reference doc consumption habit — Tech Lead reads planner's references before dispatching

Preserve template placeholders:
- `{golem_dir}`, `{project_root}`, `{max_writer_retries}`, `{qa_depth}`, `{max_parallel_writers}`, `{critique_content}` — keep

**Files to modify:**
- `src/golem/prompts/tech_lead.md` — full rewrite

**Validation command:** `uv run pytest tests/test_tech_lead.py -v`

---

## Task 7.3: Rewrite Junior Dev System Prompt

**Skills to load:** `agent-prompting`, `superpowers:brainstorming`

**Architecture notes:**

The Junior Dev prompt (`prompts/junior_dev.md` and `junior_dev_rework.md`) must be rewritten with identity-first persona as a **focused implementer**.

Key changes:
- Replace any remaining "writer" references with "Junior Dev"
- Identity: "You are a Junior Developer on the Golem team" — not a rule-follower, but someone with a specific role and habits
- Reference doc consumption as a core habit (reads references BEFORE touching code)
- Fullstack Developer sub-agent for parallel file writing (new capability mentioned in spec)
- Self-critique as a natural quality instinct, not a checklist
- QA loop as professional pride, not a requirement

Preserve template placeholders:
- All existing placeholders: `{ticket_context}`, `{plan_section}`, `{file_contents}`, `{references}`, `{blueprint}`, `{acceptance}`, `{qa_checks}`, `{parallelism_hints}`, `{iteration}`, `{rework_context}`, `{worktree_isolation_warning}`

**Files to modify:**
- `src/golem/prompts/junior_dev.md` — full rewrite
- `src/golem/prompts/junior_dev_rework.md` — full rewrite

**Validation command:** `uv run pytest tests/test_junior_dev.py -v`

---

## Task 7.4: Delete Legacy Files

**Skills to load:** None

**Architecture notes:**

Before deleting, verify no remaining imports:
1. `rg "from golem.ui import\|import golem.ui\|golem\.ui\." src/ tests/` — must return only `test_ui.py` and `server.py` (the legacy route)
2. `rg "from golem.tasks import\|import golem.tasks\|golem\.tasks\." src/ tests/` — must return only `test_tasks.py`
3. `rg "ui_template" src/ tests/` — must return only `server.py` and `ui.py`

Delete:
- `src/golem/ui.py`
- `src/golem/ui_template.html`
- `src/golem/tasks.py`
- `tests/test_ui.py`
- `tests/test_tasks.py`

Update `server.py`: remove the legacy `/legacy` route if it references `ui_template.html`. Remove `ui_template.html` loading code from `create_app()`.

Update `__init__.py` if it re-exports anything from deleted modules.

**Files to delete:**
- `src/golem/ui.py`
- `src/golem/ui_template.html`
- `src/golem/tasks.py`
- `tests/test_ui.py`
- `tests/test_tasks.py`

**Validation command:** `uv run pytest -x --tb=short` (full suite — import errors surface immediately)

---

## Task 7.5: Update CLAUDE.md and Documentation

**Skills to load:** `claude-md-management:revise-claude-md`

**Architecture notes:**

Update `CLAUDE.md` with:
- New project structure reflecting all changes (edict.py, repos.py, pipeline.py, dashboard.html, junior_dev.py)
- Remove deleted files (ui.py, ui_template.html, tasks.py)
- Update Quick Start with new edict commands
- Add Edict system to Key Design Decisions
- Update test file listing
- Update version history with v0.5.0 entry
- Add any new gotchas discovered during implementation

Update test count after full suite passes.

**Files to modify:**
- `CLAUDE.md`

**Validation command:** Visual review

---

## Task 7.6: Integration Testing

**Skills to load:** `superpowers:verification-before-completion`

**Architecture notes:**

Run the full test suite and verify:
1. `uv run pytest` — all tests pass
2. `uv run ruff check src/` — no lint errors in new/modified files
3. `uv run ruff format --check src/` — formatting is correct
4. Manual smoke test:
   - `uv run golem server start`
   - Open browser to `http://localhost:7665`
   - Verify dashboard loads with repo tabs
   - Add a repo
   - Create an edict
   - Verify board renders (with mock/no data initially)
   - `uv run golem edict list` — verify CLI works
   - `uv run golem server stop`

**Validation command:** `uv run pytest && uv run ruff check src/ && uv run ruff format --check src/`
