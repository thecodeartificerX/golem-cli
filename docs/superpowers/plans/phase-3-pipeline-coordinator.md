# Phase 3: Pipeline Coordinator

## Gotchas
- The current pipeline is assembled inline in `cli.py:_run_async()` (lines ~403-509) — not in a reusable module. `pipeline.py` extracts and formalizes this
- `run_session()` in `server.py` is the in-process entry point for the current pipeline — `pipeline.py` replaces it
- The planner enriches the Edict (writes plans, references), but doesn't produce ticket cards — the Tech Lead does. The Planner column on the board shows an activity indicator, not tickets
- `session.py:create_session_dir()` creates the scaffold — the new `create_edict_dir()` should create the same structure but namespaced under edict ID
- `recovery.py` has `needs_attention` as a concept but no code path to set it — this phase adds the escalation path
- `EventBus` must be threaded through the entire pipeline for observability
- `conductor.py:classify_spec()` still applies — complexity tiers determine which pipeline stages are skipped

## Files
```
src/golem/
├── pipeline.py           # CREATE — Pipeline coordinator (Edict through full agent pipeline)
├── session.py            # MODIFY — add create_edict_dir(), keep create_session_dir() for backward compat
├── recovery.py           # MODIFY — add needs_attention escalation path
tests/
├── test_pipeline.py      # CREATE — Pipeline coordinator tests
├── test_session.py       # MODIFY — test new create_edict_dir()
```

---

## Task 3.1: Create Pipeline Coordinator

**Skills to load:** `superpowers:test-driven-development`

**Architecture notes:**

`PipelineCoordinator` class — the single orchestration entry point for processing an Edict:

```
class PipelineCoordinator:
    def __init__(
        self,
        edict: Edict,
        edict_store: EdictStore,
        ticket_store: TicketStore,
        config: GolemConfig,
        project_root: Path,
        golem_dir: Path,
        event_bus: EventBus | None = None,
    ): ...

    async def run(self) -> PipelineResult: ...
    async def pause(self) -> None: ...
    async def resume(self) -> None: ...
    async def kill(self) -> None: ...
    async def send_guidance(self, text: str) -> None: ...
```

`PipelineResult` dataclass:
```
edict_id: str
status: str           — final edict status
pr_url: str | None
total_cost_usd: float
duration_s: float
tickets_passed: int
tickets_failed: int
error: str | None
```

`run()` orchestration flow:
1. Emit `EdictCreated` event
2. Update edict status to `planning`
3. Run complexity classification (`classify_spec()` from `conductor.py`)
4. Apply complexity profile to config
5. Detect infrastructure checks
6. **Planner stage:** Call `run_planner()` — enriches edict with plans/references/tickets
7. Update edict status to `in_progress`, emit `EdictUpdated`
8. **Route to execution path** based on config:
   - `config.orchestrator_enabled` → `WaveExecutor.run()`
   - `config.skip_tech_lead` (TRIVIAL) → direct `spawn_junior_dev()`
   - Default → `run_tech_lead()`
9. On success: update edict to `done`, set `pr_url`
10. On recoverable failure (after retries exhausted): update edict to `needs_attention`, emit `EdictNeedsAttention`
11. On unrecoverable failure: update edict to `failed`

`pause()` / `resume()` / `kill()`:
- Use `asyncio.Event` pattern (same as `SessionState.resume_event`)
- Store a `_resume_event: asyncio.Event` and `_current_task: asyncio.Task | None`
- `kill()` cancels the task

`send_guidance()`:
- Creates a guidance ticket in the ticket store (same as current server behavior)

**Design constraint:** The pipeline must update `edict.ticket_ids` as the Tech Lead creates tickets. This means the Tech Lead's MCP `create_ticket` tool handler needs to also update the parent Edict's `ticket_ids` list. This is wired via the MCP tool context, not by modifying the tool itself — the pipeline passes an `on_ticket_created` callback.

**Design constraint:** The pipeline must update `edict.cost_usd` incrementally as agents report costs. Use the `AgentComplete` event to accumulate costs.

**Files to create:**
- `src/golem/pipeline.py` — PipelineCoordinator, PipelineResult

**Validation command:** `uv run pytest tests/test_pipeline.py -v`

**Tests to write (`test_pipeline.py`):**
- Pipeline creation with valid Edict
- Pipeline run with mocked planner + tech_lead (verify status transitions: pending → planning → in_progress → done)
- Pipeline run with planner failure (verify status: planning → failed)
- Pipeline run with needs_attention escalation (mock recovery exhausted → needs_attention)
- Pause/resume lifecycle (verify resume_event cleared/set)
- Kill cancels the running task
- Guidance creates a ticket in the store
- Cost aggregation from AgentComplete events
- Ticket IDs accumulated on edict as tickets are created

---

## Task 3.2: Add create_edict_dir() to session.py

**Skills to load:** None (minor addition)

**Architecture notes:**

Add a new function alongside `create_session_dir()`:

```python
def create_edict_dir(edicts_dir: Path, edict_id: str) -> Path:
    """Create the directory scaffold for an edict."""
    edict_dir = edicts_dir / edict_id
    edict_dir.mkdir(parents=True, exist_ok=True)
    for sub in ("tickets", "plans", "research", "references", "reports", "worktrees"):
        (edict_dir / sub).mkdir(exist_ok=True)
    return edict_dir
```

This mirrors `create_session_dir()` but:
- No `spec.md` copy (the edict body is stored in the Edict JSON)
- No `session.json` (edicts have their own JSON in the edicts dir)
- Subdirectory structure is the same (tickets, plans, research, references, reports, worktrees)

Keep `create_session_dir()` unchanged for backward compatibility.

**Files to modify:**
- `src/golem/session.py` — add `create_edict_dir()`

**Validation command:** `uv run pytest tests/test_session.py -v`

**Tests to write/update:**
- `create_edict_dir()` creates all expected subdirectories
- Idempotent (calling twice doesn't error)

---

## Task 3.3: Add needs_attention Escalation to Recovery

**Skills to load:** None (targeted modification)

**Architecture notes:**

Currently `RecoveryCoordinator.run_with_recovery()` raises `RecoveryExhausted` when all retries are spent. The pipeline catches this and sets edict status to `needs_attention`.

However, the recovery coordinator should also emit an event when it escalates. Add to `recovery.py`:

- When `RecoveryExhausted` is about to be raised, if `event_bus` is available, emit `EdictNeedsAttention(edict_id=..., reason=..., ticket_id=...)` before raising
- The `edict_id` comes from a new optional parameter on `run_with_recovery()`: `edict_id: str = ""`
- If `edict_id` is empty (backward compat), skip the event emission

This is a minimal touch — the actual `needs_attention` status update happens in the pipeline coordinator.

**Files to modify:**
- `src/golem/recovery.py` — add `edict_id` parameter, emit event before raising RecoveryExhausted

**Validation command:** `uv run pytest tests/test_recovery.py -v`

**Tests to write/update:**
- RecoveryExhausted emits EdictNeedsAttention event when edict_id is provided
- RecoveryExhausted does NOT emit event when edict_id is empty (backward compat)
