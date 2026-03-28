from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from golem.events import EventBus


class ProgressLogger:
    def __init__(self, golem_dir: Path) -> None:
        self._path = golem_dir / "progress.log"
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _write(self, msg: str) -> None:
        ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")

    def log_task_start(self, task_id: str) -> None:
        self._write(f"START {task_id}")

    def log_task_complete(self, task_id: str) -> None:
        self._write(f"COMPLETE {task_id}")

    def log_task_retry(self, task_id: str, attempt: int, feedback: str) -> None:
        short_feedback = feedback[:200].replace("\n", " ")
        self._write(f"RETRY {task_id} attempt={attempt} feedback={short_feedback!r}")

    def log_task_blocked(self, task_id: str, reason: str) -> None:
        self._write(f"BLOCKED {task_id} reason={reason!r}")

    def log_group_complete(self, group_id: str) -> None:
        self._write(f"GROUP_COMPLETE {group_id}")

    def log_integration_review(self, passed: bool) -> None:
        result = "PASSED" if passed else "FAILED"
        self._write(f"INTEGRATION_REVIEW {result}")

    def log_final_validation(self, passed: bool) -> None:
        result = "PASSED" if passed else "FAILED"
        self._write(f"FINAL_VALIDATION {result}")

    # -- v2 pipeline events --

    def log_planner_start(self) -> None:
        self._write("LEAD_ARCHITECT_START")

    def log_planner_complete(self, ticket_id: str) -> None:
        self._write(f"LEAD_ARCHITECT_COMPLETE ticket={ticket_id}")

    def log_tech_lead_start(self, ticket_id: str) -> None:
        self._write(f"TECH_LEAD_START ticket={ticket_id}")

    def log_tech_lead_complete(self, elapsed_s: float | None = None) -> None:
        if elapsed_s is not None:
            mins, secs = divmod(int(elapsed_s), 60)
            self._write(f"TECH_LEAD_COMPLETE elapsed={mins}m{secs}s")
        else:
            self._write("TECH_LEAD_COMPLETE")

    def log_ticket_created(self, ticket_id: str, title: str) -> None:
        self._write(f"TICKET_CREATED {ticket_id} title={title!r}")

    def log_writer_dispatched(self, ticket_id: str) -> None:
        self._write(f"JUNIOR_DEV_DISPATCHED {ticket_id}")

    def log_qa_result(self, ticket_id: str, passed: bool, summary: str) -> None:
        result = "PASSED" if passed else "FAILED"
        short = summary[:200].replace("\n", " ")
        self._write(f"QA_{result} {ticket_id} {short}")

    def log_merge_complete(self, branch: str) -> None:
        self._write(f"MERGE_COMPLETE branch={branch}")

    def log_guidance_received(self, note: str) -> None:
        """Log that operator guidance was received."""
        self._write(f"GUIDANCE_RECEIVED note={note}")

    def log_error(self, role: str, message: str) -> None:
        self._write(f"ERROR role={role} message={message!r}")

    def log_warning(self, role: str, message: str) -> None:
        self._write(f"WARNING role={role} message={message!r}")

    # -- Orchestrator wave events --

    def log_wave_start(self, wave_number: int, total_waves: int, ticket_ids: list[str]) -> None:
        tickets_str = ", ".join(ticket_ids)
        self._write(f"[WAVE {wave_number + 1}/{total_waves}] Starting: {tickets_str}")

    def log_wave_complete(self, wave_number: int, passed: int, failed: int) -> None:
        self._write(f"[WAVE {wave_number + 1}] Complete: {passed} passed, {failed} failed")

    def log_wave_skipped(self, wave_number: int, reason: str) -> None:
        self._write(f"[WAVE {wave_number + 1}] Skipped: {reason}")

    def log_classification(self, complexity: str, reasoning: str) -> None:
        self._write(f"CLASSIFICATION complexity={complexity} reasoning={reasoning}")

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

    def log_stall_warning(self, role: str, turn: int, max_turns: int, mcp_calls: int) -> None:
        self._write(f"STALL_WARNING role={role} turn={turn}/{max_turns} mcp_actions={mcp_calls}")

    def log_stall_detected(self, role: str, turn: int, max_turns: int, mcp_calls: int) -> None:
        self._write(f"STALL_DETECTED role={role} turn={turn}/{max_turns} mcp_actions={mcp_calls}")

    def log_stall_fatal(self, role: str, turn: int) -> None:
        self._write(f"STALL_FATAL role={role} turn={turn} -- retry also stalled")

    def log_stall_retry(self, role: str) -> None:
        self._write(f"STALL_RETRY role={role} -- restarting with escalated prompt")

    # -- Session lifecycle events --

    def log_session_start(self, session_id: str, spec_path: str) -> None:
        self._write(f"SESSION_START session_id={session_id} spec={spec_path}")

    def log_session_complete(self, session_id: str, status: str) -> None:
        self._write(f"SESSION_COMPLETE session_id={session_id} status={status}")

    def log_merge_queued(self, session_id: str) -> None:
        self._write(f"MERGE_QUEUED session_id={session_id}")

    def log_pr_created(self, session_id: str, pr_number: int) -> None:
        self._write(f"PR_CREATED session_id={session_id} pr={pr_number}")

    def log_pr_merged(self, session_id: str, pr_number: int) -> None:
        self._write(f"PR_MERGED session_id={session_id} pr={pr_number}")

    def log_rebase_start(self, session_id: str, onto: str) -> None:
        self._write(f"REBASE_START session_id={session_id} onto={onto}")

    def log_rebase_complete(self, session_id: str) -> None:
        self._write(f"REBASE_COMPLETE session_id={session_id}")

    def log_rebase_failed(self, session_id: str, error: str) -> None:
        self._write(f"REBASE_FAILED session_id={session_id} error={error}")

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

    def sum_agent_costs(self) -> float:
        """Sum all AGENT_COST entries in the progress log."""
        total = 0.0
        if not self._path.exists():
            return total
        for line in self._path.read_text(encoding="utf-8").splitlines():
            m = re.search(r"AGENT_COST.*cost=\$([0-9.]+)", line)
            if m:
                total += float(m.group(1))
        return total
