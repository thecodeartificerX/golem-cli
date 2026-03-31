from __future__ import annotations

import tempfile
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from golem.config import GolemConfig
from golem.qa import QACheck, QAResult
from golem.tickets import Ticket, TicketContext, TicketEvent, TicketStore
from golem.writer import JuniorDevResult, WriterResult, build_writer_prompt, spawn_junior_dev, spawn_writer_pair


# ---------------------------------------------------------------------------
# ClaudeSDKClient mock helper (mirrors test_supervisor.py)
# ---------------------------------------------------------------------------


def _make_mock_sdk_client(
    fake_gen_fn: Callable[..., AsyncGenerator[Any, None]],
    captured_cwd: list[str] | None = None,
) -> type:
    """Return a mock ClaudeSDKClient class whose receive_response() drives fake_gen_fn.

    If captured_cwd is provided, options.cwd will be appended to it on construction
    so tests can verify the cwd passed to the session.
    """

    class _MockClient:
        def __init__(self, options: Any = None, **kwargs: Any) -> None:
            self._prompt: str = ""
            self._gen: AsyncGenerator[Any, None] | None = None
            if captured_cwd is not None and options is not None:
                captured_cwd.append(options.cwd)

        async def __aenter__(self) -> "_MockClient":
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

        async def query(self, prompt: str, session_id: str = "default") -> None:
            self._prompt = prompt
            self._gen = fake_gen_fn(prompt)

        async def receive_response(self) -> AsyncGenerator[Any, None]:  # type: ignore[override]
            if self._gen is None:
                self._gen = fake_gen_fn()
            async for msg in self._gen:
                yield msg

        def interrupt(self) -> None:
            pass

    return _MockClient


def _make_ticket_with_context() -> Ticket:
    ctx = TicketContext(
        plan_file="",
        files={"src/main.py": "def main(): pass\n"},
        references=["references/api.md"],
        blueprint="Build the main entry point module.",
        acceptance=["main() function exists", "no syntax errors"],
        qa_checks=["python -m py_compile src/main.py"],
        parallelism_hints=["can be split into create + test"],
    )
    return Ticket(
        id="TICKET-001",
        type="task",
        title="Implement main module",
        status="pending",
        priority="high",
        created_by="tech_lead",
        assigned_to="writer",
        context=ctx,
    )


def test_build_writer_prompt_injects_all_fields() -> None:
    ticket = _make_ticket_with_context()
    prompt = build_writer_prompt(ticket)
    assert "TICKET-001" in prompt
    assert "Implement main module" in prompt
    assert "def main(): pass" in prompt
    assert "references/api.md" in prompt
    assert "Build the main entry point module" in prompt
    assert "main() function exists" in prompt
    assert "python -m py_compile src/main.py" in prompt
    assert "can be split into create + test" in prompt


def test_build_writer_prompt_strips_empty_sections() -> None:
    ticket = _make_ticket_with_context()
    ticket.context.parallelism_hints = []
    prompt = build_writer_prompt(ticket)
    assert "{parallelism_hints}" not in prompt


def test_build_writer_prompt_no_leftover_placeholders() -> None:
    ticket = _make_ticket_with_context()
    # Set all optional fields to empty
    ticket.context.parallelism_hints = []
    ticket.context.references = []
    ticket.context.qa_checks = []
    ticket.context.acceptance = []
    prompt = build_writer_prompt(ticket)
    assert "{" not in prompt


