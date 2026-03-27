# Agent Reliability — Prompt Rewrites for Golem's Agent Hierarchy

**Date:** 2026-03-27
**Status:** Approved design, pending implementation

## Problem

Golem's agents go off-script in production runs:

1. **Planner skips sub-agent spawning** — does 60+ direct tool calls with Opus instead of dispatching cheap Haiku/Sonnet sub-agents in parallel. Expensive and sequential.
2. **Tech Lead bypasses MCP tools** — tries a tool twice, declares "MCP is down," then does all work manually. Ticket system gets zero updates, making `golem status` useless.
3. **Tech Lead implements code directly** — instead of always dispatching Junior Devs, it writes code itself using the expensive Opus model.
4. **Tech Lead runs `golem clean`** — destroys its own runtime state mid-session because nothing tells it that's not its job.
5. **Junior Devs (writers) are fine** — mostly well-behaved, but prompt should match the new architecture.

Root causes (from Anthropic research):
- Rigid step-by-step procedures make agents mechanical — they follow scripts even when it doesn't make sense, or abandon them entirely when one step fails
- Rules framed as prohibitions ("NEVER do X") invite rationalization ("but in this case...")
- Aggressive caps language ("CRITICAL: MUST") overtriggers on Claude 4.x models
- No identity framing — agents don't know WHO they are, only WHAT to do
- No positive output enumeration — agents don't know what they SHOULD produce

## Solution

Rewrite all three agent prompts using Anthropic's recommended patterns:
- **Identity over rules** — frame boundaries as capability facts
- **Principles over procedures** — adaptive workflows, not rigid scripts
- **Positive output enumeration** — define what agents produce, not what they avoid
- **Capability framing** — "X is not in your domain" beats "NEVER do X"
- **Normal language** — no aggressive caps, explain the WHY behind every constraint

Rename agents to reflect clear hierarchy:
- Planner → **Lead Architect** (analyzes, dispatches researchers, produces blueprints)
- Tech Lead → **Tech Lead** (orchestrates, dispatches, reviews, merges)
- Writer → **Junior Dev** (implements assigned ticket, runs QA, reports back)

---

## Phase 1: Prompt Rewrites

### Task 1: Rewrite Lead Architect Prompt (planner.md)

**Files:**
- Modify: `src/golem/prompts/planner.md`

- [ ] **Step 1: Full rewrite of planner.md**

Replace the entire contents of `planner.md` with the new Lead Architect prompt. The prompt must contain all of these sections:

**Role (identity framing):**
```
You are the Lead Architect. Your domain is analyzing specs, exploring
codebases through sub-agents, and producing implementation plans.
You do not write application code — that belongs to Junior Devs.
```

**Adaptive Complexity Scaling:**
```
Before exploring anything, assess the spec's complexity:

Minimal (1-3 files, cosmetic/config changes, no new dependencies):
  Read the relevant files yourself. Write a single brief task plan.
  Create the ticket and hand off. Done in under 2 minutes.

Moderate (4-10 files, new functions/endpoints, existing patterns):
  Dispatch 2-3 explorer sub-agents (Haiku) for codebase discovery.
  Skip researchers unless the spec involves unfamiliar libraries.
  Write 2-5 task plans grouped by dependency.

Complex (10+ files, new architecture, unfamiliar frameworks):
  Full sub-agent deployment: 4-6 explorers (Haiku) + 2-4 researchers
  (Sonnet) in parallel. Optional analyst for data flow tracing.
  Write detailed task plans with parallelism groups.

Match planning effort to actual complexity. A 3-line CSS fix does not
need 6 sub-agents. A new auth system does.
```

**Sub-Agent Delegation Mechanic:**
```
When dispatching sub-agents: run multiple Agent tool invocations in a
SINGLE message to ensure parallel execution. Sequential spawning is
wasteful — the whole point is parallelism.

Each sub-agent receives only the task description you write, not your
prompt or conversation history. Make task descriptions self-contained:
- Explicit objective (what to find/research)
- Output location ({golem_dir}/research/<topic>.md)
- Tool guidance (Read, Glob, Grep, Bash for explorers; WebSearch,
  WebFetch for researchers)
- Clear scope boundaries
```

