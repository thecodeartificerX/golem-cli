# Run Economics & Observability

## Problem

Golem has zero visibility into run costs. Every SDK session returns `ResultMessage.total_cost_usd` and `ResultMessage.usage` with detailed token counts — but we ignore them entirely. Operators have no idea whether a run cost $0.50 or $50. They cannot identify which agents or tickets are expensive, cannot compare the cost of different complexity levels, and cannot make informed decisions about model selection.

ZeroShot publishes `TOKEN_USAGE` events after every agent task, aggregates by role via `getTokensByRole()`, and displays a running cost total in the status bar.

## Design

### Capture from ResultMessage

The Claude Agent SDK's `ResultMessage` exposes:
- `total_cost_usd: float | None` — authoritative cost for the query() call
- `usage: dict | None` — `{input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens}`
- `duration_ms: int` — wall-clock time
- `num_turns: int` — tool-use round trips

Each of the three session loops (`planner.py`, `tech_lead.py`, `writer.py`) already handles `ResultMessage` but only reads `message.result`. We extend them to also capture cost/token data.

### Store in Progress Events

Add `AGENT_COST` events to `progress.log` following the existing `key=value` format:

```
[2026-03-27T12:00:00Z] AGENT_COST role=lead_architect cost=$0.042300 input_tokens=15200 output_tokens=3800 cache_read=8500 turns=12 duration=45s
```

This is immediately visible in `golem logs -f` and the UI dashboard (SSE tails progress.log).

### Aggregate in golem stats

Parse `AGENT_COST` events from `progress.log` and display a cost breakdown table:

```
Run Economics
  Lead Architect:  $0.04  (15.2K in / 3.8K out / 12 turns / 45s)
  Tech Lead:       $1.23  (245K in / 42K out / 87 turns / 12m)
  Junior Devs:     $0.86  (4 writers, avg $0.22/ticket)
  Total:           $2.13
```

### Return cost from session runners

Change return types to include cost data:
- `run_planner()` → returns `PlannerResult(ticket_id, cost_usd, usage, duration_ms, num_turns)`
- `run_tech_lead()` → returns `TechLeadResult(cost_usd, usage, duration_ms, num_turns)`
- `spawn_writer_pair()` → returns `WriterResult(result_text, cost_usd, usage, duration_ms, num_turns)`

Use lightweight dataclasses, not raw tuples.

---

## Implementation

### Task 1: Add Cost Logging Methods to ProgressLogger

**Files:**
- Modify: `src/golem/progress.py`

- [ ] **Step 1: Add `log_agent_cost` method**

Add the following two methods to the `ProgressLogger` class at the end of `src/golem/progress.py`:

```python
def log_agent_cost(
    self,
    role: str,
    cost_usd: float,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    turns: int = 0,
    duration_s: int = 0,
) -> None:
    self._write(
        f"AGENT_COST role={role} cost=${cost_usd:.6f} "
        f"input_tokens={input_tokens} output_tokens={output_tokens} "
        f"cache_read={cache_read} turns={turns} duration={duration_s}s"
    )

def log_run_cost_summary(self, total_cost_usd: float) -> None:
    self._write(f"RUN_COST total=${total_cost_usd:.6f}")
```

- [ ] **Step 2: Commit**

```bash
git add src/golem/progress.py
git commit -m "feat: add log_agent_cost and log_run_cost_summary to ProgressLogger"
```

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. log_agent_cost method exists in progress.py
python -c "from golem.progress import ProgressLogger; assert hasattr(ProgressLogger, 'log_agent_cost'), 'missing log_agent_cost'" && echo "PROGRESS_AGENT_COST: PASS" || echo "PROGRESS_AGENT_COST: FAIL"

# 2. log_run_cost_summary method exists
python -c "from golem.progress import ProgressLogger; assert hasattr(ProgressLogger, 'log_run_cost_summary'), 'missing log_run_cost_summary'" && echo "PROGRESS_RUN_COST: PASS" || echo "PROGRESS_RUN_COST: FAIL"

