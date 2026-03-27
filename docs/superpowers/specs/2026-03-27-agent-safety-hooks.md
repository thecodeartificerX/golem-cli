# Agent Safety Hooks

## Problem

Golem's SDK agent sessions (Lead Architect, Tech Lead, Junior Devs) run headless with `bypassPermissions`. The only thing stopping them from running destructive commands is prompt instructions like "don't run golem clean." Prompt instructions are suggestions — agents can rationalize around them, especially after many turns when system prompt attention degrades.

In Golem's first self-build run, the Tech Lead ran `golem clean` mid-session, which destroyed `.golem/progress.log` and caused the run to exit with code 1. This is the exact failure mode that architectural enforcement prevents.

ZeroShot solves this with Python PreToolUse hooks gated by environment variables. The hooks structurally deny dangerous tool calls before the agent ever sees them. We adopt the same pattern.

## Design

### Architecture: Direct hooks in project settings.json

We use direct PreToolUse hook entries in the project's `.claude/settings.json`, NOT hookify. Hookify requires itself to be enabled in `enabledPlugins` and uses a relative glob for rule files. Our SDK sessions use `setting_sources=["project"]`, so they load the project's `.claude/settings.json` automatically — no plugin activation needed.

### Gating: GOLEM_SDK_SESSION env var

All hook scripts check `GOLEM_SDK_SESSION=1` first and pass through silently if not set. This ensures hooks don't interfere with interactive Claude Code development sessions on the golem project itself. Only SDK sessions spawned by Golem set this env var.

### Three hook scripts

1. **block-golem-cli.py** — Blocks `Bash` tool calls that run golem CLI commands: `golem clean`, `golem reset-ticket`, `golem export`, `golem status`, `golem run`. Agents are subprocesses — running these destroys their own runtime state.

2. **block-ask-user-question.py** — Blocks `AskUserQuestion` tool entirely. SDK sessions are headless — there is no user to respond. The deny message instructs the agent to make autonomous decisions.

3. **block-dangerous-git.py** — Blocks destructive git operations in `Bash` tool calls: `git stash`, `git checkout -- .`, `git checkout -f`, `git reset --hard`, `git push --force`, `git push -f`, `git clean -f`, `git branch -D`, `git rebase -i`, `git add -i`, `git add -p`. These can corrupt worktree state or destroy uncommitted work.

### Hook output format

PreToolUse hooks communicate deny decisions via JSON on stdout:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "Explanation of why this was blocked and what to do instead"
  }
}
```

A hook that approves (or doesn't apply) exits with code 0 and no stdout.

## Implementation

---

### Task 1: Add GOLEM_SDK_SESSION to sdk_env()

**Files:**
- Modify: `src/golem/config.py`

- [ ] **Step 1: Update sdk_env() return value**

  In `src/golem/config.py`, find the `sdk_env()` function and add `"GOLEM_SDK_SESSION": "1"` to the returned dict:

  ```python
  def sdk_env() -> dict[str, str]:
      return {
          "ANTHROPIC_API_KEY": "",
          "CLAUDECODE": "",
          "GOLEM_SDK_SESSION": "1",
      }
  ```

- [ ] **Step 2: Commit**

  ```bash
  git add src/golem/config.py
  git commit -m "feat: add GOLEM_SDK_SESSION to sdk_env() for hook gating"
  ```

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. sdk_env() returns GOLEM_SDK_SESSION=1
python -c "
import sys; sys.path.insert(0, 'src')
from golem.config import sdk_env
env = sdk_env()
assert env.get('GOLEM_SDK_SESSION') == '1', f'GOLEM_SDK_SESSION missing or wrong: {env}'
print('SDK_ENV: PASS')
" || echo "SDK_ENV: FAIL"
```

Expected output:
```
SDK_ENV: PASS
```

If any check fails, fix the issue and re-run ALL checks before proceeding.

---

### Task 2: Update .claude/settings.json with PreToolUse hooks

**Files:**
- Modify: `.claude/settings.json`

