# Complexity Conductor

## Problem

Golem runs the same pipeline for every spec: full Opus planner with sub-agents, full Opus Tech Lead (100 turns), full Opus Junior Devs (50 turns each). A 3-line CSS fix costs the same as a new authentication system. The planner prompt has an "Adaptive Complexity Scaling" section that tells the planner model to self-classify, but this is advisory text — the underlying SDK session always uses the same model, same turn limits, same everything.

ZeroShot solves this with a "conductor" pattern: a cheap classifier (Sonnet) reads the issue, classifies it on a complexity scale, and the orchestrator dynamically loads different agent configurations per complexity level. TRIVIAL tasks skip the planner sub-agents entirely. CRITICAL tasks get extra validation passes.

## Design

### Pre-step classification in cli.py

Inject a `classify_spec()` call between spec validation and `run_planner()`. This is the least invasive injection point — it keeps the decision visible to the operator, requires no changes to `run_planner()` or `run_tech_lead()` signatures (other than reading from config), and can be skipped with `--no-classify`.

### Heuristic-first, AI-optional

v1 uses a pure-Python heuristic classifier — no extra SDK session needed. The heuristic reads the spec text and optionally the project context, then scores based on:
- File count mentioned in the spec
- Keyword indicators (cosmetic, config, refactor vs. architecture, auth, migration, security)
- Spec length (short specs tend to be simpler)
- Dependency mentions (new packages = higher complexity)

v2 (future) adds an optional AI classifier using a cheap model (Haiku) with structured JSON output.

### Complexity levels

| Level | Planner Model | Planner Turns | Tech Lead Model | TL Turns | Worker Model | Worker Turns | Skip TL? |
|---|---|---|---|---|---|---|---|
| **TRIVIAL** | haiku | 10 | -- | -- | sonnet | 20 | Yes |
| **SIMPLE** | sonnet | 20 | sonnet | 30 | sonnet | 30 | No |
| **STANDARD** | opus | 50 | opus | 100 | opus | 50 | No |
| **CRITICAL** | opus | 80 | opus | 150 | opus | 80 | No |

STANDARD is the current behavior (defaults preserved).

### Config schema: ComplexityProfile

```python
@dataclass
class ComplexityProfile:
    planner_model: str
    planner_max_turns: int
    tech_lead_model: str
    tech_lead_max_turns: int
    worker_model: str
    worker_max_turns: int
    skip_tech_lead: bool = False
```

Stored in `GolemConfig` as `complexity_profiles: dict[str, ComplexityProfile]` with defaults for all 4 levels. Operators can override any level via `golem config set`.

### TRIVIAL: Skip Tech Lead

For TRIVIAL specs, the pipeline becomes: Planner (Haiku, 10 turns) → single Junior Dev. The planner reads the spec, writes a minimal plan, creates a ticket. `cli.py` skips `run_tech_lead()` and directly calls `spawn_writer_pair()` with the ticket.

This requires a new code path in `cli.py`'s `_run_async()`.

### Fix hardcoded planner max_turns

`planner.py` currently hardcodes `max_turns=50`. This must become `config.planner_max_turns` (new field) so the conductor can override it.

---

## Implementation

### Task 1: Create `src/golem/conductor.py`

**Files:**
- Create: `src/golem/conductor.py`

- [ ] **Step 1: Create the conductor module**

Create `src/golem/conductor.py` with the full heuristic classifier:

```python
"""Spec complexity classification for adaptive pipeline scaling."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ClassificationResult:
    complexity: str  # TRIVIAL | SIMPLE | STANDARD | CRITICAL
    reasoning: str
    confidence: float  # 0.0-1.0


# Keyword indicators (case-insensitive)
_TRIVIAL_KEYWORDS = {"typo", "cosmetic", "readme", "comment", "docstring", "version bump", "changelog"}
_SIMPLE_KEYWORDS = {"config", "env", "rename", "move", "delete file", "add field", "update dependency"}
_CRITICAL_KEYWORDS = {
    "auth", "authentication", "authorization", "security", "migration",
    "database schema", "payment", "billing", "encryption", "credentials",
    "production", "deploy", "infrastructure",
}

# File count patterns
_FILE_MENTION_PATTERN = re.compile(r"(?:modify|create|edit|update|change|add to|delete)\s+[`'\"]?[\w/.-]+\.[a-z]+", re.I)