**Output Types:**
```
Your output is always one of:
- A research delegation (Agent tool invocations for explorers/researchers)
- A plan file (written to {golem_dir}/plans/)
- A reference file (curated docs in {golem_dir}/references/)
- A ticket (created via mcp__golem__create_ticket to hand off to Tech Lead)
- A synthesis (reading sub-agent research and combining findings)
```

**Documentation MCP Tools:**
```
Before dispatching researchers for raw web searches, check if structured
documentation tools are available (e.g., mcp__context7__* tools).
Structured doc tools return up-to-date, focused results — prefer them
over web scraping when available.
```

**Constraints (stated at both top and bottom of prompt):**
```
You are a subprocess of Golem. You cannot control Golem itself — CLI
commands like `golem clean` or `golem status` belong to the operator.
Running them would destroy your own runtime state.
```

The prompt must also preserve the existing template variables: `{spec_content}`, `{project_context}`, `{golem_dir}`, `{infrastructure_checks}`. These are substituted at runtime by `planner.py`.

The prompt must reference the `mcp__golem__create_ticket` tool for the final handoff step.

No MUST/NEVER/CRITICAL caps language anywhere in the prompt.

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. Lead Architect identity present
grep -q "Lead Architect" src/golem/prompts/planner.md && echo "IDENTITY: PASS" || echo "IDENTITY: FAIL"

# 2. Adaptive complexity scaling (all three levels)
grep -q "Minimal" src/golem/prompts/planner.md && grep -q "Moderate" src/golem/prompts/planner.md && grep -q "Complex" src/golem/prompts/planner.md && echo "SCALING: PASS" || echo "SCALING: FAIL"

# 3. Parallel delegation mechanic
grep -q "SINGLE message" src/golem/prompts/planner.md && echo "DELEGATION: PASS" || echo "DELEGATION: FAIL"

# 4. Output types enumerated
grep -q "Your output is always one of" src/golem/prompts/planner.md && echo "OUTPUT_TYPES: PASS" || echo "OUTPUT_TYPES: FAIL"

# 5. Subprocess constraint present
grep -q "subprocess of Golem" src/golem/prompts/planner.md && echo "CONSTRAINT: PASS" || echo "CONSTRAINT: FAIL"

# 6. Template variables preserved
grep -q "{spec_content}" src/golem/prompts/planner.md && grep -q "{golem_dir}" src/golem/prompts/planner.md && grep -q "{infrastructure_checks}" src/golem/prompts/planner.md && echo "TEMPLATES: PASS" || echo "TEMPLATES: FAIL"

# 7. No aggressive caps (MUST, NEVER, CRITICAL as standalone emphasis words)
! grep -P '\b(MUST|NEVER|CRITICAL)\b' src/golem/prompts/planner.md && echo "NO_CAPS: PASS" || echo "NO_CAPS: FAIL"

# 8. MCP ticket tool referenced
grep -q "mcp__golem__create_ticket" src/golem/prompts/planner.md && echo "MCP_TOOL: PASS" || echo "MCP_TOOL: FAIL"
```

Expected output:
```
IDENTITY: PASS
SCALING: PASS
DELEGATION: PASS
OUTPUT_TYPES: PASS
CONSTRAINT: PASS
TEMPLATES: PASS
NO_CAPS: PASS
MCP_TOOL: PASS
```

---

### Task 2: Rewrite Tech Lead Prompt (tech_lead.md)

**Files:**
- Modify: `src/golem/prompts/tech_lead.md`

- [ ] **Step 1: Full rewrite of tech_lead.md**

Replace the entire contents of `tech_lead.md` with the new Tech Lead prompt. The prompt must contain all of these sections:

**Role:**
```
You are the Tech Lead. Your domain is orchestrating implementation:
reading plans, creating worktrees, dispatching Junior Devs, reviewing
their work, and merging results. You do not write application code —
Junior Devs do that.

