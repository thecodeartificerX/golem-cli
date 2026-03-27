# External Tools Integration — MCP, Plugins, and Skills for Golem Agents

**Date:** 2026-03-27
**Status:** Approved design, pending implementation

## Problem

Golem SDK sessions run in complete isolation — no external MCP servers, no plugins, no skills. The `setting_sources` config field exists in `GolemConfig` but is never wired into `ClaudeAgentOptions` (dead config). Agents are limited to golem's in-process MCP tools and the base Claude Code toolset (Read, Write, Edit, Glob, Grep, Bash, WebSearch, WebFetch, Agent).

This means:
- Planner researchers do raw web searches instead of using structured doc tools like Context7
- Writers can't use skills like frontend-design or feature-dev
- No project-specific tool inheritance — a React project and a Python CLI project get identical agent capabilities

## Solution

**Project `.claude/` as base, Golem config as override layer.**

Golem agents inherit the target project's Claude Code configuration (plugins, MCPs, skills) via `setting_sources`, and Golem's own config adds per-role overrides for extra MCP servers and setting source customization. A new `golem preflight` command resolves and displays the effective tool ecosystem before a run, with pitfall detection.

## Design

### 1. Config Schema Changes

New fields on `GolemConfig` (in `config.py`):

```python
# Base setting sources — loaded by all agents unless overridden per-role
# Default: ["project"] — inherits target project's .claude/settings.json
setting_sources: list[str] = field(default_factory=lambda: ["project"])

# Per-role setting source overrides
# Keys: "planner", "tech_lead", "writer"
# If a role isn't listed, it falls back to base setting_sources
agent_setting_sources: dict[str, list[str]] = field(default_factory=dict)

# Per-role extra MCP servers (merged with golem's in-process MCP + project MCPs)
# Keys: "planner", "tech_lead", "writer"
# Values: dict of server_name -> MCP server config (stdio/sse/http format)
extra_mcp_servers: dict[str, dict[str, dict[str, object]]] = field(default_factory=dict)
```

Example config.json:

```json
{
  "setting_sources": ["project"],
  "agent_setting_sources": {
    "writer": ["project", "user"]
  },
  "extra_mcp_servers": {
    "planner": {
      "context7": {
        "command": "npx",
        "args": ["-y", "@upstash/context7-mcp@latest"]
      }
    }
  }
}
```

### 2. Resolution Helper

New function in `config.py`:

```python
def resolve_agent_options(
    config: GolemConfig,
    role: str,  # "planner" | "tech_lead" | "writer"
    golem_mcp: McpSdkServerConfig,
    golem_mcp_name: str = "golem",
) -> tuple[list[str], dict[str, Any]]:
    """Return (setting_sources, mcp_servers) for the given agent role."""
    sources = config.agent_setting_sources.get(role) or config.setting_sources
    mcps: dict[str, Any] = {golem_mcp_name: golem_mcp}
    mcps.update(config.extra_mcp_servers.get(role, {}))
    return sources, mcps
```

### 3. SDK Session Wiring

All three agent spawners (`planner.py`, `tech_lead.py`, `writer.py`) must pass the resolved values to `ClaudeAgentOptions`:

```python
sources, mcps = resolve_agent_options(config, "planner", mcp_server)

options = ClaudeAgentOptions(
    model=config.planner_model,
    cwd=str(cwd),
    tools={"type": "preset", "preset": "claude_code"},
    mcp_servers=mcps,
    setting_sources=sources,
    max_turns=50,
    permission_mode="bypassPermissions",
    env=sdk_env(),
)
```

The SDK merges explicit `mcp_servers` (passed via `--mcp-config`) with MCPs loaded from settings files. They coexist — no conflicts unless names collide (explicit wins).

### 4. CLAUDECODE Inheritance Bug Fix

`sdk_env()` in `config.py` must also clear `CLAUDECODE` to prevent "cannot launch inside another Claude Code session" errors when golem is run from within Claude Code:

```python
def sdk_env() -> dict[str, str]:
    return {"ANTHROPIC_API_KEY": "", "CLAUDECODE": ""}
```

### 5. Planner Prompt Update

Add to `prompts/planner.md` Step 3 (Researcher sub-agents):

```markdown
Before spawning researchers that do raw web searches, check if you have
MCP tools available for documentation lookup:

1. Check for `mcp__context7__*` tools first -- if available, use Context7
   to resolve library documentation before falling back to web search.
   Context7 returns structured, up-to-date docs directly.
2. Fall back to `WebSearch` + `WebFetch` only when Context7 doesn't have
   coverage for the library/framework, or when Context7 is not available.

This applies to any documentation MCP -- always prefer structured doc tools
over raw web scraping.
```

No hardcoded tool names — prompts say "check if you have X tools available" so they remain generic if MCPs change.