# 3. AGENT_COST line written with correct format
python -c "
import tempfile, re
from pathlib import Path
from golem.progress import ProgressLogger
with tempfile.TemporaryDirectory() as d:
    logger = ProgressLogger(Path(d))
    logger.log_agent_cost('lead_architect', 0.042300, 15200, 3800, cache_read=8500, turns=12, duration_s=45)
    content = (Path(d) / 'progress.log').read_text(encoding='utf-8')
    assert 'AGENT_COST' in content
    assert 'role=lead_architect' in content
    assert 'cost=\$' in content
    assert 'input_tokens=15200' in content
    assert 'output_tokens=3800' in content
    print('AGENT_COST_FORMAT: PASS')
" || echo "AGENT_COST_FORMAT: FAIL"

# 4. RUN_COST line written with correct format
python -c "
import tempfile
from pathlib import Path
from golem.progress import ProgressLogger
with tempfile.TemporaryDirectory() as d:
    logger = ProgressLogger(Path(d))
    logger.log_run_cost_summary(2.13)
    content = (Path(d) / 'progress.log').read_text(encoding='utf-8')
    assert 'RUN_COST total=\$' in content
    print('RUN_COST_FORMAT: PASS')
" || echo "RUN_COST_FORMAT: FAIL"
```

Expected output:
```
PROGRESS_AGENT_COST: PASS
PROGRESS_RUN_COST: PASS
AGENT_COST_FORMAT: PASS
RUN_COST_FORMAT: PASS
```

If any check fails, fix the issue and re-run ALL checks before proceeding.

---

### Task 2: Add Result Dataclasses to planner.py, tech_lead.py, writer.py

**Files:**
- Modify: `src/golem/planner.py`
- Modify: `src/golem/tech_lead.py`
- Modify: `src/golem/writer.py`

- [ ] **Step 1: Add `PlannerResult` dataclass and update `run_planner` return type**

In `src/golem/planner.py`, add the `dataclass` import and the `PlannerResult` dataclass near the top of the file (after existing imports):

```python
from dataclasses import dataclass, field

@dataclass
class PlannerResult:
    ticket_id: str
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    num_turns: int = 0
    duration_ms: int = 0
```

Change the `run_planner` function signature return type from `str` to `PlannerResult`:

```python
async def run_planner(
    spec_path: Path,
    golem_dir: Path,
    config: GolemConfig,
    repo_root: Path | None = None,
) -> PlannerResult:
```

In the session loop, update the `ResultMessage` handler to capture cost fields (replacing the existing `elif isinstance(message, ResultMessage)` block):

```python
elif isinstance(message, ResultMessage):
    # Capture cost/tokens regardless of result text
    cost_usd = message.total_cost_usd or 0.0
    usage = message.usage or {}
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    cache_read = usage.get("cache_read_input_tokens", 0)
    num_turns = message.num_turns
    duration_ms = message.duration_ms

    if message.result:
        preview = message.result[:120].replace("\n", " ")
        print(f"[LEAD ARCHITECT] result: {preview}", file=sys.stderr)
```

Declare these variables with zero-defaults before the retry loop so they are always in scope:

```python
cost_usd: float = 0.0
input_tokens: int = 0
output_tokens: int = 0
cache_read: int = 0
num_turns: int = 0
duration_ms: int = 0
```

After the session loop (just before or after the `store.list_tickets()` call, once the ticket_id is known), log the cost and build the return value:

```python
from golem.progress import ProgressLogger
progress = ProgressLogger(golem_dir)
progress.log_agent_cost(
    role="lead_architect",
    cost_usd=cost_usd,
    input_tokens=input_tokens,
    output_tokens=output_tokens,
    cache_read=cache_read,
    turns=num_turns,
    duration_s=duration_ms // 1000,
)
```

Change the final `return ticket_id` (string) to:

```python
return PlannerResult(
    ticket_id=ticket_id,
    cost_usd=cost_usd,
    input_tokens=input_tokens,
    output_tokens=output_tokens,
    cache_read_tokens=cache_read,
    num_turns=num_turns,
    duration_ms=duration_ms,
)
```

- [ ] **Step 2: Add `TechLeadResult` dataclass and update `run_tech_lead` return type**

In `src/golem/tech_lead.py`, add the `dataclass` import and the `TechLeadResult` dataclass:

```python
from dataclasses import dataclass

