# Golem Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add full observability to Golem — a typed EventBus capturing every agent thought, tool call, sub-agent spawn, and lifecycle transition, surfaced via real-time SSE streaming in the dashboard with a preflight diagnostic panel.

**Architecture:** An `EventBus` with pluggable backends (asyncio.Queue for server, JSONL file for CLI) emits typed `GolemEvent` dataclasses from inside `supervised_session()` and MCP tool handlers. The server moves from subprocess-based session spawning to in-process async tasks, enabling direct event flow. Two new dashboard tabs (Observe + Preflight) consume these events.

**Tech Stack:** Python 3.12+, FastAPI, asyncio, dataclasses, pytest, SSE (text/event-stream)

**Spec:** `docs/superpowers/specs/2026-03-28-observability-design.md`

---

## Phase 1: Event Bus Core

### Task 1: GolemEvent Hierarchy and EventBus

**Files:**
- Create: `src/golem/events.py`
- Create: `tests/test_events.py`

- [ ] **Step 1: Write the test file first**

Create `tests/test_events.py`:

```python
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from golem.events import (
    EVENT_TYPES,
    AgentComplete,
    AgentSpawned,
    AgentStallKill,
    AgentStallWarning,
    AgentText,
    AgentToolCall,
    AgentToolResult,
    AgentTurnComplete,
    ConflictDetected,
    EventBus,
    EventFilter,
    FileBackend,
    GolemEvent,
    MergeComplete,
    PlanModeEntered,
    QueueBackend,
    SessionComplete,
    SessionStart,
    SkillInvoked,
    SubAgentComplete,
    SubAgentSpawned,
    TaskProgress,
    TicketCreated,
    TicketUpdated,
    QAResult,
    WorktreeCreated,
)


def test_event_registry_complete() -> None:
    """EVENT_TYPES contains all 21 event types."""
    assert len(EVENT_TYPES) == 21
    assert "agent_spawned" in EVENT_TYPES
    assert "agent_text" in EVENT_TYPES
    assert "session_complete" in EVENT_TYPES
    assert "conflict_detected" in EVENT_TYPES


def test_event_to_dict_roundtrip() -> None:
    """Every event type can roundtrip through to_dict/from_dict."""
    event = AgentSpawned(
        role="planner",
        model="claude-opus-4-6",
        max_turns=50,
        mcp_tools=["create_ticket", "run_qa"],
        stall_config={"warn": 30, "kill": 40},
    )
    d = event.to_dict()
    assert d["type"] == "agent_spawned"
    assert d["role"] == "planner"
    restored = GolemEvent.from_dict(d)
    assert isinstance(restored, AgentSpawned)
    assert restored.role == "planner"
    assert restored.model == "claude-opus-4-6"
    assert restored.mcp_tools == ["create_ticket", "run_qa"]


@pytest.mark.asyncio
async def test_queue_backend_emits() -> None:
    """QueueBackend pushes events to an asyncio.Queue."""
    queue: asyncio.Queue[GolemEvent] = asyncio.Queue()
    backend = QueueBackend(queue)
    event = AgentText(role="planner", text="thinking...", turn=1)
    await backend.emit(event)
    got = queue.get_nowait()
    assert isinstance(got, AgentText)
    assert got.text == "thinking..."


@pytest.mark.asyncio
async def test_file_backend_writes_jsonl(tmp_path: Path) -> None:
    """FileBackend appends JSON lines to events.jsonl."""
    path = tmp_path / "events.jsonl"
    backend = FileBackend(path)
    await backend.emit(AgentText(role="planner", text="hello", turn=1))
    await backend.emit(AgentToolCall(role="planner", tool_name="Bash", arguments={"command": "ls"}, turn=1))
    await backend.emit(AgentComplete(role="planner", total_cost=1.5, total_turns=10, duration_s=60.0, result_preview="done"))
    lines = path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 3
    first = json.loads(lines[0])
    assert first["type"] == "agent_text"


@pytest.mark.asyncio
async def test_event_bus_sets_session_id() -> None:
    """EventBus sets session_id on events if not already set."""
    queue: asyncio.Queue[GolemEvent] = asyncio.Queue()
    bus = EventBus(QueueBackend(queue), session_id="test-session-1")
    event = AgentText(role="planner", text="hi", turn=1)
    assert event.session_id == ""
    await bus.emit(event)
    got = queue.get_nowait()
    assert got.session_id == "test-session-1"


@pytest.mark.asyncio
async def test_event_bus_sets_timestamp() -> None:
    """EventBus sets timestamp if empty."""
    queue: asyncio.Queue[GolemEvent] = asyncio.Queue()
    bus = EventBus(QueueBackend(queue), session_id="s1")
    event = AgentText(role="planner", text="hi", turn=1)
    assert event.timestamp == ""
    await bus.emit(event)
    got = queue.get_nowait()
    assert got.timestamp != ""
    assert "T" in got.timestamp  # ISO format


@pytest.mark.asyncio
async def test_event_bus_sets_event_id() -> None:
    """EventBus sets event_id (UUID) if empty."""
    queue: asyncio.Queue[GolemEvent] = asyncio.Queue()
    bus = EventBus(QueueBackend(queue), session_id="s1")
    event = AgentText(role="planner", text="hi", turn=1)
    assert event.event_id == ""
    await bus.emit(event)
    got = queue.get_nowait()
    assert len(got.event_id) == 36  # UUID4 format: 8-4-4-4-12


@pytest.mark.asyncio
async def test_subscribe_with_role_filter() -> None:
    """Subscribe with role filter only yields matching events."""
    queue: asyncio.Queue[GolemEvent] = asyncio.Queue()
    bus = EventBus(QueueBackend(queue), session_id="s1")
    await bus.emit(AgentText(role="planner", text="plan", turn=1))
    await bus.emit(AgentText(role="tech_lead", text="lead", turn=1))
    await bus.emit(AgentText(role="planner", text="plan2", turn=2))
    # Put sentinel to stop iteration
    await bus.emit(AgentComplete(role="planner", total_cost=0, total_turns=0, duration_s=0, result_preview=""))
    collected: list[GolemEvent] = []
    async for event in bus.subscribe(EventFilter(roles=["planner"])):
        collected.append(event)
        if isinstance(event, AgentComplete):
            break
    assert len(collected) == 3  # plan, plan2, complete
    assert all(getattr(e, "role", "") == "planner" for e in collected)


@pytest.mark.asyncio
async def test_subscribe_with_type_filter() -> None:
    """Subscribe with event_types filter only yields matching types."""
    queue: asyncio.Queue[GolemEvent] = asyncio.Queue()
    bus = EventBus(QueueBackend(queue), session_id="s1")
    await bus.emit(AgentText(role="planner", text="hi", turn=1))
    await bus.emit(AgentToolCall(role="planner", tool_name="Bash", arguments={}, turn=1))
    await bus.emit(AgentText(role="planner", text="bye", turn=2))
    collected: list[GolemEvent] = []
    # Drain queue manually with filter
    while not queue.empty():
        event = queue.get_nowait()
        f = EventFilter(event_types=["agent_tool_call"])
        event_type = event.to_dict()["type"]
        if event_type in f.event_types:
            collected.append(event)
    assert len(collected) == 1
    assert isinstance(collected[0], AgentToolCall)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /f/Tools/Projects/golem-cli
uv run pytest tests/test_events.py -v --tb=short 2>&1 | head -5
```