def test_build_writer_prompt_all_fields_populated_no_placeholders() -> None:
    """With ALL context fields populated, no {placeholder} patterns should remain."""
    ctx = TicketContext(
        plan_file="",  # empty plan file — won't read from disk
        files={"src/app.py": "print('hello')\n", "tests/test_app.py": "def test(): pass\n"},
        references=["docs/api.md", "docs/guide.md"],
        blueprint="Full architectural blueprint for the project with all details",
        acceptance=["All tests pass", "No lint errors", "Coverage > 80%"],
        qa_checks=["uv run pytest", "ruff check .", "mypy ."],
        parallelism_hints=["tests can run parallel", "lint is independent"],
    )
    ticket = Ticket(
        id="TICKET-099",
        type="task",
        title="Full context ticket",
        status="in_progress",
        priority="medium",
        created_by="tech_lead",
        assigned_to="writer",
        context=ctx,
    )
    prompt = build_writer_prompt(ticket)
    # No template placeholders should remain
    import re
    leftover = re.findall(r"\{[a-z_]+\}", prompt)
    assert leftover == [], f"Unresolved placeholders: {leftover}"
    # All content should be present
    assert "TICKET-099" in prompt
    assert "src/app.py" in prompt
    assert "docs/api.md" in prompt
    assert "All tests pass" in prompt
    assert "uv run pytest" in prompt
    assert "tests can run parallel" in prompt


def test_build_writer_prompt_reads_plan_file_from_disk() -> None:
    """When plan_file points to a real file, its contents appear in the prompt."""
    with tempfile.TemporaryDirectory() as tmpdir:
        plan_path = Path(tmpdir) / "task-001.md"
        plan_path.write_text("## Step 1: Build the widget\nCreate widget.py with Widget class.", encoding="utf-8")

        ticket = _make_ticket_with_context()
        ticket.context.plan_file = str(plan_path)
        prompt = build_writer_prompt(ticket)
        assert "Build the widget" in prompt
        assert "Widget class" in prompt


def test_build_writer_prompt_injects_reference_content(tmp_path: Path) -> None:
    """When golem_dir is provided, reference file content is injected inline."""
    ref_file = tmp_path / "references" / "api.md"
    ref_file.parent.mkdir(parents=True)
    ref_file.write_text("# API Reference\nGET /users returns a list.", encoding="utf-8")

    ticket = _make_ticket_with_context()
    ticket.context.references = ["references/api.md"]
    prompt = build_writer_prompt(ticket, golem_dir=tmp_path)

    assert "### references/api.md" in prompt
    assert "# API Reference" in prompt
    assert "GET /users returns a list." in prompt


def test_build_writer_prompt_reference_per_file_cap(tmp_path: Path) -> None:
    """Reference content is truncated at 5,000 chars per file."""
    ref_file = tmp_path / "big.md"
    ref_file.write_text("X" * 10_000, encoding="utf-8")

    ticket = _make_ticket_with_context()
    ticket.context.references = ["big.md"]
    prompt = build_writer_prompt(ticket, golem_dir=tmp_path)

    # Content is capped at 5000 chars
    assert "### big.md" in prompt
    # The injected content should be exactly 5000 X's, not 10000
    section_start = prompt.index("### big.md\n") + len("### big.md\n")
    # Find the end of the section (next ### or end of references area)
    x_count = 0
    for ch in prompt[section_start:]:
        if ch == "X":
            x_count += 1
        else:
            break
    assert x_count == 5_000


def test_build_writer_prompt_reference_total_cap(tmp_path: Path) -> None:
    """Once total reference content exceeds 20,000 chars, remaining files show omitted message."""
    # Create 5 files, each 5000 chars — total would be 25000, exceeding 20000 cap
    for i in range(5):
        ref_file = tmp_path / f"ref{i}.md"
        ref_file.write_text(f"{'A' * 5_000}", encoding="utf-8")

    ticket = _make_ticket_with_context()
    ticket.context.references = [f"ref{i}.md" for i in range(5)]
    prompt = build_writer_prompt(ticket, golem_dir=tmp_path)

    # First 4 files fit (4 * 5000 = 20000), 5th should be omitted
    for i in range(4):
        assert f"### ref{i}.md" in prompt
    assert "### ref4.md" in prompt
    assert "(content omitted -- read if needed)" in prompt