@dataclass
class TechLeadResult:
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    num_turns: int = 0
    duration_ms: int = 0
```

Apply the same pattern as planner.py: declare zero-default cost variables before the retry loop, capture them in the `ResultMessage` handler, log with `progress.log_agent_cost(role="tech_lead", ...)` after the session, and return `TechLeadResult(...)` instead of `None`.

Change the `run_tech_lead` function return type annotation from `None` to `TechLeadResult`.

- [ ] **Step 3: Add `WriterResult` dataclass and update `spawn_writer_pair` return type**

In `src/golem/writer.py`, add the `dataclass` import and the `WriterResult` dataclass:

```python
from dataclasses import dataclass

@dataclass
class WriterResult:
    result_text: str = ""
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    num_turns: int = 0
    duration_ms: int = 0
```

Apply the same pattern: declare zero-default cost variables before the retry loop, capture in the `ResultMessage` handler, log with `progress.log_agent_cost(role=f"junior_dev/{ticket.id}", ...)` using the golem_dir progress logger, and return `WriterResult(result_text=result_text, ...)` instead of the bare string.

Change `spawn_writer_pair` return type annotation from `str` to `WriterResult`.

- [ ] **Step 4: Commit**

```bash
git add src/golem/planner.py src/golem/tech_lead.py src/golem/writer.py
git commit -m "feat: add PlannerResult, TechLeadResult, WriterResult dataclasses with cost fields"
```

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. PlannerResult dataclass importable with correct fields
python -c "
from golem.planner import PlannerResult
r = PlannerResult(ticket_id='TICKET-001')
assert r.ticket_id == 'TICKET-001'
assert r.cost_usd == 0.0
assert r.input_tokens == 0
assert r.output_tokens == 0
assert r.cache_read_tokens == 0
assert r.num_turns == 0
assert r.duration_ms == 0
print('PLANNER_RESULT: PASS')
" || echo "PLANNER_RESULT: FAIL"

# 2. TechLeadResult dataclass importable with correct fields
python -c "
from golem.tech_lead import TechLeadResult
r = TechLeadResult()
assert r.cost_usd == 0.0
assert r.input_tokens == 0
assert r.output_tokens == 0
assert r.cache_read_tokens == 0
assert r.num_turns == 0
assert r.duration_ms == 0
print('TECH_LEAD_RESULT: PASS')
" || echo "TECH_LEAD_RESULT: FAIL"

# 3. WriterResult dataclass importable with correct fields
python -c "
from golem.writer import WriterResult
r = WriterResult()
assert r.result_text == ''
assert r.cost_usd == 0.0
assert r.input_tokens == 0
assert r.output_tokens == 0
assert r.cache_read_tokens == 0
assert r.num_turns == 0
assert r.duration_ms == 0
print('WRITER_RESULT: PASS')
" || echo "WRITER_RESULT: FAIL"

# 4. Return type annotation on run_planner is PlannerResult (not str)
python -c "
import inspect, typing
from golem.planner import run_planner, PlannerResult
hints = typing.get_type_hints(run_planner)
assert hints.get('return') is PlannerResult, f'expected PlannerResult, got {hints.get(\"return\")}'
print('PLANNER_RETURN_TYPE: PASS')
" || echo "PLANNER_RETURN_TYPE: FAIL"

# 5. Return type annotation on spawn_writer_pair is WriterResult (not str)
python -c "
import inspect, typing
from golem.writer import spawn_writer_pair, WriterResult
hints = typing.get_type_hints(spawn_writer_pair)
assert hints.get('return') is WriterResult, f'expected WriterResult, got {hints.get(\"return\")}'
print('WRITER_RETURN_TYPE: PASS')
" || echo "WRITER_RETURN_TYPE: FAIL"
```

