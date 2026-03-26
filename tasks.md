# Golem — Task Board

## Completed Work (v0.2.0)

112 tasks shipped across 27 batches during the overnight autonomous development session.
See `git log --oneline b546782..HEAD` for the full commit history.

---

## Wave 2: The Next 150 (Tasks 113–262)

Tasks are organized by theme. Each task should be:
- Implemented
- Tested (`uv run pytest`)
- Committed with a descriptive message
- Checked off below

---

### Theme A: Test Coverage — Untested Code Paths (113–137)

#### [x] 113. Test `create_pr()` — Verify PR creation with mocked `gh` (DONE: eef538a)
**Size:** Small | **Files:** `tests/test_worktree.py`
**What:** `worktree.py:create_pr()` is completely untested. Mock `subprocess.run` for `gh pr create` and verify: success returns PR URL, failure raises RuntimeError, draft flag passes `--draft`.
**Done when:** `uv run pytest` passes with 3 new tests.

#### [x] 114. Test `golem status` with real tickets — Verify table rendering (DONE: 17592ff)
**Size:** Small | **Files:** `tests/test_cli.py`
**What:** Create tickets in a temp `.golem/tickets/` dir, invoke the status command, verify the table contains ticket IDs, titles, and status values in the output.
**Done when:** `uv run pytest` passes.

#### [x] 115. Test `golem inspect` with real ticket — Verify all sections printed (DONE: a864323)
**Size:** Small | **Files:** `tests/test_cli.py`
**What:** Create a ticket with full context (blueprint, acceptance, qa_checks, files, references), invoke inspect, verify all fields appear in output.
**Done when:** `uv run pytest` passes.

#### [x] 116. Test `golem history` with ticket events — Verify timeline rendering (DONE: 79e2ed8)
**Size:** Small | **Files:** `tests/test_cli.py`
**What:** Create tickets with multiple status updates, invoke history, verify events appear chronologically in output.
**Done when:** `uv run pytest` passes.

#### [x] 117. Test `golem clean` with real `.golem/` state — Verify cleanup (DONE: f983c95)
**Size:** Small | **Files:** `tests/test_cli.py`
**What:** Create `.golem/` with tickets and branches, invoke `clean --force`, verify directory removed and branches deleted.
**Done when:** `uv run pytest` passes.

#### [x] 118. Test `golem logs` with existing log file — Verify output printed (DONE: 1c62171)
**Size:** Small | **Files:** `tests/test_cli.py`
**What:** Create a `progress.log` with entries, invoke `golem logs` (non-follow mode), verify log lines appear in output.
**Done when:** `uv run pytest` passes.

#### [x] 119. Test `golem run` stale state detection — Verify warning and `--force` override (DONE: 3971398)
**Size:** Small | **Files:** `tests/test_cli.py`
**What:** Create `.golem/tickets/` with files, invoke `golem run spec.md` without `--force`, verify it exits with warning. Then with `--force`, verify it proceeds (and fails on missing planner, not on stale state).
**Done when:** `uv run pytest` passes.

#### [x] 120. Test `handle_tool_call` — `update_ticket` dispatch (DONE: bdd0e6c)
**Size:** Small | **Files:** `tests/test_tools.py`
**What:** Call `handle_tool_call` with `update_ticket` tool, verify status change persists.
**Done when:** `uv run pytest` passes.

#### [x] 121. Test `handle_tool_call` — `read_ticket` dispatch (DONE: bdd0e6c)
**Size:** Small | **Files:** `tests/test_tools.py`
**What:** Create a ticket, call `handle_tool_call` with `read_ticket`, verify returned JSON has all fields.
**Done when:** `uv run pytest` passes.

#### [x] 122. Test `handle_tool_call` — `list_tickets` dispatch (DONE: bdd0e6c)
**Size:** Small | **Files:** `tests/test_tools.py`
**What:** Create multiple tickets, call `handle_tool_call` with `list_tickets` and status filter, verify correct count.
**Done when:** `uv run pytest` passes.

#### [x] 123. Test `handle_tool_call` — `create_worktree` dispatch (DONE: bdd0e6c)
**Size:** Small | **Files:** `tests/test_tools.py`
**What:** Mock `worktree.create_worktree`, call `handle_tool_call` with `create_worktree`, verify args passed correctly.
**Done when:** `uv run pytest` passes.

#### [x] 124. Test `handle_tool_call` — `merge_branches` dispatch (DONE: bdd0e6c)
**Size:** Small | **Files:** `tests/test_tools.py`
**What:** Mock `worktree.merge_group_branches`, call `handle_tool_call` with `merge_branches`, verify args and return value.
**Done when:** `uv run pytest` passes.

#### [x] 125. Test `handle_tool_call` — `commit_worktree` dispatch (DONE: bdd0e6c)
**Size:** Small | **Files:** `tests/test_tools.py`
**What:** Mock `worktree.commit_task`, call `handle_tool_call` with `commit_worktree`, verify args.
**Done when:** `uv run pytest` passes.

#### [x] 126. Test `create_worktree()` — Branch already exists path (DONE: e652757)
**Size:** Small | **Files:** `tests/test_worktree.py`
**What:** Create a branch first, then call `create_worktree` with same branch name. Verify it uses `git worktree add <path> <branch>` (no `-b`).
**Done when:** `uv run pytest` passes.

#### [x] 127. Test `_create_golem_dirs()` — All 6 subdirectories created (DONE: 6891b69)
**Size:** Small | **Files:** `tests/test_cli.py`
**What:** Call `_create_golem_dirs` with a temp path, verify all directories exist: tickets, research, plans, references, reports, worktrees.
**Done when:** `uv run pytest` passes.

#### [x] 128. Test `_get_golem_dir()` and `_get_project_root()` — Return correct paths (DONE: 6891b69)
**Size:** Small | **Files:** `tests/test_cli.py`
**What:** Verify `_get_golem_dir()` returns `<cwd>/.golem` and `_get_project_root()` returns `<cwd>`.
**Done when:** `uv run pytest` passes.

#### [x] 129. Test `golem version` — Test count logic accuracy (DONE: 7ec7e9f)
**Size:** Small | **Files:** `tests/test_cli.py`
**What:** Invoke `golem version`, verify the test count matches actual `uv run pytest --co -q` count.
**Done when:** `uv run pytest` passes.

