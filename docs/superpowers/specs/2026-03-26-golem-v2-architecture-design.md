# Golem v2 — Ticket-Driven Agent Architecture

## Overview

Replace Golem's mechanical executor loop with a ticket-driven agent hierarchy: Planner (ephemeral, researches + writes plans) → Tech Lead (persistent, orchestrates + reviews) → Writer+Validator pairs (long-lived, paired, share context). Communication happens through structured JSON tickets. Validation is deterministic-first via a `run_qa()` tool.

See the full design rationale in the brainstorming session (2026-03-26). This spec is the implementation plan.

## Existing Codebase (v1 files to modify or replace)

```
src/golem/
  tasks.py        — TasksFile, Group, Task dataclasses (REPLACE ticket system)
  executor.py     — execute_group, execute_all_groups (REPLACE with Tech Lead)
  planner.py      — run_planner (REFACTOR for sub-agent architecture)
  worker.py       — run_worker (REPLACE with Writer pair spawning)
  validator.py    — run_validation, run_ai_validator (REPLACE with run_qa tool)
  config.py       — GolemConfig (MODIFY — add v2 fields)
  cli.py          — CLI commands (MODIFY — wire v2 pipeline)
  worktree.py     — git operations (KEEP — add merge tool wrappers)
  progress.py     — ProgressLogger (MODIFY — add ticket events)
  prompts/
    planner.md    — planner prompt (REWRITE for sub-agent spawning)
    worker.md     — worker prompt (REWRITE as Writer prompt)
    validator.md  — validator prompt (REMOVE — validation is a tool now)
    integration_reviewer.md — (REMOVE — Tech Lead does this inline)
```

## Implementation Epics

### Epic 0: Environment Setup

**0.1** Install Python dependencies:
```bash
uv sync
```

**0.2** Verify tools on PATH:
```bash
python --version    # 3.12+
rg --version        # ripgrep
git --version
```

**0.3** Run existing v1 tests to establish baseline:
```bash
uv run pytest tests/ -v
```
All 86 tests must pass before any changes.

**Acceptance criteria:**
- `uv sync` completes without error
- `python --version` reports 3.12+
- `rg --version` exits 0 (ripgrep on PATH)
- `git --version` exits 0 (git on PATH)
- `uv run pytest` reports 86 passed, 0 failed

**Git checkpoint:** `git add -A && git commit -m "epic-0: environment setup verified"`

---

### Epic 1: Ticket System Data Layer

Build the ticket system — the communication backbone for all agents.

**1.1** Create `src/golem/tickets.py` with the core data model:

- `Ticket` dataclass with fields: `id`, `type` (task|review|merge|qa|ux-test), `title`, `status` (pending|in_progress|qa_passed|ready_for_review|needs_work|approved|done|blocked), `priority`, `created_by`, `assigned_to`, `context` (dict), `history` (list of `TicketEvent`)
- `TicketEvent` dataclass: `ts` (ISO timestamp), `agent`, `action`, `note`, `attachments` (list of str)
- `TicketContext` dataclass: `plan_file` (str), `files` (dict[str, str] — filename→contents), `references` (list[str] — paths), `blueprint` (str), `acceptance` (list[str]), `qa_checks` (list[str]), `parallelism_hints` (list[str])
- `TicketStore` class: manages `.golem/tickets/` directory
  - `create(ticket) → ticket_id` — writes `TICKET-NNN.json`, auto-increments ID
  - `read(ticket_id) → Ticket` — reads from disk
  - `update(ticket_id, status, note, attachments)` — appends to history, updates status
  - `list_tickets(status_filter, assigned_to_filter) → list[Ticket]`
  - All reads/writes use `encoding="utf-8"` (Windows compat)
  - Thread-safe via `asyncio.Lock` (same pattern as v1 `tasks.py`)