Tech Lead and Writer prompts are unchanged. Skills are discovered automatically via setting sources — the writer session sees them like any interactive Claude Code session.

### 6. CLI — Dot-Notation Config Set

Extend `golem config set` to support nested fields:

```bash
golem config set agent_setting_sources.writer '["project", "user"]'
golem config set extra_mcp_servers.planner.context7 '{"command": "npx", "args": ["-y", "@upstash/context7-mcp@latest"]}'
golem config set extra_mcp_servers.planner.context7 null  # delete key
```

Implementation:
1. Split key on `.`
2. Traverse/create nested dicts as needed
3. JSON-parse the value (so `'["project", "user"]'` becomes a list)
4. `null` value deletes the key

`golem config show` already dumps full config as JSON — no changes needed.

### 7. Pre-Flight Command

New `golem preflight <spec>` command that resolves the effective tool ecosystem per agent role and detects pitfalls. Integrated into the UI as an "Initialize" step between spec selection and run.

**Output format:**

```
Golem Pre-Flight -- spec.md
Project: F:\Tools\Projects\my-app

Setting Sources
  Base: ["project"]
  Writer override: ["project", "user"]

Planner
  Setting sources: ["project"]
  Golem MCP: create_ticket
  Extra MCPs: context7 (stdio: npx @upstash/context7-mcp)
  Project plugins: (none)

Tech Lead
  Setting sources: ["project"]
  Golem MCP: create_ticket, update_ticket, read_ticket, list_tickets,
             run_qa, create_worktree, merge_branches, commit_worktree
  Extra MCPs: (none)
  Project plugins: (none)

Writer
  Setting sources: ["project", "user"]
  Golem MCP: run_qa, update_ticket
  Extra MCPs: (none)
  Project plugins: frontend-design, feature-dev
  User plugins: superpowers, commit-commands

Pitfalls
  [WARN] claude-mem@sakib-plugins will load for writers via "user" setting source
  [INFO] No .claude/settings.json found in project root

Result: 0 errors -- ready to run
```

**Pitfall detection table:**

