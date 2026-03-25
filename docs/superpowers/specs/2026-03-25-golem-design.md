# Golem — Autonomous Spec Executor

**Date:** 2026-03-25
**Status:** Draft
**Repository:** Standalone (new repo, `golem-cli`)

## Problem

Current spec execution tools (ZeroShot) use a single sequential worker agent to implement entire specs. For specs with 27+ file changes across 8 epics, this creates a serial bottleneck. Validation only happens after all code is written, leading to late-stage failures that waste tokens. There is no structured feedback loop — if a task fails validation, the worker has already moved on and lost context.

We need an autonomous spec executor that:
- Parses design specs into structured task graphs with dependency analysis
- Executes independent task groups in parallel across git worktrees
- Validates each task immediately after implementation with a two-tier system (deterministic checks + AI review)
- Runs worker/validator feedback loops until tasks pass or are marked blocked
- Creates a single PR with all completed work
- Resumes from interruptions without losing progress

## Goals

1. **Parallel execution** — identify independent task groups from the spec and run them simultaneously in separate worktrees.
2. **Immediate validation** — every task is validated right after implementation, not at the end of the entire spec.
3. **Feedback loops** — when validation fails, the worker gets specific feedback and retries in a fresh session. Loop until pass or max retries.
4. **Token efficiency** — deterministic checks (lint, tsc, grep) run for free in Python before spending tokens on AI validation. Sessions are ephemeral — no context accumulation.
5. **Crash resilience** — `tasks.json` is the durable source of truth. `golem resume` picks up where it left off.
6. **Project-agnostic** — works on any codebase, any language. Not tied to Kaizen OS.

## Non-Goals

- Agent Teams (peer-to-peer communication between workers) — too token-heavy for spec execution.
- Real-time human-in-the-loop during execution — Golem is fully autonomous once started.
- Custom tool definitions for workers — workers use Claude Code's built-in tools (bash, edit, read, etc.).
- Distribution as a hosted service — Golem is a local CLI tool.

## Architecture

### Overview

Golem is a **deterministic Python orchestrator** that spawns ephemeral Claude Code SDK sessions for intelligent work (planning, coding, reviewing) and handles everything else (state management, concurrency, git operations, deterministic validation) in plain Python.

```
golem run spec.md
    │
    ▼
┌──────────────────────────────────────────────────┐
│  PLANNER (Claude Code SDK, Opus, one-time)       │
│  Reads spec.md → produces tasks.json             │
│  Infers dependencies and parallel groups         │
│  Optionally researches external APIs             │
│  Session killed after tasks.json is written      │
└──────────────┬───────────────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────────────┐
│  PYTHON ORCHESTRATOR (deterministic, no tokens)  │
│  Reads tasks.json                                │
│  Creates git worktrees per parallel group        │
│  Manages ThreadPoolExecutor for concurrency      │
│  Routes tasks to worker/validator sessions       │
│  Updates tasks.json after each task              │
│  Handles retries, skip-on-blocked, git commits   │
└──────┬──────────────┬──────────────┬─────────────┘
       │              │              │
       ▼              ▼              ▼
   Worktree A     Worktree B     Worktree C
   (Group 1)      (Group 2)      (Group 3)
       │              │              │
       ▼              ▼              ▼
   ┌────────┐     ┌────────┐     ┌────────┐
   │Per-task │     │Per-task │     │Per-task │
   │loop:   │     │loop:   │     │loop:   │
   │ Worker │     │ Worker │     │ Worker │
   │ Det.   │     │ Det.   │     │ Det.   │
   │ Valid. │     │ Valid. │     │ Valid. │
   │ AI Rev.│     │ AI Rev.│     │ AI Rev.│
   └────────┘     └────────┘     └────────┘
       │              │              │
       ▼              ▼              ▼
┌──────────────────────────────────────────────────┐
│  MERGE + FINAL VALIDATION                        │
│  Merge worktree branches → single feature branch │
│  Run final validation commands                   │
│  Create PR (or draft PR if validation fails)     │
└──────────────────────────────────────────────────┘
```

### Design Principles

