# Phase 2: Tests + Verification

## Gotchas

- `asyncio_mode = "auto"` is set in `pyproject.toml` — no `@pytest.mark.asyncio` needed on async tests
- Test files use `tempfile.TemporaryDirectory` for filesystem tests — follow the same pattern for config file tests
- `encoding="utf-8"` must be used on all file writes in tests (Windows compat)
- `test_config.py` does NOT exist yet — create it from scratch
- Existing tests must pass without modification — run the full suite after creating the new tests

## Files

```
tests/
├── __init__.py          # EXISTS — no change
├── test_config.py       # CREATE — new config tests
├── test_executor.py     # EXISTS — verify still passes
├── test_tasks.py        # EXISTS — verify still passes
├── test_validator.py    # EXISTS — verify still passes
└── test_worktree.py     # EXISTS — verify still passes
```

## Task 1: Write `test_config.py` Phase 1 tests

**Skills to load:** `superpowers:test-driven-development`

**Description:** Create `tests/test_config.py` with tests verifying the new `setting_sources` field behavior.

**Acceptance criteria:**
- `test_default_setting_sources` — `GolemConfig().setting_sources == ["user", "project", "local"]`
- `test_load_config_overrides_setting_sources` — write a `config.json` with `{"setting_sources": ["project"]}` to a temp dir, call `load_config`, verify result has `setting_sources == ["project"]`
- `test_load_config_preserves_default_setting_sources` — write a `config.json` without `setting_sources`, verify default is preserved
- `test_save_config_includes_setting_sources` — create a config, save it, read the JSON back, verify `setting_sources` is present

**Files:**
- `tests/test_config.py` — new file

**Architecture notes:**
- Follow the existing test pattern: factory helpers for test data, `tempfile.TemporaryDirectory` for filesystem ops
- Import `GolemConfig`, `load_config`, `save_config` from `golem.config`
- All file operations use `encoding="utf-8"`

**Test descriptions:**
- Default field value test — pure unit test, no filesystem
- Config override test — writes JSON to tempdir, reads back via `load_config`
- Missing field test — JSON without `setting_sources` key, verify default preserved
- Round-trip test — save then load, verify field survives

**Validation:** `uv run pytest tests/test_config.py -v`

---

## Task 2: Run full test suite and verify

**Description:** Run the entire test suite to confirm Phase 1 changes don't break existing tests.

**Acceptance criteria:**
- All tests in `tests/test_config.py` pass
- All tests in `tests/test_tasks.py` pass
- All tests in `tests/test_executor.py` pass
- All tests in `tests/test_validator.py` pass
- All tests in `tests/test_worktree.py` pass
- `uv run ruff check src/ tests/` passes

**Validation:** `uv run pytest -v && uv run ruff check src/ tests/`

---

## Phase Gate

All tests green. Lint clean. Ready for manual smoke test or Phase 3 if needed.