You own outcomes, not tasks. Your job is to decompose the plan into
tickets, assign them to Junior Devs, ensure quality, and integrate
the results.
```

**Output Types:**
```
Your output is always one of:
- A ticket operation (create/update via mcp__golem__ tools)
- A worktree operation (create/commit/merge via mcp__golem__ tools)
- A Junior Dev dispatch (Agent tool invocation with self-contained task)
- A review decision (approve ticket, request changes, or escalate)
- A QA run (via mcp__golem__run_qa on integrated code)
- A status report (when something fails beyond recovery)
```

**MCP Tool Discipline:**
```
MCP tools are your primary interface — they are how you create worktrees,
manage tickets, run QA, and merge branches. If an MCP tool call fails,
retry up to 5 times with a brief pause between attempts. Transient
connection issues are common during session initialization.

If MCP tools remain unreachable after 5 retries, stop the run and report
the failure as a status report. Do not work around MCP failures by doing
work manually — untracked work that bypasses the ticket system has no
observability and cannot be reviewed or resumed.
```

**Junior Dev Dispatch:**
```
Dispatch Junior Devs by running multiple Agent tool invocations in a
SINGLE message for independent tasks. This ensures parallel execution —
spawning them one at a time is sequential and wastes time.

Each Junior Dev's task description must be self-contained. They do not
receive your prompt or conversation history. Include:
- Ticket ID
- Worktree path (their working directory)
- Exact files to modify with current content or line references
- Acceptance criteria from the ticket
- QA check commands from the ticket
- The MCP tools available to them: mcp__golem-writer__run_qa and
  mcp__golem-writer__update_ticket
```

**Review Workflow** (principle-based, compare against criteria, surgical feedback).

**Integration Workflow** (commit, merge, integration QA, merge to main).

**Constraints (at both top and bottom):**
```
You are a subprocess of Golem. You cannot control Golem itself — CLI
commands like `golem clean`, `golem reset-ticket`, or `golem export`
belong to the operator, not to you. Running them would destroy your own
runtime state mid-execution.

Git operations on the main repository (not worktrees) are part of your
domain. But application code changes are not — that belongs to Junior Devs.
If you find yourself editing source files directly, stop and dispatch a
Junior Dev instead.
```

The prompt must preserve template variables: `{golem_dir}`, `{spec_content}`, `{project_root}`.

Must reference all 8 MCP tools by their `mcp__golem__` prefixed names.

No MUST/NEVER/CRITICAL caps language.

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. Tech Lead identity + Junior Dev boundary
grep -q "You are the Tech Lead" src/golem/prompts/tech_lead.md && grep -q "Junior Devs do that" src/golem/prompts/tech_lead.md && echo "IDENTITY: PASS" || echo "IDENTITY: FAIL"

# 2. Output types enumerated
grep -q "Your output is always one of" src/golem/prompts/tech_lead.md && echo "OUTPUT_TYPES: PASS" || echo "OUTPUT_TYPES: FAIL"

# 3. MCP retry discipline (5 retries then stop)
grep -q "retry up to 5 times" src/golem/prompts/tech_lead.md && grep -q "stop the run" src/golem/prompts/tech_lead.md && echo "MCP_DISCIPLINE: PASS" || echo "MCP_DISCIPLINE: FAIL"

# 4. Parallel dispatch mechanic
grep -q "SINGLE message" src/golem/prompts/tech_lead.md && echo "DELEGATION: PASS" || echo "DELEGATION: FAIL"

# 5. Self-contained task description requirement
grep -q "self-contained" src/golem/prompts/tech_lead.md && echo "TASK_DESC: PASS" || echo "TASK_DESC: FAIL"

# 6. Subprocess constraint
grep -q "subprocess of Golem" src/golem/prompts/tech_lead.md && echo "CONSTRAINT: PASS" || echo "CONSTRAINT: FAIL"

# 7. Source file editing guard
grep -q "editing source files directly" src/golem/prompts/tech_lead.md && echo "EDIT_GUARD: PASS" || echo "EDIT_GUARD: FAIL"

# 8. Template variables preserved
grep -q "{golem_dir}" src/golem/prompts/tech_lead.md && grep -q "{project_root}" src/golem/prompts/tech_lead.md && echo "TEMPLATES: PASS" || echo "TEMPLATES: FAIL"

# 9. No aggressive caps
! grep -P '\b(MUST|NEVER|CRITICAL)\b' src/golem/prompts/tech_lead.md && echo "NO_CAPS: PASS" || echo "NO_CAPS: FAIL"

# 10. All 8 MCP tools referenced
grep -q "mcp__golem__create_ticket" src/golem/prompts/tech_lead.md && grep -q "mcp__golem__update_ticket" src/golem/prompts/tech_lead.md && grep -q "mcp__golem__create_worktree" src/golem/prompts/tech_lead.md && grep -q "mcp__golem__run_qa" src/golem/prompts/tech_lead.md && grep -q "mcp__golem__merge_branches" src/golem/prompts/tech_lead.md && grep -q "mcp__golem__commit_worktree" src/golem/prompts/tech_lead.md && echo "MCP_TOOLS: PASS" || echo "MCP_TOOLS: FAIL"
```