def classify_spec(spec_text: str, project_context: str = "") -> ClassificationResult:
    """Classify spec complexity using heuristics.

    Returns a ClassificationResult with the complexity level, reasoning, and confidence.
    """
    text = (spec_text + " " + project_context).lower()
    spec_length = len(spec_text)

    # Count file mentions
    file_mentions = len(_FILE_MENTION_PATTERN.findall(spec_text))

    # Score keywords
    trivial_hits = sum(1 for kw in _TRIVIAL_KEYWORDS if kw in text)
    simple_hits = sum(1 for kw in _SIMPLE_KEYWORDS if kw in text)
    critical_hits = sum(1 for kw in _CRITICAL_KEYWORDS if kw in text)

    # Decision logic
    reasons = []

    if critical_hits >= 2:
        reasons.append(f"{critical_hits} critical keywords detected")
        return ClassificationResult("CRITICAL", "; ".join(reasons), 0.8)

    if file_mentions <= 2 and spec_length < 500 and trivial_hits > 0:
        reasons.append(f"{file_mentions} files, {spec_length} chars, trivial keywords")
        return ClassificationResult("TRIVIAL", "; ".join(reasons), 0.7)

    if file_mentions <= 3 and spec_length < 1500:
        reasons.append(f"{file_mentions} files, {spec_length} chars")
        if simple_hits > 0:
            reasons.append(f"{simple_hits} simple keywords")
        return ClassificationResult("SIMPLE", "; ".join(reasons), 0.6)

    if file_mentions > 10 or spec_length > 5000 or critical_hits >= 1:
        reasons.append(f"{file_mentions} files, {spec_length} chars, {critical_hits} critical keywords")
        return ClassificationResult("CRITICAL", "; ".join(reasons), 0.5)

    reasons.append(f"{file_mentions} files, {spec_length} chars (default)")
    return ClassificationResult("STANDARD", "; ".join(reasons), 0.5)
```

- [ ] **Step 2: Commit**

```bash
git add src/golem/conductor.py
git commit -m "feat: add conductor.py heuristic spec classifier"
```

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. File exists and is non-empty
test -s src/golem/conductor.py && echo "FILE: PASS" || echo "FILE: FAIL"

# 2. Module imports cleanly
uv run python -c "from golem.conductor import classify_spec, ClassificationResult; print('IMPORT: PASS')" || echo "IMPORT: FAIL"

# 3. classify_spec returns a ClassificationResult with a valid complexity level
uv run python -c "
from golem.conductor import classify_spec
r = classify_spec('fix typo in readme')
assert r.complexity in ('TRIVIAL','SIMPLE','STANDARD','CRITICAL'), f'bad complexity: {r.complexity}'
assert r.reasoning
assert 0.0 <= r.confidence <= 1.0
print('SMOKE: PASS')
" || echo "SMOKE: FAIL"
```

Expected output:
```
FILE: PASS
IMPORT: PASS
SMOKE: PASS
```

If any check fails, fix the issue and re-run ALL checks before proceeding.

---

### Task 2: Add `ComplexityProfile` and new fields to `src/golem/config.py`

**Files:**
- Modify: `src/golem/config.py`

- [ ] **Step 1: Add `ComplexityProfile` dataclass to config.py**

Insert the `ComplexityProfile` dataclass before `GolemConfig`. It must be a `@dataclass`:

```python
@dataclass
class ComplexityProfile:
    planner_model: str = "claude-opus-4-6"
    planner_max_turns: int = 50
    tech_lead_model: str = "claude-opus-4-6"
    tech_lead_max_turns: int = 100
    worker_model: str = "claude-opus-4-6"
    worker_max_turns: int = 50
    skip_tech_lead: bool = False
```

- [ ] **Step 2: Add new fields to `GolemConfig`**

Add the following fields to `GolemConfig` (after existing fields, before closing of the dataclass):