def test_build_writer_prompt_reference_missing_file(tmp_path: Path) -> None:
    """Missing reference files get a (file not found) placeholder."""
    ticket = _make_ticket_with_context()
    ticket.context.references = ["does_not_exist.md"]
    prompt = build_writer_prompt(ticket, golem_dir=tmp_path)

    assert "### does_not_exist.md" in prompt
    assert "(file not found)" in prompt


def test_both_prompts_list_all_writer_tools() -> None:
    """Both junior_dev.md and junior_dev_rework.md must list every tool in WRITER_TOOLS."""
    from golem.tool_registry import WRITER_TOOLS

    prompts_dir = Path(__file__).resolve().parent.parent / "src" / "golem" / "prompts"
    junior_dev_prompt = (prompts_dir / "junior_dev.md").read_text(encoding="utf-8")
    rework_prompt = (prompts_dir / "junior_dev_rework.md").read_text(encoding="utf-8")

    for tool_name in sorted(WRITER_TOOLS):
        mcp_name = f"mcp__golem-junior-dev__{tool_name}"
        assert mcp_name in junior_dev_prompt, f"junior_dev.md missing tool: {mcp_name}"
        assert mcp_name in rework_prompt, f"junior_dev_rework.md missing tool: {mcp_name}"


@pytest.mark.asyncio
async def test_spawn_writer_pair_uses_worktree_cwd() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        golem_dir = Path(tmpdir) / ".golem"
        (golem_dir / "tickets").mkdir(parents=True)
        ticket = _make_ticket_with_context()
        config = GolemConfig()
        captured_cwd: list[str] = []

        async def fake_gen(prompt: str = "") -> AsyncGenerator[Any, None]:
            return
            yield

        with patch("golem.supervisor.ClaudeSDKClient", _make_mock_sdk_client(fake_gen, captured_cwd=captured_cwd)), \
             patch.dict(__import__("os").environ, {"GOLEM_TEST_MODE": "1"}):
            await spawn_junior_dev(ticket, tmpdir, config, golem_dir=golem_dir)

        assert captured_cwd == [tmpdir]


def test_build_prompt_first_attempt() -> None:
    """rework_count=0: iteration placeholder resolved to '1', rework_context is empty."""
    ticket = _make_ticket_with_context()
    prompt = build_writer_prompt(ticket, rework_count=0)
    assert "{iteration}" not in prompt
    assert "{rework_context}" not in prompt
    assert "Previous Rejection" not in prompt


def test_build_prompt_rework() -> None:
    """rework_count=2, notes=['fix lint', 'wrong file'] -> iteration=3, both notes in prompt."""
    ticket = _make_ticket_with_context()
    notes = ["fix lint errors", "wrong file modified"]
    prompt = build_writer_prompt(ticket, rework_count=2, rework_notes=notes)
    assert "{iteration}" not in prompt
    assert "{rework_context}" not in prompt
    assert "fix lint errors" in prompt
    assert "wrong file modified" in prompt


def test_build_prompt_rework_limits_notes() -> None:
    """With 5 rework notes, only the last 3 appear in the prompt."""
    ticket = _make_ticket_with_context()
    notes = ["n1", "n2", "n3", "n4", "n5"]
    prompt = build_writer_prompt(ticket, rework_count=5, rework_notes=notes)
    assert "n3" in prompt
    assert "n4" in prompt
    assert "n5" in prompt
    assert "n1" not in prompt
    assert "n2" not in prompt


def test_get_rework_info_counts_needs_work() -> None:
    """_get_rework_info counts needs_work events and extracts notes."""
    from golem.writer import _get_rework_info

    ticket = _make_ticket_with_context()
    ticket.history = [
        TicketEvent(ts="2026-01-01T00:00:00+00:00", agent="tl", action="status_changed_to_needs_work", note="fix lint"),
        TicketEvent(ts="2026-01-01T00:01:00+00:00", agent="tl", action="approved", note=""),
        TicketEvent(ts="2026-01-01T00:02:00+00:00", agent="tl", action="status_changed_to_needs_work", note="wrong file"),
    ]
    count, notes = _get_rework_info(ticket)
    assert count == 2
    assert notes == ["fix lint", "wrong file"]


