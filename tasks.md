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

## Batch 6: Overnight Queue (continued)

### [x] 31. (DONE: b7d357a) `golem run` — Detect stale .golem/ and warn
**Size:** Small | **Files:** `cli.py`
**What:** If `.golem/` already exists from a previous run, warn the user and suggest `golem clean` or `--force` to overwrite.
**How:** Check if `.golem/tickets/` has files. If so, print warning. With `--force`, proceed anyway.
**Done when:** `uv run pytest` passes.

### [x] 32. (DONE: 828b322) ProgressLogger tests — Cover v2 event methods
**Size:** Small | **Files:** `tests/test_progress.py` (new or extend existing)
**What:** The new v2 progress methods (planner_start, tech_lead_complete, ticket_created, etc.) have no tests.
**How:** Create tests that write events and verify the log file contents.
**Done when:** `uv run pytest` passes with new tests.

### [x] 33. (DONE: 8a599cc) `golem run` summary — Show final ticket status counts
**Size:** Small | **Files:** `cli.py`
**What:** After run completes, show how many tickets are done/approved/blocked/pending.
**How:** Read all tickets from store after tech lead, print summary counts.
**Done when:** `uv run pytest` passes.

### [x] 34. (DONE: 1052eb8) QA result summary in stderr — Show pass/fail count during runs
**Size:** Small | **Files:** `tools.py`
**What:** When run_qa MCP tool is called, log a summary to stderr (e.g. "[QA] 5/6 checks passed").
**How:** In _handle_run_qa, print summary to stderr after running checks.
**Done when:** `uv run pytest` passes.

### [x] 35. (DONE: a704205) `golem` (no command) — Show help with available commands summary
**Size:** Small | **Files:** `cli.py`
**What:** Running just `golem` with no subcommand should show a clean help message listing all commands.
**How:** Typer already does this by default, but verify and add `invoke_without_command=True` with a help print if missing.
**Done when:** `uv run golem` shows help.

---

## Batch 7: Overnight Queue (continued)

### [x] 36. (DONE: feea667) CLI tests — Test `_validate_spec` and `_detect_infrastructure_checks`
**Size:** Small | **Files:** `tests/test_cli.py` (new)
**What:** _validate_spec and _detect_infrastructure_checks have no tests. Add coverage.
**How:** Test: empty spec exits, non-.md exits, valid spec passes, ruff detected from pyproject.toml, npm lint detected from package.json.
**Done when:** `uv run pytest` passes with new tests.

### [x] 37. (DONE: 05c0379) Worktree tests — Test `merge_group_branches` conflict handling
**Size:** Small | **Files:** `tests/test_worktree.py`
**What:** merge_group_branches conflict path has no test coverage.
**How:** Create two branches that conflict, verify merge returns (False, conflict_info).
**Done when:** `uv run pytest` passes.

### [x] 38. (covered by task 36) QA test — Test `detect_infrastructure_checks` for tsconfig
**Size:** Small | **Files:** `tests/test_qa.py`
**What:** tsconfig.json detection path in _detect_infrastructure_checks untested.
**How:** Create temp tsconfig.json, verify "tsc --noEmit" detected.
**Done when:** `uv run pytest` passes.

### [x] 39. (DONE: eacb1c2) Tech Lead prompt — Add timeout guidance
**Size:** Small | **Files:** `prompts/tech_lead.md`
**What:** Tech Lead has no guidance on what to do if a writer times out. Add instructions.
**How:** Add section: if writer fails/times out, create a new ticket for the remaining work and dispatch fresh writer.
**Done when:** Prompt updated, tests pass.

### [x] 40. (DONE: memory files updated) Update memory files — Record overnight session learnings
**Size:** Small | **Files:** memory files
**What:** Update project status memory with overnight results. Record MCP naming gotcha.
**Done when:** Memory files updated.

---

## Batch 8: Overnight Queue (testing + polish)