```python
# Conductor
conductor_enabled: bool = True
planner_max_turns: int = 50  # FIX: was hardcoded in planner.py

# Complexity profiles (defaults provided, operator can override)
complexity_profiles: dict[str, dict] = field(default_factory=lambda: {
    "TRIVIAL": {"planner_model": "claude-haiku-4-5-20251001", "planner_max_turns": 10,
                "tech_lead_model": "", "tech_lead_max_turns": 0,
                "worker_model": "claude-sonnet-4-6", "worker_max_turns": 20,
                "skip_tech_lead": True},
    "SIMPLE": {"planner_model": "claude-sonnet-4-6", "planner_max_turns": 20,
               "tech_lead_model": "claude-sonnet-4-6", "tech_lead_max_turns": 30,
               "worker_model": "claude-sonnet-4-6", "worker_max_turns": 30,
               "skip_tech_lead": False},
    "STANDARD": {"planner_model": "claude-opus-4-6", "planner_max_turns": 50,
                 "tech_lead_model": "claude-opus-4-6", "tech_lead_max_turns": 100,
                 "worker_model": "claude-opus-4-6", "worker_max_turns": 50,
                 "skip_tech_lead": False},
    "CRITICAL": {"planner_model": "claude-opus-4-6", "planner_max_turns": 80,
                 "tech_lead_model": "claude-opus-4-6", "tech_lead_max_turns": 150,
                 "worker_model": "claude-opus-4-6", "worker_max_turns": 80,
                 "skip_tech_lead": False},
})
```

- [ ] **Step 3: Add `apply_complexity_profile()` method to `GolemConfig`**

Add the following method to the `GolemConfig` class:

```python
def apply_complexity_profile(self, complexity: str) -> None:
    """Mutate config fields based on the complexity profile."""
    profile_dict = self.complexity_profiles.get(complexity)
    if not profile_dict:
        return  # STANDARD defaults already set
    self.planner_model = profile_dict.get("planner_model", self.planner_model)
    self.planner_max_turns = profile_dict.get("planner_max_turns", self.planner_max_turns)
    self.tech_lead_model = profile_dict.get("tech_lead_model", self.tech_lead_model)
    self.max_tech_lead_turns = profile_dict.get("tech_lead_max_turns", self.max_tech_lead_turns)
    self.worker_model = profile_dict.get("worker_model", self.worker_model)
    self.max_worker_turns = profile_dict.get("worker_max_turns", self.max_worker_turns)
```

- [ ] **Step 4: Commit**

```bash
git add src/golem/config.py
git commit -m "feat: add ComplexityProfile, conductor_enabled, planner_max_turns, complexity_profiles to GolemConfig"
```

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. ComplexityProfile imports
uv run python -c "from golem.config import ComplexityProfile; print('COMPLEXITY_PROFILE: PASS')" || echo "COMPLEXITY_PROFILE: FAIL"

# 2. GolemConfig has new fields
uv run python -c "
from golem.config import GolemConfig
cfg = GolemConfig()
assert hasattr(cfg, 'conductor_enabled'), 'missing conductor_enabled'
assert hasattr(cfg, 'planner_max_turns'), 'missing planner_max_turns'
assert hasattr(cfg, 'complexity_profiles'), 'missing complexity_profiles'
assert cfg.conductor_enabled is True
assert cfg.planner_max_turns == 50
assert set(cfg.complexity_profiles.keys()) == {'TRIVIAL','SIMPLE','STANDARD','CRITICAL'}
print('FIELDS: PASS')
" || echo "FIELDS: FAIL"

# 3. apply_complexity_profile mutates the config
uv run python -c "
from golem.config import GolemConfig
cfg = GolemConfig()
cfg.apply_complexity_profile('TRIVIAL')
assert cfg.planner_max_turns == 10, f'expected 10, got {cfg.planner_max_turns}'
print('APPLY_PROFILE: PASS')
" || echo "APPLY_PROFILE: FAIL"

