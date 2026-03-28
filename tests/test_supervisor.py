"""Tests for golem.supervisor — ToolCallRegistry, StallConfig, supervised_session."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, ToolUseBlock

from golem.config import GolemConfig
from golem.supervisor import (
    StallConfig,
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

    captured_prompts: list[str] = []
    call_count = 0

    async def fake_query(prompt, options=None, **kwargs):
        nonlocal call_count
        call_count += 1
        captured_prompts.append(prompt)
        if call_count == 1:
            # First call: yield 5 AssistantMessages with no action → warning at 4
            for i in range(5):
                yield AssistantMessage(
                    content=[ToolUseBlock(id=f"t{i}", name="Read", input={})],
                    model="claude-opus-4-5",
                )
        else:
            # Second call (after warning restart): complete normally
            yield ResultMessage(
                subtype="result",
                duration_ms=100,
                duration_api_ms=50,
                is_error=False,
                num_turns=5,
                session_id="s2",
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

    assert result.stalled is False
    # Warning should have been injected; second prompt should contain PROGRESS CHECK
    assert call_count == 2
    assert "PROGRESS CHECK" in captured_prompts[1]
    assert "do work" in captured_prompts[1]  # Original prompt preserved


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