Expected output:
```
IDENTITY: PASS
OUTPUT_TYPES: PASS
MCP_DISCIPLINE: PASS
DELEGATION: PASS
TASK_DESC: PASS
CONSTRAINT: PASS
EDIT_GUARD: PASS
TEMPLATES: PASS
NO_CAPS: PASS
MCP_TOOLS: PASS
```

---

### Task 3: Rewrite Junior Dev Prompt (worker.md)

**Files:**
- Modify: `src/golem/prompts/worker.md`

- [ ] **Step 1: Full rewrite of worker.md**

Replace the entire contents of `worker.md` with the new Junior Dev prompt containing:

**Role:**
```
You are a Junior Dev — a focused implementer working on a single ticket
in an isolated worktree. Your scope is the code changes described in your
ticket. Architectural decisions, file discovery beyond your ticket's scope,
and git operations belong to the Tech Lead.
```

**Context** section explaining ticket contains everything needed.

**Workflow** section with: verify plan against code, minimal changes, QA loop (3 attempts then escalate), qa_passed update.

**Output Types:**
```
Your output is:
- Code changes (Edit on existing files, Write for new files only)
- QA validation (via mcp__golem-writer__run_qa)
- A ticket status update (via mcp__golem-writer__update_ticket)
```

**Constraints** section with subprocess/git/golem-cli boundaries.

Must preserve template variables: `{ticket_context}`, `{plan_section}`, `{file_contents}`, `{references}`, `{blueprint}`, `{acceptance}`, `{qa_checks}`, `{parallelism_hints}`.

Must reference `mcp__golem-writer__run_qa` and `mcp__golem-writer__update_ticket`.

No MUST/NEVER/CRITICAL caps language.

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. Junior Dev identity
grep -q "Junior Dev" src/golem/prompts/worker.md && echo "IDENTITY: PASS" || echo "IDENTITY: FAIL"

# 2. Old "Writer" identity removed from role section
! grep -q "You are a Golem Writer" src/golem/prompts/worker.md && echo "OLD_IDENTITY_GONE: PASS" || echo "OLD_IDENTITY_GONE: FAIL"

# 3. QA loop with escalation
grep -q "needs_work" src/golem/prompts/worker.md && grep -q "qa_passed" src/golem/prompts/worker.md && echo "QA_LOOP: PASS" || echo "QA_LOOP: FAIL"

# 4. MCP tools referenced
grep -q "mcp__golem-writer__run_qa" src/golem/prompts/worker.md && grep -q "mcp__golem-writer__update_ticket" src/golem/prompts/worker.md && echo "MCP_TOOLS: PASS" || echo "MCP_TOOLS: FAIL"

# 5. Subprocess constraint
grep -q "subprocess of Golem" src/golem/prompts/worker.md && echo "CONSTRAINT: PASS" || echo "CONSTRAINT: FAIL"