Expected output:
```
PLANNER_RESULT: PASS
TECH_LEAD_RESULT: PASS
WRITER_RESULT: PASS
PLANNER_RETURN_TYPE: PASS
WRITER_RETURN_TYPE: PASS
```

If any check fails, fix the issue and re-run ALL checks before proceeding.

---

### Task 3: Update cli.py — Cost Summary in `run`, Cost Table in `stats`

**Files:**
- Modify: `src/golem/cli.py`

- [ ] **Step 1: Capture cost results in the `run` command**

In `cli.py`, the `_run_async()` function calls `run_planner()` and `run_tech_lead()`. Update the call sites to capture `PlannerResult` and `TechLeadResult`.

Currently `run_planner` returns a `str` (ticket_id). After Task 2, it returns a `PlannerResult`. Update the call site:

```python
planner_result = await run_planner(spec_path, golem_dir, config, repo_root=project_root)
ticket_id = planner_result.ticket_id
```

Similarly for `run_tech_lead`, capture the `TechLeadResult`:

```python
tech_lead_result = await run_tech_lead(ticket_id, golem_dir, config, project_root)
```

After both complete, log the run cost summary and print a console line:

```python
total_cost = (planner_result.cost_usd or 0.0) + (tech_lead_result.cost_usd or 0.0)
progress.log_run_cost_summary(total_cost)
if total_cost > 0:
    console.print(f"[dim]Run cost: ${total_cost:.4f}[/dim]")
```

- [ ] **Step 2: Add `_parse_cost_events` helper and cost table to `stats` command**

Add the `_parse_cost_events` helper function to `cli.py` (at module level, before the `stats` command):

```python
def _parse_cost_events(golem_dir: Path) -> list[dict[str, str]]:
    """Parse AGENT_COST events from progress.log."""
    log_path = golem_dir / "progress.log"
    if not log_path.exists():
        return []
    events: list[dict[str, str]] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if "AGENT_COST" not in line:
            continue
        # Parse key=value pairs after AGENT_COST
        parts = line.split("AGENT_COST", 1)[1].strip()
        event: dict[str, str] = {}
        for pair in parts.split():
            if "=" in pair:
                k, v = pair.split("=", 1)
                event[k] = v
        events.append(event)
    return events
```

In the `stats` command body, after the existing ticket statistics table, add cost breakdown logic:

```python
cost_events = _parse_cost_events(golem_dir)
if cost_events:
    cost_table = Table(title="Run Economics", show_header=True, header_style="bold cyan")
    cost_table.add_column("Role", style="dim")
    cost_table.add_column("Cost", justify="right")
    cost_table.add_column("Details", style="dim")

    role_totals: dict[str, float] = {}
    role_details: dict[str, dict[str, int]] = {}
    for event in cost_events:
        role = event.get("role", "unknown")
        cost_str = event.get("cost", "$0").lstrip("$")
        try:
            cost = float(cost_str)
        except ValueError:
            cost = 0.0
        role_totals[role] = role_totals.get(role, 0.0) + cost
        if role not in role_details:
            role_details[role] = {"input_tokens": 0, "output_tokens": 0, "turns": 0}
        try:
            role_details[role]["input_tokens"] += int(event.get("input_tokens", 0))
            role_details[role]["output_tokens"] += int(event.get("output_tokens", 0))
            role_details[role]["turns"] += int(event.get("turns", 0))
        except ValueError:
            pass

    total_cost = sum(role_totals.values())
    for role, cost in sorted(role_totals.items()):
        d = role_details.get(role, {})
        in_k = d.get("input_tokens", 0) / 1000
        out_k = d.get("output_tokens", 0) / 1000
        turns = d.get("turns", 0)
        details = f"{in_k:.1f}K in / {out_k:.1f}K out / {turns} turns"
        cost_table.add_row(role, f"${cost:.4f}", details)
    cost_table.add_row("Total", f"${total_cost:.4f}", "", style="bold")
    console.print(cost_table)
```