def test_get_rework_info_empty_history() -> None:
    """_get_rework_info returns (0, []) for a ticket with no history."""
    from golem.writer import _get_rework_info

    ticket = _make_ticket_with_context()
    # Ticket from _make_ticket_with_context has empty history by default
    count, notes = _get_rework_info(ticket)
    assert count == 0
    assert notes == []


@pytest.mark.asyncio
async def test_jitter_skip_in_test_mode() -> None:
    """With GOLEM_TEST_MODE=1, no asyncio.sleep is called for jitter."""
    import os

    slept_durations: list[float] = []

    async def fake_sleep(n: float) -> None:
        slept_durations.append(n)

    async def fake_gen(prompt: str = "") -> AsyncGenerator[Any, None]:
        return
        yield

    ticket = _make_ticket_with_context()
    config = GolemConfig(dispatch_jitter_max=10.0)

    with patch("golem.supervisor.ClaudeSDKClient", _make_mock_sdk_client(fake_gen)), \
         patch("asyncio.sleep", side_effect=fake_sleep), \
         patch.dict(os.environ, {"GOLEM_TEST_MODE": "1"}):
        with tempfile.TemporaryDirectory() as tmpdir:
            golem_dir = Path(tmpdir) / ".golem"
            (golem_dir / "tickets").mkdir(parents=True)
            await spawn_junior_dev(ticket, tmpdir, config, golem_dir=golem_dir)

    # asyncio.sleep may be called for retry_delay but NOT for jitter (which is 10.0s max)
    jitter_sleeps = [d for d in slept_durations if d >= 1.0]
    assert len(jitter_sleeps) == 0, f"Jitter sleep called in test mode: {slept_durations}"


