# Hybrid Ticket Architecture

## Problem

The current pipeline has two disconnected ticket creation paths:

1. **Planner** creates 1 handoff ticket ("Tech Lead: Execute overview.md") with no `pipeline_stage`, no `edict_id`, no `depends_on`
2. **Tech Lead** reads the handoff, then creates N work tickets from scratch — re-doing decomposition the planner already performed in `task-NNN.md` files

This causes:
- **Board blindness** — no tickets visible until Tech Lead creates them (minutes into execution)
- **Duplicate planning** — Tech Lead spends turns reading plans and re-decomposing into tickets
- **Handoff ticket is ceremony** — TICKET-001 isn't real work, just a relay that sits in "pending" or "tech_lead" column awkwardly
- **Pipeline has no visibility** — `pipeline.py` only tracks the handoff ticket; all writer tickets are invisible to the coordinator
- **`depends_on`, `edict_id`, `pipeline_stage` are declared but unused** — the schema supports a full pipeline board but nobody populates it

## Design: Skeleton Tickets from Planner, Enrichment by Tech Lead

### Core Idea

The planner creates **one ticket per task** (not just one handoff), with `pipeline_stage`, `depends_on`, and `edict_id` set. These are "skeleton" tickets — they have the plan reference and acceptance criteria but NOT the pre-loaded file contents that writers need.

The Tech Lead's job changes from "decompose plans into tickets" to "enrich skeleton tickets with file contents, create worktrees, and dispatch writers." It keeps full agency to split, merge, or reject tickets based on what it discovers.

### Ticket Flow (Before vs After)

**Before:**
```
Planner → 1 handoff ticket (TICKET-001: "Execute overview.md")
  Tech Lead → N work tickets (TICKET-002..N: actual tasks)
    Junior Devs → work on TICKET-002..N
```

**After:**
```
Planner → N skeleton tickets (TICKET-001..N: one per task-NNN.md)
  Tech Lead → enriches TICKET-001..N with file contents, worktree paths
    Junior Devs → work on TICKET-001..N (same tickets, now enriched)
```

### What Changes

#### 1. Planner Prompt (`prompts/planner.md`)

Replace the single handoff ticket instruction with:

```markdown
## Phase 4: Create Tickets

Create ONE ticket per task plan file using `mcp__golem__create_ticket`:

For each `task-NNN.md` you wrote:
- type: "task"
- title: the task title from the plan file
- assigned_to: "tech_lead" (Tech Lead will enrich and dispatch to writers)
- plan_file: path to the task-NNN.md file
- references: [paths to relevant reference files]
- blueprint: relevant excerpt from overview.md for this task
- acceptance: acceptance criteria from the task plan
- qa_checks: QA validation commands from the task plan
- depends_on: [ticket IDs this task depends on] (use IDs from previously created tickets)
- pipeline_stage: "planner" (will advance as agents pick up the work)
- edict_id: "{edict_id}"

Create tickets in dependency order so you can reference earlier ticket IDs in `depends_on`.

After creating all task tickets, create ONE summary ticket:
- type: "review"
- title: "Integration: merge all branches, run QA, create PR"
- assigned_to: "tech_lead"  
- depends_on: [all task ticket IDs]
- pipeline_stage: "planner"
- edict_id: "{edict_id}"

This summary ticket is what the pipeline uses to track overall completion.
```

New template variable: `{edict_id}` — injected by `run_planner()` from `self._edict.id`.

#### 2. MCP `create_ticket` Schema (`tools.py`)

Add optional fields to `_handle_create_ticket`:

```python
# New optional fields in input_schema:
"pipeline_stage": {"type": "string", "description": "Board column: planner, tech_lead, junior_dev, qa, done, failed"},
"edict_id": {"type": "string", "description": "Parent edict ID (e.g. EDICT-001)"},
"depends_on": {"type": "array", "items": {"type": "string"}, "description": "Ticket IDs this ticket depends on"},
```

Handler creates the ticket with these fields set (currently hard-coded to empty).

#### 3. Pipeline Coordinator (`pipeline.py`)

Replace the single-ticket tracking with multi-ticket awareness:

```python
# After planner completes:
tickets = await self._ticket_store.list_tickets()
task_tickets = [t for t in tickets if t.type == "task"]
summary_ticket = next((t for t in tickets if t.type == "review"), None)

# Use summary ticket ID for Tech Lead dispatch (or last ticket as fallback)
dispatch_ticket_id = summary_ticket.id if summary_ticket else planner_result.ticket_id

# Advance ALL planner tickets to tech_lead stage
for ticket in tickets:
    if ticket.pipeline_stage == "planner":
        await self._ticket_store.update(
            ticket.id, status="pending",
            note="Advancing to Tech Lead",
            pipeline_stage="tech_lead",
        )
```

After Tech Lead completes, the pipeline can now count real ticket outcomes:
```python
tickets = await self._ticket_store.list_tickets()
result.tickets_passed = sum(1 for t in tickets if t.status == "done")
result.tickets_failed = sum(1 for t in tickets if t.status == "failed")
```

This already works — but now it counts real work tickets, not just the handoff.

#### 4. Tech Lead Prompt (`prompts/tech_lead.md`)

Change Phase 3 from "Create Writer Tickets" to "Enrich Existing Tickets":