**Acceptance criteria:**
- `Ticket` dataclass has all fields: id, type, title, status, priority, created_by, assigned_to, context, history
- `TicketStore.create()` writes a JSON file to `.golem/tickets/`
- `TicketStore.read()` deserializes a ticket from disk
- `TicketStore.update()` appends a `TicketEvent` to history and persists
- `TicketStore.list_tickets()` filters by status and/or assignee
- All file I/O uses `encoding="utf-8"`
- Round-trip test: create → read → update → read returns correct state

**Validation commands:**
```bash
rg -q "class Ticket" src/golem/tickets.py
rg -q "class TicketEvent" src/golem/tickets.py
rg -q "class TicketContext" src/golem/tickets.py
rg -q "class TicketStore" src/golem/tickets.py
rg -q "encoding.*utf-8" src/golem/tickets.py
uv run pytest tests/test_tickets.py -v
```

**1.2** Create `tests/test_tickets.py` with comprehensive tests:

- `test_create_ticket_writes_json` — create ticket, verify file exists on disk
- `test_read_ticket_roundtrip` — create → read → fields match
- `test_update_appends_history` — create → update status → read → history has 2 events
- `test_list_tickets_filters_by_status` — create 3 tickets with different statuses, filter returns correct subset
- `test_list_tickets_filters_by_assignee` — filter by assigned_to field
- `test_ticket_id_auto_increments` — create 3 tickets → IDs are TICKET-001, TICKET-002, TICKET-003
- `test_context_preserves_file_contents` — create ticket with `context.files` dict containing file contents, read back, contents match exactly
- `test_concurrent_creates_no_corruption` — 5 concurrent `asyncio.gather` creates, all succeed with unique IDs

**Acceptance criteria:**
- All 8 tests pass
- Tests use `tempfile.TemporaryDirectory` for isolation (no disk state leaks)
- Tests follow existing patterns in `tests/test_tasks.py`

**Validation commands:**
```bash
uv run pytest tests/test_tickets.py -v
```

**Git checkpoint:** `git add -A && git commit -m "epic-1: ticket system data layer with full test coverage"`

---

### Epic 2: QA Tool

Build the deterministic QA tool that replaces AI-based validation.

**2.1** Create `src/golem/qa.py` with the `run_qa()` function:

- `QAResult` dataclass: `passed` (bool), `checks` (list of `QACheck`), `summary` (str)
- `QACheck` dataclass: `type` (syntax|lint|acceptance|test), `tool` (str — the command), `passed` (bool), `stdout` (str), `stderr` (str)
- `run_qa(worktree_path: str, checks: list[str], infrastructure_checks: list[str]) -> QAResult`
  - Runs infrastructure checks first (always-on, from config auto-detection)
  - Then runs spec-defined checks
  - Each check is a subprocess: `subprocess.run(cmd, shell=True, cwd=worktree_path, capture_output=True, text=True, encoding="utf-8")`
  - Uses `_subprocess_env()` from existing `validator.py` for Windows PATH fix
  - Uses `_normalize_cmd()` from existing `validator.py` for Windows quote fix
  - Returns structured `QAResult` — not a pass/fail boolean, the full structured report
  - `summary` field is auto-generated: "N/M checks passed. Failed: [list of failed check commands]"
- `run_autofix(worktree_path: str, infrastructure_checks: list[str]) -> None`
  - If "ruff" in checks → `ruff check --fix .` + `ruff format .`
  - If "prettier" in checks → `npx prettier --write .`
  - Runs before counting a retry
- `detect_infrastructure_checks(project_root: Path) -> list[str]`
  - Move existing `_detect_infrastructure_checks` from `cli.py` into this module
  - Detects ruff, npm lint, tsc from project files

