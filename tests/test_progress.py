from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

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


def test_sum_agent_costs_empty_log() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        logger = ProgressLogger(Path(tmpdir))
        assert logger.sum_agent_costs() == 0.0


def test_sum_agent_costs_multiple_entries() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        logger = ProgressLogger(Path(tmpdir))
        logger.log_agent_cost("lead_architect", 0.05, 10000, 2000)
        logger.log_agent_cost("tech_lead", 0.12, 30000, 5000)
        logger.log_agent_cost("junior_dev/TICKET-001", 0.03, 8000, 1500)
        total = logger.sum_agent_costs()
        assert abs(total - 0.20) < 0.001


def test_log_session_start() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        logger = ProgressLogger(Path(tmpdir))
        logger.log_session_start("auth-flow-1", "specs/auth.md")
        content = (Path(tmpdir) / "progress.log").read_text(encoding="utf-8")
        assert "SESSION_START" in content
        assert "session_id=auth-flow-1" in content
        assert "spec=specs/auth.md" in content


def test_log_session_complete() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        logger = ProgressLogger(Path(tmpdir))
        logger.log_session_complete("auth-flow-1", "merged")
        content = (Path(tmpdir) / "progress.log").read_text(encoding="utf-8")
        assert "SESSION_COMPLETE" in content
        assert "session_id=auth-flow-1" in content
        assert "status=merged" in content


def test_log_merge_queued() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        logger = ProgressLogger(Path(tmpdir))
        logger.log_merge_queued("auth-flow-1")
        content = (Path(tmpdir) / "progress.log").read_text(encoding="utf-8")
        assert "MERGE_QUEUED" in content
        assert "session_id=auth-flow-1" in content


def test_log_pr_created() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        logger = ProgressLogger(Path(tmpdir))
        logger.log_pr_created("auth-flow-1", 42)
        content = (Path(tmpdir) / "progress.log").read_text(encoding="utf-8")
        assert "PR_CREATED" in content
        assert "session_id=auth-flow-1" in content
        assert "pr=42" in content


def test_log_pr_merged() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        logger = ProgressLogger(Path(tmpdir))
        logger.log_pr_merged("auth-flow-1", 42)
        content = (Path(tmpdir) / "progress.log").read_text(encoding="utf-8")
        assert "PR_MERGED" in content
        assert "pr=42" in content


def test_log_rebase_start() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        logger = ProgressLogger(Path(tmpdir))
        logger.log_rebase_start("auth-flow-1", "main")
        content = (Path(tmpdir) / "progress.log").read_text(encoding="utf-8")
        assert "REBASE_START" in content
        assert "onto=main" in content


def test_log_rebase_complete() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        logger = ProgressLogger(Path(tmpdir))
        logger.log_rebase_complete("auth-flow-1")
        content = (Path(tmpdir) / "progress.log").read_text(encoding="utf-8")
        assert "REBASE_COMPLETE" in content
        assert "session_id=auth-flow-1" in content


def test_log_rebase_failed() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        logger = ProgressLogger(Path(tmpdir))
        logger.log_rebase_failed("auth-flow-1", "conflict in server.py")
        content = (Path(tmpdir) / "progress.log").read_text(encoding="utf-8")
        assert "REBASE_FAILED" in content
        assert "error=conflict in server.py" in content


# ---------------------------------------------------------------------------
# EventBus subscriber tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_progress_logger_subscribes_to_event_bus(tmp_path: Path) -> None:
    """ProgressLogger formats AgentSpawned(planner) as LEAD_ARCHITECT_START."""
    from golem.events import AgentSpawned, EventBus, QueueBackend

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
    from golem.events import AgentComplete, EventBus, QueueBackend

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
    from golem.events import EventBus, QueueBackend
    from golem.events import QAResult as QAResultEvent

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
    golem_dir = tmp_path / ".golem"
    golem_dir.mkdir()
    logger = ProgressLogger(golem_dir)
    logger.log_planner_start()
    log = (golem_dir / "progress.log").read_text(encoding="utf-8")
    assert "LEAD_ARCHITECT_START" in log
