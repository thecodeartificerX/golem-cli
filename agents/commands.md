# Golem CLI Commands & Runtime State

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
uv run golem ui                  # Launch web dashboard (port 7665)
uv run golem server start        # Start multi-session server (port 7665)
uv run golem server stop         # Stop running server
uv run golem server status       # Check server status
uv run golem pause SESSION       # Pause a running session
uv run golem resume SESSION      # Resume a paused session
uv run golem kill SESSION        # Kill a running session
uv run golem guidance SESSION    # Send guidance to a session
uv run golem tickets SESSION     # Show tickets for a session
uv run golem cost SESSION        # Show run cost for a session
uv run golem merge SESSION       # Enqueue session for merge
uv run golem approve SESSION     # Approve and merge session's PR
uv run golem merge-queue         # Show current merge queue
uv run golem conflicts           # Show cross-session file conflicts
.\Golem.ps1                      # PowerShell ops dashboard (env checks + multi-session server)
.\Golem.ps1 -Clean               # Wipe .golem/ then start fresh
.\Golem.ps1 -Port 8000           # Use a custom port (default: 7665)
uv run pytest                    # Run tests
```

## Runtime State (`.golem/`)
Created by `golem run` in the project root (gitignored):
- `config.json` — run configuration snapshot
- `tickets/` — structured JSON tickets (communication backbone)
- `plans/` — overview.md + per-task plan files from planner
- `research/` — sub-agent findings (explorer, researcher)
- `references/` — curated external docs for writers
- `progress.log` — timestamped execution events
- `events.jsonl` — structured GolemEvent stream (JSONL, one event per line)
- `worktrees/` — git worktrees per parallel group
- `sessions/` — per-session state directories (config, tickets, plans, logs)

> Update this file whenever significant changes are made to CLI commands or runtime state layout.