- [ ] **Step 1: Replace settings.json content**

  Replace the current content of `.claude/settings.json` with:

  ```json
  {
    "enabledPlugins": {},
    "hooks": {
      "PreToolUse": [
        {
          "matcher": "Bash",
          "hooks": [
            {
              "type": "command",
              "command": "python .claude/hooks/block-golem-cli.py",
              "timeout": 5
            }
          ]
        },
        {
          "matcher": "Bash",
          "hooks": [
            {
              "type": "command",
              "command": "python .claude/hooks/block-dangerous-git.py",
              "timeout": 5
            }
          ]
        },
        {
          "matcher": "AskUserQuestion",
          "hooks": [
            {
              "type": "command",
              "command": "python .claude/hooks/block-ask-user-question.py",
              "timeout": 5
            }
          ]
        }
      ]
    }
  }
  ```

  The `matcher` field pre-filters by tool name so git hooks don't fire on non-Bash tool calls.

- [ ] **Step 2: Commit**

  ```bash
  git add .claude/settings.json
  git commit -m "feat: add PreToolUse safety hooks to .claude/settings.json"
  ```

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. settings.json is valid JSON
python -c "
import json
with open('.claude/settings.json', encoding='utf-8') as f:
    data = json.load(f)
print('SETTINGS_JSON: PASS')
" || echo "SETTINGS_JSON: FAIL"

# 2. PreToolUse hooks present
python -c "
import json
with open('.claude/settings.json', encoding='utf-8') as f:
    data = json.load(f)
hooks = data.get('hooks', {}).get('PreToolUse', [])
assert len(hooks) == 3, f'Expected 3 PreToolUse entries, got {len(hooks)}'
matchers = [h['matcher'] for h in hooks]
assert 'Bash' in matchers, 'Bash matcher missing'
assert 'AskUserQuestion' in matchers, 'AskUserQuestion matcher missing'
cmds = [h['hooks'][0]['command'] for h in hooks]
assert any('block-golem-cli' in c for c in cmds), 'block-golem-cli hook missing'
assert any('block-dangerous-git' in c for c in cmds), 'block-dangerous-git hook missing'
assert any('block-ask-user-question' in c for c in cmds), 'block-ask-user-question hook missing'
print('SETTINGS_HOOKS: PASS')
" || echo "SETTINGS_HOOKS: FAIL"
```

Expected output:
```
SETTINGS_JSON: PASS
SETTINGS_HOOKS: PASS
```

If any check fails, fix the issue and re-run ALL checks before proceeding.

---

### Task 3: Create hook script — block-golem-cli.py

**Files:**
- Create: `.claude/hooks/block-golem-cli.py`

- [ ] **Step 1: Create the hooks directory if it doesn't exist**

  ```bash
  mkdir -p .claude/hooks
  ```

- [ ] **Step 2: Write block-golem-cli.py**

  Create `.claude/hooks/block-golem-cli.py`:

  ```python
  #!/usr/bin/env python3
  """Block golem CLI commands in SDK sessions."""
  import json, os, re, sys

  if os.environ.get("GOLEM_SDK_SESSION") != "1":
      sys.exit(0)

  hook_input = json.loads(sys.stdin.read())
  tool_name = hook_input.get("tool_name", "")
  tool_input = hook_input.get("tool_input", {})

  if tool_name != "Bash":
      sys.exit(0)

  command = tool_input.get("command", "")
  BLOCKED_PATTERNS = [
      r"\bgolem\s+(clean|reset-ticket|export|status|run|resume|ui|doctor)\b",
      r"\buv\s+run\s+golem\s+(clean|reset-ticket|export|status|run|resume|ui|doctor)\b",
  ]

  for pattern in BLOCKED_PATTERNS:
      if re.search(pattern, command):
          result = {
              "hookSpecificOutput": {
                  "hookEventName": "PreToolUse",
                  "permissionDecision": "deny",
                  "permissionDecisionReason": (
                      f"BLOCKED: '{command}' — You are a subprocess of Golem. "
                      "Running golem CLI commands destroys the runtime state you depend on. "
                      "Use your MCP tools for ticket/worktree operations instead. "
                      "NO BYPASS EXISTS."
                  ),
              }
          }
          print(json.dumps(result))
          sys.exit(0)

  sys.exit(0)
  ```

- [ ] **Step 3: Commit**

  ```bash
  git add .claude/hooks/block-golem-cli.py
  git commit -m "feat: add block-golem-cli.py PreToolUse hook"
  ```

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. File exists and is non-empty
test -s .claude/hooks/block-golem-cli.py && echo "FILE_EXISTS: PASS" || echo "FILE_EXISTS: FAIL"

# 2. Hook blocks golem clean when GOLEM_SDK_SESSION=1
python -c "
import subprocess, json, os
env = {**os.environ, 'GOLEM_SDK_SESSION': '1'}
stdin = json.dumps({'tool_name': 'Bash', 'tool_input': {'command': 'uv run golem clean'}})
r = subprocess.run(['python', '.claude/hooks/block-golem-cli.py'], input=stdin, capture_output=True, text=True, env=env)
out = json.loads(r.stdout)
assert out['hookSpecificOutput']['permissionDecision'] == 'deny', f'Expected deny, got: {out}'
print('BLOCKS_CLEAN: PASS')
" || echo "BLOCKS_CLEAN: FAIL"

# 3. Hook allows normal bash commands when GOLEM_SDK_SESSION=1
python -c "
import subprocess, json, os
env = {**os.environ, 'GOLEM_SDK_SESSION': '1'}
stdin = json.dumps({'tool_name': 'Bash', 'tool_input': {'command': 'ruff check .'}})
r = subprocess.run(['python', '.claude/hooks/block-golem-cli.py'], input=stdin, capture_output=True, text=True, env=env)
assert r.stdout.strip() == '', f'Expected empty stdout, got: {r.stdout!r}'
print('ALLOWS_RUFF: PASS')
" || echo "ALLOWS_RUFF: FAIL"

# 4. Hook passes through when GOLEM_SDK_SESSION not set
python -c "
import subprocess, json, os
env = {k: v for k, v in os.environ.items() if k != 'GOLEM_SDK_SESSION'}
stdin = json.dumps({'tool_name': 'Bash', 'tool_input': {'command': 'uv run golem clean'}})
r = subprocess.run(['python', '.claude/hooks/block-golem-cli.py'], input=stdin, capture_output=True, text=True, env=env)
assert r.stdout.strip() == '', f'Expected empty stdout without env var, got: {r.stdout!r}'
print('PASSTHROUGH_NO_ENV: PASS')
" || echo "PASSTHROUGH_NO_ENV: FAIL"
```