# 4. apply_complexity_profile with unknown level is a no-op
uv run python -c "
from golem.config import GolemConfig
cfg = GolemConfig()
cfg.apply_complexity_profile('UNKNOWN')
assert cfg.planner_max_turns == 50
print('APPLY_UNKNOWN: PASS')
" || echo "APPLY_UNKNOWN: FAIL"
```

Expected output:
```
COMPLEXITY_PROFILE: PASS
FIELDS: PASS
APPLY_PROFILE: PASS
APPLY_UNKNOWN: PASS
```

If any check fails, fix the issue and re-run ALL checks before proceeding.

---

### Task 3: Fix hardcoded `max_turns` in `src/golem/planner.py`

**Files:**
- Modify: `src/golem/planner.py`

- [ ] **Step 1: Replace hardcoded `max_turns=50` with `config.planner_max_turns`**

Find the `ClaudeAgentOptions` instantiation in `planner.py` that hardcodes `max_turns=50`. Replace it:

```python
options=ClaudeAgentOptions(
    model=config.planner_model,
    max_turns=config.planner_max_turns,  # was hardcoded 50
    ...
)
```

The surrounding code must remain unchanged. Only `max_turns=50` → `max_turns=config.planner_max_turns`.

- [ ] **Step 2: Commit**

```bash
git add src/golem/planner.py
git commit -m "fix: planner.py uses config.planner_max_turns instead of hardcoded 50"
```

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. Hardcoded max_turns=50 is gone from planner.py
grep -n "max_turns=50" src/golem/planner.py && echo "HARDCODE: FAIL" || echo "HARDCODE: PASS"

# 2. config.planner_max_turns reference exists in planner.py
grep -q "config.planner_max_turns" src/golem/planner.py && echo "CONFIG_TURNS: PASS" || echo "CONFIG_TURNS: FAIL"

# 3. planner module still imports cleanly
uv run python -c "import golem.planner; print('PLANNER_IMPORT: PASS')" || echo "PLANNER_IMPORT: FAIL"
```

Expected output:
```
HARDCODE: PASS
CONFIG_TURNS: PASS
PLANNER_IMPORT: PASS
```

If any check fails, fix the issue and re-run ALL checks before proceeding.

---

### Task 4: Wire classification into `src/golem/cli.py`

**Files:**
- Modify: `src/golem/cli.py`

- [ ] **Step 1: Add `classify_spec` import and classification step in the `run` command**

In `cli.py`, import `classify_spec` from `golem.conductor`:

```python
from golem.conductor import classify_spec
```

After config loading, before `run_planner()`, add the classification step. The variable `classification` must be accessible in `_run_async()` for the TRIVIAL shortcut:

```python
if config.conductor_enabled and not force:
    spec_text = spec_path.read_text(encoding="utf-8")
    classification = classify_spec(spec_text, project_context)
    console.print(f"  Complexity: [bold]{classification.complexity}[/bold] ({classification.reasoning})")
    config.apply_complexity_profile(classification.complexity)
    save_config(config, golem_dir)
```

`project_context` is an empty string `""` if not otherwise available at the injection point.

- [ ] **Step 2: Add TRIVIAL shortcut in `_run_async()`**

After the `run_planner()` call and dry-run check, add the TRIVIAL shortcut:

```python
ticket_id = await run_planner(spec_path, golem_dir, config, project_root)
if dry_run:
    return

# Check if Tech Lead should be skipped (TRIVIAL complexity)
profile = config.complexity_profiles.get(classification.complexity, {})
if profile.get("skip_tech_lead"):
    console.print("  [dim]TRIVIAL: skipping Tech Lead, dispatching single Junior Dev[/dim]")
    ticket = await store.read(ticket_id)
    await spawn_writer_pair(ticket, str(project_root), config, golem_dir)
    return

await run_tech_lead(ticket_id, golem_dir, config, project_root)
```

- [ ] **Step 3: Add `--no-classify` flag to the `run` command**

Add to the `run` typer command signature:

```python
no_classify: bool = typer.Option(False, "--no-classify", help="Skip complexity classification, run STANDARD pipeline")
```

When `--no-classify` is passed, set `config.conductor_enabled = False` (or skip the classification block entirely by passing `no_classify` into `_run_async()`).

- [ ] **Step 4: Commit**

```bash
git add src/golem/cli.py
git commit -m "feat: inject classify_spec into run command, add TRIVIAL shortcut and --no-classify flag"
```

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. classify_spec is imported in cli.py
grep -q "from golem.conductor import classify_spec" src/golem/cli.py && echo "IMPORT: PASS" || echo "IMPORT: FAIL"

# 2. --no-classify flag is present in CLI help
uv run golem run --help | grep -q "no-classify" && echo "FLAG: PASS" || echo "FLAG: FAIL"

# 3. cli module imports cleanly
uv run python -c "import golem.cli; print('CLI_IMPORT: PASS')" || echo "CLI_IMPORT: FAIL"