- **Claude fires only when intelligence is needed** — planning, coding, and reviewing. Everything else is Python.
- **Sessions are ephemeral** — every worker and validator session starts with fresh context. No accumulation, no compaction anxiety.
- **`tasks.json` is the only shared state** — all agents read from it, only the Python orchestrator writes to it. All writes are serialized through an `asyncio.Lock` to prevent concurrent write corruption from parallel group threads.
- **Deterministic validation before AI validation** — the Python orchestrator runs lint, tsc, grep checks for free. The AI validator only fires if deterministic checks pass. This eliminates most wasted validator tokens.
- **Fail-forward** — blocked tasks don't stop the run. Golem skips to the next non-dependent task, reports blocked tasks at the end.
- **`depends_on` is intra-group only** — task dependencies reference other tasks within the same group. If the planner detects cross-group dependencies, it must merge those groups. The per-group executor never waits on tasks in other groups.

## Detailed Design

### 1. CLI Interface

Golem is installed via `pip install golem-cli` or `uvx golem`.

**Research step:** Before implementing `cli.py`, dispatch a sub-agent to use **WebSearch** and **WebFetch** to look up the latest 2026 documentation for `typer` (PyPI). Verify the current patterns for defining commands, `typer.Argument`, `typer.Option`, callback usage, and app composition. Do NOT rely on training data for `typer`'s API.

**Commands:**

```bash
golem run <spec.md>              # Full autonomous run (plan + execute)
golem run <spec.md> --force      # Skip TUI, use defaults (for CI/non-interactive)
golem plan <spec.md>             # Generate tasks.json only (dry run)
golem status                     # Show current run progress
golem resume                     # Resume interrupted run from tasks.json
golem clean                      # Remove .golem/ state, worktrees, and branches
```

**Pre-execution TUI:**

When `golem run` is invoked, Golem displays an interactive setup screen before going autonomous:

```
🗿 Golem v0.1.0

Analyzing spec... done.

Found 14 tasks across 4 parallel groups:

  Group A (sequential):  Foundation → Path Migration (7 tasks)
  Group B (parallel):    Prompt Pipeline (4 tasks)
  Group C (parallel):    Plugin Cleanup (2 tasks)
  Group D (sequential):  Migration + Docs (4 tasks)
  Final:                 Verification — runs after all groups

Settings:
  [1] Max parallel worktrees:  3
  [2] Max retries per task:    3
  [3] Planner model:           opus
  [4] Worker model:            opus
  [5] Validator model:         sonnet

  [Enter] Start execution
  [e] Edit settings
  [d] Dry run (show tasks.json only)
  [q] Quit
```

**Research step:** Before implementing `tui.py`, dispatch a sub-agent to use **WebSearch** and **WebFetch** to look up the latest 2026 documentation for `rich` (PyPI). Verify the current API for `rich.live.Live`, `rich.table.Table`, `rich.progress.Progress`, and `rich.prompt.Prompt` — specifically how to nest these components, update them in real time, and compose them in a single `Live` context. Do NOT rely on training data for `rich`'s API.

**Live execution dashboard:**

During execution, the TUI shows real-time progress:

```
🗿 Golem — executing runtime-isolation

  Group A [worker-a] ██████████░░░░░░  3/7 tasks   Task 1.4: Update start.ts
  Group B [worker-b] ████████████████  2/2 tasks   ✓ Complete
  Group C [worker-c] ██████░░░░░░░░░░  1/3 tasks   Task 4.2: Delete plugin dirs
                                                     ↳ Validator: checking...

  Progress: 6/14 tasks  |  Passed: 5  |  Blocked: 1  |  Retries: 2
  Elapsed: 12m 34s  |  tasks.json updated 23s ago
```

### 2. Project State Directory

Golem creates `.golem/` in the directory where it's invoked (the project root). This directory is gitignored.

```
project-root/
  .golem/
    tasks.json              ← Shared task graph (source of truth)
    progress.log            ← Human-readable session log
    config.json             ← Run configuration snapshot
    reference/              ← API docs from planner research phase
    worktrees/
      group-a/              ← Git worktree for parallel group A
      group-b/              ← Git worktree for parallel group B
```

**Run lifecycle:**

When `golem run` is invoked and a previous `.golem/` exists:
- If the previous run's PR was merged → prompt to clean up and start fresh
- If the previous run's PR is still open → warn that starting new will delete previous worktrees
- User chooses via TUI: clean and start, resume previous, or quit