Expected output:
```
FILE_EXISTS: PASS
BLOCKS_CLEAN: PASS
ALLOWS_RUFF: PASS
PASSTHROUGH_NO_ENV: PASS
```

If any check fails, fix the issue and re-run ALL checks before proceeding.

---

### Task 4: Create hook script — block-ask-user-question.py

**Files:**
- Create: `.claude/hooks/block-ask-user-question.py`

- [ ] **Step 1: Write block-ask-user-question.py**

  Create `.claude/hooks/block-ask-user-question.py`:

  ```python
  #!/usr/bin/env python3
  """Block AskUserQuestion in headless SDK sessions."""
  import json, os, sys

  if os.environ.get("GOLEM_SDK_SESSION") != "1":
      sys.exit(0)

  hook_input = json.loads(sys.stdin.read())
  tool_name = hook_input.get("tool_name", "")

  if tool_name != "AskUserQuestion":
      sys.exit(0)

  result = {
      "hookSpecificOutput": {
          "hookEventName": "PreToolUse",
          "permissionDecision": "deny",
          "permissionDecisionReason": (
              "BLOCKED: AskUserQuestion — You are running in a headless SDK session. "
              "There is no user to respond. Make autonomous decisions. "
              "If unsure, choose the option that maintains code quality and correctness. "
              "If blocked, update your ticket to needs_work with details."
          ),
      }
  }
  print(json.dumps(result))
  sys.exit(0)
  ```