```markdown
## Phase 3: Enrich Tickets & Create Worktrees

Tickets already exist from the planner. For each task ticket:

1. Read the ticket via `mcp__golem__read_ticket`
2. Read the plan file from `ticket.context.plan_file`
3. Pre-load all referenced files into `context.files` via `mcp__golem__update_ticket`
4. Create the worktree for the task's parallel group
5. Update ticket: status="in_progress", pipeline_stage="junior_dev", agent_id="<writer-id>"
6. Dispatch the writer with the enriched ticket

You MAY split a ticket into sub-tickets if the task is too large.
You MAY merge tickets if they're trivially small.
You MAY create NEW tickets for work the planner missed.
You MAY mark a ticket as "failed" with a note if the plan is wrong.
```

#### 5. Planner MCP Server (`tools.py` / `planner.py`)

The planner currently gets a limited MCP server (`create_golem_planner_mcp_server`) without `update_ticket`. No change needed — planner only creates, doesn't update.

But the planner needs to know what `edict_id` to set. Inject it via `get_session_context`:

```python
# In _handle_get_session_context, add:
"edict_id": context.edict_id,  # New field on ToolContext
```

#### 6. PlannerResult Changes

`PlannerResult.ticket_id` becomes `PlannerResult.ticket_ids: list[str]` — all created ticket IDs, with the summary/integration ticket last.

For backward compat, keep `ticket_id` as a property that returns the last ID:
```python
@property
def ticket_id(self) -> str:
    return self.ticket_ids[-1] if self.ticket_ids else ""
```

#### 7. `depends_on` Enforcement (Optional, Phase 2)

Currently `depends_on` is declared but never enforced. For the first pass, it's informational only — the Tech Lead reads it to decide wave ordering. In a future phase, the pipeline coordinator could enforce it:

```python
# Future: block ticket dispatch until dependencies are done
async def _can_dispatch(self, ticket: Ticket) -> bool:
    for dep_id in ticket.depends_on:
        dep = await self._ticket_store.read(dep_id)
        if dep.status != "done":
            return False
    return True
```

### What Stays the Same

- **Tech Lead still has full agency** — can split, merge, reject, create new tickets
- **Writer ticket lifecycle** — unchanged (pending → in_progress → approved → done)
- **QA flow** — unchanged
- **Worktree management** — unchanged (Tech Lead creates worktrees)
- **TRIVIAL path** — unchanged (planner creates 1 task ticket, pipeline dispatches Junior Dev directly)
- **Board columns** — unchanged (planner, tech_lead, junior_dev, qa, done, failed)
- **Self-healing fallbacks** — planner fallback ticket creation still works if MCP fails

### Board Impact

**Before:** Board is empty during planning, shows 1 pending handoff in tech_lead column, then work tickets appear late.

**After:** Board shows N skeleton tickets in "planner" column during planning. They shift to "tech_lead" when planning completes. Then to "junior_dev" as Tech Lead dispatches writers. Full pipeline visibility from the start.

### Migration / Backward Compatibility

- `PlannerResult.ticket_id` property preserves the old interface
- Pipeline falls back to single-ticket behavior if only 1 ticket exists
- `create_ticket` MCP schema additions are optional fields — old prompts still work
- No database migration needed — tickets are JSON files

### Scope

| Item | Phase |
|---|---|
| MCP `create_ticket` schema: add `pipeline_stage`, `edict_id`, `depends_on` | 1 |
| Planner prompt: create N skeleton tickets | 1 |
| `PlannerResult.ticket_ids` + backward-compat property | 1 |
| Pipeline: advance all planner tickets to tech_lead | 1 |
| Tech Lead prompt: enrich existing tickets instead of creating new ones | 1 |
| `edict_id` wired through `ToolContext` → `get_session_context` | 1 |
| `depends_on` enforcement in pipeline coordinator | 2 |
| Pipeline-level wave scheduling from ticket DAG | 2 |
| Board: show dependency arrows between tickets | 2 |

### Risks

1. **Planner token cost increases** — creating N tickets costs more turns than 1. Mitigate: skeleton tickets are small (just plan_file + acceptance, no file contents).
2. **Planner hallucination on depends_on** — it might set wrong dependency IDs. Mitigate: `depends_on` is advisory in Phase 1, not enforced.
3. **Tech Lead confusion** — existing tickets vs. creating new ones. Mitigate: clear prompt instructions, "enrich existing" not "create new."
4. **TRIVIAL path regression** — planner creates 1 ticket, pipeline dispatches directly. Should work unchanged since it's still 1 ticket.
5. **Prompt length** — adding ticket creation instructions increases planner prompt. Mitigate: replace the existing handoff section, don't add alongside it.

### Test Plan

- Planner creates N tickets with correct `pipeline_stage="planner"` and `edict_id`
- Pipeline advances all tickets from planner → tech_lead stage
- Tech Lead enriches existing tickets instead of creating duplicates
- Board shows tickets in correct columns at each phase
- TRIVIAL path still works (1 ticket, direct Junior Dev dispatch)
- Fallback ticket creation still works when MCP fails
- `PlannerResult.ticket_id` backward compat returns last ticket ID
- `depends_on` is stored and returned but not enforced (Phase 1)