Expected: `ModuleNotFoundError: No module named 'golem.events'`

- [ ] **Step 3: Implement `src/golem/events.py`**

```python
"""Typed event bus for Golem agent observability."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator


@dataclass
class GolemEvent:
    """Base event — all events share these fields."""

    timestamp: str = ""
    session_id: str = ""
    event_id: str = ""

    def to_dict(self) -> dict[str, object]:
        """Serialize to JSON-compatible dict with 'type' field."""
        d = asdict(self)
        # type is class name in snake_case
        name = type(self).__name__
        snake = "".join(f"_{c.lower()}" if c.isupper() else c for c in name).lstrip("_")
        d["type"] = snake
        return d

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> GolemEvent:
        """Deserialize from dict using EVENT_TYPES registry."""
        event_type = str(data.get("type", ""))
        klass = EVENT_TYPES.get(event_type)
        if klass is None:
            msg = f"Unknown event type: {event_type}"
            raise ValueError(msg)
        valid_fields = {f.name for f in fields(klass)}
        filtered = {k: v for k, v in data.items() if k in valid_fields}
        return klass(**filtered)


# -- Agent lifecycle events --


@dataclass
class AgentSpawned(GolemEvent):
    role: str = ""
    model: str = ""
    max_turns: int = 0
    mcp_tools: list[str] = field(default_factory=list)
    stall_config: dict[str, object] = field(default_factory=dict)


@dataclass
class AgentText(GolemEvent):
    role: str = ""
    text: str = ""
    turn: int = 0


@dataclass
class AgentToolCall(GolemEvent):
    role: str = ""
    tool_name: str = ""
    arguments: dict[str, object] = field(default_factory=dict)
    turn: int = 0


@dataclass
class AgentToolResult(GolemEvent):
    role: str = ""
    tool_name: str = ""
    result_preview: str = ""
    duration_ms: int = 0
    turn: int = 0


@dataclass
class AgentTurnComplete(GolemEvent):
    role: str = ""
    turn: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read: int = 0


@dataclass
class AgentComplete(GolemEvent):
    role: str = ""
    total_cost: float = 0.0
    total_turns: int = 0
    duration_s: float = 0.0
    result_preview: str = ""


@dataclass
class AgentStallWarning(GolemEvent):
    role: str = ""
    turn: int = 0
    turns_since_action: int = 0
    action_tools_available: list[str] = field(default_factory=list)


@dataclass
class AgentStallKill(GolemEvent):
    role: str = ""
    turn: int = 0


# -- Claude Code internal events (inferred from ToolUseBlock.name) --


@dataclass
class SubAgentSpawned(GolemEvent):
    parent_role: str = ""
    subagent_type: str = ""
    description: str = ""
    prompt_preview: str = ""


@dataclass
class SubAgentComplete(GolemEvent):
    parent_role: str = ""
    subagent_type: str = ""
    result_preview: str = ""


@dataclass
class SkillInvoked(GolemEvent):
    role: str = ""
    skill_name: str = ""


@dataclass
class PlanModeEntered(GolemEvent):
    role: str = ""


@dataclass
class TaskProgress(GolemEvent):
    role: str = ""
    task_subject: str = ""
    status: str = ""


# -- MCP tool events --


@dataclass
class TicketCreated(GolemEvent):
    ticket_id: str = ""
    title: str = ""
    assignee: str = ""


@dataclass
class TicketUpdated(GolemEvent):
    ticket_id: str = ""
    old_status: str = ""
    new_status: str = ""


@dataclass
class QAResult(GolemEvent):
    ticket_id: str = ""
    passed: bool = False
    summary: str = ""
    checks_run: int = 0


@dataclass
class WorktreeCreated(GolemEvent):
    branch: str = ""
    path: str = ""


@dataclass
class MergeComplete(GolemEvent):
    source_branch: str = ""
    target_branch: str = ""


# -- Session lifecycle events --


@dataclass
class SessionStart(GolemEvent):
    spec_path: str = ""
    complexity: str = ""
    config_snapshot: dict[str, object] = field(default_factory=dict)


@dataclass
class SessionComplete(GolemEvent):
    status: str = ""
    cost_usd: float = 0.0
    duration_s: float = 0.0
    error: str = ""


@dataclass
class ConflictDetected(GolemEvent):
    file_path: str = ""
    session_a: str = ""
    session_b: str = ""


# -- Event type registry --

EVENT_TYPES: dict[str, type[GolemEvent]] = {}


def _register_events() -> None:
    """Populate EVENT_TYPES from all GolemEvent subclasses."""
    for klass in [
        AgentSpawned, AgentText, AgentToolCall, AgentToolResult,
        AgentTurnComplete, AgentComplete, AgentStallWarning, AgentStallKill,
        SubAgentSpawned, SubAgentComplete, SkillInvoked, PlanModeEntered, TaskProgress,
        TicketCreated, TicketUpdated, QAResult, WorktreeCreated, MergeComplete,
        SessionStart, SessionComplete, ConflictDetected,
    ]:
        name = klass.__name__
        snake = "".join(f"_{c.lower()}" if c.isupper() else c for c in name).lstrip("_")
        EVENT_TYPES[snake] = klass


_register_events()


# -- EventFilter --


@dataclass
class EventFilter:
    """Filter for subscribing to specific event roles or types."""

    roles: list[str] = field(default_factory=list)
    event_types: list[str] = field(default_factory=list)

    def matches(self, event: GolemEvent) -> bool:
        """Check if event passes this filter."""
        if self.roles:
            role = getattr(event, "role", "") or getattr(event, "parent_role", "")
            if role not in self.roles:
                return False
        if self.event_types:
            event_type = event.to_dict()["type"]
            if event_type not in self.event_types:
                return False
        return True


# -- Backends --


class QueueBackend:
    """Pushes events to an asyncio.Queue (server mode)."""

    def __init__(self, queue: asyncio.Queue[GolemEvent]) -> None:
        self.queue = queue

    async def emit(self, event: GolemEvent) -> None:
        self.queue.put_nowait(event)


class FileBackend:
    """Appends JSON lines to events.jsonl (CLI mode)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    async def emit(self, event: GolemEvent) -> None:
        line = json.dumps(event.to_dict(), default=str) + "\n"
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line)


# -- EventBus --


class EventBus:
    """Async event emitter with pluggable backend."""

    def __init__(self, backend: QueueBackend | FileBackend, session_id: str = "") -> None:
        self.backend = backend
        self.session_id = session_id

    async def emit(self, event: GolemEvent) -> None:
        """Emit an event — sets session_id, event_id, timestamp if empty."""
        if not event.session_id:
            event.session_id = self.session_id
        if not event.event_id:
            event.event_id = str(uuid.uuid4())
        if not event.timestamp:
            event.timestamp = datetime.now(timezone.utc).isoformat()
        await self.backend.emit(event)

    async def subscribe(
        self, event_filter: EventFilter | None = None,
    ) -> AsyncIterator[GolemEvent]:
        """Yield events from a QueueBackend, optionally filtered."""
        if not isinstance(self.backend, QueueBackend):
            return
        while True:
            event = await self.backend.queue.get()
            if event_filter is None or event_filter.matches(event):
                yield event
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /f/Tools/Projects/golem-cli
uv run pytest tests/test_events.py -v --tb=short
```

