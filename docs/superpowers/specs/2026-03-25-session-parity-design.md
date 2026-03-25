# Session Parity: Give Golem Sessions Full Claude Code Capabilities

## Problem

Golem's SDK sessions (planner, worker, validator) spawn with a hardcoded subset of 6 tools (`Bash`, `Read`, `Edit`, `Write`, `Glob`, `Grep`) and no access to the user's installed plugins, skills, MCP servers, or project-level settings. This makes golem workers significantly less capable than a standalone Claude Code session running in the same project — they can't use WebSearch/WebFetch for research, can't leverage skills like `frontend-design` or `superpowers:systematic-debugging`, and don't inherit the user's permissions or project configuration.

## Goal

Golem sessions should have the same power as a standalone Claude Code session. Whatever the user has set up — plugins, skills, MCP servers, permissions, CLAUDE.md — golem's spawned sessions inherit automatically with zero configuration.

## Design

### Phase 1: `setting_sources` + Claude Code Preset

The primary mechanism is two SDK parameters that already exist but golem isn't using:

**`setting_sources=["user", "project", "local"]`** — tells the spawned `claude` subprocess to load all config scopes:
- `user` — `~/.claude/settings.json` (global plugins, permissions, preferences)
- `project` — `.claude/settings.json` in the repo (project-specific rules, plugins)
- `local` — `.claude/settings.local.json` (user's local overrides, gitignored)

This makes the subprocess discover and load installed plugins, MCP servers, hookify rules, CLAUDE.md, and permission rules — exactly as an interactive session would.

**`tools={"type": "preset", "preset": "claude_code"}`** — gives the session the full Claude Code tool palette instead of a hardcoded whitelist. This includes WebSearch, WebFetch, Agent (subagents), NotebookEdit, and any tools contributed by loaded plugins/MCP servers.

### Phase 2 (Fallback): Explicit Plugin Discovery

If testing reveals that `setting_sources` alone does not carry plugins through to the subprocess, add explicit plugin discovery:

1. Add `discover_plugins()` to `config.py` that reads `~/.claude/plugins/installed_plugins.json`
2. Builds `[{"type": "local", "path": "..."}]` entries for each enabled plugin
3. Passes them via `plugins=discover_plugins()` in each `query()` call

This is a fallback, not the primary approach. Implement only if Phase 1 testing shows plugins are not loaded.

### Session Configurations

Three session types, two configurations:

#### Planner + Worker Sessions

```python
ClaudeAgentOptions(
    tools={"type": "preset", "preset": "claude_code"},
    setting_sources=config.setting_sources,
    permission_mode="bypassPermissions",
    env=sdk_env(),
    cwd=...,
    max_turns=...,
    model=...,
)
```

Replaces the current hardcoded `allowed_tools` lists. Remove the `allowed_tools=` kwarg entirely from planner and worker `query()` calls — do not pass it as `[]`, just omit it. The `tools` preset provides the full tool set. Planner keeps its existing `max_turns=30` as a hardcoded literal (no config field for planner turns).

#### Validator Sessions

```python
ClaudeAgentOptions(
    tools={"type": "preset", "preset": "claude_code"},
    disallowed_tools=["Write", "Edit"],
    setting_sources=config.setting_sources,
    permission_mode="bypassPermissions",
    env=sdk_env(),
    cwd=...,
    max_turns=...,
    model=...,
)
```

Full preset but write-protected. The existing `disallowed_tools=["Write", "Edit"]` constraint is preserved.

### Config Changes

`GolemConfig` gets one new field:

```python
@dataclass
class GolemConfig:
    # ... existing fields ...
    setting_sources: list[str] = field(default_factory=lambda: ["user", "project", "local"])  # SDK accepts Literal["user", "project", "local"]
```

Default mimics a normal CC session. Can be overridden via `config.json` for edge cases (e.g., `[]` for a clean session).

`sdk_env()` is unchanged — clearing `ANTHROPIC_API_KEY` is still required regardless of setting sources.

### Phase 2: `discover_plugins()` (Fallback)

```python
from claude_agent_sdk import SdkPluginConfig

def discover_plugins() -> list[SdkPluginConfig]:
    """Read installed plugins from the Claude Code plugin cache.

    Reads Path.home() / ".claude" / "plugins" / "installed_plugins.json".
    Returns list of SdkPluginConfig ({"type": "local", "path": "..."}) for each enabled plugin.
    Returns empty list if file not found or malformed.
    """
```

- Uses `Path.home() / ".claude" / "plugins" / "installed_plugins.json"` (Windows-safe, no `~` expansion)
- Filters to enabled plugins only
- Returns `list[SdkPluginConfig]` with absolute paths to plugin cache directories
- Gracefully returns `[]` on any error (missing file, malformed JSON, missing cache dirs)

## Files Changed

| File | Change |
|------|--------|
| `src/golem/config.py` | Update import to `from dataclasses import asdict, dataclass, field`. Add `setting_sources` field to `GolemConfig`. Add `discover_plugins()` function (Phase 2). |
| `src/golem/worker.py` | Replace `allowed_tools` with `tools` preset. Add `setting_sources` to `ClaudeAgentOptions`. |
| `src/golem/validator.py` | Replace `allowed_tools` with `tools` preset (keep `disallowed_tools`). Add `setting_sources` to `ClaudeAgentOptions`. |
| `src/golem/planner.py` | Replace `allowed_tools` with `tools` preset. Add `setting_sources` to `ClaudeAgentOptions`. |
| `tests/test_config.py` | New file. Test `GolemConfig` defaults, `load_config` override, `discover_plugins()`. |

## Files NOT Changed

| File | Reason |
|------|--------|
| `src/golem/executor.py` | Passes config through, no SDK options changes needed |
| `src/golem/tasks.py` | Unrelated to SDK session configuration |
| `src/golem/tui.py` | No UI changes required; `setting_sources` configurable via `config.json` |
| `src/golem/cli.py` | No changes needed |
| `src/golem/progress.py` | Unrelated |
| `src/golem/worktree.py` | Unrelated |

## Testing

### Unit Tests

**`tests/test_config.py` (new):**

Phase 1 tests (always implement):
- `test_default_setting_sources` — verify `GolemConfig().setting_sources == ["user", "project", "local"]`
- `test_load_config_overrides_setting_sources` — verify `load_config` respects `setting_sources` from `config.json`

Phase 2 tests (implement only if Phase 2 is triggered):
- `test_discover_plugins_reads_installed` — verify `discover_plugins()` reads `installed_plugins.json` and returns `list[SdkPluginConfig]`
- `test_discover_plugins_missing_file` — verify returns `[]` when file doesn't exist
- `test_discover_plugins_malformed_json` — verify returns `[]` on malformed input

**Existing tests:**
- No changes needed. `test_executor.py` mocks at function boundaries (`run_worker`, `run_validation`), not SDK options. `test_validator.py` mocks `query()` directly. Neither is affected by the options change.

### Manual Smoke Test

1. Run `golem plan` on a spec — verify planner output references web research (confirms WebSearch/WebFetch access)
2. Run `golem run` on a frontend spec — verify worker output shows skill-driven patterns (confirms plugin loading)
3. Run with `"setting_sources": []` in `config.json` — verify sessions run clean without plugins (confirms override works)
4. If Phase 1 fails to load plugins, implement Phase 2 and re-test

## Verification Criteria

### Phase 1 (Required)

- [ ] All three session types (planner, worker, validator) use `setting_sources=config.setting_sources`
- [ ] Planner and worker sessions use `tools={"type": "preset", "preset": "claude_code"}` with no `allowed_tools` kwarg
- [ ] Validator sessions use `tools` preset with `disallowed_tools=["Write", "Edit"]` and no `allowed_tools` kwarg
- [ ] `GolemConfig.setting_sources` defaults to `["user", "project", "local"]` and is configurable via `config.json`
- [ ] Planner keeps `max_turns=30` as a hardcoded literal
- [ ] `sdk_env()` still clears `ANTHROPIC_API_KEY`
- [ ] Existing tests pass without modification
- [ ] New `test_config.py` Phase 1 tests pass (`test_default_setting_sources`, `test_load_config_overrides_setting_sources`)

### Phase 2 (Implement only if Phase 1 testing shows plugins are not loaded)

- [ ] `discover_plugins()` returns `list[SdkPluginConfig]` and handles missing/malformed input gracefully
- [ ] Plugin paths use `Path.home()` not `~` string expansion
- [ ] Phase 2 tests pass (`test_discover_plugins_reads_installed`, `test_discover_plugins_missing_file`, `test_discover_plugins_malformed_json`)