@pytest.mark.asyncio
async def test_spawn_writer_pair_golem_dir_none_fallback() -> None:
    """When golem_dir is None, create_writer_mcp_server uses Path(worktree_path)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "tickets").mkdir()
        ticket = _make_ticket_with_context()
        config = GolemConfig()
        captured_mcp_dir: list[Path] = []

        original_create = spawn_writer_pair.__module__

        def fake_create_writer_mcp(golem_dir: Path, registry=None, event_bus=None, **kwargs):
            captured_mcp_dir.append(golem_dir)
            # Return a minimal server config
            from unittest.mock import MagicMock
            return {"name": "golem-junior-dev", "type": "sdk", "instance": MagicMock()}

        async def fake_gen(prompt: str = "") -> AsyncGenerator[Any, None]:
            return
            yield

        with patch("golem.writer.create_junior_dev_mcp_server", side_effect=fake_create_writer_mcp), \
             patch("golem.supervisor.ClaudeSDKClient", _make_mock_sdk_client(fake_gen)):
            await spawn_junior_dev(ticket, tmpdir, config, golem_dir=None)

        assert len(captured_mcp_dir) == 1
        assert captured_mcp_dir[0] == Path(tmpdir)


def test_writer_result_dataclass() -> None:
    """JuniorDevResult has expected fields with correct defaults."""
    r = JuniorDevResult()
    assert r.result_text == ""
    assert r.cost_usd == 0.0
    assert r.input_tokens == 0
    assert r.output_tokens == 0
    assert r.cache_read_tokens == 0
    assert r.num_turns == 0
    assert r.duration_ms == 0


def test_writer_result_backward_compat_alias() -> None:
    """WriterResult is a backward-compatible alias for JuniorDevResult."""
    assert WriterResult is JuniorDevResult
    r = WriterResult()
    assert isinstance(r, JuniorDevResult)


def test_spawn_writer_pair_backward_compat_alias() -> None:
    """spawn_writer_pair is a backward-compatible alias for spawn_junior_dev."""
    assert spawn_writer_pair is spawn_junior_dev


# ---------------------------------------------------------------------------
# Stall and no-diff retry tests
# ---------------------------------------------------------------------------


def _ok_result() -> object:
    from golem.supervisor import ContinuationResult, ToolCallRegistry
    return ContinuationResult(
        result_text="done", cost_usd=0.0, input_tokens=0, output_tokens=0,
        turns=5, duration_s=0.1, stalled=False, stall_turn=None,
        registry=ToolCallRegistry(), continuation_count=0, exhausted=False,
    )


def _stalled_result() -> object:
    from golem.supervisor import ContinuationResult, ToolCallRegistry
    return ContinuationResult(
        result_text="", cost_usd=0.0, input_tokens=0, output_tokens=0,
        turns=10, duration_s=0.1, stalled=True, stall_turn=10,
        registry=ToolCallRegistry(), continuation_count=0, exhausted=False,
    )


@pytest.mark.asyncio
async def test_junior_dev_stall_triggers_retry() -> None:
    """First supervised_session stall triggers retry with escalated prompt."""
    with tempfile.TemporaryDirectory() as tmpdir:
        golem_dir = Path(tmpdir) / ".golem"
        (golem_dir / "tickets").mkdir(parents=True)
        ticket = _make_ticket_with_context()
        config = GolemConfig()

        mock_session = AsyncMock(side_effect=[_stalled_result(), _ok_result()])

        with patch("golem.writer.continuation_supervised_session", mock_session), \
             patch.dict(__import__("os").environ, {"GOLEM_TEST_MODE": "1"}):
            await spawn_junior_dev(ticket, tmpdir, config, golem_dir=golem_dir)

        assert mock_session.call_count == 2


@pytest.mark.asyncio
async def test_junior_dev_no_diff_triggers_retry() -> None:
    """Empty git diff after session triggers retry."""
    with tempfile.TemporaryDirectory() as tmpdir:
        golem_dir = Path(tmpdir) / ".golem"
        (golem_dir / "tickets").mkdir(parents=True)
        ticket = _make_ticket_with_context()
        config = GolemConfig()

        mock_session = AsyncMock(return_value=_ok_result())

        # GOLEM_TEST_MODE not set, so diff check runs; mock git diff to return empty
        import subprocess as sp
        fake_diff = sp.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        with patch("golem.writer.continuation_supervised_session", mock_session), \
             patch("subprocess.run", return_value=fake_diff):
            await spawn_junior_dev(ticket, tmpdir, config, golem_dir=golem_dir)

        # Initial session + no-diff retry = 2 calls
        assert mock_session.call_count == 2


@pytest.mark.asyncio
async def test_junior_dev_double_stall_fatal() -> None:
    """Two consecutive stalls raise RuntimeError and mark ticket as failed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        golem_dir = Path(tmpdir) / ".golem"
        (golem_dir / "tickets").mkdir(parents=True)
        ticket = _make_ticket_with_context()
        config = GolemConfig()

        mock_session = AsyncMock(return_value=_stalled_result())

        with patch("golem.writer.continuation_supervised_session", mock_session), \
             patch.dict(__import__("os").environ, {"GOLEM_TEST_MODE": "1"}):
            with pytest.raises(RuntimeError, match="stall"):
                await spawn_junior_dev(ticket, tmpdir, config, golem_dir=golem_dir)


# ---------------------------------------------------------------------------
# Unit 11: Verification gate tests
# ---------------------------------------------------------------------------

import json
from dataclasses import asdict
from typing import Any

from golem.qa import QACheck, QAResult
from golem.tickets import TicketStore


class _PassthroughCoordinator:
    """Test double for RecoveryCoordinator that calls session_fn() directly."""

    def __init__(self, config: GolemConfig) -> None:
        pass

    async def run_with_recovery(
        self,
        session_fn: Any,
        **kwargs: Any,
    ) -> Any:
        return await session_fn()


def _write_ticket_json(tickets_dir: Path, ticket: Ticket) -> None:
    """Write a ticket JSON file for harness tests that need to read/update tickets."""
    tickets_dir.mkdir(parents=True, exist_ok=True)
    path = tickets_dir / f"{ticket.id}.json"
    path.write_text(json.dumps(asdict(ticket), indent=2), encoding="utf-8")