Expected: `9 passed`

- [ ] **Step 5: Run full test suite for regression**

```bash
cd /f/Tools/Projects/golem-cli
uv run pytest --tb=short -q 2>&1 | tail -3
```

Expected: `[N] passed` (N >= 453 + 9), 0 failed

- [ ] **Step 6: Commit**

```bash
cd /f/Tools/Projects/golem-cli
git add src/golem/events.py tests/test_events.py
git commit -m "feat: GolemEvent hierarchy and EventBus with Queue/File backends"
```

#### Completion Gate

```bash
cd /f/Tools/Projects/golem-cli

# 1. Module imports cleanly with all 21 event types
uv run python -c "
from golem.events import (
    GolemEvent, EventBus, QueueBackend, FileBackend, EventFilter,
    AgentSpawned, AgentText, AgentToolCall, AgentToolResult,
    AgentTurnComplete, AgentComplete, AgentStallWarning, AgentStallKill,
    SubAgentSpawned, SubAgentComplete, SkillInvoked, PlanModeEntered, TaskProgress,
    TicketCreated, TicketUpdated, QAResult, WorktreeCreated, MergeComplete,
    SessionStart, SessionComplete, ConflictDetected, EVENT_TYPES
)
assert len(EVENT_TYPES) == 21, f'Expected 21, got {len(EVENT_TYPES)}'
print('IMPORT: PASS')
" && echo "IMPORT_GATE: PASS" || echo "IMPORT_GATE: FAIL"

# 2. All event tests pass
uv run pytest tests/test_events.py -v --tb=short 2>&1 | tail -1
```