### 3. The Planner

The planner is a single Claude Code SDK session (Opus) that reads the full spec and produces `tasks.json`. It runs once and is killed after output.

**Planner flow:**

1. Spawn Claude Code session with planner prompt
2. Session reads the spec file and the project's CLAUDE.md / README for context
3. Session identifies: files to create/modify, dependencies between changes, parallel groups, acceptance criteria per task, whether external API research is needed
4. If external APIs need research: planner spawns researcher sub-agents to fetch docs into `.golem/reference/`
5. Session outputs structured JSON to `.golem/tasks.json`
6. The orchestrator waits for the `query()` async iterator to complete, then the session ends naturally

All sessions (planner, worker, validator) terminate by awaiting the `query()` iterator to completion. The orchestrator never force-kills sessions. If a session exceeds `max_turns`, the SDK terminates it automatically and the orchestrator treats the result as the session's output.

**Planner prompt structure:**

```
You are a planner for an autonomous code execution system.

Read the spec file and the project context, then produce a tasks.json
that breaks the spec into atomic, independently-executable tasks.

## Spec File
{spec content}

## Project Context
{CLAUDE.md content, if exists}

## Your Job
1. Identify every discrete code change the spec requires
2. Group changes by file dependency — tasks touching the same files
   must be in the same group and ordered sequentially
3. Tasks touching completely different files can be in separate groups
   (these will run in parallel)
4. For each task, define:
   - Clear description of what to implement
   - Files to create and/or modify
   - Dependencies on other tasks (by task ID)
   - Acceptance criteria (specific, verifiable statements)
   - Validation commands (bash commands that verify correctness)
5. Identify if any external library APIs need documentation research
6. Define a final validation block that runs after all tasks merge

Output valid JSON matching this schema:
{tasks.json schema}
```

### 4. tasks.json Schema

```json
{
  "spec": "path/to/spec.md",
  "created": "ISO-8601 timestamp",
  "project": "project name (from directory)",
  "branch": "golem/<spec-slug>",
  "models": {
    "planner": "opus",
    "worker": "opus",
    "validator": "sonnet"
  },
  "config": {
    "max_retries": 3,
    "max_parallel": 3,
    "max_worker_turns": 50,
    "max_validator_turns": 20
  },
  "groups": [
    {
      "id": "group-slug",
      "description": "Human-readable group description",
      "worktree_branch": "golem/<spec-slug>/<group-slug>",
      "tasks": [
        {
          "id": "task-001",
          "description": "What to implement",
          "files_create": ["path/to/new/file.ts"],
          "files_modify": ["path/to/existing/file.ts"],
          "depends_on": [],
          "acceptance": [
            "Specific verifiable criterion 1",
            "Specific verifiable criterion 2"
          ],
          "validation_commands": [
            "bash command that returns 0 on success"
          ],
          "reference_docs": ["relative/path/to/reference.md"],
          "status": "pending",
          "retries": 0,
          "last_feedback": null,
          "blocked_reason": null,
          "completed_at": null
        }
      ]
    }
  ],
  "final_validation": {
    "depends_on_all": true,
    "commands": [
      "bun test",
      "other project-wide validation commands"
    ]
  }
}
```

### 5. Execution Loop

The Python orchestrator manages the execution lifecycle deterministically.

**Parallel group execution:**

The SDK is async-native (`query()` returns an `AsyncIterator`), so the orchestrator uses `asyncio.gather()` for concurrency. Each group runs as a coroutine on the event loop — no OS threads needed.

```
asyncio.gather(
    execute_group(group_a),   ← coroutine 1
    execute_group(group_b),   ← coroutine 2
    execute_group(group_c),   ← coroutine 3
)
```

**Per-task loop within a group:**