- [ ] **Step 2: Commit**

  ```bash
  git add .claude/hooks/block-ask-user-question.py
  git commit -m "feat: add block-ask-user-question.py PreToolUse hook"
  ```

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. File exists and is non-empty
test -s .claude/hooks/block-ask-user-question.py && echo "FILE_EXISTS: PASS" || echo "FILE_EXISTS: FAIL"

# 2. Hook blocks AskUserQuestion when GOLEM_SDK_SESSION=1
python -c "
import subprocess, json, os
env = {**os.environ, 'GOLEM_SDK_SESSION': '1'}
stdin = json.dumps({'tool_name': 'AskUserQuestion', 'tool_input': {'question': 'What should I do?'}})
r = subprocess.run(['python', '.claude/hooks/block-ask-user-question.py'], input=stdin, capture_output=True, text=True, env=env)
out = json.loads(r.stdout)
assert out['hookSpecificOutput']['permissionDecision'] == 'deny', f'Expected deny, got: {out}'
print('BLOCKS_ASK: PASS')
" || echo "BLOCKS_ASK: FAIL"

# 3. Hook passes through when GOLEM_SDK_SESSION not set
python -c "
import subprocess, json, os
env = {k: v for k, v in os.environ.items() if k != 'GOLEM_SDK_SESSION'}
stdin = json.dumps({'tool_name': 'AskUserQuestion', 'tool_input': {'question': 'What?'}})
r = subprocess.run(['python', '.claude/hooks/block-ask-user-question.py'], input=stdin, capture_output=True, text=True, env=env)
assert r.stdout.strip() == '', f'Expected empty stdout without env var, got: {r.stdout!r}'
print('PASSTHROUGH_NO_ENV: PASS')
" || echo "PASSTHROUGH_NO_ENV: FAIL"
```

Expected output:
```
FILE_EXISTS: PASS
BLOCKS_ASK: PASS
PASSTHROUGH_NO_ENV: PASS
```

If any check fails, fix the issue and re-run ALL checks before proceeding.

---

### Task 5: Create hook script — block-dangerous-git.py

**Files:**
- Create: `.claude/hooks/block-dangerous-git.py`

- [ ] **Step 1: Write block-dangerous-git.py**

  Create `.claude/hooks/block-dangerous-git.py`:

  ```python
  #!/usr/bin/env python3
  """Block destructive git operations in SDK sessions."""
  import json, os, re, sys

  if os.environ.get("GOLEM_SDK_SESSION") != "1":
      sys.exit(0)

  hook_input = json.loads(sys.stdin.read())
  tool_name = hook_input.get("tool_name", "")
  tool_input = hook_input.get("tool_input", {})

  if tool_name != "Bash":
      sys.exit(0)

  command = tool_input.get("command", "")
  DANGEROUS_PATTERNS = [
      (r"git\s+stash", "Use git worktree operations instead of stash"),
      (r"git\s+checkout\s+--\s", "Do not discard uncommitted changes"),
      (r"git\s+checkout\s+-f", "Do not force-checkout"),
      (r"git\s+checkout\s+\.", "Do not discard all changes"),
      (r"git\s+reset\s+--hard", "Do not hard-reset — changes will be lost"),
      (r"git\s+push\s+--force", "Do not force-push — use --force-with-lease if needed"),
      (r"git\s+push\s+-f\b", "Do not force-push"),
      (r"git\s+clean\s+-f", "Do not clean untracked files"),
      (r"git\s+branch\s+-D\s", "Use -d (safe delete) instead of -D (force delete)"),
      (r"git\s+rebase\s+-i", "Interactive rebase requires user input — not available in headless mode"),
      (r"git\s+add\s+-i", "Interactive add requires user input — not available in headless mode"),
      (r"git\s+add\s+-p", "Patch mode requires user input — not available in headless mode"),
  ]

  for pattern, reason in DANGEROUS_PATTERNS:
      if re.search(pattern, command):
          result = {
              "hookSpecificOutput": {
                  "hookEventName": "PreToolUse",
                  "permissionDecision": "deny",
                  "permissionDecisionReason": (
                      f"[GIT-SAFE BLOCKED] {reason}. "
                      f"Attempted command: {command}. "
                      "NO BYPASS EXISTS."
                  ),
              }
          }
          print(json.dumps(result))
          sys.exit(0)

  sys.exit(0)
  ```

- [ ] **Step 2: Commit**

  ```bash
  git add .claude/hooks/block-dangerous-git.py
  git commit -m "feat: add block-dangerous-git.py PreToolUse hook"
  ```

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. File exists and is non-empty
test -s .claude/hooks/block-dangerous-git.py && echo "FILE_EXISTS: PASS" || echo "FILE_EXISTS: FAIL"

# 2. Hook blocks git stash
python -c "
import subprocess, json, os
env = {**os.environ, 'GOLEM_SDK_SESSION': '1'}
stdin = json.dumps({'tool_name': 'Bash', 'tool_input': {'command': 'git stash'}})
r = subprocess.run(['python', '.claude/hooks/block-dangerous-git.py'], input=stdin, capture_output=True, text=True, env=env)
out = json.loads(r.stdout)
assert out['hookSpecificOutput']['permissionDecision'] == 'deny', f'Expected deny: {out}'
print('BLOCKS_STASH: PASS')
" || echo "BLOCKS_STASH: FAIL"

# 3. Hook blocks git reset --hard
python -c "
import subprocess, json, os
env = {**os.environ, 'GOLEM_SDK_SESSION': '1'}
stdin = json.dumps({'tool_name': 'Bash', 'tool_input': {'command': 'git reset --hard HEAD~1'}})
r = subprocess.run(['python', '.claude/hooks/block-dangerous-git.py'], input=stdin, capture_output=True, text=True, env=env)
out = json.loads(r.stdout)
assert out['hookSpecificOutput']['permissionDecision'] == 'deny', f'Expected deny: {out}'
print('BLOCKS_RESET_HARD: PASS')
" || echo "BLOCKS_RESET_HARD: FAIL"

# 4. Hook blocks git push --force
python -c "
import subprocess, json, os
env = {**os.environ, 'GOLEM_SDK_SESSION': '1'}
stdin = json.dumps({'tool_name': 'Bash', 'tool_input': {'command': 'git push --force origin main'}})
r = subprocess.run(['python', '.claude/hooks/block-dangerous-git.py'], input=stdin, capture_output=True, text=True, env=env)
out = json.loads(r.stdout)
assert out['hookSpecificOutput']['permissionDecision'] == 'deny', f'Expected deny: {out}'
print('BLOCKS_FORCE_PUSH: PASS')
" || echo "BLOCKS_FORCE_PUSH: FAIL"

# 5. Hook allows safe git operations (git add, commit, push)
python -c "
import subprocess, json, os
env = {**os.environ, 'GOLEM_SDK_SESSION': '1'}
for cmd in ['git add .', 'git commit -m test', 'git push', 'git branch -d feat']:
    stdin = json.dumps({'tool_name': 'Bash', 'tool_input': {'command': cmd}})
    r = subprocess.run(['python', '.claude/hooks/block-dangerous-git.py'], input=stdin, capture_output=True, text=True, env=env)
    assert r.stdout.strip() == '', f'Expected pass-through for {cmd!r}, got: {r.stdout!r}'
print('ALLOWS_SAFE_OPS: PASS')
" || echo "ALLOWS_SAFE_OPS: FAIL"

# 6. Hook passes through when GOLEM_SDK_SESSION not set
python -c "
import subprocess, json, os
env = {k: v for k, v in os.environ.items() if k != 'GOLEM_SDK_SESSION'}
stdin = json.dumps({'tool_name': 'Bash', 'tool_input': {'command': 'git reset --hard HEAD'}})
r = subprocess.run(['python', '.claude/hooks/block-dangerous-git.py'], input=stdin, capture_output=True, text=True, env=env)
assert r.stdout.strip() == '', f'Expected empty stdout without env var, got: {r.stdout!r}'
print('PASSTHROUGH_NO_ENV: PASS')
" || echo "PASSTHROUGH_NO_ENV: FAIL"
```