#### [S] 130. Test `detect_infrastructure_checks` — tsconfig.json detection (SKIP: already covered by test_detect_infrastructure_checks_finds_tsc)
**Size:** Small | **Files:** `tests/test_qa.py`
**What:** `qa.detect_infrastructure_checks()` should detect `tsconfig.json` and add `"npx tsc --noEmit"`. Currently untested.
**Done when:** `uv run pytest` passes.

#### [x] 131. Test `run_autofix` — Both ruff AND prettier present simultaneously (DONE: 5c7a8b2)
**Size:** Small | **Files:** `tests/test_qa.py`
**What:** Pass checks containing both `ruff check .` and `prettier --check .`, verify both autofix commands invoked.
**Done when:** `uv run pytest` passes.

#### [x] 132. Test `QACheck.type` classification — lint vs test vs acceptance (DONE: 5c7a8b2)
**Size:** Small | **Files:** `tests/test_qa.py`
**What:** Run checks with ruff (lint), pytest (test), and custom command (acceptance), verify `check.type` is correct.
**Done when:** `uv run pytest` passes.

#### [x] 133. Test `_handle_create_ticket` — `files` dict field preserved (DONE: 5c7a8b2)
**Size:** Small | **Files:** `tests/test_tools.py`
**What:** Call `handle_tool_call` with `create_ticket` including a `files` dict. Verify `str(k): str(v)` conversion works and persists.
**Done when:** `uv run pytest` passes.

#### [x] 134. Test `TicketStore.read` — Corrupt JSON raises clean error (DONE: 5c7a8b2)
**Size:** Small | **Files:** `tests/test_tickets.py`
**What:** Write a corrupt JSON file to the tickets dir, call `store.read()`, verify it raises a descriptive error (not raw `json.JSONDecodeError`).
**Done when:** `uv run pytest` passes.

#### [x] 135. Test `merge_group_branches` — Empty branch list (DONE: 5c7a8b2)
**Size:** Small | **Files:** `tests/test_worktree.py`
**What:** Call `merge_group_branches([], "integration", repo)`, verify `(True, "")` returned without error.
**Done when:** `uv run pytest` passes.

#### [x] 136. Test `spawn_writer_pair` — `golem_dir=None` fallback (DONE: 97fc8ba)
**Size:** Small | **Files:** `tests/test_writer.py`
**What:** Call `spawn_writer_pair` without `golem_dir`, verify it uses `create_writer_mcp_server(Path(worktree_path))`.
**Done when:** `uv run pytest` passes.

#### [x] 137. Test `write_tasks_sync` — Legacy v1 sync writer (DONE: a34bd03)
**Size:** Small | **Files:** `tests/test_tasks.py`
**What:** `write_tasks_sync()` is never tested. Call it with a TasksFile, verify it writes to disk.
**Done when:** `uv run pytest` passes.

---

### Theme B: Error Handling & Robustness (138–152)

#### [x] 138. Worktree `_run()` — Add timeout to all subprocess calls (DONE: a34bd03)
**Size:** Small | **Files:** `src/golem/worktree.py`
**What:** All `subprocess.run` calls in worktree.py have no timeout. A hung git operation blocks indefinitely. Add `timeout=60` to all `_run()` calls.
**Done when:** `uv run pytest` passes.

#### [x] 139. `create_worktree()` — Clean up parent dir on failure (DONE: ccc46b8)
**Size:** Small | **Files:** `src/golem/worktree.py`
**What:** If `git worktree add` fails after `path.parent.mkdir()`, the empty directory is left behind. Add cleanup in except block.
**Done when:** `uv run pytest` passes with test for failure case.

#### [S] 140. `TicketStore.read()` — Handle corrupt JSON gracefully (SKIP: JSONDecodeError now caught at CLI layer in task 144; wrapping would break test 134)
**Size:** Small | **Files:** `src/golem/tickets.py`
**What:** If a ticket JSON is corrupt, `json.loads` raises `JSONDecodeError` that propagates unhandled. Catch it and raise `ValueError(f"Ticket {ticket_id} file is corrupt")`.
**Done when:** `uv run pytest` passes (pairs with test 134).

#### [x] 141. `_ensure_merged_to_main()` — Catch CalledProcessError from `git checkout main` (DONE: 564b453)
**Size:** Small | **Files:** `src/golem/tech_lead.py`
**What:** If the repo uses `master` instead of `main`, `git checkout main` fails. Catch the error and try `master` as fallback, or detect default branch from `git symbolic-ref`.
**Done when:** `uv run pytest` passes.

#### [x] 142. `ui.py:api_run()` — Handle missing `uv` on PATH (DONE: 12b4df9)
**Size:** Small | **Files:** `src/golem/ui.py`
**What:** If `uv` is not on PATH, `asyncio.create_subprocess_exec` raises `FileNotFoundError`. Catch and return 500 with user-friendly message.
**Done when:** `uv run pytest` passes.

#### [x] 143. `planner.py` — Handle unreadable spec file gracefully (DONE: 12b4df9)
**Size:** Small | **Files:** `src/golem/planner.py`
**What:** `spec_path.read_text()` raises `PermissionError` if unreadable. Catch before retry logic and raise descriptive error.
**Done when:** `uv run pytest` passes.

#### [x] 144. `cli.py:inspect()` — Catch `JSONDecodeError` alongside `FileNotFoundError` (DONE: 6b549c7)
**Size:** Small | **Files:** `src/golem/cli.py`
**What:** Currently only catches `(FileNotFoundError, KeyError)`. A corrupt ticket JSON gives an ugly traceback instead of "ticket corrupt" message.
**Done when:** `uv run pytest` passes.

#### [x] 145. `cli.py:clean()` — Handle subprocess errors from `git worktree remove` (DONE: 6b549c7)
**Size:** Small | **Files:** `src/golem/cli.py`
**What:** `subprocess.run` for worktree remove has no `check` param. If it fails, `wt_count` is still incremented. Check returncode before counting.
**Done when:** `uv run pytest` passes.

#### [x] 146. `golem status` — Count `qa_passed` and `ready_for_review` as "done-ish" (DONE: 6b549c7)
**Size:** Small | **Files:** `src/golem/cli.py`
**What:** Currently only `"done"` and `"approved"` count toward completion. Add `"qa_passed"` and `"ready_for_review"` to done_count.
**Done when:** `uv run pytest` passes.

