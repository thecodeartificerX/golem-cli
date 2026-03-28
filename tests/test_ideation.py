"""Tests for the ideation module (golem ideate command)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from golem.config import GolemConfig
from golem.ideation import (
    ALL_CATEGORIES,
    Idea,
    IdeaCategory,
    IdeationResult,
    _gather_codebase_context,
    _get_prompt_for_category,
    _parse_ideas,
    run_all_ideation,
    run_ideation,
)


# ---------------------------------------------------------------------------
# Dataclass construction tests
# ---------------------------------------------------------------------------


def test_idea_construction() -> None:
    idea = Idea(
        title="Remove unused import",
        description="The `os` module is imported but never used in this file.",
        category="code_improvements",
        file="src/golem/config.py",
        priority="low",
        effort="small",
    )
    assert idea.title == "Remove unused import"
    assert idea.description == "The `os` module is imported but never used in this file."
    assert idea.category == "code_improvements"
    assert idea.file == "src/golem/config.py"
    assert idea.priority == "low"
    assert idea.effort == "small"


def test_idea_empty_file_allowed() -> None:
    idea = Idea(
        title="Centralize error handling",
        description="Cross-cutting concern.",
        category="code_quality",
        file="",
        priority="high",
        effort="large",
    )
    assert idea.file == ""


def test_idea_all_priorities() -> None:
    for priority in ("high", "medium", "low"):
        idea = Idea(title="t", description="d", category="code_improvements", file="", priority=priority, effort="small")
        assert idea.priority == priority


def test_idea_all_efforts() -> None:
    for effort in ("small", "medium", "large"):
        idea = Idea(title="t", description="d", category="code_improvements", file="", priority="medium", effort=effort)
        assert idea.effort == effort


def test_ideation_result_defaults() -> None:
    result = IdeationResult(category="code_improvements")
    assert result.category == "code_improvements"
    assert result.ideas == []
    assert result.summary == ""
    assert result.duration_s == 0.0


def test_ideation_result_with_ideas() -> None:
    ideas = [
        Idea(title="Idea 1", description="Desc 1", category="security_hardening", file="", priority="high", effort="small"),
        Idea(title="Idea 2", description="Desc 2", category="security_hardening", file="src/foo.py", priority="medium", effort="large"),
    ]
    result = IdeationResult(category="security_hardening", ideas=ideas, summary="Fix auth gaps", duration_s=3.5)
    assert len(result.ideas) == 2
    assert result.ideas[0].title == "Idea 1"
    assert result.summary == "Fix auth gaps"
    assert result.duration_s == 3.5


def test_all_categories_tuple_length() -> None:
    assert len(ALL_CATEGORIES) == 6


def test_all_categories_values() -> None:
    expected = {
        "code_improvements",
        "ui_ux_improvements",
        "documentation_gaps",
        "security_hardening",
        "performance_optimizations",
        "code_quality",
    }
    assert set(ALL_CATEGORIES) == expected


# ---------------------------------------------------------------------------
# _get_prompt_for_category tests
# ---------------------------------------------------------------------------


def test_get_prompt_non_empty_for_all_categories() -> None:
    for category in ALL_CATEGORIES:
        prompt = _get_prompt_for_category(category)
        assert isinstance(prompt, str)
        assert len(prompt) > 50, f"Prompt for {category} is too short"


def test_get_prompt_code_improvements_mentions_dead_code() -> None:
    prompt = _get_prompt_for_category("code_improvements")
    assert "dead" in prompt.lower() or "unused" in prompt.lower() or "simplif" in prompt.lower()


def test_get_prompt_security_mentions_validation() -> None:
    prompt = _get_prompt_for_category("security_hardening")
    assert "validation" in prompt.lower() or "inject" in prompt.lower() or "input" in prompt.lower()


def test_get_prompt_performance_mentions_async() -> None:
    prompt = _get_prompt_for_category("performance_optimizations")
    assert "async" in prompt.lower() or "block" in prompt.lower() or "cache" in prompt.lower()


def test_get_prompt_returns_json_instructions() -> None:
    for category in ALL_CATEGORIES:
        prompt = _get_prompt_for_category(category)
        assert "JSON" in prompt, f"Prompt for {category} should mention JSON output format"


def test_get_prompt_unique_per_category() -> None:
    prompts = [_get_prompt_for_category(c) for c in ALL_CATEGORIES]
    # All prompts should be distinct
    assert len(set(prompts)) == 6


# ---------------------------------------------------------------------------
# _parse_ideas tests
# ---------------------------------------------------------------------------


def test_parse_ideas_valid_json() -> None:
    data = {
        "ideas": [
            {
                "title": "Remove dead code in validator.py",
                "description": "The _old_validate function is never called.",
                "file": "src/golem/validator.py",
                "priority": "low",
                "effort": "small",
            }
        ],
        "summary": "Minor dead code cleanup opportunity.",
    }
    ideas, summary = _parse_ideas(json.dumps(data), "code_improvements")
    assert len(ideas) == 1
    assert ideas[0].title == "Remove dead code in validator.py"
    assert ideas[0].category == "code_improvements"
    assert ideas[0].priority == "low"
    assert ideas[0].effort == "small"
    assert summary == "Minor dead code cleanup opportunity."


def test_parse_ideas_with_preamble() -> None:
    """Model may prefix text before the JSON object."""
    raw = 'Here are my findings:\n{"ideas": [{"title": "Fix X", "description": "Do Y", "file": "", "priority": "high", "effort": "medium"}], "summary": "One fix."}'
    ideas, summary = _parse_ideas(raw, "code_quality")
    assert len(ideas) == 1
    assert ideas[0].title == "Fix X"
    assert summary == "One fix."


def test_parse_ideas_empty_string() -> None:
    ideas, summary = _parse_ideas("", "code_improvements")
    assert ideas == []
    assert summary == ""


def test_parse_ideas_invalid_json() -> None:
    ideas, summary = _parse_ideas("{not valid json}", "code_improvements")
    assert ideas == []
    assert summary == ""


def test_parse_ideas_no_json_braces() -> None:
    ideas, summary = _parse_ideas("just some text here", "code_improvements")
    assert ideas == []
    assert summary == ""


def test_parse_ideas_unknown_priority_normalized() -> None:
    data = {
        "ideas": [{"title": "T", "description": "D", "file": "", "priority": "urgent", "effort": "small"}],
        "summary": "",
    }
    ideas, _ = _parse_ideas(json.dumps(data), "code_improvements")
    assert len(ideas) == 1
    assert ideas[0].priority == "medium"


def test_parse_ideas_unknown_effort_normalized() -> None:
    data = {
        "ideas": [{"title": "T", "description": "D", "file": "", "priority": "high", "effort": "enormous"}],
        "summary": "",
    }
    ideas, _ = _parse_ideas(json.dumps(data), "code_improvements")
    assert len(ideas) == 1
    assert ideas[0].effort == "medium"


def test_parse_ideas_skips_entries_without_title() -> None:
    data = {
        "ideas": [
            {"description": "No title here", "file": "", "priority": "high", "effort": "small"},
            {"title": "Has title", "description": "desc", "file": "", "priority": "low", "effort": "medium"},
        ],
        "summary": "",
    }
    ideas, _ = _parse_ideas(json.dumps(data), "code_improvements")
    assert len(ideas) == 1
    assert ideas[0].title == "Has title"


def test_parse_ideas_category_set_correctly() -> None:
    data = {
        "ideas": [{"title": "T", "description": "D", "file": "src/x.py", "priority": "high", "effort": "large"}],
        "summary": "s",
    }
    ideas, _ = _parse_ideas(json.dumps(data), "security_hardening")
    assert ideas[0].category == "security_hardening"


def test_parse_ideas_multiple_ideas() -> None:
    data = {
        "ideas": [
            {"title": f"Idea {i}", "description": f"Desc {i}", "file": "", "priority": "medium", "effort": "small"}
            for i in range(5)
        ],
        "summary": "Five improvements found.",
    }
    ideas, summary = _parse_ideas(json.dumps(data), "code_quality")
    assert len(ideas) == 5
    assert summary == "Five improvements found."


# ---------------------------------------------------------------------------
# _gather_codebase_context tests
# ---------------------------------------------------------------------------


def test_gather_context_returns_string(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text(
        "def hello():\n    pass\n", encoding="utf-8"
    )
    ctx = _gather_codebase_context(tmp_path, "code_improvements")
    assert isinstance(ctx, str)
    assert len(ctx) > 0


def test_gather_context_includes_project_root(tmp_path: Path) -> None:
    ctx = _gather_codebase_context(tmp_path, "code_improvements")
    assert str(tmp_path) in ctx


def test_gather_context_includes_file_content(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "example.py").write_text(
        "def my_function():\n    return 42\n", encoding="utf-8"
    )
    # Mock rg to return the file path
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = str(src / "example.py") + "\n"

    with patch("subprocess.run", return_value=mock_result):
        ctx = _gather_codebase_context(tmp_path, "code_improvements")

    assert "my_function" in ctx or "example.py" in ctx


def test_gather_context_truncates_to_max(tmp_path: Path) -> None:
    from golem.ideation import _MAX_CONTEXT_CHARS

    src = tmp_path / "src"
    src.mkdir()
    # Create a large file
    (src / "big.py").write_text("x = 1\n" * 20_000, encoding="utf-8")

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = str(src / "big.py") + "\n"

    with patch("subprocess.run", return_value=mock_result):
        ctx = _gather_codebase_context(tmp_path, "code_improvements")

    assert len(ctx) <= _MAX_CONTEXT_CHARS + 200  # small tolerance for header


def test_gather_context_handles_rg_not_found(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "fallback.py").write_text("def f(): pass\n", encoding="utf-8")

    with patch("subprocess.run", side_effect=FileNotFoundError("rg not found")):
        ctx = _gather_codebase_context(tmp_path, "code_improvements")

    # Should fall back gracefully and include the file via glob
    assert isinstance(ctx, str)


def test_gather_context_security_category(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "auth.py").write_text(
        "import subprocess\npassword = 'secret'\ndef run_cmd(cmd): subprocess.run(cmd, shell=True)\n",
        encoding="utf-8",
    )
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = str(src / "auth.py") + "\n"

    with patch("subprocess.run", return_value=mock_result):
        ctx = _gather_codebase_context(tmp_path, "security_hardening")

    assert isinstance(ctx, str)
    assert "security_hardening" in ctx


# ---------------------------------------------------------------------------
# run_ideation tests (mocked SDK)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_ideation_success(tmp_path: Path) -> None:
    config = GolemConfig()

    expected_response = json.dumps({
        "ideas": [
            {
                "title": "Extract duplicated validation logic",
                "description": "The validation logic in config.py and validator.py is duplicated.",
                "file": "src/golem/validator.py",
                "priority": "medium",
                "effort": "medium",
            }
        ],
        "summary": "Consolidate validation logic into a shared helper.",
    })

    async def fake_query(prompt, options=None, **kwargs):  # type: ignore[misc]
        from claude_agent_sdk import ResultMessage
        msg = MagicMock(spec=ResultMessage)
        msg.result = expected_response
        yield msg

    with patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="")), \
         patch("golem.ideation.query", side_effect=fake_query):
        result = await run_ideation("code_improvements", tmp_path, config, max_ideas=10)

    assert result.category == "code_improvements"
    assert len(result.ideas) == 1
    assert result.ideas[0].title == "Extract duplicated validation logic"
    assert result.ideas[0].priority == "medium"
    assert result.summary == "Consolidate validation logic into a shared helper."
    assert result.duration_s >= 0.0


@pytest.mark.asyncio
async def test_run_ideation_sdk_failure_returns_empty(tmp_path: Path) -> None:
    config = GolemConfig()

    async def bad_query(*args, **kwargs):  # type: ignore[misc]
        raise RuntimeError("SDK unavailable")
        yield  # make it a generator

    with patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="")), \
         patch("golem.ideation.query", side_effect=bad_query):
        result = await run_ideation("code_quality", tmp_path, config)

    assert result.category == "code_quality"
    assert result.ideas == []
    assert "failed" in result.summary.lower()


@pytest.mark.asyncio
async def test_run_ideation_max_ideas_cap(tmp_path: Path) -> None:
    config = GolemConfig()

    expected_response = json.dumps({
        "ideas": [
            {"title": f"Idea {i}", "description": f"Desc {i}", "file": "", "priority": "low", "effort": "small"}
            for i in range(15)
        ],
        "summary": "Many improvements found.",
    })

    async def fake_query(prompt, options=None, **kwargs):  # type: ignore[misc]
        from claude_agent_sdk import ResultMessage
        msg = MagicMock(spec=ResultMessage)
        msg.result = expected_response
        yield msg

    with patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="")), \
         patch("golem.ideation.query", side_effect=fake_query):
        result = await run_ideation("code_improvements", tmp_path, config, max_ideas=5)

    assert len(result.ideas) == 5


@pytest.mark.asyncio
async def test_run_ideation_uses_validator_model(tmp_path: Path) -> None:
    config = GolemConfig(validator_model="claude-sonnet-4-6")
    captured_options: list[object] = []

    async def capturing_query(prompt, options=None, **kwargs):  # type: ignore[misc]
        captured_options.append(options)
        from claude_agent_sdk import ResultMessage
        msg = MagicMock(spec=ResultMessage)
        msg.result = json.dumps({"ideas": [], "summary": ""})
        yield msg

    with patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="")), \
         patch("golem.ideation.query", side_effect=capturing_query):
        await run_ideation("documentation_gaps", tmp_path, config)

    assert len(captured_options) == 1
    from claude_agent_sdk import ClaudeAgentOptions
    opts = captured_options[0]
    assert isinstance(opts, ClaudeAgentOptions)
    assert opts.model == "claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_run_ideation_parses_assistant_message_fallback(tmp_path: Path) -> None:
    """AssistantMessage text blocks used when ResultMessage.result is empty."""
    config = GolemConfig()
    response_json = json.dumps({
        "ideas": [{"title": "Use caching", "description": "Cache repeated calls", "file": "", "priority": "high", "effort": "small"}],
        "summary": "Add caching layer.",
    })

    async def fake_query(prompt, options=None, **kwargs):  # type: ignore[misc]
        from claude_agent_sdk import AssistantMessage as AM
        from claude_agent_sdk import ResultMessage as RM
        # First: AssistantMessage with text
        am = MagicMock(spec=AM)
        text_block = MagicMock()
        text_block.text = response_json
        am.content = [text_block]
        yield am
        # Then: ResultMessage with empty result
        rm = MagicMock(spec=RM)
        rm.result = ""
        rm.usage = {}
        yield rm

    with patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="")), \
         patch("golem.ideation.query", side_effect=fake_query):
        result = await run_ideation("performance_optimizations", tmp_path, config)

    assert len(result.ideas) == 1
    assert result.ideas[0].title == "Use caching"


# ---------------------------------------------------------------------------
# run_all_ideation tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_all_ideation_returns_six_results(tmp_path: Path) -> None:
    config = GolemConfig()
    call_count = 0

    async def fake_query(prompt, options=None, **kwargs):  # type: ignore[misc]
        nonlocal call_count
        call_count += 1
        from claude_agent_sdk import ResultMessage
        msg = MagicMock(spec=ResultMessage)
        msg.result = json.dumps({"ideas": [], "summary": f"Pass {call_count} done."})
        yield msg

    with patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="")), \
         patch("golem.ideation.query", side_effect=fake_query):
        results = await run_all_ideation(tmp_path, config)

    assert len(results) == 6
    categories = [r.category for r in results]
    assert set(categories) == set(ALL_CATEGORIES)


@pytest.mark.asyncio
async def test_run_all_ideation_continues_after_failure(tmp_path: Path) -> None:
    """A single category failure does not abort remaining categories."""
    config = GolemConfig()
    call_count = 0

    async def sometimes_failing_query(prompt, options=None, **kwargs):  # type: ignore[misc]
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("Simulated SDK error for category 2")
        from claude_agent_sdk import ResultMessage
        msg = MagicMock(spec=ResultMessage)
        msg.result = json.dumps({"ideas": [], "summary": "ok"})
        yield msg

    with patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="")), \
         patch("golem.ideation.query", side_effect=sometimes_failing_query):
        results = await run_all_ideation(tmp_path, config)

    # Should still get 6 results (failed one returns empty result)
    assert len(results) == 6
    # The failed category has empty ideas
    assert any(r.ideas == [] for r in results)
