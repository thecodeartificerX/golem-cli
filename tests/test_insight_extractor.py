"""Tests for insight_extractor module."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from golem.config import GolemConfig
from golem.insight_extractor import (
    FileInsight,
    InsightResult,
    _parse_response,
    extract_insights,
    write_insights,
)


# ---------------------------------------------------------------------------
# Dataclass creation tests
# ---------------------------------------------------------------------------


def test_file_insight_creation() -> None:
    fi = FileInsight(path="src/foo.py", observation="uses factory pattern", category="pattern")
    assert fi.path == "src/foo.py"
    assert fi.observation == "uses factory pattern"
    assert fi.category == "pattern"


def test_file_insight_all_categories() -> None:
    for cat in ("pattern", "gotcha", "convention", "dependency"):
        fi = FileInsight(path="x.py", observation="obs", category=cat)
        assert fi.category == cat


def test_insight_result_defaults() -> None:
    r = InsightResult()
    assert r.file_insights == []
    assert r.patterns_discovered == []
    assert r.gotchas_discovered == []
    assert r.approach_outcome == ""
    assert r.recommendations == []


def test_insight_result_with_values() -> None:
    fi = FileInsight(path="src/bar.py", observation="no emoji", category="gotcha")
    r = InsightResult(
        file_insights=[fi],
        patterns_discovered=["use async/await"],
        gotchas_discovered=["cp1252 on Windows"],
        approach_outcome="Added module X",
        recommendations=["always set encoding=utf-8"],
    )
    assert len(r.file_insights) == 1
    assert r.file_insights[0].path == "src/bar.py"
    assert r.patterns_discovered == ["use async/await"]
    assert r.gotchas_discovered == ["cp1252 on Windows"]
    assert r.approach_outcome == "Added module X"
    assert r.recommendations == ["always set encoding=utf-8"]


# ---------------------------------------------------------------------------
# _parse_response unit tests
# ---------------------------------------------------------------------------


def test_parse_response_valid_json() -> None:
    data = {
        "file_insights": [
            {"path": "src/foo.py", "observation": "uses factory", "category": "pattern"}
        ],
        "patterns_discovered": ["factory pattern"],
        "gotchas_discovered": ["windows encoding"],
        "approach_outcome": "Implemented widget",
        "recommendations": ["use utf-8 everywhere"],
    }
    result = _parse_response(json.dumps(data))
    assert len(result.file_insights) == 1
    assert result.file_insights[0].path == "src/foo.py"
    assert result.patterns_discovered == ["factory pattern"]
    assert result.gotchas_discovered == ["windows encoding"]
    assert result.approach_outcome == "Implemented widget"
    assert result.recommendations == ["use utf-8 everywhere"]


def test_parse_response_with_preamble() -> None:
    """Model may prefix text before JSON — still parses."""
    raw = 'Here are the insights:\n{"file_insights": [], "patterns_discovered": ["p1"], "gotchas_discovered": [], "approach_outcome": "done", "recommendations": []}'
    result = _parse_response(raw)
    assert result.patterns_discovered == ["p1"]
    assert result.approach_outcome == "done"


def test_parse_response_empty_string() -> None:
    result = _parse_response("")
    assert result == InsightResult()


def test_parse_response_invalid_json() -> None:
    result = _parse_response("{not valid json}")
    assert result == InsightResult()


def test_parse_response_no_json_braces() -> None:
    result = _parse_response("just some text with no JSON")
    assert result == InsightResult()


def test_parse_response_invalid_category_normalized() -> None:
    """Unknown category falls back to 'pattern'."""
    data = {
        "file_insights": [
            {"path": "x.py", "observation": "obs", "category": "unknown_cat"}
        ],
        "patterns_discovered": [],
        "gotchas_discovered": [],
        "approach_outcome": "",
        "recommendations": [],
    }
    result = _parse_response(json.dumps(data))
    assert result.file_insights[0].category == "pattern"


def test_parse_response_skips_invalid_file_insights() -> None:
    """File insights missing path or observation are skipped."""
    data = {
        "file_insights": [
            {"observation": "no path here", "category": "pattern"},
            {"path": "x.py", "category": "gotcha"},  # no observation
            {"path": "y.py", "observation": "valid", "category": "convention"},
        ],
        "patterns_discovered": [],
        "gotchas_discovered": [],
        "approach_outcome": "",
        "recommendations": [],
    }
    result = _parse_response(json.dumps(data))
    assert len(result.file_insights) == 1
    assert result.file_insights[0].path == "y.py"


# ---------------------------------------------------------------------------
# write_insights tests
# ---------------------------------------------------------------------------


def test_write_insights_gotchas_md(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    result = InsightResult(gotchas_discovered=["Always use encoding=utf-8", "No emoji in output"])
    write_insights(result, memory_dir)

    gotchas_file = memory_dir / "gotchas.md"
    assert gotchas_file.exists()
    content = gotchas_file.read_text(encoding="utf-8")
    assert "Always use encoding=utf-8" in content
    assert "No emoji in output" in content
    assert "# Gotchas" in content


def test_write_insights_gotchas_md_appends(tmp_path: Path) -> None:
    """Second call appends to existing gotchas.md without repeating the header."""
    memory_dir = tmp_path / "memory"
    result1 = InsightResult(gotchas_discovered=["first gotcha"])
    result2 = InsightResult(gotchas_discovered=["second gotcha"])
    write_insights(result1, memory_dir)
    write_insights(result2, memory_dir)

    content = (memory_dir / "gotchas.md").read_text(encoding="utf-8")
    assert "first gotcha" in content
    assert "second gotcha" in content
    # Header appears only once
    assert content.count("# Gotchas") == 1


def test_write_insights_codebase_map_json(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    result = InsightResult(
        file_insights=[
            FileInsight(path="src/config.py", observation="has GolemConfig dataclass", category="convention"),
            FileInsight(path="src/tools.py", observation="uses MCP tools", category="pattern"),
        ]
    )
    write_insights(result, memory_dir)

    map_file = memory_dir / "codebase_map.json"
    assert map_file.exists()
    data = json.loads(map_file.read_text(encoding="utf-8"))
    assert "discovered_files" in data
    assert "src/config.py" in data["discovered_files"]
    assert "src/tools.py" in data["discovered_files"]
    assert data["discovered_files"]["src/config.py"]["category"] == "convention"


def test_write_insights_codebase_map_merges_existing(tmp_path: Path) -> None:
    """write_insights merges new file insights with existing codebase_map.json entries."""
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    existing = {
        "discovered_files": {
            "src/old.py": {"description": "old file", "category": "general", "discovered_at": "2026-01-01"}
        },
        "last_updated": "2026-01-01T00:00:00+00:00",
    }
    (memory_dir / "codebase_map.json").write_text(json.dumps(existing), encoding="utf-8")

    result = InsightResult(
        file_insights=[FileInsight(path="src/new.py", observation="new discovery", category="dependency")]
    )
    write_insights(result, memory_dir)

    data = json.loads((memory_dir / "codebase_map.json").read_text(encoding="utf-8"))
    assert "src/old.py" in data["discovered_files"]
    assert "src/new.py" in data["discovered_files"]


def test_write_insights_patterns_json(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    result = InsightResult(
        patterns_discovered=["use async/await", "factory pattern"],
        recommendations=["prefer uv over pip"],
        approach_outcome="Implemented insight extractor",
    )
    write_insights(result, memory_dir)

    patterns_file = memory_dir / "patterns.json"
    assert patterns_file.exists()
    data = json.loads(patterns_file.read_text(encoding="utf-8"))
    assert "use async/await" in data["patterns"]
    assert "factory pattern" in data["patterns"]
    assert "prefer uv over pip" in data["recommendations"]
    assert "Implemented insight extractor" in data["outcomes"]


def test_write_insights_patterns_json_merges(tmp_path: Path) -> None:
    """Second write appends to existing patterns.json."""
    memory_dir = tmp_path / "memory"
    write_insights(InsightResult(patterns_discovered=["p1"]), memory_dir)
    write_insights(InsightResult(patterns_discovered=["p2"]), memory_dir)

    data = json.loads((memory_dir / "patterns.json").read_text(encoding="utf-8"))
    assert "p1" in data["patterns"]
    assert "p2" in data["patterns"]


def test_write_insights_creates_memory_dir(tmp_path: Path) -> None:
    """write_insights creates memory_dir if it doesn't exist."""
    memory_dir = tmp_path / "deep" / "nested" / "memory"
    result = InsightResult(gotchas_discovered=["test gotcha"])
    write_insights(result, memory_dir)
    assert memory_dir.exists()
    assert (memory_dir / "gotchas.md").exists()