Expected output:
```
FILE_EXISTS: PASS
BLOCKS_STASH: PASS
BLOCKS_RESET_HARD: PASS
BLOCKS_FORCE_PUSH: PASS
ALLOWS_SAFE_OPS: PASS
PASSTHROUGH_NO_ENV: PASS
```

If any check fails, fix the issue and re-run ALL checks before proceeding.

---

### Task 6: Write tests — tests/test_hooks.py

**Files:**
- Create: `tests/test_hooks.py`

- [ ] **Step 1: Write test_hooks.py**

  Create `tests/test_hooks.py` using the pattern below. Each test invokes the hook script as a subprocess with mock stdin data.

  ### Test pattern

  ```python
  import subprocess, json

  def run_hook(script_path, tool_name, tool_input=None, env_override=None):
      env = {**os.environ, "GOLEM_SDK_SESSION": "1"}
      if env_override:
          env.update(env_override)
      stdin_data = json.dumps({"tool_name": tool_name, "tool_input": tool_input or {}})
      result = subprocess.run(
          ["python", str(script_path)],
          input=stdin_data, capture_output=True, text=True, env=env,
      )
      if result.stdout.strip():
          return json.loads(result.stdout)
      return None
  ```

  Tests to implement:

  - `test_block_golem_cli_blocks_clean` — Set `GOLEM_SDK_SESSION=1`, pipe `{"tool_name": "Bash", "tool_input": {"command": "uv run golem clean"}}`, assert stdout contains `permissionDecision: deny`
  - `test_block_golem_cli_allows_normal_bash` — Same env, pipe `{"tool_name": "Bash", "tool_input": {"command": "ruff check ."}}`, assert empty stdout (pass-through)
  - `test_block_golem_cli_passthrough_without_env` — No env var, pipe golem clean command, assert empty stdout
  - `test_block_ask_user_question_blocks` — Set env, pipe `{"tool_name": "AskUserQuestion"}`, assert deny
  - `test_block_ask_user_question_passthrough_without_env` — No env, assert pass-through
  - `test_block_dangerous_git_blocks_stash` — Set env, pipe `git stash`, assert deny
  - `test_block_dangerous_git_blocks_reset_hard` — Set env, pipe `git reset --hard`, assert deny
  - `test_block_dangerous_git_blocks_force_push` — Set env, pipe `git push --force`, assert deny
  - `test_block_dangerous_git_allows_safe_ops` — Set env, pipe `git add .`, `git commit -m "test"`, `git push`, assert pass-through
  - `test_block_dangerous_git_allows_branch_d_lowercase` — Set env, pipe `git branch -d feat`, assert pass-through (safe delete)
  - `test_sdk_env_includes_golem_session` — Import `sdk_env` from config, assert `"GOLEM_SDK_SESSION"` is `"1"`