def test_verification_gate_text_in_junior_dev_prompt() -> None:
    """The verification gate text must appear in the junior dev prompt template."""
    prompt_path = Path(__file__).parent.parent / "src" / "golem" / "prompts" / "junior_dev.md"
    content = prompt_path.read_text(encoding="utf-8")
    assert "Verification Gate (MANDATORY)" in content
    assert "RUN the tests. Quote the output." in content
    assert "pipeline will reject your submission" in content


def test_verification_gate_rework_text_in_junior_dev_prompt() -> None:
    """The rework path must also mandate re-running QA."""
    prompt_path = Path(__file__).parent.parent / "src" / "golem" / "prompts" / "junior_dev.md"
    content = prompt_path.read_text(encoding="utf-8")
    assert "re-run QA (MANDATORY" in content


@pytest.mark.asyncio
async def test_harness_detects_missing_qa_and_forces_run() -> None:
    """When writer never calls run_qa, the harness forces a QA run."""
    with tempfile.TemporaryDirectory() as tmpdir:
        golem_dir = Path(tmpdir) / ".golem"
        ticket = _make_ticket_with_context()
        _write_ticket_json(golem_dir / "tickets", ticket)
        config = GolemConfig()

        # Mock continuation_supervised_session — returns a result where qa was never called
        session_result = _ok_result()

        passing_qa = QAResult(passed=True, checks=[
            QACheck(type="acceptance", tool="python -m py_compile src/main.py", passed=True, stdout="", stderr=""),
        ], summary="1/1 checks passed.")

        with patch("golem.writer.continuation_supervised_session", AsyncMock(return_value=session_result)), \
             patch("golem.writer.run_qa", return_value=passing_qa) as mock_qa, \
             patch.dict(__import__("os").environ, {"GOLEM_TEST_MODE": "1"}), \
             patch("golem.writer.RecoveryCoordinator", _PassthroughCoordinator):
            result = await spawn_junior_dev(ticket, tmpdir, config, golem_dir=golem_dir)

        assert result.qa_called is False
        assert result.qa_forced is True
        assert result.qa_passed is True
        mock_qa.assert_called_once()


@pytest.mark.asyncio
async def test_harness_forced_qa_failure_triggers_rework() -> None:
    """When forced QA fails, the harness updates the ticket to needs_work."""
    with tempfile.TemporaryDirectory() as tmpdir:
        golem_dir = Path(tmpdir) / ".golem"
        ticket = _make_ticket_with_context()
        _write_ticket_json(golem_dir / "tickets", ticket)
        config = GolemConfig()

        session_result = _ok_result()

        failing_qa = QAResult(passed=False, checks=[
            QACheck(type="acceptance", tool="python -m py_compile src/main.py", passed=False, stdout="", stderr="error"),
        ], summary="0/1 checks passed. Failed: ['python -m py_compile src/main.py']")

        with patch("golem.writer.continuation_supervised_session", AsyncMock(return_value=session_result)), \
             patch("golem.writer.run_qa", return_value=failing_qa), \
             patch.dict(__import__("os").environ, {"GOLEM_TEST_MODE": "1"}), \
             patch("golem.writer.RecoveryCoordinator", _PassthroughCoordinator):
            result = await spawn_junior_dev(ticket, tmpdir, config, golem_dir=golem_dir)

        assert result.qa_called is False
        assert result.qa_forced is True
        assert result.qa_passed is False

        # Verify ticket was updated to needs_work
        store = TicketStore(golem_dir / "tickets")
        updated_ticket = await store.read(ticket.id)
        assert updated_ticket.status == "needs_work"
        # Check that the harness note is in the history
        harness_events = [e for e in updated_ticket.history if e.agent == "harness"]
        assert len(harness_events) == 1
        assert "writer skipped QA" in harness_events[0].note