def test_write_insights_empty_result_no_files(tmp_path: Path) -> None:
    """Empty InsightResult writes nothing."""
    memory_dir = tmp_path / "memory"
    write_insights(InsightResult(), memory_dir)
    # No files should be created for an empty result
    assert not (memory_dir / "gotchas.md").exists()
    assert not (memory_dir / "codebase_map.json").exists()
    assert not (memory_dir / "patterns.json").exists()


# ---------------------------------------------------------------------------
# extract_insights tests (mocked SDK + subprocess)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_insights_success(tmp_path: Path) -> None:
    """extract_insights calls git diff and parses model response."""
    config = GolemConfig(insight_model="claude-haiku-4-5-20251001")

    fake_stat = "src/foo.py | 5 +++++\n 1 file changed, 5 insertions(+)"
    fake_diff = "diff --git a/src/foo.py b/src/foo.py\n+def new_function(): pass\n"

    expected_json = json.dumps({
        "file_insights": [{"path": "src/foo.py", "observation": "new function added", "category": "pattern"}],
        "patterns_discovered": ["define functions clearly"],
        "gotchas_discovered": [],
        "approach_outcome": "Added new_function to foo.py",
        "recommendations": ["document new functions"],
    })

    fake_stat_proc = MagicMock()
    fake_stat_proc.stdout = fake_stat
    fake_diff_proc = MagicMock()
    fake_diff_proc.stdout = fake_diff

    async def fake_query(prompt, options=None, **kwargs):  # type: ignore[misc]
        from claude_agent_sdk import ResultMessage
        msg = MagicMock(spec=ResultMessage)
        msg.result = expected_json
        yield msg

    with patch("subprocess.run", side_effect=[fake_stat_proc, fake_diff_proc]), \
         patch("golem.insight_extractor.query", side_effect=fake_query):
        result = await extract_insights(tmp_path, "TICKET-001", config)

    assert len(result.file_insights) == 1
    assert result.file_insights[0].path == "src/foo.py"
    assert result.patterns_discovered == ["define functions clearly"]
    assert result.approach_outcome == "Added new_function to foo.py"
    assert result.recommendations == ["document new functions"]


