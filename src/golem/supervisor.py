"""Agent stall detection and recovery for Golem SDK sessions.

Provides ToolCallRegistry for tracking MCP tool calls, StallConfig for
role-specific thresholds, SupervisedResult for session outcomes, and
supervised_session() which wraps query() with circuit breakers and
auto-restart on stall detection.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, ToolUseBlock, query

from golem.config import GolemConfig

if TYPE_CHECKING:
    from golem.events import EventBus

# MCP action tool names — calls to these reset the stall counter.
# SDK exposes MCP tools as "mcp__<server>__<name>" so we strip the prefix
# before checking membership.  "Read tools" (Read, Grep, Glob, Bash) are
# not listed here.
ACTION_TOOLS: set[str] = {
    "create_ticket",
    "update_ticket",
    "read_ticket",
    "list_tickets",
    "create_worktree",
    "merge_branches",
    "commit_worktree",
    "run_qa",
}


def _is_action_tool(tool_name: str) -> bool:
    """Check if a tool name is an action tool, handling MCP prefix stripping."""
    if tool_name in ACTION_TOOLS:
        return True
    # Strip mcp__<server>__ prefix: "mcp__golem__create_ticket" → "create_ticket"
    if tool_name.startswith("mcp__"):
        bare = tool_name.split("__", 2)[-1] if tool_name.count("__") >= 2 else tool_name
        return bare in ACTION_TOOLS
    return False


@dataclass
class ToolCallRecord:
    tool_name: str
    turn_number: int
    timestamp: str
    is_action: bool


@dataclass
class ToolCallRegistry:
    records: list[ToolCallRecord] = field(default_factory=list)

    def record(self, tool_name: str, turn: int) -> None:
        """Append a tool call record. Sets is_action based on ACTION_TOOLS membership."""
        ts = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.records.append(
            ToolCallRecord(
                tool_name=tool_name,
                turn_number=turn,
                timestamp=ts,
                is_action=_is_action_tool(tool_name),
            )
        )

    def action_call_count(self) -> int:
        """Count records where is_action is True."""
        return sum(1 for r in self.records if r.is_action)

    def total_call_count(self) -> int:
        """Count all records."""
        return len(self.records)

    def turns_since_last_action(self, current_turn: int) -> int:
        """Return consecutive turns since the last action tool was called.

        Returns current_turn if no action tools have ever been called.
        """
        for r in reversed(self.records):
            if r.is_action:
                return current_turn - r.turn_number
        return current_turn

    def has_called(self, tool_name: str) -> bool:
        """Return True if tool_name appears in any record."""
        return any(r.tool_name == tool_name for r in self.records)

    def has_called_any_action(self) -> bool:
        """Return True if any action tool has been called."""
        return self.action_call_count() > 0


@dataclass
class StallConfig:
    """Role-specific stall detection thresholds.

    warning_turn() and kill_turn() return the number of consecutive turns
    without an MCP action call that triggers each threshold.
    """

    warning_pct: float
    kill_pct: float
    expected_actions: list[str]
    role: str
    max_turns: int

    def warning_turn(self) -> int:
        """Consecutive turns without action before warning injection."""
        return int(self.max_turns * self.warning_pct)

    def kill_turn(self) -> int:
        """Consecutive turns without action before session termination."""
        return int(self.max_turns * self.kill_pct)


def stall_config_for_role(role: str, max_turns: int, skip_research: bool = False) -> StallConfig:
    """Return role-specific StallConfig with default thresholds.

    Roles:
    - planner: warning at 60%, kill at 80%, expected create_ticket
    - tech_lead: warning at 30%, kill at 50%, expected create_worktree/create_ticket
    - junior_dev (or any other): warning at 30%, kill at 50%, no expected MCP actions

    When skip_research=True for the planner role, the expected action count is lower
    because sub-agent tool calls won't appear.
    """
    if role == "planner":
        expected: list[str] = ["create_ticket"]
        if not skip_research:
            expected = ["spawn explorer", "spawn researcher"] + expected
        return StallConfig(
            warning_pct=0.6,
            kill_pct=0.8,
            expected_actions=expected,
            role=role,
            max_turns=max_turns,
        )
    elif role == "tech_lead":
        return StallConfig(
            warning_pct=0.3,
            kill_pct=0.5,
            expected_actions=["create_worktree", "create_ticket"],
            role=role,
            max_turns=max_turns,
        )
    else:
        # junior_dev: verified via git diff, not MCP action calls
        return StallConfig(
            warning_pct=0.3,
            kill_pct=0.5,
            expected_actions=[],
            role=role,
            max_turns=max_turns,
        )


@dataclass
class SupervisedResult:
    result_text: str
    cost_usd: float
    input_tokens: int
    output_tokens: int
    turns: int
    duration_s: float
    stalled: bool
    stall_turn: int | None
    registry: ToolCallRegistry
    stop_reason: str | None = None   # from ResultMessage
    sdk_session_id: str = ""         # from ResultMessage.session_id


@dataclass
class ContinuationResult:
    """Merged result from one or more supervised session segments."""

    result_text: str
    cost_usd: float           # Cumulative across all segments
    input_tokens: int         # Cumulative
    output_tokens: int        # Cumulative
    turns: int                # Cumulative
    duration_s: float         # Cumulative
    stalled: bool             # True if final segment stalled
    stall_turn: int | None    # From final segment
    registry: ToolCallRegistry  # From final segment only
    continuation_count: int   # 0 = no continuation, N = N continuations used
    exhausted: bool           # True if hit max_continuations cap


# Context exhaustion detection constants
CONTEXT_EXHAUSTION_REASONS: frozenset[str] = frozenset({
    "max_tokens",
    "context_length",
    "length",
})

CONTEXT_EXHAUSTION_KEYWORDS: tuple[str, ...] = (
    "context window",
    "maximum context",
    "too long",
    "token limit",
    "context length",
)


def _is_context_exhausted(result: SupervisedResult) -> bool:
    """Return True if the session ended due to context window exhaustion."""
    # Primary: check stop_reason captured from ResultMessage
    if result.stop_reason is not None and result.stop_reason in CONTEXT_EXHAUSTION_REASONS:
        return True
    # Fallback: check result_text for exhaustion keywords
    if result.result_text:
        text_lower = result.result_text.lower()
        if any(kw in text_lower for kw in CONTEXT_EXHAUSTION_KEYWORDS):
            return True
    return False


def _build_stall_warning(
    role: str,
    current_turn: int,
    max_turns: int,
    stall_turns: int,
    expected_actions: list[str],
) -> str:
    """Build the warning message injected when the warning threshold is hit."""
    action_list = ", ".join(expected_actions) if expected_actions else "(any action tool)"
    return (
        f"PROGRESS CHECK: You have used {current_turn} of {max_turns} turns.\n"
        f"You have NOT called any action tools in {stall_turns} consecutive turns.\n"
        f"Expected action tools for {role}: {action_list}\n"
        f"You MUST take action NOW or your session will be terminated."
    )


def build_escalated_prompt(
    role: str,
    original_prompt: str,
    turns_used: int,
    expected_actions: list[str],
) -> str:
    """Prepend a CRITICAL stall warning to the original prompt for retry sessions."""
    action_list = ", ".join(expected_actions) if expected_actions else "an action tool"
    escalation = (
        f"CRITICAL: Previous session stalled after {turns_used} turns without action.\n"
        f"You MUST call {action_list} within the first 10 turns. Act immediately.\n\n"
    )
    return escalation + original_prompt


async def supervised_session(
    prompt: str,
    options: ClaudeAgentOptions,
    role: str,
    config: GolemConfig,  # noqa: ARG001 — reserved for future use (retry_delay, etc.)
    stall_config: StallConfig,
    on_text: Callable[[str], None] | None = None,
    on_tool: Callable[[str], None] | None = None,
    golem_dir: Path | None = None,
    event_bus: EventBus | None = None,
) -> SupervisedResult:
    """Run a supervised SDK session with stall detection and circuit breakers.

    - Tracks tool calls via ToolCallRegistry
    - Injects a warning message when stall_config.warning_turn() consecutive turns
      pass without any action tool being called (restarts session with warning prepended)
    - Returns SupervisedResult(stalled=True) when stall_config.kill_turn() is reached
    - Logs STALL_WARNING and STALL_DETECTED events to progress.log if golem_dir provided

    At most 2 query() iterations: initial + 1 warning-injected restart.
    """
    from golem.progress import ProgressLogger

    registry = ToolCallRegistry()
    current_turn = 0
    warned = False
    start = time.monotonic()
    result_text = ""
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    stall_turn: int | None = None
    current_prompt = prompt

    if event_bus:
        from golem.events import AgentSpawned
        await event_bus.emit(AgentSpawned(
            role=role,
            model=options.model or "",
            max_turns=options.max_turns or 0,
            mcp_tools=[],
            stall_config={"warn": stall_config.warning_turn(), "kill": stall_config.kill_turn()},
        ))

    kill_hit = False
    stop_reason: str | None = None
    sdk_session_id: str = ""

    # Single-pass query — never break out of the async generator to avoid
    # anyio cancel-scope cross-task RuntimeError on cleanup.  Stall flags
    # are set in-flight; callers handle retry with escalated prompts.
    async for message in query(prompt=current_prompt, options=options):
        if isinstance(message, AssistantMessage):
            current_turn += 1

            # After kill threshold, skip message processing but keep
            # consuming so the generator exits naturally.
            if kill_hit:
                continue

            for block in message.content:
                if isinstance(block, TextBlock):
                    if on_text:
                        on_text(block.text)
                    if event_bus:
                        from golem.events import AgentText
                        await event_bus.emit(AgentText(role=role, text=block.text, turn=current_turn))
                elif isinstance(block, ToolUseBlock):
                    registry.record(block.name, current_turn)
                    if on_tool:
                        on_tool(block.name)
                    if event_bus:
                        from golem.events import (
                            AgentToolCall, SubAgentSpawned, SkillInvoked,
                            PlanModeEntered, TaskProgress,
                        )
                        await event_bus.emit(AgentToolCall(
                            role=role, tool_name=block.name,
                            arguments=block.input if isinstance(block.input, dict) else {},
                            turn=current_turn,
                        ))
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

            turns_since = registry.turns_since_last_action(current_turn)

            # Warning threshold: log but do NOT break — let query finish naturally
            if not warned and turns_since >= stall_config.warning_turn():
                warned = True
                if golem_dir:
                    ProgressLogger(golem_dir).log_stall_warning(
                        role, current_turn, stall_config.max_turns, registry.action_call_count()
                    )
                if event_bus:
                    from golem.events import AgentStallWarning
                    await event_bus.emit(AgentStallWarning(
                        role=role, turn=current_turn,
                        turns_since_action=registry.turns_since_last_action(current_turn),
                        action_tools_available=[],
                    ))

            # Kill threshold: mark stalled, skip further processing but
            # keep consuming so the SDK generator exits cleanly.
            if not kill_hit and turns_since >= stall_config.kill_turn():
                kill_hit = True
                stall_turn = current_turn
                if golem_dir:
                    ProgressLogger(golem_dir).log_stall_detected(
                        role, current_turn, stall_config.max_turns, registry.action_call_count()
                    )
                if event_bus:
                    from golem.events import AgentStallKill
                    await event_bus.emit(AgentStallKill(role=role, turn=current_turn))

        elif isinstance(message, ResultMessage):
            cost_usd = message.total_cost_usd or 0.0
            usage = message.usage or {}
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)
            stop_reason = message.stop_reason
            sdk_session_id = message.session_id or ""
            if message.result:
                result_text = message.result
                if on_text:
                    on_text(message.result)

    elapsed = time.monotonic() - start
    stalled = kill_hit
    if event_bus:
        from golem.events import AgentComplete
        await event_bus.emit(AgentComplete(
            role=role,
            total_cost=cost_usd,
            total_turns=current_turn,
            duration_s=elapsed,
            result_preview=result_text[:500] if result_text else "",
        ))
    return SupervisedResult(
        result_text=result_text,
        cost_usd=cost_usd,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        turns=current_turn,
        duration_s=elapsed,
        stalled=stalled,
        stall_turn=stall_turn,
        registry=registry,
        stop_reason=stop_reason,
        sdk_session_id=sdk_session_id,
    )


# ---------------------------------------------------------------------------
# Session transcript compaction helpers
# ---------------------------------------------------------------------------


def _serialize_session_messages(messages: list) -> str:
    """Serialize SDK SessionMessage list to readable text blocks."""
    parts: list[str] = []
    for msg in messages:
        role = getattr(msg, "type", "unknown").upper()
        # msg.message is a raw Anthropic API dict: {"role": "...", "content": [...]}
        raw = getattr(msg, "message", {})
        if isinstance(raw, dict):
            content = raw.get("content", "")
            if isinstance(content, list):
                # Extract text from content blocks
                texts: list[str] = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            texts.append(block.get("text", ""))
                        elif block.get("type") == "tool_use":
                            texts.append(f"[TOOL: {block.get('name', '')}({block.get('input', {})})]")
                        elif block.get("type") == "tool_result":
                            texts.append(f"[TOOL RESULT: {str(block.get('content', ''))[:200]}]")
                content = "\n".join(texts)
            parts.append(f"[{role}]\n{content}")
        else:
            parts.append(f"[{role}]\n{raw}")
    return "\n\n---\n\n".join(parts)


def _raw_truncation(messages: list, max_chars: int) -> str:
    """Fallback: serialize last 5 messages and truncate from the end."""
    last_five = messages[-5:] if len(messages) > 5 else messages
    text = _serialize_session_messages(last_five)
    if len(text) <= max_chars:
        return text
    return text[-max_chars:] + "\n\n[... truncated ...]"


def _build_minimal_fallback(original_prompt: str) -> str:
    """Return a minimal context string when no transcript is available."""
    preview = original_prompt[:500].replace("\n", " ")
    return (
        f"No transcript available for the previous session segment.\n"
        f"Original task: {preview}\n\n"
        f"Please continue working on the original task from the beginning."
    )


async def _run_summarizer(serialized: str, original_prompt: str, config: GolemConfig) -> str:
    """Call cheap model to summarize the session transcript."""
    from claude_agent_sdk import AssistantMessage as _AssistantMessage
    from claude_agent_sdk import ClaudeAgentOptions as _ClaudeAgentOptions
    from claude_agent_sdk import ResultMessage as _ResultMessage
    from claude_agent_sdk import TextBlock as _TextBlock
    from claude_agent_sdk import query as _query

    from golem.config import sdk_env

    system = (
        "You are a concise technical summarizer. Given a conversation between "
        "an AI agent and its tools, extract the key information needed to continue "
        "the work. Focus on: what has been accomplished, what files were modified, "
        "what remains to be done, and any critical decisions or findings. "
        "Use bullet points. Be thorough but concise."
    )

    target_words = config.continuation_summary_target_words
    user_prompt = (
        f"Summarize this AI agent conversation in approximately {target_words} words.\n\n"
        f"The agent was working on this task:\n{original_prompt[:300]}\n\n"
        f"Focus on:\n"
        f"- What tasks/subtasks have been completed\n"
        f"- What files were created, modified, or read\n"
        f"- Key decisions made and their rationale\n"
        f"- What work remains to be done\n"
        f"- Any errors encountered and how they were resolved\n\n"
        f"## Conversation:\n{serialized}\n\n## Summary:"
    )

    summarizer_options = _ClaudeAgentOptions(
        model=config.continuation_model,
        max_turns=3,
        permission_mode="bypassPermissions",
        env=sdk_env(),
    )

    result_text = ""
    async for message in _query(prompt=user_prompt, options=summarizer_options):
        if isinstance(message, _AssistantMessage):
            for block in message.content:
                if isinstance(block, _TextBlock):
                    result_text += block.text
        elif isinstance(message, _ResultMessage):
            if message.result:
                result_text = message.result  # Use structured result if available
    return result_text


async def compact_session_messages(
    sdk_session_id: str,
    original_prompt: str,
    config: GolemConfig,
) -> str:
    """Summarize a session's transcript using a cheap model.

    Fetches the transcript via get_session_messages(), serializes it, and
    calls Haiku to produce a concise summary. Falls back to raw truncation
    of the last 5 messages if summarization fails or returns empty.

    Args:
        sdk_session_id: The SDK session_id from ResultMessage.session_id.
        original_prompt: The original session prompt — prepended to context
            so the summarizer understands what task was being performed.
        config: GolemConfig for continuation settings and model choice.

    Returns:
        Summary string to inject as the continuation opening message.
    """
    from claude_agent_sdk import get_session_messages

    # Fetch transcript — returns [] if session_id not found
    messages: list = []
    try:
        messages = get_session_messages(sdk_session_id)
    except Exception:
        pass  # Will fall through to raw truncation

    if not messages:
        # No transcript available — use a minimal fallback
        return _build_minimal_fallback(original_prompt)

    # Serialize messages to text
    serialized = _serialize_session_messages(messages)

    # Truncate input to avoid overwhelming the summarizer
    max_chars = config.continuation_summary_max_chars
    if len(serialized) > max_chars:
        serialized = serialized[:max_chars] + "\n\n[... transcript truncated ...]"

    # Attempt summarization with Haiku
    try:
        summary = await _run_summarizer(serialized, original_prompt, config)
        if summary.strip():
            return summary.strip()
    except Exception:
        pass

    # Fallback: last 5 messages raw-truncated
    return _raw_truncation(messages, config.continuation_raw_truncation_chars)


def _build_continuation_prompt(summary: str, continuation_number: int, original_prompt: str) -> str:
    """Build the opening message for a continuation session."""
    return (
        f"## Session Continuation ({continuation_number})\n\n"
        f"You are continuing a previous session that ran out of context window space.\n"
        f"Here is a summary of your prior work:\n\n"
        f"{summary}\n\n"
        f"## Original Task\n\n"
        f"{original_prompt[:500]}\n\n"
        f"Continue where you left off. Do NOT repeat completed work. "
        f"Focus on what remains to be done."
    )


# ---------------------------------------------------------------------------
# Continuation wrapper
# ---------------------------------------------------------------------------


async def continuation_supervised_session(
    prompt: str,
    options: ClaudeAgentOptions,
    role: str,
    config: GolemConfig,
    stall_config: StallConfig,
    on_text: Callable[[str], None] | None = None,
    on_tool: Callable[[str], None] | None = None,
    golem_dir: Path | None = None,
    event_bus: EventBus | None = None,
) -> ContinuationResult:
    """Run supervised_session() with context exhaustion continuation.

    Wraps supervised_session() in a loop that detects context exhaustion
    (stop_reason == "max_tokens" etc.), compacts the session transcript via
    a cheap model, and restarts with the summary injected as the opening
    message.

    Continuation is disabled when config.continuation_enabled is False,
    in which case this is a thin pass-through to supervised_session().

    Metrics (cost, tokens, turns, duration) are accumulated across all
    segments and returned in a single ContinuationResult.

    At most config.max_continuations continuation sessions are spawned
    after the initial one (i.e., max config.max_continuations + 1 total
    sessions). If the limit is hit, the result is treated as complete.
    """
    if not config.continuation_enabled:
        # Fast path — no continuation, direct delegation
        result = await supervised_session(
            prompt=prompt, options=options, role=role, config=config,
            stall_config=stall_config, on_text=on_text, on_tool=on_tool,
            golem_dir=golem_dir, event_bus=event_bus,
        )
        return ContinuationResult(
            result_text=result.result_text,
            cost_usd=result.cost_usd,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            turns=result.turns,
            duration_s=result.duration_s,
            stalled=result.stalled,
            stall_turn=result.stall_turn,
            registry=result.registry,
            continuation_count=0,
            exhausted=False,
        )

    max_continuations = config.max_continuations
    current_prompt = prompt
    continuation_count = 0
    total_cost: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_turns: int = 0
    total_duration_s: float = 0.0

    for i in range(max_continuations + 1):  # +1 for the initial session
        result = await supervised_session(
            prompt=current_prompt,
            options=options,
            role=role,
            config=config,
            stall_config=stall_config,
            on_text=on_text,
            on_tool=on_tool,
            golem_dir=golem_dir,
            event_bus=event_bus,
        )

        # Accumulate metrics from this segment
        total_cost += result.cost_usd
        total_input_tokens += result.input_tokens
        total_output_tokens += result.output_tokens
        total_turns += result.turns
        total_duration_s += result.duration_s

        # Check if context exhausted
        exhausted = _is_context_exhausted(result)

        if not exhausted:
            # Normal completion (or stall) — return merged result
            return ContinuationResult(
                result_text=result.result_text,
                cost_usd=total_cost,
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
                turns=total_turns,
                duration_s=total_duration_s,
                stalled=result.stalled,
                stall_turn=result.stall_turn,
                registry=result.registry,
                continuation_count=continuation_count,
                exhausted=False,
            )

        # Emit ContextExhausted event
        if event_bus:
            from golem.events import ContextExhausted as _ContextExhausted
            await event_bus.emit(_ContextExhausted(
                role=role,
                turn=total_turns,
                continuation_number=continuation_count,
                session_id_segment=result.sdk_session_id,
            ))

        # Check continuation cap
        if i >= max_continuations:
            # Hit the cap — treat as completed, not an error
            return ContinuationResult(
                result_text=result.result_text,
                cost_usd=total_cost,
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
                turns=total_turns,
                duration_s=total_duration_s,
                stalled=result.stalled,
                stall_turn=result.stall_turn,
                registry=result.registry,
                continuation_count=continuation_count,
                exhausted=True,  # Caller can log this but should not fail
            )

        # Compact and build the continuation prompt
        continuation_count += 1
        summary = await compact_session_messages(
            sdk_session_id=result.sdk_session_id,
            original_prompt=prompt,       # Always the ORIGINAL prompt for context
            config=config,
        )

        current_prompt = _build_continuation_prompt(summary, continuation_count, prompt)

        # Emit SessionContinued event
        if event_bus:
            from golem.events import SessionContinued as _SessionContinued
            await event_bus.emit(_SessionContinued(
                role=role,
                continuation_number=continuation_count,
                summary_chars=len(summary),
                cumulative_cost_usd=total_cost,
                cumulative_turns=total_turns,
            ))

    # Should not be reached — loop handles all exits
    raise RuntimeError(f"continuation_supervised_session: loop exited unexpectedly for role={role}")