- [ ] **Step 2: Commit**

  ```bash
  git add tests/test_hooks.py
  git commit -m "test: add test_hooks.py for PreToolUse hook scripts"
  ```

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. Test file exists and is non-empty
test -s tests/test_hooks.py && echo "FILE_EXISTS: PASS" || echo "FILE_EXISTS: FAIL"

# 2. All 11 hook tests pass
uv run pytest tests/test_hooks.py -v 2>&1 | tail -5
uv run pytest tests/test_hooks.py --tb=short -q 2>&1 | grep -E "passed|failed|error" | python -c "
import sys
line = sys.stdin.read().strip()
if 'failed' in line or 'error' in line:
    print(f'HOOK_TESTS: FAIL ({line})')
elif '11 passed' in line:
    print('HOOK_TESTS: PASS')
else:
    print(f'HOOK_TESTS: FAIL (unexpected: {line!r})')
" || echo "HOOK_TESTS: FAIL"
```

Expected output:
```
FILE_EXISTS: PASS
HOOK_TESTS: PASS
```

If any check fails, fix the issue and re-run ALL checks before proceeding.

---

### Task 7: Verify no regressions in existing test suite

**Files:** (no changes — verification only)

- [ ] **Step 1: Run full test suite**

  ```bash
  uv run pytest --tb=short -q
  ```

  All existing tests must pass. No new failures are acceptable.

- [ ] **Step 2: Commit if any minor fixes were needed**

  Only commit if you made fixes to address regressions. Otherwise skip.

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. Full test suite passes (250+ tests expected: 239 prior + 11 new)
uv run pytest --tb=short -q 2>&1 | tail -3
uv run pytest --tb=short -q 2>&1 | grep -E "^\d+ passed" | python -c "
import sys, re
line = sys.stdin.read().strip()
m = re.search(r'(\d+) passed', line)
if not m:
    print(f'FULL_SUITE: FAIL (no passed count: {line!r})')
    sys.exit(1)
count = int(m.group(1))
if count < 250:
    print(f'FULL_SUITE: FAIL (only {count} passed, expected >= 250)')
elif 'failed' in line or 'error' in line:
    print(f'FULL_SUITE: FAIL ({line})')
else:
    print(f'FULL_SUITE: PASS ({count} tests)')
" || echo "FULL_SUITE: FAIL"
```

