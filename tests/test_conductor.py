from __future__ import annotations

from unittest.mock import patch

from golem.conductor import ClassificationResult, _heuristic_fast_path, classify_spec


def _assert_valid(result: ClassificationResult) -> None:
    assert result.complexity in ("TRIVIAL", "SIMPLE", "STANDARD", "CRITICAL")
    assert isinstance(result.reasoning, str)
    assert 0.0 <= result.confidence <= 1.0


def test_classify_trivial() -> None:
    # short spec (<500 chars), trivial keyword, <=2 file mentions
    spec = "Fix typo in the readme file."
    result = classify_spec(spec)
    assert result.complexity == "TRIVIAL"
    _assert_valid(result)


def test_classify_simple() -> None:
    # moderate spec with "config" keyword, 2-3 file mentions, <1500 chars
    spec = (
        "Update config to add a new env variable.\n"
        "Modify src/golem/config.py and update src/golem/cli.py accordingly.\n"
        "This is a small change."
    )
    result = classify_spec(spec)
    assert result.complexity == "SIMPLE"
    _assert_valid(result)


def test_classify_standard() -> None:
    # moderate spec: 4-10 file mentions, 1500-5000 chars, no special keywords
    files = "\n".join(
        f"Modify src/golem/module{i}.py to add helper function." for i in range(6)
    )
    spec = files + "\n" + ("This is a general refactor of multiple modules.\n" * 30)
    result = classify_spec(spec)
    assert result.complexity == "STANDARD"
    _assert_valid(result)


def test_classify_critical_keywords() -> None:
    # 2+ critical keyword hits (auth + migration)
    spec = "Implement authentication system with database migration for user credentials."
    result = classify_spec(spec)
    assert result.complexity == "CRITICAL"
    _assert_valid(result)


def test_classify_critical_by_file_count() -> None:
    # 10+ file mentions triggers CRITICAL
    files = "\n".join(
        f"Modify src/golem/module{i}.py to change something." for i in range(12)
    )
    spec = files
    result = classify_spec(spec)
    assert result.complexity == "CRITICAL"
    _assert_valid(result)


def test_classify_defaults_to_standard() -> None:
    # ambiguous spec with no matching keywords, moderate size
    spec = "A" * 2000  # 2000 chars, no keywords, no file mentions
    result = classify_spec(spec)
    assert result.complexity == "STANDARD"
    _assert_valid(result)


def test_classify_empty_spec() -> None:
    # Empty spec: 0 chars, 0 file mentions, no keywords — classifies as SIMPLE (short, few files)
    result = classify_spec("")
    assert result.complexity in ("TRIVIAL", "SIMPLE", "STANDARD")
    _assert_valid(result)


# ---------------------------------------------------------------------------
# Heuristic fast-path tests (spec 07)
# ---------------------------------------------------------------------------


def test_heuristic_fast_path_returns_trivial_for_short_typo_spec() -> None:
    """Short spec with 'fix typo' pattern returns TRIVIAL with confidence >= 0.9."""
    result = _heuristic_fast_path("fix typo in the readme")
    assert result is not None
    assert result.complexity == "TRIVIAL"
    assert result.confidence >= 0.9


def test_heuristic_fast_path_returns_trivial_for_color_pattern() -> None:
    """Pattern 'change color' triggers the fast path."""
    result = _heuristic_fast_path("change the button color to blue")
    assert result is not None
    assert result.complexity == "TRIVIAL"


def test_heuristic_fast_path_returns_trivial_for_version_bump() -> None:
    """Pattern 'bump version' triggers the fast path."""
    result = _heuristic_fast_path("bump version to 1.2.3")
    assert result is not None
    assert result.complexity == "TRIVIAL"


def test_heuristic_fast_path_returns_trivial_for_remove_unused() -> None:
    """Pattern 'remove unused' triggers the fast path."""
    result = _heuristic_fast_path("remove unused imports from the config file")
    assert result is not None
    assert result.complexity == "TRIVIAL"


def test_heuristic_fast_path_returns_none_for_long_spec() -> None:
    """Spec over 30 words returns None even if it contains simple keywords."""
    # Build a spec with way more than 30 words containing simple keywords
    long_spec = " ".join(["fix typo"] * 20)  # 40 words well over the limit
    result = _heuristic_fast_path(long_spec)
    assert result is None


def test_heuristic_fast_path_returns_none_for_unmatched_short_spec() -> None:
    """Short spec with no matching pattern returns None."""
    result = _heuristic_fast_path("build a new authentication system")
    assert result is None


def test_classify_spec_calls_heuristic_first() -> None:
    """classify_spec returns the heuristic result without running the scorer when fast-path fires."""
    sentinel = ClassificationResult("TRIVIAL", "sentinel", 0.99)
    with patch("golem.conductor._heuristic_fast_path", return_value=sentinel) as mock_heuristic:
        result = classify_spec("some spec text")
    mock_heuristic.assert_called_once()
    assert result is sentinel


def test_classify_spec_falls_through_when_heuristic_returns_none() -> None:
    """classify_spec runs the keyword scorer when heuristic returns None."""
    with patch("golem.conductor._heuristic_fast_path", return_value=None):
        result = classify_spec("implement authentication system with database migration for user credentials")
    assert result.complexity == "CRITICAL"