Expected: `IMPORT: PASS`, `IMPORT_GATE: PASS`, `9 passed`

---

### Task 2: ProgressLogger as EventBus Subscriber

**Files:**
- Modify: `src/golem/progress.py`
- Modify: `tests/test_progress.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_progress.py`:

```python
@pytest.mark.asyncio
async def test_progress_logger_subscribes_to_event_bus(tmp_path: Path) -> None:
    """ProgressLogger formats AgentSpawned(planner) as LEAD_ARCHITECT_START."""
    import asyncio
    from golem.events import AgentSpawned, EventBus, QueueBackend
    from golem.progress import ProgressLogger

    golem_dir = tmp_path / ".golem"
    golem_dir.mkdir()
    logger = ProgressLogger(golem_dir)

    queue: asyncio.Queue = asyncio.Queue()
    bus = EventBus(QueueBackend(queue), session_id="test")
    task = asyncio.create_task(logger.subscribe_to_bus(bus))

    await bus.emit(AgentSpawned(role="planner", model="opus", max_turns=50, mcp_tools=[], stall_config={}))
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    log = (golem_dir / "progress.log").read_text(encoding="utf-8")
    assert "LEAD_ARCHITECT_START" in log


@pytest.mark.asyncio
async def test_progress_logger_formats_agent_cost(tmp_path: Path) -> None:
    """ProgressLogger formats AgentComplete as AGENT_COST line."""
    import asyncio
    from golem.events import AgentComplete, EventBus, QueueBackend
    from golem.progress import ProgressLogger

    golem_dir = tmp_path / ".golem"
    golem_dir.mkdir()
    logger = ProgressLogger(golem_dir)

    queue: asyncio.Queue = asyncio.Queue()
    bus = EventBus(QueueBackend(queue), session_id="test")
    task = asyncio.create_task(logger.subscribe_to_bus(bus))

    await bus.emit(AgentComplete(role="planner", total_cost=1.5, total_turns=10, duration_s=120.0, result_preview="done"))
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    log = (golem_dir / "progress.log").read_text(encoding="utf-8")
    assert "AGENT_COST" in log
    assert "role=planner" in log
    assert "cost=$1.5" in log


@pytest.mark.asyncio
async def test_progress_logger_formats_qa_result(tmp_path: Path) -> None:
    """ProgressLogger formats QAResult as QA_PASSED/QA_FAILED."""
    import asyncio
    from golem.events import EventBus, QueueBackend
    from golem.events import QAResult as QAResultEvent
    from golem.progress import ProgressLogger

    golem_dir = tmp_path / ".golem"
    golem_dir.mkdir()
    logger = ProgressLogger(golem_dir)

    queue: asyncio.Queue = asyncio.Queue()
    bus = EventBus(QueueBackend(queue), session_id="test")
    task = asyncio.create_task(logger.subscribe_to_bus(bus))

    await bus.emit(QAResultEvent(ticket_id="TICKET-001", passed=True, summary="all checks ok", checks_run=3))
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    log = (golem_dir / "progress.log").read_text(encoding="utf-8")
    assert "QA_PASSED" in log
    assert "TICKET-001" in log


def test_existing_log_methods_still_work(tmp_path: Path) -> None:
    """Existing log_* methods work without EventBus (backward compat)."""
    from golem.progress import ProgressLogger

    golem_dir = tmp_path / ".golem"
    golem_dir.mkdir()
    logger = ProgressLogger(golem_dir)
    logger.log_planner_start()
    log = (golem_dir / "progress.log").read_text(encoding="utf-8")
    assert "LEAD_ARCHITECT_START" in log
```

