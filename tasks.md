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

### [x] 14. `golem history` — Show ticket event timeline (DONE: 1e9e4bf)
**Size:** Small | **Files:** `cli.py`
**What:** New CLI command that shows the full event history across all tickets in chronological order.
**How:** Read all tickets, flatten all TicketEvents, sort by timestamp, print as a timeline.
**Test:** Existing tests pass.
**Done when:** `uv run pytest` passes.

### [x] 15. Worktree Cleanup on Error — Don't leave orphaned worktrees (DONE: 292a48b)
**Size:** Small | **Files:** `tech_lead.py`
**What:** If the Tech Lead session fails, clean up any worktrees it created so they don't block future runs.
**How:** Wrap `run_tech_lead` in a try/finally that lists and removes golem worktrees on failure.
**Test:** Existing tests pass.
**Done when:** `uv run pytest` passes.

---

## Batch 3: Overnight Queue (continued)

### [x] 16. `golem inspect <ticket-id>` — Show full ticket details (DONE: e86cdec)
**Size:** Small | **Files:** `cli.py`
**What:** New CLI command to show full details of a single ticket: all fields, full context, complete event history.
**How:** Read ticket by ID, print formatted output with all sections.
**Done when:** `uv run pytest` passes.

### [x] 17. Planner Prompt — Inject infra checks into planner context (DONE: 6255c85)
**Size:** Small | **Files:** `planner.py`
**What:** The planner doesn't know about detected infrastructure checks. Pass them so it can include them in QA check lists for writers.
**How:** Add `{infrastructure_checks}` template variable to planner.md and inject in run_planner().
**Done when:** `uv run pytest` passes.

### [x] 18. CLAUDE.md Update — Reflect overnight changes (DONE: b69e64c)
**Size:** Small | **Files:** `CLAUDE.md`
**What:** CLAUDE.md project structure and CLI commands are stale. Update to reflect all new commands (history, inspect) and new modules (qa.py, tools.py, tech_lead.py, writer.py).
**Done when:** CLAUDE.md matches reality.

### [x] 19. TicketStore.list_tickets — Case-insensitive ticket file glob (DONE: 1fbcead)
**Size:** Small | **Files:** `tickets.py`, `tests/test_tickets.py`
**What:** list_tickets accepts status_filter but not assigned_to_filter, even though the spec defined it.
**How:** Add assigned_to parameter to list_tickets(). Add test.
**Done when:** `uv run pytest` passes.

### [x] 20. Tech Lead Retry — Same retry pattern as planner (DONE: 8906083)
**Size:** Small | **Files:** `tech_lead.py`
**What:** Tech Lead should retry on transient SDK errors, same as the planner (task 9).
**How:** Wrap query() in retry loop with same _MAX_RETRIES/_RETRY_DELAY_S pattern.
**Done when:** `uv run pytest` passes.

---

## Batch 4: Overnight Queue (continued)

### [x] 21. `golem logs` (DONE: 3f9d627) — Tail progress.log in real-time
**Size:** Small | **Files:** `cli.py`
**What:** New CLI command that tails `.golem/progress.log` and prints new lines as they appear.
**How:** Read progress.log, print existing lines, then poll for new lines every 1s.
**Done when:** `uv run pytest` passes and `golem logs --help` works.

### [x] 22. Writer Retry (DONE: 09eddcb) — Same retry pattern as planner/tech lead
**Size:** Small | **Files:** `writer.py`
**What:** Writer should retry on transient SDK errors.
**How:** Wrap query() in retry loop with _MAX_RETRIES/_RETRY_DELAY_S.
**Done when:** `uv run pytest` passes.

### [x] 23. TicketStore.read (DONE: 8910f4c) — Case-insensitive lookup
**Size:** Small | **Files:** `tickets.py`, `tests/test_tickets.py`
**What:** `store.read("ticket-001")` should find `TICKET-001.json`. Currently exact match only.
**How:** Try exact match first, then case-insensitive fallback.
**Done when:** `uv run pytest` passes with new test.

### [x] 24. Config `save_config` (DONE: 02cee63) — Pretty-print with sorted keys
**Size:** Small | **Files:** `config.py`
**What:** config.json should have sorted keys for deterministic diffs.
**How:** Add `sort_keys=True` to json.dump.
**Done when:** `uv run pytest` passes.

### [x] 25. `golem plan` (DONE: d6df574) — Show plan summary after completion
**Size:** Small | **Files:** `cli.py`
**What:** After planner completes, show a summary of what was planned (task count, research files, reference files).
**How:** Read plans/ and research/ directories after planner returns, print counts.
**Done when:** `uv run pytest` passes.

---

## Batch 5: Overnight Queue (continued)

### [x] 26. `golem run` — Show elapsed time on completion (DONE: b9fc81e)
**Size:** Small | **Files:** `cli.py`
**What:** Print total elapsed time when `golem run` finishes (e.g. "Run complete in 4m 32s").
**How:** Capture `time.monotonic()` at start, compute delta at end, format as Xm Ys.
**Done when:** `uv run pytest` passes.

### [x] 27. TicketStore.create (DONE: e275da1) — Normalize ticket IDs to uppercase
**Size:** Small | **Files:** `tickets.py`, `tests/test_tickets.py`
**What:** MCP tool creates lowercase IDs (ticket-001). Store should normalize to TICKET-001 on create.
**How:** In create(), uppercase the generated ID before writing. Add test.
**Done when:** `uv run pytest` passes.

### [x] 28. `golem run` — Print ticket summary (DONE: 36058b9) before Tech Lead starts
**Size:** Small | **Files:** `cli.py`
**What:** After planner creates the ticket, show the ticket title and plan file path before handing off to Tech Lead.
**How:** Read the ticket from store after planner returns, print key fields.
**Done when:** `uv run pytest` passes.

### [x] 29. Progress logger (DONE: f7ac19f) — Add run elapsed time to completion event
**Size:** Small | **Files:** `progress.py`, `cli.py`
**What:** TECH_LEAD_COMPLETE event should include total elapsed time.
**How:** Add optional `elapsed_s` param to `log_tech_lead_complete()`, format in the log line.
**Done when:** `uv run pytest` passes.

### [x] 30. `golem clean` (DONE: be00fb4) — Prompt for confirmation unless --force
**Size:** Small | **Files:** `cli.py`
**What:** `golem clean` is destructive. Ask "Are you sure?" unless --force is passed.
**How:** Add `--force` flag, use `typer.confirm()` when not forced.
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