**Acceptance criteria:**
- `run_qa()` returns a `QAResult` with structured per-check results
- Infrastructure checks run before spec checks
- Each check includes stdout/stderr capture
- `summary` field is human-readable
- `run_autofix()` runs ruff fix when ruff is in the check list
- `detect_infrastructure_checks()` detects ruff from pyproject.toml
- Reuses `_subprocess_env()` and `_normalize_cmd()` from validator.py (import, don't duplicate)
- If a build-tool check (`tsc`, `vite build`, `bun build`) exits with code 127 (tool not on PATH — common in WSL), `run_qa()` records a `QACheck` with `passed=False` and `stderr="build tool not found on PATH"`, then appends a fallback code-inspection check (e.g., `rg -q "compilerOptions" tsconfig.json` for tsc) marked as `type="fallback-inspection"`

**Validation commands:**
```bash
rg -q "class QAResult" src/golem/qa.py
rg -q "class QACheck" src/golem/qa.py
rg -q "def run_qa" src/golem/qa.py
rg -q "def run_autofix" src/golem/qa.py
rg -q "def detect_infrastructure_checks" src/golem/qa.py
rg -q "_subprocess_env" src/golem/qa.py
uv run pytest tests/test_qa.py -v
```

**2.2** Create `tests/test_qa.py`:

- `test_run_qa_all_pass` — pass checks that succeed (e.g., `rg -q "def" some_file.py`), verify `passed=True`
- `test_run_qa_one_fails` — mix of passing and failing checks, verify `passed=False` and failed check captured
- `test_run_qa_captures_stdout_stderr` — check that fails with output, verify stdout/stderr in QACheck
- `test_run_qa_summary_format` — verify summary string lists failed checks
- `test_run_autofix_runs_ruff` — mock subprocess, verify `ruff check --fix` called when ruff in checks
- `test_detect_infrastructure_checks_finds_ruff` — create temp pyproject.toml with `[tool.ruff]`, verify detection
- `test_detect_infrastructure_checks_finds_npm_lint` — create temp package.json with scripts.lint, verify detection
- `test_infrastructure_checks_run_first` — pass both infra and spec checks, verify infra checks appear first in results

**Acceptance criteria:**
- All 8 tests pass
- Tests use temp directories with real files, not mocks for subprocess (integration-style)

**Validation commands:**
```bash
uv run pytest tests/test_qa.py -v
```

**Git checkpoint:** `git add -A && git commit -m "epic-2: deterministic QA tool with structured results"`

---

### Epic 3: Agent Prompt Templates

Write the prompt templates for all agent roles.

**3.1** Rewrite `src/golem/prompts/planner.md` for sub-agent architecture:

The planner prompt must instruct the agent to:
- Read the spec file
- Spawn Explorer sub-agents (Haiku) in a single message for codebase discovery — they write findings to `.golem/research/`
- Spawn Researcher sub-agents (Sonnet) in a single message for online API/framework docs — they write to `.golem/research/`
- Optionally spawn an Analyst sub-agent (Sonnet) for data flow tracing
- Read all `research/*.md` files after sub-agents complete
- Synthesize into `plans/overview.md` (blueprint, task graph, parallelism strategy)
- Synthesize into `plans/task-NNN.md` per task (exact files, lines, what to change, references, acceptance criteria, QA checks)
- Curate `references/*.md` from research findings
- Create a ticket for the Tech Lead via the `create_ticket` tool
- The number of sub-agents is dynamic based on codebase size and research scope

Template variables: `{spec_content}`, `{project_context}`, `{golem_dir}`

**Acceptance criteria:**
- Prompt instructs sub-agent spawning with model hints (Haiku for explorers, Sonnet for researchers)
- Prompt requires sub-agents to write to `.golem/research/` files, not return summaries
- Prompt requires synthesis into `plans/` and `references/` directories
- Prompt requires ticket creation at the end via tool call
- Template variables are all present and documented

**Validation commands:**
```bash
rg -q "research/" src/golem/prompts/planner.md
rg -q "plans/" src/golem/prompts/planner.md
rg -q "references/" src/golem/prompts/planner.md
rg -q "create_ticket" src/golem/prompts/planner.md
rg -q "Haiku" src/golem/prompts/planner.md
rg -q "Sonnet" src/golem/prompts/planner.md
rg -q "spec_content" src/golem/prompts/planner.md
```

**3.2** Rewrite `src/golem/prompts/worker.md` as the Writer prompt:

The writer prompt must instruct the agent to:
- Read the ticket context (plan section, file contents, references, blueprint)
- Sanity check the plan against the references before coding
- Use surgical `Edit` on existing files, never `Write` on files that already exist
- If parallelism hints are provided, spawn sub-writers in a single message
- Call `run_qa` tool after making changes
- If QA fails, read structured error output and fix in-context
- If QA passes, write a completion report and update ticket to `ready_for_review`
- Stay alive and wait for Tech Lead review
- If Tech Lead sends `needs_work`, pick up feedback in-context and fix

Template variables: `{ticket_context}`, `{plan_section}`, `{file_contents}`, `{references}`, `{blueprint}`, `{acceptance}`, `{qa_checks}`, `{parallelism_hints}`

**Acceptance criteria:**
- Prompt has sanity-check instruction (verify plan against references)
- Prompt has "never Write on existing files" rule
- Prompt has parallelism hint instruction
- Prompt has run_qa tool call instruction
- Prompt has "stay alive, wait for review" instruction
- Prompt has "pick up needs_work feedback in-context" instruction
- All template variables present

**Validation commands:**
```bash
rg -q "run_qa" src/golem/prompts/worker.md
rg -q "Edit" src/golem/prompts/worker.md
rg -q "never.*Write" src/golem/prompts/worker.md
rg -q "ready_for_review" src/golem/prompts/worker.md
rg -q "needs_work" src/golem/prompts/worker.md
rg -q "sanity" src/golem/prompts/worker.md
rg -q "blueprint" src/golem/prompts/worker.md
```

**3.3** Create `src/golem/prompts/tech_lead.md` — new Tech Lead prompt:

The Tech Lead prompt must instruct the agent to:
- Read `plans/overview.md` and all `plans/task-NNN.md` files
- Read `references/*.md` for full context
- Create worktrees using `create_worktree` tool
- Create tickets for writer pairs with pre-loaded context (file contents, plan section, references, blueprint, acceptance, QA checks, parallelism hints)
- Spawn writer pairs in a single message for independent tasks
- Monitor ticket updates — when a writer reports `ready_for_review`, read the report
- Review: compare report against plan and acceptance criteria
- If LGTM: update ticket to `approved`
- If needs work: update ticket to `needs_work` with specific feedback
- After all tickets approved: commit worktrees, merge branches, run `run_qa` on merged code
- Do integration review inline (has full context, no need for separate agent)
- If web project: spawn UX smoke test session
- Create PR with full run report

Template variables: `{golem_dir}`, `{spec_content}`, `{project_root}`

**Acceptance criteria:**
- Prompt covers full lifecycle: read plans → create tickets → spawn writers → review → merge → QA → PR
- Prompt has tool usage instructions for all Tech Lead tools
- Prompt has integration review instructions (inline, not separate agent)
- Prompt has UX smoke test instructions for web projects specifying **Playwright test:** browser-based verification — the Tech Lead must spawn a session that navigates the running app and asserts visible UI elements
- All template variables present

**Validation commands:**
```bash
rg -q "plans/overview" src/golem/prompts/tech_lead.md
rg -q "create_ticket" src/golem/prompts/tech_lead.md
rg -q "create_worktree" src/golem/prompts/tech_lead.md
rg -q "run_qa" src/golem/prompts/tech_lead.md
rg -q "merge" src/golem/prompts/tech_lead.md
rg -q "approved" src/golem/prompts/tech_lead.md
rg -q "needs_work" src/golem/prompts/tech_lead.md
rg -q "smoke test" src/golem/prompts/tech_lead.md
```

**3.4** Remove obsolete prompts:
- Delete `src/golem/prompts/validator.md` (replaced by `run_qa` tool)
- Delete `src/golem/prompts/integration_reviewer.md` (Tech Lead does this inline)

**Acceptance criteria:**
- `validator.md` does not exist
- `integration_reviewer.md` does not exist
- No imports reference these deleted files

**Validation commands:**
```bash
test ! -f src/golem/prompts/validator.md
test ! -f src/golem/prompts/integration_reviewer.md
rg -q "validator\.md\|integration_reviewer\.md" src/golem/ && exit 1 || exit 0
```

**Git checkpoint:** `git add -A && git commit -m "epic-3: agent prompt templates for planner, writer, tech lead"`

---

### Epic 4: Planner Refactor

Refactor the planner to use sub-agents that write to `research/` and synthesize into `plans/` + `references/`.

**4.1** Modify `src/golem/planner.py`:

**Research step:** Before implementing, dispatch a sub-agent to use **WebSearch** and **WebFetch** to look up the latest 2026 documentation for `claude-agent-sdk` — verify the current API surface for `query()`, `ClaudeAgentOptions`, model parameter names, and how to spawn sub-agents with different models. Do NOT rely on training data for this library's API.

- Refactor `run_planner()` to:
  - Create `.golem/research/`, `.golem/plans/`, `.golem/references/` directories
  - Build planner prompt from template with `{spec_content}`, `{project_context}`, `{golem_dir}`
  - Spawn planner as SDK session with `claude_code` tools + ticket tools
  - The planner agent itself spawns Explorer/Researcher/Analyst sub-agents via the `Agent` tool within its session
  - After planner completes, verify that `plans/overview.md` exists (the minimum viable output)
  - Read the planner's ticket creation from the ticket store
  - Return the ticket ID for the Tech Lead to pick up
- Remove `_TASKS_JSON_SCHEMA` — no longer needed (planner writes markdown plan files, not JSON)
- Remove `_extract_json()` — no longer needed
- Keep the SDK timeout monkey-patch (still needed for long-running planner sessions)

**Acceptance criteria:**
- `run_planner()` creates research/, plans/, references/ directories
- Planner is spawned as a full SDK session with claude_code tools
- After planner completes, `plans/overview.md` exists on disk
- A ticket is created in the ticket store for the Tech Lead
- `_TASKS_JSON_SCHEMA` and `_extract_json()` are removed
- SDK timeout patch is preserved

**Validation commands:**
```bash
rg -q "research" src/golem/planner.py
rg -q "plans" src/golem/planner.py
rg -q "references" src/golem/planner.py
rg -q "plans/overview.md" src/golem/planner.py
rg -q "TicketStore\|create_ticket\|ticket" src/golem/planner.py
test ! "$(rg '_TASKS_JSON_SCHEMA' src/golem/planner.py)"
```

**4.2** Update `tests/test_planner.py` (or create if it doesn't exist):

**Research step:** Before implementing, dispatch a sub-agent to use **WebSearch** and **WebFetch** to look up the latest 2026 documentation for `claude-agent-sdk` — verify the correct import path for `query()`, its return type (async generator vs. coroutine), and the recommended pattern for mocking it in `pytest` tests using `unittest.mock.patch`. Do NOT assume the mock target or return shape from training data.

- Test that `run_planner()` creates the expected directory structure
- Test that planner creates a ticket in the store
- Mock the SDK `query()` to avoid actual API calls in tests

**Acceptance criteria:**
- Tests verify directory creation and ticket creation
- Tests don't hit real API (mock SDK)

**Validation commands:**
```bash
uv run pytest tests/ -k "planner" -v
```

**Git checkpoint:** `git add -A && git commit -m "epic-4: planner refactor with sub-agent research architecture"`

---

### Epic 5: Tech Lead Agent

Build the persistent Tech Lead that reads plans, creates tickets, spawns writers, reviews work, and merges.

**5.1** Create `src/golem/tech_lead.py`:

**Research step:** Before implementing, dispatch a sub-agent to use **WebSearch** and **WebFetch** to look up the latest 2026 documentation for `claude-agent-sdk` — verify how to create long-running persistent agent sessions, how to inject custom tools into sessions, and how `query()` handles tool results and continuation. Do NOT rely on training data.

- `run_tech_lead(ticket_id: str, golem_dir: Path, config: GolemConfig, project_root: Path) -> None`
  - Reads the planner's ticket to get plan file paths
  - Builds Tech Lead prompt from template
  - Spawns Tech Lead as SDK session with:
    - `claude_code` preset tools
    - Custom tools: `create_ticket`, `update_ticket`, `read_ticket`, `list_tickets`, `run_qa`, `create_worktree`, `merge_branches`, `commit_worktree`
    - Permission mode: `bypassPermissions`
  - The Tech Lead agent handles the full execution lifecycle within its session
  - Function returns when the Tech Lead creates a PR or reports completion

- Custom tool definitions for the Tech Lead SDK session:
  - Ticket tools call `TicketStore` methods
  - QA tool calls `run_qa()` from `qa.py`
  - Git tools call existing functions from `worktree.py`
  - Each tool is registered as a JSON schema for the SDK's tool use

**Acceptance criteria:**
- `run_tech_lead()` spawns a persistent SDK session
- Tech Lead session has ticket tools, QA tool, and git tools injected
- Tech Lead reads plan files from disk within its session
- Function blocks until Tech Lead completes (PR created or error)

**Validation commands:**
```bash
rg -q "def run_tech_lead" src/golem/tech_lead.py
rg -q "create_ticket\|update_ticket\|read_ticket" src/golem/tech_lead.py
rg -q "run_qa" src/golem/tech_lead.py
rg -q "create_worktree\|merge_branches" src/golem/tech_lead.py
rg -q "bypassPermissions" src/golem/tech_lead.py
```

**5.2** Create `src/golem/tools.py` — custom tool definitions for SDK injection:

**Research step:** Before implementing, dispatch a sub-agent to use **WebSearch** and **WebFetch** to look up the latest 2026 documentation for `claude-agent-sdk` — verify the exact JSON schema format for custom tool definitions (field names, `input_schema` structure, required vs. optional fields), how tool call results are returned to the agent within the `query()` loop, and how the SDK expects tool dispatch to be wired. Do NOT rely on training data for this SDK's tool injection API.

- Define JSON schemas for each custom tool (matching Claude Agent SDK's tool definition format)
- Tool handler functions that dispatch to the correct Python function
- `get_tech_lead_tools(golem_dir, config, project_root) -> list[dict]` — returns all tool schemas
- `handle_tool_call(tool_name, tool_input, golem_dir, config, project_root) -> str` — dispatches and returns result as string

**Acceptance criteria:**
- Each tool has a JSON schema with name, description, and input_schema
- `handle_tool_call()` dispatches to correct function for all tools
- Tool results are returned as JSON strings

**Validation commands:**
```bash
rg -q "def get_tech_lead_tools" src/golem/tools.py
rg -q "def handle_tool_call" src/golem/tools.py
rg -q "create_ticket" src/golem/tools.py
rg -q "run_qa" src/golem/tools.py
rg -q "input_schema" src/golem/tools.py
uv run pytest tests/test_tools.py -v
```

**5.3** Create `tests/test_tools.py`:

- `test_get_tech_lead_tools_returns_all_tools` — verify all tool schemas present
- `test_handle_tool_call_create_ticket` — call with create_ticket, verify ticket created
- `test_handle_tool_call_run_qa` — call with run_qa input, verify structured result
- `test_handle_tool_call_unknown_tool_raises` — unknown tool name raises error

**Acceptance criteria:**
- All tests pass
- Tests use temp directories for ticket store isolation

**Validation commands:**
```bash
uv run pytest tests/test_tools.py -v
```

**Git checkpoint:** `git add -A && git commit -m "epic-5: tech lead agent with custom tool injection"`

---

### Epic 6: Writer Pair Spawning

Build the writer+validator pair architecture where writers are spawned by the Tech Lead and stay alive until approved.

**6.1** Create `src/golem/writer.py` (replace old `worker.py`):

**Research step:** Before implementing, dispatch a sub-agent to use **WebSearch** and **WebFetch** to look up the latest 2026 documentation for `claude-agent-sdk` — verify how sub-agents work within a parent session, how to keep sessions alive and send follow-up messages, and how tool results flow back. Do NOT rely on training data.

- `build_writer_prompt(ticket: Ticket) -> str`
  - Loads `prompts/worker.md` template
  - Injects ticket context: plan section, file contents, references, blueprint, acceptance, QA checks, parallelism hints
  - Strips empty sections (same pattern as v1 `_strip_section`)
- `spawn_writer_pair(ticket: Ticket, worktree_path: str, config: GolemConfig) -> str`
  - Builds prompt from ticket
  - Spawns SDK session with `claude_code` tools + `run_qa` custom tool
  - `cwd` = worktree_path
  - Returns the writer's result text (completion report or error)

**Acceptance criteria:**
- `build_writer_prompt()` injects all ticket context fields into the template
- Empty context sections are stripped (no `{placeholder}` leftovers in output)
- `spawn_writer_pair()` spawns SDK session with run_qa tool available
- Writer session runs in the correct worktree (cwd = worktree_path)

**Validation commands:**
```bash
rg -q "def build_writer_prompt" src/golem/writer.py
rg -q "def spawn_writer_pair" src/golem/writer.py
rg -q "run_qa" src/golem/writer.py
rg -q "worktree_path" src/golem/writer.py
rg -q "_strip_section\|strip.*section" src/golem/writer.py
```

**6.2** Remove or deprecate old files:
- Remove `src/golem/executor.py` (replaced by Tech Lead orchestration)
- Remove old `src/golem/worker.py` (replaced by new `writer.py`)
- Update `src/golem/validator.py` — keep `_subprocess_env()` and `_normalize_cmd()` (used by `qa.py`), remove everything else

**Acceptance criteria:**
- `executor.py` no longer exists
- Old `worker.py` no longer exists (new `writer.py` replaces it)
- `validator.py` only exports `_subprocess_env` and `_normalize_cmd`
- No remaining imports reference deleted functions

**Validation commands:**
```bash
test ! -f src/golem/executor.py
rg -q "def _subprocess_env" src/golem/validator.py
rg -q "def _normalize_cmd" src/golem/validator.py
test ! "$(rg 'run_ai_validator\|run_integration_reviewer\|run_validation' src/golem/validator.py)"
```

**Git checkpoint:** `git add -A && git commit -m "epic-6: writer pair spawning with ticket-driven context injection"`

---

### Epic 7: CLI Pipeline Wiring

Wire the new v2 pipeline into the CLI.

**7.1** Modify `src/golem/cli.py`:

**Research step:** Before implementing, dispatch a sub-agent to use **WebSearch** and **WebFetch** to look up the latest 2026 `typer` documentation — verify the current API for `async` command callbacks, `typer.Option` / `typer.Argument` signatures, and any deprecation warnings or breaking changes in the version pinned in `pyproject.toml`. Do NOT assume the API surface from training data.

- Update `run` command to use v2 pipeline:
  1. Create `.golem/` directories (tickets/, research/, plans/, references/, reports/, worktrees/)
  2. Detect infrastructure checks from target project
  3. Run planner: `await run_planner(spec, golem_dir, config, project_root)`
  4. Run tech lead: `await run_tech_lead(ticket_id, golem_dir, config, project_root)`
  5. Tech Lead handles everything else (worktrees, writers, merge, QA, PR)
- Update `status` command to read from ticket store instead of tasks.json
- Update `clean` command to also remove tickets/, research/, plans/, references/, reports/
- Keep `plan` command as dry-run (planner only, no tech lead)
- Keep `resume` command — reads existing tickets, re-spawns tech lead

**Acceptance criteria:**
- `golem run` creates all `.golem/` subdirectories
- `golem run` calls `run_planner` then `run_tech_lead`
- `golem status` reads from ticket store
- `golem clean` removes all v2 directories
- `golem plan` runs planner only

**Validation commands:**
```bash
rg -q "run_planner" src/golem/cli.py
rg -q "run_tech_lead" src/golem/cli.py
rg -q "TicketStore\|tickets" src/golem/cli.py
rg -q "research\|plans\|references\|reports" src/golem/cli.py
```

**7.2** Update `src/golem/config.py`:

- Remove `infrastructure_checks` field (now ephemeral in `qa.py`, not in config)
- Keep all other fields
- Add `tech_lead_model: str = "claude-opus-4-6"` (separate from planner model for flexibility)

**Acceptance criteria:**
- `infrastructure_checks` removed from GolemConfig
- `tech_lead_model` field added with default
- Existing config fields preserved
- `save_config` still excludes ephemeral fields

**Validation commands:**
```bash
rg -q "tech_lead_model" src/golem/config.py
test ! "$(rg 'infrastructure_checks' src/golem/config.py)"
```

**7.3** Update all existing tests to work with v2 architecture:

- Update `tests/test_executor.py` → rename to `tests/test_tech_lead.py` or remove (executor is gone)
- Update `tests/test_config.py` — adjust for removed/added fields
- Update `tests/test_validator.py` — adjust for stripped-down validator module
- Ensure `uv run pytest` passes with 0 failures

**Acceptance criteria:**
- No test references deleted modules (`executor`, old `worker`, old `validator` functions)
- All tests pass: `uv run pytest` reports 0 failures
- Test count may change (some removed, some added) but 0 failures

**Validation commands:**
```bash
uv run pytest tests/ -v
```

**Git checkpoint:** `git add -A && git commit -m "epic-7: CLI pipeline wired to v2 ticket-driven architecture"`

---

### Epic 8: End-to-End Verification

Run the full v2 pipeline against a real spec to verify everything works.

**8.1** Run the smoke test spec:
```bash
uv run golem run --force docs/rg-smoke.md
```
Verify: planner creates plans/, tech lead spawns writers, writer completes, ticket shows `approved`, smoke.html created.

**8.2** Run the brownfield weather dashboard spec against `F:/Tools/Projects/golem-test/`:
```bash
uv run golem run --force F:/Tools/Projects/golem-test/docs/spec.md
```
Verify: all 4 tasks complete, CSS/HTML/JS align, air quality panel works.

**8.3** Inspect ticket history:
```bash
cat .golem/tickets/TICKET-001.json
```
Verify: ticket has full history with timestamps, agent actions, QA results.

**Acceptance criteria:**
- Smoke test completes with 1 task completed, 0 blocked
- Weather dashboard completes with 4 tasks completed, 0 blocked
- **Playwright test:** Tech Lead UX smoke test navigates the weather dashboard, verifies search results appear after typing a city, and air quality panel renders with AQI data
- Ticket JSON files exist in `.golem/tickets/` with full history
- `plans/overview.md` exists with blueprint
- `research/` contains sub-agent findings
- `references/` contains curated docs

**Validation commands:**
```bash
test -f .golem/plans/overview.md
test -d .golem/research
test -d .golem/references
test -d .golem/tickets
ls .golem/tickets/*.json
```

**Git checkpoint:** `git add -A && git commit -m "epic-8: end-to-end verification passed"`

---

## Completion Criteria

The implementation is **done** when all of the following pass:

- [ ] All epics committed (Epic 0 through Epic 8)
- [ ] `uv run pytest` — all tests green, 0 failures
- [ ] `ruff check .` exits 0 (no lint errors introduced)
- [ ] `uv run golem run --force docs/rg-smoke.md` — completes with v2 pipeline (tickets, plans, tech lead)
- [ ] `.golem/tickets/` contains structured ticket JSON with full history
- [ ] `.golem/plans/overview.md` exists with blueprint content
- [ ] `.golem/research/` contains sub-agent findings
- [ ] No `Any` types introduced in new code
- [ ] Old v1 executor loop is fully replaced — `executor.py` deleted
- [ ] `uv run golem version` reports correct version
