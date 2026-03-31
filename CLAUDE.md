# Golem — Autonomous Spec Executor

## What This Is
A standalone CLI tool that autonomously executes markdown design specs. Uses a ticket-driven agent hierarchy (Planner → Tech Lead → Writer pairs) with deterministic QA validation and parallel git worktree execution.

For CLI commands, runtime state layout, and PowerShell ops — read agents/commands.md

## Prerequisites
- **Claude CLI authenticated** — run `claude login` once; the SDK spawns `claude` subprocesses using OAuth (not API keys)
- **uv installed** — `pip install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`
- **ripgrep (`rg`) on PATH** — required by spec validation commands; `winget install BurntSushi.ripgrep` or `scoop install ripgrep`

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
- **Windows PATH goes stale in long-lived shells** — tools installed after the shell opened aren't found; `Golem.ps1` calls `Sync-PathFromRegistry` (mirrors `validator.py:_subprocess_env()` registry refresh); `config.py:run_environment_checks()` uses `shutil.which()` which also misses stale PATH
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

For the full project file tree and module descriptions, read agents/project-structure.md

### Testing
- **Framework:** pytest with pytest-asyncio
- **Run:** `uv run pytest` (532+ tests)
- **Focus on:** task graph, state machine, ticket CRUD, config validation, QA checks, worktree merge, CLI commands, progress events, prompt rendering
- **Do NOT mock** the Claude Agent SDK in tests — test the orchestration logic around it
- **Test count:** `uv run golem version` shows the current test count

### Testing Gotchas
- **Use `tmp_path` fixture, not `tempfile.TemporaryDirectory()`** — `monkeypatch.chdir()` inside a `with` block causes Windows PermissionError on cleanup (CWD holds the dir lock)
- **Use `monkeypatch.setattr` for UI module globals** — never assign `ui_module.current_process` or `ui_module.current_cwd` directly in tests; monkeypatch ensures cleanup even on failure
- **Rich table wrapping breaks string assertions** — assert on short strings or individual words; Rich wraps/truncates cell content in narrow terminals
- **Mock `run_planner`/`run_tech_lead` in CLI tests** — any test that proceeds past stale-state check will hang trying to start the Claude SDK
- **Mock `run_session` in server tests** — session creation uses in-process `run_session()`, not subprocesses; `patch("golem.server.run_session", side_effect=async_noop)` where `async_noop` does `await asyncio.sleep(999)`
- **Pause/resume returns 400 for in-process sessions** — SIGSTOP/SIGCONT don't apply to coroutines; only subprocess-based sessions support pause
- **`conftest.py` has shared fixtures** — `make_ticket`, `git_repo`, `golem_dir`, `write_ticket_json` — use these instead of redefining per-file
- **Validate PowerShell syntax from bash** — write a temp `.ps1` that calls `[System.Management.Automation.Language.Parser]::ParseFile(...)` and run with `powershell.exe -NoProfile -ExecutionPolicy Bypass -File`; bash `$` escaping makes inline PS one-liners unreliable

### Claude Agent SDK
- **Always invoke the `claude-api` skill first** — it has the complete Agent SDK reference (message types, `ClaudeAgentOptions`, hooks, MCP, errors). Do NOT use Context7 (only 12 snippets). Do NOT guess from memory. Fall back to `WebFetch` of `platform.claude.com/docs/en/agent-sdk/python` only if the skill doesn't cover a topic

### FastAPI / UI Gotchas
Use your fast API skills whenever working with FastAPI.

For architectural decisions and design rationale, read agents/architectural-decisions.md

## Do NOT
- Use `pip` directly — use `uv` for everything
- Add dependencies not listed in pyproject.toml without updating the spec
- Use `threading` — the SDK is async-native, use `asyncio`
- Create files outside the project structure defined above
- Use `Any` type — always use explicit types
- Use Context7 for Claude Agent SDK documentation — it only has 12 README snippets; invoke the `claude-api` skill instead (full API reference)
- Write or modify SDK integration code without first invoking the `claude-api` skill to verify current API signatures and patterns
