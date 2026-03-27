# Golem — Autonomous Spec Executor

## What This Is
A standalone CLI tool that autonomously executes markdown design specs. Uses a ticket-driven agent hierarchy (Planner → Tech Lead → Writer pairs) with deterministic QA validation and parallel git worktree execution.

## Quick Start
```bash
uv sync                          # Install dependencies
uv run golem run spec.md         # Execute a spec (full pipeline)
uv run golem run spec.md --no-classify  # Skip complexity classification
uv run golem run spec.md --dry-run  # Planner only, skip Tech Lead
uv run golem plan spec.md        # Dry run — planner only, no Tech Lead
uv run golem status              # Ticket status table (color-coded)
uv run golem history             # Chronological event timeline
uv run golem inspect TICKET-001  # Full details of a single ticket
uv run golem stats               # Ticket pass rate and counts
uv run golem logs -f             # Tail progress.log (follow mode)
uv run golem resume              # Resume interrupted run from tickets
uv run golem diff                # Git diff from last run
uv run golem export              # Zip .golem/ artifacts
uv run golem pr                  # Create GitHub PR with ticket summaries
uv run golem doctor              # Check environment (claude, uv, git, rg)
uv run golem list-specs          # Find .md spec files in project
uv run golem reset-ticket TICKET-001  # Reset ticket to pending
uv run golem config show         # Print effective config as JSON
uv run golem config set KEY VAL  # Set a config value
uv run golem config reset        # Reset config to defaults
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
- `config.json` — run configuration snapshot
- `tickets/` — structured JSON tickets (communication backbone)
- `plans/` — overview.md + per-task plan files from planner
- `research/` — sub-agent findings (explorer, researcher)
- `references/` — curated external docs for writers
- `progress.log` — timestamped execution events
- `worktrees/` — git worktrees per parallel group

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
    tasks.py            ← v1 legacy (unused by v2, kept for test compat)
    conductor.py        ← Spec complexity classifier (TRIVIAL/SIMPLE/STANDARD/CRITICAL)
    config.py           ← Settings, defaults, model configuration
    progress.py         ← Human-readable progress.log writer
    tui.py              ← Pre-run settings screen + live dashboard
    ui.py               ← FastAPI server (SSE events, subprocess mgmt)
    ui_template.html    ← Self-contained HTML dashboard (no CDN)
    prompts/
      planner.md        ← Planner system prompt template
      worker.md         ← Writer agent system prompt template
      worker_rework.md  ← Escalated prompt for rejected rework attempts
      tech_lead.md      ← Tech Lead agent system prompt template
tests/
  conftest.py           ← Shared fixtures (ticket factory, git repo, golem dir)
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
  test_version.py       ← Version info and architecture string
  test_ui.py            ← UI server endpoints, SSE, helpers
  test_conductor.py     ← Complexity classification tests
  test_hooks.py         ← PreToolUse hook script tests
Golem.ps1               ← PowerShell ops dashboard (server lifecycle + TUI)
.claude/
  hooks/
    block-golem-cli.py          ← Blocks golem CLI commands in SDK sessions
    block-dangerous-git.py      ← Blocks destructive git ops in SDK sessions
    block-ask-user-question.py  ← Blocks AskUserQuestion in headless sessions
```

### Testing
- **Framework:** pytest with pytest-asyncio
- **Run:** `uv run pytest` (314 tests)
- **Focus on:** task graph, state machine, ticket CRUD, config validation, QA checks, worktree merge, CLI commands, progress events, prompt rendering
- **Do NOT mock** the Claude Agent SDK in tests — test the orchestration logic around it
- **Test count:** `uv run golem version` shows the current test count

### Testing Gotchas
- **Use `tmp_path` fixture, not `tempfile.TemporaryDirectory()`** — `monkeypatch.chdir()` inside a `with` block causes Windows PermissionError on cleanup (CWD holds the dir lock)
- **Use `monkeypatch.setattr` for UI module globals** — never assign `ui_module.current_process` or `ui_module.current_cwd` directly in tests; monkeypatch ensures cleanup even on failure
- **Rich table wrapping breaks string assertions** — assert on short strings or individual words; Rich wraps/truncates cell content in narrow terminals
- **Mock `run_planner`/`run_tech_lead` in CLI tests** — any test that proceeds past stale-state check will hang trying to start the Claude SDK
- **`conftest.py` has shared fixtures** — `make_ticket`, `git_repo`, `golem_dir`, `write_ticket_json` — use these instead of redefining per-file

