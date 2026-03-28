# Golem Observability — Deep Agent Instrumentation & Preflight Diagnostics

## Overview

Add full observability to Golem: a typed event bus that captures every agent thought, tool call, sub-agent spawn, skill invocation, and lifecycle transition — surfaced through real-time streaming in the existing dashboard and a comprehensive preflight diagnostic panel.

**Problem:** Golem currently runs agents as blind subprocesses. Agent stderr is piped but never read by the server. All observability is a flat text `progress.log` that the server polls and re-emits as raw SSE strings. There is no visibility into what agents are thinking, what tools they're calling, when sub-agents spawn, or what MCP tools are available — making debugging impossible and monitoring purely reactive.

**Solution:** Replace the subprocess-based session spawning with in-process async tasks, introduce a typed `EventBus` that `supervised_session()` emits to directly, and add two new dashboard tabs (Observe + Preflight) that surface everything in real-time.

**Depends on:** Specs 1-5 merged to main (Foundation, CLI-as-Client, Merge Coordinator, Dashboard UI, Cross-Session Intelligence).

---

## Phase 1: Event Bus Core

### Task 1: GolemEvent Hierarchy and EventBus

**Files:**
- Create: `src/golem/events.py`
- Create: `tests/test_events.py`

- [ ] **Step 1: Define base event and all event types**

Create `src/golem/events.py` with:

Base dataclass `GolemEvent` with shared fields: `timestamp` (UTC ISO string), `session_id` (str), `event_id` (str, UUID4).

Agent lifecycle events (all subclass `GolemEvent`):

| Event Type | Key Fields |
|---|---|
| `AgentSpawned` | role: str, model: str, max_turns: int, mcp_tools: list[str], stall_config: dict |
| `AgentText` | role: str, text: str, turn: int |
| `AgentToolCall` | role: str, tool_name: str, arguments: dict, turn: int |
| `AgentToolResult` | role: str, tool_name: str, result_preview: str, duration_ms: int, turn: int |
| `AgentTurnComplete` | role: str, turn: int, tokens_in: int, tokens_out: int, cache_read: int |
| `AgentComplete` | role: str, total_cost: float, total_turns: int, duration_s: float, result_preview: str |
| `AgentStallWarning` | role: str, turn: int, turns_since_action: int, action_tools_available: list[str] |
| `AgentStallKill` | role: str, turn: int |

Claude Code internal events (inferred from `ToolUseBlock.name` patterns):

| Event Type | Key Fields |
|---|---|
| `SubAgentSpawned` | parent_role: str, subagent_type: str, description: str, prompt_preview: str |
| `SubAgentComplete` | parent_role: str, subagent_type: str, result_preview: str |
| `SkillInvoked` | role: str, skill_name: str |
| `PlanModeEntered` | role: str |
| `TaskProgress` | role: str, task_subject: str, status: str |

MCP tool events:

| Event Type | Key Fields |
|---|---|
| `TicketCreated` | ticket_id: str, title: str, assignee: str |
| `TicketUpdated` | ticket_id: str, old_status: str, new_status: str |
| `QAResult` | ticket_id: str, passed: bool, summary: str, checks_run: int |
| `WorktreeCreated` | branch: str, path: str |
| `MergeComplete` | source_branch: str, target_branch: str |

Session lifecycle events:

| Event Type | Key Fields |
|---|---|
| `SessionStart` | spec_path: str, complexity: str, config_snapshot: dict |
| `SessionComplete` | status: str, cost_usd: float, duration_s: float, error: str |
| `ConflictDetected` | file_path: str, session_a: str, session_b: str |

Add `to_dict()` method on `GolemEvent` that serializes to a JSON-compatible dict including the event `type` field (the class name in snake_case). Add `from_dict(data: dict) -> GolemEvent` classmethod that deserializes.

Add a module-level registry `EVENT_TYPES: dict[str, type[GolemEvent]]` that maps type strings to classes, populated at module load time.

- [ ] **Step 2: Implement EventBus with two backends**

In the same `events.py`:

```python
class QueueBackend:
    """Pushes events to an asyncio.Queue (server mode)."""
    def __init__(self, queue: asyncio.Queue) -> None: ...
    async def emit(self, event: GolemEvent) -> None: ...

class FileBackend:
    """Appends JSON lines to events.jsonl (CLI mode)."""
    def __init__(self, path: Path) -> None: ...
    async def emit(self, event: GolemEvent) -> None: ...

class EventBus:
    """Async event emitter with pluggable backend."""
    def __init__(self, backend: QueueBackend | FileBackend, session_id: str = "") -> None: ...
    async def emit(self, event: GolemEvent) -> None: ...
    def subscribe(self, event_filter: EventFilter | None = None) -> AsyncIterator[GolemEvent]: ...
```

`EventFilter` is a simple dataclass with optional `roles: list[str]` and `event_types: list[str]` fields.

