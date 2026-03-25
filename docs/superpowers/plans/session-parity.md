# Session Parity — Implementation Plan Index

## Goal

Give golem's SDK sessions (planner, worker, validator) the same capabilities as a standalone Claude Code session by passing `setting_sources` and the `claude_code` tools preset instead of hardcoded tool lists.

## Architecture Overview

The change is surgical: three `query()` call sites in `worker.py`, `validator.py`, and `planner.py` each replace their `allowed_tools` kwarg with a `tools` preset and add `setting_sources` from config. One new field (`setting_sources`) is added to `GolemConfig`. A fallback `discover_plugins()` function is added to `config.py` but only wired in if Phase 1 testing shows plugins aren't loaded.

No new files in `src/golem/`. One new test file (`tests/test_config.py`). No changes to execution flow, task graph, TUI, or git operations.

## Tech Stack

- Python 3.12+
- `claude-agent-sdk` >=0.1.50 (already installed)
- pytest + pytest-asyncio

## Phase Dependency Graph

```
Phase 1: Config + Session Options
         |
Phase 2: Tests + Verification
         |
Phase 3: Plugin Discovery Fallback (conditional)
```

Phase 1 and 2 are sequential (tests validate Phase 1 changes). Phase 3 is conditional — only implement if Phase 1 smoke testing shows plugins aren't loaded.

## Parallel Opportunities

Phase 1 tasks are parallelizable — config.py, worker.py, validator.py, and planner.py changes are independent edits.

Phase 2 tasks are parallelizable — test_config.py tests and running existing tests are independent.

## Phases

### Phase 1: Config + Session Options
- **File:** phase-1-config-and-sessions.md
- **Tasks:** 4
- **Skills:** None required — changes are mechanical
- **Creates:** (none)
- **Modifies:** `src/golem/config.py`, `src/golem/worker.py`, `src/golem/validator.py`, `src/golem/planner.py`
- **Reference docs:** None
- **Ordering notes:** All 4 tasks are independent and parallelizable

### Phase 2: Tests + Verification
- **File:** phase-2-tests-and-verification.md
- **Tasks:** 2
- **Skills:** `superpowers:test-driven-development` for test writing
- **Creates:** `tests/test_config.py`
- **Modifies:** (none)
- **Reference docs:** None
- **Ordering notes:** Write tests first, then run full suite

### Phase 3: Plugin Discovery Fallback (Conditional)
- **File:** phase-3-plugin-discovery-fallback.md
- **Tasks:** 3
- **Skills:** `superpowers:test-driven-development` for test writing
- **Creates:** (none — adds to existing `config.py` and `test_config.py`)
- **Modifies:** `src/golem/config.py`, `tests/test_config.py`, `src/golem/worker.py`, `src/golem/validator.py`, `src/golem/planner.py`
- **Reference docs:** None
- **Ordering notes:** Implement `discover_plugins()` first, then wire into query() calls, then test. Only implement if Phase 1 smoke testing shows plugins aren't loaded.

## Spec

`docs/superpowers/specs/2026-03-25-session-parity-design.md`

## Reference Docs

None required — all changes use existing `claude_agent_sdk` types already installed.