#### [x] 147. `golem status` — Fix double `console.print(table)` bug (DONE: 6b549c7)
**Size:** Small | **Files:** `src/golem/cli.py`
**What:** Lines 296 and 299 both print the table. Remove the duplicate.
**Done when:** `uv run pytest` passes and table prints once.

#### [ ] 148. `TicketStore` — File-level locking for cross-process safety
**Size:** Medium | **Files:** `src/golem/tickets.py`
**What:** `asyncio.Lock` only protects within one process. If two Golem processes write simultaneously, JSON can corrupt. Use `filelock` (add to deps) or OS-level lock file.
**Done when:** `uv run pytest` passes.

#### [x] 149. `worktree.py` tool handlers — Return structured JSON errors on failure (DONE: 6f21132)
**Size:** Small | **Files:** `src/golem/tools.py`
**What:** If `create_worktree` or `commit_task` raises `CalledProcessError`, the MCP handler should return `{"error": "..."}` instead of letting the exception propagate.
**Done when:** `uv run pytest` passes.

#### [x] 150. `golem inspect` — Validate ticket ID format before lookup (DONE: 12b4df9)
**Size:** Small | **Files:** `src/golem/cli.py`
**What:** If user passes `golem inspect foo`, print "Invalid ticket ID format. Expected TICKET-NNN" instead of a generic "not found".
**Done when:** `uv run pytest` passes.

#### [ ] 151. `ui.py` — Per-client SSE event queue fanout
**Size:** Medium | **Files:** `src/golem/ui.py`
**What:** Global `event_queue` is shared across all SSE clients. One client draining events means others miss them. Switch to per-client `asyncio.Queue` with a broadcast pattern.
**Done when:** `uv run pytest` passes with test for two concurrent SSE clients.

#### [ ] 152. `golem run` — Timeout for overall run
**Size:** Small | **Files:** `src/golem/cli.py`
**What:** Add `--timeout` flag (default: no limit) that kills the run after N minutes. Useful for CI and overnight loops.
**Done when:** `uv run pytest` passes.

---

### Theme C: Dead Code Cleanup (153–160)

#### [x] 153. Remove `_strip_section()` from `writer.py` — Dead code (DONE: 6fc93cf)
**Size:** Small | **Files:** `src/golem/writer.py`
**What:** Defined but never called. The actual replacement logic uses a dict loop. Remove the dead function.
**Done when:** `uv run pytest` passes.

#### [x] 154. Remove `integration_reviewer.md` — Orphaned prompt (DONE: 6fc93cf)
**Size:** Small | **Files:** `src/golem/prompts/integration_reviewer.md`
**What:** Never loaded by any Python code. Tech lead does integration review inline. Delete the file.
**Done when:** File removed, `uv run pytest` passes.

#### [x] 155. Remove `_TaskState` from `tui.py` — Unused dataclass (DONE: 6fc93cf)
**Size:** Small | **Files:** `src/golem/tui.py`
**What:** Defined but never instantiated. Dead code from v1 TUI.
**Done when:** `uv run pytest` passes.

#### [x] 156. Remove `executor.py` pyc cache — Stale compiled file (DONE: 6fc93cf)
**Size:** Small | **Files:** `__pycache__/executor.cpython-314.pyc`
**What:** References a deleted source file. Clean up the stale cache.
**Done when:** File removed.

#### [x] 157. Audit `GolemConfig.auto_pr` — Either implement or remove (DONE: 6fc93cf — removed)
**Size:** Small | **Files:** `src/golem/config.py`, `src/golem/prompts/tech_lead.md`
**What:** `auto_pr: bool = True` exists in config but is never checked. Either: (a) add a check in tech_lead prompt/cli, or (b) remove the field.
**Done when:** Field is either functional or removed. Tests pass.

#### [x] 158. Audit `GolemConfig.max_validator_turns` — Either use or remove (DONE: 6fc93cf — removed)
**Size:** Small | **Files:** `src/golem/config.py`
**What:** Defined but never used in any SDK session. No separate validator agent in v2. Remove or repurpose.
**Done when:** Field removed or used. Tests pass.

#### [S] 159. Audit `GolemConfig.pr_target` — Wire it to tech lead or remove (SKIP: requires prompt template changes and SDK testing)
**Size:** Small | **Files:** `src/golem/config.py`, `src/golem/prompts/tech_lead.md`
**What:** `pr_target` exists in config but is never passed to the tech lead. The prompt hardcodes `main`. Either inject `{pr_target}` into the prompt or remove the field.
**Done when:** Field is functional or removed. Tests pass.