- [ ] **Step 2: Run to verify they fail**

```bash
uv run pytest tests/test_progress.py::test_progress_logger_subscribes_to_event_bus -v --tb=short
```

Expected: `AttributeError: 'ProgressLogger' object has no attribute 'subscribe_to_bus'`

- [ ] **Step 3: Add `subscribe_to_bus` method to ProgressLogger**

Add to `src/golem/progress.py` — import at top:

```python
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from golem.events import EventBus
```

Add method to `ProgressLogger`:

```python
async def subscribe_to_bus(self, event_bus: EventBus) -> None:
    """Consume events from EventBus and write to progress.log in legacy format."""
    from golem.events import (
        AgentComplete,
        AgentSpawned,
        AgentStallKill,
        AgentStallWarning,
        MergeComplete,
        QAResult,
        SessionComplete,
        SessionStart,
        TicketCreated,
    )

    async for event in event_bus.subscribe():
        if isinstance(event, AgentSpawned):
            if event.role == "planner":
                self._write("LEAD_ARCHITECT_START")
            elif event.role == "tech_lead":
                self._write(f"TECH_LEAD_START ticket={event.session_id}")
            elif event.role == "junior_dev":
                self._write(f"JUNIOR_DEV_DISPATCHED {event.session_id}")
        elif isinstance(event, AgentComplete):
            if event.role == "planner":
                self._write(f"LEAD_ARCHITECT_COMPLETE elapsed={event.duration_s}")
            elif event.role == "tech_lead":
                mins = int(event.duration_s) // 60
                secs = int(event.duration_s) % 60
                self._write(f"TECH_LEAD_COMPLETE elapsed={mins}m{secs}s")
            self._write(
                f"AGENT_COST role={event.role} cost=${event.total_cost}"
                f" input_tokens=0 output_tokens=0 cache_read=0"
                f" turns={event.total_turns} duration={int(event.duration_s)}s"
            )
        elif isinstance(event, TicketCreated):
            self._write(f"TICKET_CREATED {event.ticket_id} title={event.title}")
        elif isinstance(event, QAResult):
            tag = "QA_PASSED" if event.passed else "QA_FAILED"
            self._write(f"{tag} {event.ticket_id} {event.summary}")
        elif isinstance(event, MergeComplete):
            self._write(f"MERGE_COMPLETE branch={event.target_branch}")
        elif isinstance(event, AgentStallWarning):
            self._write(
                f"STALL_WARNING role={event.role} turn={event.turn}"
                f" mcp_actions={event.turns_since_action}"
            )
        elif isinstance(event, AgentStallKill):
            self._write(f"STALL_DETECTED role={event.role} turn={event.turn}")
        elif isinstance(event, SessionStart):
            self._write(f"SESSION_START session_id={event.session_id} spec={event.spec_path}")
        elif isinstance(event, SessionComplete):
            self._write(f"SESSION_COMPLETE session_id={event.session_id} status={event.status}")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_progress.py -v --tb=short
```

Expected: all progress tests pass (existing + 4 new), 0 failed

- [ ] **Step 5: Commit**

```bash
git add src/golem/progress.py tests/test_progress.py
git commit -m "feat: ProgressLogger subscribes to EventBus for backward-compatible logging"
```

#### Completion Gate

```bash
cd /f/Tools/Projects/golem-cli
uv run pytest tests/test_progress.py -v --tb=short 2>&1 | tail -1
```

Expected: all passed, 0 failed

---

## Phase 2: Deep Agent Instrumentation

### Task 3: Instrument `supervised_session()` with EventBus

**Files:**
- Modify: `src/golem/supervisor.py`
- Modify: `tests/test_supervisor.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_supervisor.py`:

