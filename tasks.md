# Golem — Task Board

## Overnight Work Queue (feat/overnight-improvements)

Tasks are ordered by priority. Work through them top to bottom. Each task should be:
- Implemented
- Tested (`uv run pytest`)
- Committed with a descriptive message
- Checked off below

### [x] 1. SDK Message Streaming — Restore stderr output for all agent sessions (DONE: ea785c1)
**Size:** Small | **Files:** `planner.py`, `tech_lead.py`, `writer.py`
**What:** The v1 codebase had `[PLANNER]`/`[WORKER]`/`[VALIDATOR]` prefixed stderr streaming that showed text blocks, tool calls, and results in real-time. The v2 rewrite lost this — all sessions just do `pass` or silently iterate. Restore stderr streaming for all three agent types so we can see what they're doing.
**How:** In the `async for message in query(...)` loops, print `AssistantMessage` text blocks and `ToolUseBlock`/`ToolResultBlock` info to stderr with role prefixes. Match the v1 pattern.
**Test:** Existing tests should still pass. Manual verification by running golem.
**Done when:** `uv run pytest` passes and stderr shows agent activity during runs.

### [ ] 2. Tech Lead Merge-to-Main — Fix the integration→main merge gap
**Size:** Small | **Files:** `prompts/tech_lead.md`, `tech_lead.py`
**What:** Tech Lead doesn't merge integration branch to main. Add explicit prompt instruction + self-healing fallback in `run_tech_lead()`.
**How:**
- Add "CRITICAL: merge integration branch into main" section to tech_lead.md prompt
- In `run_tech_lead()`, after the session completes, check if main has new commits. If not, do `git merge` programmatically.
**Test:** Existing tests pass. The merge logic is a fallback — verify it doesn't break when there's nothing to merge.
**Done when:** `uv run pytest` passes and `run_tech_lead()` has merge verification.

### [ ] 3. Ticket Lifecycle Updates — Tech Lead updates ticket status
**Size:** Small | **Files:** `prompts/tech_lead.md`
**What:** Tech Lead prompt should explicitly instruct ticket status updates at each stage.
**How:** Add clear instructions in tech_lead.md: "When dispatching a writer, update ticket to in_progress. When writer completes, update to qa_passed. After review, update to approved or needs_work."
**Test:** Existing tests pass (prompt-only change).
**Done when:** tech_lead.md has explicit status update instructions at each lifecycle point.

### [ ] 4. CLI `golem status` — Show ticket details in a rich table
**Size:** Medium | **Files:** `cli.py`
**What:** The `status` command currently reads tickets but the display is basic. Show a rich table with: ticket ID, title, status, assigned_to, last event timestamp.
**How:** Read all tickets from TicketStore, build a rich Table, print it.
**Test:** Add a test that creates tickets in a temp dir and verifies the status output.
**Done when:** `uv run pytest` passes and `golem status` shows a formatted table.

### [ ] 5. Config Validation — Warn on invalid model names
**Size:** Small | **Files:** `config.py`
**What:** If someone sets an invalid model name in config.json, golem silently fails deep in the SDK. Validate model names on load.
**How:** Add a `validate()` method to `GolemConfig` that checks known model patterns. Print a warning (not error) for unknown models.
**Test:** Add test for validate() with valid and invalid model names.
**Done when:** `uv run pytest` passes.

### [ ] 6. Progress Events — Write structured events to progress.log
**Size:** Small | **Files:** `progress.py`, `planner.py`, `tech_lead.py`
**What:** The v2 pipeline doesn't write to progress.log at all. Add event logging for key milestones.
**How:** Call `ProgressLogger` at: planner start/end, tech lead start/end, ticket creation, writer dispatch, QA results.
**Test:** Existing progress tests should still pass.
**Done when:** `uv run pytest` passes and progress.log gets written during runs.

### [ ] 7. Better Error Messages — Wrap SDK errors with context
**Size:** Small | **Files:** `planner.py`, `tech_lead.py`, `writer.py`
**What:** When the SDK fails (timeout, auth, etc.), the raw traceback is unhelpful. Wrap with context about what was happening.
**How:** Add try/except around `query()` calls with descriptive error messages that include the agent role, what it was doing, and suggestions (e.g., "check claude login").
**Test:** Existing tests pass.
**Done when:** `uv run pytest` passes.

### [ ] 8. `golem clean` — Also clean up stale git branches
**Size:** Small | **Files:** `cli.py`
**What:** `golem clean` removes `.golem/` but leaves behind `golem/*` git branches from previous runs. Clean those too.
**How:** After removing `.golem/`, list local branches matching `golem/*` and delete them.
**Test:** Add a test that creates golem branches, runs clean, verifies they're gone.
**Done when:** `uv run pytest` passes.

### [ ] 9. Retry Logic — Planner retries on SDK timeout
**Size:** Small | **Files:** `planner.py`
**What:** The planner sometimes times out on the SDK initialize (even with the 180s monkey-patch). Add a simple retry with backoff.
**How:** Wrap the `query()` call in a retry loop (max 2 retries, 10s backoff). Log retries to stderr.
**Test:** Existing tests pass.
**Done when:** `uv run pytest` passes.

### [ ] 10. `golem version` — Show v2 architecture info
**Size:** Small | **Files:** `cli.py`, `version.py`
**What:** `golem version` should indicate it's running the v2 ticket-driven architecture.
**How:** Update version string or add "Architecture: v2 (ticket-driven)" to version output.
**Test:** Verify version output includes architecture info.
**Done when:** `uv run pytest` passes.

---

## Ideas & Future Work (Not Yet Scheduled)

### Agent Observability / Live Streaming (EXPANDED)
Beyond basic stderr streaming (task 1), we eventually want:
- A TUI dashboard showing all active agents and their current activity
- The web UI (`golem ui`) to show real-time agent activity via SSE
- Structured event format that both TUI and web UI can consume

### Writer Gets Ticket Tools
Give the writer MCP access to `update_ticket` so it can self-report status. Requires creating a combined MCP server with both QA and ticket tools for writer sessions.

### Parallel Writer Verification
After Tech Lead dispatches multiple writers in parallel, verify all worktrees have changes before merging. Currently no validation that writers actually wrote code.