#### [x] 160. `tui.py:PreRunScreen` — Version string says v0.1.0, should be dynamic (DONE: 6fc93cf)
**Size:** Small | **Files:** `src/golem/tui.py`
**What:** Hardcoded `"Golem v0.1.0"` in the TUI. If keeping the TUI, import `__version__`. If removing it (it's unused in v2), delete the whole class.
**Done when:** Fixed or removed. Tests pass.

---

### Theme D: Configuration & Validation (161–170)

#### [x] 161. Validate `setting_sources` values — Warn on unknown sources (DONE: 0bee1b1)
**Size:** Small | **Files:** `src/golem/config.py`
**What:** `setting_sources` accepts any list. An empty list or unknown values like `["typo"]` are silently passed to the SDK. Validate against `["project", "user"]`.
**Done when:** `uv run pytest` passes with test.

#### [S] 162. Validate `max_validator_turns` — Same as `max_worker_turns` (SKIP: field removed in task 158)
**Size:** Small | **Files:** `src/golem/config.py`
**What:** `validate()` checks `max_worker_turns >= 1` but not `max_validator_turns`. Add the check.
**Done when:** `uv run pytest` passes.

#### [ ] 163. Wire `GolemConfig.setting_sources` to SDK sessions
**Size:** Medium | **Files:** `src/golem/planner.py`, `src/golem/tech_lead.py`, `src/golem/writer.py`
**What:** `config.setting_sources` is loaded but never passed to `ClaudeAgentOptions`. Add `setting_sources=config.setting_sources` to all three agent spawners.
**Done when:** `uv run pytest` passes.

#### [x] 164. Add `sdk_timeout` config option — Replace hardcoded 180s (DONE: df39c92)
**Size:** Small | **Files:** `src/golem/config.py`, `src/golem/planner.py`
**What:** The SDK timeout monkey-patch hardcodes `180`. Add `sdk_timeout: int = 180` to config so users can tune it.
**Done when:** `uv run pytest` passes.

#### [x] 165. Add `retry_delay` config option — Replace hardcoded 10s (DONE: df39c92)
**Size:** Small | **Files:** `src/golem/config.py`, `src/golem/planner.py`, `src/golem/tech_lead.py`, `src/golem/writer.py`
**What:** `_RETRY_DELAY_S = 10` is a module constant in all three. Pull from config.
**Done when:** `uv run pytest` passes.

#### [x] 166. Add `max_tech_lead_turns` config option (DONE: df39c92)
**Size:** Small | **Files:** `src/golem/config.py`, `src/golem/tech_lead.py`
**What:** `run_tech_lead()` hardcodes `max_turns=100`. Add config field and wire it.
**Done when:** `uv run pytest` passes.

#### [x] 167. Config — Show effective config at `golem run` start (DONE: c830546)
**Size:** Small | **Files:** `src/golem/cli.py`
**What:** Print key config values (models, max_parallel, max_worker_turns) at the start of a run for debugging.
**Done when:** `uv run pytest` passes.

#### [x] 168. Config — Add `golem config show` subcommand (DONE: e165467)
**Size:** Small | **Files:** `src/golem/cli.py`
**What:** New CLI command that prints the current config as pretty JSON (from `.golem/config.json` or defaults).
**Done when:** `uv run pytest` passes.

#### [x] 169. Config — Add `golem config set <key> <value>` subcommand (DONE: c7f21e5)
**Size:** Medium | **Files:** `src/golem/cli.py`
**What:** Allow setting config values from CLI without editing JSON. Validate the key and value type.
**Done when:** `uv run pytest` passes.

#### [x] 170. Config — Add `golem config reset` subcommand (DONE: e165467)
**Size:** Small | **Files:** `src/golem/cli.py`
**What:** Reset config to defaults (delete `.golem/config.json`).
**Done when:** `uv run pytest` passes.

---

### Theme E: Prompt Quality & Agent Behavior (171–185)

#### [x] 171. Planner prompt — Add task graph format example (DONE: b40f53e)
**Size:** Small | **Files:** `src/golem/prompts/planner.md`
**What:** Step 6 "Task Graph: table" gives no format example. Add a concrete markdown table example showing ID, title, deps, group columns.
**Done when:** Prompt updated, tests pass.

#### [x] 172. Planner prompt — Handle "(none detected)" infrastructure checks (DONE: b40f53e)
**Size:** Small | **Files:** `src/golem/prompts/planner.md`
**What:** When no infra checks detected, the placeholder renders as "(none detected)". Add instruction: "If no infrastructure checks listed, use an empty qa_checks list."
**Done when:** Prompt updated, tests pass.

#### [x] 173. Planner prompt — Guidance for specs with no enumerable tasks (DONE: b40f53e)
**Size:** Small | **Files:** `src/golem/prompts/planner.md`
**What:** If spec is pure prose with no clear task breakdown, the planner has no guidance. Add: "If the spec cannot be broken into discrete tasks, create a single task covering the entire scope."
**Done when:** Prompt updated, tests pass.

#### [x] 174. Planner prompt — Handle `create_ticket` tool failure (DONE: b40f53e)
**Size:** Small | **Files:** `src/golem/prompts/planner.md`
**What:** No instruction for what to do if the MCP tool call fails. Add: "If `mcp__golem__create_ticket` returns an error, retry once. If still failing, log the error and proceed."
**Done when:** Prompt updated, tests pass.

#### [x] 175. Tech Lead prompt — Define writer timeout threshold (DONE: b40f53e)
**Size:** Small | **Files:** `src/golem/prompts/tech_lead.md`
**What:** "if a writer fails or times out" but no timeout threshold defined. Add: "If a writer hasn't updated its ticket in 15 minutes, consider it timed out."
**Done when:** Prompt updated, tests pass.

#### [x] 176. Tech Lead prompt — Clarify UX smoke test criteria (DONE: b40f53e)
**Size:** Small | **Files:** `src/golem/prompts/tech_lead.md`
**What:** Phase 7 "if the project is a web project" is vague. Add: "A project is a web project if it has an `index.html`, a `dev` script in `package.json`, or a frontend framework (React/Vue/Svelte) in dependencies."
**Done when:** Prompt updated, tests pass.

#### [x] 177. Tech Lead prompt — Verify merge with more than 3 commits (DONE: 43261fe)
**Size:** Small | **Files:** `src/golem/prompts/tech_lead.md`
**What:** Phase 8 says "`git log --oneline -3`" which is insufficient for large merges. Change to "`git log --oneline -10`" or `git log integration..main`.
**Done when:** Prompt updated, tests pass.

#### [x] 178. Tech Lead prompt — Explain tool name distinction (DONE: 43261fe)
**Size:** Small | **Files:** `src/golem/prompts/tech_lead.md`
**What:** The tool list mentions `mcp__golem__run_qa` (tech lead's tool) vs `mcp__golem-writer__run_qa` (writer's tool). Add a note clarifying the tech lead should use `mcp__golem__*` tools only.
**Done when:** Prompt updated, tests pass.

#### [x] 179. Worker prompt — Add `update_ticket` on QA failure (DONE: 43261fe)
**Size:** Small | **Files:** `src/golem/prompts/worker.md`
**What:** After 3 QA failures, the worker reports failure but doesn't update the ticket. Add: "Call `mcp__golem-writer__update_ticket` with status `needs_work` and the failure details."
**Done when:** Prompt updated, tests pass.

#### [x] 180. Worker prompt — Reiterate "no git commit" rule in implementation steps (DONE: 43261fe)
**Size:** Small | **Files:** `src/golem/prompts/worker.md`
**What:** The "no git commit" rule is only at the bottom. Add a reminder in Step 2: "Do NOT run `git commit` or `git push` — the Tech Lead handles all git operations."
**Done when:** Prompt updated, tests pass.

#### [x] 181. Worker prompt — Explain "wait for review" mechanism (DONE: 43261fe)
**Size:** Small | **Files:** `src/golem/prompts/worker.md`
**What:** Step 7 says "Stay alive. Do not exit." but doesn't explain how. Add: "After updating ticket to `ready_for_review`, call `mcp__golem-writer__read_ticket` in a polling loop (every 30s) to check for status changes."
**Done when:** Prompt updated, tests pass.

#### [S] 182. Planner prompt — Add encoding reminder for file writes (SKIP: already exists at line 169 of planner.md)
**Size:** Small | **Files:** `src/golem/prompts/planner.md`
**What:** The Rules section should remind the planner to use `encoding="utf-8"` on all file writes, same as the worker prompt.
**Done when:** Prompt updated, tests pass.

#### [x] 183. All prompts — Standardize MCP tool prefix documentation (DONE: 93a87f1)
**Size:** Small | **Files:** `src/golem/prompts/*.md`
**What:** Create a consistent "Available Tools" section in all three prompts that lists every MCP tool with its full `mcp__<server>__<name>` format.
**Done when:** All prompts updated, tests pass.

#### [ ] 184. Tech Lead prompt — Add explicit "create PR" vs "skip PR" based on config
**Size:** Small | **Files:** `src/golem/prompts/tech_lead.md`
**What:** If `auto_pr` is wired (task 157), the prompt should conditionally include or skip the PR creation step.
**Done when:** Prompt updated, tests pass. Depends on task 157.

#### [ ] 185. Tech Lead prompt — Add `pr_target` template variable
**Size:** Small | **Files:** `src/golem/prompts/tech_lead.md`, `src/golem/tech_lead.py`
**What:** Replace hardcoded "Base branch: `main`" with `{pr_target}` injected from config. Depends on task 159.
**Done when:** Prompt updated, tests pass.

---

### Theme F: UI Dashboard Improvements (186–200)

#### [x] 186. Test `/api/specs` endpoint — Verify spec discovery (DONE: 00e1ae7)
**Size:** Small | **Files:** `tests/test_ui.py`
**What:** Create temp project with `.md` files, call `/api/specs`, verify specs returned with correct paths.
**Done when:** `uv run pytest` passes.

#### [x] 187. Test `tail_progress_log()` — Verify file seek and line buffering (DONE: 6c12918)
**Size:** Small | **Files:** `tests/test_ui.py`
**What:** Create a log file, start `tail_progress_log` in a task, write new lines, verify they appear in `log_buffer`.
**Done when:** `uv run pytest` passes.

#### [ ] 188. Test `stream_subprocess_output()` — Verify stdout capture
**Size:** Small | **Files:** `tests/test_ui.py`
**What:** Run a simple subprocess via `stream_subprocess_output`, verify stdout lines captured.
**Done when:** `uv run pytest` passes.

#### [x] 189. UI — Add `/api/config` endpoint (DONE: b90c06f)
**Size:** Small | **Files:** `src/golem/ui.py`
**What:** GET returns current config JSON. POST updates config values. Enables UI-based config editing.
**Done when:** `uv run pytest` passes.

#### [x] 190. UI — Add `/api/clean` endpoint (DONE: b90c06f)
**Size:** Small | **Files:** `src/golem/ui.py`
**What:** POST triggers `golem clean --force`. Returns cleanup summary.
**Done when:** `uv run pytest` passes.

#### [ ] 191. UI — Add run history to dashboard
**Size:** Medium | **Files:** `src/golem/ui.py`, `src/golem/ui_template.html`
**What:** Store run results (start time, duration, ticket count, pass/fail) in a JSON file. Show past runs in a table on the dashboard.
**Done when:** `uv run pytest` passes.

#### [ ] 192. UI — Ticket detail panel
**Size:** Medium | **Files:** `src/golem/ui_template.html`
**What:** Clicking a ticket in the dashboard opens a detail panel showing full context, history, and files.
**Done when:** Visual inspection confirms panel works.

#### [ ] 193. UI — Real-time agent activity indicators
**Size:** Medium | **Files:** `src/golem/ui.py`, `src/golem/ui_template.html`
**What:** Show which agents are currently active (planner, tech lead, writers) with spinner animations. Driven by SSE events.
**Done when:** Visual inspection confirms indicators work.

#### [ ] 194. UI — Error toast notifications
**Size:** Small | **Files:** `src/golem/ui_template.html`
**What:** When an SSE error event arrives, show a toast notification in the UI instead of silently logging.
**Done when:** Visual inspection confirms toasts work.

#### [ ] 195. UI — Keyboard shortcuts
**Size:** Small | **Files:** `src/golem/ui_template.html`
**What:** Add keyboard shortcuts: `r` = run, `c` = clean, `s` = stop, `Esc` = close panels.
**Done when:** Visual inspection confirms shortcuts work.

#### [ ] 196. UI — Dark/light theme toggle
**Size:** Small | **Files:** `src/golem/ui_template.html`
**What:** Add a theme toggle button. Save preference in localStorage.
**Done when:** Visual inspection confirms both themes work.

#### [ ] 197. UI — Progress bar for overall run
**Size:** Small | **Files:** `src/golem/ui_template.html`
**What:** Show a progress bar based on ticket completion (done/total). Updates via SSE.
**Done when:** Visual inspection confirms progress bar works.

#### [ ] 198. UI — Log viewer with search and filter
**Size:** Medium | **Files:** `src/golem/ui_template.html`
**What:** The log panel should support searching (Ctrl+F) and filtering by event type (planner, tech_lead, writer, qa).
**Done when:** Visual inspection confirms search and filter work.

#### [ ] 199. UI — Mobile responsive layout
**Size:** Small | **Files:** `src/golem/ui_template.html`
**What:** Dashboard should be usable on mobile screens. Add responsive CSS breakpoints.
**Done when:** Visual inspection on mobile viewport confirms usability.

#### [x] 200. UI — Favicon and page title (DONE: f37720c)
**Size:** Small | **Files:** `src/golem/ui_template.html`
**What:** Add a simple favicon (inline SVG) and dynamic page title showing run status (e.g. "Golem - Running" / "Golem - Idle").
**Done when:** Visual inspection confirms favicon and title.

---

### Theme G: CLI Enhancements (201–220)

#### [x] 201. `golem diff` — Show git diff of changes from last run (DONE: e5c5a6d)
**Size:** Small | **Files:** `src/golem/cli.py`
**What:** New command that runs `git diff main` (or configured base branch) to show what the last golem run changed.
**Done when:** `uv run pytest` passes.

#### [ ] 202. `golem export` — Export run artifacts as a zip
**Size:** Medium | **Files:** `src/golem/cli.py`
**What:** Bundle `.golem/` contents (tickets, plans, research, progress.log) into a zip for sharing/archival.
**Done when:** `uv run pytest` passes.

#### [ ] 203. `golem stats` — Show cumulative run statistics
**Size:** Small | **Files:** `src/golem/cli.py`
**What:** New command showing: total runs, total tickets, average duration, pass rate. Stored in `.golem/stats.json`.
**Done when:** `uv run pytest` passes.

#### [ ] 204. `golem retry <ticket-id>` — Re-run a specific failed ticket
**Size:** Medium | **Files:** `src/golem/cli.py`
**What:** Reset ticket to `pending`, re-dispatch to a fresh writer with original context.
**Done when:** `uv run pytest` passes.

#### [ ] 205. `golem doctor` — Diagnose environment issues
**Size:** Medium | **Files:** `src/golem/cli.py`
**What:** New command that checks: claude CLI installed, authenticated, uv installed, git configured, rg installed. Print pass/fail for each.
**Done when:** `uv run pytest` passes.

#### [x] 206. `golem run --dry-run` — Full pipeline dry run (DONE: d7620ad)
**Size:** Small | **Files:** `src/golem/cli.py`
**What:** Add `--dry-run` flag that runs the planner and shows the plan but doesn't dispatch the tech lead. Same as `golem plan` but from the `run` command.
**Done when:** `uv run pytest` passes.

#### [ ] 207. `golem run --verbose` — Enable debug logging
**Size:** Small | **Files:** `src/golem/cli.py`
**What:** Add `--verbose` flag that sets `GOLEM_DEBUG=1` and enables more detailed stderr output.
**Done when:** `uv run pytest` passes.

#### [ ] 208. `golem run` — Accept spec from stdin
**Size:** Small | **Files:** `src/golem/cli.py`
**What:** Allow `cat spec.md | golem run -` to read spec from stdin. Write to a temp file before proceeding.
**Done when:** `uv run pytest` passes.

#### [S] 209. CLI — Colored output for ticket statuses (SKIP: already implemented in status command with status_styles dict)
**Size:** Small | **Files:** `src/golem/cli.py`
**What:** Use rich markup for ticket statuses: green=done/approved, yellow=in_progress, red=needs_work, blue=pending.
**Done when:** Visual inspection confirms colors.

#### [ ] 210. CLI — `golem run` shows spinner while planner works
**Size:** Small | **Files:** `src/golem/cli.py`
**What:** Use rich Progress/Spinner to show activity during the planner phase instead of just streaming to stderr.
**Done when:** Visual inspection confirms spinner.

#### [ ] 211. CLI — `golem tickets` as alias for `golem status`
**Size:** Small | **Files:** `src/golem/cli.py`
**What:** Add `tickets` as a command alias for `status` since that's more intuitive.
**Done when:** `uv run pytest` passes.

#### [ ] 212. CLI — Tab completion for ticket IDs
**Size:** Medium | **Files:** `src/golem/cli.py`
**What:** `golem inspect` and `golem retry` should tab-complete ticket IDs from `.golem/tickets/`.
**Done when:** Tab completion works in bash/zsh.

#### [ ] 213. CLI — `golem pr` — Create PR from last run's changes
**Size:** Small | **Files:** `src/golem/cli.py`
**What:** Wrapper around `gh pr create` with auto-generated title/body from the spec and ticket summaries.
**Done when:** `uv run pytest` passes.

#### [ ] 214. CLI — `golem report` — Generate HTML report of run
**Size:** Medium | **Files:** `src/golem/cli.py`
**What:** Generate a self-contained HTML report with: spec summary, ticket statuses, QA results, timeline, diff stats. Save to `.golem/reports/`.
**Done when:** `uv run pytest` passes.

#### [ ] 215. CLI — Interrupt handling with cleanup
**Size:** Small | **Files:** `src/golem/cli.py`
**What:** Handle Ctrl+C gracefully: print "Interrupted. Cleaning up...", kill subprocess agents, save progress state.
**Done when:** `uv run pytest` passes.

#### [ ] 216. CLI — `golem watch <spec>` — Auto-re-run on spec changes
**Size:** Medium | **Files:** `src/golem/cli.py`
**What:** Watch the spec file for changes and auto-trigger a new run when it's modified. Uses `watchdog` or polling.
**Done when:** `uv run pytest` passes.

#### [ ] 217. CLI — `golem list-specs` — Show available specs in project
**Size:** Small | **Files:** `src/golem/cli.py`
**What:** List all `.md` files that look like specs (have headings, tasks, etc.) in the project.
**Done when:** `uv run pytest` passes.

#### [ ] 218. CLI — Show progress percentage during run
**Size:** Small | **Files:** `src/golem/cli.py`
**What:** Print `[3/5 tickets complete]` style progress updates as tickets finish.
**Done when:** `uv run pytest` passes.

#### [ ] 219. CLI — `golem reset <ticket-id>` — Reset ticket to pending
**Size:** Small | **Files:** `src/golem/cli.py`
**What:** Reset a single ticket's status back to `pending` without cleaning the whole run.
**Done when:** `uv run pytest` passes.

#### [ ] 220. CLI — Help text improvements for all commands
**Size:** Small | **Files:** `src/golem/cli.py`
**What:** Audit all command help strings. Add examples, clarify options, improve descriptions.
**Done when:** `golem --help` and all subcommand `--help` are clear and complete.

---

### Theme H: Pipeline Improvements (221–240)

#### [ ] 221. Planner — Sub-agent research caching
**Size:** Medium | **Files:** `src/golem/planner.py`
**What:** If `.golem/research/` already has files from a previous partial run, reuse them instead of re-running sub-agents.
**Done when:** `uv run pytest` passes.

#### [ ] 222. Planner — Emit structured events for sub-agent dispatch
**Size:** Small | **Files:** `src/golem/planner.py`, `src/golem/progress.py`
**What:** Add `log_explorer_dispatched`, `log_researcher_dispatched` progress events so the UI can show sub-agent activity.
**Done when:** `uv run pytest` passes.

#### [ ] 223. Tech Lead — Parallel writer dispatch tracking
**Size:** Medium | **Files:** `src/golem/tech_lead.py`, `src/golem/progress.py`
**What:** Log which writers are dispatched in parallel, track which finish first, which fail.
**Done when:** `uv run pytest` passes.

#### [ ] 224. Tech Lead — Verify worktree has changes before merge
**Size:** Small | **Files:** `src/golem/tech_lead.py`
**What:** Before merging a writer's worktree, check `git diff --stat` is non-empty. Skip empty worktrees.
**Done when:** `uv run pytest` passes.

#### [ ] 225. Writer — Pre-check that required files exist before implementation
**Size:** Small | **Files:** `src/golem/prompts/worker.md`
**What:** Add a step before implementation: "Verify all files listed in your ticket context actually exist in the worktree. If not, create them."
**Done when:** Prompt updated, tests pass.

#### [ ] 226. QA — Parallel check execution
**Size:** Medium | **Files:** `src/golem/qa.py`
**What:** Currently checks run sequentially. Use `asyncio.gather` or `subprocess` parallelism for independent checks.
**Done when:** `uv run pytest` passes.

#### [ ] 227. QA — Check timeout
**Size:** Small | **Files:** `src/golem/qa.py`
**What:** Add `timeout=120` to `subprocess.run` in `run_qa`. A hung check blocks the entire pipeline.
**Done when:** `uv run pytest` passes.

#### [ ] 228. QA — Custom check patterns from config
**Size:** Small | **Files:** `src/golem/config.py`, `src/golem/qa.py`
**What:** Add `extra_qa_checks: list[str] = []` to config. These always run in addition to spec-specified and infra-detected checks.
**Done when:** `uv run pytest` passes.

#### [ ] 229. Pipeline — Retry individual tickets, not entire run
**Size:** Medium | **Files:** `src/golem/tech_lead.py`
**What:** If a writer fails on a ticket, instead of failing the whole run, mark ticket as `needs_work` and continue with others.
**Done when:** `uv run pytest` passes.

#### [ ] 230. Pipeline — Post-run QA sweep
**Size:** Medium | **Files:** `src/golem/cli.py`
**What:** After tech lead completes, run all QA checks against the final merged code to catch integration issues.
**Done when:** `uv run pytest` passes.

#### [ ] 231. Pipeline — Spec complexity estimator
**Size:** Small | **Files:** `src/golem/cli.py`
**What:** Before running, estimate complexity from spec (word count, task count, file references) and print "Estimated: N tasks, ~Xm runtime".
**Done when:** `uv run pytest` passes.

#### [ ] 232. Pipeline — Checkpoint/resume at ticket level
**Size:** Medium | **Files:** `src/golem/cli.py`, `src/golem/tech_lead.py`
**What:** When `golem resume` runs, skip tickets already in `done`/`approved` status and only dispatch writers for `pending`/`needs_work` tickets.
**Done when:** `uv run pytest` passes.

#### [ ] 233. Pipeline — Configurable agent models per role
**Size:** Medium | **Files:** `src/golem/config.py`, `src/golem/planner.py`, `src/golem/tech_lead.py`, `src/golem/writer.py`
**What:** Add `planner_model`, `tech_lead_model`, `writer_model` to config. Currently only `model` is used for all.
**Done when:** `uv run pytest` passes.

#### [ ] 234. Pipeline — Cost estimation
**Size:** Medium | **Files:** `src/golem/cli.py`
**What:** Estimate API token usage based on spec size and ticket count. Print before running: "Estimated cost: ~$X.XX".
**Done when:** `uv run pytest` passes.

#### [ ] 235. Worktree — `uv sync` inside new worktrees automatically
**Size:** Small | **Files:** `src/golem/worktree.py`
**What:** After creating a worktree, run `uv sync` if `pyproject.toml` exists. Currently left to the agent (which sometimes forgets).
**Done when:** `uv run pytest` passes.

#### [ ] 236. Worktree — npm/bun install inside new worktrees
**Size:** Small | **Files:** `src/golem/worktree.py`
**What:** After creating a worktree, run `npm install` or `bun install` if `package.json` exists.
**Done when:** `uv run pytest` passes.

#### [ ] 237. Planner — Pre-validate spec headings/structure
**Size:** Small | **Files:** `src/golem/planner.py`
**What:** Before sending to the LLM, parse the spec and warn about: no headings, no code blocks, no task-like patterns. More thorough than current `_validate_spec`.
**Done when:** `uv run pytest` passes.

#### [ ] 238. Pipeline — Run hook scripts at lifecycle points
**Size:** Medium | **Files:** `src/golem/cli.py`, `src/golem/config.py`
**What:** Add config for lifecycle hooks: `on_planner_complete`, `on_writer_complete`, `on_run_complete`. Each is a shell command to run.
**Done when:** `uv run pytest` passes.

#### [ ] 239. Pipeline — Git stash before run, restore on failure
**Size:** Small | **Files:** `src/golem/cli.py`
**What:** If the working tree has uncommitted changes, `git stash` before running and `git stash pop` on failure.
**Done when:** `uv run pytest` passes.

#### [ ] 240. Pipeline — Auto-detect project type for QA defaults
**Size:** Small | **Files:** `src/golem/qa.py`
**What:** Expand `detect_infrastructure_checks` to detect: mypy from pyproject.toml, jest from package.json, cargo test from Cargo.toml.
**Done when:** `uv run pytest` passes.

---

### Theme I: Documentation & Developer Experience (241–252)

#### [ ] 241. Docstrings — `tools.py` module and all handlers
**Size:** Small | **Files:** `src/golem/tools.py`
**What:** Add module docstring explaining MCP architecture. Add docstrings to all `_handle_*` functions with input/output docs.
**Done when:** All public and handler functions documented.

#### [ ] 242. Docstrings — `worktree.py:create_pr()`
**Size:** Small | **Files:** `src/golem/worktree.py`
**What:** Add docstring: purpose, parameters (especially `draft`), return value, error conditions.
**Done when:** Documented.

#### [x] 243. Docstrings — `run_planner()` full behavior (DONE: f3cf149)
**Size:** Small | **Files:** `src/golem/planner.py`
**What:** Update docstring to mention: retry logic, SDK timeout monkey-patch, self-healing fallback ticket creation.
**Done when:** Documented.

#### [x] 244. Docstrings — `run_tech_lead()` full behavior (DONE: f3cf149)
**Size:** Small | **Files:** `src/golem/tech_lead.py`
**What:** Update docstring to mention: `_ensure_merged_to_main`, `_cleanup_golem_worktrees`, retry logic.
**Done when:** Documented.

#### [x] 245. Docstrings — `sdk_env()` clarify partial override (DONE: f3cf149)
**Size:** Small | **Files:** `src/golem/config.py`
**What:** Clarify that `sdk_env()` returns a minimal dict (just clears `ANTHROPIC_API_KEY`), not a full environment copy.
**Done when:** Documented.

#### [x] 246. Docstrings — `_resolve_spec_project_root()` fallback behavior (DONE: f3cf149)
**Size:** Small | **Files:** `src/golem/cli.py`
**What:** Document the fallback: if no `.git` found, returns spec's parent directory.
**Done when:** Documented.

#### [x] 247. Docstrings — `run_autofix()` purpose and callers (DONE: f3cf149)
**Size:** Small | **Files:** `src/golem/qa.py`
**What:** Add docstring explaining when autofix runs, what it does for ruff/prettier, and that it's invoked by the QA pipeline.
**Done when:** Documented.

#### [x] 248. Docstrings — `dialogs.py:open_folder_dialog()` `initial_dir` clarification (DONE: f3cf149)
**Size:** Small | **Files:** `src/golem/dialogs.py`
**What:** Change "reserved for future use" to "silently ignored due to Windows SHBrowseForFolder API limitation".
**Done when:** Documented.

#### [ ] 249. README.md — Create a proper README
**Size:** Medium | **Files:** `README.md`
**What:** Create README with: project description, installation, quick start, architecture overview, contributing guide.
**Done when:** README exists and is accurate.

#### [ ] 250. Contributing guide — Development setup
**Size:** Small | **Files:** `CONTRIBUTING.md`
**What:** Document: how to set up dev environment, run tests, code style, PR process.
**Done when:** Guide exists.

#### [ ] 251. Architecture diagram — Visual pipeline flow
**Size:** Small | **Files:** `docs/architecture.md`
**What:** Create a text-based diagram (mermaid or ASCII) showing: Spec → Planner → Tech Lead → Writers → QA → Merge flow.
**Done when:** Diagram exists and is accurate.

#### [ ] 252. Changelog — Start maintaining CHANGELOG.md
**Size:** Small | **Files:** `CHANGELOG.md`
**What:** Create changelog with v0.1.0 and v0.2.0 entries. Follow Keep a Changelog format.
**Done when:** Changelog exists with both versions documented.

---

### Theme J: Testing Infrastructure (253–262)

#### [ ] 253. Test fixtures — Shared ticket factory
**Size:** Small | **Files:** `tests/conftest.py`
**What:** Multiple test files create tickets with boilerplate. Create a shared `conftest.py` fixture for creating test tickets with sensible defaults.
**Done when:** `uv run pytest` passes, at least 2 test files use the fixture.

#### [ ] 254. Test fixtures — Shared git repo factory
**Size:** Small | **Files:** `tests/conftest.py`
**What:** Multiple test files call `_init_git_repo`. Move to a shared fixture in conftest.py.
**Done when:** `uv run pytest` passes.

#### [ ] 255. Test fixtures — Shared golem dir factory
**Size:** Small | **Files:** `tests/conftest.py`
**What:** Create a fixture that sets up a complete `.golem/` directory structure for CLI integration tests.
**Done when:** `uv run pytest` passes.

#### [ ] 256. Integration test — Full planner → tech lead flow (mocked SDK)
**Size:** Large | **Files:** `tests/test_integration.py`
**What:** End-to-end test that mocks the SDK but exercises the full pipeline: spec → planner → tickets → tech lead → worktrees → merge.
**Done when:** `uv run pytest` passes.

#### [x] 257. Test — `golem ui` server startup and shutdown (DONE: fee52c1)
**Size:** Small | **Files:** `tests/test_ui.py`
**What:** Test that the FastAPI app starts, serves the HTML template at `/`, and shuts down cleanly.
**Done when:** `uv run pytest` passes.

#### [ ] 258. Test — `golem ui` browse endpoints with mock dialogs
**Size:** Small | **Files:** `tests/test_ui.py`
**What:** Mock `dialogs.open_file_dialog` and `open_folder_dialog`, call `/api/browse/spec` and `/api/browse/root`, verify responses.
**Done when:** `uv run pytest` passes.

#### [x] 259. Test — `validator.py:_subprocess_env()` on non-Windows (DONE: a99a236)
**Size:** Small | **Files:** `tests/test_validator.py`
**What:** Verify `_subprocess_env()` returns a valid PATH dict on non-Windows platforms (where winreg isn't available).
**Done when:** `uv run pytest` passes.

#### [x] 260. Test — `validator.py:_normalize_cmd()` edge cases (DONE: a99a236)
**Size:** Small | **Files:** `tests/test_validator.py`
**What:** Test: empty string, string with `&` and `|` characters, string with both single and double quotes.
**Done when:** `uv run pytest` passes.

#### [x] 261. Test performance — Ensure test suite runs in < 30s (DONE: already at 15.73s with 230 tests)
**Size:** Small | **Files:** `tests/`
**What:** Profile test suite, identify slow tests, add markers for slow tests (`@pytest.mark.slow`) so they can be skipped in fast mode.
**Done when:** `uv run pytest` (without slow) runs in < 30s.

#### [ ] 262. Test coverage report — Add `pytest-cov` and coverage target
**Size:** Small | **Files:** `pyproject.toml`, `tests/`
**What:** Add `pytest-cov` to dev deps. Configure coverage reporting with target of 80%. Add `uv run pytest --cov=golem` to the test command.
**Done when:** Coverage report generated, current coverage measured.

---

## Ideas & Future Work (Not Yet Scheduled)

### Agent Observability / Live Streaming
Beyond basic stderr streaming, we eventually want:
- A TUI dashboard showing all active agents and their current activity
- The web UI (`golem ui`) to show real-time agent activity via SSE
- Structured event format that both TUI and web UI can consume

### Parallel Writer Verification
After Tech Lead dispatches multiple writers in parallel, verify all worktrees have changes before merging. Currently no validation that writers actually wrote code.

### Multi-Spec Orchestration
Run multiple specs in sequence or parallel. Useful for large projects broken into sub-specs.

### Spec Templates
Provide starter spec templates for common project types: REST API, CLI tool, React app, Python library.

### Plugin System
Allow custom MCP tools to be injected into agent sessions from project-level config.

### Remote Execution
Run Golem agents on remote machines or in containers for isolation and parallelism.