`EventBus.emit()` sets `session_id` on the event (from the bus's session_id) if the event's session_id is empty, sets `event_id` if empty, sets `timestamp` if empty, then delegates to the backend.

`FileBackend.emit()` appends one JSON line (via `to_dict()`) to the path, using `encoding="utf-8"`.

`QueueBackend.emit()` calls `queue.put_nowait(event)`.

- [ ] **Step 3: Write tests**

Create `tests/test_events.py` with:

1. `test_event_to_dict_roundtrip` — create each event type, call `to_dict()`, call `from_dict()`, assert equal
2. `test_event_registry_complete` — assert `EVENT_TYPES` contains all 21 event types
3. `test_queue_backend_emits` — create `asyncio.Queue`, `QueueBackend`, emit event, `queue.get_nowait()` returns it
4. `test_file_backend_writes_jsonl` — create `FileBackend(tmp_path / "events.jsonl")`, emit 3 events, read file, assert 3 JSON lines with correct types
5. `test_event_bus_sets_session_id` — create EventBus with `session_id="test-1"`, emit event with empty session_id, assert it's set to "test-1"
6. `test_event_bus_sets_timestamp` — emit event with empty timestamp, assert timestamp is set
7. `test_event_bus_sets_event_id` — emit event with empty event_id, assert UUID format
8. `test_subscribe_with_filter` — emit 3 events with different roles, subscribe with `roles=["planner"]`, assert only planner events received
9. `test_subscribe_with_type_filter` — emit AgentText + AgentToolCall, subscribe with `event_types=["agent_tool_call"]`, assert only tool calls received

- [ ] **Step 4: Commit**

```bash
git add src/golem/events.py tests/test_events.py
git commit -m "feat: GolemEvent hierarchy and EventBus with Queue/File backends"
```

#### Completion Gate

All checks must pass. If any fail, fix and re-run all checks before proceeding.

```bash
cd /f/Tools/Projects/golem-cli

# 1. Module imports cleanly with all event types
uv run python -c "
from golem.events import (
    GolemEvent, EventBus, QueueBackend, FileBackend, EventFilter,
    AgentSpawned, AgentText, AgentToolCall, AgentToolResult,
    AgentTurnComplete, AgentComplete, AgentStallWarning, AgentStallKill,
    SubAgentSpawned, SubAgentComplete, SkillInvoked, PlanModeEntered, TaskProgress,
    TicketCreated, TicketUpdated, QAResult, WorktreeCreated, MergeComplete,
    SessionStart, SessionComplete, ConflictDetected, EVENT_TYPES
)
assert len(EVENT_TYPES) == 21, f'Expected 21 event types, got {len(EVENT_TYPES)}'
print('IMPORT: PASS')
" && echo "IMPORT_GATE: PASS" || echo "IMPORT_GATE: FAIL"

# 2. All event tests pass
uv run pytest tests/test_events.py -v --tb=short 2>&1 | tail -1
```

Expected:
```
IMPORT: PASS
IMPORT_GATE: PASS
9 passed
```

---

### Task 2: ProgressLogger as EventBus Subscriber

**Files:**
- Modify: `src/golem/progress.py`
- Modify: `tests/test_progress.py`

- [ ] **Step 1: Add EventBus subscription to ProgressLogger**

Modify `ProgressLogger` to accept an optional `EventBus` in its constructor. Add a method `subscribe_to_bus(event_bus: EventBus) -> None` that starts an async task consuming events from the bus and formatting them into the existing `progress.log` text format.

The mapping from GolemEvent types to progress.log text:

| Event Type | Progress.log format |
|---|---|
| `AgentSpawned` (role=planner) | `LEAD_ARCHITECT_START` |
| `AgentComplete` (role=planner) | `LEAD_ARCHITECT_COMPLETE elapsed={duration_s}` |
| `AgentSpawned` (role=tech_lead) | `TECH_LEAD_START` |
| `AgentComplete` (role=tech_lead) | `TECH_LEAD_COMPLETE elapsed={m}m{s}s` |
| `AgentSpawned` (role=junior_dev) | `JUNIOR_DEV_DISPATCHED` |
| `AgentComplete` (any role) | `AGENT_COST role={role} cost=${cost} ...` |
| `TicketCreated` | `TICKET_CREATED {ticket_id} title={title}` |
| `QAResult` (passed=True) | `QA_PASSED {ticket_id} {summary}` |
| `QAResult` (passed=False) | `QA_FAILED {ticket_id} {summary}` |
| `MergeComplete` | `MERGE_COMPLETE branch={target_branch}` |
| `AgentStallWarning` | `STALL_WARNING role={role} turn={turn}/{max} mcp_actions={n}` |
| `AgentStallKill` | `STALL_DETECTED role={role} turn={turn}` |
| `SessionStart` | `SESSION_START session_id={session_id} ...` |
| `SessionComplete` | `SESSION_COMPLETE session_id={session_id} ...` |

All existing `log_*` methods on `ProgressLogger` stay unchanged for backward compatibility — callers that don't use the EventBus path still work.

- [ ] **Step 2: Add tests**

Add to `tests/test_progress.py`:

1. `test_progress_logger_subscribes_to_event_bus` — create EventBus with QueueBackend, subscribe ProgressLogger, emit `AgentSpawned(role="planner", ...)`, assert `LEAD_ARCHITECT_START` appears in progress.log
2. `test_progress_logger_formats_agent_cost` — emit `AgentComplete`, assert `AGENT_COST` line matches existing format
3. `test_progress_logger_formats_qa_result` — emit `QAResult(passed=True)`, assert `QA_PASSED` line
4. `test_existing_log_methods_still_work` — call `log_planner_start()` directly, assert it still writes to progress.log (backward compat)

- [ ] **Step 3: Commit**

```bash
git add src/golem/progress.py tests/test_progress.py
git commit -m "feat: ProgressLogger subscribes to EventBus for backward-compatible logging"
```

#### Completion Gate

```bash
cd /f/Tools/Projects/golem-cli

# 1. Existing progress tests still pass
uv run pytest tests/test_progress.py -v --tb=short 2>&1 | tail -1

# 2. New subscriber tests pass (included in above)
```

Expected: all progress tests pass (existing + 4 new), 0 failed.

---

## Phase 1 Completion Gate

**Phase 1 is NOT complete until every check below passes.** If any check fails, return to the responsible task, fix the issue, and re-run this entire gate.

### Gate 1: Event Module

```bash
cd /f/Tools/Projects/golem-cli
uv run python -c "
from golem.events import EVENT_TYPES, EventBus, QueueBackend, FileBackend
assert len(EVENT_TYPES) == 21
print('EVENT_MODULE: PASS')
"
```

Expected: `EVENT_MODULE: PASS`

### Gate 2: Event Tests

```bash
cd /f/Tools/Projects/golem-cli
uv run pytest tests/test_events.py -v --tb=short 2>&1 | tail -1
```

Expected: `9 passed`

### Gate 3: Progress Tests (backward compat + new)

```bash
cd /f/Tools/Projects/golem-cli
uv run pytest tests/test_progress.py -v --tb=short 2>&1 | tail -1
```

Expected: all passed, 0 failed

### Gate 4: Full Test Suite (regression)

```bash
cd /f/Tools/Projects/golem-cli
uv run pytest -v --tb=short 2>&1 | tail -1
```

Expected: `[N] passed, 0 failed` (N >= 453 + new tests)

### Phase 1 Verdict

| Gate | Validates Tasks |
|------|----------------|
| Gate 1 | Task 1 |
| Gate 2 | Task 1 |
| Gate 3 | Task 2 |
| Gate 4 | Tasks 1-2 (regression) |

---

## Phase 2: Deep Agent Instrumentation

### Task 3: Instrument `supervised_session()` with EventBus

**Files:**
- Modify: `src/golem/supervisor.py`
- Modify: `tests/test_supervisor.py`

- [ ] **Step 1: Add `event_bus` parameter to `supervised_session()`**

Add `event_bus: EventBus | None = None` as the last parameter of `supervised_session()`.

At session start (before the `query()` call), emit:
```python
AgentSpawned(role=role, model=options.model, max_turns=options.max_turns,
             mcp_tools=[name for name in mcp_server_tools], stall_config={...})
```

Where `mcp_server_tools` is derived from `options.mcp_servers` — extract tool names from the MCP server configs.

- [ ] **Step 2: Emit events from the message loop**

Inside the `async for message in query(...)` loop in `supervised_session()`:

For `AssistantMessage`:
- For each `TextBlock` in `message.content`: emit `AgentText(role, text, turn)`
- For each `ToolUseBlock` in `message.content`: emit `AgentToolCall(role, tool_name=block.name, arguments=block.input, turn)`
  - **CC-internal inference**: if `block.name == "Agent"`: also emit `SubAgentSpawned(parent_role=role, subagent_type=block.input.get("subagent_type", ""), description=block.input.get("description", ""), prompt_preview=block.input.get("prompt", "")[:200])`
  - If `block.name == "Skill"`: also emit `SkillInvoked(role=role, skill_name=block.input.get("skill", ""))`
  - If `block.name == "EnterPlanMode"`: emit `PlanModeEntered(role=role)`
  - If `block.name in ("TaskCreate", "TaskUpdate")`: emit `TaskProgress(role=role, task_subject=block.input.get("subject", ""), status=block.input.get("status", ""))`

For `ResultMessage`:
- For each `ToolResultBlock`: emit `AgentToolResult(role, tool_name, result_preview=str(block.content)[:500], duration_ms=0, turn)` (duration_ms tracked if feasible, 0 otherwise)
  - If the original tool call was `name == "Agent"`: also emit `SubAgentComplete(parent_role=role, subagent_type=..., result_preview=str(block.content)[:500])`

At end of each turn: emit `AgentTurnComplete(role, turn, tokens_in, tokens_out, cache_read)` using `message.usage` dict.

- [ ] **Step 3: Emit stall and completion events**

At stall warning point (where `_build_stall_warning()` is called): emit `AgentStallWarning(role, turn, turns_since_action, action_tools_available)`.

At stall kill point: emit `AgentStallKill(role, turn)`.

At session end: emit `AgentComplete(role, total_cost, total_turns, duration_s, result_preview)`.

- [ ] **Step 4: Write tests**

Add to `tests/test_supervisor.py`:

1. `test_supervised_session_emits_agent_spawned` — mock `query()`, run `supervised_session()` with EventBus, assert `AgentSpawned` event emitted with correct role/model
2. `test_supervised_session_emits_text_events` — mock query with TextBlock, assert `AgentText` events
3. `test_supervised_session_emits_tool_call_events` — mock query with ToolUseBlock, assert `AgentToolCall` events with correct tool_name and arguments
4. `test_supervised_session_emits_agent_complete` — assert `AgentComplete` with cost/turns/duration
5. `test_supervised_session_emits_stall_warning` — trigger stall threshold, assert `AgentStallWarning`
6. `test_supervised_session_infers_subagent_spawn` — mock ToolUseBlock with name="Agent", assert `SubAgentSpawned`
7. `test_supervised_session_infers_skill_invoked` — mock ToolUseBlock with name="Skill", assert `SkillInvoked`
8. `test_supervised_session_no_events_without_bus` — run without event_bus, assert no errors (backward compat)

- [ ] **Step 5: Commit**

```bash
git add src/golem/supervisor.py tests/test_supervisor.py
git commit -m "feat: instrument supervised_session with EventBus emission"
```

#### Completion Gate

```bash
cd /f/Tools/Projects/golem-cli

# 1. All supervisor tests pass (existing + new)
uv run pytest tests/test_supervisor.py -v --tb=short 2>&1 | tail -1
```

Expected: all passed (existing + 8 new), 0 failed

---

### Task 4: Instrument MCP Tool Handlers

**Files:**
- Modify: `src/golem/tools.py`
- Modify: `tests/test_tools.py`

- [ ] **Step 1: Wire EventBus into MCP server factories**

Add `event_bus: EventBus | None = None` parameter to `create_golem_mcp_server()` and `create_junior_dev_mcp_server()`.

In each tool handler, emit the corresponding event after successful execution:
- `create_ticket` handler → `TicketCreated(ticket_id, title, assignee)`
- `update_ticket` handler → `TicketUpdated(ticket_id, old_status, new_status)`
- `run_qa` handler → `QAResult(ticket_id, passed, summary, checks_run)`
- `create_worktree` handler → `WorktreeCreated(branch, path)`
- `merge_branches` handler → `MergeComplete(source_branch, target_branch)`

- [ ] **Step 2: Write tests**

Add to `tests/test_tools.py`:

1. `test_create_ticket_emits_event` — pass EventBus to `create_golem_mcp_server()`, call create_ticket, assert `TicketCreated` event
2. `test_update_ticket_emits_event` — call update_ticket, assert `TicketUpdated` with old/new status
3. `test_run_qa_emits_event` — call run_qa, assert `QAResult` event
4. `test_no_events_without_bus` — create server without event_bus, call tools, no errors

- [ ] **Step 3: Commit**

```bash
git add src/golem/tools.py tests/test_tools.py
git commit -m "feat: emit MCP tool events via EventBus"
```

#### Completion Gate

```bash
cd /f/Tools/Projects/golem-cli

# 1. All tools tests pass (existing + new)
uv run pytest tests/test_tools.py -v --tb=short 2>&1 | tail -1
```

Expected: all passed, 0 failed

---

### Task 5: Thread EventBus Through Agent Functions

**Files:**
- Modify: `src/golem/planner.py`
- Modify: `src/golem/tech_lead.py`
- Modify: `src/golem/writer.py`
- Modify: `src/golem/cli.py`

- [ ] **Step 1: Add `event_bus` parameter to agent functions**

Add `event_bus: EventBus | None = None` as the last parameter to:
- `run_planner(spec, golem_dir, config, cwd, event_bus=None)`
- `run_tech_lead(golem_dir, config, event_bus=None)`
- `spawn_junior_dev(ticket, golem_dir, config, event_bus=None)`

Each function passes `event_bus` to:
- `supervised_session(..., event_bus=event_bus)`
- `create_golem_mcp_server(..., event_bus=event_bus)` or `create_junior_dev_mcp_server(..., event_bus=event_bus)`

- [ ] **Step 2: Wire FileBackend in CLI `golem run`**

In `cli.py`, when running without server (`--no-server` or no server detected):
- Create `FileBackend(golem_dir / "events.jsonl")`
- Create `EventBus(backend, session_id=config.session_id)`
- Pass `event_bus` to `run_planner()` and `run_tech_lead()`

- [ ] **Step 3: Verify existing tests still pass**

No new tests needed — this is plumbing. The existing tests mock `supervised_session` and won't see the new parameter (it's optional with default None).