### [x] 41. (DONE: d63adfd) Test `_ensure_merged_to_main` — Verify self-healing merge logic
**Size:** Small | **Files:** `tests/test_tech_lead.py` (new)
**What:** _ensure_merged_to_main has no tests. Cover: no branches (noop), branch already merged (skip), branch needs merge (merges).
**Done when:** `uv run pytest` passes with new tests.

### [x] 42. (DONE: 5719590) Test `GolemConfig.validate` — Edge cases
**Size:** Small | **Files:** `tests/test_config.py`
**What:** Test negative max_retries and max_worker_turns < 1.
**Done when:** `uv run pytest` passes.

### [x] 43. (DONE: 1e8edfe) `golem run` summary — Include plan file counts
**Size:** Small | **Files:** `cli.py`
**What:** After run completes, also show how many plan/research/reference files were created.
**Done when:** `uv run pytest` passes.

### [x] 44. (DONE: 633cc86) CLAUDE.md — Add new CLI commands to Quick Start
**Size:** Small | **Files:** `CLAUDE.md`
**What:** Quick Start section is missing `golem logs` and `golem inspect` commands.
**Done when:** CLAUDE.md updated.

### [x] 45. (DONE: this commit) Final overnight summary commit
**Size:** Small
**What:** Update overnight-log.md with final stats, commit count, test count. Git log summary.
**Done when:** Log updated and committed.

---

## Batch 9: Overnight Queue (hardening)

### [x] 46. (DONE: e52b0f7) Test `_cleanup_golem_worktrees` — Verify error cleanup
**Size:** Small | **Files:** `tests/test_tech_lead.py`
**What:** _cleanup_golem_worktrees has no tests. Verify it removes worktrees and handles missing dirs gracefully.
**Done when:** `uv run pytest` passes with new tests.

### [x] 47. (DONE: 3882fcf) `golem run` — Log spec path and project root at start
**Size:** Small | **Files:** `cli.py`
**What:** Print the spec file path and resolved project root at the start of a run for debugging.
**Done when:** `uv run pytest` passes.

### [x] 48. (DONE: b5a687a) Writer prompt — Add file size warning
**Size:** Small | **Files:** `prompts/worker.md`
**What:** If pre-loaded file contents are large, warn the writer to focus on the specific sections mentioned in the plan rather than reading the entire file again.
**Done when:** Prompt updated, tests pass.

### [x] 49. (DONE: 6f9cae3) `golem status` — Show "no active run" when .golem/ missing
**Size:** Small | **Files:** `cli.py`
**What:** Currently exits with error code. Should be friendlier — print a helpful message and exit 0.
**Done when:** `uv run pytest` passes.

### [x] 50. (DONE: 380dad3) Test `build_writer_prompt` — Verify all template variables replaced
**Size:** Small | **Files:** `tests/test_writer.py`
**What:** Add a test that builds a prompt with ALL context fields populated and verifies no `{placeholder}` patterns remain.
**Done when:** `uv run pytest` passes.

---

## Batch 10: Overnight Queue (edge cases + quality)

### [x] 51. (DONE: 34bd57e) Test TicketStore.update — Verify status change and history append
**Size:** Small | **Files:** `tests/test_tickets.py`
**What:** update() with case-insensitive ID lookup is untested for the new _resolve_path path.
**Done when:** `uv run pytest` passes with new test.

### [x] 52. (DONE: 6fb31ab) QA test — run_qa with empty check lists
**Size:** Small | **Files:** `tests/test_qa.py`
**What:** run_qa([],[]) should return passed=True with empty checks list.
**Done when:** `uv run pytest` passes.

### [x] 53. (DONE: 25be51c) Test merge_group_branches — Clean merge (no conflicts)
**Size:** Small | **Files:** `tests/test_worktree.py`
**What:** Test the happy path: two non-conflicting branches merge cleanly.
**Done when:** `uv run pytest` passes.

### [x] 54. (DONE: d1c3f30) `golem history` and `golem inspect` — Friendly when .golem/ missing
**Size:** Small | **Files:** `cli.py`
**What:** Same fix as task 49 — show helpful message instead of error exit.
**Done when:** `uv run pytest` passes.

