from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


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
