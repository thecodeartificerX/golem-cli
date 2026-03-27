# Dispatch & Integration Hardening

## Problem

Three reliability gaps in Golem's dispatch and integration pipeline:

1. **No dispatch jitter.** When the Tech Lead dispatches 5 Junior Devs simultaneously, all 5 hit `uv sync` in their worktrees at the same time. This causes uv cache lock contention on Windows and can lead to intermittent failures. ZeroShot adds 0-15s random jitter before each validator spawn to stagger I/O.

2. **No PR verification.** The Tech Lead creates a PR via `gh pr create`, trusts the stdout URL, and the run ends. There is no verification that the PR actually exists on GitHub. GitHub's API is eventually consistent — a successful `gh pr create` does not guarantee the PR is immediately queryable. ZeroShot polls `gh pr view` up to 6 times (30s total) to verify.

3. **No iteration-based prompt escalation.** When a Junior Dev's work is rejected and the Tech Lead dispatches a fresh session for rework, the new session gets the exact same generic prompt. There is no signal that this is a rework attempt, no previous failure context, and no escalated urgency. ZeroShot swaps the entire system prompt on subsequent iterations — from a gentle "execute the plan" to a direct "YOU FAILED. FIX IT."

## Design

### Dispatch Jitter

Add `await asyncio.sleep(random.uniform(0, config.dispatch_jitter_max))` before the `query()` call in `spawn_writer_pair()`. Default jitter: 5 seconds (lighter than ZeroShot's 15s — our writers run in separate worktrees so contention is only on the uv cache, not a shared workspace lock).

Skip jitter in tests via `config.dispatch_jitter_max = 0.0` or a `GOLEM_TEST_MODE` env var check.

### PR Verification

Add `verify_pr(pr_url: str, repo_root: Path)` to `worktree.py` that:
1. Extracts the PR number from the URL
2. Runs `gh pr view <number> --json state,url,number`
3. Polls up to 6 times with 5s between attempts (30s total window)
4. Raises `RuntimeError` if the PR doesn't exist or the URL doesn't match

Call this from `create_pr()` after the initial `gh pr create` succeeds.

### Iteration-Based Prompt Escalation

Track rework iterations by counting `needs_work` events in `ticket.history`. Pass the count to `build_writer_prompt()` which:

1. Injects `{iteration}` (1-based) and `{rework_context}` (previous failure notes) into the prompt template
2. Optionally selects a different template on rework (`worker_rework.md`) for stronger framing

The rework context is built from the last 2-3 `needs_work` history events' notes — this gives the Junior Dev specific, targeted feedback about what failed previously.

---

## Implementation

### Task 1: Add `dispatch_jitter_max` Config Field

**Files:**
- Modify: `src/golem/config.py`

- [ ] **Step 1: Add field to `GolemConfig` dataclass**

  In `GolemConfig`, add after `retry_delay`:

  ```python
  dispatch_jitter_max: float = 5.0  # Max seconds of random jitter before writer spawn
  ```

- [ ] **Step 2: Commit**

  ```
  feat: add dispatch_jitter_max config field (default 5.0)
  ```

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. Field exists with correct default
python -c "from golem.config import GolemConfig; c = GolemConfig(); assert c.dispatch_jitter_max == 5.0, f'got {c.dispatch_jitter_max}'; print('FIELD_DEFAULT: PASS')" && echo "FIELD_DEFAULT: PASS" || echo "FIELD_DEFAULT: FAIL"

# 2. Field survives save/load roundtrip
python -c "
import tempfile, json
from pathlib import Path
from golem.config import GolemConfig, save_config, load_config
with tempfile.TemporaryDirectory() as d:
    cfg = GolemConfig(dispatch_jitter_max=3.5)
    save_config(cfg, Path(d))
    loaded = load_config(Path(d))
    assert loaded.dispatch_jitter_max == 3.5, f'got {loaded.dispatch_jitter_max}'
    print('ROUNDTRIP: PASS')
"

# 3. Field is not an ephemeral field (it should be persisted to config.json)
python -c "
import tempfile, json
from pathlib import Path
from golem.config import GolemConfig, save_config
with tempfile.TemporaryDirectory() as d:
    save_config(GolemConfig(dispatch_jitter_max=2.0), Path(d))
    data = json.loads((Path(d) / 'config.json').read_text(encoding='utf-8'))
    assert 'dispatch_jitter_max' in data, 'not in config.json'
    print('PERSISTED: PASS')
"
```

Expected output:
```
FIELD_DEFAULT: PASS
ROUNDTRIP: PASS
PERSISTED: PASS
```

If any check fails, fix the issue and re-run ALL checks before proceeding.

---

### Task 2: Add Dispatch Jitter to `spawn_writer_pair()`

**Files:**
- Modify: `src/golem/writer.py`

- [ ] **Step 1: Add `import random` and `import os` at the top of `writer.py`**

  `asyncio` is already imported. Add `random` and verify `os` is available.

- [ ] **Step 2: Add jitter block in `spawn_writer_pair()` before the retry loop**

  Insert after the `prompt = build_writer_prompt(...)` call and before `result_text = ""`:

  ```python
  import random
  import os

  # Stagger parallel writer spawns to reduce I/O contention on uv cache
  jitter = config.dispatch_jitter_max
  if jitter > 0 and os.environ.get("GOLEM_TEST_MODE") != "1":
      delay = random.uniform(0, jitter)
      print(f"[JUNIOR DEV] {ticket.id}: jitter delay {delay:.1f}s", file=sys.stderr)
      await asyncio.sleep(delay)
  ```

- [ ] **Step 3: Commit**

  ```
  feat: add dispatch jitter to spawn_writer_pair (skip in GOLEM_TEST_MODE=1)
  ```

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. writer.py imports random
python -c "import ast, pathlib; src = pathlib.Path('src/golem/writer.py').read_text(encoding='utf-8'); tree = ast.parse(src); imports = [n.names[0].name if isinstance(n, ast.Import) else n.module for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]; assert 'random' in imports or any('random' in str(s) for s in ast.walk(tree)), 'random not imported'; print('IMPORT_RANDOM: PASS')"

# 2. Jitter code pattern exists in writer.py
python -c "
from pathlib import Path
src = Path('src/golem/writer.py').read_text(encoding='utf-8')
assert 'dispatch_jitter_max' in src, 'dispatch_jitter_max not referenced'
assert 'GOLEM_TEST_MODE' in src, 'GOLEM_TEST_MODE check missing'
assert 'asyncio.sleep' in src, 'asyncio.sleep call missing'
print('JITTER_CODE: PASS')
"

# 3. Jitter is skipped in test mode (GOLEM_TEST_MODE=1)
python -c "
import asyncio, os, sys
os.environ['GOLEM_TEST_MODE'] = '1'
from unittest.mock import patch, AsyncMock, MagicMock
import importlib
import golem.writer
importlib.reload(golem.writer)
slept = []
async def fake_sleep(n): slept.append(n)
async def run():
    ticket = MagicMock()
    ticket.id = 'T-001'
    ticket.history = []
    ticket.context.plan_file = ''
    ticket.context.files = {}
    ticket.context.references = []
    ticket.context.blueprint = ''
    ticket.context.acceptance = []
    ticket.context.qa_checks = []
    ticket.context.parallelism_hints = []
    from golem.config import GolemConfig
    config = GolemConfig(dispatch_jitter_max=10.0)
    async def fake_query(*a, **kw):
        return; yield
    with patch('golem.writer.query', side_effect=fake_query), patch('asyncio.sleep', side_effect=fake_sleep):
        await golem.writer.spawn_writer_pair(ticket, '/tmp', config)
asyncio.run(run())
assert len(slept) == 0, f'sleep called with GOLEM_TEST_MODE=1: {slept}'
print('JITTER_SKIP: PASS')
" 2>/dev/null || echo "JITTER_SKIP: FAIL"
```

Expected output:
```
IMPORT_RANDOM: PASS
JITTER_CODE: PASS
JITTER_SKIP: PASS
```

If any check fails, fix the issue and re-run ALL checks before proceeding.

---

### Task 3: Add `verify_pr()` to `worktree.py`

**Files:**
- Modify: `src/golem/worktree.py`

- [ ] **Step 1: Add `import json` and `import re` at the top of `worktree.py`**

  `subprocess` and `Path` are already imported.

- [ ] **Step 2: Add `verify_pr()` function after `create_pr()`**

  ```python
  def verify_pr(pr_url: str, repo_root: Path, poll_attempts: int = 6, poll_interval: float = 5.0) -> None:
      """Verify a PR exists on GitHub by polling gh pr view.

      Raises RuntimeError if the PR cannot be verified after all attempts.
      GitHub's API is eventually consistent — a successful gh pr create does
      not guarantee the PR is immediately queryable.
      """
      import re
      import time

      match = re.search(r"/pull/(\d+)", pr_url)
      if not match:
          raise RuntimeError(f"Could not extract PR number from URL: {pr_url}")
      pr_number = match.group(1)

      for attempt in range(poll_attempts):
          result = _run(
              ["gh", "pr", "view", pr_number, "--json", "state,url,number"],
              cwd=repo_root, check=False,
          )
          if result.returncode == 0:
              data = json.loads(result.stdout)
              return  # PR confirmed to exist

          stderr = result.stderr.lower()
          if "could not resolve" in stderr or "no pull requests" in stderr:
              if attempt < poll_attempts - 1:
                  time.sleep(poll_interval)
                  continue
              raise RuntimeError(
                  f"PR verification failed: {pr_url} does not exist on GitHub "
                  f"after {poll_attempts} attempts ({poll_attempts * poll_interval}s)"
              )

          # Other gh errors (auth, network)
          if attempt < poll_attempts - 1:
              time.sleep(poll_interval)
              continue
          raise RuntimeError(f"gh pr view failed after {poll_attempts} attempts: {result.stderr}")
  ```

- [ ] **Step 3: Update `create_pr()` to call `verify_pr()` after success**

  After `pr_url = result.stdout.strip()`, add:

  ```python
  # Verify PR exists (GitHub API eventual consistency)
  verify_pr(pr_url, repo_root)
  ```

  The updated `create_pr()` tail becomes:

  ```python
  if result.returncode != 0:
      raise RuntimeError(f"gh pr create failed: {result.stderr}")
  pr_url = result.stdout.strip()

  # Verify PR exists (GitHub API eventual consistency)
  verify_pr(pr_url, repo_root)

  return pr_url
  ```

- [ ] **Step 4: Commit**

  ```
  feat: add verify_pr() to worktree.py and call it from create_pr()
  ```

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. verify_pr is importable from golem.worktree
python -c "from golem.worktree import verify_pr; print('IMPORT: PASS')"

# 2. verify_pr raises RuntimeError for invalid URL
python -c "
from golem.worktree import verify_pr
from pathlib import Path
try:
    verify_pr('https://github.com/owner/repo/issues/42', Path('.'))
    print('INVALID_URL: FAIL')
except RuntimeError as e:
    assert 'Could not extract PR number' in str(e), f'wrong error: {e}'
    print('INVALID_URL: PASS')
"

# 3. verify_pr polls and raises after all attempts fail
python -c "
import json
from unittest.mock import patch, MagicMock
from pathlib import Path
from golem.worktree import verify_pr

mock_result = MagicMock()
mock_result.returncode = 1
mock_result.stderr = 'could not resolve to a PullRequest'
mock_result.stdout = ''

with patch('golem.worktree._run', return_value=mock_result), \
     patch('time.sleep'):
    try:
        verify_pr('https://github.com/owner/repo/pull/99', Path('.'), poll_attempts=3, poll_interval=0)
        print('POLL_FAIL: FAIL')
    except RuntimeError as e:
        assert 'PR verification failed' in str(e), f'wrong error: {e}'
        print('POLL_FAIL: PASS')
"

# 4. verify_pr succeeds when gh returns valid JSON
python -c "
import json
from unittest.mock import patch, MagicMock
from pathlib import Path
from golem.worktree import verify_pr

mock_result = MagicMock()
mock_result.returncode = 0
mock_result.stdout = json.dumps({'state': 'OPEN', 'url': 'https://github.com/owner/repo/pull/99', 'number': 99})

with patch('golem.worktree._run', return_value=mock_result):
    verify_pr('https://github.com/owner/repo/pull/99', Path('.'))
    print('SUCCESS: PASS')
"

# 5. create_pr calls verify_pr after gh pr create succeeds
python -c "
from pathlib import Path
from unittest.mock import patch, MagicMock, call
from golem.worktree import create_pr

mock_result = MagicMock()
mock_result.returncode = 0
mock_result.stdout = 'https://github.com/owner/repo/pull/42\n'

calls = []
def fake_verify(pr_url, repo_root, **kw):
    calls.append(pr_url)

with patch('golem.worktree._run', return_value=mock_result), \
     patch('golem.worktree.verify_pr', side_effect=fake_verify):
    result = create_pr('my-branch', 'title', 'body', False, Path('.'))
    assert result == 'https://github.com/owner/repo/pull/42', f'wrong url: {result}'
    assert len(calls) == 1, f'verify_pr call count: {len(calls)}'
    print('CREATE_PR_VERIFY: PASS')
"
```

Expected output:
```
IMPORT: PASS
INVALID_URL: PASS
POLL_FAIL: PASS
SUCCESS: PASS
CREATE_PR_VERIFY: PASS
```

If any check fails, fix the issue and re-run ALL checks before proceeding.

---

### Task 4: Add Rework Info Helpers and Update `build_writer_prompt()`

**Files:**
- Modify: `src/golem/writer.py`

- [ ] **Step 1: Add `_get_rework_info()` helper function**

  Add after the `_WRITER_PROMPT_TEMPLATE` module-level constant:

  ```python
  def _get_rework_info(ticket: Ticket) -> tuple[int, list[str]]:
      """Count needs_work events and extract rejection notes from ticket history."""
      rework_count = 0
      rework_notes: list[str] = []
      for event in ticket.history:
          if "needs_work" in (event.action or "").lower() or (
              event.note and "needs_work" in event.note.lower()
          ):
              rework_count += 1
              if event.note:
                  rework_notes.append(event.note)
      return rework_count, rework_notes
  ```

- [ ] **Step 2: Update `build_writer_prompt()` signature to accept rework parameters**

  Change the signature from:

  ```python
  def build_writer_prompt(ticket: Ticket) -> str:
  ```

  To:

  ```python
  def build_writer_prompt(ticket: Ticket, rework_count: int = 0, rework_notes: list[str] | None = None) -> str:
      """Build the Junior Dev prompt from a ticket, with optional rework context."""
  ```

- [ ] **Step 3: Add rework template selection logic inside `build_writer_prompt()`**

  After the `template = _WRITER_PROMPT_TEMPLATE.read_text(...)` line, replace it with:

  ```python
  template_name = "worker_rework.md" if rework_count > 0 else "worker.md"
  template_path = Path(__file__).parent / "prompts" / template_name
  # Fall back to worker.md if rework template doesn't exist yet
  if not template_path.exists():
      template_path = Path(__file__).parent / "prompts" / "worker.md"
  template = template_path.read_text(encoding="utf-8")
  ```

- [ ] **Step 4: Add rework context builder and inject into replacements dict**

  Before the `replacements = {...}` dict, add:

  ```python
  # Build rework context string
  rework_context = ""
  if rework_count > 0 and rework_notes:
      rework_context = "## Previous Rejection Feedback\n\n"
      for i, note in enumerate(rework_notes[-3:], 1):  # Last 3 rejections
          rework_context += f"### Attempt {i} Feedback\n{note}\n\n"
      rework_context += (
          f"This is attempt {rework_count + 1}. "
          "Address ALL previous feedback before submitting.\n"
      )
  ```

  Add to the `replacements` dict:

  ```python
  "{iteration}": str(rework_count + 1),
  "{rework_context}": rework_context,
  ```

- [ ] **Step 5: Update `spawn_writer_pair()` to call `_get_rework_info()` and pass results to `build_writer_prompt()`**

  Change:

  ```python
  prompt = build_writer_prompt(ticket)
  ```

  To:

  ```python
  rework_count, rework_notes = _get_rework_info(ticket)
  prompt = build_writer_prompt(ticket, rework_count=rework_count, rework_notes=rework_notes)
  ```

- [ ] **Step 6: Commit**

  ```
  feat: add _get_rework_info() and rework context to build_writer_prompt()
  ```

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. _get_rework_info is importable
python -c "from golem.writer import _get_rework_info; print('IMPORT_REWORK: PASS')"

# 2. build_writer_prompt accepts rework_count and rework_notes params
python -c "
import inspect
from golem.writer import build_writer_prompt
sig = inspect.signature(build_writer_prompt)
params = list(sig.parameters.keys())
assert 'rework_count' in params, f'rework_count missing: {params}'
assert 'rework_notes' in params, f'rework_notes missing: {params}'
print('SIGNATURE: PASS')
"

# 3. _get_rework_info returns correct count from ticket history
python -c "
from golem.writer import _get_rework_info
from golem.tickets import Ticket, TicketContext, HistoryEvent
ctx = TicketContext(plan_file='', files={}, references=[], blueprint='', acceptance=[], qa_checks=[], parallelism_hints=[])
ticket = Ticket(id='T-001', type='task', title='test', status='pending', priority='high', created_by='tl', assigned_to='w', context=ctx)
ticket.history = [
    HistoryEvent(action='needs_work', note='fix lint errors'),
    HistoryEvent(action='needs_work', note='wrong file modified'),
]
count, notes = _get_rework_info(ticket)
assert count == 2, f'count={count}'
assert notes == ['fix lint errors', 'wrong file modified'], f'notes={notes}'
print('REWORK_COUNT: PASS')
"

# 4. _get_rework_info returns 0,[] for empty history
python -c "
from golem.writer import _get_rework_info
from golem.tickets import Ticket, TicketContext
ctx = TicketContext(plan_file='', files={}, references=[], blueprint='', acceptance=[], qa_checks=[], parallelism_hints=[])
ticket = Ticket(id='T-001', type='task', title='test', status='pending', priority='high', created_by='tl', assigned_to='w', context=ctx)
count, notes = _get_rework_info(ticket)
assert count == 0, f'count={count}'
assert notes == [], f'notes={notes}'
print('EMPTY_HISTORY: PASS')
"

# 5. First attempt prompt has iteration=1, empty rework_context
python -c "
from golem.writer import build_writer_prompt
from golem.tickets import Ticket, TicketContext
ctx = TicketContext(plan_file='', files={}, references=[], blueprint='test', acceptance=[], qa_checks=[], parallelism_hints=[])
ticket = Ticket(id='T-001', type='task', title='test', status='pending', priority='high', created_by='tl', assigned_to='w', context=ctx)
prompt = build_writer_prompt(ticket, rework_count=0)
assert '1' in prompt or 'attempt' in prompt.lower(), 'iteration marker missing'
assert 'Previous Rejection' not in prompt, 'rework context should be empty'
print('FIRST_ATTEMPT: PASS')
"

# 6. Rework prompt includes previous notes (last 3 of 5)
python -c "
from golem.writer import build_writer_prompt
from golem.tickets import Ticket, TicketContext
ctx = TicketContext(plan_file='', files={}, references=[], blueprint='test', acceptance=[], qa_checks=[], parallelism_hints=[])
ticket = Ticket(id='T-001', type='task', title='test', status='pending', priority='high', created_by='tl', assigned_to='w', context=ctx)
notes = ['note1', 'note2', 'note3', 'note4', 'note5']
prompt = build_writer_prompt(ticket, rework_count=5, rework_notes=notes)
assert 'note3' in prompt, 'note3 (3rd from end) missing'
assert 'note4' in prompt, 'note4 (2nd from end) missing'
assert 'note5' in prompt, 'note5 (last) missing'
assert 'note1' not in prompt, 'note1 (too old) should be excluded'
assert 'note2' not in prompt, 'note2 (too old) should be excluded'
print('LAST_3_NOTES: PASS')
"
```

Expected output:
```
IMPORT_REWORK: PASS
SIGNATURE: PASS
REWORK_COUNT: PASS
EMPTY_HISTORY: PASS
FIRST_ATTEMPT: PASS
LAST_3_NOTES: PASS
```

If any check fails, fix the issue and re-run ALL checks before proceeding.

---

### Task 5: Update `worker.md` Prompt Template

**Files:**
- Modify: `src/golem/prompts/worker.md`

- [ ] **Step 1: Add iteration section after the "Blueprint" section and before the "Context" section**

  After the `## Blueprint` section (which ends with `{blueprint}`), add:

  ```markdown
  ## Iteration

  This is attempt {iteration}.

  {rework_context}
  ```

  The placement: after `{blueprint}` line and before the `---` separator that precedes the `## Context` section.

- [ ] **Step 2: Commit**

  ```
  feat: add {iteration} and {rework_context} template variables to worker.md
  ```

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. worker.md contains {iteration} placeholder
python -c "
from pathlib import Path
content = Path('src/golem/prompts/worker.md').read_text(encoding='utf-8')
assert '{iteration}' in content, '{iteration} placeholder missing'
print('ITERATION_VAR: PASS')
"

# 2. worker.md contains {rework_context} placeholder
python -c "
from pathlib import Path
content = Path('src/golem/prompts/worker.md').read_text(encoding='utf-8')
assert '{rework_context}' in content, '{rework_context} placeholder missing'
print('REWORK_VAR: PASS')
"

# 3. build_writer_prompt with a full ticket leaves no {placeholder} patterns
python -c "
import re
from golem.writer import build_writer_prompt
from golem.tickets import Ticket, TicketContext
ctx = TicketContext(
    plan_file='',
    files={'src/main.py': 'def main(): pass'},
    references=['docs/api.md'],
    blueprint='Build the main module.',
    acceptance=['main() exists'],
    qa_checks=['python -m py_compile src/main.py'],
    parallelism_hints=[],
)
ticket = Ticket(id='T-001', type='task', title='test', status='pending', priority='high', created_by='tl', assigned_to='w', context=ctx)
prompt = build_writer_prompt(ticket, rework_count=0)
leftover = re.findall(r'\{[a-z_]+\}', prompt)
assert leftover == [], f'Unresolved placeholders: {leftover}'
print('NO_LEFTOVER_PLACEHOLDERS: PASS')
"
```

Expected output:
```
ITERATION_VAR: PASS
REWORK_VAR: PASS
NO_LEFTOVER_PLACEHOLDERS: PASS
```

If any check fails, fix the issue and re-run ALL checks before proceeding.

---

### Task 6: Create `worker_rework.md` Prompt Template

**Files:**
- Create: `src/golem/prompts/worker_rework.md`

- [ ] **Step 1: Create `worker_rework.md` based on `worker.md`**

  Start from the full content of `worker.md`. Apply these key differences:

  - Open with a new first section before "Ticket Context":

    ```markdown
    ## Notice

    Your previous attempt was rejected. The Tech Lead found specific issues with your work.
    Do not repeat the same mistakes. Read the rejection feedback carefully before writing any code.

    {rework_context}
    ```

  - In the `## QA Loop` step (Step 4), reduce max retries from 3 to 2:

    Change:
    > If still failing after 3 attempts, call `mcp__golem-writer__update_ticket`

    To:
    > If still failing after 2 attempts, call `mcp__golem-writer__update_ticket`

  - In Step 4, after the retries instruction, add:

    > If you are unsure how to address a piece of feedback, update the ticket to needs_work with a specific question rather than guessing.

  - Do NOT use aggressive caps or emoji. Be direct and factual, not punitive.
  - All other sections remain identical to `worker.md`.
  - Include all existing template variables: `{ticket_context}`, `{plan_section}`, `{file_contents}`, `{references}`, `{blueprint}`, `{acceptance}`, `{qa_checks}`, `{parallelism_hints}`, `{iteration}`, `{rework_context}`.

- [ ] **Step 2: Commit**

  ```
  feat: add worker_rework.md prompt template for rework sessions
  ```

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. worker_rework.md exists and is non-empty
test -s src/golem/prompts/worker_rework.md && echo "FILE_EXISTS: PASS" || echo "FILE_EXISTS: FAIL"

# 2. worker_rework.md contains the rejection notice
python -c "
from pathlib import Path
content = Path('src/golem/prompts/worker_rework.md').read_text(encoding='utf-8')
assert 'previous attempt was rejected' in content.lower(), 'rejection notice missing'
print('REJECTION_NOTICE: PASS')
"

# 3. worker_rework.md has all required template variables
python -c "
from pathlib import Path
content = Path('src/golem/prompts/worker_rework.md').read_text(encoding='utf-8')
required = ['{ticket_context}', '{plan_section}', '{file_contents}', '{references}', '{blueprint}', '{acceptance}', '{qa_checks}', '{iteration}', '{rework_context}']
missing = [v for v in required if v not in content]
assert not missing, f'Missing variables: {missing}'
print('TEMPLATE_VARS: PASS')
"

# 4. build_writer_prompt selects worker_rework.md when rework_count > 0
python -c "
from golem.writer import build_writer_prompt
from golem.tickets import Ticket, TicketContext
ctx = TicketContext(plan_file='', files={}, references=[], blueprint='test', acceptance=[], qa_checks=[], parallelism_hints=[])
ticket = Ticket(id='T-001', type='task', title='test', status='pending', priority='high', created_by='tl', assigned_to='w', context=ctx)
prompt = build_writer_prompt(ticket, rework_count=1, rework_notes=['fix the lint'])
assert 'previous attempt' in prompt.lower(), 'rework template not selected'
print('TEMPLATE_SELECTION: PASS')
"

# 5. worker_rework.md mentions 2 retries (not 3)
python -c "
from pathlib import Path
content = Path('src/golem/prompts/worker_rework.md').read_text(encoding='utf-8')
assert '2 attempts' in content or 'after 2' in content, '2-attempt limit not found'
print('REDUCED_RETRIES: PASS')
"
```

Expected output:
```
FILE_EXISTS: PASS
REJECTION_NOTICE: PASS
TEMPLATE_VARS: PASS
TEMPLATE_SELECTION: PASS
REDUCED_RETRIES: PASS
```

If any check fails, fix the issue and re-run ALL checks before proceeding.

---

### Task 7: Write Tests

**Files:**
- Modify: `tests/test_writer.py`
- Modify: `tests/test_worktree.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Add tests to `tests/test_config.py`**

  Add two tests:

  ```python
  def test_dispatch_jitter_max_default() -> None:
      config = GolemConfig()
      assert config.dispatch_jitter_max == 5.0


  def test_dispatch_jitter_max_roundtrip() -> None:
      with tempfile.TemporaryDirectory() as tmpdir:
          golem_dir = Path(tmpdir)
          config = GolemConfig(dispatch_jitter_max=3.5)
          save_config(config, golem_dir)
          loaded = load_config(golem_dir)
          assert loaded.dispatch_jitter_max == 3.5
  ```

- [ ] **Step 2: Add tests to `tests/test_writer.py`**

  Add these tests — import `HistoryEvent` from `golem.tickets` at the top of the file:

  ```python
  from golem.tickets import HistoryEvent
  ```

  Tests:

  ```python
  def test_build_prompt_first_attempt() -> None:
      """rework_count=0: iteration placeholder resolved to '1', rework_context is empty."""
      ticket = _make_ticket_with_context()
      prompt = build_writer_prompt(ticket, rework_count=0)
      # {iteration} and {rework_context} must not appear as raw placeholders
      assert "{iteration}" not in prompt
      assert "{rework_context}" not in prompt
      # Rework section should not appear
      assert "Previous Rejection" not in prompt


  def test_build_prompt_rework() -> None:
      """rework_count=2, notes=['fix lint', 'wrong file'] -> iteration=3, both notes in prompt."""
      ticket = _make_ticket_with_context()
      notes = ["fix lint errors", "wrong file modified"]
      prompt = build_writer_prompt(ticket, rework_count=2, rework_notes=notes)
      assert "{iteration}" not in prompt
      assert "{rework_context}" not in prompt
      assert "fix lint errors" in prompt
      assert "wrong file modified" in prompt


  def test_build_prompt_rework_limits_notes() -> None:
      """With 5 rework notes, only the last 3 appear in the prompt."""
      ticket = _make_ticket_with_context()
      notes = ["n1", "n2", "n3", "n4", "n5"]
      prompt = build_writer_prompt(ticket, rework_count=5, rework_notes=notes)
      assert "n3" in prompt
      assert "n4" in prompt
      assert "n5" in prompt
      assert "n1" not in prompt
      assert "n2" not in prompt


  def test_get_rework_info_counts_needs_work() -> None:
      """_get_rework_info counts needs_work events and extracts notes."""
      from golem.writer import _get_rework_info
      from golem.tickets import HistoryEvent
      ticket = _make_ticket_with_context()
      ticket.history = [
          HistoryEvent(action="needs_work", note="fix lint"),
          HistoryEvent(action="approved", note=""),
          HistoryEvent(action="needs_work", note="wrong file"),
      ]
      count, notes = _get_rework_info(ticket)
      assert count == 2
      assert notes == ["fix lint", "wrong file"]


  def test_get_rework_info_empty_history() -> None:
      """_get_rework_info returns (0, []) for a ticket with no history."""
      from golem.writer import _get_rework_info
      ticket = _make_ticket_with_context()
      # Ticket from _make_ticket_with_context has empty history by default
      count, notes = _get_rework_info(ticket)
      assert count == 0
      assert notes == []


  @pytest.mark.asyncio
  async def test_jitter_skip_in_test_mode() -> None:
      """With GOLEM_TEST_MODE=1, no asyncio.sleep is called for jitter."""
      import os
      slept_durations: list[float] = []

      async def fake_sleep(n: float) -> None:
          slept_durations.append(n)

      async def fake_query(*args, **kwargs):
          return
          yield

      ticket = _make_ticket_with_context()
      config = GolemConfig(dispatch_jitter_max=10.0)

      with patch("golem.writer.query", side_effect=fake_query), \
           patch("asyncio.sleep", side_effect=fake_sleep), \
           patch.dict(os.environ, {"GOLEM_TEST_MODE": "1"}):
          with tempfile.TemporaryDirectory() as tmpdir:
              golem_dir = Path(tmpdir) / ".golem"
              (golem_dir / "tickets").mkdir(parents=True)
              await spawn_writer_pair(ticket, tmpdir, config, golem_dir=golem_dir)

      # asyncio.sleep may be called for retry_delay but NOT for jitter (which is 10.0s max)
      jitter_sleeps = [d for d in slept_durations if d >= 1.0]
      assert len(jitter_sleeps) == 0, f"Jitter sleep called in test mode: {slept_durations}"
  ```

- [ ] **Step 3: Add tests to `tests/test_worktree.py`**

  Add import at top: `import json`
  Add import at top: `from unittest.mock import MagicMock, patch`
  Add import from golem.worktree: `verify_pr`

  Tests:

  ```python
  def test_verify_pr_success() -> None:
      """verify_pr does not raise when gh pr view returns valid JSON."""
      mock_result = MagicMock()
      mock_result.returncode = 0
      mock_result.stdout = json.dumps({"state": "OPEN", "url": "https://github.com/o/r/pull/1", "number": 1})

      with patch("golem.worktree._run", return_value=mock_result):
          verify_pr("https://github.com/o/r/pull/1", Path("/tmp"))  # Should not raise


  def test_verify_pr_not_found_retries() -> None:
      """verify_pr retries and succeeds when gh fails first then returns valid JSON."""
      fail_result = MagicMock()
      fail_result.returncode = 1
      fail_result.stderr = "could not resolve to a PullRequest"

      ok_result = MagicMock()
      ok_result.returncode = 0
      ok_result.stdout = json.dumps({"state": "OPEN", "url": "https://github.com/o/r/pull/5", "number": 5})

      with patch("golem.worktree._run", side_effect=[fail_result, fail_result, ok_result]), \
           patch("time.sleep"):
          verify_pr("https://github.com/o/r/pull/5", Path("/tmp"), poll_attempts=6, poll_interval=0)


  def test_verify_pr_not_found_all_retries() -> None:
      """verify_pr raises RuntimeError after all poll_attempts fail."""
      fail_result = MagicMock()
      fail_result.returncode = 1
      fail_result.stderr = "could not resolve to a PullRequest"

      with patch("golem.worktree._run", return_value=fail_result), \
           patch("time.sleep"):
          with pytest.raises(RuntimeError, match="PR verification failed"):
              verify_pr("https://github.com/o/r/pull/7", Path("/tmp"), poll_attempts=3, poll_interval=0)


  def test_verify_pr_invalid_url() -> None:
      """verify_pr raises RuntimeError when URL has no /pull/NNN segment."""
      with pytest.raises(RuntimeError, match="Could not extract PR number"):
          verify_pr("https://github.com/o/r/issues/42", Path("/tmp"))


  def test_verify_pr_gh_auth_error() -> None:
      """verify_pr raises RuntimeError after all attempts with an auth error."""
      auth_fail = MagicMock()
      auth_fail.returncode = 1
      auth_fail.stderr = "authentication required: run gh auth login"

      with patch("golem.worktree._run", return_value=auth_fail), \
           patch("time.sleep"):
          with pytest.raises(RuntimeError, match="gh pr view failed after"):
              verify_pr("https://github.com/o/r/pull/3", Path("/tmp"), poll_attempts=2, poll_interval=0)
  ```

- [ ] **Step 4: Commit**

  ```
  test: add tests for jitter, verify_pr, rework info, and config field
  ```

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. New config tests pass (2 new tests — expect previous + 2)
uv run pytest tests/test_config.py -v -k "dispatch_jitter" 2>&1 | tail -5
uv run pytest tests/test_config.py -v -k "dispatch_jitter" 2>&1 | grep -E "passed|failed" && echo "CONFIG_TESTS: PASS" || echo "CONFIG_TESTS: FAIL"

# 2. New writer tests pass
uv run pytest tests/test_writer.py -v -k "rework or jitter or first_attempt" 2>&1 | tail -5
uv run pytest tests/test_writer.py -v -k "rework or jitter or first_attempt" 2>&1 | grep -E "passed|failed" && echo "WRITER_TESTS: PASS" || echo "WRITER_TESTS: FAIL"

# 3. New worktree tests pass
uv run pytest tests/test_worktree.py -v -k "verify_pr" 2>&1 | tail -5
uv run pytest tests/test_worktree.py -v -k "verify_pr" 2>&1 | grep -E "passed|failed" && echo "WORKTREE_TESTS: PASS" || echo "WORKTREE_TESTS: FAIL"

# 4. Full test suite passes with no regressions — expect 272 or more tests passing
uv run pytest --tb=short -q 2>&1 | tail -10
uv run pytest --tb=short -q 2>&1 | grep -E "^\d+ passed" && echo "FULL_SUITE: PASS" || echo "FULL_SUITE: FAIL"
```

Expected output:
```
CONFIG_TESTS: PASS
WRITER_TESTS: PASS
WORKTREE_TESTS: PASS
FULL_SUITE: PASS
```

If any check fails, fix the issue and re-run ALL checks before proceeding.

---

## Tests

### File: `tests/test_writer.py`

- `test_build_prompt_first_attempt` — rework_count=0, verify `{iteration}` replaced with "1", `{rework_context}` is empty
- `test_build_prompt_rework` — rework_count=2, rework_notes=["fix lint", "wrong file"], verify iteration=3, rework_context contains both notes
- `test_build_prompt_rework_limits_notes` — 5 rework notes, verify only last 3 are included
- `test_get_rework_info_counts_needs_work` — ticket with 2 needs_work events, verify count=2 and notes extracted
- `test_get_rework_info_empty_history` — new ticket, verify count=0, notes=[]
- `test_jitter_skip_in_test_mode` — verify jitter is skipped when `GOLEM_TEST_MODE=1`

### File: `tests/test_worktree.py`

- `test_verify_pr_success` — mock `gh pr view` returning valid JSON, verify no exception
- `test_verify_pr_not_found_retries` — mock `gh pr view` failing 3 times then succeeding, verify success after retries
- `test_verify_pr_not_found_all_retries` — mock `gh pr view` failing all 6 times, verify RuntimeError raised
- `test_verify_pr_invalid_url` — pass URL without `/pull/NNN`, verify RuntimeError
- `test_verify_pr_gh_auth_error` — mock gh returning auth error, verify RuntimeError after retries

### File: `tests/test_config.py`

- `test_dispatch_jitter_max_default` — verify default is 5.0
- `test_dispatch_jitter_max_roundtrip` — save/load config with jitter, verify preserved

---

## Acceptance Criteria

- [ ] `dispatch_jitter_max` config field exists (default 5.0)
- [ ] Junior Dev spawn has random jitter delay (0 to `dispatch_jitter_max` seconds)
- [ ] Jitter is skipped in test mode
- [ ] Jitter delay is logged to stderr
- [ ] `verify_pr()` polls `gh pr view` up to 6 times with 5s intervals
- [ ] `verify_pr()` raises RuntimeError if PR cannot be verified
- [ ] `create_pr()` calls `verify_pr()` after successful creation
- [ ] `build_writer_prompt()` accepts `rework_count` and `rework_notes` parameters
- [ ] First-attempt prompts have iteration=1 and empty rework context
- [ ] Rework prompts include previous rejection feedback (last 3 notes)
- [ ] `worker.md` has `{iteration}` and `{rework_context}` template variables
- [ ] `_get_rework_info()` correctly counts needs_work events from ticket history
- [ ] All new tests pass
- [ ] Existing test suite passes (no regressions)

---

## Phase Completion Gate

Run this after ALL tasks are complete to verify the full implementation:

```bash
cd F:/Tools/Projects/golem-cli

# 1. Config field exists with correct default
python -c "from golem.config import GolemConfig; c = GolemConfig(); assert c.dispatch_jitter_max == 5.0; print('CFG_JITTER: PASS')"

# 2. verify_pr importable and works correctly
python -c "from golem.worktree import verify_pr; print('WORKTREE_IMPORT: PASS')"

# 3. _get_rework_info importable
python -c "from golem.writer import _get_rework_info; print('WRITER_IMPORT: PASS')"

# 4. build_writer_prompt has rework_count parameter
python -c "import inspect; from golem.writer import build_writer_prompt; assert 'rework_count' in inspect.signature(build_writer_prompt).parameters; print('PROMPT_SIG: PASS')"

# 5. worker.md has both new template variables
python -c "
from pathlib import Path
c = Path('src/golem/prompts/worker.md').read_text(encoding='utf-8')
assert '{iteration}' in c and '{rework_context}' in c
print('WORKER_MD: PASS')
"

# 6. worker_rework.md exists and is non-empty
test -s src/golem/prompts/worker_rework.md && echo "REWORK_MD: PASS" || echo "REWORK_MD: FAIL"

# 7. Full test suite passes
uv run pytest --tb=short -q 2>&1 | tail -3
uv run pytest --tb=short -q 2>&1 | grep -E "^\d+ passed" && echo "ALL_TESTS: PASS" || echo "ALL_TESTS: FAIL"
```

Expected output:
```
CFG_JITTER: PASS
WORKTREE_IMPORT: PASS
WRITER_IMPORT: PASS
PROMPT_SIG: PASS
WORKER_MD: PASS
REWORK_MD: PASS
ALL_TESTS: PASS
```

If any check fails, fix the issue and re-run ALL checks before proceeding.
