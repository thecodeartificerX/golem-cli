# Golem Architectural Decisions

- **Ticket-driven agent hierarchy (v2)** — Planner -> Tech Lead -> Writer pairs. Communication via structured JSON tickets in `.golem/tickets/`.
- **Planner spawns sub-agents** — Explorer (Haiku) + Researcher (Sonnet) sub-agents write to `.golem/research/`, planner synthesizes into `.golem/plans/` and `.golem/references/`.
- **Tech Lead is persistent** — reads plans, creates worktrees, dispatches writers, reviews work, merges, creates PR. Single long-running SDK session.
- **Writers are ephemeral** — spawned per ticket, run in worktrees, have QA + ticket update tools via MCP.
- **Two-stage QA pipeline** — `run_qa()` runs infrastructure checks first (fast gate); if any fail, spec checks are skipped. `QACheck`/`QAResult` have `cannot_validate` and `stage` fields.
- **PreToolUse safety hooks** — `.claude/hooks/` contains Python scripts that block dangerous agent operations (golem CLI, destructive git, AskUserQuestion) in headless SDK sessions, gated by `GOLEM_SDK_SESSION=1` env var.
- **Session-scoped state** — each spec execution is a "session" with `.golem/sessions/<id>/` directory. Tickets, plans, and worktree branches are namespaced under `golem/{session_id}/` to enable concurrent execution.
- **Self-healing fallbacks** — planner creates fallback tickets, tech lead merges to main, worktrees cleaned on error.
- **Merge coordinator** — FIFO merge queue with JSON persistence. Auto-enqueues sessions on completion, creates PRs via `gh`, rebase cascades queued sessions after each merge, detects cross-session file conflicts.
- **CLI-as-client architecture** — CLI is a thin HTTP client (`client.py`) that delegates to the server. `find_server()` reads `.golem/server.json` with cross-platform PID liveness check. `--no-server` flag preserves direct execution for CI.
- **Default port is 7665** — chosen to avoid conflicts with WSL port forwarding (`wslrelay.exe` commonly occupies 9664); all defaults in `cli.py`, `server.py`, `client.py`, `config.py`, `ui.py`, and `Golem.ps1` must stay in sync.
- **Cross-session intelligence** — conflict resolution strategies, session analytics, cost aggregation across concurrent sessions.
- **MCP tools for orchestration** — ticket CRUD, QA, worktree ops injected via in-process MCP servers.

> Update this file whenever significant changes are made to the overall application architecture.
