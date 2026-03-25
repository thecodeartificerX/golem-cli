# Phase 3: Plugin Discovery Fallback (Conditional)

> **Only implement this phase if Phase 1 + 2 smoke testing reveals that `setting_sources` alone does not carry plugins through to SDK subprocess sessions.**

## Gotchas

- `installed_plugins.json` format may vary — always handle missing keys gracefully
- Use `Path.home()` not `os.path.expanduser("~")` for Windows compatibility
- `SdkPluginConfig` is a `TypedDict` with `type: Literal["local"]` and `path: str` — import from `claude_agent_sdk`
- Plugin cache paths in `installed_plugins.json` are absolute paths — verify they exist before including
- Return `[]` on ANY error — never let plugin discovery crash a golem run

## Files

```
src/golem/
└── config.py        # MODIFY — add discover_plugins()
tests/
└── test_config.py   # MODIFY — add Phase 2 discover_plugins tests
src/golem/
├── worker.py        # MODIFY — add plugins=discover_plugins() to query()
├── validator.py     # MODIFY — add plugins=discover_plugins() to query()
└── planner.py       # MODIFY — add plugins=discover_plugins() to query()
```

## Task 1: Implement `discover_plugins()`

**Skills to load:** `superpowers:test-driven-development`

**Description:** Add a `discover_plugins()` function to `config.py` that reads the Claude Code plugin cache and returns a list of `SdkPluginConfig` entries.

**Acceptance criteria:**
- Reads `Path.home() / ".claude" / "plugins" / "installed_plugins.json"`
- Filters to enabled plugins only
- Returns `list[SdkPluginConfig]` with `{"type": "local", "path": "<absolute_path>"}` entries
- Returns `[]` if file not found, malformed JSON, or any error
- Verifies each plugin cache path exists before including it

**Files:**
- `src/golem/config.py` — add `discover_plugins()` function, add `from claude_agent_sdk import SdkPluginConfig` import

**Architecture notes:**
- The function is pure — no side effects, no state
- `installed_plugins.json` structure needs to be read from the actual file at `Path.home() / ".claude" / "plugins" / "installed_plugins.json"` to determine the schema. The function should handle both known and unknown schema shapes gracefully.
- Wrap the entire function body in a try/except that returns `[]` — plugin discovery must never crash golem

**Validation:** `uv run python -c "from golem.config import discover_plugins; print(discover_plugins())"`

---

## Task 2: Wire `discover_plugins()` into query() calls

**Description:** Add `plugins=discover_plugins()` to all three `ClaudeAgentOptions` constructors.

**Acceptance criteria:**
- `worker.py` query() call includes `plugins=discover_plugins()`
- `validator.py` query() call includes `plugins=discover_plugins()`
- `planner.py` query() call includes `plugins=discover_plugins()`
- `discover_plugins` is imported from `golem.config` in each file

**Files:**
- `src/golem/worker.py` — add import + kwarg
- `src/golem/validator.py` — add import + kwarg
- `src/golem/planner.py` — add import + kwarg

**Architecture notes:**
- `discover_plugins()` is called fresh on each `query()` invocation. This is fine — it reads a small JSON file and the cost is negligible vs. spawning a Claude session.
- Import alongside existing `from golem.config import GolemConfig, sdk_env` — add `discover_plugins` to the import list

**Validation:** `uv run ruff check src/`

---

## Task 3: Add Phase 2 tests to `test_config.py`

**Skills to load:** `superpowers:test-driven-development`

**Description:** Add tests for `discover_plugins()` to the existing `test_config.py`.

**Acceptance criteria:**
- `test_discover_plugins_missing_file` — call with no `installed_plugins.json` present, verify returns `[]`
- `test_discover_plugins_malformed_json` — write invalid JSON to the expected path, verify returns `[]`
- `test_discover_plugins_reads_installed` — write a valid `installed_plugins.json` with known entries, verify returns correct `SdkPluginConfig` list

**Files:**
- `tests/test_config.py` — add tests

**Architecture notes:**
- These tests need to mock or redirect `Path.home()` to a tempdir to avoid reading the real user's plugin config
- Use `unittest.mock.patch` on `Path.home` to point at the tempdir
- Follow existing test patterns: `tempfile.TemporaryDirectory`, `encoding="utf-8"`

**Test descriptions:**
- Missing file — `Path.home()` points to empty tempdir, function returns `[]`
- Malformed JSON — write garbage to the expected path, function returns `[]`
- Valid file — write a realistic `installed_plugins.json`, verify correct entries returned with correct types

**Validation:** `uv run pytest tests/test_config.py -v`

---

## Phase Gate

`uv run pytest -v && uv run ruff check src/ tests/` — all green.