- [ ] **Step 4: Commit**

```bash
git add src/golem/planner.py src/golem/tech_lead.py src/golem/writer.py src/golem/cli.py
git commit -m "feat: thread EventBus through planner, tech lead, writer, and CLI"
```

#### Completion Gate

```bash
cd /f/Tools/Projects/golem-cli

# 1. Planner, tech lead, writer, CLI tests all pass
uv run pytest tests/test_planner.py tests/test_tech_lead.py tests/test_writer.py tests/test_cli.py -v --tb=short 2>&1 | tail -1
```

Expected: all passed, 0 failed

---

## Phase 2 Completion Gate

**Phase 2 is NOT complete until every check below passes.** If any check fails, return to the responsible task, fix the issue, and re-run this entire gate.

### Gate 1: Supervisor Instrumentation

```bash
cd /f/Tools/Projects/golem-cli
uv run pytest tests/test_supervisor.py -v --tb=short 2>&1 | tail -1
```

Expected: all passed, 0 failed

### Gate 2: MCP Tool Instrumentation

```bash
cd /f/Tools/Projects/golem-cli
uv run pytest tests/test_tools.py -v --tb=short 2>&1 | tail -1
```

Expected: all passed, 0 failed

### Gate 3: Agent Function Signatures

```bash
cd /f/Tools/Projects/golem-cli
uv run python -c "
import inspect
from golem.planner import run_planner
from golem.tech_lead import run_tech_lead
from golem.writer import spawn_junior_dev
from golem.supervisor import supervised_session
for fn in [run_planner, run_tech_lead, spawn_junior_dev, supervised_session]:
    sig = inspect.signature(fn)
    assert 'event_bus' in sig.parameters, f'{fn.__name__} missing event_bus param'
print('SIGNATURES: PASS')
"
```

