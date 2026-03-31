"""Tests for golem.supervisor — ToolCallRegistry, StallConfig, supervised_session."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, ToolUseBlock

from golem.config import GolemConfig
import pytest

from golem.supervisor import (
    StallConfig,
    SupervisedResult,
    ToolCallRegistry,
    _build_stall_warning,
    build_escalated_prompt,
    stall_config_for_role,
    supervised_session,
)

# ---------------------------------------------------------------------------
# ToolCallRegistry tests
# ---------------------------------------------------------------------------


def test_registry_record_action() -> None:
    """Action tool recorded with is_action=True."""
    registry = ToolCallRegistry()
    registry.record("create_ticket", 1)
    assert len(registry.records) == 1
    assert registry.records[0].is_action is True
    assert registry.records[0].tool_name == "create_ticket"
    assert registry.records[0].turn_number == 1


def test_registry_record_mcp_prefixed_action() -> None:
    """MCP-prefixed action tool recorded with is_action=True."""
    registry = ToolCallRegistry()
    registry.record("mcp__golem__create_ticket", 1)
    assert registry.records[0].is_action is True
    registry.record("mcp__golem__list_tickets", 2)
    assert registry.records[1].is_action is True
    registry.record("mcp__golem-junior-dev__run_qa", 3)
    assert registry.records[2].is_action is True


def test_registry_record_read() -> None:
    """Non-action tool recorded with is_action=False."""
    registry = ToolCallRegistry()
    registry.record("Read", 2)
    assert len(registry.records) == 1
    assert registry.records[0].is_action is False
    assert registry.records[0].tool_name == "Read"


def test_registry_action_count() -> None:
    """action_call_count counts only action tools."""
    registry = ToolCallRegistry()
    registry.record("Read", 1)
    registry.record("Grep", 2)
    registry.record("create_ticket", 3)
    registry.record("Glob", 4)
    registry.record("run_qa", 5)
    assert registry.action_call_count() == 2
    assert registry.total_call_count() == 5


def test_registry_turns_since_last_action() -> None:
    """turns_since_last_action returns correct gap from last action."""
    registry = ToolCallRegistry()
    registry.record("Read", 1)
    registry.record("create_ticket", 5)
    registry.record("Grep", 6)
    # Last action was at turn 5; current_turn = 10 → gap = 5
    assert registry.turns_since_last_action(10) == 5


def test_registry_turns_since_no_actions() -> None:
    """Returns current_turn when no action tools have ever been called."""
    registry = ToolCallRegistry()
    registry.record("Read", 1)
    registry.record("Grep", 3)
    assert registry.turns_since_last_action(7) == 7


def test_registry_has_called() -> None:
    """has_called returns True only for tools that were called."""
    registry = ToolCallRegistry()
    registry.record("create_ticket", 1)
    registry.record("Read", 2)
    assert registry.has_called("create_ticket") is True
    assert registry.has_called("Read") is True
    assert registry.has_called("create_worktree") is False


# ---------------------------------------------------------------------------
# StallConfig tests
# ---------------------------------------------------------------------------


def test_stall_config_planner() -> None:
    """StallConfig for planner has correct thresholds."""
    sc = stall_config_for_role("planner", 50)
    assert sc.warning_pct == 0.6
    assert sc.kill_pct == 0.8
    assert "create_ticket" in sc.expected_actions
    assert sc.role == "planner"
    assert sc.max_turns == 50


def test_stall_config_tech_lead() -> None:
    """StallConfig for tech_lead has correct thresholds."""
    sc = stall_config_for_role("tech_lead", 100)
    assert sc.warning_pct == 0.3
    assert sc.kill_pct == 0.5
    assert "create_worktree" in sc.expected_actions
    assert "create_ticket" in sc.expected_actions


def test_stall_config_junior_dev() -> None:
    """StallConfig for junior_dev has correct thresholds and no expected MCP actions."""
    sc = stall_config_for_role("junior_dev", 50)
    assert sc.warning_pct == 0.3
    assert sc.kill_pct == 0.5
    assert sc.expected_actions == []


def test_stall_config_warning_turn() -> None:
    """warning_turn() returns int(max_turns * warning_pct)."""
    sc = stall_config_for_role("tech_lead", 100)
    assert sc.warning_turn() == 30


def test_stall_config_kill_turn() -> None:
    """kill_turn() returns int(max_turns * kill_pct)."""
    sc = stall_config_for_role("tech_lead", 100)
    assert sc.kill_turn() == 50


# ---------------------------------------------------------------------------
# Message builder tests
# ---------------------------------------------------------------------------


def test_build_stall_warning_tech_lead() -> None:
    """Warning message contains expected tools for tech_lead."""
    msg = _build_stall_warning("tech_lead", 35, 100, 35, ["create_worktree", "create_ticket"])
    assert "35" in msg
    assert "100" in msg
    assert "create_worktree" in msg
    assert "create_ticket" in msg
    assert "PROGRESS CHECK" in msg


def test_build_stall_warning_planner() -> None:
    """Warning message contains create_ticket for planner."""
    msg = _build_stall_warning("planner", 30, 50, 30, ["create_ticket"])
    assert "create_ticket" in msg
    assert "PROGRESS CHECK" in msg


def test_build_escalated_prompt() -> None:
    """Escalated prompt prepends CRITICAL block to original prompt."""
    original = "Do the work."
    escalated = build_escalated_prompt("planner", original, 40, ["create_ticket"])
    assert escalated.startswith("CRITICAL:")
    assert "40 turns" in escalated
    assert "create_ticket" in escalated
    assert "Do the work." in escalated


# ---------------------------------------------------------------------------
# supervised_session tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_supervised_session_normal_completion() -> None:
    """Normal session (empty generator) returns stalled=False."""
    config = GolemConfig()
    stall_cfg = stall_config_for_role("planner", 50)
    from claude_agent_sdk import ClaudeAgentOptions

    options = ClaudeAgentOptions(
        model="claude-opus-4-5",
        cwd=".",
        tools={"type": "preset", "preset": "claude_code"},
        max_turns=50,
        permission_mode="bypassPermissions",
        env={},
    )

    async def fake_query(*args, **kwargs):
        # Yield a ResultMessage directly — no stall
        yield ResultMessage(
            subtype="result",
            duration_ms=100,
            duration_api_ms=50,
            is_error=False,
            num_turns=1,
            session_id="s1",
        )

    with patch("golem.supervisor.query", side_effect=fake_query):
        result = await supervised_session(
            prompt="do work",
            options=options,
            role="planner",
            config=config,
            stall_config=stall_cfg,
        )

    assert result.stalled is False
    assert result.stall_turn is None


@pytest.mark.asyncio
async def test_supervised_session_stall_kill() -> None:
    """Session killed when kill threshold of consecutive idle turns is reached."""
    config = GolemConfig()
    # max_turns=10, kill at 50% = 5 consecutive turns without action
    stall_cfg = StallConfig(
        warning_pct=0.4,
        kill_pct=0.5,
        expected_actions=["create_ticket"],
        role="planner",
        max_turns=10,
    )
    from claude_agent_sdk import ClaudeAgentOptions

    options = ClaudeAgentOptions(
        model="claude-opus-4-5",
        cwd=".",
        tools={"type": "preset", "preset": "claude_code"},
        max_turns=10,
        permission_mode="bypassPermissions",
        env={},
    )

    call_count = 0

    async def fake_query(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        # Yield 6 AssistantMessages with a read-only tool — no action tools
        for i in range(6):
            yield AssistantMessage(
                content=[ToolUseBlock(id=f"t{i}", name="Read", input={"path": "."})],
                model="claude-opus-4-5",
            )

    with patch("golem.supervisor.query", side_effect=fake_query):
        result = await supervised_session(
            prompt="do work",
            options=options,
            role="planner",
            config=config,
            stall_config=stall_cfg,
        )

    assert result.stalled is True
    assert result.stall_turn is not None
    assert result.stall_turn >= stall_cfg.kill_turn()


@pytest.mark.asyncio
async def test_supervised_session_stall_warning_injected() -> None:
    """Warning injected at warning threshold; second session completes normally."""
    config = GolemConfig()
    # warning at 40% = 4 turns, kill at 80% = 8 turns
    stall_cfg = StallConfig(
        warning_pct=0.4,
        kill_pct=0.8,
        expected_actions=["create_ticket"],
        role="planner",
        max_turns=10,
    )
    from claude_agent_sdk import ClaudeAgentOptions

    options = ClaudeAgentOptions(
        model="claude-opus-4-5",
        cwd=".",
        tools={"type": "preset", "preset": "claude_code"},
        max_turns=10,
        permission_mode="bypassPermissions",
        env={},
    )

    async def fake_query(prompt, options=None, **kwargs):
        # Yield 5 AssistantMessages with no action tools → warning at turn 4
        for i in range(5):
            yield AssistantMessage(
                content=[ToolUseBlock(id=f"t{i}", name="Read", input={})],
                model="claude-opus-4-5",
            )
        # Session ends naturally after 5 turns
        yield ResultMessage(
            subtype="result",
            duration_ms=100,
            duration_api_ms=50,
            is_error=False,
            num_turns=5,
            session_id="s1",
            result="done",
        )

    with patch("golem.supervisor.query", side_effect=fake_query):
        result = await supervised_session(
            prompt="do work",
            options=options,
            role="planner",
            config=config,
            stall_config=stall_cfg,
        )

    # Warning threshold crossed but kill threshold not reached → stalled=False
    # (callers handle retry based on post-session content verification, not warning-only)
    assert result.stalled is False
    assert result.turns == 5


@pytest.mark.asyncio
async def test_supervised_session_action_tool_resets_counter() -> None:
    """Action tool call resets stall counter so warning is not triggered."""
    config = GolemConfig()
    # warning at 30% of 10 = 3 consecutive turns
    stall_cfg = stall_config_for_role("tech_lead", 10)
    from claude_agent_sdk import ClaudeAgentOptions

    options = ClaudeAgentOptions(
        model="claude-opus-4-5",
        cwd=".",
        tools={"type": "preset", "preset": "claude_code"},
        max_turns=10,
        permission_mode="bypassPermissions",
        env={},
    )

    async def fake_query(*args, **kwargs):
        # 2 read turns, then 1 action turn, then 2 read turns, then result
        # Should NOT trigger warning (max consecutive idle = 2, below warning_turn=3)
        for i in range(2):
            yield AssistantMessage(
                content=[ToolUseBlock(id=f"r{i}", name="Read", input={})],
                model="m",
            )
        yield AssistantMessage(
            content=[ToolUseBlock(id="a1", name="create_worktree", input={})],
            model="m",
        )
        for i in range(2):
            yield AssistantMessage(
                content=[ToolUseBlock(id=f"r2{i}", name="Read", input={})],
                model="m",
            )
        yield ResultMessage(
            subtype="result",
            duration_ms=100,
            duration_api_ms=50,
            is_error=False,
            num_turns=6,
            session_id="s1",
        )

    with patch("golem.supervisor.query", side_effect=fake_query):
        result = await supervised_session(
            prompt="do work",
            options=options,
            role="tech_lead",
            config=config,
            stall_config=stall_cfg,
        )

    assert result.stalled is False
    assert result.registry.has_called("create_worktree")
    assert result.registry.action_call_count() == 1


# ---------------------------------------------------------------------------
# Progress stall event tests
# ---------------------------------------------------------------------------


def test_progress_stall_warning_format() -> None:
    """log_stall_warning writes correct event to progress.log."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from golem.progress import ProgressLogger
        logger = ProgressLogger(Path(tmpdir))
        logger.log_stall_warning("tech_lead", 30, 100, 0)
        content = (Path(tmpdir) / "progress.log").read_text(encoding="utf-8")
        assert "STALL_WARNING" in content
        assert "role=tech_lead" in content
        assert "turn=30/100" in content
        assert "mcp_actions=0" in content