```
For each task in group (respecting depends_on ordering):
│
├─ Check dependencies → skip if not met, mark blocked
├─ Set status = "in_progress", save tasks.json
│
├─ RETRY LOOP (up to max_retries):
│   │
│   ├─ 1. Spawn Worker (Claude Code SDK, Opus)
│   │      - Scoped prompt: task description + files + acceptance + feedback
│   │      - cwd = worktree path
│   │      - allowed_tools = [bash, edit, write, read, glob, grep]
│   │      - Await query() iterator to completion (or max_turns hit)
│   │      - If max_turns reached, proceed to validation anyway
│   │
│   ├─ 2. Deterministic Validation (Python subprocess, zero tokens)
│   │      - Run each validation_command
│   │      - If ANY fails → set last_feedback = error output → retry
│   │
│   ├─ 3. AI Validation (Claude Code SDK, Sonnet)
│   │      - Only runs if deterministic checks all passed
│   │      - Scoped prompt: task description + acceptance criteria
│   │      - allowed_tools = [read, glob, grep, bash]
│   │      - Note: bash access is a pragmatic trade-off (needed for verification
│   │        commands). "Read-only" is enforced via prompt instructions, not
│   │        technically sandboxed. The validator prompt explicitly says "Do NOT
│   │        fix code."
│   │      - Returns PASS or FAIL with specific feedback
│   │      - If FAIL → set last_feedback = validator feedback → retry
│   │
│   └─ 4. If PASS → break retry loop
│
├─ If passed: status = "completed", git commit, save tasks.json
└─ If max retries exhausted: status = "blocked", save tasks.json, continue
```

### 6. SDK Integration

**Research step:** Before implementing any SDK integration code, dispatch a sub-agent to use **WebSearch** and **WebFetch** to look up the latest 2026 documentation for `claude-agent-sdk` (PyPI package). Verify the current import paths, the signature of `query()`, the fields on `ClaudeAgentOptions`, and the message types (`AssistantMessage`, `TextBlock`, `ResultMessage`, `ResultMessage.result`). Do NOT rely on training data or the code examples below as authoritative — treat them as illustrative only. Confirm the exact installed version with `uv run pip show claude-agent-sdk` and cross-reference against the official changelog.

Golem uses the `claude-agent-sdk` Python package (v0.1.50+), which wraps the Claude Code CLI and provides an async API for spawning and managing agent sessions.

**Spawning a worker session:**

```python
from claude_agent_sdk import query, ClaudeAgentOptions, AssistantMessage, TextBlock, ResultMessage

async def run_worker(task: Task, worktree_path: str, feedback: str | None) -> str:
    prompt = build_worker_prompt(task, feedback)
    result_text = ""

    async for message in query(
        prompt=prompt,
        options=ClaudeAgentOptions(
            model="claude-opus-4-6",
            cwd=worktree_path,
            allowed_tools=["Bash", "Read", "Edit", "Write", "Glob", "Grep"],
            max_turns=50,
            permission_mode="acceptEdits",
        ),
    ):
        if isinstance(message, ResultMessage):
            result_text = message.result or ""
        elif isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    # Stream to TUI dashboard
                    update_dashboard(task.id, block.text)

    return result_text
```

**Spawning a validator session:**

```python
async def run_validator(task: Task, worktree_path: str) -> tuple[bool, str]:
    prompt = build_validator_prompt(task)

    async for message in query(
        prompt=prompt,
        options=ClaudeAgentOptions(
            model="claude-sonnet-4-6",
            cwd=worktree_path,
            allowed_tools=["Read", "Glob", "Grep", "Bash"],
            disallowed_tools=["Write", "Edit"],
            max_turns=20,
            permission_mode="acceptEdits",
        ),
    ):
        if isinstance(message, ResultMessage):
            text = message.result or ""
            if text.startswith("PASS"):
                return (True, text)
            else:
                return (False, text)

    return (False, "Validator session ended without verdict")
```

**Parallel group execution (async):**

```python
async def execute_all_groups(groups: list[Group]):
    tasks = [execute_group(group) for group in groups if not group.depends_on_all]
    await asyncio.gather(*tasks)
```

### 7. Worker Sessions

Workers are ephemeral Claude Code SDK sessions scoped to a single task.

**Worker prompt:**

```
You are implementing a single task in an existing codebase.

## Your Task
{task.description}

## Files to Create
{task.files_create}

## Files to Modify
{task.files_modify}

## Acceptance Criteria
{task.acceptance — ALL must be true when you're done}

## Reference Docs
{content of referenced docs, if any}

## Previous Attempt Feedback
{task.last_feedback — only present on retry attempts}

## Rules
- Implement ONLY this task. Do not touch files outside your scope.
- Read existing code before modifying anything.
- Follow existing patterns, conventions, and code style.
- Do not add unnecessary abstractions or improvements.
- When done, your changes should satisfy ALL acceptance criteria.
```