Expected: `SIGNATURES: PASS`

### Gate 4: Full Test Suite (regression)

```bash
cd /f/Tools/Projects/golem-cli
uv run pytest -v --tb=short 2>&1 | tail -1
```

Expected: `[N] passed, 0 failed`

### Phase 2 Verdict

| Gate | Validates Tasks |
|------|----------------|
| Gate 1 | Task 3 |
| Gate 2 | Task 4 |
| Gate 3 | Task 5 |
| Gate 4 | Tasks 3-5 (regression) |

---

## Phase 3: Session Spawning Refactor

### Task 6: In-Process Session Runner

**Files:**
- Modify: `src/golem/server.py`
- Modify: `tests/test_server.py`

- [ ] **Step 1: Create `run_session()` async function**

Add to `server.py`:

```python
async def run_session(
    spec_path: Path,
    project_root: Path,
    config: GolemConfig,
    event_bus: EventBus,
    golem_dir: Path,
) -> None:
```

This function runs the full Golem pipeline in-process:
1. Emit `SessionStart(spec_path=str(spec_path), complexity=..., config_snapshot=config.to_dict())`
2. Run conductor classification: `classify_spec(spec_path)` → `config.apply_complexity_profile()`
3. Call `run_planner(spec_path, golem_dir, config, project_root, event_bus=event_bus)`
4. If not `config.skip_tech_lead`: call `run_tech_lead(golem_dir, config, event_bus=event_bus)`
5. Emit `SessionComplete(status="awaiting_merge", cost_usd=..., duration_s=...)`
6. On exception: emit `SessionComplete(status="failed", error=str(e))`