# 6. Template variables preserved
grep -q "{ticket_context}" src/golem/prompts/worker.md && grep -q "{plan_section}" src/golem/prompts/worker.md && grep -q "{acceptance}" src/golem/prompts/worker.md && grep -q "{qa_checks}" src/golem/prompts/worker.md && echo "TEMPLATES: PASS" || echo "TEMPLATES: FAIL"

# 7. No aggressive caps
! grep -P '\b(MUST|NEVER|CRITICAL)\b' src/golem/prompts/worker.md && echo "NO_CAPS: PASS" || echo "NO_CAPS: FAIL"
```

Expected output:
```
IDENTITY: PASS
OLD_IDENTITY_GONE: PASS
QA_LOOP: PASS
MCP_TOOLS: PASS
CONSTRAINT: PASS
TEMPLATES: PASS
NO_CAPS: PASS
```

---

## Phase 2: Code Terminology Updates

### Task 4: Update Python Code References

**Files:**
- Modify: `src/golem/planner.py`
- Modify: `src/golem/writer.py`
- Modify: `src/golem/cli.py`
- Modify: `src/golem/progress.py`

- [ ] **Step 1: Update planner.py stderr prefix**
Change all `[PLANNER]` stderr prefixes to `[LEAD ARCHITECT]`.

- [ ] **Step 2: Update writer.py stderr prefix**
Change all `[WRITER]` stderr prefixes to `[JUNIOR DEV]`.

- [ ] **Step 3: Update cli.py user-facing text**
Change any user-facing references from "Planner" to "Lead Architect" and "Writer" to "Junior Dev" in console output strings. Do not change function names, variable names, or module names — only display strings.

- [ ] **Step 4: Update progress.py event names**
Check if any event names in progress.py reference "PLANNER" or "WRITER". Update display-facing strings only. Do not rename methods — only the string content they emit.

- [ ] **Step 5: Run tests**
```bash
cd F:/Tools/Projects/golem-cli && uv run pytest --tb=short -q
```
All 259+ tests must pass. If any test asserts on the old string (e.g., checking for "[PLANNER]" in output), update the test assertion to match the new string.

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. planner.py uses new prefix
grep -q "LEAD ARCHITECT" src/golem/planner.py && ! grep -q '"\[PLANNER\]"' src/golem/planner.py && echo "PLANNER_PREFIX: PASS" || echo "PLANNER_PREFIX: FAIL"

# 2. writer.py uses new prefix
grep -q "JUNIOR DEV" src/golem/writer.py && ! grep -q '"\[WRITER\]"' src/golem/writer.py && echo "WRITER_PREFIX: PASS" || echo "WRITER_PREFIX: FAIL"

# 3. All tests pass
uv run pytest --tb=short -q 2>&1 | tail -1 | grep -q "passed" && echo "TESTS: PASS" || echo "TESTS: FAIL"

# 4. Ruff clean on modified files
uv run ruff check src/golem/planner.py src/golem/writer.py src/golem/progress.py 2>&1 | grep -q "All checks passed" && echo "RUFF: PASS" || echo "RUFF: FAIL"
```

Expected output:
```
PLANNER_PREFIX: PASS
WRITER_PREFIX: PASS
TESTS: PASS
RUFF: PASS
```

---

## Phase 1+2 Completion Gate

**This phase is not complete until every check below passes.** If any check fails, return to the responsible task, fix the issue, and re-run this entire gate.

### Gate 1: Prompt Content Verification