# 4. TRIVIAL shortcut code path exists in cli.py
grep -q "skip_tech_lead" src/golem/cli.py && echo "TRIVIAL_PATH: PASS" || echo "TRIVIAL_PATH: FAIL"
```

Expected output:
```
IMPORT: PASS
FLAG: PASS
CLI_IMPORT: PASS
TRIVIAL_PATH: PASS
```

If any check fails, fix the issue and re-run ALL checks before proceeding.

---

### Task 5: Add `log_classification` to `src/golem/progress.py`

**Files:**
- Modify: `src/golem/progress.py`

- [ ] **Step 1: Add `log_classification` method**

Add the following method to the progress logger class in `progress.py`:

```python
def log_classification(self, complexity: str, reasoning: str) -> None:
    self._write(f"CLASSIFICATION complexity={complexity} reasoning={reasoning}")
```

- [ ] **Step 2: Call `log_classification` from `cli.py` after classification**

In `cli.py`, after calling `config.apply_complexity_profile(classification.complexity)`, call:

```python
progress_logger.log_classification(classification.complexity, classification.reasoning)
```

(Use whatever variable name holds the progress logger instance in `cli.py`.)

- [ ] **Step 3: Commit**

```bash
git add src/golem/progress.py src/golem/cli.py
git commit -m "feat: log classification event to progress.log"
```

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. log_classification method exists in progress.py
grep -q "log_classification" src/golem/progress.py && echo "METHOD: PASS" || echo "METHOD: FAIL"

# 2. progress module imports cleanly
uv run python -c "import golem.progress; print('PROGRESS_IMPORT: PASS')" || echo "PROGRESS_IMPORT: FAIL"

# 3. log_classification is callable and writes correct format
uv run python -c "
import tempfile, pathlib
from golem.progress import ProgressLogger
with tempfile.TemporaryDirectory() as td:
    log_path = pathlib.Path(td) / 'progress.log'
    logger = ProgressLogger(log_path)
    logger.log_classification('TRIVIAL', 'short spec')
    content = log_path.read_text(encoding='utf-8')
    assert 'CLASSIFICATION' in content, f'missing CLASSIFICATION in: {content}'
    assert 'TRIVIAL' in content
    print('LOG_WRITE: PASS')
" || echo "LOG_WRITE: FAIL"
```

Expected output:
```
METHOD: PASS
PROGRESS_IMPORT: PASS
LOG_WRITE: PASS
```

If any check fails, fix the issue and re-run ALL checks before proceeding.

---

### Task 6: Write tests for conductor and config

**Files:**
- Create: `tests/test_conductor.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Create `tests/test_conductor.py`**

Create `tests/test_conductor.py` with the following tests:

- `test_classify_trivial` — short spec (< 500 chars) with "typo" keyword, <=2 file mentions → `TRIVIAL`
- `test_classify_simple` — moderate spec with "config" keyword, 2-3 file mentions, < 1500 chars → `SIMPLE`
- `test_classify_standard` — default classification for moderate spec (no special keywords, 4-10 files, 1500-5000 chars) → `STANDARD`
- `test_classify_critical_keywords` — spec with "auth" + "migration" keywords (2 critical hits) → `CRITICAL`
- `test_classify_critical_by_file_count` — spec with 10+ file mentions → `CRITICAL`
- `test_classify_defaults_to_standard` — ambiguous spec with no matching keywords → `STANDARD`
- `test_classify_empty_spec` — empty string `""` → `STANDARD`

Each test must assert:
1. `result.complexity == "EXPECTED_LEVEL"`
2. `isinstance(result.reasoning, str)`
3. `0.0 <= result.confidence <= 1.0`

- [ ] **Step 2: Add tests to `tests/test_config.py`**

Add the following three test functions to the existing `tests/test_config.py`:

- `test_apply_complexity_profile_trivial` — create `GolemConfig()`, call `apply_complexity_profile("TRIVIAL")`, assert `planner_max_turns == 10` and `planner_model` contains "haiku"
- `test_apply_complexity_profile_unknown` — create `GolemConfig()`, call `apply_complexity_profile("UNKNOWN")`, assert `planner_max_turns == 50` (unchanged defaults)
- `test_complexity_profiles_roundtrip` — create `GolemConfig()`, serialize to dict (via `dataclasses.asdict` or JSON), reconstruct, assert all 4 profile keys present

- [ ] **Step 3: Commit**

```bash
git add tests/test_conductor.py tests/test_config.py
git commit -m "test: add conductor classification tests and config profile tests"
```

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. test_conductor.py exists and is non-empty
test -s tests/test_conductor.py && echo "TEST_FILE: PASS" || echo "TEST_FILE: FAIL"

# 2. All conductor tests pass (7 tests)
uv run pytest tests/test_conductor.py -v 2>&1 | tail -5
uv run pytest tests/test_conductor.py --tb=short -q 2>&1 | grep -E "passed|failed|error" | head -3
uv run pytest tests/test_conductor.py -q 2>&1 | grep "7 passed" && echo "CONDUCTOR_TESTS: PASS" || echo "CONDUCTOR_TESTS: FAIL"

# 3. New config tests pass
uv run pytest tests/test_config.py -k "apply_complexity or complexity_profiles_roundtrip" -q 2>&1 | grep "3 passed" && echo "CONFIG_TESTS: PASS" || echo "CONFIG_TESTS: FAIL"
```

