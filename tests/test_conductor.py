from __future__ import annotations

from golem.conductor import ClassificationResult, classify_spec


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