**Worker configuration:**

| Setting | Value |
|---------|-------|
| Model | Opus (configurable) |
| Max turns | 50 (configurable) |
| Allowed tools | bash, edit, write, read, glob, grep |
| CWD | Worktree path for this group |
| System prompt | Loaded from worker prompt template |

### 8. Validator Sessions

Validation has two tiers. Tier 1 is free. Tier 2 only fires if Tier 1 passes.

**Tier 1 — Deterministic (Python, zero tokens):**

The orchestrator runs each `validation_command` from the task as a subprocess. These are bash commands that return exit code 0 on success. Examples:
- `grep -c 'export function getRuntimeDir' src/runtime.ts` — verify function exists
- `test ! -d commands/` — verify directory was deleted
- `bun build --no-bundle src/runtime.ts --outdir /tmp/golem-check` — verify compilation
- `bun test` — run project tests

If any command fails, the output (stdout + stderr) is captured and fed back to the worker as `last_feedback`. No AI tokens spent.

**WSL path fallback for build-tool commands:**

On Windows hosts running Python natively (non-WSL), build tools like `bun build`, `tsc`, and `vite build` may fail due to path translation issues (e.g., `/tmp/` not resolving correctly). Any `validation_command` that invokes a build tool MUST include a code-inspection fallback. The fallback runs when the build command returns a non-zero exit code AND the stderr contains path-related errors (e.g., `ENOENT`, `cannot find`, `no such file`).

The fallback replaces the build command with static code inspection:
- `grep` for expected exports/imports in the relevant source files
- `python -c "import ast; ast.parse(open('file.py').read())"` for Python syntax validation
- File existence checks via `test -f path/to/file`

Example `validation_commands` entry with WSL fallback:
```json
"validation_commands": [
  "bun build --no-bundle src/runtime.ts --outdir /tmp/golem-check || grep -c 'export function getRuntimeDir' src/runtime.ts"
]
```

The orchestrator treats a fallback exit code of 0 as a pass for deterministic validation purposes, and records in `progress.log` that the build tool was unavailable and code inspection was used instead.

**Tier 2 — AI Review (Claude Code SDK, Sonnet):**

A separate Claude Code session reviews the implementation against acceptance criteria.

**Validator prompt:**

```
You are a code reviewer validating a single task implementation.

## Task That Was Implemented
{task.description}

## Acceptance Criteria — ALL must pass:
{task.acceptance}

## Instructions
- Read the changed files and verify each acceptance criterion.
- Run any commands needed to verify behavior.
- Be skeptical — check edge cases the worker might have missed.
- Do NOT fix code. Only review and report.

Respond with EXACTLY one of:
PASS: All criteria met. {brief confirmation of what you verified}
FAIL: {which criterion failed} — {specific, actionable feedback for the worker}
```

**Validator configuration:**

| Setting | Value |
|---------|-------|
| Model | Sonnet (configurable) |
| Max turns | 20 (configurable) |
| Allowed tools | read, glob, grep, bash (read-only + verification commands) |
| CWD | Same worktree path as the worker |

### 9. Git Workflow

**Branch strategy:**

```
main
  └─ golem/<spec-slug>                    ← final merged branch, becomes the PR
       ├─ golem/<spec-slug>/group-a       ← worktree branch for group A
       ├─ golem/<spec-slug>/group-b       ← worktree branch for group B
       └─ golem/<spec-slug>/group-c       ← worktree branch for group C
```

**Per-task commits:**

After each task passes validation, the orchestrator commits in the worktree:
```
git add -A
git commit -m "golem: task-001 — Create src/runtime.ts with path accessors"
```

**Merge flow:**

After all groups complete (or all remaining tasks are blocked):
1. Create `golem/<spec-slug>` branch from main
2. Merge each group branch into it sequentially
3. If merge conflicts arise, spawn a Claude Code session (Opus, max 30 turns) to resolve them. If unresolvable after 30 turns, create draft PR with conflict markers noted in the body.
4. Run `final_validation` commands on the merged branch
5. If final validation passes → create PR (ready for review)
6. If final validation fails → create draft PR with failure report

