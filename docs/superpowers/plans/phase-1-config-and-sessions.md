# Phase 1: Config + Session Options

## Gotchas

- `field` is NOT currently imported in `config.py` — only `asdict` and `dataclass`. Must add `field` to the import.
- The `tools` parameter takes a dict `{"type": "preset", "preset": "claude_code"}`, not a string. Do not pass `"claude_code"`.
- Remove `allowed_tools=` kwarg entirely — do not pass it as `[]`. The `tools` preset provides the full set.
- Planner uses `max_turns=30` as a hardcoded literal. Do NOT change this to a config field.
- Validator must keep `disallowed_tools=["Write", "Edit"]` alongside the new `tools` preset.
- All `encoding="utf-8"` on file operations must be preserved (Windows compat).

## Files

```
src/golem/
├── config.py     # MODIFY — add field import, add setting_sources to GolemConfig
├── worker.py     # MODIFY — swap allowed_tools for tools preset, add setting_sources
├── validator.py  # MODIFY — swap allowed_tools for tools preset, add setting_sources
└── planner.py    # MODIFY — swap allowed_tools for tools preset, add setting_sources
```

## Task 1: Add `setting_sources` to `GolemConfig`

**Description:** Add a `setting_sources` field to the `GolemConfig` dataclass that defaults to `["user", "project", "local"]`. Update the `dataclasses` import to include `field`.

**Acceptance criteria:**
- `GolemConfig().setting_sources` returns `["user", "project", "local"]`
- `load_config` correctly reads and applies `setting_sources` from `config.json` (it already handles unknown keys by filtering against `__dataclass_fields__`, so this works automatically)
- `save_config` serializes the new field (it uses `asdict`, so this works automatically)

**Files:**
- `src/golem/config.py` — add `from dataclasses import asdict, dataclass, field`; add `setting_sources: list[str] = field(default_factory=lambda: ["user", "project", "local"])` as the last field in the dataclass

**Architecture notes:**
- Type is `list[str]` not `list[Literal[...]]` — the SDK accepts the literal values but we keep the config type simple since `load_config` deserializes from JSON strings
- Default must use `field(default_factory=...)` because mutable default
- Position: add as the last field in the dataclass to maintain `config.json` compatibility

**Validation:** `python -c "from golem.config import GolemConfig; c = GolemConfig(); assert c.setting_sources == ['user', 'project', 'local']"`

---

## Task 2: Update worker session options

**Description:** Replace the hardcoded `allowed_tools` with the `claude_code` tools preset and pass `setting_sources` from config.

**Acceptance criteria:**
- `query()` call uses `tools={"type": "preset", "preset": "claude_code"}` instead of `allowed_tools`
- `setting_sources=config.setting_sources` is passed
- No `allowed_tools` kwarg is present
- All other kwargs (`model`, `cwd`, `max_turns`, `permission_mode`, `env`) unchanged

**Files:**
- `src/golem/worker.py` — modify the `ClaudeAgentOptions(...)` constructor in `run_worker()`

**Architecture notes:**
- The `config` parameter is already passed to `run_worker` as `GolemConfig` — access `config.setting_sources` directly
- Keep `permission_mode="bypassPermissions"` and `env=sdk_env()` as-is

**Validation:** `uv run python -c "from golem.worker import run_worker; print('import ok')"`

---

## Task 3: Update validator session options

**Description:** Replace the hardcoded `allowed_tools` with the `claude_code` tools preset and pass `setting_sources` from config. Preserve `disallowed_tools`.

**Acceptance criteria:**
- `query()` call in `run_ai_validator` uses `tools={"type": "preset", "preset": "claude_code"}` instead of `allowed_tools`
- `setting_sources=config.setting_sources` is passed
- `disallowed_tools=["Write", "Edit"]` is preserved
- No `allowed_tools` kwarg is present

**Files:**
- `src/golem/validator.py` — modify the `ClaudeAgentOptions(...)` constructor in `run_ai_validator()`

**Architecture notes:**
- Only `run_ai_validator` has a `query()` call. `run_deterministic_checks` uses `subprocess.run` and is unaffected.
- The `config` parameter is already passed to `run_ai_validator`

**Validation:** `uv run python -c "from golem.validator import run_validation; print('import ok')"`

---

## Task 4: Update planner session options

**Description:** Replace the hardcoded `allowed_tools` with the `claude_code` tools preset and pass `setting_sources` from config.

**Acceptance criteria:**
- `query()` call uses `tools={"type": "preset", "preset": "claude_code"}` instead of `allowed_tools`
- `setting_sources=config.setting_sources` is passed
- `max_turns=30` remains as a hardcoded literal
- No `allowed_tools` kwarg is present

**Files:**
- `src/golem/planner.py` — modify the `ClaudeAgentOptions(...)` constructor in `run_planner()`

**Architecture notes:**
- The `config` parameter is already passed to `run_planner`
- Do not change `max_turns=30` to a config field

**Validation:** `uv run python -c "from golem.planner import run_planner; print('import ok')"`

---

## Phase Gate

Run `uv run ruff check src/ tests/` — must pass with no errors.