Expected output:
```
FULL_SUITE: PASS (250 tests)
```

If any check fails, fix the issue and re-run ALL checks before proceeding.

---

## Phase Completion Gate

Run this after all tasks are complete to verify the entire feature end-to-end.

```bash
cd F:/Tools/Projects/golem-cli

echo "=== Phase Completion Gate: Agent Safety Hooks ==="

# 1. sdk_env() includes GOLEM_SDK_SESSION=1
python -c "
import sys; sys.path.insert(0, 'src')
from golem.config import sdk_env
env = sdk_env()
assert env.get('GOLEM_SDK_SESSION') == '1', f'Missing: {env}'
print('TASK1_SDK_ENV: PASS')
" || echo "TASK1_SDK_ENV: FAIL"

# 2. settings.json has 3 PreToolUse hooks
python -c "
import json
with open('.claude/settings.json', encoding='utf-8') as f:
    data = json.load(f)
hooks = data.get('hooks', {}).get('PreToolUse', [])
assert len(hooks) == 3, f'Expected 3, got {len(hooks)}'
matchers = [h['matcher'] for h in hooks]
assert 'Bash' in matchers
assert 'AskUserQuestion' in matchers
print('TASK2_SETTINGS: PASS')
" || echo "TASK2_SETTINGS: FAIL"

# 3. All 3 hook scripts exist
test -s .claude/hooks/block-golem-cli.py && echo "TASK3_HOOK_CLI: PASS" || echo "TASK3_HOOK_CLI: FAIL"
test -s .claude/hooks/block-ask-user-question.py && echo "TASK4_HOOK_ASK: PASS" || echo "TASK4_HOOK_ASK: FAIL"
test -s .claude/hooks/block-dangerous-git.py && echo "TASK5_HOOK_GIT: PASS" || echo "TASK5_HOOK_GIT: FAIL"

# 4. block-golem-cli.py blocks golem clean, allows ruff
python -c "
import subprocess, json, os
env = {**os.environ, 'GOLEM_SDK_SESSION': '1'}
# Block check
r = subprocess.run(['python', '.claude/hooks/block-golem-cli.py'], input=json.dumps({'tool_name':'Bash','tool_input':{'command':'uv run golem clean'}}), capture_output=True, text=True, env=env)
out = json.loads(r.stdout)
assert out['hookSpecificOutput']['permissionDecision'] == 'deny'
# Allow check
r2 = subprocess.run(['python', '.claude/hooks/block-golem-cli.py'], input=json.dumps({'tool_name':'Bash','tool_input':{'command':'ruff check .'}}), capture_output=True, text=True, env=env)
assert r2.stdout.strip() == ''
print('TASK3_HOOK_CLI_BEHAVIOR: PASS')
" || echo "TASK3_HOOK_CLI_BEHAVIOR: FAIL"

# 5. block-ask-user-question.py blocks AskUserQuestion
python -c "
import subprocess, json, os
env = {**os.environ, 'GOLEM_SDK_SESSION': '1'}
r = subprocess.run(['python', '.claude/hooks/block-ask-user-question.py'], input=json.dumps({'tool_name':'AskUserQuestion','tool_input':{}}), capture_output=True, text=True, env=env)
out = json.loads(r.stdout)
assert out['hookSpecificOutput']['permissionDecision'] == 'deny'
print('TASK4_HOOK_ASK_BEHAVIOR: PASS')
" || echo "TASK4_HOOK_ASK_BEHAVIOR: FAIL"

# 6. block-dangerous-git.py blocks reset --hard, allows git add
python -c "
import subprocess, json, os
env = {**os.environ, 'GOLEM_SDK_SESSION': '1'}
r = subprocess.run(['python', '.claude/hooks/block-dangerous-git.py'], input=json.dumps({'tool_name':'Bash','tool_input':{'command':'git reset --hard'}}), capture_output=True, text=True, env=env)
out = json.loads(r.stdout)
assert out['hookSpecificOutput']['permissionDecision'] == 'deny'
r2 = subprocess.run(['python', '.claude/hooks/block-dangerous-git.py'], input=json.dumps({'tool_name':'Bash','tool_input':{'command':'git add .'}}), capture_output=True, text=True, env=env)
assert r2.stdout.strip() == ''
print('TASK5_HOOK_GIT_BEHAVIOR: PASS')
" || echo "TASK5_HOOK_GIT_BEHAVIOR: FAIL"

# 7. Hook tests pass
uv run pytest tests/test_hooks.py -q 2>&1 | grep -E "passed|failed|error" | python -c "
import sys
line = sys.stdin.read().strip()
if 'failed' in line or 'error' in line:
    print(f'TASK6_HOOK_TESTS: FAIL ({line})')
elif 'passed' in line:
    print(f'TASK6_HOOK_TESTS: PASS ({line})')
else:
    print(f'TASK6_HOOK_TESTS: FAIL (unexpected: {line!r})')
" || echo "TASK6_HOOK_TESTS: FAIL"

# 8. Full suite passes with no regressions
uv run pytest --tb=short -q 2>&1 | grep -E "^\d+ passed" | python -c "
import sys, re
line = sys.stdin.read().strip()
m = re.search(r'(\d+) passed', line)
if m and int(m.group(1)) >= 250 and 'failed' not in line:
    print(f'TASK7_FULL_SUITE: PASS ({m.group(1)} tests)')
else:
    print(f'TASK7_FULL_SUITE: FAIL ({line!r})')
" || echo "TASK7_FULL_SUITE: FAIL"

echo "=== End Phase Completion Gate ==="
```