| Category | Check | Severity |
|---|---|---|
| Hooks | claude-mem enabled in any role's setting sources | Warning |
| Hooks | SessionEnd or Stop hooks running destructive commands | Warning |
| Hooks | Hook timeouts > 60s (slows agent startup) | Info |
| MCP conflicts | Extra MCP server name collides with golem built-in names (golem, golem-writer, golem-qa) | Error |
| MCP health | Stdio MCP command not found on PATH (e.g., npx missing) | Error |
| Plugin state | Plugin enabled in settings but not installed in cache | Error |
| Plugin state | Plugin enabled at user level but "user" not in role's setting sources (dead config) | Warning |
| SDK | CLAUDECODE env var set (running inside Claude Code) | Error |
| SDK | ANTHROPIC_API_KEY env var set (overrides OAuth) | Warning |
| Project | No .claude/settings.json but setting_sources includes "project" | Info |
| Project | .env files present in project root | Info |
| Git | Dirty working tree (uncommitted changes) | Warning |
| Git | Existing golem/* branches from previous run | Warning |

Errors block the run (unless `--force`). Warnings show but proceed. Info is informational only.

**UI integration:** The web dashboard's control bar flow becomes: Select Spec -> Initialize (pre-flight) -> Review tools -> Run. The pre-flight results display in the dashboard before the user confirms the run.

### 8. Validation

Extend `GolemConfig.validate()`:
- Warn on unknown role names in `agent_setting_sources` and `extra_mcp_servers` (valid: `planner`, `tech_lead`, `writer`)
- Warn if `"user"` is in any setting sources
- Validate MCP server configs have at minimum `command` (stdio) or `url` (sse/http)

Runtime logging at session startup:

```
[PLANNER] setting_sources=["project"], extra_mcps=["context7"]
[TECH LEAD] setting_sources=["project"], extra_mcps=[]
[WRITER] setting_sources=["project", "user"], extra_mcps=[]
```

## Files to Modify

| File | Change |
|---|---|
| `src/golem/config.py` | Add `agent_setting_sources`, `extra_mcp_servers` fields. Add `resolve_agent_options()`. Fix `sdk_env()` to clear `CLAUDECODE`. Extend `validate()`. |
| `src/golem/planner.py` | Wire `resolve_agent_options()` into `ClaudeAgentOptions`. |
| `src/golem/tech_lead.py` | Wire `resolve_agent_options()` into `ClaudeAgentOptions`. |
| `src/golem/writer.py` | Wire `resolve_agent_options()` into `ClaudeAgentOptions`. |
| `src/golem/cli.py` | Add `preflight` command. Extend `config set` for dot-notation. |
| `src/golem/prompts/planner.md` | Add Context7-first doc lookup guidance to Step 3. |
| `src/golem/ui.py` | Add `/api/preflight` endpoint for UI initialize step. |
| `src/golem/ui_template.html` | Add Initialize step to control bar flow. |
| `tests/test_config.py` | Test new fields, validation, `resolve_agent_options()`, dot-notation config set. |
| `tests/test_cli.py` | Test `preflight` command output and pitfall detection. |

## QA Checks

All changes must pass these commands:

```bash
uv run pytest                    # All 239+ tests pass (no regressions)
uv run ruff check src/ tests/    # No new lint errors
uv run python -c "from golem.config import GolemConfig, resolve_agent_options; print('import ok')"  # New function importable
```

## Acceptance Criteria

### Config (Design Section 1-2)
- [ ] `GolemConfig` has `agent_setting_sources: dict[str, list[str]]` field with empty dict default
- [ ] `GolemConfig` has `extra_mcp_servers: dict[str, dict[str, dict[str, object]]]` field with empty dict default
- [ ] `setting_sources` field default remains `["project"]` (already exists, no behavior change to default)
- [ ] `resolve_agent_options()` function exists in `config.py` and returns `(sources, mcp_servers)` tuple
- [ ] `resolve_agent_options()` falls back to base `setting_sources` when role not in `agent_setting_sources`
- [ ] `resolve_agent_options()` merges golem's in-process MCP with role's extra MCPs
- [ ] New config fields round-trip through `save_config()` / `load_config()` correctly
- [ ] New fields are NOT in `_EPHEMERAL_FIELDS` (they must persist to config.json)

### SDK Wiring (Design Section 3-4)
- [ ] `planner.py` passes `setting_sources` and merged `mcp_servers` from `resolve_agent_options("planner", ...)`
- [ ] `tech_lead.py` passes `setting_sources` and merged `mcp_servers` from `resolve_agent_options("tech_lead", ...)`
- [ ] `writer.py` passes `setting_sources` and merged `mcp_servers` from `resolve_agent_options("writer", ...)`
- [ ] `sdk_env()` returns `{"ANTHROPIC_API_KEY": "", "CLAUDECODE": ""}` (bug fix)

### Planner Prompt (Design Section 5)
- [ ] `prompts/planner.md` Step 3 mentions checking for documentation MCP tools before web search
- [ ] No hardcoded tool names — uses "check if available" phrasing

### CLI (Design Section 6)
- [ ] `golem config set` supports dot-notation keys (e.g., `extra_mcp_servers.planner.context7`)
- [ ] Dot-notation traverses/creates nested dicts
- [ ] Values are JSON-parsed (strings, lists, objects all work)
- [ ] Setting a value to `null` deletes the key
- [ ] `golem config show` displays new fields

### Pre-Flight (Design Section 7)
- [ ] `golem preflight <spec>` command exists and runs without error
- [ ] Output shows effective setting sources per role
- [ ] Output shows golem MCP tools per role
- [ ] Output shows extra MCPs per role
- [ ] Output shows project/user plugins per role (reads `.claude/settings.json`)
- [ ] Pitfall detection: warns on MCP name collisions with golem built-ins
- [ ] Pitfall detection: warns on `"user"` in setting sources
- [ ] Pitfall detection: errors on stdio MCP command not found on PATH
- [ ] Pitfall detection: warns on dirty git working tree
- [ ] Pitfall detection: warns on existing `golem/*` branches
- [ ] Errors block with non-zero exit code (unless `--force`)

### Validation (Design Section 8)
- [ ] `GolemConfig.validate()` warns on unknown role names in `agent_setting_sources`
- [ ] `GolemConfig.validate()` warns on unknown role names in `extra_mcp_servers`
- [ ] `GolemConfig.validate()` warns when `"user"` appears in any setting sources
- [ ] `GolemConfig.validate()` validates MCP configs have `command` or `url`

### Tests
- [ ] `test_config.py`: test `resolve_agent_options()` with defaults, with overrides, with extra MCPs
- [ ] `test_config.py`: test new fields in `validate()` — unknown roles, user warning, bad MCP config
- [ ] `test_config.py`: test `load_config()` / `save_config()` round-trip with new fields
- [ ] `test_cli.py`: test `golem preflight` command output format
- [ ] `test_cli.py`: test `golem config set` with dot-notation keys
- [ ] `test_cli.py`: test pitfall detection (MCP name collision, dirty git, etc.)

### UI (Design Section 7 — UI integration)
- [ ] `/api/preflight` endpoint exists and returns JSON preflight results
- [ ] UI template has an Initialize step in the control bar flow

## Non-Goals

- No blocklist/allowlist mechanism for plugins — use setting sources and project config instead
- No claude-mem integration — excluded by design (observation overhead, SessionEnd risk)
- No changes to the ticket system or agent hierarchy
- No new dependencies
