# Golem Project Structure

```
src/
  golem/
    __init__.py
    cli.py              <- CLI entry point (typer + rich)
    client.py           <- HTTP client (GolemClient, find_server) for server communication
    conductor.py        <- Spec complexity + preflight (classify, topology, conflicts)
    config.py           <- Settings, defaults, model configuration
    dialogs.py          <- Native Windows file/folder picker dialogs (ctypes win32)
    mcp_sse.py          <- MCP-over-SSE registry + JSONRPC routing (ready for when CLI fixes SSE/HTTP)
    merge.py            <- Merge coordinator (FIFO queue, PR lifecycle, rebase cascade)
    planner.py          <- Spec -> research + tickets (sub-agent architecture)
    events.py           <- Typed EventBus for agent observability (21 GolemEvent types)
    progress.py         <- Human-readable progress.log writer
    qa.py               <- Deterministic QA tool (subprocess checks)
    server.py           <- Multi-session FastAPI server (concurrent spec execution)
    session.py          <- Session metadata, state constants, directory scaffolding
    supervisor.py       <- Agent stall detection, circuit breakers, supervised sessions
    tasks.py            <- v1 legacy (unused by v2, kept for test compat)
    tech_lead.py        <- Tech Lead agent orchestrator (ticket dispatch)
    tickets.py          <- Ticket store (JSON-based, .golem/tickets/)
    tools.py            <- Custom SDK tools for Tech Lead sessions
    tui.py              <- Pre-run settings screen + live dashboard
    ui.py               <- Legacy single-session dashboard (SSE events, subprocess mgmt)
    ui_template.html    <- Self-contained HTML dashboard (no CDN)
    validator.py        <- Subprocess env helpers (_subprocess_env, _normalize_cmd)
    version.py          <- Version/platform metadata utility
    worktree.py         <- Git worktree creation/management/merge
    writer.py           <- Writer pair spawner (ticket-driven coding)
    prompts/
      planner.md        <- Planner system prompt template
      worker.md         <- Writer agent system prompt template
      worker_rework.md  <- Escalated prompt for rejected rework attempts
      tech_lead.md      <- Tech Lead agent system prompt template
tests/
  conftest.py           <- Shared fixtures (ticket factory, git repo, golem dir)
  __init__.py
  test_cli.py           <- Spec validation, infra check detection
  test_client.py        <- GolemClient and find_server tests
  test_conductor.py     <- Complexity classification tests
  test_conflicts.py     <- Cross-session conflict detection tests
  test_config.py        <- GolemConfig, setting_sources, validation
  test_events.py        <- EventBus, backends, roundtrip, subscribe filters
  test_hooks.py         <- PreToolUse hook script tests
  test_mcp_durability.py  <- MCP handler durability (concurrent, sequential, error recovery)
  test_mcp_sse.py         <- MCP SSE registry, endpoints, lifecycle integration
  test_merge.py         <- Merge coordinator queue, PR, rebase tests
  test_preflight.py     <- Topology derivation, conflict prediction, env checks, cost
  test_planner.py       <- Planner directory creation and ticket output
  test_progress.py      <- Progress event logging (v2 milestones)
  test_qa.py            <- QA checks, autofix, infra detection
  test_server.py        <- Multi-session server endpoints, SSE, lifecycle
  test_session.py       <- Session metadata and directory scaffolding
  test_supervisor.py    <- Stall detection and supervised session tests
  test_tasks.py         <- Task graph parsing and state machine
  test_tech_lead.py     <- Self-healing merge + worktree cleanup
  test_tickets.py       <- Ticket store CRUD and concurrency
  test_tools.py         <- Tech Lead tool dispatch
  test_ui.py            <- UI server endpoints, SSE, helpers
  test_validator.py     <- Subprocess env helpers
  test_version.py       <- Version info and architecture string
  test_worktree.py      <- Git worktree operations + merge conflict handling
  test_writer.py        <- Writer prompt building and spawning
Golem.ps1               <- PowerShell ops dashboard (server lifecycle + TUI)
.claude/
  hooks/
    block-golem-cli.py          <- Blocks golem CLI commands in SDK sessions
    block-dangerous-git.py      <- Blocks destructive git ops in SDK sessions
    block-ask-user-question.py  <- Blocks AskUserQuestion in headless sessions
```

> Update this file whenever significant changes are made to the overall application structure.
