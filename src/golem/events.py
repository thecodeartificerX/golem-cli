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
    """Base event -- all events share these fields."""

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


@dataclass
class AgentErrorClassified(GolemEvent):
    """Emitted when RecoveryCoordinator classifies an exception or stall result."""

    role: str = ""
    label: str = ""             # ticket_id or human label
    failure_type: str = ""      # FailureType.value -- str avoids circular import
    attempt: int = 0
    error_preview: str = ""     # first 300 chars of exception message


@dataclass
class AgentRecoveryStarted(GolemEvent):
    """Emitted immediately before RecoveryCoordinator sleeps and retries."""

    role: str = ""
    label: str = ""
    failure_type: str = ""
    attempt: int = 0
    delay_s: float = 0.0


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


# -- Session continuation events --


@dataclass
class ContextExhausted(GolemEvent):
    """Emitted when a session hits context window limits and will be continued."""

    role: str = ""
    turn: int = 0
    continuation_number: int = 0   # 0 = first exhaustion, 1 = second, etc.
    session_id_segment: str = ""   # SDK session_id of the exhausted segment


@dataclass
class SessionContinued(GolemEvent):
    """Emitted when a fresh session starts after context compaction."""

    role: str = ""
    continuation_number: int = 0   # 1 = first continuation
    summary_chars: int = 0         # Length of the injected summary
    cumulative_cost_usd: float = 0.0
    cumulative_turns: int = 0


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


# -- Orchestrator events --


@dataclass
class OrchestratorStarted(GolemEvent):
    wave_count: int = 0
    ticket_count: int = 0
    wave_sizes: dict[str, int] = field(default_factory=dict)   # wave_number -> ticket_count (str keys for JSON)


@dataclass
class OrchestratorComplete(GolemEvent):
    waves_completed: int = 0
    tickets_passed: int = 0
    tickets_failed: int = 0
    tickets_skipped: int = 0
    total_cost_usd: float = 0.0
    duration_s: float = 0.0
    integration_branch: str = ""


@dataclass
class OrchestratorAborted(GolemEvent):
    reason: str = ""


@dataclass
class WaveStarted(GolemEvent):
    wave_number: int = 0
    ticket_ids: list[str] = field(default_factory=list)
    base_branch: str = ""


@dataclass
class WaveCompleted(GolemEvent):
    wave_number: int = 0
    passed: int = 0
    failed: int = 0
    merge_success: bool = False
    integration_branch: str = ""


@dataclass
class WaveFailed(GolemEvent):
    wave_number: int = 0
    reason: str = ""


@dataclass
class WaveSkipped(GolemEvent):
    wave_number: int = 0
    reason: str = ""


@dataclass
class TicketQueued(GolemEvent):
    ticket_id: str = ""
    worktree_path: str = ""


@dataclass
class MergeStarted(GolemEvent):
    wave_number: int = 0
    source_branches: list[str] = field(default_factory=list)
    target_branch: str = ""


@dataclass
class MergeCompleted(GolemEvent):
    wave_number: int = 0
    source_branches: list[str] = field(default_factory=list)
    target_branch: str = ""
    success: bool = False
    error: str = ""


@dataclass
class MergeConflictPredicted(GolemEvent):
    filename: str = ""
    branch_a: str = ""
    branch_b: str = ""
    wave_number: int = 0


@dataclass
class RateLimitBackoff(GolemEvent):
    delay_s: float = 0.0
    rate_limited_count: int = 0


# -- Edict pipeline events --


@dataclass
class EdictCreated(GolemEvent):
    edict_id: str = ""
    title: str = ""
    repo_path: str = ""


@dataclass
class EdictUpdated(GolemEvent):
    edict_id: str = ""
    old_status: str = ""
    new_status: str = ""


@dataclass
class EdictNeedsAttention(GolemEvent):
    edict_id: str = ""
    reason: str = ""
    ticket_id: str = ""


# -- Parallel executor events --


@dataclass
class SubtaskStarted(GolemEvent):
    """Emitted when a subtask begins execution inside ParallelExecutor."""

    subtask_id: str = ""       # ticket ID or planner sub-agent ID


@dataclass
class SubtaskCompleted(GolemEvent):
    """Emitted when a subtask finishes successfully inside ParallelExecutor."""

    subtask_id: str = ""
    duration_s: float = 0.0
    cost_usd: float = 0.0


@dataclass
class SubtaskFailed(GolemEvent):
    """Emitted when a subtask fails (any exception) inside ParallelExecutor."""

    subtask_id: str = ""
    error: str = ""
    rate_limited: bool = False


@dataclass
class SubtaskBatchRateLimited(GolemEvent):
    """Emitted before ParallelExecutor sleeps for exponential backoff after a rate-limited batch."""

    backoff_s: float = 0.0          # how long we will wait before the next batch
    rate_limited_count: int = 0     # cumulative count across all batches so far


# -- Event type registry --

EVENT_TYPES: dict[str, type[GolemEvent]] = {}


def _register_events() -> None:
    """Populate EVENT_TYPES from all GolemEvent subclasses (44 event types)."""
    for klass in [
        AgentSpawned, AgentText, AgentToolCall, AgentToolResult,
        AgentTurnComplete, AgentComplete, AgentStallWarning, AgentStallKill,
        AgentErrorClassified, AgentRecoveryStarted,
        SubAgentSpawned, SubAgentComplete, SkillInvoked, PlanModeEntered, TaskProgress,
        TicketCreated, TicketUpdated, QAResult, WorktreeCreated, MergeComplete,
        ContextExhausted, SessionContinued,
        SessionStart, SessionComplete, ConflictDetected,
        SubtaskStarted, SubtaskCompleted, SubtaskFailed, SubtaskBatchRateLimited,
        OrchestratorStarted, OrchestratorComplete, OrchestratorAborted,
        WaveStarted, WaveCompleted, WaveFailed, WaveSkipped,
        TicketQueued, MergeStarted, MergeCompleted, MergeConflictPredicted, RateLimitBackoff,
        EdictCreated, EdictUpdated, EdictNeedsAttention,
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
        self._lock = asyncio.Lock()

    async def emit(self, event: GolemEvent) -> None:
        line = json.dumps(event.to_dict(), default=str) + "\n"
        async with self._lock:
            def _write() -> None:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line)
            await asyncio.to_thread(_write)


class FanoutBackend:
    """Emits to multiple backends (e.g. QueueBackend + FileBackend)."""

    def __init__(self, backends: list[QueueBackend | FileBackend]) -> None:
        self.backends = backends

    async def emit(self, event: GolemEvent) -> None:
        for backend in self.backends:
            try:
                await backend.emit(event)
            except Exception:
                pass  # Never let one backend failure starve others


# -- EventBus --


class EventBus:
    """Async event emitter with pluggable backend."""

    def __init__(self, backend: QueueBackend | FileBackend | FanoutBackend, session_id: str = "") -> None:
        self.backend = backend
        self.session_id = session_id

    async def emit(self, event: GolemEvent) -> None:
        """Emit an event -- sets session_id, event_id, timestamp if empty."""
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