Expected output:
```
=== Phase Completion Gate: Agent Safety Hooks ===
TASK1_SDK_ENV: PASS
TASK2_SETTINGS: PASS
TASK3_HOOK_CLI: PASS
TASK4_HOOK_ASK: PASS
TASK5_HOOK_GIT: PASS
TASK3_HOOK_CLI_BEHAVIOR: PASS
TASK4_HOOK_ASK_BEHAVIOR: PASS
TASK5_HOOK_GIT_BEHAVIOR: PASS
TASK6_HOOK_TESTS: PASS
TASK7_FULL_SUITE: PASS (250 tests)
=== End Phase Completion Gate ===
```

If any check fails, fix the issue and re-run ALL checks before proceeding.

## Acceptance Criteria

- [ ] `sdk_env()` returns `GOLEM_SDK_SESSION=1`
- [ ] `.claude/settings.json` has PreToolUse hooks for Bash and AskUserQuestion matchers
- [ ] All 3 hook scripts exist in `.claude/hooks/` and are executable
- [ ] `golem clean` is blocked in SDK sessions (hook returns deny)
- [ ] `golem reset-ticket` is blocked in SDK sessions
- [ ] `AskUserQuestion` is blocked in SDK sessions
- [ ] `git reset --hard` is blocked in SDK sessions
- [ ] `git push --force` is blocked in SDK sessions
- [ ] Normal bash commands (ruff, pytest) are NOT blocked
- [ ] Safe git operations (git add, git commit, git push) are NOT blocked
- [ ] Interactive Claude Code sessions (no GOLEM_SDK_SESSION) are NOT affected
- [ ] All hook tests pass
- [ ] Existing test suite passes (no regressions)