**PR body:**

```markdown
## Golem Run Report

**Spec:** `docs/superpowers/specs/2026-03-25-runtime-isolation-design.md`
**Duration:** 23m 47s
**Tasks:** 13/14 completed, 1 blocked

### Completed Tasks
- [x] task-001: Create src/runtime.ts with path accessors
- [x] task-002: Create src/commands/init.ts with scaffold logic
- [x] task-003: Create seed directories
...

### Blocked Tasks
- [ ] task-009: Auto-trigger migration in start command
  - Failed after 3 attempts
  - Last feedback: "Missing edge case: empty state.json not handled"

### Validation
- [x] bun test — passed
- [x] grep stale references — 0 matches
- [ ] vite build — not applicable (no UI changes)

🗿 Generated by [Golem](https://github.com/user/golem)
```

### 10. Crash Resilience

`tasks.json` is written to disk after every task status change. If Golem crashes or is interrupted:

```bash
golem resume
```

Resume reads `.golem/tasks.json` and recovers based on state:

1. **Worktree exists, tasks pending/in_progress** — continue from next pending task. Any `in_progress` task is reset to `pending` (its session was lost in the crash).
2. **Worktree deleted, branch exists with committed work** — recreate worktree from the group branch (`git worktree add`), reset `in_progress` tasks to `pending`, continue.
3. **Worktree and branch both missing** — mark all incomplete tasks in that group as `blocked` with reason "worktree and branch lost during interruption."

Completed tasks are never re-run.

### 11. Configuration

**Default settings (overridable in TUI):**

| Setting | Default | Description |
|---------|---------|-------------|
| `max_parallel` | 3 | Maximum concurrent worktrees |
| `max_retries` | 3 | Max worker retry attempts per task |
| `planner_model` | opus | Model for the planner session |
| `worker_model` | opus | Model for worker sessions |
| `validator_model` | sonnet | Model for AI validator sessions |
| `max_worker_turns` | 50 | Safety ceiling for worker session length |
| `max_validator_turns` | 20 | Safety ceiling for validator session length |
| `auto_pr` | true | Automatically create PR on completion |
| `pr_target` | main | Target branch for the PR |

Settings are saved to `.golem/config.json` and reused on `golem resume`.

## Project Structure

```
golem/
  src/
    golem/
      __init__.py
      cli.py                ← CLI entry point (typer + rich)
      planner.py            ← Spec parser → tasks.json generation
      executor.py           ← Main execution loop (Python orchestrator)
      worker.py             ← Claude Code SDK worker session spawner
      validator.py          ← Deterministic checks + AI reviewer
      worktree.py           ← Git worktree creation/management/merge
      tasks.py              ← tasks.json read/write/state machine
      config.py             ← Settings, defaults, model configuration
      progress.py           ← Human-readable progress.log writer (timestamps, task completions, retries)
      tui.py                ← Pre-run settings screen + live dashboard
      prompts/
        planner.md          ← Planner system prompt template
        worker.md           ← Worker system prompt template
        validator.md        ← Validator system prompt template
  tests/
    test_tasks.py           ← Task graph parsing and state machine
    test_executor.py        ← Execution loop logic
    test_worktree.py        ← Git worktree operations
    test_validator.py       ← Deterministic validation
  pyproject.toml            ← uv/pip package definition (golem-cli)
  README.md
  LICENSE                   ← MIT
```

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Language | Python 3.12+ |
| Package manager | uv |
| CLI framework | typer + rich |
| TUI | rich (Live, Table, Progress) |
| Agent SDK | `claude-agent-sdk` v0.1.50+ (PyPI: `pip install claude-agent-sdk`) |
| Auth | `ANTHROPIC_API_KEY` env var (API key for programmatic use) |
| Concurrency | `asyncio.gather()` (SDK is async-native; coroutines on single event loop) |
| Git | subprocess calls to `git` CLI |
| State | JSON files (tasks.json, config.json) |
| Testing | pytest |

## Risks

