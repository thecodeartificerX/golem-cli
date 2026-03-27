from __future__ import annotations

import tempfile
from pathlib import Path

from golem.progress import ProgressLogger


def test_log_planner_start_writes_event() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        logger = ProgressLogger(Path(tmpdir))
        logger.log_planner_start()
        content = (Path(tmpdir) / "progress.log").read_text(encoding="utf-8")
        assert "LEAD_ARCHITECT_START" in content


def test_log_planner_complete_includes_ticket_id() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        logger = ProgressLogger(Path(tmpdir))
        logger.log_planner_complete("TICKET-001")
        content = (Path(tmpdir) / "progress.log").read_text(encoding="utf-8")
        assert "LEAD_ARCHITECT_COMPLETE" in content
        assert "TICKET-001" in content


def test_log_tech_lead_complete_with_elapsed() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        logger = ProgressLogger(Path(tmpdir))
        logger.log_tech_lead_complete(elapsed_s=272.5)
        content = (Path(tmpdir) / "progress.log").read_text(encoding="utf-8")
        assert "TECH_LEAD_COMPLETE" in content
        assert "elapsed=4m32s" in content


def test_log_tech_lead_complete_without_elapsed() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        logger = ProgressLogger(Path(tmpdir))
        logger.log_tech_lead_complete()
        content = (Path(tmpdir) / "progress.log").read_text(encoding="utf-8")
        assert "TECH_LEAD_COMPLETE" in content
        assert "elapsed" not in content


def test_log_ticket_created() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        logger = ProgressLogger(Path(tmpdir))
        logger.log_ticket_created("TICKET-003", "Build the widget")
        content = (Path(tmpdir) / "progress.log").read_text(encoding="utf-8")
        assert "TICKET_CREATED TICKET-003" in content
        assert "Build the widget" in content


def test_log_qa_result_passed() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        logger = ProgressLogger(Path(tmpdir))
        logger.log_qa_result("TICKET-001", passed=True, summary="5/5 checks passed")
        content = (Path(tmpdir) / "progress.log").read_text(encoding="utf-8")
        assert "QA_PASSED TICKET-001" in content


def test_log_qa_result_failed() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        logger = ProgressLogger(Path(tmpdir))
        logger.log_qa_result("TICKET-002", passed=False, summary="3/5 checks passed")
        content = (Path(tmpdir) / "progress.log").read_text(encoding="utf-8")
        assert "QA_FAILED TICKET-002" in content


def test_log_writer_dispatched() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        logger = ProgressLogger(Path(tmpdir))
        logger.log_writer_dispatched("TICKET-003")
        content = (Path(tmpdir) / "progress.log").read_text(encoding="utf-8")
        assert "JUNIOR_DEV_DISPATCHED TICKET-003" in content


def test_log_merge_complete() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        logger = ProgressLogger(Path(tmpdir))
        logger.log_merge_complete("golem/spec/integration")
        content = (Path(tmpdir) / "progress.log").read_text(encoding="utf-8")
        assert "MERGE_COMPLETE" in content
        assert "golem/spec/integration" in content


def test_log_task_start() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        logger = ProgressLogger(Path(tmpdir))
        logger.log_task_start("task-001")
        content = (Path(tmpdir) / "progress.log").read_text(encoding="utf-8")
        assert "START task-001" in content


def test_log_task_complete() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        logger = ProgressLogger(Path(tmpdir))
        logger.log_task_complete("task-001")
        content = (Path(tmpdir) / "progress.log").read_text(encoding="utf-8")
        assert "COMPLETE task-001" in content


def test_log_task_retry() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        logger = ProgressLogger(Path(tmpdir))
        logger.log_task_retry("task-001", 2, "lint failed")
        content = (Path(tmpdir) / "progress.log").read_text(encoding="utf-8")
        assert "RETRY task-001" in content
        assert "attempt=2" in content


def test_log_task_blocked() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        logger = ProgressLogger(Path(tmpdir))
        logger.log_task_blocked("task-002", "dependency failed")
        content = (Path(tmpdir) / "progress.log").read_text(encoding="utf-8")
        assert "BLOCKED task-002" in content


def test_log_group_complete() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        logger = ProgressLogger(Path(tmpdir))
        logger.log_group_complete("group-1")
        content = (Path(tmpdir) / "progress.log").read_text(encoding="utf-8")
        assert "GROUP_COMPLETE group-1" in content


def test_log_integration_review_passed() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        logger = ProgressLogger(Path(tmpdir))
        logger.log_integration_review(passed=True)
        content = (Path(tmpdir) / "progress.log").read_text(encoding="utf-8")
        assert "INTEGRATION_REVIEW PASSED" in content


def test_log_integration_review_failed() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        logger = ProgressLogger(Path(tmpdir))
        logger.log_integration_review(passed=False)
        content = (Path(tmpdir) / "progress.log").read_text(encoding="utf-8")
        assert "INTEGRATION_REVIEW FAILED" in content


def test_log_final_validation() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        logger = ProgressLogger(Path(tmpdir))
        logger.log_final_validation(passed=True)
        content = (Path(tmpdir) / "progress.log").read_text(encoding="utf-8")
        assert "FINAL_VALIDATION PASSED" in content


def test_multiple_events_appended() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        logger = ProgressLogger(Path(tmpdir))
        logger.log_planner_start()
        logger.log_planner_complete("TICKET-001")
        logger.log_tech_lead_start("TICKET-001")
        lines = (Path(tmpdir) / "progress.log").read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 3


def test_log_guidance_received() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        logger = ProgressLogger(Path(tmpdir))
        logger.log_guidance_received("adjust scope")
        content = (Path(tmpdir) / "progress.log").read_text(encoding="utf-8")
        assert "GUIDANCE_RECEIVED" in content
        assert "adjust scope" in content


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