- [ ] **Step 2: Replace subprocess spawning in session creation endpoint**

In the `POST /api/sessions` endpoint handler, replace:
```python
proc = await asyncio.create_subprocess_exec("uv", "run", "golem", "run", ...)
```
With:
```python
event_bus = EventBus(QueueBackend(state.event_queue), session_id=session_id)
task = asyncio.create_task(run_session(spec_path, project_root, config, event_bus, golem_dir))
state.background_tasks.append(task)
```

- [ ] **Step 3: Replace `monitor_process()` with task lifecycle**

Remove `monitor_process()` and `tail_progress_log()`. Session status is now driven by `run_session()` emitting `SessionComplete`.

Add a task done callback:
```python
def _on_session_done(task: asyncio.Task, state: SessionState, mgr: SessionManager) -> None:
    if task.cancelled():
        state.status = "failed"
    elif task.exception():
        state.status = "failed"
    else:
        state.status = "awaiting_merge"
```

- [ ] **Step 4: Update pause/resume/kill to use task handles**

- Kill: `task.cancel()` instead of `process.terminate()`
- Pause/Resume: these become no-ops or raise 400 for in-process tasks (SIGSTOP/SIGCONT don't apply to coroutines). Document this limitation.

- [ ] **Step 5: Add new SSE endpoint for typed events**

Add `GET /api/sessions/{id}/observe` — SSE stream that reads from `state.event_queue` and emits typed events:
```python
async def session_observe(session_id: str):
    state = session_mgr.get_session(session_id)
    if not state:
        return JSONResponse({"error": "not found"}, status_code=404)
    async def generate():
        bus = EventBus(QueueBackend(state.event_queue), session_id=session_id)
        async for event in bus.subscribe():
            yield format_sse("agent_event", event.to_dict())
    return StreamingResponse(generate(), media_type="text/event-stream")
```

Add `GET /api/sessions/{id}/agents` — returns current agent tree state as JSON.

- [ ] **Step 6: Write tests**

Add to `tests/test_server.py`:

1. `test_run_session_emits_lifecycle_events` — mock `run_planner`/`run_tech_lead`, call `run_session()` with EventBus, assert `SessionStart` and `SessionComplete` events
2. `test_run_session_failure_emits_failed` — mock `run_planner` to raise, assert `SessionComplete(status="failed")`
3. `test_session_creation_uses_in_process` — POST `/api/sessions`, verify no subprocess created (mock `run_session`)
4. `test_kill_session_cancels_task` — create session, DELETE it, verify task cancelled
5. `test_observe_endpoint_404` — GET `/api/sessions/fake/observe`, assert 404
6. `test_agents_endpoint_404` — GET `/api/sessions/fake/agents`, assert 404

- [ ] **Step 7: Commit**

```bash
git add src/golem/server.py tests/test_server.py
git commit -m "feat: in-process session spawning with EventBus-driven lifecycle"
```

#### Completion Gate

```bash
cd /f/Tools/Projects/golem-cli

# 1. Server tests pass (existing + new)
uv run pytest tests/test_server.py -v --tb=short 2>&1 | tail -1

# 2. New endpoints exist
uv run python -c "
from golem.server import create_app
from fastapi.routing import APIRoute
app = create_app()
routes = {r.path for r in app.routes if isinstance(r, APIRoute)}
assert '/api/sessions/{session_id}/observe' in routes, 'Missing /observe endpoint'
assert '/api/sessions/{session_id}/agents' in routes, 'Missing /agents endpoint'
print('ENDPOINTS: PASS')
"
```

Expected: all server tests passed, `ENDPOINTS: PASS`

---

## Phase 3 Completion Gate

**Phase 3 is NOT complete until every check below passes.**

### Gate 1: Server Tests

```bash
cd /f/Tools/Projects/golem-cli
uv run pytest tests/test_server.py -v --tb=short 2>&1 | tail -1
```

Expected: all passed, 0 failed

### Gate 2: New Endpoints Registered

```bash
cd /f/Tools/Projects/golem-cli
uv run python -c "
from golem.server import create_app
from fastapi.routing import APIRoute
app = create_app()
routes = {r.path for r in app.routes if isinstance(r, APIRoute)}
required = ['/api/sessions/{session_id}/observe', '/api/sessions/{session_id}/agents']
for r in required:
    assert r in routes, f'Missing: {r}'
print('ENDPOINTS: PASS')
"
```

Expected: `ENDPOINTS: PASS`

### Gate 3: No Subprocess Spawning in Session Creation

```bash
cd /f/Tools/Projects/golem-cli
uv run python -c "
import ast, sys
with open('src/golem/server.py', encoding='utf-8') as f:
    source = f.read()
# Should not contain create_subprocess_exec for session spawning
count = source.count('create_subprocess_exec')
# Allow 0 or check context — the subprocess call should be gone from session creation
print(f'SUBPROCESS_REFS: {count}')
if count == 0:
    print('NO_SUBPROCESS: PASS')
else:
    print('NO_SUBPROCESS: CHECK MANUALLY (may be in non-session context)')
"
```

Expected: `NO_SUBPROCESS: PASS` or `CHECK MANUALLY` with 0 refs in session creation path

### Gate 4: Full Test Suite

```bash
cd /f/Tools/Projects/golem-cli
uv run pytest -v --tb=short 2>&1 | tail -1
```

Expected: `[N] passed, 0 failed`

### Phase 3 Verdict

| Gate | Validates Tasks |
|------|----------------|
| Gate 1 | Task 6 |
| Gate 2 | Task 6 |
| Gate 3 | Task 6 |
| Gate 4 | Task 6 (regression) |

---

## Phase 4: Preflight System

### Task 7: Agent Topology and Preflight Analysis

**Files:**
- Modify: `src/golem/conductor.py`
- Modify: `src/golem/config.py`
- Modify: `src/golem/server.py`
- Create: `tests/test_preflight.py`

- [ ] **Step 1: Add `derive_agent_topology()` to conductor.py**

```python
def derive_agent_topology(config: GolemConfig) -> dict:
    """Derive the agent tree that would spawn for the given config."""
```

Returns a dict:
```python
{
    "planner": {
        "model": config.planner_model,
        "max_turns": config.planner_max_turns,
        "mcp_server": "golem",
        "mcp_tools": ["create_ticket", "update_ticket", "read_ticket", "list_tickets",
                       "run_qa", "create_worktree", "merge_branches", "commit_worktree"],
        "stall_warn": int(config.planner_max_turns * 0.6),
        "stall_kill": int(config.planner_max_turns * 0.8),
        "sub_agents": [
            {"role": "explorer", "model": "claude-haiku-4-5"},
            {"role": "researcher", "model": "claude-sonnet-4-6"},
        ],
    },
    "tech_lead": {
        "model": config.tech_lead_model,
        "max_turns": config.max_tech_lead_turns,
        "mcp_server": "golem",
        "mcp_tools": [...],  # same 8
        "stall_warn": int(config.max_tech_lead_turns * 0.3),
        "stall_kill": int(config.max_tech_lead_turns * 0.5),
    },
    "junior_dev": {
        "model": config.worker_model,
        "max_turns": config.max_worker_turns,
        "mcp_server": "golem-junior-dev",
        "mcp_tools": ["run_qa", "update_ticket", "read_ticket"],
        "stall_warn": int(config.max_worker_turns * 0.3),
        "stall_kill": int(config.max_worker_turns * 0.5),
        "dispatch_jitter_max": config.dispatch_jitter_max,
    },
    "skip_tech_lead": config.skip_tech_lead,
}
```

- [ ] **Step 2: Add conflict prediction**

Add to `conductor.py`:

```python
def predict_conflicts(spec_paths: list[Path]) -> list[dict]:
    """Parse specs for file references and predict cross-spec conflicts."""
```

For each spec, scan the markdown for file path patterns (`src/golem/*.py`, `tests/test_*.py`, backtick-quoted paths). Cross-reference the file sets. Return:
```python
[{"file": "cli.py", "specs": ["spec-2", "spec-5"]}, ...]
```

- [ ] **Step 3: Add environment check and cost estimate helpers to config.py**

Add to `config.py`:

```python
async def run_environment_checks(project_root: Path) -> list[dict]:
    """Run preflight environment checks."""
```

Returns list of `{"check": str, "passed": bool, "detail": str}` for: claude CLI, rg, git clean, stale .golem, port 9664.

```python
def estimate_cost(config: GolemConfig, history_dir: Path | None = None) -> dict:
    """Estimate cost from historical AGENT_COST data or model pricing."""
```

Returns `{"planner": {"min": float, "max": float}, "tech_lead": {...}, "junior_dev": {...}, "total": {"min": float, "max": float}, "based_on": int}`.

- [ ] **Step 4: Add enhanced preflight endpoint**

Update `POST /api/preflight` in `server.py` to return the full preflight analysis:

```python
{
    "topology": derive_agent_topology(config),
    "environment": await run_environment_checks(project_root),
    "cost_estimate": estimate_cost(config, golem_dir),
    "conflicts": predict_conflicts(spec_paths),  # if multiple specs
    "ready": all(c["passed"] for c in environment_checks),
}
```

- [ ] **Step 5: Write tests**

Create `tests/test_preflight.py`:

1. `test_derive_topology_standard` — STANDARD config → correct models, turns, tools per role
2. `test_derive_topology_trivial` — TRIVIAL config → haiku planner, skip_tech_lead=True
3. `test_derive_topology_critical` — CRITICAL config → opus everywhere, higher turn limits
4. `test_predict_conflicts_overlap` — two specs referencing same file → conflict entry
5. `test_predict_conflicts_no_overlap` — two specs with different files → empty list
6. `test_predict_conflicts_single_spec` — one spec → empty list (no conflicts possible)
7. `test_environment_checks_all_pass` — mock all tools present → all passed
8. `test_environment_checks_missing_rg` — mock rg missing → rg check fails
9. `test_cost_estimate_with_history` — provide mock progress.log with AGENT_COST lines → min/max/avg
10. `test_cost_estimate_no_history` — no history → fallback estimate based on model pricing
11. `test_preflight_endpoint_returns_full_analysis` — POST `/api/preflight`, assert response has all 4 sections

- [ ] **Step 6: Commit**

```bash
git add src/golem/conductor.py src/golem/config.py src/golem/server.py tests/test_preflight.py
git commit -m "feat: preflight analysis — topology, conflicts, environment, cost"
```

#### Completion Gate

```bash
cd /f/Tools/Projects/golem-cli

# 1. Preflight module imports
uv run python -c "
from golem.conductor import derive_agent_topology, predict_conflicts
from golem.config import run_environment_checks, estimate_cost
print('PREFLIGHT_IMPORTS: PASS')
"

# 2. Preflight tests pass
uv run pytest tests/test_preflight.py -v --tb=short 2>&1 | tail -1
```

Expected: `PREFLIGHT_IMPORTS: PASS`, `11 passed`

---

## Phase 4 Completion Gate

**Phase 4 is NOT complete until every check below passes.**

### Gate 1: Preflight Functions

```bash
cd /f/Tools/Projects/golem-cli
uv run python -c "
from golem.conductor import derive_agent_topology, predict_conflicts
from golem.config import GolemConfig, run_environment_checks, estimate_cost
config = GolemConfig()
topo = derive_agent_topology(config)
assert 'planner' in topo
assert 'tech_lead' in topo
assert 'junior_dev' in topo
assert len(topo['planner']['mcp_tools']) == 8
assert len(topo['junior_dev']['mcp_tools']) == 3
print('TOPOLOGY: PASS')
"
```

Expected: `TOPOLOGY: PASS`

### Gate 2: Preflight Tests

```bash
cd /f/Tools/Projects/golem-cli
uv run pytest tests/test_preflight.py -v --tb=short 2>&1 | tail -1
```

Expected: `11 passed`

### Gate 3: Full Test Suite

```bash
cd /f/Tools/Projects/golem-cli
uv run pytest -v --tb=short 2>&1 | tail -1
```

Expected: `[N] passed, 0 failed`

### Phase 4 Verdict

| Gate | Validates Tasks |
|------|----------------|
| Gate 1 | Task 7 |
| Gate 2 | Task 7 |
| Gate 3 | Task 7 (regression) |

---

## Phase 5: Dashboard UI

### Task 8: Observe Tab and Preflight Tab

**Files:**
- Modify: `src/golem/ui_template.html`
- Modify: `tests/test_ui.py`

- [ ] **Step 1: Add Observe tab to session detail view**

Add a new "Observe" tab (purple accent) to the per-session tab bar in `ui_template.html`. The tab content has a split layout:

**Left panel (240px) — Agent Tree:**
- Hierarchical view of agents: Planner → Tech Lead → Junior Devs
- Each node shows: role, status (running/done/queued icon), current turn count, model name
- Expandable section per node showing MCP tools list and stall state
- Updated live via `EventSource` listening to `/api/sessions/{id}/observe`
- On `agent_event` with type `agent_spawned` or `agent_complete`: update the tree

**Right panel (flex) — Event Stream:**
- Scrolling feed of events, newest at bottom, auto-scroll
- Each event line: `[timestamp] [role color-coded] event content`
- `AgentText`: show full text (the agent's thinking)
- `AgentToolCall`: show `tool: name(arg_preview)` in yellow, with expandable arguments
- `AgentToolResult`: indented result preview, collapsed by default
- `SubAgentSpawned`: distinct callout with subagent type and description
- `SkillInvoked`: show skill name
- `AgentStallWarning`: yellow warning banner
- `AgentStallKill`: red error banner
- LIVE indicator badge in the header
- Connect to `/api/sessions/{id}/observe` SSE endpoint

- [ ] **Step 2: Add Preflight tab**

Add a "Preflight" tab (green accent). Content loads from `POST /api/preflight` when the tab is opened (or when session is selected before launch):

**Agent Topology section (left):**
- Tree showing each role with model, turn budget, MCP server + tool count, stall thresholds
- Sub-agents listed under planner

**Environment + Cost section (right, stacked):**
- Environment checks as checkmark/cross list
- Cost estimate with per-role min/max and total

**Conflict prediction section (bottom):**
- Only shown when multiple specs are being launched
- Table: files as columns, specs as rows, M/NEW markers in cells
- Warning row highlighting predicted conflicts

- [ ] **Step 3: Enhance sidebar with live stats**

Update the session sidebar entries to show:
- Running sessions: current agent phase name, turn `N/max`, number of active junior devs, running cost `$X.XX`
- These update from the session's SSE event stream (listen for `AgentSpawned`, `AgentComplete`, `AgentTurnComplete`)

- [ ] **Step 4: Write tests**

Add to `tests/test_ui.py`:

1. `test_template_has_observe_tab` — assert `Observe` or `observe` in template HTML
2. `test_template_has_preflight_tab` — assert `Preflight` or `preflight` in template HTML
3. `test_template_has_agent_tree` — assert `agent-tree` or `agentTree` in template
4. `test_template_has_event_stream` — assert `event-stream` or `eventStream` in template
5. `test_template_connects_observe_sse` — assert `/api/sessions/` and `observe` in template (SSE connection)

- [ ] **Step 5: Commit**

```bash
git add src/golem/ui_template.html tests/test_ui.py
git commit -m "feat: Observe and Preflight tabs in multi-session dashboard"
```

#### Completion Gate

```bash
cd /f/Tools/Projects/golem-cli

# 1. Template checks
uv run python -c "
with open('src/golem/ui_template.html', encoding='utf-8') as f:
    html = f.read()
checks = {
    'observe': 'observe' in html.lower(),
    'preflight': 'preflight' in html.lower(),
    'agent_tree': 'agent-tree' in html or 'agentTree' in html or 'agent_tree' in html,
    'event_stream': 'event-stream' in html or 'eventStream' in html or 'event_stream' in html,
    'observe_sse': '/observe' in html,
}
for name, passed in checks.items():
    status = 'PASS' if passed else 'FAIL'
    print(f'{name.upper()}: {status}')
all_pass = all(checks.values())
print(f'TEMPLATE: {\"PASS\" if all_pass else \"FAIL\"}')
"

# 2. UI tests pass
uv run pytest tests/test_ui.py -v --tb=short 2>&1 | tail -1
```

Expected: all 5 template checks PASS, UI tests all passed

---

## Phase 5 Completion Gate

**Phase 5 is NOT complete until every check below passes.**

### Gate 1: Template Content

```bash
cd /f/Tools/Projects/golem-cli
uv run python -c "
with open('src/golem/ui_template.html', encoding='utf-8') as f:
    html = f.read().lower()
assert 'observe' in html, 'Missing observe tab'
assert 'preflight' in html, 'Missing preflight tab'
assert 'eventsource' in html, 'Missing EventSource for SSE'
print('TEMPLATE: PASS')
"
```

Expected: `TEMPLATE: PASS`

### Gate 2: No CDN References

```bash
cd /f/Tools/Projects/golem-cli
grep -ciE "cdn\.|unpkg\.|jsdelivr\.|cloudflare\." src/golem/ui_template.html
```

Expected: `0`

### Gate 3: UI Tests

```bash
cd /f/Tools/Projects/golem-cli
uv run pytest tests/test_ui.py -v --tb=short 2>&1 | tail -1
```

Expected: all passed, 0 failed

### Gate 4: Full Test Suite

```bash
cd /f/Tools/Projects/golem-cli
uv run pytest -v --tb=short 2>&1 | tail -1
```

Expected: `[N] passed, 0 failed`

### Phase 5 Verdict

| Gate | Validates Tasks |
|------|----------------|
| Gate 1 | Task 8 |
| Gate 2 | Task 8 |
| Gate 3 | Task 8 |
| Gate 4 | Task 8 (regression) |

---

## Final Completion Gate

**The spec is NOT complete until every check below passes.**

### Gate 1: All New Modules Import

```bash
cd /f/Tools/Projects/golem-cli
uv run python -c "
from golem.events import EventBus, QueueBackend, FileBackend, EVENT_TYPES
from golem.conductor import derive_agent_topology, predict_conflicts
from golem.config import run_environment_checks, estimate_cost
assert len(EVENT_TYPES) == 21
print('ALL_IMPORTS: PASS')
"
```

### Gate 2: EventBus Tests

```bash
cd /f/Tools/Projects/golem-cli
uv run pytest tests/test_events.py -v --tb=short 2>&1 | tail -1
```

### Gate 3: Preflight Tests

```bash
cd /f/Tools/Projects/golem-cli
uv run pytest tests/test_preflight.py -v --tb=short 2>&1 | tail -1
```

### Gate 4: Observe/Agents Endpoints Exist

```bash
cd /f/Tools/Projects/golem-cli
uv run python -c "
from golem.server import create_app
from fastapi.routing import APIRoute
app = create_app()
routes = {r.path for r in app.routes if isinstance(r, APIRoute)}
assert '/api/sessions/{session_id}/observe' in routes
assert '/api/sessions/{session_id}/agents' in routes
print('ENDPOINTS: PASS')
"
```

### Gate 5: Template Has Observe + Preflight

```bash
cd /f/Tools/Projects/golem-cli
uv run python -c "
with open('src/golem/ui_template.html', encoding='utf-8') as f:
    html = f.read().lower()
assert 'observe' in html
assert 'preflight' in html
print('TEMPLATE: PASS')
"
```

### Gate 6: Full Test Suite

```bash
cd /f/Tools/Projects/golem-cli
uv run pytest -v --tb=short 2>&1 | tail -1
```

Expected: `[N] passed, 0 failed` where N >= 453 + all new tests

### Final Verdict

| Gate | Validates |
|------|-----------|
| Gate 1 | Events module, preflight functions |
| Gate 2 | EventBus core (Phase 1) |
| Gate 3 | Preflight analysis (Phase 4) |
| Gate 4 | Server endpoints (Phase 3) |
| Gate 5 | Dashboard UI (Phase 5) |
| Gate 6 | Everything (regression) |

---

## Files Summary

| File | Action | Phase |
|---|---|---|
| `src/golem/events.py` | NEW | 1 |
| `tests/test_events.py` | NEW | 1 |
| `tests/test_preflight.py` | NEW | 4 |
| `src/golem/progress.py` | MODIFY | 1 |
| `src/golem/supervisor.py` | MODIFY | 2 |
| `src/golem/tools.py` | MODIFY | 2 |
| `src/golem/planner.py` | MODIFY | 2 |
| `src/golem/tech_lead.py` | MODIFY | 2 |
| `src/golem/writer.py` | MODIFY | 2 |
| `src/golem/cli.py` | MODIFY | 2 |
| `src/golem/server.py` | MODIFY | 3 |
| `src/golem/conductor.py` | MODIFY | 4 |
| `src/golem/config.py` | MODIFY | 4 |
| `src/golem/ui_template.html` | MODIFY | 5 |
| `tests/test_progress.py` | MODIFY | 1 |
| `tests/test_supervisor.py` | MODIFY | 2 |
| `tests/test_tools.py` | MODIFY | 2 |
| `tests/test_server.py` | MODIFY | 3 |
| `tests/test_ui.py` | MODIFY | 5 |

## Out of Scope

- Historical event replay from completed sessions (future: load events.jsonl for post-mortem)
- Agent cost alerting / budget limits
- Token-level streaming (word-by-word agent output)
- Custom event filters in the UI (future: filter by role, event type)
- OpenTelemetry / external tracing integration