Expected output:
```
TEST_FILE: PASS
CONDUCTOR_TESTS: PASS
CONFIG_TESTS: PASS
```

If any check fails, fix the issue and re-run ALL checks before proceeding.

---

## Phase Completion Gate

Run this after all tasks are complete to verify the full implementation end-to-end.

```bash
cd F:/Tools/Projects/golem-cli

# 1. All source files exist
test -s src/golem/conductor.py && echo "CONDUCTOR_FILE: PASS" || echo "CONDUCTOR_FILE: FAIL"

# 2. All modules import cleanly
uv run python -c "
from golem.conductor import classify_spec, ClassificationResult
from golem.config import GolemConfig, ComplexityProfile
import golem.planner
import golem.cli
import golem.progress
print('ALL_IMPORTS: PASS')
" || echo "ALL_IMPORTS: FAIL"

# 3. planner.py no longer hardcodes max_turns=50
grep -n "max_turns=50" src/golem/planner.py && echo "HARDCODE: FAIL" || echo "HARDCODE: PASS"

# 4. cli.py has all three new additions
grep -q "from golem.conductor import classify_spec" src/golem/cli.py && \
grep -q "no-classify" src/golem/cli.py && \
grep -q "skip_tech_lead" src/golem/cli.py && \
echo "CLI_WIRING: PASS" || echo "CLI_WIRING: FAIL"

# 5. progress.py has log_classification
grep -q "log_classification" src/golem/progress.py && echo "PROGRESS_METHOD: PASS" || echo "PROGRESS_METHOD: FAIL"

# 6. Full test suite passes with no regressions (expect >= 266 tests: 259 existing + 7 new conductor + 3 new config = 269 minimum)
uv run pytest --tb=short -q 2>&1 | tail -5
uv run pytest -q 2>&1 | grep -E "^\d+ passed" | awk -F' ' '{if ($1 >= 266) print "TOTAL_TESTS: PASS"; else print "TOTAL_TESTS: FAIL (got " $1 " expected >= 266)"}'

# 7. --no-classify flag visible in help
uv run golem run --help | grep -q "no-classify" && echo "CLI_FLAG: PASS" || echo "CLI_FLAG: FAIL"
```

Expected output:
```
CONDUCTOR_FILE: PASS
ALL_IMPORTS: PASS
HARDCODE: PASS
CLI_WIRING: PASS
PROGRESS_METHOD: PASS
TOTAL_TESTS: PASS
CLI_FLAG: PASS
```

If any check fails, fix the issue and re-run ALL checks before proceeding.

---

## Acceptance Criteria

- [ ] `classify_spec()` returns TRIVIAL, SIMPLE, STANDARD, or CRITICAL with reasoning
- [ ] `GolemConfig` has `conductor_enabled`, `planner_max_turns`, and `complexity_profiles` fields
- [ ] `apply_complexity_profile()` mutates config fields based on classification
- [ ] `cli.py` runs classification before `run_planner()` (when enabled and not --force)
- [ ] TRIVIAL specs skip Tech Lead and dispatch a single Junior Dev
- [ ] `planner.py` uses `config.planner_max_turns` instead of hardcoded 50
- [ ] `--no-classify` flag skips classification (runs STANDARD pipeline)
- [ ] Classification is logged to progress.log and printed to console
- [ ] All new tests pass
- [ ] Existing test suite passes (no regressions)
