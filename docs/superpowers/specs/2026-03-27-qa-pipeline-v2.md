# QA Pipeline v2

## Problem

Golem's QA pipeline has three gaps that waste time and mask real issues:

1. **No short-circuit on lint failure.** Infrastructure checks (ruff, tsc) and spec checks (pytest) run as a flat list. When `ruff check .` fails (code doesn't parse), `pytest` still runs all tests (60+ seconds wasted on code that won't import). ZeroShot's two-stage validation runs quick checks first and skips heavy checks if quick fails.

2. **No CANNOT_VALIDATE concept.** When a QA check fails because the tool isn't installed (e.g., `ruff` not on PATH, `pytest` binary missing), the failure is indistinguishable from a code failure. The Junior Dev retries 3 times on a broken environment, wastes tokens, then escalates. ZeroShot tracks `CANNOT_VALIDATE` and carries it forward so validators don't retry impossible checks.

3. **Crash = silent failure.** If `subprocess.run` raises an `OSError` or `FileNotFoundError` (binary not found, permission denied), the exception propagates uncaught through `run_qa` → `_handle_run_qa` → MCP response. The Junior Dev receives an error dict instead of a structured `QAResult`, with no clear signal. ZeroShot treats validator crashes as explicit rejections — `approved: false, crashedAfterRetries: true`.

Additionally, there are two divergent `detect_infrastructure_checks` implementations (one in `qa.py`, one in `cli.py`) that detect different tools.

## Design

### Two-Stage Validation

Split `run_qa()` into two phases:
- **Phase 1 (fast gate):** Run `infrastructure_checks` only. If ANY fail, return immediately with `stage="infrastructure_failed"` — skip the full test suite entirely.
- **Phase 2 (full suite):** Run `checks` (spec checks) only if Phase 1 passed.

This saves 60+ seconds on every iteration where code has syntax/lint errors.

### CANNOT_VALIDATE

Add `cannot_validate: bool` to both `QACheck` and `QAResult`. A check is `cannot_validate=True` when:
- The command binary is not found (`FileNotFoundError`)
- Permission denied (`PermissionError`)
- Any `OSError` subclass that indicates an environment problem, not a code problem

`QAResult.cannot_validate = any(c.cannot_validate for c in result.checks)`. When `cannot_validate` is true, `passed` is still `False`, but the summary and MCP response clearly distinguish it from a code failure.

