# Golem — Autonomous Spec Executor

## What This Is
A standalone CLI tool that autonomously executes markdown design specs. Parses specs into structured task graphs, runs parallel workers in git worktrees, validates with two-tier checks (deterministic + AI), and creates PRs with completed work.

## Quick Start
```bash
uv sync                          # Install dependencies
uv run golem run spec.md         # Execute a spec
uv run golem plan spec.md        # Dry run — generate tasks.json only
uv run golem status              # Check current run progress
uv run golem resume              # Resume interrupted run
uv run golem clean               # Clean up .golem/ state
uv run pytest                    # Run tests
```

## The Spec
**Read `docs/superpowers/specs/2026-03-25-golem-design.md` before doing ANY work.** It contains the complete design with architecture, SDK integration, task schema, execution loop, and verification criteria.

## Coding Conventions

### Runtime & Language
- **Runtime:** Python 3.12+
- **Package manager:** uv (never pip directly)
- **Strict typing** — use `type` hints everywhere, no `Any`
- **Async-first** — the SDK is async-native, use `async/await` throughout
- **Imports:** absolute imports from `golem.*` (source is in `src/golem/`)

### Style
- **Formatter/Linter:** ruff (configured in pyproject.toml)
- **Line length:** 120
- **Match existing patterns** — do NOT refactor code you're not changing
- **Minimal diff scope** — only touch what the spec says to touch

### Dependencies
- **`claude-agent-sdk`** — Claude Agent SDK for spawning worker/validator sessions
- **`typer`** — CLI framework
- **`rich`** — TUI (Live, Table, Progress, Prompt)
- **No other dependencies** unless the spec explicitly calls for them

### Project Structure
```
src/
  golem/
    __init__.py
    cli.py              ← CLI entry point (typer + rich)
    planner.py          ← Spec parser → tasks.json generation
    executor.py         ← Main execution loop (async orchestrator)
    worker.py           ← Claude Agent SDK worker session spawner
    validator.py        ← Deterministic checks + AI reviewer
    worktree.py         ← Git worktree creation/management/merge
    tasks.py            ← tasks.json read/write/state machine
    config.py           ← Settings, defaults, model configuration
    progress.py         ← Human-readable progress.log writer
    tui.py              ← Pre-run settings screen + live dashboard
    prompts/
      planner.md        ← Planner system prompt template
      worker.md         ← Worker system prompt template
      validator.md      ← Validator system prompt template
tests/
  test_tasks.py         ← Task graph parsing and state machine
  test_executor.py      ← Execution loop logic
  test_worktree.py      ← Git worktree operations
  test_validator.py     ← Deterministic validation
```

### Testing
- **Framework:** pytest with pytest-asyncio
- **Run:** `uv run pytest`
- **Focus on:** task graph logic, state machine transitions, deterministic validation, git operations
- **Do NOT mock** the Claude Agent SDK in tests — test the orchestration logic around it

## Key Design Decisions
- **Deterministic Python orchestrator** — the execution loop is plain Python, not an AI agent. Claude fires only for planning, coding, and reviewing.
- **Sessions are ephemeral** — worker and validator sessions are killed after each task. Fresh context every time.
- **`tasks.json` is the source of truth** — shared across all worktrees, serialized writes via `asyncio.Lock`.
- **`depends_on` is intra-group only** — cross-group dependencies are handled by merging groups.
- **Two-tier validation** — deterministic checks (free) run before AI review (tokens).
- **Fail-forward** — blocked tasks don't stop the run.

## Do NOT
- Use `pip` directly — use `uv` for everything
- Add dependencies not listed in pyproject.toml without updating the spec
- Use `threading` — the SDK is async-native, use `asyncio`
- Create files outside the project structure defined above
- Use `Any` type — always use explicit types
