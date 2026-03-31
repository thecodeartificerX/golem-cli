# Phase 2: File Rename (writer → junior_dev)

## Gotchas
- `writer.py` already uses `junior_dev` internally (JuniorDevResult, spawn_junior_dev, junior_dev.md template) — this phase is purely the file rename + import path updates
- Backward-compatible aliases exist (`WriterResult = JuniorDevResult`, `spawn_writer_pair = spawn_junior_dev`) — remove these after all imports are updated
- The MCP server name is `"golem-junior-dev"` already — no MCP changes needed
- `prompts/junior_dev.md` and `prompts/junior_dev_rework.md` already exist (renamed in a prior version) — verify before attempting rename
- `test_writer.py` imports from `golem.writer` — the test file rename must also update these imports
- `CLAUDE.md` references `writer.py` in the project structure — update the docs

## Files
```
src/golem/
├── writer.py             # RENAME → junior_dev.py
├── prompts/worker.md     # VERIFY — may already be junior_dev.md
├── prompts/worker_rework.md  # VERIFY — may already be junior_dev_rework.md
├── tech_lead.py          # MODIFY — update imports from golem.writer → golem.junior_dev
├── orchestrator.py       # MODIFY — update imports
├── planner.py            # MODIFY — update imports (if any)
├── server.py             # MODIFY — update imports
├── cli.py                # MODIFY — update imports
├── tools.py              # MODIFY — update imports
├── __init__.py           # MODIFY — update imports (if re-exported)
tests/
├── test_writer.py        # RENAME → test_junior_dev.py + update imports
├── test_tech_lead.py     # MODIFY — update imports if referencing writer
├── test_tools.py         # MODIFY — update imports if referencing writer
├── test_server.py        # MODIFY — update mock paths from golem.writer → golem.junior_dev
```

---

## Task 2.1: Rename writer.py → junior_dev.py and Update All Imports

**Skills to load:** None (mechanical refactor)

**Architecture notes:**

This is a mechanical rename. Steps:
1. `git mv src/golem/writer.py src/golem/junior_dev.py`
2. Search all `.py` files for `from golem.writer` and `import golem.writer` — replace with `golem.junior_dev`
3. Search all `.py` files for `"golem.writer"` in string literals (mock paths) — replace with `"golem.junior_dev"`
4. Remove the backward-compatible aliases at the bottom of junior_dev.py:
   - `WriterResult = JuniorDevResult` — remove
   - `spawn_writer_pair = spawn_junior_dev` — remove
5. Update any remaining references to `WriterResult` in other files to `JuniorDevResult`
6. Update any remaining references to `spawn_writer_pair` to `spawn_junior_dev`

Key files to check for writer imports (from codebase exploration):
- `tech_lead.py` — imports `spawn_writer_pair` or `spawn_junior_dev`
- `orchestrator.py` — imports `spawn_junior_dev` (line in `_run_single_ticket`)
- `server.py` — may import `run_session` which chains to writer
- `cli.py` — may import writer for direct dispatch in TRIVIAL tier
- `tools.py` — may reference writer for MCP tool registration
- `conftest.py` — shared fixtures may reference writer

**Files to modify:** All files containing `golem.writer` import paths (use `rg "golem\.writer" src/ tests/` to find all)

**Validation command:** `uv run pytest -x --tb=short` (full suite — import errors will surface immediately)

**Tests:** No new tests. Existing tests must pass with renamed imports.

---

## Task 2.2: Rename test_writer.py → test_junior_dev.py

**Skills to load:** None (mechanical refactor)

**Architecture notes:**

1. `git mv tests/test_writer.py tests/test_junior_dev.py`
2. Update all `from golem.writer` imports inside the file to `from golem.junior_dev`
3. Update any mock paths (`patch("golem.writer.xyz")` → `patch("golem.junior_dev.xyz")`)

**Files to modify:**
- `tests/test_writer.py` → `tests/test_junior_dev.py`

**Validation command:** `uv run pytest tests/test_junior_dev.py -v`

**Tests:** Existing writer tests must pass under the new filename.

---

## Task 2.3: Verify Prompt File Names

**Skills to load:** None

**Architecture notes:**

Verify that these files already exist with the correct names:
- `src/golem/prompts/junior_dev.md` (not `worker.md`)
- `src/golem/prompts/junior_dev_rework.md` (not `worker_rework.md`)

If old names still exist:
- `git mv src/golem/prompts/worker.md src/golem/prompts/junior_dev.md`
- `git mv src/golem/prompts/worker_rework.md src/golem/prompts/junior_dev_rework.md`

Search for any remaining string references to `"worker.md"` or `"worker_rework.md"` in Python source and update them.

**Validation command:** `uv run pytest tests/test_junior_dev.py -v`

---

## Task 2.4: Update CLAUDE.md References

**Skills to load:** `claude-md-management:revise-claude-md`

Update the project structure section and any other references in `CLAUDE.md`:
- `writer.py` → `junior_dev.py` with updated description
- `prompts/worker.md` → `prompts/junior_dev.md`
- `prompts/worker_rework.md` → `prompts/junior_dev_rework.md`
- `test_writer.py` → `test_junior_dev.py`
- Any "writer" references in gotchas sections → "junior_dev"

**Validation command:** Visual review of CLAUDE.md