@pytest.mark.asyncio
async def test_extract_insights_empty_diff(tmp_path: Path) -> None:
    """Empty diff returns empty InsightResult without calling the SDK."""
    config = GolemConfig()

    fake_stat_proc = MagicMock()
    fake_stat_proc.stdout = ""
    fake_diff_proc = MagicMock()
    fake_diff_proc.stdout = ""

    with patch("subprocess.run", side_effect=[fake_stat_proc, fake_diff_proc]):
        result = await extract_insights(tmp_path, "TICKET-002", config)

    assert result == InsightResult()


@pytest.mark.asyncio
async def test_extract_insights_graceful_failure(tmp_path: Path) -> None:
    """Exception in subprocess.run returns empty InsightResult instead of raising."""
    config = GolemConfig()

    with patch("subprocess.run", side_effect=OSError("git not found")):
        result = await extract_insights(tmp_path, "TICKET-003", config)

    assert result == InsightResult()


@pytest.mark.asyncio
async def test_extract_insights_sdk_failure_graceful(tmp_path: Path) -> None:
    """Exception from Claude SDK returns empty InsightResult instead of raising."""
    config = GolemConfig()

    fake_stat_proc = MagicMock()
    fake_stat_proc.stdout = "src/x.py | 1 +"
    fake_diff_proc = MagicMock()
    fake_diff_proc.stdout = "diff content here"

    async def bad_query(*args, **kwargs):  # type: ignore[misc]
        raise RuntimeError("SDK unavailable")
        yield  # make it a generator

    with patch("subprocess.run", side_effect=[fake_stat_proc, fake_diff_proc]), \
         patch("golem.insight_extractor.query", side_effect=bad_query):
        result = await extract_insights(tmp_path, "TICKET-004", config)

    assert result == InsightResult()


@pytest.mark.asyncio
async def test_extract_insights_truncates_large_diff(tmp_path: Path) -> None:
    """Diff larger than _MAX_DIFF_CHARS is truncated before sending to SDK."""
    from golem.insight_extractor import _MAX_DIFF_CHARS

    config = GolemConfig()
    large_diff = "+" + "x" * (_MAX_DIFF_CHARS + 5000)

    fake_stat_proc = MagicMock()
    fake_stat_proc.stdout = "many files changed"
    fake_diff_proc = MagicMock()
    fake_diff_proc.stdout = large_diff

    captured_prompts: list[str] = []

    async def capturing_query(prompt, options=None, **kwargs):  # type: ignore[misc]
        captured_prompts.append(prompt)
        from claude_agent_sdk import ResultMessage
        msg = MagicMock(spec=ResultMessage)
        msg.result = json.dumps({
            "file_insights": [],
            "patterns_discovered": [],
            "gotchas_discovered": [],
            "approach_outcome": "",
            "recommendations": [],
        })
        yield msg

    with patch("subprocess.run", side_effect=[fake_stat_proc, fake_diff_proc]), \
         patch("golem.insight_extractor.query", side_effect=capturing_query):
        await extract_insights(tmp_path, "TICKET-005", config)

    assert len(captured_prompts) == 1
    assert "[... diff truncated ...]" in captured_prompts[0]