def test_progress_stall_detected_format() -> None:
    """log_stall_detected writes correct event to progress.log."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from golem.progress import ProgressLogger
        logger = ProgressLogger(Path(tmpdir))
        logger.log_stall_detected("planner", 40, 50, 2)
        content = (Path(tmpdir) / "progress.log").read_text(encoding="utf-8")
        assert "STALL_DETECTED" in content
        assert "role=planner" in content
        assert "turn=40/50" in content


def test_progress_stall_fatal_format() -> None:
    """log_stall_fatal writes correct event."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from golem.progress import ProgressLogger
        logger = ProgressLogger(Path(tmpdir))
        logger.log_stall_fatal("junior_dev", 25)
        content = (Path(tmpdir) / "progress.log").read_text(encoding="utf-8")
        assert "STALL_FATAL" in content
        assert "role=junior_dev" in content
        assert "turn=25" in content


def test_progress_stall_retry_format() -> None:
    """log_stall_retry writes correct event."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from golem.progress import ProgressLogger
        logger = ProgressLogger(Path(tmpdir))
        logger.log_stall_retry("tech_lead")
        content = (Path(tmpdir) / "progress.log").read_text(encoding="utf-8")
        assert "STALL_RETRY" in content
        assert "role=tech_lead" in content


# ---------------------------------------------------------------------------
# EventBus instrumentation tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_supervised_session_emits_agent_spawned() -> None:
    """supervised_session emits AgentSpawned when event_bus provided."""
    import asyncio
    from unittest.mock import MagicMock

    from claude_agent_sdk import ClaudeAgentOptions

    from golem.events import AgentSpawned, EventBus, QueueBackend

    queue: asyncio.Queue = asyncio.Queue()
    bus = EventBus(QueueBackend(queue), session_id="test")
    config = GolemConfig()
    stall_cfg = StallConfig(warning_pct=0.6, kill_pct=0.8, expected_actions=[], role="planner", max_turns=50)

    async def fake_query(*args, **kwargs):
        yield ResultMessage(
            subtype="result",
            duration_ms=100,
            duration_api_ms=50,
            is_error=False,
            num_turns=1,
            session_id="sdk-123",
            result="done",
            total_cost_usd=0.5,
            usage={"input_tokens": 100, "output_tokens": 50},
        )

    options = ClaudeAgentOptions(
        model="claude-opus-4-6",
        cwd=".",
        tools={"type": "preset", "preset": "claude_code"},
        max_turns=50,
        permission_mode="bypassPermissions",
        env={},
    )

    with patch("golem.supervisor.query", side_effect=fake_query):
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
    from claude_agent_sdk import ClaudeAgentOptions

    config = GolemConfig()
    stall_cfg = StallConfig(warning_pct=0.6, kill_pct=0.8, expected_actions=[], role="planner", max_turns=50)

    async def fake_query(*args, **kwargs):
        yield ResultMessage(
            subtype="result",
            duration_ms=100,
            duration_api_ms=50,
            is_error=False,
            num_turns=1,
            session_id="sdk-456",
            result="done",
            total_cost_usd=0.1,
            usage={"input_tokens": 10, "output_tokens": 5},
        )

    options = ClaudeAgentOptions(
        model="claude-opus-4-5",
        cwd=".",
        tools={"type": "preset", "preset": "claude_code"},
        max_turns=50,
        permission_mode="bypassPermissions",
        env={},
    )

    with patch("golem.supervisor.query", side_effect=fake_query):
        result = await supervised_session(
            "test prompt", options, "planner", config, stall_cfg,
        )
    assert result.result_text == "done"


# ---------------------------------------------------------------------------
# SupervisedResult new fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_supervised_session_captures_stop_reason() -> None:
    """supervised_session captures stop_reason from ResultMessage."""
    from claude_agent_sdk import ClaudeAgentOptions

    config = GolemConfig()
    stall_cfg = StallConfig(warning_pct=0.6, kill_pct=0.8, expected_actions=[], role="planner", max_turns=50)

    async def fake_query(*args, **kwargs):
        yield ResultMessage(
            subtype="result",
            duration_ms=100,
            duration_api_ms=50,
            is_error=False,
            num_turns=1,
            session_id="sdk-abc",
            result="done",
            stop_reason="max_tokens",
        )

    options = ClaudeAgentOptions(
        model="claude-opus-4-5",
        cwd=".",
        tools={"type": "preset", "preset": "claude_code"},
        max_turns=50,
        permission_mode="bypassPermissions",
        env={},
    )

    with patch("golem.supervisor.query", side_effect=fake_query):
        result = await supervised_session("test", options, "planner", config, stall_cfg)

    assert result.stop_reason == "max_tokens"
    assert result.sdk_session_id == "sdk-abc"


@pytest.mark.asyncio
async def test_supervised_session_stop_reason_none_by_default() -> None:
    """stop_reason is None when ResultMessage has no stop_reason."""
    from claude_agent_sdk import ClaudeAgentOptions

    config = GolemConfig()
    stall_cfg = StallConfig(warning_pct=0.6, kill_pct=0.8, expected_actions=[], role="planner", max_turns=50)

    async def fake_query(*args, **kwargs):
        yield ResultMessage(
            subtype="result",
            duration_ms=100,
            duration_api_ms=50,
            is_error=False,
            num_turns=1,
            session_id="s1",
            result="done",
        )

    options = ClaudeAgentOptions(
        model="claude-opus-4-5",
        cwd=".",
        tools={"type": "preset", "preset": "claude_code"},
        max_turns=50,
        permission_mode="bypassPermissions",
        env={},
    )

    with patch("golem.supervisor.query", side_effect=fake_query):
        result = await supervised_session("test", options, "planner", config, stall_cfg)

    assert result.stop_reason is None
    assert result.sdk_session_id == "s1"


# ---------------------------------------------------------------------------
# _is_context_exhausted tests
# ---------------------------------------------------------------------------


def test_is_context_exhausted_max_tokens() -> None:
    """_is_context_exhausted returns True for stop_reason='max_tokens'."""
    from golem.supervisor import ToolCallRegistry, _is_context_exhausted

    registry = ToolCallRegistry()
    result = SupervisedResult(
        result_text="",
        cost_usd=0.0,
        input_tokens=0,
        output_tokens=0,
        turns=10,
        duration_s=1.0,
        stalled=False,
        stall_turn=None,
        registry=registry,
        stop_reason="max_tokens",
        sdk_session_id="",
    )
    assert _is_context_exhausted(result) is True


def test_is_context_exhausted_context_length() -> None:
    """_is_context_exhausted returns True for stop_reason='context_length'."""
    from golem.supervisor import ToolCallRegistry, _is_context_exhausted

    registry = ToolCallRegistry()
    result = SupervisedResult(
        result_text="",
        cost_usd=0.0,
        input_tokens=0,
        output_tokens=0,
        turns=5,
        duration_s=1.0,
        stalled=False,
        stall_turn=None,
        registry=registry,
        stop_reason="context_length",
        sdk_session_id="",
    )
    assert _is_context_exhausted(result) is True


def test_is_context_exhausted_keyword_in_result_text() -> None:
    """_is_context_exhausted returns True when result_text contains exhaustion keyword."""
    from golem.supervisor import ToolCallRegistry, _is_context_exhausted

    registry = ToolCallRegistry()
    result = SupervisedResult(
        result_text="Error: context window exceeded the maximum allowed size",
        cost_usd=0.0,
        input_tokens=0,
        output_tokens=0,
        turns=5,
        duration_s=1.0,
        stalled=False,
        stall_turn=None,
        registry=registry,
        stop_reason=None,
        sdk_session_id="",
    )
    assert _is_context_exhausted(result) is True


def test_is_context_exhausted_normal_completion() -> None:
    """_is_context_exhausted returns False for normal completion."""
    from golem.supervisor import ToolCallRegistry, _is_context_exhausted

    registry = ToolCallRegistry()
    result = SupervisedResult(
        result_text="All tasks completed successfully.",
        cost_usd=0.0,
        input_tokens=0,
        output_tokens=0,
        turns=5,
        duration_s=1.0,
        stalled=False,
        stall_turn=None,
        registry=registry,
        stop_reason="end_turn",
        sdk_session_id="",
    )
    assert _is_context_exhausted(result) is False


def test_is_context_exhausted_length_stop_reason() -> None:
    """_is_context_exhausted returns True for stop_reason='length' alias."""
    from golem.supervisor import ToolCallRegistry, _is_context_exhausted

    registry = ToolCallRegistry()
    result = SupervisedResult(
        result_text="",
        cost_usd=0.0,
        input_tokens=0,
        output_tokens=0,
        turns=3,
        duration_s=0.5,
        stalled=False,
        stall_turn=None,
        registry=registry,
        stop_reason="length",
        sdk_session_id="",
    )
    assert _is_context_exhausted(result) is True


# ---------------------------------------------------------------------------
# _build_continuation_prompt tests
# ---------------------------------------------------------------------------


def test_build_continuation_prompt_format() -> None:
    """_build_continuation_prompt includes summary, continuation number, and original task."""
    from golem.supervisor import _build_continuation_prompt

    summary = "Completed: modified src/foo.py. Remaining: update tests."
    prompt = _build_continuation_prompt(summary, 1, "Do the task")

    assert "Session Continuation (1)" in prompt
    assert summary in prompt
    assert "Do the task" in prompt
    assert "Do NOT repeat completed work" in prompt


def test_build_continuation_prompt_number() -> None:
    """_build_continuation_prompt uses the correct continuation number."""
    from golem.supervisor import _build_continuation_prompt

    prompt = _build_continuation_prompt("summary", 3, "original")
    assert "Session Continuation (3)" in prompt


def test_build_continuation_prompt_truncates_original() -> None:
    """_build_continuation_prompt truncates original_prompt to 500 chars."""
    from golem.supervisor import _build_continuation_prompt

    long_prompt = "x" * 1000
    prompt = _build_continuation_prompt("summary", 1, long_prompt)
    # The original prompt section should be at most 500 chars of the long prompt
    assert "x" * 500 in prompt
    assert "x" * 501 not in prompt


# ---------------------------------------------------------------------------
# _serialize_session_messages tests
# ---------------------------------------------------------------------------


def test_serialize_session_messages_text_blocks() -> None:
    """_serialize_session_messages extracts text from content blocks."""
    from golem.supervisor import _serialize_session_messages

    class FakeMsg:
        type = "assistant"
        message = {"role": "assistant", "content": [{"type": "text", "text": "Hello world"}]}

    result = _serialize_session_messages([FakeMsg()])
    assert "[ASSISTANT]" in result
    assert "Hello world" in result


def test_serialize_session_messages_multiple() -> None:
    """_serialize_session_messages joins multiple messages with separator."""
    from golem.supervisor import _serialize_session_messages

    class FakeUser:
        type = "user"
        message = {"role": "user", "content": [{"type": "text", "text": "Do X"}]}

    class FakeAssistant:
        type = "assistant"
        message = {"role": "assistant", "content": [{"type": "text", "text": "Done"}]}

    result = _serialize_session_messages([FakeUser(), FakeAssistant()])
    assert "[USER]" in result
    assert "Do X" in result
    assert "[ASSISTANT]" in result
    assert "Done" in result
    assert "---" in result


# ---------------------------------------------------------------------------
# _raw_truncation tests
# ---------------------------------------------------------------------------


def test_raw_truncation_short_messages() -> None:
    """_raw_truncation returns full text when under max_chars."""
    from golem.supervisor import _raw_truncation

    class FakeMsg:
        type = "assistant"
        message = {"role": "assistant", "content": [{"type": "text", "text": "short"}]}

    result = _raw_truncation([FakeMsg()], max_chars=5000)
    assert "short" in result
    assert "[... truncated ...]" not in result


def test_raw_truncation_takes_last_5() -> None:
    """_raw_truncation uses only the last 5 messages."""
    from golem.supervisor import _raw_truncation

    class FakeMsg:
        def __init__(self, n: int) -> None:
            self.type = "assistant"
            self.message = {"role": "assistant", "content": [{"type": "text", "text": f"msg-{n}"}]}

    msgs = [FakeMsg(i) for i in range(10)]
    result = _raw_truncation(msgs, max_chars=50000)
    # Should include msgs 5-9 (last 5)
    assert "msg-5" in result
    assert "msg-9" in result
    # Should NOT include early messages
    assert "msg-0" not in result
    assert "msg-4" not in result


def test_raw_truncation_truncates_long_text() -> None:
    """_raw_truncation truncates text exceeding max_chars."""
    from golem.supervisor import _raw_truncation

    class FakeMsg:
        type = "assistant"
        message = {"role": "assistant", "content": [{"type": "text", "text": "x" * 10000}]}

    result = _raw_truncation([FakeMsg()], max_chars=100)
    assert "[... truncated ...]" in result
    # Result should be capped near max_chars + truncation marker
    assert len(result) < 500


# ---------------------------------------------------------------------------
# _build_minimal_fallback tests
# ---------------------------------------------------------------------------


def test_build_minimal_fallback() -> None:
    """_build_minimal_fallback returns context string with task preview."""
    from golem.supervisor import _build_minimal_fallback

    result = _build_minimal_fallback("Do important work on the project")
    assert "No transcript available" in result
    assert "Do important work on the project" in result


# ---------------------------------------------------------------------------
# ContinuationResult dataclass
# ---------------------------------------------------------------------------


def test_continuation_result_fields() -> None:
    """ContinuationResult has all required fields."""
    from golem.supervisor import ContinuationResult, ToolCallRegistry

    registry = ToolCallRegistry()
    cr = ContinuationResult(
        result_text="done",
        cost_usd=0.5,
        input_tokens=100,
        output_tokens=50,
        turns=10,
        duration_s=2.0,
        stalled=False,
        stall_turn=None,
        registry=registry,
        continuation_count=2,
        exhausted=False,
    )
    assert cr.continuation_count == 2
    assert cr.exhausted is False
    assert cr.cost_usd == 0.5
    assert cr.turns == 10


# ---------------------------------------------------------------------------
# continuation_supervised_session tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_continuation_disabled_passthrough() -> None:
    """When continuation_enabled=False, continuation_supervised_session is a thin pass-through."""
    from claude_agent_sdk import ClaudeAgentOptions

    from golem.supervisor import ContinuationResult, continuation_supervised_session

    config = GolemConfig(continuation_enabled=False)
    stall_cfg = StallConfig(warning_pct=0.6, kill_pct=0.8, expected_actions=[], role="planner", max_turns=50)

    async def fake_query(*args, **kwargs):
        yield ResultMessage(
            subtype="result",
            duration_ms=100,
            duration_api_ms=50,
            is_error=False,
            num_turns=1,
            session_id="s1",
            result="done",
            stop_reason="end_turn",
            total_cost_usd=0.1,
            usage={"input_tokens": 10, "output_tokens": 5},
        )

    options = ClaudeAgentOptions(
        model="claude-opus-4-5",
        cwd=".",
        tools={"type": "preset", "preset": "claude_code"},
        max_turns=50,
        permission_mode="bypassPermissions",
        env={},
    )

    with patch("golem.supervisor.query", side_effect=fake_query):
        result = await continuation_supervised_session(
            "do work", options, "planner", config, stall_cfg,
        )

    assert isinstance(result, ContinuationResult)
    assert result.result_text == "done"
    assert result.continuation_count == 0
    assert result.exhausted is False


@pytest.mark.asyncio
async def test_continuation_no_exhaustion_returns_immediately() -> None:
    """Normal session (no context exhaustion) returns after one iteration."""
    from claude_agent_sdk import ClaudeAgentOptions

    from golem.supervisor import ContinuationResult, continuation_supervised_session

    config = GolemConfig(continuation_enabled=True)
    stall_cfg = StallConfig(warning_pct=0.6, kill_pct=0.8, expected_actions=[], role="planner", max_turns=50)
    call_count = 0

    async def fake_query(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        yield ResultMessage(
            subtype="result",
            duration_ms=100,
            duration_api_ms=50,
            is_error=False,
            num_turns=1,
            session_id="s1",
            result="done",
            stop_reason="end_turn",
            total_cost_usd=0.2,
            usage={"input_tokens": 20, "output_tokens": 10},
        )

    options = ClaudeAgentOptions(
        model="claude-opus-4-5",
        cwd=".",
        tools={"type": "preset", "preset": "claude_code"},
        max_turns=50,
        permission_mode="bypassPermissions",
        env={},
    )

    with patch("golem.supervisor.query", side_effect=fake_query):
        result = await continuation_supervised_session(
            "do work", options, "planner", config, stall_cfg,
        )

    assert isinstance(result, ContinuationResult)
    assert result.continuation_count == 0
    assert result.exhausted is False
    assert call_count == 1  # Only one session ran


@pytest.mark.asyncio
async def test_continuation_on_max_tokens_triggers_continuation() -> None:
    """Context exhaustion triggers continuation loop and accumulates metrics."""
    from claude_agent_sdk import ClaudeAgentOptions

    from golem.supervisor import ContinuationResult, continuation_supervised_session

    config = GolemConfig(continuation_enabled=True, max_continuations=3)
    stall_cfg = StallConfig(warning_pct=0.6, kill_pct=0.8, expected_actions=[], role="planner", max_turns=50)
    call_count = 0

    async def fake_query(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First session: context exhausted
            yield ResultMessage(
                subtype="result",
                duration_ms=200,
                duration_api_ms=100,
                is_error=False,
                num_turns=5,
                session_id="s1",
                result="partial work done",
                stop_reason="max_tokens",
                total_cost_usd=0.5,
                usage={"input_tokens": 500, "output_tokens": 200},
            )
        else:
            # Second session: normal completion.
            # total_cost_usd is CUMULATIVE across all segments: 0.5 (seg1) + 0.3 (seg2) = 0.8
            yield ResultMessage(
                subtype="result",
                duration_ms=150,
                duration_api_ms=75,
                is_error=False,
                num_turns=3,
                session_id="s2",
                result="all done",
                stop_reason="end_turn",
                total_cost_usd=0.8,
                usage={"input_tokens": 300, "output_tokens": 100},
            )

    options = ClaudeAgentOptions(
        model="claude-opus-4-5",
        cwd=".",
        tools={"type": "preset", "preset": "claude_code"},
        max_turns=50,
        permission_mode="bypassPermissions",
        env={},
    )

    with patch("golem.supervisor.query", side_effect=fake_query), \
         patch("golem.supervisor.compact_session_messages", return_value="Summary of work done."):
        result = await continuation_supervised_session(
            "do work", options, "planner", config, stall_cfg,
        )

    assert isinstance(result, ContinuationResult)
    assert result.continuation_count == 1  # One continuation happened
    assert result.exhausted is False
    assert call_count == 2  # Two sessions ran
    # Metrics accumulated
    assert result.cost_usd == pytest.approx(0.8)
    assert result.input_tokens == 800
    assert result.output_tokens == 300
    assert result.result_text == "all done"


@pytest.mark.asyncio
async def test_continuation_cap_returns_exhausted() -> None:
    """When max_continuations is hit, exhausted=True is returned."""
    from claude_agent_sdk import ClaudeAgentOptions

    from golem.supervisor import ContinuationResult, continuation_supervised_session

    # max_continuations=2 means 3 total sessions (initial + 2)
    config = GolemConfig(continuation_enabled=True, max_continuations=2)
    stall_cfg = StallConfig(warning_pct=0.6, kill_pct=0.8, expected_actions=[], role="planner", max_turns=50)
    call_count = 0

    async def fake_query(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        # Every session hits max_tokens.
        # total_cost_usd is CUMULATIVE: seg1=0.1, seg2=0.2, seg3=0.3
        yield ResultMessage(
            subtype="result",
            duration_ms=100,
            duration_api_ms=50,
            is_error=False,
            num_turns=2,
            session_id=f"s{call_count}",
            result=f"partial {call_count}",
            stop_reason="max_tokens",
            total_cost_usd=call_count * 0.1,
            usage={"input_tokens": 100, "output_tokens": 50},
        )

    options = ClaudeAgentOptions(
        model="claude-opus-4-5",
        cwd=".",
        tools={"type": "preset", "preset": "claude_code"},
        max_turns=50,
        permission_mode="bypassPermissions",
        env={},
    )

    with patch("golem.supervisor.query", side_effect=fake_query), \
         patch("golem.supervisor.compact_session_messages", return_value="summary"):
        result = await continuation_supervised_session(
            "do work", options, "planner", config, stall_cfg,
        )

    assert isinstance(result, ContinuationResult)
    assert result.exhausted is True
    # 3 sessions total (initial + 2 continuations)
    assert call_count == 3
    # Metrics accumulated across all 3: seg1=0.1, seg2=0.2-0.1=0.1, seg3=0.3-0.2=0.1 → total=0.3
    assert result.cost_usd == pytest.approx(0.3)
    assert result.input_tokens == 300


@pytest.mark.asyncio
async def test_continuation_emits_context_exhausted_event() -> None:
    """ContextExhausted event is emitted when context exhaustion is detected."""
    import asyncio

    from claude_agent_sdk import ClaudeAgentOptions

    from golem.events import ContextExhausted, EventBus, QueueBackend
    from golem.supervisor import continuation_supervised_session

    config = GolemConfig(continuation_enabled=True, max_continuations=3)
    stall_cfg = StallConfig(warning_pct=0.6, kill_pct=0.8, expected_actions=[], role="planner", max_turns=50)
    queue: asyncio.Queue = asyncio.Queue()
    bus = EventBus(QueueBackend(queue), session_id="test")
    call_count = 0

    async def fake_query(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # total_cost_usd is CUMULATIVE: seg1 = 0.1
            yield ResultMessage(
                subtype="result",
                duration_ms=100,
                duration_api_ms=50,
                is_error=False,
                num_turns=2,
                session_id="s1",
                result="partial",
                stop_reason="max_tokens",
                total_cost_usd=0.1,
                usage={"input_tokens": 100, "output_tokens": 50},
            )
        else:
            # total_cost_usd is CUMULATIVE: seg1 + seg2 = 0.1 + 0.1 = 0.2
            yield ResultMessage(
                subtype="result",
                duration_ms=100,
                duration_api_ms=50,
                is_error=False,
                num_turns=2,
                session_id="s2",
                result="done",
                stop_reason="end_turn",
                total_cost_usd=0.2,
                usage={"input_tokens": 100, "output_tokens": 50},
            )

    options = ClaudeAgentOptions(
        model="claude-opus-4-5",
        cwd=".",
        tools={"type": "preset", "preset": "claude_code"},
        max_turns=50,
        permission_mode="bypassPermissions",
        env={},
    )

    with patch("golem.supervisor.query", side_effect=fake_query), \
         patch("golem.supervisor.compact_session_messages", return_value="summary"):
        await continuation_supervised_session(
            "do work", options, "planner", config, stall_cfg, event_bus=bus,
        )

    events = []
    while not queue.empty():
        events.append(queue.get_nowait())

    exhausted_events = [e for e in events if isinstance(e, ContextExhausted)]
    assert len(exhausted_events) == 1
    assert exhausted_events[0].role == "planner"
    assert exhausted_events[0].session_id_segment == "s1"


@pytest.mark.asyncio
async def test_continuation_emits_session_continued_event() -> None:
    """SessionContinued event is emitted before each continuation session."""
    import asyncio

    from claude_agent_sdk import ClaudeAgentOptions

    from golem.events import EventBus, QueueBackend, SessionContinued
    from golem.supervisor import continuation_supervised_session

    config = GolemConfig(continuation_enabled=True, max_continuations=3)
    stall_cfg = StallConfig(warning_pct=0.6, kill_pct=0.8, expected_actions=[], role="planner", max_turns=50)
    queue: asyncio.Queue = asyncio.Queue()
    bus = EventBus(QueueBackend(queue), session_id="test")
    call_count = 0

    async def fake_query(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            yield ResultMessage(
                subtype="result",
                duration_ms=100,
                duration_api_ms=50,
                is_error=False,
                num_turns=2,
                session_id="s1",
                result="partial",
                stop_reason="max_tokens",
                total_cost_usd=0.2,
                usage={"input_tokens": 200, "output_tokens": 100},
            )
        else:
            # total_cost_usd is CUMULATIVE: seg1 (0.2) + seg2 (0.1) = 0.3
            yield ResultMessage(
                subtype="result",
                duration_ms=100,
                duration_api_ms=50,
                is_error=False,
                num_turns=2,
                session_id="s2",
                result="done",
                stop_reason="end_turn",
                total_cost_usd=0.3,
                usage={"input_tokens": 100, "output_tokens": 50},
            )

    options = ClaudeAgentOptions(
        model="claude-opus-4-5",
        cwd=".",
        tools={"type": "preset", "preset": "claude_code"},
        max_turns=50,
        permission_mode="bypassPermissions",
        env={},
    )

    with patch("golem.supervisor.query", side_effect=fake_query), \
         patch("golem.supervisor.compact_session_messages", return_value="the summary"):
        await continuation_supervised_session(
            "do work", options, "planner", config, stall_cfg, event_bus=bus,
        )

    events = []
    while not queue.empty():
        events.append(queue.get_nowait())

    continued_events = [e for e in events if isinstance(e, SessionContinued)]
    assert len(continued_events) == 1
    assert continued_events[0].role == "planner"
    assert continued_events[0].continuation_number == 1
    assert continued_events[0].summary_chars == len("the summary")


# ---------------------------------------------------------------------------
# Config new fields tests
# ---------------------------------------------------------------------------


def test_config_continuation_defaults() -> None:
    """GolemConfig has correct continuation defaults."""
    config = GolemConfig()
    assert config.continuation_enabled is True
    assert config.max_continuations == 5
    assert config.continuation_model == "claude-haiku-4-5-20251001"
    assert config.continuation_summary_max_chars == 30_000
    assert config.continuation_summary_target_words == 800
    assert config.continuation_raw_truncation_chars == 3_000


def test_config_continuation_model_validation() -> None:
    """GolemConfig warns on unknown continuation_model."""
    config = GolemConfig(continuation_model="unknown-model-xyz")
    warnings = config.validate()
    assert any("continuation_model" in w for w in warnings)


def test_config_max_continuations_validation() -> None:
    """GolemConfig warns on negative max_continuations."""
    config = GolemConfig(max_continuations=-1)
    warnings = config.validate()
    assert any("max_continuations" in w for w in warnings)


def test_config_max_continuations_zero_valid() -> None:
    """max_continuations=0 is valid (disables continuations when used)."""
    config = GolemConfig(max_continuations=0)
    warnings = config.validate()
    # No warning about max_continuations
    assert not any("max_continuations" in w for w in warnings)


# ---------------------------------------------------------------------------
# Events registry tests
# ---------------------------------------------------------------------------


def test_context_exhausted_event_registered() -> None:
    """ContextExhausted is registered in EVENT_TYPES."""
    from golem.events import EVENT_TYPES, ContextExhausted

    assert "context_exhausted" in EVENT_TYPES
    assert EVENT_TYPES["context_exhausted"] is ContextExhausted


def test_session_continued_event_registered() -> None:
    """SessionContinued is registered in EVENT_TYPES."""
    from golem.events import EVENT_TYPES, SessionContinued

    assert "session_continued" in EVENT_TYPES
    assert EVENT_TYPES["session_continued"] is SessionContinued


def test_context_exhausted_roundtrip() -> None:
    """ContextExhausted serializes and deserializes correctly."""
    from golem.events import ContextExhausted, GolemEvent

    event = ContextExhausted(
        role="planner",
        turn=50,
        continuation_number=1,
        session_id_segment="sdk-abc",
    )
    d = event.to_dict()
    assert d["type"] == "context_exhausted"
    assert d["role"] == "planner"

    restored = GolemEvent.from_dict(d)
    assert isinstance(restored, ContextExhausted)
    assert restored.role == "planner"
    assert restored.turn == 50
    assert restored.session_id_segment == "sdk-abc"


def test_session_continued_roundtrip() -> None:
    """SessionContinued serializes and deserializes correctly."""
    from golem.events import GolemEvent, SessionContinued

    event = SessionContinued(
        role="tech_lead",
        continuation_number=2,
        summary_chars=1500,
        cumulative_cost_usd=1.5,
        cumulative_turns=20,
    )
    d = event.to_dict()
    assert d["type"] == "session_continued"
    assert d["continuation_number"] == 2

    restored = GolemEvent.from_dict(d)
    assert isinstance(restored, SessionContinued)
    assert restored.summary_chars == 1500
    assert restored.cumulative_cost_usd == pytest.approx(1.5)
