# SDK Optimization & Cleanup Spec

## Overview

Deep code review of all Claude Agent SDK integration files revealed **31 findings** across 4 categories: bugs, SDK migration opportunities, dead code, and performance issues. This spec organizes the work into 3 phases with 9 parallel worktrees for maximum throughput.

**Scope:** `supervisor.py`, `planner.py`, `tech_lead.py`, `junior_dev.py`, `config.py`, `recovery.py`, `parallel.py`, `orchestrator.py`, `pipeline.py`, `tools.py`, `server.py`, `tool_registry.py`

**Source:** `claude-api` skill SDK reference (full Agent SDK docs), 7 parallel review agents

---

## Phase 1 -- Independent Fixes (4 parallel worktrees)

Zero file overlap between worktrees. All low-risk.

### WT-1: pipeline.py (_current_task bug)

**Files:** `pipeline.py`
**Items:** #2

**Bug:** `PipelineCoordinator._current_task` is defined at line 71 but **never assigned**. `kill()` at line 344 calls `self._current_task.cancel()` but it is always `None`. Kill only works cooperatively via the `_killed` flag checked at 2 guard points (lines 135, 219).

**Fix:**
- Wrap the `run_planner()` / `run_tech_lead()` / `spawn_junior_dev()` awaits in `asyncio.create_task()` and assign to `self._current_task`
- Clear `_current_task = None` after each stage completes
- Verify `kill()` actually cancels the running stage task

**Tests:** Add test that `kill()` during a running stage actually cancels the task (mock the agent call with a long `asyncio.sleep`, call `kill()`, verify it completes promptly).

**Completion gate:** `uv run pytest tests/test_pipeline.py -x` passes, kill test verifies cancellation.

---

### WT-2: tools.py + server.py (dead code + consolidation)

**Files:** `tools.py`, `server.py`
**Items:** #16, #17, #18, #22, #27

**Dead code removal:**
- **#16:** Delete `_build_tools()` (lines 889-962) -- duplicates `build_tool_registry()`, missing 4 newer tools. Also delete `handle_tool_call()` (line 1098) and `get_tech_lead_tools()` (lines 970-978) if they depend solely on `_build_tools`.
- **#17:** Delete `create_qa_mcp_server()` (lines 1036-1044) -- standalone QA server, appears unused by any active code path. Verify with `grep -r "create_qa_mcp_server"` first.
- **#18:** Delete `create_golem_mcp_sse_config()` (lines 1023-1033) -- SSE transport broken per CLAUDE.md, dead code.

**Consolidation:**
- **#22:** `server.py` lines 769-785 bypass `build_tool_registry()` and import internal functions directly (`_build_tools`, `_handle_run_qa`, schema dicts). Refactor to use `build_tool_registry()` or `create_junior_dev_mcp_server()` instead, eliminating the third tool construction path.

**Blocking I/O:**
- **#27:** `_handle_get_session_context` (lines 598-655) calls `read_text()` synchronously on the event loop. Wrap in `asyncio.to_thread()`. Also check `_handle_read_ticket`, `_handle_list_tickets`, `_handle_get_build_progress` for same pattern.

**Tests:** `uv run pytest tests/test_tools.py tests/test_server.py -x`

**Completion gate:** All tool tests pass. `grep -r "_build_tools\|create_qa_mcp_server\|create_golem_mcp_sse_config" src/` returns zero hits (except comments/changelog).

---

### WT-3: config.py (dead code + sdk_env fix)

**Files:** `config.py`
**Items:** #4, #13, #14, plus prep for Phase 3

**Dead code:**
- **#13:** Delete `ComplexityProfile` dataclass (lines 12-20) -- orphaned, `apply_complexity_profile()` works from raw dicts. Or wire it up properly (preferred: type the complexity_profiles dict values as `ComplexityProfile`).
- **#14:** Delete `subagent_max_steps` field (line 89) -- zero consumers anywhere in codebase.

**Bug fix:**
- **#4:** `sdk_env()` has `session_id` and `golem_dir` optional params (lines 319-322) but all 11 call sites use `sdk_env()` with no args. Audit each call site in `planner.py`, `tech_lead.py`, `junior_dev.py`, `supervisor.py` and pass `session_id`/`golem_dir` where available. This ensures hooks receive `GOLEM_SESSION_ID` via the environment.

**Phase 3 prep:**
- Add `max_budget_usd` fields to `GolemConfig` per complexity tier: `planner_budget_usd`, `tech_lead_budget_usd`, `worker_budget_usd` with sensible defaults (e.g., TRIVIAL=0.10, SIMPLE=0.50, STANDARD=2.00, CRITICAL=5.00). Also add to `complexity_profiles` dict.
- Add `fallback_model` field (default `"claude-sonnet-4-6"`).

**Tests:** `uv run pytest tests/test_config.py -x`

