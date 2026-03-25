# Golem

Autonomous spec executor with parallel workers, validators, and feedback loops.

```bash
pip install golem-cli
golem run spec.md
```

## What It Does

Golem reads a markdown design spec and autonomously implements it:

1. **Plans** — parses the spec into a structured task graph with dependencies
2. **Parallelizes** — identifies independent task groups and runs them in separate git worktrees
3. **Implements** — spawns Claude Code SDK worker sessions (Opus) per task
4. **Validates** — two-tier checks: deterministic (lint, tsc, grep) + AI code review (Sonnet)
5. **Iterates** — failed tasks get specific feedback and retry in fresh sessions
6. **Ships** — merges worktrees and creates a PR with all completed work

## Quick Start

```bash
# Install
pip install golem-cli

# Run a spec
golem run path/to/spec.md

# Dry run (plan only)
golem plan path/to/spec.md

# Resume an interrupted run
golem resume

# Check progress
golem status

# Clean up
golem clean
```

## Requirements

- Python 3.12+
- Git
- Claude Code CLI (authenticated via `ANTHROPIC_API_KEY`)

## License

MIT
