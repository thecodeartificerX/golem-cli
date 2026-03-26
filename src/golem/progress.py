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