# ---------------------------------------------------------------------------
# Config gating tests
# ---------------------------------------------------------------------------


def test_config_insight_extraction_enabled_default() -> None:
    config = GolemConfig()
    assert config.insight_extraction_enabled is True


def test_config_insight_model_default() -> None:
    config = GolemConfig()
    assert config.insight_model == "claude-haiku-4-5-20251001"


def test_config_insight_extraction_disabled() -> None:
    config = GolemConfig(insight_extraction_enabled=False)
    assert config.insight_extraction_enabled is False


@pytest.mark.asyncio
async def test_spawn_junior_dev_skips_extraction_when_disabled(tmp_path: Path) -> None:
    """When insight_extraction_enabled=False, extract_insights is never called."""
    import os
    from golem.tickets import Ticket, TicketContext
    from golem.writer import spawn_junior_dev

    golem_dir = tmp_path / ".golem"
    (golem_dir / "tickets").mkdir(parents=True)
    ticket = Ticket(
        id="TICKET-100",
        type="task",
        title="Test ticket",
        status="pending",
        priority="medium",
        created_by="tech_lead",
        assigned_to="writer",
        context=TicketContext(),
    )
    config = GolemConfig(insight_extraction_enabled=False)

    from golem.supervisor import ContinuationResult, ToolCallRegistry
    ok_result = ContinuationResult(
        result_text="done", cost_usd=0.0, input_tokens=0, output_tokens=0,
        turns=3, duration_s=0.1, stalled=False, stall_turn=None,
        registry=ToolCallRegistry(), continuation_count=0, exhausted=False,
    )

    extract_called: list[bool] = []

    async def fake_extract(*args, **kwargs):  # type: ignore[misc]
        extract_called.append(True)
        return InsightResult()

    with patch("golem.writer.continuation_supervised_session", AsyncMock(return_value=ok_result)), \
         patch("golem.insight_extractor.extract_insights", side_effect=fake_extract), \
         patch.dict(os.environ, {"GOLEM_TEST_MODE": "1"}):
        await spawn_junior_dev(ticket, str(tmp_path), config, golem_dir=golem_dir)

    assert extract_called == [], "extract_insights should not be called when disabled"


@pytest.mark.asyncio
async def test_spawn_junior_dev_skips_extraction_in_test_mode(tmp_path: Path) -> None:
    """GOLEM_TEST_MODE=1 suppresses insight extraction regardless of config."""
    import os
    from golem.tickets import Ticket, TicketContext
    from golem.writer import spawn_junior_dev

    golem_dir = tmp_path / ".golem"
    (golem_dir / "tickets").mkdir(parents=True)
    ticket = Ticket(
        id="TICKET-101",
        type="task",
        title="Test ticket",
        status="pending",
        priority="medium",
        created_by="tech_lead",
        assigned_to="writer",
        context=TicketContext(),
    )
    config = GolemConfig(insight_extraction_enabled=True)

    from golem.supervisor import ContinuationResult, ToolCallRegistry
    ok_result = ContinuationResult(
        result_text="done", cost_usd=0.0, input_tokens=0, output_tokens=0,
        turns=3, duration_s=0.1, stalled=False, stall_turn=None,
        registry=ToolCallRegistry(), continuation_count=0, exhausted=False,
    )

    extract_called: list[bool] = []

    async def fake_extract(*args, **kwargs):  # type: ignore[misc]
        extract_called.append(True)
        return InsightResult()

    with patch("golem.writer.continuation_supervised_session", AsyncMock(return_value=ok_result)), \
         patch("golem.insight_extractor.extract_insights", side_effect=fake_extract), \
         patch.dict(os.environ, {"GOLEM_TEST_MODE": "1"}):
        await spawn_junior_dev(ticket, str(tmp_path), config, golem_dir=golem_dir)

    assert extract_called == [], "extract_insights should not run in GOLEM_TEST_MODE"