### Claude Agent SDK Gotchas
- **Use `permission_mode="bypassPermissions"`** for all SDK sessions — `acceptEdits` blocks headless file writes
- **Clear `ANTHROPIC_API_KEY` in SDK env** — the env var overrides CLI OAuth auth and fails; use `env=sdk_env()` from `config.py`
- **Use `tools={"type": "preset", "preset": "claude_code"}` + `setting_sources=config.setting_sources`** — replaces old `allowed_tools` lists; gives sessions full CC capabilities
- **`setting_sources` defaults to `["project"]` only** — excludes `"user"` to prevent user-level plugin hooks (e.g. claude-mem SessionEnd) from firing in headless SDK sessions and killing them
- **SDK initialize timeout is monkey-patched to 180s** — the SDK hardcodes 60s with no public API to override; `planner.py` patches `Query.__init__.__defaults__` at import time
- **Capture `AssistantMessage` text blocks as fallback** — `ResultMessage.result` may be empty; check both
- **Validator PASS detection must be fuzzy** — AI models prefix preamble before "PASS:"; search anywhere, not just `startswith`
- **Worktrees auto-install deps** — `create_worktree()` runs `uv sync` (if pyproject.toml) or `npm/bun install` (if package.json) automatically; clears `VIRTUAL_ENV` to avoid conflicts
- **MCP tool naming** — SDK exposes tools as `mcp__<server>__<name>` (e.g. `mcp__golem__create_ticket`). Prompts must use the full prefixed name, not bare names
- **Planner self-healing fallback** — if planner doesn't call `create_ticket` via MCP, `run_planner()` creates a fallback ticket programmatically
- **Tech Lead self-healing merge** — if Tech Lead doesn't merge integration→main, `_ensure_merged_to_main()` does it after the session
- **Planner retry logic** — retries up to 2 times on `CLIConnectionError`/`ClaudeSDKError` with configurable `retry_delay` (default 10s)
- **Agent functions return result dataclasses** — `run_planner()` → `PlannerResult`, `run_tech_lead()` → `TechLeadResult`, `spawn_writer_pair()` → `WriterResult` — each has `cost_usd`, `input_tokens`, `output_tokens`, `turns`, `duration_s`
- **`ResultMessage.usage` is `dict[str, Any] | None`** — guard with `usage = message.usage or {}` then `.get("input_tokens", 0)` etc.
- **`create_pr()` and `verify_pr()` are async** — use `await`; `verify_pr` uses `asyncio.sleep` for polling, not `time.sleep`
- **Worktree cleanup on error** — Tech Lead cleans orphaned worktrees in `finally` block on session failure
- **Verbose SDK streaming** — `planner.py`, `tech_lead.py`, `writer.py` print `[PLANNER]`/`[TECH LEAD]`/`[WRITER]` prefixed messages to stderr showing text blocks, tool calls, and results in real-time
- **`uv run` in worktrees fails if parent `VIRTUAL_ENV` set** — delete `.venv` and `unset VIRTUAL_ENV` before `uv sync` in worktrees
- **`typer.Exit` raises `click.exceptions.Exit`** — tests must catch `ClickExit` from click, not `SystemExit`

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
- **Two-stage QA pipeline** — `run_qa()` runs infrastructure checks first (fast gate); if any fail, spec checks are skipped. `QACheck`/`QAResult` have `cannot_validate` and `stage` fields.
- **PreToolUse safety hooks** — `.claude/hooks/` contains Python scripts that block dangerous agent operations (golem CLI, destructive git, AskUserQuestion) in headless SDK sessions, gated by `GOLEM_SDK_SESSION=1` env var.
- **Self-healing fallbacks** — planner creates fallback tickets, tech lead merges to main, worktrees cleaned on error.
- **MCP tools for orchestration** — ticket CRUD, QA, worktree ops injected via in-process MCP servers.

## Version History
- **v0.2.2** (2026-03-27) — ZeroShot-inspired features: safety hooks, two-stage QA, run economics, complexity conductor, live operator guidance, dispatch hardening. 314 tests. New config: `dispatch_jitter_max`, `conductor_enabled`, `skip_tech_lead`, `planner_max_turns`, `complexity_profiles`.
- **v0.2.1** (2026-03-27) — Auto-dev session: 99 tasks, 239 tests, 12 new CLI commands, dead code cleanup, prompt improvements. Config added: `max_tech_lead_turns`, `sdk_timeout`, `retry_delay`. Removed: `auto_pr`, `max_validator_turns`.
- **v0.2.0** (2026-03-27) — v2 ticket-driven architecture, 185 tests, 112 overnight improvements. See `docs/overnight-log.md` for details.
- **v0.1.0** (2026-03-25) — v1 flat task graph with executor loop.

## Do NOT
- Use `pip` directly — use `uv` for everything
- Add dependencies not listed in pyproject.toml without updating the spec
- Use `threading` — the SDK is async-native, use `asyncio`
- Create files outside the project structure defined above
- Use `Any` type — always use explicit types