The Junior Dev prompt (worker.md) gets a new branch: if `cannot_validate` is true, escalate to `needs_work` immediately (don't retry 3 times).

### Crash = Rejection

Wrap every `subprocess.run` call in `run_qa()` with a catch-all `except Exception`. On crash, create a `QACheck(passed=False, cannot_validate=True, stderr=f"Command error: {e}")`.

Additionally, wrap the entire `run_qa()` call in `_handle_run_qa` with a safety net that always returns valid `QAResult` JSON — never let an exception propagate as a malformed MCP response.

### Consolidate Infrastructure Detection

Move the canonical `detect_infrastructure_checks()` to `qa.py` (its logical home). Have `cli.py` import and call it. Delete the duplicate `_detect_infrastructure_checks` from `cli.py`.

---

## Implementation

### Task 1: Add `cannot_validate` and `stage` fields to QACheck/QAResult dataclasses

**Files:**
- Modify: `src/golem/qa.py`

- [ ] **Step 1: Add `cannot_validate: bool = False` to `QACheck`**

  Update the `QACheck` dataclass to add the new field with a default of `False`:

  ```python
  @dataclass
  class QACheck:
      type: str
      tool: str
      passed: bool
      stdout: str
      stderr: str
      cannot_validate: bool = False  # True when check failed due to environment, not code
  ```

- [ ] **Step 2: Add `cannot_validate: bool` and `stage: str` to `QAResult`**

  Update the `QAResult` dataclass:

  ```python
  @dataclass
  class QAResult:
      passed: bool
      checks: list[QACheck] = field(default_factory=list)
      summary: str = ""
      cannot_validate: bool = False  # True when any check has cannot_validate=True
      stage: str = "complete"        # "infrastructure_failed" | "complete" | "crashed"
  ```

- [ ] **Step 3: Commit**

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. QACheck has cannot_validate field
python -c "from golem.qa import QACheck; fields = QACheck.__dataclass_fields__; print('QACHECK_CANNOT_VALIDATE:', 'PASS' if 'cannot_validate' in fields else 'FAIL')"

# 2. QACheck.cannot_validate defaults to False
python -c "from golem.qa import QACheck; c = QACheck(type='test', tool='x', passed=True, stdout='', stderr=''); print('QACHECK_DEFAULT_FALSE:', 'PASS' if c.cannot_validate is False else 'FAIL')"

# 3. QAResult has cannot_validate field
python -c "from golem.qa import QAResult; fields = QAResult.__dataclass_fields__; print('QARESULT_CANNOT_VALIDATE:', 'PASS' if 'cannot_validate' in fields else 'FAIL')"

# 4. QAResult has stage field
python -c "from golem.qa import QAResult; fields = QAResult.__dataclass_fields__; print('QARESULT_STAGE:', 'PASS' if 'stage' in fields else 'FAIL')"

# 5. QAResult.stage defaults to "complete"
python -c "from golem.qa import QAResult; r = QAResult(passed=True); print('QARESULT_STAGE_DEFAULT:', 'PASS' if r.stage == 'complete' else 'FAIL')"
```

Expected output:
```
QACHECK_CANNOT_VALIDATE: PASS
QACHECK_DEFAULT_FALSE: PASS
QARESULT_CANNOT_VALIDATE: PASS
QARESULT_STAGE: PASS
QARESULT_STAGE_DEFAULT: PASS
```

If any check fails, fix the issue and re-run ALL checks before proceeding.

---

### Task 2: Rewrite `run_qa()` with two-stage validation and `_run_single_check` / `_build_result` helpers

**Files:**
- Modify: `src/golem/qa.py`

- [ ] **Step 1: Add `_classify_check()` helper** (extract the existing type-classification logic from `run_qa` into a named helper):

  ```python
  def _classify_check(cmd: str) -> str:
      if "ruff" in cmd or "lint" in cmd or "eslint" in cmd:
          return "lint"
      if "tsc" in cmd or "mypy" in cmd or "pyright" in cmd:
          return "lint"
      if "pytest" in cmd or "jest" in cmd or "npm test" in cmd:
          return "test"
      return "acceptance"
  ```

- [ ] **Step 2: Add `_run_single_check()` helper** — wraps a single subprocess call with full exception handling:

  ```python
  def _run_single_check(cmd: str, worktree_path: str) -> QACheck:
      check_type = _classify_check(cmd)
      try:
          result = subprocess.run(
              _normalize_cmd(cmd), shell=True, capture_output=True, text=True,
              encoding="utf-8", timeout=120, cwd=worktree_path, env=_subprocess_env(),
          )
          return QACheck(
              type=check_type, tool=cmd,
              passed=result.returncode == 0,
              stdout=result.stdout, stderr=result.stderr,
          )
      except subprocess.TimeoutExpired:
          return QACheck(
              type=check_type, tool=cmd, passed=False,
              stdout="", stderr="Command timed out after 120 seconds",
          )
      except (FileNotFoundError, PermissionError, OSError) as e:
          return QACheck(
              type=check_type, tool=cmd, passed=False,
              stdout="", stderr=f"Environment error: {e}",
              cannot_validate=True,
          )
      except Exception as e:
          return QACheck(
              type=check_type, tool=cmd, passed=False,
              stdout="", stderr=f"Unexpected error: {e}",
              cannot_validate=True,
          )
  ```

- [ ] **Step 3: Add `_build_result()` helper** — builds a `QAResult` from a list of checks:

  ```python
  def _build_result(checks: list[QACheck], stage: str) -> QAResult:
      passed_count = sum(1 for c in checks if c.passed)
      failed = [c.tool for c in checks if not c.passed and not c.cannot_validate]
      env_failed = [c.tool for c in checks if c.cannot_validate]
      has_cannot_validate = any(c.cannot_validate for c in checks)

      parts = [f"{passed_count}/{len(checks)} checks passed"]
      if failed:
          parts.append(f"Failed: {failed}")
      if env_failed:
          parts.append(f"Environment errors: {env_failed}")
      if stage == "infrastructure_failed":
          parts.append("Spec checks skipped (infrastructure failed)")

      return QAResult(
          passed=len(failed) == 0 and not has_cannot_validate,
          checks=checks, summary=". ".join(parts),
          cannot_validate=has_cannot_validate, stage=stage,
      )
  ```

- [ ] **Step 4: Rewrite `run_qa()` to use two-stage logic with the new helpers:**

  ```python
  def run_qa(worktree_path: str, checks: list[str], infrastructure_checks: list[str] | None = None) -> QAResult:
      """Run infrastructure checks first (fast gate), then spec checks. Returns structured QAResult."""
      all_checks: list[QACheck] = []
      infra = infrastructure_checks or []

      # Phase 1: Infrastructure checks (fast gate)
      for cmd in infra:
          check = _run_single_check(cmd, worktree_path)
          all_checks.append(check)

      infra_failed = any(not c.passed for c in all_checks)
      if infra_failed:
          return _build_result(all_checks, stage="infrastructure_failed")

      # Phase 2: Spec checks (only if infra passed)
      for cmd in checks:
          check = _run_single_check(cmd, worktree_path)
          all_checks.append(check)

      return _build_result(all_checks, stage="complete")
  ```

  Note: the signature change (`infrastructure_checks` becomes optional with `None` default) preserves backward compatibility with callers passing an empty list.

- [ ] **Step 5: Commit**

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. _run_single_check exists and is callable
python -c "from golem.qa import _run_single_check; print('RUN_SINGLE_CHECK:', 'PASS')"

# 2. _build_result exists and is callable
python -c "from golem.qa import _build_result; print('BUILD_RESULT:', 'PASS')"

# 3. run_qa returns stage="infrastructure_failed" when infra check fails
python -c "
import tempfile, os
from golem.qa import run_qa
with tempfile.TemporaryDirectory() as t:
    r = run_qa(t, ['exit 0'], ['exit 1'])
    print('INFRA_FAIL_STAGE:', 'PASS' if r.stage == 'infrastructure_failed' else 'FAIL')
"

# 4. run_qa skips spec checks when infra fails (only 1 check in result, not 2)
python -c "
import tempfile
from golem.qa import run_qa
with tempfile.TemporaryDirectory() as t:
    r = run_qa(t, ['exit 0'], ['exit 1'])
    print('INFRA_FAIL_SKIPS_SPEC:', 'PASS' if len(r.checks) == 1 else 'FAIL')
"

# 5. run_qa returns stage="complete" when infra passes
python -c "
import tempfile
from golem.qa import run_qa
with tempfile.TemporaryDirectory() as t:
    r = run_qa(t, ['exit 0'], ['exit 0'])
    print('INFRA_PASS_STAGE:', 'PASS' if r.stage == 'complete' else 'FAIL')
"

# 6. FileNotFoundError produces cannot_validate=True
python -c "
from unittest.mock import patch
import subprocess, tempfile
from golem.qa import _run_single_check
with patch('subprocess.run', side_effect=FileNotFoundError('not found')):
    with tempfile.TemporaryDirectory() as t:
        c = _run_single_check('fake-binary', t)
        print('FNFE_CANNOT_VALIDATE:', 'PASS' if c.cannot_validate is True else 'FAIL')
"

# 7. TimeoutExpired produces passed=False, cannot_validate=False
python -c "
from unittest.mock import patch
import subprocess, tempfile
from golem.qa import _run_single_check
with patch('subprocess.run', side_effect=subprocess.TimeoutExpired('cmd', 120)):
    with tempfile.TemporaryDirectory() as t:
        c = _run_single_check('slow-cmd', t)
        print('TIMEOUT_NOT_CANNOT_VALIDATE:', 'PASS' if c.passed is False and c.cannot_validate is False else 'FAIL')
"
```

Expected output:
```
RUN_SINGLE_CHECK: PASS
BUILD_RESULT: PASS
INFRA_FAIL_STAGE: PASS
INFRA_FAIL_SKIPS_SPEC: PASS
INFRA_PASS_STAGE: PASS
FNFE_CANNOT_VALIDATE: PASS
TIMEOUT_NOT_CANNOT_VALIDATE: PASS
```

If any check fails, fix the issue and re-run ALL checks before proceeding.

---

### Task 3: Add safety wrapper to `_handle_run_qa` in `tools.py`

**Files:**
- Modify: `src/golem/tools.py`

- [ ] **Step 1: Import `QAResult` at top of file** (it is already imported via `run_qa`, but `QAResult` must be explicitly importable for the safety-net fallback):

  In `tools.py`, update the import from `golem.qa`:
  ```python
  from golem.qa import QAResult, run_qa
  ```

- [ ] **Step 2: Rewrite `_handle_run_qa`** to add a try/except safety wrapper and `CANNOT_VALIDATE` status string:

  ```python
  async def _handle_run_qa(args: dict[str, object]) -> dict[str, object]:
      import sys
      try:
          checks_raw = args.get("checks") or []
          infra_raw = args.get("infrastructure_checks") or []
          result = run_qa(
              worktree_path=str(args["worktree_path"]),
              checks=[str(c) for c in checks_raw],
              infrastructure_checks=[str(c) for c in infra_raw],
          )
      except Exception as e:
          # Safety net: always return valid QAResult JSON — never let an exception
          # propagate as a malformed MCP response
          result = QAResult(
              passed=False, checks=[], summary=f"QA runner crashed: {e}",
              cannot_validate=True, stage="crashed",
          )

      passed = sum(1 for c in result.checks if c.passed)
      total = len(result.checks)
      status = "PASSED" if result.passed else ("CANNOT_VALIDATE" if result.cannot_validate else "FAILED")
      print(f"[QA] {status} -- {passed}/{total} checks passed", file=sys.stderr)
      return {"content": [{"type": "text", "text": json.dumps(asdict(result))}]}
  ```

- [ ] **Step 3: Commit**

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. QAResult is importable from tools.py context
python -c "from golem.qa import QAResult; print('QARESULT_IMPORT:', 'PASS')"

# 2. _handle_run_qa returns valid JSON even when run_qa raises
python -c "
import asyncio, json
from unittest.mock import patch
from golem.tools import _handle_run_qa

async def check():
    with patch('golem.tools.run_qa', side_effect=RuntimeError('boom')):
        result = await _handle_run_qa({'worktree_path': '/tmp', 'checks': []})
        text = result['content'][0]['text']
        data = json.loads(text)
        ok = data.get('cannot_validate') is True and data.get('stage') == 'crashed'
        print('CRASH_SAFETY:', 'PASS' if ok else 'FAIL')

asyncio.run(check())
"

# 3. Normal run still returns a JSON-serialisable QAResult
python -c "
import asyncio, json, tempfile
from golem.tools import _handle_run_qa

async def check():
    with tempfile.TemporaryDirectory() as t:
        result = await _handle_run_qa({'worktree_path': t, 'checks': ['exit 0'], 'infrastructure_checks': []})
        data = json.loads(result['content'][0]['text'])
        ok = 'passed' in data and 'stage' in data
        print('NORMAL_QA_RESULT:', 'PASS' if ok else 'FAIL')

asyncio.run(check())
"
```

Expected output:
```
QARESULT_IMPORT: PASS
CRASH_SAFETY: PASS
NORMAL_QA_RESULT: PASS
```

If any check fails, fix the issue and re-run ALL checks before proceeding.

---

### Task 4: Consolidate `detect_infrastructure_checks` — remove duplicate from `cli.py`

**Files:**
- Modify: `src/golem/cli.py`

The canonical `detect_infrastructure_checks()` already lives in `qa.py`. `cli.py` has a private duplicate `_detect_infrastructure_checks` that must be replaced.

- [ ] **Step 1: Remove `_detect_infrastructure_checks` function definition from `cli.py`**

  Delete the entire `def _detect_infrastructure_checks(project_root: Path) -> list[str]:` function body from `cli.py`.

- [ ] **Step 2: Add import of `detect_infrastructure_checks` from `golem.qa` in `cli.py`**

  ```python
  from golem.qa import detect_infrastructure_checks
  ```

- [ ] **Step 3: Replace both call-sites** (`_detect_infrastructure_checks(spec_project_root)` → `detect_infrastructure_checks(spec_project_root)`)

  There are two call sites (around line 153 and line 363 of the current `cli.py`):
  ```python
  config.infrastructure_checks = detect_infrastructure_checks(spec_project_root)
  ```

- [ ] **Step 4: Commit**

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. _detect_infrastructure_checks no longer exists in cli.py
python -c "
import ast, pathlib
src = pathlib.Path('src/golem/cli.py').read_text(encoding='utf-8')
has_private = '_detect_infrastructure_checks' in src
print('NO_PRIVATE_DUPLICATE:', 'PASS' if not has_private else 'FAIL')
"

# 2. cli.py imports detect_infrastructure_checks from golem.qa
python -c "
import pathlib
src = pathlib.Path('src/golem/cli.py').read_text(encoding='utf-8')
has_import = 'from golem.qa import' in src and 'detect_infrastructure_checks' in src
print('QA_IMPORT_PRESENT:', 'PASS' if has_import else 'FAIL')
"

# 3. detect_infrastructure_checks is callable from qa module
python -c "from golem.qa import detect_infrastructure_checks; import pathlib; r = detect_infrastructure_checks(pathlib.Path('.')); print('DETECT_CALLABLE:', 'PASS')"

# 4. cli module still imports cleanly (no broken references)
python -c "import golem.cli; print('CLI_IMPORT:', 'PASS')"
```

Expected output:
```
NO_PRIVATE_DUPLICATE: PASS
QA_IMPORT_PRESENT: PASS
DETECT_CALLABLE: PASS
CLI_IMPORT: PASS
```

If any check fails, fix the issue and re-run ALL checks before proceeding.

---

### Task 5: Update `worker.md` prompt + add new tests to `test_qa.py`

**Files:**
- Modify: `src/golem/prompts/worker.md`
- Modify: `tests/test_qa.py`

#### 5a: Worker Prompt Update

- [ ] **Step 1: Add `cannot_validate` handling to the QA Loop section of `worker.md`**

  After the existing "If still failing after 3 attempts" paragraph in **Step 4: QA Loop**, add:

  ```
  If `cannot_validate` is `true` in the QA result, do NOT retry. This means
  the QA environment is broken (missing tools, permission errors), not your
  code. Update the ticket to `needs_work` immediately with the environment
  error details so the Tech Lead can assess.
  ```

  After the `cannot_validate` paragraph, add:

  ```
  If `stage` is `infrastructure_failed`, only lint/type checks failed — the
  test suite was skipped. Fix the lint errors first, then re-run QA.
  ```

#### 5b: New Tests

- [ ] **Step 2: Add 8 new tests to `tests/test_qa.py`**

  Append the following tests. Use `tmp_path` fixture instead of `tempfile.TemporaryDirectory()` per project testing conventions:

  ```python
  def test_two_stage_skips_spec_checks_on_infra_failure(tmp_path: Path) -> None:
      """Infra check fails -> spec checks not run -> stage='infrastructure_failed'."""
      result = run_qa(str(tmp_path), checks=["exit 0"], infrastructure_checks=["exit 1"])
      assert result.stage == "infrastructure_failed"
      # Only the infra check ran — spec check was skipped
      assert len(result.checks) == 1
      assert result.passed is False


  def test_two_stage_runs_spec_checks_on_infra_pass(tmp_path: Path) -> None:
      """Infra check passes -> spec checks run -> stage='complete'."""
      result = run_qa(str(tmp_path), checks=["exit 0"], infrastructure_checks=["exit 0"])
      assert result.stage == "complete"
      assert len(result.checks) == 2
      assert result.passed is True


  def test_cannot_validate_on_file_not_found(tmp_path: Path) -> None:
      """FileNotFoundError on subprocess.run sets cannot_validate=True on QACheck."""
      with patch("subprocess.run", side_effect=FileNotFoundError("binary not found")):
          check = _run_single_check("fake-binary", str(tmp_path))
      assert check.cannot_validate is True
      assert check.passed is False


  def test_cannot_validate_on_os_error(tmp_path: Path) -> None:
      """OSError on subprocess.run sets cannot_validate=True on QACheck."""
      with patch("subprocess.run", side_effect=OSError("permission denied")):
          check = _run_single_check("locked-cmd", str(tmp_path))
      assert check.cannot_validate is True
      assert check.passed is False


  def test_cannot_validate_propagates_to_result(tmp_path: Path) -> None:
      """QAResult.cannot_validate=True when any check has cannot_validate=True."""
      with patch("subprocess.run", side_effect=FileNotFoundError("not found")):
          result = run_qa(str(tmp_path), checks=["missing-tool"], infrastructure_checks=[])
      assert result.cannot_validate is True
      assert result.passed is False


  def test_timeout_is_not_cannot_validate(tmp_path: Path) -> None:
      """TimeoutExpired -> passed=False but cannot_validate=False (real failure, not env issue)."""
      with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 120)):
          check = _run_single_check("slow-cmd", str(tmp_path))
      assert check.passed is False
      assert check.cannot_validate is False


  def test_crash_safety_in_handle_run_qa(tmp_path: Path) -> None:
      """_handle_run_qa never raises — returns valid QAResult JSON even when run_qa crashes."""
      import asyncio

      async def run() -> None:
          with patch("golem.tools.run_qa", side_effect=RuntimeError("runner exploded")):
              result = await _handle_run_qa({"worktree_path": str(tmp_path), "checks": []})
          text = result["content"][0]["text"]
          data = json.loads(text)
          assert data["cannot_validate"] is True
          assert data["stage"] == "crashed"
          assert data["passed"] is False

      asyncio.run(run())


  def test_detect_infrastructure_checks_consolidated(tmp_path: Path) -> None:
      """The consolidated detect_infrastructure_checks detects ruff, mypy, npm lint, npm test, tsc, cargo test."""
      # ruff via pyproject.toml
      (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 120\n", encoding="utf-8")
      checks = detect_infrastructure_checks(tmp_path)
      assert "ruff check ." in checks

      # npm lint + npm test via package.json
      (tmp_path / "package.json").write_text(
          json.dumps({"scripts": {"lint": "eslint src", "test": "jest"}}), encoding="utf-8"
      )
      checks = detect_infrastructure_checks(tmp_path)
      assert "npm run lint" in checks
      assert "npm test" in checks

      # tsc via tsconfig.json
      (tmp_path / "tsconfig.json").write_text("{}", encoding="utf-8")
      checks = detect_infrastructure_checks(tmp_path)
      assert "npx tsc --noEmit" in checks

      # cargo test via Cargo.toml
      (tmp_path / "Cargo.toml").write_text('[package]\nname = "test"\n', encoding="utf-8")
      checks = detect_infrastructure_checks(tmp_path)
      assert "cargo test" in checks
  ```

  The new tests require these additional imports at the top of `test_qa.py`:
  ```python
  import subprocess
  from golem.qa import _run_single_check
  from golem.tools import _handle_run_qa
  ```

- [ ] **Step 3: Commit**

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. worker.md contains cannot_validate handling instructions
python -c "
src = open('src/golem/prompts/worker.md', encoding='utf-8').read()
has_cv = 'cannot_validate' in src
has_stage = 'infrastructure_failed' in src
print('WORKER_CANNOT_VALIDATE:', 'PASS' if has_cv else 'FAIL')
print('WORKER_INFRA_FAILED:', 'PASS' if has_stage else 'FAIL')
"

# 2. New tests exist in test_qa.py
python -c "
src = open('tests/test_qa.py', encoding='utf-8').read()
tests = [
    'test_two_stage_skips_spec_checks_on_infra_failure',
    'test_two_stage_runs_spec_checks_on_infra_pass',
    'test_cannot_validate_on_file_not_found',
    'test_cannot_validate_on_os_error',
    'test_cannot_validate_propagates_to_result',
    'test_timeout_is_not_cannot_validate',
    'test_crash_safety_in_handle_run_qa',
    'test_detect_infrastructure_checks_consolidated',
]
missing = [t for t in tests if t not in src]
print('NEW_TESTS_PRESENT:', 'PASS' if not missing else f'FAIL missing={missing}')
"

# 3. All new tests pass (8 new tests added to existing 259 = 267 total)
uv run pytest tests/test_qa.py -q 2>&1 | tail -3
```

Expected output:
```
WORKER_CANNOT_VALIDATE: PASS
WORKER_INFRA_FAILED: PASS
NEW_TESTS_PRESENT: PASS
[pytest output showing all test_qa.py tests passing]
```

If any check fails, fix the issue and re-run ALL checks before proceeding.

---

## Phase Completion Gate

Run all checks after completing all 5 tasks to verify the full implementation:

```bash
cd F:/Tools/Projects/golem-cli

# 1. Dataclass fields — QACheck
python -c "
from golem.qa import QACheck
f = QACheck.__dataclass_fields__
ok = 'cannot_validate' in f and f['cannot_validate'].default is False
print('QACHECK_FIELDS:', 'PASS' if ok else 'FAIL')
"

# 2. Dataclass fields — QAResult
python -c "
from golem.qa import QAResult
f = QAResult.__dataclass_fields__
ok = 'cannot_validate' in f and 'stage' in f
print('QARESULT_FIELDS:', 'PASS' if ok else 'FAIL')
"

# 3. Two-stage short-circuit
python -c "
import tempfile
from golem.qa import run_qa
with tempfile.TemporaryDirectory() as t:
    r = run_qa(t, ['exit 0'], ['exit 1'])
    print('TWO_STAGE:', 'PASS' if r.stage == 'infrastructure_failed' and len(r.checks) == 1 else 'FAIL')
"

# 4. cannot_validate on environment error
python -c "
import tempfile
from unittest.mock import patch
from golem.qa import _run_single_check
with tempfile.TemporaryDirectory() as t:
    with patch('subprocess.run', side_effect=FileNotFoundError('x')):
        c = _run_single_check('x', t)
    print('CANNOT_VALIDATE:', 'PASS' if c.cannot_validate is True else 'FAIL')
"

# 5. _handle_run_qa safety net
python -c "
import asyncio, json
from unittest.mock import patch
from golem.tools import _handle_run_qa
async def run():
    with patch('golem.tools.run_qa', side_effect=RuntimeError('boom')):
        r = await _handle_run_qa({'worktree_path': '/tmp', 'checks': []})
    d = json.loads(r['content'][0]['text'])
    print('CRASH_SAFETY:', 'PASS' if d['cannot_validate'] and d['stage'] == 'crashed' else 'FAIL')
asyncio.run(run())
"

# 6. No private duplicate in cli.py
python -c "
src = open('src/golem/cli.py', encoding='utf-8').read()
print('NO_CLI_DUPLICATE:', 'PASS' if '_detect_infrastructure_checks' not in src else 'FAIL')
"

# 7. worker.md contains both new prompt instructions
python -c "
src = open('src/golem/prompts/worker.md', encoding='utf-8').read()
print('WORKER_CV:', 'PASS' if 'cannot_validate' in src else 'FAIL')
print('WORKER_STAGE:', 'PASS' if 'infrastructure_failed' in src else 'FAIL')
"

# 8. Full test suite passes with expected count (267 = 259 existing + 8 new)
uv run pytest -q 2>&1 | tail -3
```

Expected output:
```
QACHECK_FIELDS: PASS
QARESULT_FIELDS: PASS
TWO_STAGE: PASS
CANNOT_VALIDATE: PASS
CRASH_SAFETY: PASS
NO_CLI_DUPLICATE: PASS
WORKER_CV: PASS
WORKER_STAGE: PASS
267 passed in ...s
```

If any check fails, fix the issue and re-run ALL checks before proceeding.

---

## Acceptance Criteria

- [ ] `QACheck` has `cannot_validate: bool` field (default False)
- [ ] `QAResult` has `cannot_validate: bool` and `stage: str` fields
- [ ] Infrastructure check failure short-circuits (spec checks skipped)
- [ ] `FileNotFoundError` and `OSError` produce `cannot_validate=True`, not just `passed=False`
- [ ] `TimeoutExpired` produces `passed=False, cannot_validate=False` (real failure)
- [ ] `_handle_run_qa` never raises — always returns valid QAResult JSON
- [ ] `detect_infrastructure_checks` is consolidated in `qa.py`, imported by `cli.py`
- [ ] Worker prompt includes `cannot_validate` and `stage` handling instructions
- [ ] All new tests pass
- [ ] Existing test suite passes (no regressions)