**Completion gate:** Config tests pass. `grep -r "subagent_max_steps" src/` returns only config.py definition (if kept for backward compat) or zero hits.

---

### WT-4: parallel.py + orchestrator.py (dedup + performance)

**Files:** `parallel.py`, `orchestrator.py`
**Items:** #20, #21, #28, #29, #30

**Deduplication:**
- **#20:** `_is_rate_limit_error()` is defined identically in `parallel.py:49` and `orchestrator.py:255`. Extract to a shared module (e.g., `recovery.py` which already does rate limit classification, or a new `golem/utils.py`). Both files import from the shared location.
- **#21:** `_create_batches()` is defined identically in `parallel.py:81` and `orchestrator.py:260`. Same treatment -- extract to shared location.

**Performance fixes:**
- **#28:** `ParallelExecutor` semaphore (line 128) is redundant with batch size (line 144) since both equal `max_concurrency`. Replace fixed batching with semaphore-only work-stealing: launch all tasks gated by semaphore, no batch boundaries. This keeps workers busy when one task is slow.
- **#29:** Rate limit backoff exponent (lines 177-179) accumulates globally across ALL batches via `sum(1 for r in all_results if r.rate_limited)`. This causes unbounded backoff escalation. Fix: count only the current batch's rate-limited results for the exponent, or cap the exponent (e.g., `min(rl_count, 5)`).
- **#30:** `WaveExecutor` in `orchestrator.py` uses plain `asyncio.sleep(backoff_s)` (line 629) for rate limit backoff. Replace with `_interruptible_sleep()` pattern (check cancel event) matching `ParallelExecutor`'s approach.

**Tests:** `uv run pytest tests/test_parallel.py tests/test_orchestrator.py -x`

**Completion gate:** All parallel/orchestrator tests pass. `grep -rn "_is_rate_limit_error\|_create_batches" src/` shows each defined in exactly one location.

---

## Phase 2 -- Cross-cutting Fixes (3 parallel worktrees)

Requires Phase 1 merged first. Careful interface boundaries between worktrees.

### WT-5: supervisor.py + recovery.py (core refactor)

**Files:** `supervisor.py`, `recovery.py`
**Items:** #1, #6, #23, #24

**Bug fix:**
- **#1 (HIGH):** Continuation cost double-counting at `supervisor.py:680`. `total_cost_usd` from `ResultMessage` is cumulative across the entire session including prior continuation segments. But `continuation_supervised_session()` sums each segment's `cost_usd`. Fix: track `previous_cumulative_cost` and compute `segment_cost = total_cost_usd - previous_cumulative_cost` for each segment. Apply same fix to `input_tokens` and `output_tokens` if they exhibit the same cumulative behavior.

**RateLimitEvent handling:**
- **#6 (partial):** Add `isinstance(message, RateLimitEvent)` branch in `supervised_session()` message loop (after line 317). When received:
  - Emit `AgentRateLimitWarning` event (new event type in `events.py`) with `utilization`, `status`, `resets_at`
  - If `status == "rejected"`, set `kill_hit = True` (same as stall kill) and record the `resets_at` timestamp
  - Propagate `resets_at` through `SupervisedResult` (new field: `rate_limit_resets_at: float | None = None`)
  - In `recovery.py`, use `resets_at` for precise sleep duration instead of fixed `rate_limit_cooldown_s=300`

**Type mismatch:**
- **#23:** `RecoveryCoordinator.run_with_recovery()` is typed to return `SupervisedResult` (recovery.py line 336) but receives `ContinuationResult` from the session lambda. Fix: make `run_with_recovery()` generic (`T`) or change the type annotation to `ContinuationResult`. Both dataclasses have compatible fields -- formalize with a shared `Protocol` or base class.

**Token burn prep:**
- **#24:** Document the current drain-after-kill pattern. Add a `_drain_remaining_turns` counter to `SupervisedResult` so callers know how many tokens were wasted. This is prep for Phase 3's `ClaudeSDKClient.interrupt()` which will eliminate the drain entirely.

**New field on SupervisedResult:**
- Add `cache_read_tokens: int = 0` field. Extract `cache_read_input_tokens` from `ResultMessage.usage` dict (line 399) alongside existing `input_tokens`/`output_tokens` extraction.

**Tests:** `uv run pytest tests/test_recovery.py tests/test_events.py -x` (update event count if new event type added).

**Completion gate:** Recovery tests pass. Cost double-counting test verifies `segment_cost = total - previous`. RateLimitEvent test verifies structured handling.

---

### WT-6: Agent files (edict_id + stall consistency + blocking I/O)

**Files:** `planner.py`, `tech_lead.py`, `junior_dev.py`
**Items:** #3, #15, #19, #25, #26, #31