1. **Planner quality** — if the planner generates a bad task graph (wrong dependencies, missing tasks, poor grouping), the entire run suffers. Mitigation: planner uses Opus and gets the full spec + CLAUDE.md for context. The `golem plan` dry-run command lets users inspect tasks.json before executing.
2. **Merge conflicts between worktrees** — parallel groups modify different files by design, but the planner could mis-classify dependencies. Mitigation: spawn a Claude Code session to resolve conflicts during merge. Report unresolvable conflicts in the PR.
3. **Worker scope creep** — workers might modify files outside their task scope, causing conflicts with other worktrees. Mitigation: worker prompts are surgically scoped with explicit file lists. The validator checks for out-of-scope changes.
4. **Token cost** — Opus workers are expensive. A 14-task spec with 3 retries could mean ~50 Opus sessions. Mitigation: deterministic validation catches most failures for free. Sessions are scoped and short. Max turns ceiling prevents runaway sessions. Configurable models allow Sonnet workers for cost-sensitive runs.
5. **Claude Code SDK API surface** — the Python SDK is evolving. Mitigation: pin the SDK version, research current API before implementing, abstract SDK calls behind a thin wrapper.

## Verification

After implementation, verify each module passes ALL of the following acceptance criteria:

### CLI (`cli.py`)
- `uv run golem --help` exits 0 and lists `run`, `plan`, `status`, `resume`, `clean` subcommands
- `uv run golem run spec.md --force` runs without prompting for TUI input
- `uv run golem plan spec.md` writes `.golem/tasks.json` and exits without executing any workers

### Planner (`planner.py`)
- `uv run golem plan spec.md` produces a `.golem/tasks.json` that validates against the schema in Section 4 (all required keys present, `status` is `"pending"` for all tasks, `depends_on` references only tasks within the same group)
- Every task in the output has at least one entry in `acceptance` and at least one entry in `validation_commands`
- Tasks that reference the same files are in the same group

### Executor (`executor.py`)
- Running a 2-group spec launches exactly 2 coroutines concurrently (verifiable via `asyncio.gather` call in logs)
- A task with `depends_on` pointing to an incomplete predecessor is not started until the predecessor reaches `status: "completed"`
- A task that fails 3 times transitions to `status: "blocked"` and execution continues with remaining tasks

### Worker (`worker.py`)
- Worker session's `cwd` is the worktree path, not the main repo root (verifiable by reading the subprocess call)
- Worker prompt includes `last_feedback` content on retry attempts and omits it on first attempt
- Worker session is terminated (iterator exhausted) before the validator runs

### Validator (`validator.py`)
- Deterministic validation commands are run as subprocesses; a non-zero exit code sets `last_feedback` without invoking the AI validator
- AI validator only fires when all deterministic commands return exit code 0
- Validator session returns `(True, text)` for a response starting with `PASS` and `(False, text)` for any other response

### Worktree (`worktree.py`)
- `git worktree list` shows one entry per parallel group after worktrees are created
- After all groups complete, each group branch is merged into `golem/<spec-slug>` sequentially
- Merge conflicts trigger a Claude Code conflict-resolution session (Opus, max 30 turns)

### Tasks (`tasks.py`)
- `tasks.json` is written after every status transition (pending → in_progress, in_progress → completed, in_progress → blocked)
- Concurrent writes from parallel groups do not corrupt `tasks.json` (serialized via `asyncio.Lock`)
- `golem resume` resets any `in_progress` task to `pending` and continues from the next unfinished task

### TUI (`tui.py`)
- Pre-run screen displays group count, task count, and all 5 configurable settings before execution begins
- Live dashboard updates task status in real time during execution (group progress bars, current task name, validator status)
- `--force` flag bypasses TUI and uses defaults without rendering the settings screen

### Final Validation & PR
- `final_validation.commands` are run on the merged branch after all groups complete
- A ready-to-review PR is created when final validation passes; a draft PR is created when it fails
- PR body includes completed task list, blocked task list with last feedback, and validation command results

## Implementation Plan

### Epic 0: Environment Setup

**0.1** Verify the environment is clean and install all dependencies:
```bash
uv sync
uv run pytest --collect-only
```

**Research step:** Before implementing any test files, dispatch a sub-agent to use **WebSearch** and **WebFetch** to look up the latest 2026 documentation for `pytest-asyncio` (PyPI). Verify the current `asyncio_mode` configuration options, how to declare async fixtures, and whether `@pytest.mark.asyncio` is still required or auto-applied. Do NOT rely on training data for `pytest-asyncio` configuration.

