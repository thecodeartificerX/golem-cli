# Golem — Autonomous Spec Executor

## What This Is
A standalone CLI tool that autonomously executes markdown design specs. Parses specs into structured task graphs, runs parallel workers in git worktrees, validates with two-tier checks (deterministic + AI), and creates PRs with completed work.

## Quick Start
```bash
uv sync                          # Install dependencies
uv run golem run spec.md         # Execute a spec (full pipeline)
uv run golem plan spec.md        # Dry run — planner only, no Tech Lead
uv run golem status              # Ticket status table (color-coded)
uv run golem history             # Chronological event timeline
uv run golem inspect TICKET-001  # Full details of a single ticket
uv run golem logs -f             # Tail progress.log (follow mode)
uv run golem resume              # Resume interrupted run from tickets
uv run golem clean               # Remove .golem/ + golem/* branches
uv run golem version             # Version, architecture, Python, platform
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
- **Pre-existing lint errors** — `cli.py`, `planner.py`, `tui.py`, `progress.py`, `tasks.py`, and some test files have known ruff warnings; don't fix unless explicitly asked
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
    planner.py          ← Spec → research + tickets (sub-agent architecture)
    tech_lead.py        ← Tech Lead agent orchestrator (ticket dispatch)
    writer.py           ← Writer pair spawner (ticket-driven coding)
    tickets.py          ← Ticket store (JSON-based, .golem/tickets/)
    tools.py            ← Custom SDK tools for Tech Lead sessions
    qa.py               ← Deterministic QA tool (subprocess checks)
    validator.py        ← Subprocess env helpers (_subprocess_env, _normalize_cmd)
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
      worker.md         ← Writer agent system prompt template
      tech_lead.md      ← Tech Lead agent system prompt template
tests/
  __init__.py
  test_tasks.py         ← Task graph parsing and state machine
  test_planner.py       ← Planner directory creation and ticket output
  test_tickets.py       ← Ticket store CRUD and concurrency
  test_tools.py         ← Tech Lead tool dispatch
  test_qa.py            ← QA checks, autofix, infra detection
  test_writer.py        ← Writer prompt building and spawning
  test_worktree.py      ← Git worktree operations + merge conflict handling
  test_tech_lead.py     ← Self-healing merge + worktree cleanup
  test_validator.py     ← Subprocess env helpers
  test_config.py        ← GolemConfig, setting_sources, validation
  test_cli.py           ← Spec validation, infra check detection
  test_progress.py      ← Progress event logging (v2 milestones)
  test_ui.py            ← UI server endpoints, SSE, helpers
Golem.ps1               ← PowerShell ops dashboard (server lifecycle + TUI)
```

### Testing
- **Framework:** pytest with pytest-asyncio
- **Run:** `uv run pytest` (150+ tests)
- **Focus on:** task graph, state machine, ticket CRUD, config validation, QA checks, worktree merge, CLI commands, progress events, prompt rendering
- **Do NOT mock** the Claude Agent SDK in tests — test the orchestration logic around it
- **Test count:** `uv run golem version` shows the current test count

### Claude Agent SDK Gotchas
- **Use `permission_mode="bypassPermissions"`** for all SDK sessions — `acceptEdits` blocks headless file writes
- **Clear `ANTHROPIC_API_KEY` in SDK env** — the env var overrides CLI OAuth auth and fails; use `env=sdk_env()` from `config.py`
- **Use `tools={"type": "preset", "preset": "claude_code"}` + `setting_sources=config.setting_sources`** — replaces old `allowed_tools` lists; gives sessions full CC capabilities
- **`setting_sources` defaults to `["project"]` only** — excludes `"user"` to prevent user-level plugin hooks (e.g. claude-mem SessionEnd) from firing in headless SDK sessions and killing them
- **SDK initialize timeout is monkey-patched to 180s** — the SDK hardcodes 60s with no public API to override; `planner.py` patches `Query.__init__.__defaults__` at import time
- **Capture `AssistantMessage` text blocks as fallback** — `ResultMessage.result` may be empty; check both
- **Validator PASS detection must be fuzzy** — AI models prefix preamble before "PASS:"; search anywhere, not just `startswith`
- **Run `uv sync` in worktrees after creation** — new worktrees lack venv; module imports fail without it
- **MCP tool naming** — SDK exposes tools as `mcp__<server>__<name>` (e.g. `mcp__golem__create_ticket`). Prompts must use the full prefixed name, not bare names
- **Planner self-healing fallback** — if planner doesn't call `create_ticket` via MCP, `run_planner()` creates a fallback ticket programmatically
- **Tech Lead self-healing merge** — if Tech Lead doesn't merge integration→main, `_ensure_merged_to_main()` does it after the session
- **Planner retry logic** — retries up to 2 times on `CLIConnectionError`/`ClaudeSDKError` with 10s delay
- **Worktree cleanup on error** — Tech Lead cleans orphaned worktrees in `finally` block on session failure
- **Verbose SDK streaming** — `planner.py`, `tech_lead.py`, `writer.py` print `[PLANNER]`/`[TECH LEAD]`/`[WRITER]` prefixed messages to stderr showing text blocks, tool calls, and results in real-time

### FastAPI / UI Gotchas
- **Pydantic models must be module-level** — defining `BaseModel` subclasses inside `create_app()` breaks FastAPI's annotation resolution; requests get 422 instead of binding to the body
- **SSE tests must drive the generator directly** — `TestClient` hangs on infinite SSE streams; use `async for` with early `break` + `aclose()` instead
- **UI server for direct PID control:** `python -m uvicorn golem.ui:create_app --factory --host 127.0.0.1 --port 9664` (when venv is activated, avoids double-process nesting from `uv run`)
- **Native file dialogs via ctypes** — `dialogs.py` uses `GetOpenFileNameW` / `SHBrowseForFolderW`; called via `asyncio.to_thread()` from browse endpoints; `OFN_NOCHANGEDIR` prevents CWD corruption
- **UI control bar has two inputs** — SPEC (file path) + ROOT (project directory); SPEC browse auto-fills ROOT with parent dir; `/api/run` accepts `project_root` field (backward-compatible, empty = spec's parent)
- **Golem.ps1 uses polling loop, not `WaitForExit()`** — .NET `WaitForExit()` swallows Ctrl+C; poll `$proc.HasExited` with `Start-Sleep -Milliseconds 300` instead; `try/finally` kills child process on exit

## Key Design Decisions
- **Ticket-driven agent hierarchy (v2)** — Planner → Tech Lead → Writer pairs. Communication via structured JSON tickets in `.golem/tickets/`.
- **Planner spawns sub-agents** — Explorer (Haiku) + Researcher (Sonnet) sub-agents write to `.golem/research/`, planner synthesizes into `.golem/plans/` and `.golem/references/`.
- **Tech Lead is persistent** — reads plans, creates worktrees, dispatches writers, reviews work, merges, creates PR. Single long-running SDK session.
- **Writers are ephemeral** — spawned per ticket, run in worktrees, have QA + ticket update tools via MCP.
- **Deterministic QA first** — `run_qa()` runs subprocess checks (ruff, tests) before any AI review.
- **Self-healing fallbacks** — planner creates fallback tickets, tech lead merges to main, worktrees cleaned on error.
- **MCP tools for orchestration** — ticket CRUD, QA, worktree ops injected via in-process MCP servers.

## Do NOT
- Use `pip` directly — use `uv` for everything
- Add dependencies not listed in pyproject.toml without updating the spec
- Use `threading` — the SDK is async-native, use `asyncio`
- Create files outside the project structure defined above
- Use `Any` type — always use explicit types