**Bug fix:**
- **#3:** Pass `edict_id` to `RecoveryCoordinator.run_with_recovery()` in both `planner.py` (line 183) and `tech_lead.py` (line 228). Extract `edict_id` from the golem_dir path or accept it as a parameter. This enables `needs_attention` events to fire on failures.

**Dead code:**
- **#15:** Delete `_MAX_RETRIES = 2` from `tech_lead.py:9` -- never referenced.

**Consistency:**
- **#19:** All 3 agents have stall-retry paths that bypass `RecoveryCoordinator`:
  - `planner.py` lines 212-227
  - `tech_lead.py` lines 266-282 and 303-319
  - `junior_dev.py` lines 252-306
  
  These catch only `CLIConnectionError | ClaudeSDKError` with no backoff, no circular detection, no event emission. Fix: wrap each stall-retry `continuation_supervised_session()` call in a second `RecoveryCoordinator.run_with_recovery()` invocation (with reduced `max_retries=1`), or extract the error handling into a shared helper.

**Performance:**
- **#25:** `tech_lead.py` `_ensure_merged_to_main()` and `_check_integration_commits()` (lines 43-145) use blocking `subprocess.run()`. Wrap each in `await asyncio.to_thread(subprocess.run, ...)`.
- **#26:** `junior_dev.py` `git diff --stat HEAD` (lines 313-322) uses blocking `subprocess.run()`. Same fix.
- **#31:** Read `cache_read_tokens` from `SupervisedResult` (added in WT-5) and populate each agent's result dataclass (`PlannerResult`, `TechLeadResult`, `JuniorDevResult`).

**Tests:** `uv run pytest tests/test_planner.py tests/test_tech_lead.py tests/test_junior_dev.py -x` (excluding slow tests for fast iteration).

**Completion gate:** Agent tests pass. `grep -rn "needs_attention" src/golem/recovery.py` confirms event is emitted. Stall retry paths use RecoveryCoordinator.

---

### WT-7: parallel.py (RateLimitEvent wire-up)

**Files:** `parallel.py` (post-Phase 1 merge)
**Items:** #6 (parallel.py part)

**Wire RateLimitEvent data into executor:**
- After WT-5 adds `rate_limit_resets_at` to `SupervisedResult`, update `ParallelExecutor` to:
  - Read `resets_at` from task results when available
  - Use `max(0, resets_at - time.time())` as the backoff duration instead of geometric `base * 2^count`
  - Fall back to geometric backoff if `resets_at` is `None`
- Replace `_is_rate_limit_error()` calls (now in shared location from WT-4) with structured `RateLimitEvent` data where available.

**Tests:** `uv run pytest tests/test_parallel.py -x`

**Completion gate:** Parallel tests pass. Backoff uses precise `resets_at` when available.

---

## Phase 3 -- SDK Migration (2 parallel worktrees)

Requires Phase 2 merged first. High impact, needs thorough testing.

### WT-8: ClaudeSDKClient migration (the big one)

**Files:** `supervisor.py`, `planner.py`, `tech_lead.py`, `junior_dev.py`, `config.py`, `recovery.py`
**Items:** #5, #7, #8, #9, #11, #12

**Pre-work: Verify `interrupt()` safety**
Before starting, dispatch a research agent to:
1. Check if `ClaudeSDKClient.interrupt()` cleanly terminates the `receive_response()` generator without triggering the anyio cancel-scope cross-task `RuntimeError`
2. If it does trigger the error, this item is blocked and must remain on `query()` with drain pattern
3. Test with a minimal repro script on Windows