**Git checkpoint:** `git add -A && git commit -m "epic-0: environment setup and dependency install"`

### Epic 1: Core Data Model (`tasks.py`, `config.py`)

**1.1** Implement `tasks.py` — `Task`, `Group`, `TasksFile` dataclasses, `read_tasks()`, `write_tasks()`, status state machine, `asyncio.Lock` serialization.
**1.2** Implement `config.py` — `GolemConfig` dataclass with all defaults from the Configuration table.

**Git checkpoint:** `git add -A && git commit -m "epic-1: core data model tasks.py and config.py"`

### Epic 2: Git Worktree Management (`worktree.py`)

**2.1** Implement `worktree.py` — `create_worktree()`, `delete_worktree()`, `commit_task()`, `merge_group_branches()`, merge-conflict resolution session, PR creation.

**Git checkpoint:** `git add -A && git commit -m "epic-2: git worktree management"`

### Epic 3: Planner (`planner.py`, `prompts/planner.md`)

**3.1** Implement `prompts/planner.md` — planner system prompt template with spec, project context, and tasks.json schema injection points.
**3.2** Implement `planner.py` — spawn Opus planner session, await `query()` iterator, parse `tasks.json` output, write to `.golem/tasks.json`.

**Git checkpoint:** `git add -A && git commit -m "epic-3: planner session and prompt"`

### Epic 4: Worker and Validator Sessions (`worker.py`, `validator.py`, `prompts/`)

**4.1** Implement `prompts/worker.md` — worker system prompt template.
**4.2** Implement `prompts/validator.md` — validator system prompt template.
**4.3** Implement `worker.py` — `run_worker()` async function per SDK integration spec.
**4.4** Implement `validator.py` — `run_deterministic_checks()` (subprocess, zero tokens) and `run_ai_validator()` (Sonnet session).

**Git checkpoint:** `git add -A && git commit -m "epic-4: worker and validator sessions"`

### Epic 5: Execution Orchestrator (`executor.py`, `progress.py`)

**5.1** Implement `executor.py` — `execute_group()` coroutine (per-task retry loop: worker → deterministic → AI validator), `execute_all_groups()` with `asyncio.gather()`, final validation, merge flow.
**5.2** Implement `progress.py` — timestamped `progress.log` writer for task completions, retries, and blocked events.

**Git checkpoint:** `git add -A && git commit -m "epic-5: execution orchestrator and progress log"`

### Epic 6: TUI (`tui.py`)

**6.1** Implement `tui.py` — pre-run settings screen (rich `Prompt`, settings display, `[Enter]`/`[e]`/`[d]`/`[q]` handling) and live execution dashboard (rich `Live`, `Table`, `Progress`).

**Git checkpoint:** `git add -A && git commit -m "epic-6: TUI pre-run screen and live dashboard"`

### Epic 7: CLI Entry Point (`cli.py`)

**7.1** Implement `cli.py` — `golem run`, `golem plan`, `golem status`, `golem resume`, `golem clean` commands via typer. Wire planner → TUI → executor pipeline.

**Git checkpoint:** `git add -A && git commit -m "epic-7: CLI entry point"`

### Epic 8: Tests

**8.1** Implement `tests/test_tasks.py` — task graph parsing, state machine transitions, lock serialization.
**8.2** Implement `tests/test_executor.py` — retry loop logic, blocked propagation, group completion.
**8.3** Implement `tests/test_worktree.py` — worktree create/delete/commit git operations.
**8.4** Implement `tests/test_validator.py` — deterministic validation command execution and exit code handling.

**Git checkpoint:** `git add -A && git commit -m "epic-8: test suite"`

## Definition of Done

The implementation is complete when ALL of the following are true:

1. All epics (0–8) have been implemented and their git checkpoints committed.
2. `uv run pytest` passes with 0 failures.
3. `uv run golem plan docs/superpowers/specs/2026-03-25-golem-design.md` produces a valid `tasks.json` without error.
4. `uv run golem run docs/superpowers/specs/2026-03-25-golem-design.md --force` completes without crashing (dry-run or smoke test against a sample spec).
5. All verification criteria in `## Verification` are confirmed passing.
6. A PR is created on the `golem/<spec-slug>` branch with an accurate Golem Run Report in the body.
