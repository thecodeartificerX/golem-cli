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
uv run golem version             # Show version, Python, platform info
uv run golem ui                  # Launch web dashboard (port 9664)
.\Golem.ps1                      # PowerShell ops dashboard (setup + server + TUI)
uv run pytest                    # Run tests
```

## Prerequisites
- **Claude CLI authenticated** — run `claude login` once; the SDK spawns `claude` subprocesses using OAuth (not API keys)
- **uv installed** — `pip install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`
- **ripgrep (`rg`) on PATH** — required by spec validation commands; `winget install BurntSushi.ripgrep` or `scoop install ripgrep`

## Runtime State (`.golem/`)
Created by `golem run` in the project root (gitignored):
- `tasks.json` — shared task graph (source of truth)
- `progress.log` — timestamped execution log
- `config.json` — run configuration snapshot
- `worktrees/` — git worktrees per parallel group

## The Spec
The original design spec has been implemented and removed. For architecture and design context, read this `CLAUDE.md` and the source code directly. For a minimal test spec, see `docs/rg-smoke.md`.

## Coding Conventions

### Runtime & Language
- **Runtime:** Python 3.12+
- **Package manager:** uv (never pip directly)
- **Strict typing** — use `type` hints everywhere, no `Any`
- **Async-first** — the SDK is async-native, use `async/await` throughout
- **Imports:** absolute imports from `golem.*` (source is in `src/golem/`)

### Windows Compatibility (Critical)
- **Always use `encoding="utf-8"`** on all `read_text()`, `write_text()`, and `open()` calls — Windows defaults to cp1252
- **No emoji in CLI/TUI output** — rich crashes on Windows cp1252 console; use ASCII text only
- **Tests must use `git init -b main`** — Windows git defaults vary; never assume `master`
- **Validation subprocesses read fresh PATH from Windows registry** — `_subprocess_env()` in `validator.py` reads `HKCU\Environment\Path` and prepends new entries; long-running parent processes (Claude Code) may have stale PATH
- **Spec validation commands must not assume Unix quoting** — `_normalize_cmd()` in `validator.py` converts single quotes to double quotes for cmd.exe; spec authors can use either style

### Style
- **Formatter/Linter:** ruff (configured in pyproject.toml)
- **Line length:** 120
- **Pre-existing lint errors** — `cli.py`, `executor.py`, `planner.py`, `tui.py`, and some test files have known ruff warnings; don't fix unless explicitly asked
- **Match existing patterns** — do NOT refactor code you're not changing
- **Minimal diff scope** — only touch what the spec says to touch

### Dependencies
- **`claude-agent-sdk`** — Claude Agent SDK for spawning worker/validator sessions
- **`typer`** — CLI framework
- **`rich`** — TUI (Live, Table, Progress, Prompt)
- **`fastapi`** — Web framework for UI dashboard server
- **`uvicorn[standard]`** — ASGI server for the dashboard
- **`httpx`** (dev) — Async HTTP client for UI endpoint tests

### Project Structure
```
src/
  golem/
    __init__.py
    version.py          ← Version/platform metadata utility
    cli.py              ← CLI entry point (typer + rich)
    planner.py          ← Spec parser → tasks.json generation
    executor.py         ← Main execution loop (async orchestrator)
    worker.py           ← Claude Agent SDK worker session spawner
    validator.py        ← Deterministic checks + AI reviewer
    dialogs.py          ← Native Windows file/folder picker dialogs (ctypes win32)
    worktree.py         ← Git worktree creation/management/merge
    tasks.py            ← tasks.json read/write/state machine
    config.py           ← Settings, defaults, model configuration
    progress.py         ← Human-readable progress.log writer
    tui.py              ← Pre-run settings screen + live dashboard
    ui.py               ← FastAPI server (SSE events, subprocess mgmt)
    ui_template.html    ← Self-contained HTML dashboard (no CDN)
    prompts/
      planner.md        ← Planner system prompt template
      worker.md         ← Worker system prompt template
      validator.md      ← Validator system prompt template
tests/
  __init__.py
  test_tasks.py         ← Task graph parsing and state machine
  test_executor.py      ← Execution loop logic
  test_worktree.py      ← Git worktree operations
  test_validator.py     ← Deterministic validation
  test_config.py        ← GolemConfig, setting_sources, sdk_env
  test_ui.py            ← UI server endpoints, SSE, helpers
Golem.ps1               ← PowerShell ops dashboard (server lifecycle + TUI)
```

### Testing
- **Framework:** pytest with pytest-asyncio
- **Run:** `uv run pytest`
- **Focus on:** task graph logic, state machine transitions, deterministic validation, git operations
- **Do NOT mock** the Claude Agent SDK in tests — test the orchestration logic around it

### Claude Agent SDK Gotchas
- **Use `permission_mode="bypassPermissions"`** for all SDK sessions — `acceptEdits` blocks headless file writes
- **Clear `ANTHROPIC_API_KEY` in SDK env** — the env var overrides CLI OAuth auth and fails; use `env=sdk_env()` from `config.py`
- **Use `tools={"type": "preset", "preset": "claude_code"}` + `setting_sources=config.setting_sources`** — replaces old `allowed_tools` lists; gives sessions full CC capabilities
- **`setting_sources` defaults to `["project"]` only** — excludes `"user"` to prevent user-level plugin hooks (e.g. claude-mem SessionEnd) from firing in headless SDK sessions and killing them
- **SDK initialize timeout is monkey-patched to 180s** — the SDK hardcodes 60s with no public API to override; `planner.py` patches `Query.__init__.__defaults__` at import time
- **Capture `AssistantMessage` text blocks as fallback** — `ResultMessage.result` may be empty; check both
- **Validator PASS detection must be fuzzy** — AI models prefix preamble before "PASS:"; search anywhere, not just `startswith`
- **Run `uv sync` in worktrees after creation** — new worktrees lack venv; module imports fail without it
- **Verbose SDK streaming** — `planner.py`, `worker.py`, `validator.py` print `[PLANNER]`/`[WORKER]`/`[VALIDATOR]` prefixed messages to stderr showing text blocks, tool calls, and results in real-time

### FastAPI / UI Gotchas
- **Pydantic models must be module-level** — defining `BaseModel` subclasses inside `create_app()` breaks FastAPI's annotation resolution; requests get 422 instead of binding to the body
- **SSE tests must drive the generator directly** — `TestClient` hangs on infinite SSE streams; use `async for` with early `break` + `aclose()` instead
- **UI server for direct PID control:** `python -m uvicorn golem.ui:create_app --factory --host 127.0.0.1 --port 9664` (when venv is activated, avoids double-process nesting from `uv run`)
- **Native file dialogs via ctypes** — `dialogs.py` uses `GetOpenFileNameW` / `SHBrowseForFolderW`; called via `asyncio.to_thread()` from browse endpoints; `OFN_NOCHANGEDIR` prevents CWD corruption
- **UI control bar has two inputs** — SPEC (file path) + ROOT (project directory); SPEC browse auto-fills ROOT with parent dir; `/api/run` accepts `project_root` field (backward-compatible, empty = spec's parent)
- **Golem.ps1 uses polling loop, not `WaitForExit()`** — .NET `WaitForExit()` swallows Ctrl+C; poll `$proc.HasExited` with `Start-Sleep -Milliseconds 300` instead; `try/finally` kills child process on exit

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