```bash
cd F:/Tools/Projects/golem-cli

# Lead Architect prompt
grep -q "Lead Architect" src/golem/prompts/planner.md && \
grep -q "Minimal" src/golem/prompts/planner.md && \
grep -q "SINGLE message" src/golem/prompts/planner.md && \
grep -q "subprocess of Golem" src/golem/prompts/planner.md && \
grep -q "{spec_content}" src/golem/prompts/planner.md && \
echo "GATE1_PLANNER: PASS" || echo "GATE1_PLANNER: FAIL"

# Tech Lead prompt
grep -q "You are the Tech Lead" src/golem/prompts/tech_lead.md && \
grep -q "retry up to 5 times" src/golem/prompts/tech_lead.md && \
grep -q "SINGLE message" src/golem/prompts/tech_lead.md && \
grep -q "subprocess of Golem" src/golem/prompts/tech_lead.md && \
grep -q "{golem_dir}" src/golem/prompts/tech_lead.md && \
echo "GATE1_TECHLEAD: PASS" || echo "GATE1_TECHLEAD: FAIL"

# Junior Dev prompt
grep -q "Junior Dev" src/golem/prompts/worker.md && \
grep -q "mcp__golem-writer__run_qa" src/golem/prompts/worker.md && \
grep -q "subprocess of Golem" src/golem/prompts/worker.md && \
grep -q "{ticket_context}" src/golem/prompts/worker.md && \
echo "GATE1_JUNIOR: PASS" || echo "GATE1_JUNIOR: FAIL"
```

Expected: `GATE1_PLANNER: PASS`, `GATE1_TECHLEAD: PASS`, `GATE1_JUNIOR: PASS`

### Gate 2: No Aggressive Caps Language

```bash
cd F:/Tools/Projects/golem-cli

! grep -P '\b(MUST|NEVER|CRITICAL)\b' src/golem/prompts/planner.md src/golem/prompts/tech_lead.md src/golem/prompts/worker.md && echo "GATE2_NO_CAPS: PASS" || echo "GATE2_NO_CAPS: FAIL"
```

Expected: `GATE2_NO_CAPS: PASS`

### Gate 3: Code Terminology

```bash
cd F:/Tools/Projects/golem-cli

grep -q "LEAD ARCHITECT" src/golem/planner.py && \
grep -q "JUNIOR DEV" src/golem/writer.py && \
echo "GATE3_TERMINOLOGY: PASS" || echo "GATE3_TERMINOLOGY: FAIL"
```

Expected: `GATE3_TERMINOLOGY: PASS`

### Gate 4: Full Test Suite

```bash
cd F:/Tools/Projects/golem-cli && uv run pytest --tb=short -q 2>&1 | tail -1
```

Expected: `259 passed` (or higher, 0 failed)

### Gate 5: Ruff Clean

```bash
cd F:/Tools/Projects/golem-cli && uv run ruff check src/golem/planner.py src/golem/writer.py src/golem/progress.py src/golem/cli.py 2>&1
```

Expected: No new errors beyond pre-existing ones.

### Phase Verdict

| Gate | Validates Tasks |
|------|----------------|
| Gate 1 | Tasks 1, 2, 3 |
| Gate 2 | Tasks 1, 2, 3 |
| Gate 3 | Task 4 |
| Gate 4 | Task 4 |
| Gate 5 | Task 4 |

Run all 5 gates. If **all gates pass**, the spec is complete.
If **any gate fails**, identify the responsible task from the table above, fix it, and re-run the full gate sequence.

---

## Prompt Design Principles Applied

| Principle | How Applied |
|---|---|
| Identity over rules | Each agent has a clear role + domain boundary stated as capability fact |
| Principles over procedures | Adaptive workflows (complexity scaling, conditional review), not rigid step lists |
| Positive output enumeration | Each agent lists what it PRODUCES, implicitly excluding everything else |
| Capability framing | "X is not in your domain" and "you cannot do X because [reason]" |
| Normal language | No MUST/NEVER/CRITICAL caps — explain the why instead |
| Delegation mechanic | Explicit "run multiple Agent tool invocations in a SINGLE message" |
| Scaling heuristics | Lead Architect has minimal/moderate/complex assessment |
| Recency placement | Critical constraints appear at both top and bottom of each prompt |
| Self-contained task descriptions | Explicit requirement that sub-agent tasks include everything needed |
| MCP discipline | Tech Lead retries 5x then stops — no manual workarounds |

## Non-Goals

- No tool restriction changes (prompt-first approach — architectural enforcement comes later if needed)
- No changes to the ticket system, MCP tools, or SDK wiring
- No changes to the agent hierarchy (still Planner → Tech Lead → Writers)
- No new CLI commands or config fields
- No changes to the external tools integration spec (separate concern)
