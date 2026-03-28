# Spec 6: Agent Stall Detection & Recovery

> Standalone spec — no dependency on Specs 1-5 (multi-spec orchestration).
> Can be run independently against current main.
> Fixes: tech lead burning 49 turns/$1.40 without dispatching junior devs,
> planner burning 54 turns without calling create_ticket, and any agent
> spending its entire budget reading without acting.

## Problem

Golem agents can burn their entire turn budget reading files without producing work. There is zero in-flight progress detection. The SDK's `max_turns` is a passive hard wall — Golem never inspects turn count, never tracks tool calls, never detects stalls, and treats max-turns termination identically to successful completion. The planner's fallback ticket creation masks planning failures. Post-session self-healing only fixes git artifacts, not wasted budget.

Real failure observed: tech lead spent 49 turns ($1.40) reading plan files without ever calling `create_worktree` or dispatching junior devs. The run completed "successfully" with zero code produced.

## Design

Three layers: **observability** (know what's happening) → **circuit breakers** (stop waste) → **recovery** (auto-retry with escalation).

Plus: rename "writer" to "junior dev" throughout for consistent agent identity.

### Tool Call Registry

Track every MCP tool invocation per session. Distinguish "action tools" (MCP orchestration: `create_ticket`, `create_worktree`, `run_qa`, `update_ticket`, `merge_branches`, `commit_worktree`) from "read tools" (Read, Grep, Glob, Bash). Circuit breakers fire on absence of action tools.

### Adaptive Circuit Breakers

Role-specific thresholds based on consecutive turns with zero MCP action calls:

| Role | Warning at | Kill at | Expected action tools |
|------|-----------|---------|----------------------|
| Planner | 60% budget (30/50) | 80% budget (40/50) | `create_ticket` |
| Tech Lead | 30% budget (30/100) | 50% budget (50/100) | `create_worktree`, `create_ticket` |
| Junior Dev | 30% budget (15/50) | 50% budget (25/50) | file writes (git diff) |

Threshold is **consecutive turns without an MCP action call**. An agent that calls `create_worktree` at turn 45 resets the counter.

### Mid-Run Warning Injection

When warning threshold hit, inject a warning via the existing operator guidance mechanism (write a guidance ticket that the agent reads via `read_ticket`). If guidance tickets aren't available for the role (e.g. planner has no ticket store yet), break out of `query()` and restart a new session with the warning prepended — files on disk persist across sessions since it's the same worktree. The warning message:

```
PROGRESS CHECK: You have used {current_turn} of {max_turns} turns.
You have NOT called any action tools in {stall_turns} consecutive turns.
You MUST take action NOW or your session will be terminated.
```

### Auto-Retry with Escalated Prompt

When kill threshold fires:
1. Terminate SDK session
2. Log `STALL_DETECTED role=<role> turns_used=<N> mcp_calls=<N>`
3. Spawn new session with escalated prompt:
   ```
   CRITICAL: Previous session stalled after {N} turns without action.
   You MUST call {expected_tool} within the first 10 turns. Act immediately.
   ```
4. If retry also stalls → `STALL_FATAL`, ticket status `failed`, hard stop

### Post-Session Verification

After each session completes (not stalled):
- **Planner:** `overview.md` exists, >3 lines, at least one `task-*.md` file
- **Tech Lead:** at least one commit on integration branch beyond main
- **Junior Dev:** `git diff --stat` in worktree shows changed files

Verification failure triggers the same retry-with-escalation path.

### Supervised Session Architecture

Replace raw `query()` loops with a shared supervisor:

```python
async def supervised_session(
    prompt: str,
    options: ClaudeAgentOptions,
    role: str,
    config: GolemConfig,
    registry: ToolCallRegistry,
    expected_actions: list[str],
    on_message: Callable[[AssistantMessage | ResultMessage], None],
    stall_warning_pct: float = 0.3,
    stall_kill_pct: float = 0.5,
) -> SupervisedResult:
```

Each role calls `supervised_session()` instead of `query()` directly.

### Coding Conventions

- **Python 3.12+**, async-first, strict typing, no `Any`
- **Always `encoding="utf-8"`** on all file I/O
- **No emoji in CLI/TUI output**
- **Formatter:** ruff, line length 120
- **Tests:** pytest with pytest-asyncio, use `tmp_path` fixture
- **Do NOT mock the Claude Agent SDK** — test orchestration logic around it
- **Match existing patterns** — follow the style in planner.py/tech_lead.py/writer.py

---

## Task 1: Tool Call Registry

**Files:**
- Create: `src/golem/supervisor.py`

- [ ] **Step 1: Create supervisor.py with ToolCallRegistry**
  Create `src/golem/supervisor.py` with:
  - `ACTION_TOOLS: set[str]` — the MCP action tool names: `{"create_ticket", "update_ticket", "read_ticket", "list_tickets", "create_worktree", "merge_branches", "commit_worktree", "run_qa"}`
  - `ToolCallRecord` dataclass: `tool_name: str`, `turn_number: int`, `timestamp: str`, `is_action: bool`
  - `ToolCallRegistry` class:
    - `records: list[ToolCallRecord]`
    - `def record(self, tool_name: str, turn: int) -> None` — append record, set `is_action = tool_name in ACTION_TOOLS`
    - `def action_call_count(self) -> int` — count records where `is_action`
    - `def total_call_count(self) -> int`
    - `def turns_since_last_action(self, current_turn: int) -> int` — turns since last `is_action` record, or `current_turn` if none
    - `def has_called(self, tool_name: str) -> bool`
    - `def has_called_any_action(self) -> bool`

- [ ] **Step 2: Add StallConfig dataclass**
  In `supervisor.py`:
  - `StallConfig` dataclass: `warning_pct: float`, `kill_pct: float`, `expected_actions: list[str]`, `role: str`, `max_turns: int`
  - `def warning_turn(self) -> int` — `int(self.max_turns * self.warning_pct)`
  - `def kill_turn(self) -> int` — `int(self.max_turns * self.kill_pct)`
  - Factory function `stall_config_for_role(role: str, max_turns: int) -> StallConfig` with defaults:
    - `"planner"`: warning 0.6, kill 0.8, expected `["create_ticket"]`
    - `"tech_lead"`: warning 0.3, kill 0.5, expected `["create_worktree", "create_ticket"]`
    - `"junior_dev"`: warning 0.3, kill 0.5, expected `[]` (verified via git diff instead)

- [ ] **Step 3: Add SupervisedResult dataclass**
  - `SupervisedResult` dataclass: `result_text: str`, `cost_usd: float`, `input_tokens: int`, `output_tokens: int`, `turns: int`, `duration_s: float`, `stalled: bool`, `stall_turn: int | None`, `registry: ToolCallRegistry`

- [ ] **Step 4: Create tests**
  Create `tests/test_supervisor.py` with tests:
  - `test_registry_record_action` — action tool recorded with `is_action=True`
  - `test_registry_record_read` — Read tool recorded with `is_action=False`
  - `test_registry_action_count` — counts only action tools
  - `test_registry_turns_since_last_action` — correct gap calculation
  - `test_registry_turns_since_no_actions` — returns current_turn when no actions ever
  - `test_registry_has_called` — specific tool lookup
  - `test_stall_config_planner` — correct thresholds for planner
  - `test_stall_config_tech_lead` — correct thresholds for tech lead
  - `test_stall_config_junior_dev` — correct thresholds for junior dev
  - `test_stall_config_warning_turn` — math check
  - `test_stall_config_kill_turn` — math check

- [ ] **Step 5: Commit**
  ```bash
  git add src/golem/supervisor.py tests/test_supervisor.py
  git commit -m "feat: add ToolCallRegistry and StallConfig for agent stall detection"
  ```

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. Module imports
uv run python -c "
from golem.supervisor import ToolCallRegistry, ToolCallRecord, StallConfig, SupervisedResult, stall_config_for_role, ACTION_TOOLS
assert len(ACTION_TOOLS) == 8
sc = stall_config_for_role('tech_lead', 100)
assert sc.warning_turn() == 30
assert sc.kill_turn() == 50
print('IMPORT: PASS')
"

# 2. Tests pass
uv run pytest tests/test_supervisor.py -v --tb=short 2>&1 | tail -1
```

Expected:
```
IMPORT: PASS
11 passed
```

---

## Task 2: Supervised Session Loop

**Files:**
- Modify: `src/golem/supervisor.py`

- [ ] **Step 1: Implement supervised_session()**
  Add `async def supervised_session(...)` to `supervisor.py`:
  ```python
  async def supervised_session(
      prompt: str,
      options: ClaudeAgentOptions,
      role: str,
      config: GolemConfig,
      stall_config: StallConfig,
      on_text: Callable[[str], None] | None = None,
      on_tool: Callable[[str], None] | None = None,
      golem_dir: Path | None = None,
  ) -> SupervisedResult:
  ```
  Implementation:
  - Initialize `ToolCallRegistry`, turn counter, start time
  - Loop: call `query()` with current prompt/options
  - For each `AssistantMessage`: count turn, inspect `ToolUseBlock` names, call `registry.record()`, call `on_text`/`on_tool` callbacks for verbose logging
  - After each turn: check `registry.turns_since_last_action(current_turn)`
  - If `>= stall_config.warning_turn()` AND not yet warned: break out of query, set `warned = True`
  - If `>= stall_config.kill_turn()`: break out of query, set `stalled = True`
  - On warning: build injection message with role-specific expected tools, restart `query()` with the injection prepended as a new user message in the conversation
  - On kill: return `SupervisedResult(stalled=True, stall_turn=current_turn, ...)`
  - On normal `ResultMessage`: return `SupervisedResult(stalled=False, ...)`
  - Log `STALL_WARNING` and `STALL_DETECTED` events to progress.log if `golem_dir` provided

- [ ] **Step 2: Add warning message builder**
  ```python
  def _build_stall_warning(role: str, current_turn: int, max_turns: int, stall_turns: int, expected_actions: list[str]) -> str:
  ```
  Returns the injection message. Role-specific expected actions listed explicitly.

- [ ] **Step 3: Add escalated prompt builder**
  ```python
  def build_escalated_prompt(role: str, original_prompt: str, turns_used: int, expected_actions: list[str]) -> str:
  ```
  Prepends the `CRITICAL: Previous session stalled...` block to the original prompt.

- [ ] **Step 4: Add progress events**
  In `src/golem/progress.py`, add:
  - `log_stall_warning(role: str, turn: int, max_turns: int, mcp_calls: int)`
    → `STALL_WARNING role=<role> turn=<N>/<max> mcp_actions=<N>`
  - `log_stall_detected(role: str, turn: int, max_turns: int, mcp_calls: int)`
    → `STALL_DETECTED role=<role> turn=<N>/<max> mcp_actions=<N>`
  - `log_stall_fatal(role: str, turn: int)`
    → `STALL_FATAL role=<role> turn=<N> — retry also stalled`
  - `log_stall_retry(role: str)`
    → `STALL_RETRY role=<role> — restarting with escalated prompt`

- [ ] **Step 5: Add tests**
  In `tests/test_supervisor.py`, add:
  - `test_supervised_session_normal_completion` — mock query returning ResultMessage, verify SupervisedResult.stalled is False
  - `test_supervised_session_stall_warning_injected` — mock query with only Read tool calls, verify warning message is built at warning threshold
  - `test_supervised_session_stall_kill` — mock query exceeding kill threshold, verify SupervisedResult.stalled is True
  - `test_build_stall_warning_tech_lead` — verify message contains expected tools
  - `test_build_stall_warning_planner` — verify message contains create_ticket
  - `test_build_escalated_prompt` — verify CRITICAL prefix prepended
  - `test_progress_stall_events` — verify STALL_WARNING/STALL_DETECTED format

- [ ] **Step 6: Commit**
  ```bash
  git add src/golem/supervisor.py src/golem/progress.py tests/test_supervisor.py tests/test_progress.py
  git commit -m "feat: add supervised_session with mid-run stall injection and circuit breakers"
  ```

#### Completion Gate

All checks must pass. If any fail, fix and re-run all checks before proceeding.

```bash
cd F:/Tools/Projects/golem-cli

# 1. Functions exist
uv run python -c "
from golem.supervisor import supervised_session, build_escalated_prompt
from golem.progress import ProgressLogger
for m in ['log_stall_warning', 'log_stall_detected', 'log_stall_fatal', 'log_stall_retry']:
    assert hasattr(ProgressLogger, m), f'FAIL: missing {m}'
print('FUNCTIONS: PASS')
"

# 2. Tests pass
uv run pytest tests/test_supervisor.py tests/test_progress.py -v --tb=short 2>&1 | tail -1
```

Expected:
```
FUNCTIONS: PASS
[N] passed
```

---

## Task 3: Wire Supervised Session into Tech Lead

**Files:**
- Modify: `src/golem/tech_lead.py`

- [ ] **Step 1: Replace raw query() loop with supervised_session()**
  In `run_tech_lead()`:
  - Import `supervised_session`, `stall_config_for_role`, `build_escalated_prompt` from `golem.supervisor`
  - Build `StallConfig` via `stall_config_for_role("tech_lead", config.max_tech_lead_turns)`
  - Replace the `async for message in query(...)` loop with `result = await supervised_session(...)`
  - Pass `on_text` and `on_tool` callbacks that print the existing `[TECH LEAD]` prefixed stderr output
  - After `supervised_session()` returns: check `result.stalled`

- [ ] **Step 2: Add auto-retry on stall**
  If `result.stalled`:
  - Log `STALL_DETECTED` via progress logger
  - Log `STALL_RETRY`
  - Build escalated prompt via `build_escalated_prompt("tech_lead", original_prompt, result.turns, ...)`
  - Call `supervised_session()` again with the escalated prompt
  - If retry also stalls: log `STALL_FATAL`, raise `RuntimeError("Tech lead stalled after retry")`

- [ ] **Step 3: Add post-session verification**
  After successful (non-stalled) completion:
  - Run `git log --oneline {branch_prefix}/*/integration --not main` to check for commits
  - If zero commits: treat as stall (trigger retry with escalated prompt)
  - Log verification result

- [ ] **Step 4: Update tests**
  In `tests/test_tech_lead.py`, add:
  - `test_tech_lead_stall_triggers_retry` — mock supervised_session returning stalled=True first, then stalled=False
  - `test_tech_lead_double_stall_fatal` — mock both attempts stalling, verify RuntimeError
  - `test_tech_lead_no_commits_triggers_retry` — mock empty git log, verify retry

- [ ] **Step 5: Commit**
  ```bash
  git add src/golem/tech_lead.py tests/test_tech_lead.py
  git commit -m "feat: wire supervised_session into tech lead with stall detection and auto-retry"
  ```

#### Completion Gate

All checks must pass. If any fail, fix and re-run all checks before proceeding.

```bash
cd F:/Tools/Projects/golem-cli

# 1. Tech lead imports supervisor
uv run python -c "
import ast, inspect
source = open('src/golem/tech_lead.py', encoding='utf-8').read()
assert 'supervised_session' in source, 'FAIL: supervised_session not imported'
assert 'stall_config_for_role' in source or 'StallConfig' in source, 'FAIL: StallConfig not used'
print('TECH_LEAD_WIRED: PASS')
"

# 2. Tests pass
uv run pytest tests/test_tech_lead.py tests/test_supervisor.py -v --tb=short 2>&1 | tail -1

# 3. Full suite
uv run pytest --tb=short -q 2>&1 | tail -1
```

Expected:
```
TECH_LEAD_WIRED: PASS
[N] passed
[N] passed
```

---

## Task 4: Wire Supervised Session into Planner

**Files:**
- Modify: `src/golem/planner.py`

- [ ] **Step 1: Replace raw query() loop with supervised_session()**
  In `run_planner()`:
  - Import and use `supervised_session` with `stall_config_for_role("planner", config.planner_max_turns)`
  - Pass `on_text`/`on_tool` callbacks for `[LEAD ARCHITECT]` prefixed stderr output
  - After completion: check `result.stalled`

- [ ] **Step 2: Add auto-retry on stall**
  Same pattern as tech lead: stall → retry with escalated prompt → double stall → fatal.

- [ ] **Step 3: Add post-session content verification**
  After successful completion, verify:
  - `overview.md` exists AND has >3 lines
  - At least one `task-*.md` file exists in plans/
  - If verification fails: treat as stall, trigger retry

- [ ] **Step 4: Improve fallback ticket quality**
  When fallback ticket is created (planner didn't call `create_ticket`):
  - Log `STALL_WARNING` (not just a print to stderr)
  - Validate that `overview.md` content has actual task descriptions, not just headings
  - Include task file count in the fallback ticket's `acceptance` field

- [ ] **Step 5: Update tests**
  In `tests/test_planner.py`, add:
  - `test_planner_stall_triggers_retry` — mock stalled supervised_session
  - `test_planner_empty_overview_triggers_retry` — mock overview.md with <3 lines
  - `test_planner_no_task_files_triggers_retry` — mock empty plans dir
  - `test_planner_fallback_ticket_logs_warning` — verify STALL_WARNING event

- [ ] **Step 6: Commit**
  ```bash
  git add src/golem/planner.py tests/test_planner.py
  git commit -m "feat: wire supervised_session into planner with content verification"
  ```

#### Completion Gate

All checks must pass. If any fail, fix and re-run all checks before proceeding.

```bash
cd F:/Tools/Projects/golem-cli

# 1. Planner imports supervisor
uv run python -c "
source = open('src/golem/planner.py', encoding='utf-8').read()
assert 'supervised_session' in source, 'FAIL: supervised_session not imported'
print('PLANNER_WIRED: PASS')
"

# 2. Tests pass
uv run pytest tests/test_planner.py tests/test_supervisor.py -v --tb=short 2>&1 | tail -1

# 3. Full suite
uv run pytest --tb=short -q 2>&1 | tail -1
```

Expected:
```
PLANNER_WIRED: PASS
[N] passed
[N] passed
```

---

## Task 5: Rename Writer to Junior Dev

**Files:**
- Rename: `src/golem/writer.py` → keep file, rename internals
- Modify: `src/golem/writer.py`
- Modify: `src/golem/tools.py`
- Modify: `src/golem/tech_lead.py`
- Modify: `src/golem/cli.py`
- Modify: `src/golem/progress.py`
- Rename: `src/golem/prompts/worker.md` → `src/golem/prompts/junior_dev.md`
- Rename: `src/golem/prompts/worker_rework.md` → `src/golem/prompts/junior_dev_rework.md`
- Modify: `src/golem/prompts/tech_lead.md`
- Modify: `tests/test_writer.py`

- [ ] **Step 1: Rename functions and classes in writer.py**
  - `spawn_writer_pair()` → `spawn_junior_dev()`
  - `WriterResult` → `JuniorDevResult`
  - Keep `spawn_writer_pair` as a deprecated alias: `spawn_writer_pair = spawn_junior_dev`
  - Update all internal references, docstrings, variable names
  - Change stderr prefix from `[WRITER]` to `[JUNIOR DEV]`

- [ ] **Step 2: Rename MCP server in tools.py**
  - `create_writer_mcp_server()` → `create_junior_dev_mcp_server()`
  - Keep old name as alias for backward compat
  - Update MCP server name string from `"golem-writer"` to `"golem-junior-dev"`

- [ ] **Step 3: Rename prompt files**
  - Copy `src/golem/prompts/worker.md` to `src/golem/prompts/junior_dev.md`
  - Copy `src/golem/prompts/worker_rework.md` to `src/golem/prompts/junior_dev_rework.md`
  - In both new files: replace "writer" with "junior dev", "Writer" with "Junior Dev"
  - Update the agent identity section to emphasize "You are a Junior Software Engineer"
  - Delete old files after callers are updated

- [ ] **Step 4: Update tech_lead.md prompt**
  - Replace all "writer" references with "junior dev"
  - Replace "spawn_writer_pair" with "spawn_junior_dev"
  - Update dispatch instructions to use new terminology

- [ ] **Step 5: Update callers**
  - `tech_lead.py`: change `spawn_writer_pair` calls to `spawn_junior_dev`, `WriterResult` to `JuniorDevResult`
  - `cli.py`: update any "writer" references in output strings
  - `progress.py`: ensure `AGENT_COST role=junior_dev` (check existing `role=` strings)
  - Update prompt file paths in `writer.py` from `worker.md`/`worker_rework.md` to `junior_dev.md`/`junior_dev_rework.md`

- [ ] **Step 6: Update tests**
  In `tests/test_writer.py`:
  - Rename test references from `spawn_writer_pair` to `spawn_junior_dev`
  - Rename `WriterResult` to `JuniorDevResult`
  - Verify backward compat alias `spawn_writer_pair` still works
  - Update any string assertions from "WRITER" to "JUNIOR DEV"

- [ ] **Step 7: Commit**
  ```bash
  git add src/golem/writer.py src/golem/tools.py src/golem/tech_lead.py src/golem/cli.py src/golem/progress.py
  git add src/golem/prompts/junior_dev.md src/golem/prompts/junior_dev_rework.md
  git add src/golem/prompts/tech_lead.md
  git add tests/test_writer.py
  git rm src/golem/prompts/worker.md src/golem/prompts/worker_rework.md
  git commit -m "refactor: rename writer to junior dev throughout codebase and prompts"
  ```

#### Completion Gate

All checks must pass. If any fail, fix and re-run all checks before proceeding.

```bash
cd F:/Tools/Projects/golem-cli

# 1. New names importable
uv run python -c "
from golem.writer import spawn_junior_dev, JuniorDevResult, spawn_writer_pair
assert spawn_writer_pair is spawn_junior_dev, 'FAIL: alias broken'
print('RENAME_IMPORT: PASS')
"

# 2. Prompt files renamed
test -s src/golem/prompts/junior_dev.md && echo "PROMPT: PASS" || echo "PROMPT: FAIL"
test -s src/golem/prompts/junior_dev_rework.md && echo "REWORK_PROMPT: PASS" || echo "REWORK_PROMPT: FAIL"
! test -f src/golem/prompts/worker.md && echo "OLD_DELETED: PASS" || echo "OLD_DELETED: FAIL"

# 3. No "writer" in new prompts (case-insensitive, excluding comments)
grep -ci "writer" src/golem/prompts/junior_dev.md | xargs -I{} test {} -eq 0 && echo "NO_WRITER_REF: PASS" || echo "NO_WRITER_REF: FAIL"

# 4. Tests pass
uv run pytest tests/test_writer.py -v --tb=short 2>&1 | tail -1

# 5. Full suite
uv run pytest --tb=short -q 2>&1 | tail -1
```

Expected:
```
RENAME_IMPORT: PASS
PROMPT: PASS
REWORK_PROMPT: PASS
OLD_DELETED: PASS
NO_WRITER_REF: PASS
[N] passed
[N] passed
```

---

## Task 6: Wire Supervised Session into Junior Dev

**Files:**
- Modify: `src/golem/writer.py`

- [ ] **Step 1: Replace raw query() loop with supervised_session()**
  In `spawn_junior_dev()`:
  - Import and use `supervised_session` with `stall_config_for_role("junior_dev", config.max_worker_turns)`
  - Pass `on_text`/`on_tool` callbacks for `[JUNIOR DEV]` prefixed stderr output

- [ ] **Step 2: Add post-session worktree verification**
  After successful completion:
  - Run `git diff --stat` in the worktree directory
  - If zero files changed: treat as stall, trigger retry with escalated prompt
  - Log verification result

- [ ] **Step 3: Add auto-retry on stall**
  Same pattern: stall → retry with escalated prompt (use `junior_dev_rework.md` as base) → double stall → mark ticket as `failed`.

- [ ] **Step 4: Update tests**
  In `tests/test_writer.py`, add:
  - `test_junior_dev_stall_triggers_retry` — mock stalled supervised_session
  - `test_junior_dev_no_diff_triggers_retry` — mock empty git diff, verify retry
  - `test_junior_dev_double_stall_fatal` — verify ticket marked failed

- [ ] **Step 5: Commit**
  ```bash
  git add src/golem/writer.py tests/test_writer.py
  git commit -m "feat: wire supervised_session into junior dev with worktree verification"
  ```

#### Completion Gate

All checks must pass. If any fail, fix and re-run all checks before proceeding.

```bash
cd F:/Tools/Projects/golem-cli

# 1. Junior dev imports supervisor
uv run python -c "
source = open('src/golem/writer.py', encoding='utf-8').read()
assert 'supervised_session' in source, 'FAIL: supervised_session not imported'
print('JUNIOR_DEV_WIRED: PASS')
"

# 2. Tests pass
uv run pytest tests/test_writer.py tests/test_supervisor.py -v --tb=short 2>&1 | tail -1

# 3. Full suite
uv run pytest --tb=short -q 2>&1 | tail -1
```

Expected:
```
JUNIOR_DEV_WIRED: PASS
[N] passed
[N] passed
```

---

## Task 7: Tool Call Instrumentation in MCP Servers

**Files:**
- Modify: `src/golem/tools.py`

- [ ] **Step 1: Add middleware to MCP tool handlers**
  Wrap each tool handler in `create_golem_mcp_server()` and `create_junior_dev_mcp_server()` to emit a structured log entry when called:
  - Log format: `TOOL_CALL role=<role> tool=<name> turn=<N>` to progress.log
  - This gives external observability into which tools agents are actually calling

- [ ] **Step 2: Add ToolCallRegistry integration**
  Accept an optional `ToolCallRegistry` parameter in `create_golem_mcp_server()` and `create_junior_dev_mcp_server()`. When provided, each handler records its call via `registry.record()`.

- [ ] **Step 3: Update tests**
  In `tests/test_tools.py`, add:
  - `test_tool_call_logs_to_progress` — verify TOOL_CALL event format
  - `test_tool_call_records_to_registry` — verify registry.record() called

- [ ] **Step 4: Commit**
  ```bash
  git add src/golem/tools.py tests/test_tools.py
  git commit -m "feat: add tool call instrumentation to MCP servers"
  ```

#### Completion Gate

All checks must pass. If any fail, fix and re-run all checks before proceeding.

```bash
cd F:/Tools/Projects/golem-cli

# 1. MCP servers accept registry parameter
uv run python -c "
import inspect
from golem.tools import create_golem_mcp_server, create_junior_dev_mcp_server
for fn_name, fn in [('create_golem_mcp_server', create_golem_mcp_server), ('create_junior_dev_mcp_server', create_junior_dev_mcp_server)]:
    sig = inspect.signature(fn)
    assert 'registry' in sig.parameters or 'tool_registry' in sig.parameters, f'FAIL: {fn_name} missing registry param'
print('MCP_REGISTRY: PASS')
"

# 2. Tests pass
uv run pytest tests/test_tools.py -v --tb=short 2>&1 | tail -1

# 3. Full suite still passes
uv run pytest --tb=short -q 2>&1 | tail -1
```

Expected:
```
MCP_REGISTRY: PASS
[N] passed
[N] passed
```

---

## Phase 6 Completion Gate

**Phase 6 is NOT complete until every check below passes.** If any check fails, return to the responsible task (see verdict table), fix the issue, and re-run this ENTIRE phase gate — not just the failing check.

### Gate 1: Supervisor Module

```bash
cd F:/Tools/Projects/golem-cli
uv run python -c "
from golem.supervisor import (
    ToolCallRegistry, ToolCallRecord, StallConfig, SupervisedResult,
    stall_config_for_role, supervised_session, build_escalated_prompt, ACTION_TOOLS
)
assert len(ACTION_TOOLS) >= 8
sc = stall_config_for_role('tech_lead', 100)
assert sc.warning_turn() == 30
assert sc.kill_turn() == 50
sc2 = stall_config_for_role('planner', 50)
assert sc2.warning_turn() == 30
assert sc2.kill_turn() == 40
print('SUPERVISOR: PASS')
"
```

### Gate 2: All Agents Wired

```bash
cd F:/Tools/Projects/golem-cli
FAIL=0
for f in src/golem/tech_lead.py src/golem/planner.py src/golem/writer.py; do
  if grep -q "supervised_session" "$f"; then
    echo "$f: PASS"
  else
    echo "$f: FAIL"
    FAIL=$((FAIL+1))
  fi
done
test $FAIL -eq 0 && echo "ALL_WIRED: PASS" || echo "ALL_WIRED: FAIL ($FAIL files missing)"
```

### Gate 3: Junior Dev Rename

```bash
cd F:/Tools/Projects/golem-cli
uv run python -c "from golem.writer import spawn_junior_dev, JuniorDevResult; print('RENAME: PASS')"
test -s src/golem/prompts/junior_dev.md && echo "PROMPT: PASS" || echo "PROMPT: FAIL"
! test -f src/golem/prompts/worker.md && echo "OLD_GONE: PASS" || echo "OLD_GONE: FAIL"
```

### Gate 4: Progress Events

```bash
cd F:/Tools/Projects/golem-cli
uv run python -c "
from golem.progress import ProgressLogger
for m in ['log_stall_warning', 'log_stall_detected', 'log_stall_fatal', 'log_stall_retry']:
    assert hasattr(ProgressLogger, m), f'FAIL: {m}'
print('PROGRESS: PASS')
"
```

### Gate 5: Tool Instrumentation

```bash
cd F:/Tools/Projects/golem-cli
uv run python -c "
import inspect
from golem.tools import create_golem_mcp_server, create_junior_dev_mcp_server
for fn_name, fn in [('golem', create_golem_mcp_server), ('junior_dev', create_junior_dev_mcp_server)]:
    sig = inspect.signature(fn)
    assert 'registry' in sig.parameters or 'tool_registry' in sig.parameters, f'FAIL: {fn_name}'
print('INSTRUMENTATION: PASS')
"
```

### Gate 6: Full Test Suite

```bash
cd F:/Tools/Projects/golem-cli
uv run pytest -v --tb=short 2>&1 | tail -5
```

Expected: `[N] passed, 0 failed` (must be >= 314 existing + new tests)

### Phase 6 Verdict

| Gate | Validates Tasks |
|------|----------------|
| Gate 1 | Tasks 1-2 (supervisor module) |
| Gate 2 | Tasks 3, 4, 6 (all agents wired) |
| Gate 3 | Task 5 (junior dev rename) |
| Gate 4 | Task 2 (progress events) |
| Gate 5 | Task 7 (tool instrumentation) |
| Gate 6 | All tasks (regression) |
