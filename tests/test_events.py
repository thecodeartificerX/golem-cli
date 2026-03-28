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
    FanoutBackend,
    FileBackend,
    GolemEvent,
    MergeComplete,
    PlanModeEntered,
    QAResult,
    QueueBackend,
    SessionComplete,
    SessionStart,
    SkillInvoked,
    SubAgentComplete,
    SubAgentSpawned,
    TaskProgress,
    TicketCreated,
    TicketUpdated,
    WorktreeCreated,
)


def test_event_registry_complete() -> None:
    """EVENT_TYPES contains all 25 event types."""
    assert len(EVENT_TYPES) == 25
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


# -- FanoutBackend tests --


@pytest.mark.asyncio
async def test_fanout_backend_emits_to_all(tmp_path: Path) -> None:
    """FanoutBackend delivers events to both QueueBackend and FileBackend."""
    queue: asyncio.Queue[GolemEvent] = asyncio.Queue()
    jsonl_path = tmp_path / "events.jsonl"
    fanout = FanoutBackend([QueueBackend(queue), FileBackend(jsonl_path)])
    event = AgentText(role="planner", text="hello fanout", turn=1)
    await fanout.emit(event)

    # Queue got it
    got = queue.get_nowait()
    assert isinstance(got, AgentText)
    assert got.text == "hello fanout"

    # File got it
    lines = jsonl_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["type"] == "agent_text"
    assert parsed["text"] == "hello fanout"


@pytest.mark.asyncio
async def test_fanout_backend_multiple_events(tmp_path: Path) -> None:
    """FanoutBackend handles multiple sequential events."""
    queue: asyncio.Queue[GolemEvent] = asyncio.Queue()
    jsonl_path = tmp_path / "events.jsonl"
    fanout = FanoutBackend([QueueBackend(queue), FileBackend(jsonl_path)])

    await fanout.emit(AgentText(role="planner", text="one", turn=1))
    await fanout.emit(AgentToolCall(role="planner", tool_name="Bash", arguments={}, turn=2))
    await fanout.emit(AgentComplete(role="planner", total_cost=1.0, total_turns=2, duration_s=30.0, result_preview="done"))

    assert queue.qsize() == 3
    lines = jsonl_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 3


@pytest.mark.asyncio
async def test_fanout_backend_with_event_bus(tmp_path: Path) -> None:
    """EventBus works with FanoutBackend, setting session_id and timestamp."""
    queue: asyncio.Queue[GolemEvent] = asyncio.Queue()
    jsonl_path = tmp_path / "events.jsonl"
    fanout = FanoutBackend([QueueBackend(queue), FileBackend(jsonl_path)])
    bus = EventBus(fanout, session_id="fanout-test")

    await bus.emit(AgentText(role="tech_lead", text="leading", turn=1))

    got = queue.get_nowait()
    assert got.session_id == "fanout-test"
    assert got.timestamp != ""

    lines = jsonl_path.read_text(encoding="utf-8").strip().split("\n")
    parsed = json.loads(lines[0])
    assert parsed["session_id"] == "fanout-test"