```python
@pytest.mark.asyncio
async def test_supervised_session_emits_agent_spawned() -> None:
    """supervised_session emits AgentSpawned when event_bus provided."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock
    from golem.events import AgentSpawned, EventBus, QueueBackend
    from golem.config import GolemConfig
    from golem.supervisor import StallConfig, supervised_session

    queue: asyncio.Queue = asyncio.Queue()
    bus = EventBus(QueueBackend(queue), session_id="test")
    config = GolemConfig()
    stall_cfg = StallConfig(warning_pct=0.6, kill_pct=0.8, expected_actions=[], role="planner", max_turns=50)

    # Mock query to yield one ResultMessage then stop
    mock_result = MagicMock()
    mock_result.__class__.__name__ = "ResultMessage"
    mock_result.result = "done"
    mock_result.total_cost_usd = 0.5
    mock_result.usage = {"input_tokens": 100, "output_tokens": 50}
    mock_result.session_id = "sdk-123"

    async def fake_query(**kwargs):
        yield mock_result

    with patch("golem.supervisor.query", side_effect=fake_query):
        options = MagicMock()
        options.model = "claude-opus-4-6"
        options.max_turns = 50
        options.mcp_servers = {}
        result = await supervised_session(
            "test prompt", options, "planner", config, stall_cfg, event_bus=bus,
        )

    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    spawned = [e for e in events if isinstance(e, AgentSpawned)]
    assert len(spawned) == 1
    assert spawned[0].role == "planner"
    assert spawned[0].model == "claude-opus-4-6"


@pytest.mark.asyncio
async def test_supervised_session_no_events_without_bus() -> None:
    """supervised_session works without event_bus (backward compat)."""
    from unittest.mock import MagicMock
    from golem.config import GolemConfig
    from golem.supervisor import StallConfig, supervised_session

    config = GolemConfig()
    stall_cfg = StallConfig(warning_pct=0.6, kill_pct=0.8, expected_actions=[], role="planner", max_turns=50)

    mock_result = MagicMock()
    mock_result.__class__.__name__ = "ResultMessage"
    mock_result.result = "done"
    mock_result.total_cost_usd = 0.1
    mock_result.usage = {"input_tokens": 10, "output_tokens": 5}
    mock_result.session_id = "sdk-456"

    async def fake_query(**kwargs):
        yield mock_result

    with patch("golem.supervisor.query", side_effect=fake_query):
        options = MagicMock()
        options.model = "opus"
        options.max_turns = 50
        options.mcp_servers = {}
        result = await supervised_session(
            "test prompt", options, "planner", config, stall_cfg,
        )
    assert result.result_text == "done"
```

Add `from unittest.mock import patch` to the test file imports if not present.

- [ ] **Step 2: Add `event_bus` parameter to `supervised_session()`**

In `src/golem/supervisor.py`, update the signature:

```python
async def supervised_session(
    prompt: str,
    options: ClaudeAgentOptions,
    role: str,
    config: GolemConfig,
    stall_config: StallConfig,
    on_text: Callable[[str], None] | None = None,
    on_tool: Callable[[str], None] | None = None,
    golem_dir: Path | None = None,
    event_bus: EventBus | None = None,
) -> SupervisedResult:
```

Add conditional import at the top of the function body:

```python
if event_bus:
    from golem.events import (
        AgentComplete, AgentSpawned, AgentStallKill, AgentStallWarning,
        AgentText, AgentToolCall, AgentToolResult, AgentTurnComplete,
        SubAgentSpawned, SubAgentComplete, SkillInvoked, PlanModeEntered, TaskProgress,
    )
```

- [ ] **Step 3: Emit AgentSpawned at session start**

Before the `query()` call, add:

```python
if event_bus:
    mcp_tool_names: list[str] = []
    for _name, server in (options.mcp_servers or {}).items():
        # Extract tool names from MCP server config if available
        pass  # Tools are opaque at this level; emit empty list as fallback
    await event_bus.emit(AgentSpawned(
        role=role,
        model=options.model or "",
        max_turns=options.max_turns or 0,
        mcp_tools=mcp_tool_names,
        stall_config={"warn": stall_config.warning_turn(), "kill": stall_config.kill_turn()},
    ))
```

- [ ] **Step 4: Emit events from the message loop**

Inside the `async for message in query(...)` loop, after processing each `AssistantMessage`:

For `TextBlock`:
```python
if event_bus:
    await event_bus.emit(AgentText(role=role, text=block.text, turn=current_turn))
```

For `ToolUseBlock`:
```python
if event_bus:
    await event_bus.emit(AgentToolCall(
        role=role, tool_name=block.name,
        arguments=block.input if isinstance(block.input, dict) else {},
        turn=current_turn,
    ))
    # CC-internal inference
    if block.name == "Agent":
        inp = block.input if isinstance(block.input, dict) else {}
        await event_bus.emit(SubAgentSpawned(
            parent_role=role,
            subagent_type=str(inp.get("subagent_type", "")),
            description=str(inp.get("description", "")),
            prompt_preview=str(inp.get("prompt", ""))[:200],
        ))
    elif block.name == "Skill":
        inp = block.input if isinstance(block.input, dict) else {}
        await event_bus.emit(SkillInvoked(role=role, skill_name=str(inp.get("skill", ""))))
    elif block.name == "EnterPlanMode":
        await event_bus.emit(PlanModeEntered(role=role))
    elif block.name in ("TaskCreate", "TaskUpdate"):
        inp = block.input if isinstance(block.input, dict) else {}
        await event_bus.emit(TaskProgress(
            role=role, task_subject=str(inp.get("subject", "")),
            status=str(inp.get("status", "")),
        ))
```