- [ ] **Step 3: Commit**

```bash
git add src/golem/cli.py
git commit -m "feat: capture run cost in cli run command, add cost breakdown table to stats"
```

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. _parse_cost_events function is importable from cli module internals
python -c "
import importlib, sys
# Import the module directly to access module-level functions
import golem.cli as cli_module
assert hasattr(cli_module, '_parse_cost_events'), 'missing _parse_cost_events in cli'
print('PARSE_COST_EVENTS: PASS')
" || echo "PARSE_COST_EVENTS: FAIL"

# 2. _parse_cost_events returns empty list for nonexistent log
python -c "
from pathlib import Path
import tempfile
from golem.cli import _parse_cost_events
with tempfile.TemporaryDirectory() as d:
    result = _parse_cost_events(Path(d))
    assert result == [], f'expected [], got {result}'
    print('PARSE_COST_NO_LOG: PASS')
" || echo "PARSE_COST_NO_LOG: FAIL"

# 3. _parse_cost_events parses AGENT_COST lines correctly
python -c "
from pathlib import Path
import tempfile
from golem.cli import _parse_cost_events
with tempfile.TemporaryDirectory() as d:
    log = Path(d) / 'progress.log'
    log.write_text('[2026-03-27T12:00:00Z] AGENT_COST role=lead_architect cost=\$0.042300 input_tokens=15200 output_tokens=3800 cache_read=8500 turns=12 duration=45s\n', encoding='utf-8')
    events = _parse_cost_events(Path(d))
    assert len(events) == 1
    assert events[0]['role'] == 'lead_architect'
    assert events[0]['input_tokens'] == '15200'
    print('PARSE_COST_PARSE: PASS')
" || echo "PARSE_COST_PARSE: FAIL"
```

Expected output:
```
PARSE_COST_EVENTS: PASS
PARSE_COST_NO_LOG: PASS
PARSE_COST_PARSE: PASS
```

If any check fails, fix the issue and re-run ALL checks before proceeding.

---

### Task 4: Write Tests

**Files:**
- Modify: `tests/test_progress.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_planner.py`
- Modify: `tests/test_writer.py`

- [ ] **Step 1: Add tests to `tests/test_progress.py`**

Append the following two tests. Note: the existing tests use `tempfile.TemporaryDirectory()` — match that pattern here (do NOT use `tmp_path` in this file since the existing tests don't):

```python
def test_log_agent_cost_format() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        logger = ProgressLogger(Path(tmpdir))
        logger.log_agent_cost(
            role="lead_architect",
            cost_usd=0.0423,
            input_tokens=15200,
            output_tokens=3800,
            cache_read=8500,
            turns=12,
            duration_s=45,
        )
        content = (Path(tmpdir) / "progress.log").read_text(encoding="utf-8")
        assert "AGENT_COST" in content
        assert "role=lead_architect" in content
        assert "cost=$" in content
        assert "input_tokens=15200" in content
        assert "output_tokens=3800" in content


def test_log_run_cost_summary_format() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        logger = ProgressLogger(Path(tmpdir))
        logger.log_run_cost_summary(2.134567)
        content = (Path(tmpdir) / "progress.log").read_text(encoding="utf-8")
        assert "RUN_COST total=$" in content