**Core migration (#5):**
- Refactor `supervised_session()` in `supervisor.py` from:
  ```python
  async for message in query(prompt=current_prompt, options=options):
  ```
  To:
  ```python
  async with ClaudeSDKClient(options=options) as client:
      await client.query(current_prompt)
      async for message in client.receive_response():
  ```
- Replace stall-kill drain pattern (set `kill_hit=True`, continue consuming) with `await client.interrupt()` at the kill threshold
- This eliminates the token burn after kill (up to 50% of `max_turns` savings)
- Update `continuation_supervised_session()` accordingly

**max_budget_usd (#7):**
- Wire `config.planner_budget_usd`, `config.tech_lead_budget_usd`, `config.worker_budget_usd` (added in WT-3) into `ClaudeAgentOptions` construction in each agent file
- Add `max_budget_usd` to `CONTEXT_EXHAUSTION_REASONS` handling in `supervisor.py` (budget exhaustion should not be treated as context exhaustion)

**Session resumption (#8):**
- In `continuation_supervised_session()`, replace the Haiku summarizer + prompt rebuild pattern with `resume=sdk_session_id` in `ClaudeAgentOptions`
- Keep the summarizer as a fallback if `resume` fails or `sdk_session_id` is empty
- This eliminates the summarizer call cost and preserves full context

**HookMatcher (#9):**
- Replace shell hooks in `.claude/hooks/` with Python-native `HookMatcher` callbacks in `ClaudeAgentOptions.hooks`
- Port `block-golem-cli.py` logic to a `PreToolUse` hook callback
- Port `block-dangerous-git.py` logic (delegates to `golem.security.validate_command()`) to a `PreToolUse` hook callback
- Port `block-ask-user-question.py` to a `PreToolUse` hook callback
- Remove the `GOLEM_SDK_SESSION=1` env guard -- in-process hooks only fire within SDK sessions by definition
- Keep shell hooks as fallback for any non-SDK session paths

**fallback_model (#11):**
- Set `fallback_model=config.fallback_model` (default `"claude-sonnet-4-6"`) on all `ClaudeAgentOptions`
- This provides automatic model degradation without retry logic

**task_budget (#12):**
- Add `betas=["task-budgets-2026-03-13"]` and `task_budget={"total": N}` to `ClaudeAgentOptions` for planner and tech_lead sessions
- Derive `N` from `max_turns * estimated_tokens_per_turn`
- This makes the model self-pace near its budget, reducing stall-kill frequency

**Tests:** Full test suite `uv run pytest --ignore=tests/test_mcp_durability.py --ignore=tests/test_mcp_sse.py -x`

**Completion gate:** All tests pass. Verify with a smoke test against `golem-test/smoke-counter` that:
- Sessions use `ClaudeSDKClient` (check stderr for `[PLANNER]`/`[TECH LEAD]` output)
- Kill actually interrupts immediately (not drain)
- Rate limit backoff uses precise `resets_at`
- `max_budget_usd` prevents runaway cost
- `cache_read_tokens` appears in pipeline result

---

### WT-9: tools.py (@tool decorator migration)

**Files:** `tools.py`
**Items:** #10

**Migration:**
- Replace 10 manual `_*_INPUT_SCHEMA` dicts (lines 42-215) with `@tool` decorated async functions
- Replace 12 `_make_*` factory functions (lines 715-858) with direct `@tool` registrations
- Use `create_sdk_mcp_server(name, tools=[...])` with the decorated functions
- Preserve `ToolRegistry` for per-agent filtering (SDK's `disallowed_tools` doesn't cover MCP tools)
- Preserve `ToolContext` for runtime parameter injection (SDK has no equivalent)

**Constraint:** The `@tool` decorator + `create_sdk_mcp_server` pattern works with both `query()` and `ClaudeSDKClient`. This worktree does NOT depend on WT-8 completing first -- it can run in parallel.

**Tests:** `uv run pytest tests/test_tools.py -x`

**Completion gate:** All tool tests pass. Manual schema dicts deleted. `@tool` decorated functions produce identical MCP tool schemas.

---

## Execution Timeline

```
Phase 1:  WT-1 -----+                          (pipeline.py)
          WT-2 -----------+                     (tools.py + server.py)
          WT-3 -------+                         (config.py)
          WT-4 -----------+                     (parallel.py + orchestrator.py)
                           \ merge all 4
Phase 2:            WT-5 -----------+           (supervisor.py + recovery.py)
                    WT-6 -----------+           (3 agent files)
                    WT-7 -----+                 (parallel.py wire-up)
                               \ merge all 3
Phase 3:                  WT-8 -------------------+  (SDK migration)
                          WT-9 -----------+          (@tool decorator)
                                           \ merge both
```

**Max concurrent worktrees:** 4 (Phase 1)
**Total worktrees:** 9
**Estimated items per worktree:** 1-6

---

## Risk Mitigation

| Risk | Mitigation |
|------|-----------|
| `interrupt()` triggers anyio bug | Research agent verifies before Phase 3 starts. If unsafe, keep drain pattern and skip #5 |
| `resume=session_id` conflicts with MCP server re-init | Keep Haiku summarizer as fallback, feature-flag the resume path |
| `@tool` schema differs from manual schemas | Generate both, diff, assert identical in test |
| `RateLimitEvent` not emitted by current SDK version | Guard with `isinstance` check, fall back to regex detection |
| Merge conflicts between phases | Zero file overlap within each phase by design |
| `max_budget_usd` stop_reason unhandled | Add to `CONTEXT_EXHAUSTION_REASONS` or create separate handler |

---

## Test Strategy

- **Per-worktree:** Run the relevant test files after each worktree completes
- **Per-phase:** Run full fast suite (`uv run pytest --ignore=tests/test_junior_dev.py --ignore=tests/test_tech_lead.py --ignore=tests/test_mcp_durability.py --ignore=tests/test_mcp_sse.py`) after each phase merge
- **Final:** Full suite + smoke test against `golem-test/smoke-counter`
- **Event count:** If `AgentRateLimitWarning` event is added in WT-5, update count assertions in `test_events.py`, `test_recovery.py`, `test_parallel.py` (currently 44)