### [x] 55. (DONE: d148214) CLAUDE.md — Add new test files to project structure
**Size:** Small | **Files:** `CLAUDE.md`
**What:** Project structure is missing test_cli.py, test_progress.py, test_tech_lead.py.
**Done when:** CLAUDE.md updated.

---

## Ideas & Future Work (Not Yet Scheduled)

## Batch 11: Overnight Queue (final polish)

### [x] 56. (DONE: 3d1adbe) Test `_validate_spec` — Short spec warning
**Size:** Small | **Files:** `tests/test_cli.py`
**What:** _validate_spec warns on specs < 50 chars but doesn't exit. Verify the warning path.
**Done when:** `uv run pytest` passes with new test.

### [x] 57. (DONE: 3d1adbe) Test `_validate_spec` — No structure warning
**Size:** Small | **Files:** `tests/test_cli.py`
**What:** Specs with no headings/task markers get a warning. Verify.
**Done when:** `uv run pytest` passes with new test.

### [x] 58. (DONE: cc0ff0a) Test TicketStore — Concurrent updates don't corrupt
**Size:** Small | **Files:** `tests/test_tickets.py`
**What:** 5 concurrent update() calls should all succeed and history should have all events.
**Done when:** `uv run pytest` passes.

### [x] 59. (DONE: 02fbab3) Planner prompt — Clarify sub-agent model hints
**Size:** Small | **Files:** `prompts/planner.md`
**What:** The prompt says "Haiku model" and "Sonnet model" for sub-agents but doesn't give exact model IDs. Add them.
**Done when:** Prompt updated, tests pass.

### [x] 60. (DONE: this commit) Final overnight stats update
**Size:** Small
**What:** Update overnight-log.md with final cumulative stats.
**Done when:** Log updated and committed.

---

## Batch 12: Overnight Queue (coverage gaps)

### [x] 61. (DONE: f12b88c) Test `get_version_info` — Verify all keys present
**Size:** Small | **Files:** `tests/test_version.py` (new)
**What:** get_version_info() returns version, python, platform, architecture. No tests exist.
**Done when:** `uv run pytest` passes.

### [x] 62. (DONE: f12b88c) Test `run_autofix` — Verify prettier path
**Size:** Small | **Files:** `tests/test_qa.py`
**What:** run_autofix with "prettier" in checks should call npx prettier. Currently only ruff path tested.
**Done when:** `uv run pytest` passes.

### [x] 63. (DONE: f12b88c) Test TicketStore — list_tickets combined filters
**Size:** Small | **Files:** `tests/test_tickets.py`
**What:** Test list_tickets with both status_filter AND assigned_to_filter at the same time.
**Done when:** `uv run pytest` passes.

### [x] 64. (DONE: f12b88c) Test `_resolve_spec_project_root` — Walk up to .git
**Size:** Small | **Files:** `tests/test_cli.py`
**What:** Verify it finds .git by walking up from spec file location.
**Done when:** `uv run pytest` passes.

### [x] 65. (DONE: ece8067) `golem version` — Show test count
**Size:** Small | **Files:** `cli.py`
**What:** `golem version` should show how many tests exist (count def test_ in tests/).
**Done when:** `uv run pytest` passes.

---

## Batch 13: Overnight Queue (final)

### [x] 66. (DONE: 58d5696) Test `create_writer_mcp_server` — Verify both tools present
**Size:** Small | **Files:** `tests/test_tools.py`
**What:** The existing test only checks the server is not None. Verify it has both run_qa and update_ticket tools by name.
**Done when:** `uv run pytest` passes.

### [x] 67. (DONE: 58d5696) Test `save_config` — Verify sorted keys in output
**Size:** Small | **Files:** `tests/test_config.py`
**What:** Task 24 added sort_keys=True but no test verifies key ordering.
**Done when:** `uv run pytest` passes.