```

- [ ] **Step 2: Add tests to `tests/test_cli.py`**

Add the following two tests. Use the `tmp_path` fixture and the existing CLI testing patterns in the file (check how `runner.invoke` is used and match it). Mock `run_planner` and `run_tech_lead` to avoid hanging. Import `_parse_cost_events` directly from `golem.cli`:

```python
def test_stats_shows_cost_breakdown(tmp_path: Path) -> None:
    """stats command displays cost table when AGENT_COST events exist."""
    from typer.testing import CliRunner
    from golem.cli import app, _parse_cost_events

    golem_dir = tmp_path / ".golem"
    golem_dir.mkdir()
    (golem_dir / "tickets").mkdir()
    log = golem_dir / "progress.log"
    log.write_text(
        "[2026-03-27T12:00:00Z] AGENT_COST role=lead_architect cost=$0.042300 "
        "input_tokens=15200 output_tokens=3800 cache_read=8500 turns=12 duration=45s\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    with patch("golem.cli._get_project_root", return_value=tmp_path):
        result = runner.invoke(app, ["stats"])
    assert result.exit_code == 0
    assert "lead_architect" in result.output or "Run Economics" in result.output


def test_stats_handles_no_cost_events(tmp_path: Path) -> None:
    """stats command works without AGENT_COST events in progress.log."""
    from typer.testing import CliRunner
    from golem.cli import app

    golem_dir = tmp_path / ".golem"
    golem_dir.mkdir()
    (golem_dir / "tickets").mkdir()

    runner = CliRunner()
    with patch("golem.cli._get_project_root", return_value=tmp_path):
        result = runner.invoke(app, ["stats"])
    assert result.exit_code == 0
```

- [ ] **Step 3: Add test to `tests/test_planner.py`**

Append the following test to `tests/test_planner.py`:

```python
def test_planner_result_dataclass() -> None:
    """PlannerResult has expected fields with correct defaults."""
    from golem.planner import PlannerResult
    r = PlannerResult(ticket_id="TICKET-001")
    assert r.ticket_id == "TICKET-001"
    assert r.cost_usd == 0.0
    assert r.input_tokens == 0
    assert r.output_tokens == 0
    assert r.cache_read_tokens == 0
    assert r.num_turns == 0
    assert r.duration_ms == 0
```

- [ ] **Step 4: Add test to `tests/test_writer.py`**

Append the following test to `tests/test_writer.py`:

```python
def test_writer_result_dataclass() -> None:
    """WriterResult has expected fields with correct defaults."""
    from golem.writer import WriterResult
    r = WriterResult()
    assert r.result_text == ""
    assert r.cost_usd == 0.0
    assert r.input_tokens == 0
    assert r.output_tokens == 0
    assert r.cache_read_tokens == 0
    assert r.num_turns == 0
    assert r.duration_ms == 0
```

- [ ] **Step 5: Commit**

```bash
git add tests/test_progress.py tests/test_cli.py tests/test_planner.py tests/test_writer.py
git commit -m "test: add cost event, dataclass, and stats breakdown tests"
```

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. Run new progress tests only
uv run pytest tests/test_progress.py -k "cost" -v 2>&1 | tail -10
uv run pytest tests/test_progress.py -k "cost" --tb=short -q && echo "PROGRESS_TESTS: PASS" || echo "PROGRESS_TESTS: FAIL"

# 2. Run new planner dataclass test
uv run pytest tests/test_planner.py -k "planner_result_dataclass" --tb=short -q && echo "PLANNER_DATACLASS_TEST: PASS" || echo "PLANNER_DATACLASS_TEST: FAIL"

# 3. Run new writer dataclass test
uv run pytest tests/test_writer.py -k "writer_result_dataclass" --tb=short -q && echo "WRITER_DATACLASS_TEST: PASS" || echo "WRITER_DATACLASS_TEST: FAIL"

# 4. Run new cli stats tests
uv run pytest tests/test_cli.py -k "cost" --tb=short -q && echo "CLI_COST_TESTS: PASS" || echo "CLI_COST_TESTS: FAIL"
```

Expected output:
```
PROGRESS_TESTS: PASS
PLANNER_DATACLASS_TEST: PASS
WRITER_DATACLASS_TEST: PASS
CLI_COST_TESTS: PASS
```

If any check fails, fix the issue and re-run ALL checks before proceeding.

---

## Phase Completion Gate

Run this after all four tasks are complete to verify the entire feature end to end.

```bash
cd F:/Tools/Projects/golem-cli

# 1. All new methods on ProgressLogger
python -c "
from golem.progress import ProgressLogger
assert hasattr(ProgressLogger, 'log_agent_cost')
assert hasattr(ProgressLogger, 'log_run_cost_summary')
print('PROGRESS_METHODS: PASS')
" || echo "PROGRESS_METHODS: FAIL"

# 2. All three result dataclasses importable
python -c "
from golem.planner import PlannerResult
from golem.tech_lead import TechLeadResult
from golem.writer import WriterResult
assert PlannerResult(ticket_id='X').cost_usd == 0.0
assert TechLeadResult().cost_usd == 0.0
assert WriterResult().result_text == ''
print('DATACLASSES: PASS')
" || echo "DATACLASSES: FAIL"

# 3. _parse_cost_events in cli
python -c "
from golem.cli import _parse_cost_events
from pathlib import Path
import tempfile
with tempfile.TemporaryDirectory() as d:
    log = Path(d) / 'progress.log'
    log.write_text('[ts] AGENT_COST role=tech_lead cost=\$1.230000 input_tokens=245000 output_tokens=42000 cache_read=0 turns=87 duration=720s\n', encoding='utf-8')
    events = _parse_cost_events(Path(d))
    assert events[0]['role'] == 'tech_lead'
    assert events[0]['cost'] == '\$1.230000'
    print('PARSE_COST: PASS')
" || echo "PARSE_COST: FAIL"

# 4. Full test suite — expect 265 tests (259 existing + 6 new: 2 progress + 2 cli + 1 planner + 1 writer)
uv run pytest --tb=short -q 2>&1 | tail -5
uv run pytest --tb=short -q 2>&1 | grep -E "^[0-9]+ passed" && echo "FULL_SUITE: PASS" || echo "FULL_SUITE: FAIL"

# 5. No regressions — check that all previously passing tests still pass
uv run pytest tests/test_progress.py tests/test_planner.py tests/test_writer.py tests/test_cli.py --tb=short -q && echo "NO_REGRESSIONS: PASS" || echo "NO_REGRESSIONS: FAIL"
```

Expected output:
```
PROGRESS_METHODS: PASS
DATACLASSES: PASS
PARSE_COST: PASS
FULL_SUITE: PASS
NO_REGRESSIONS: PASS
```

If any check fails, fix the issue and re-run ALL checks before proceeding.

---

## File: `src/golem/ui.py`

No changes needed — the UI's `tail_progress_log()` already streams all progress.log lines via SSE. `AGENT_COST` events will appear in the dashboard console automatically.

Optionally, add a `/api/cost` endpoint that returns parsed cost data as JSON for a future cost chart widget.

---

## Acceptance Criteria

- [ ] `ResultMessage.total_cost_usd` and `ResultMessage.usage` are captured in all 3 session loops
- [ ] `AGENT_COST` progress events are logged after each session completes
- [ ] `RUN_COST` summary event is logged at the end of the run
- [ ] `golem stats` displays a cost breakdown table (per-role and total)
- [ ] `golem stats` works correctly when no cost data exists (backward compat)
- [ ] Cost data appears in UI dashboard console via existing SSE stream
- [ ] `run_planner` returns `PlannerResult` with cost fields
- [ ] `run_tech_lead` returns `TechLeadResult` with cost fields
- [ ] `spawn_writer_pair` returns `WriterResult` with cost fields
- [ ] All new tests pass
- [ ] Existing test suite passes (no regressions)
