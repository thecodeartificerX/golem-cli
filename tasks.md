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

### [x] 2. Tech Lead Merge-to-Main — Fix the integration→main merge gap (DONE: bf1b1fa)
**Size:** Small | **Files:** `prompts/tech_lead.md`, `tech_lead.py`
**What:** Tech Lead doesn't merge integration branch to main. Add explicit prompt instruction + self-healing fallback in `run_tech_lead()`.
**How:**
- Add "CRITICAL: merge integration branch into main" section to tech_lead.md prompt
- In `run_tech_lead()`, after the session completes, check if main has new commits. If not, do `git merge` programmatically.
**Test:** Existing tests pass. The merge logic is a fallback — verify it doesn't break when there's nothing to merge.
**Done when:** `uv run pytest` passes and `run_tech_lead()` has merge verification.

### [x] 3. Ticket Lifecycle Updates — Tech Lead updates ticket status (DONE: 1b5617b)
**Size:** Small | **Files:** `prompts/tech_lead.md`
**What:** Tech Lead prompt should explicitly instruct ticket status updates at each stage.
**How:** Add clear instructions in tech_lead.md: "When dispatching a writer, update ticket to in_progress. When writer completes, update to qa_passed. After review, update to approved or needs_work."
**Test:** Existing tests pass (prompt-only change).
**Done when:** tech_lead.md has explicit status update instructions at each lifecycle point.

### [x] 4. CLI `golem status` — Show ticket details in a rich table (DONE: 591bcd0)
**Size:** Medium | **Files:** `cli.py`
**What:** The `status` command currently reads tickets but the display is basic. Show a rich table with: ticket ID, title, status, assigned_to, last event timestamp.
**How:** Read all tickets from TicketStore, build a rich Table, print it.
**Test:** Add a test that creates tickets in a temp dir and verifies the status output.
**Done when:** `uv run pytest` passes and `golem status` shows a formatted table.

### [x] 5. Config Validation — Warn on invalid model names (DONE: f8967e2)
**Size:** Small | **Files:** `config.py`
**What:** If someone sets an invalid model name in config.json, golem silently fails deep in the SDK. Validate model names on load.
**How:** Add a `validate()` method to `GolemConfig` that checks known model patterns. Print a warning (not error) for unknown models.
**Test:** Add test for validate() with valid and invalid model names.
**Done when:** `uv run pytest` passes.

### [x] 6. Progress Events — Write structured events to progress.log (DONE: 7c002bf)
**Size:** Small | **Files:** `progress.py`, `planner.py`, `tech_lead.py`
**What:** The v2 pipeline doesn't write to progress.log at all. Add event logging for key milestones.
**How:** Call `ProgressLogger` at: planner start/end, tech lead start/end, ticket creation, writer dispatch, QA results.
**Test:** Existing progress tests should still pass.
**Done when:** `uv run pytest` passes and progress.log gets written during runs.

### [x] 7. Better Error Messages — Wrap SDK errors with context (DONE: 78f2d26)
**Size:** Small | **Files:** `planner.py`, `tech_lead.py`, `writer.py`
**What:** When the SDK fails (timeout, auth, etc.), the raw traceback is unhelpful. Wrap with context about what was happening.
**How:** Add try/except around `query()` calls with descriptive error messages that include the agent role, what it was doing, and suggestions (e.g., "check claude login").
**Test:** Existing tests pass.
**Done when:** `uv run pytest` passes.

### [x] 8. `golem clean` — Also clean up stale git branches (DONE: 30aeddd)
**Size:** Small | **Files:** `cli.py`
**What:** `golem clean` removes `.golem/` but leaves behind `golem/*` git branches from previous runs. Clean those too.
**How:** After removing `.golem/`, list local branches matching `golem/*` and delete them.
**Test:** Add a test that creates golem branches, runs clean, verifies they're gone.
**Done when:** `uv run pytest` passes.

### [x] 9. Retry Logic — Planner retries on SDK timeout (DONE: d9231ce)
**Size:** Small | **Files:** `planner.py`
**What:** The planner sometimes times out on the SDK initialize (even with the 180s monkey-patch). Add a simple retry with backoff.
**How:** Wrap the `query()` call in a retry loop (max 2 retries, 10s backoff). Log retries to stderr.
**Test:** Existing tests pass.
**Done when:** `uv run pytest` passes.

### [x] 10. `golem version` — Show v2 architecture info (DONE: f6431e6)
**Size:** Small | **Files:** `cli.py`, `version.py`
**What:** `golem version` should indicate it's running the v2 ticket-driven architecture.
**How:** Update version string or add "Architecture: v2 (ticket-driven)" to version output.
**Test:** Verify version output includes architecture info.
**Done when:** `uv run pytest` passes.

---

## Batch 2: Overnight Queue (continued)

### [x] 11. Writer Gets Ticket Tools — Combined MCP server for writers (DONE: 7a09659)
**Size:** Small | **Files:** `tools.py`, `writer.py`
**What:** Give writers MCP access to `update_ticket` so they can self-report status to `ready_for_review`.
**How:** Create `create_writer_mcp_server()` in tools.py that includes both `run_qa` and `update_ticket`. Update writer.py to use it.
**Test:** Existing tests pass. Add test that `create_writer_mcp_server` returns both tools.
**Done when:** `uv run pytest` passes.

### [x] 12. `golem resume` — Re-spawn Tech Lead from existing tickets (DONE: aa446b2)
**Size:** Small | **Files:** `cli.py`
**What:** The resume command should find the last planner ticket and re-spawn the Tech Lead.
**How:** Read tickets from store, find the tech_lead ticket, call `run_tech_lead()`.
**Test:** Existing tests pass.
**Done when:** `uv run pytest` passes.

### [x] 13. Spec Validation — Check spec has required sections before running (DONE: d56c596)
**Size:** Small | **Files:** `cli.py`
**What:** Before running the planner, validate the spec has tasks/sections. Catch empty or malformed specs early.
**How:** Read spec, check it has at least one `###` or `**` section. Print warning if spec looks empty.
**Test:** Add test for the validation function.
**Done when:** `uv run pytest` passes.

### [ ] 14. `golem history` — Show ticket event timeline
**Size:** Small | **Files:** `cli.py`
**What:** New CLI command that shows the full event history across all tickets in chronological order.
**How:** Read all tickets, flatten all TicketEvents, sort by timestamp, print as a timeline.
**Test:** Existing tests pass.
**Done when:** `uv run pytest` passes.

### [ ] 15. Worktree Cleanup on Error — Don't leave orphaned worktrees
**Size:** Small | **Files:** `tech_lead.py`
**What:** If the Tech Lead session fails, clean up any worktrees it created so they don't block future runs.
**How:** Wrap `run_tech_lead` in a try/finally that lists and removes golem worktrees on failure.
**Test:** Existing tests pass.
**Done when:** `uv run pytest` passes.

---

## Ideas & Future Work (Not Yet Scheduled)

### Agent Observability / Live Streaming (EXPANDED)
Beyond basic stderr streaming (task 1), we eventually want:
- A TUI dashboard showing all active agents and their current activity
- The web UI (`golem ui`) to show real-time agent activity via SSE
- Structured event format that both TUI and web UI can consume

### Parallel Writer Verification
After Tech Lead dispatches multiple writers in parallel, verify all worktrees have changes before merging. Currently no validation that writers actually wrote code.