### [x] 68. (DONE: 58d5696) `golem clean` — Show what was cleaned
**Size:** Small | **Files:** `cli.py`
**What:** After cleaning, show counts: N ticket files, N research files, N branches deleted.
**Done when:** `uv run pytest` passes.

### [x] 69. (DONE: this commit) Update overnight-log.md final stats
**Size:** Small
**What:** Final cumulative stats for the entire overnight session.
**Done when:** Log updated.

---

## Batch 14: Overnight Queue

### [x] 70. (DONE: ae86673) Test `handle_tool_call` — run_qa tool dispatch
**Size:** Small | **Files:** `tests/test_tools.py`
**What:** The existing test_handle_tool_call_run_qa may not verify the QA result structure fully. Verify passed/checks fields.
**Done when:** `uv run pytest` passes.

### [x] 71. (DONE: ae86673) Test `ProgressLogger` — log_writer_dispatched and log_merge_complete
**Size:** Small | **Files:** `tests/test_progress.py`
**What:** These two v2 methods have no coverage. Add tests.
**Done when:** `uv run pytest` passes.

### [x] 72. (DONE: ae86673) Planner prompt — Add "do not use Write on existing files" rule
**Size:** Small | **Files:** `prompts/planner.md`
**What:** The writer prompt has this rule but the planner writes plans to disk — it should also avoid overwriting existing reference files without reading first.
**Done when:** Prompt updated, tests pass.

### [x] 73. (DONE: ae86673) CLAUDE.md — Update test count in Testing section
**Size:** Small | **Files:** `CLAUDE.md`
**What:** Testing section mentions old test count patterns. Update to reflect 150+ tests.
**Done when:** CLAUDE.md updated.

---

## Batch 15: Overnight Queue

### [x] 74. (DONE: 1af097a) Test worktree — merge_group_branches with nonexistent branch
**Size:** Small | **Files:** `tests/test_worktree.py`
**What:** If a branch doesn't exist, merge_group_branches should skip it gracefully.
**Done when:** `uv run pytest` passes.

### [x] 75. (DONE: 1af097a) Test QA — run_qa summary string format
**Size:** Small | **Files:** `tests/test_qa.py`
**What:** Verify summary includes "passed" count and lists failed checks.
**Done when:** `uv run pytest` passes.

### [x] 76. (DONE: 1af097a) `golem run` — Catch and display RuntimeError cleanly
**Size:** Small | **Files:** `cli.py`
**What:** If planner/tech lead raises RuntimeError, catch it in the run command and print the message cleanly instead of showing a full traceback.
**Done when:** `uv run pytest` passes.

### [x] 77. (DONE: this commit) Update overnight stats + memory files final
**Size:** Small
**Done when:** All updated.

---

## Batch 16: Overnight Queue

### [x] 78. (DONE: df6d44b) Mark `tasks.py` as v1 legacy — add deprecation comment
**Size:** Small | **Files:** `src/golem/tasks.py`
**What:** tasks.py is no longer imported by any v2 module. Add a docstring marking it as v1 legacy, kept for backward compatibility with v1 test suite.
**Done when:** Comment added, tests pass.

### [x] 79. (DONE: df6d44b) `golem plan` — Also catch RuntimeError cleanly
**Size:** Small | **Files:** `cli.py`
**What:** Same as task 76 but for the plan command.
**Done when:** `uv run pytest` passes.

### [x] 80. (DONE: df6d44b) Test `_ticket_to_dict` and `_ticket_from_dict` roundtrip with all fields
**Size:** Small | **Files:** `tests/test_tickets.py`
**What:** Verify serialization handles all TicketContext fields including files dict.
**Done when:** `uv run pytest` passes.

---

### Agent Observability / Live Streaming (EXPANDED)
Beyond basic stderr streaming (task 1), we eventually want:
- A TUI dashboard showing all active agents and their current activity
- The web UI (`golem ui`) to show real-time agent activity via SSE
- Structured event format that both TUI and web UI can consume

### Parallel Writer Verification
After Tech Lead dispatches multiple writers in parallel, verify all worktrees have changes before merging. Currently no validation that writers actually wrote code.