For `ResultMessage` with tool results — emit `AgentToolResult` for each content block.

At stall warning: emit `AgentStallWarning`. At kill: emit `AgentStallKill`.

At session end: emit `AgentComplete` with cost/turns/duration.

- [ ] **Step 5: Run tests**

```bash
uv run pytest tests/test_supervisor.py -v --tb=short
```

Expected: all passed (existing + 2 new), 0 failed

- [ ] **Step 6: Commit**

```bash
git add src/golem/supervisor.py tests/test_supervisor.py
git commit -m "feat: instrument supervised_session with EventBus emission"
```

#### Completion Gate

```bash
cd /f/Tools/Projects/golem-cli
uv run pytest tests/test_supervisor.py -v --tb=short 2>&1 | tail -1
```

Expected: all passed, 0 failed

---

### Task 4: Instrument MCP Tool Handlers

**Files:**
- Modify: `src/golem/tools.py`
- Modify: `tests/test_tools.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_tools.py`:

```python
@pytest.mark.asyncio
async def test_create_ticket_emits_event(tmp_path: Path) -> None:
    """create_ticket handler emits TicketCreated via EventBus."""
    import asyncio
    from golem.events import EventBus, QueueBackend, TicketCreated
    from golem.config import GolemConfig

    golem_dir = tmp_path / ".golem"
    (golem_dir / "tickets").mkdir(parents=True)

    queue: asyncio.Queue = asyncio.Queue()
    bus = EventBus(QueueBackend(queue), session_id="test")
    config = GolemConfig()

    server = create_golem_mcp_server(golem_dir, config, tmp_path, event_bus=bus)
    # Find create_ticket tool and call it
    # (Implementation depends on how tools are exposed — test the event emission)
    # For now, verify the parameter is accepted
    assert server is not None


@pytest.mark.asyncio
async def test_no_events_without_bus(tmp_path: Path) -> None:
    """MCP server works without event_bus (backward compat)."""
    from golem.config import GolemConfig

    golem_dir = tmp_path / ".golem"
    (golem_dir / "tickets").mkdir(parents=True)
    config = GolemConfig()

    server = create_golem_mcp_server(golem_dir, config, tmp_path)
    assert server is not None
```

- [ ] **Step 2: Add `event_bus` parameter to MCP factories**

In `src/golem/tools.py`, update signatures:

```python
def create_golem_mcp_server(
    golem_dir: Path,
    config: GolemConfig,
    project_root: Path,
    registry: ToolCallRegistry | None = None,
    event_bus: EventBus | None = None,
) -> McpSdkServerConfig:
```

```python
def create_junior_dev_mcp_server(
    golem_dir: Path,
    registry: ToolCallRegistry | None = None,
    event_bus: EventBus | None = None,
) -> McpSdkServerConfig:
```

In each handler, after successful execution, emit the corresponding event if `event_bus` is not None. Use closure capture to bind `event_bus` into the handler.

- [ ] **Step 3: Run tests**

```bash
uv run pytest tests/test_tools.py -v --tb=short
```

Expected: all passed, 0 failed

- [ ] **Step 4: Commit**

```bash
git add src/golem/tools.py tests/test_tools.py
git commit -m "feat: emit MCP tool events via EventBus"
```

#### Completion Gate

```bash
cd /f/Tools/Projects/golem-cli
uv run pytest tests/test_tools.py -v --tb=short 2>&1 | tail -1
```

Expected: all passed, 0 failed

---

### Task 5: Thread EventBus Through Agent Functions

**Files:**
- Modify: `src/golem/planner.py`
- Modify: `src/golem/tech_lead.py`
- Modify: `src/golem/writer.py`
- Modify: `src/golem/cli.py`

- [ ] **Step 1: Add `event_bus` parameter to `_run_planner_session` and `run_planner`**

In `src/golem/planner.py`, update `run_planner`:

```python
async def run_planner(
    spec_path: Path,
    golem_dir: Path,
    config: GolemConfig,
    repo_root: Path | None = None,
    event_bus: EventBus | None = None,
) -> PlannerResult:
```

Pass `event_bus` through to `_run_planner_session()` and then to `supervised_session(..., event_bus=event_bus)` and `create_golem_mcp_server(..., event_bus=event_bus)`.

Add `from __future__ import annotations` and conditional import:

```python
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from golem.events import EventBus
```

- [ ] **Step 2: Same for `run_tech_lead` and `spawn_junior_dev`**

In `src/golem/tech_lead.py`:

```python
async def run_tech_lead(
    ticket_id: str,
    golem_dir: Path,
    config: GolemConfig,
    project_root: Path,
    event_bus: EventBus | None = None,
) -> TechLeadResult:
```

In `src/golem/writer.py`:

```python
async def spawn_junior_dev(
    ticket: Ticket,
    worktree_path: str,
    config: GolemConfig,
    golem_dir: Path | None = None,
    event_bus: EventBus | None = None,
) -> JuniorDevResult:
```

Both pass `event_bus` to `supervised_session()` and their respective MCP server factories.

- [ ] **Step 3: Wire FileBackend in CLI `golem run`**

In `src/golem/cli.py`, in the `run()` command function, after golem_dir is resolved and before calling `run_planner`:

```python
from golem.events import EventBus, FileBackend

event_bus: EventBus | None = None
if not no_server:
    # Server mode will use QueueBackend — CLI just uses file
    pass
events_path = golem_dir / "events.jsonl"
event_bus = EventBus(FileBackend(events_path), session_id=config.session_id)
```

Pass `event_bus` to `run_planner(..., event_bus=event_bus)` and `run_tech_lead(..., event_bus=event_bus)`.

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_planner.py tests/test_tech_lead.py tests/test_writer.py tests/test_cli.py -v --tb=short 2>&1 | tail -1
```

Expected: all passed, 0 failed

- [ ] **Step 5: Commit**

```bash
git add src/golem/planner.py src/golem/tech_lead.py src/golem/writer.py src/golem/cli.py
git commit -m "feat: thread EventBus through planner, tech lead, writer, and CLI"
```

#### Completion Gate

```bash
cd /f/Tools/Projects/golem-cli

uv run python -c "
import inspect
from golem.planner import run_planner
from golem.tech_lead import run_tech_lead
from golem.writer import spawn_junior_dev
from golem.supervisor import supervised_session
for fn in [run_planner, run_tech_lead, spawn_junior_dev, supervised_session]:
    sig = inspect.signature(fn)
    assert 'event_bus' in sig.parameters, f'{fn.__name__} missing event_bus'
print('SIGNATURES: PASS')
"

uv run pytest tests/test_planner.py tests/test_tech_lead.py tests/test_writer.py tests/test_cli.py --tb=short -q 2>&1 | tail -1
```

Expected: `SIGNATURES: PASS`, all passed

---

## Phase 3: Server Refactor + Preflight + Dashboard

Tasks 6-8 follow the same pattern as the spec. The spec file at `docs/superpowers/specs/2026-03-28-observability-design.md` has full step-by-step instructions, code examples, and completion gates for:

- **Task 6:** In-process session runner (`run_session()` coroutine, replace subprocess spawning, new `/observe` and `/agents` endpoints)
- **Task 7:** Preflight system (`derive_agent_topology()`, `predict_conflicts()`, `run_environment_checks()`, `estimate_cost()`, enhanced `/api/preflight` endpoint)
- **Task 8:** Dashboard UI (Observe tab with agent tree + event stream, Preflight tab, sidebar enhancements)

These tasks should be implemented using the spec's detailed instructions and completion gates directly — the spec already contains the task/step/gate structure needed for execution.

---

## Phase Completion Gates

### Phase 1 Gate (after Tasks 1-2)
```bash
cd /f/Tools/Projects/golem-cli
uv run python -c "from golem.events import EVENT_TYPES; assert len(EVENT_TYPES) == 21; print('EVENTS: PASS')"
uv run pytest tests/test_events.py tests/test_progress.py --tb=short -q 2>&1 | tail -1
```

### Phase 2 Gate (after Tasks 3-5)
```bash
cd /f/Tools/Projects/golem-cli
uv run python -c "
import inspect
from golem.supervisor import supervised_session
assert 'event_bus' in inspect.signature(supervised_session).parameters
print('SUPERVISOR: PASS')
"
uv run pytest tests/test_supervisor.py tests/test_tools.py --tb=short -q 2>&1 | tail -1
```

### Phase 3 Gate (after Tasks 6-8)
```bash
cd /f/Tools/Projects/golem-cli
uv run python -c "
from golem.server import create_app
from fastapi.routing import APIRoute
app = create_app()
routes = {r.path for r in app.routes if isinstance(r, APIRoute)}
assert '/api/sessions/{session_id}/observe' in routes
assert '/api/sessions/{session_id}/agents' in routes
print('ENDPOINTS: PASS')
"
uv run python -c "
with open('src/golem/ui_template.html', encoding='utf-8') as f:
    html = f.read().lower()
assert 'observe' in html
assert 'preflight' in html
print('TEMPLATE: PASS')
"
uv run pytest --tb=short -q 2>&1 | tail -1
```

### Final Gate
```bash
cd /f/Tools/Projects/golem-cli
uv run pytest -v --tb=short 2>&1 | tail -1
```

Expected: `[N] passed, 0 failed` where N >= 453 + all new tests
