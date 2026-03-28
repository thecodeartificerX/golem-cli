"""Tests for the multi-pass PR review engine (pr_review.py)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from golem.pr_review import (
    ReviewFinding,
    ReviewReport,
    _deduplicate_findings,
    _parse_findings,
    _parse_quick_scan,
    _fetch_pr_info,
    _fetch_pr_diff,
    _fetch_pr_comments,
)


# ---------------------------------------------------------------------------
# _deduplicate_findings
# ---------------------------------------------------------------------------


def _make_finding(
    file: str = "src/foo.py",
    line: int = 10,
    title: str = "Test finding",
    body: str = "Details here",
    severity: str = "warning",
    category: str = "quality",
    pass_name: str = "quality",
) -> ReviewFinding:
    return ReviewFinding(
        file=file,
        line=line,
        title=title,
        body=body,
        severity=severity,
        category=category,
        pass_name=pass_name,
    )


def test_deduplicate_empty() -> None:
    assert _deduplicate_findings([]) == []


def test_deduplicate_no_duplicates() -> None:
    findings = [
        _make_finding(file="a.py", line=1, title="issue one"),
        _make_finding(file="b.py", line=2, title="issue two"),
    ]
    result = _deduplicate_findings(findings)
    assert len(result) == 2


def test_deduplicate_exact_duplicate_keeps_one() -> None:
    f = _make_finding(file="a.py", line=5, title="same issue")
    result = _deduplicate_findings([f, f])
    assert len(result) == 1


def test_deduplicate_keeps_highest_severity() -> None:
    info = _make_finding(file="a.py", line=5, title="same title", severity="info")
    warning = _make_finding(file="a.py", line=5, title="same title", severity="warning")
    critical = _make_finding(file="a.py", line=5, title="same title", severity="critical")

    result = _deduplicate_findings([info, warning, critical])
    assert len(result) == 1
    assert result[0].severity == "critical"


def test_deduplicate_keeps_highest_severity_reverse_order() -> None:
    critical = _make_finding(file="a.py", line=5, title="same title", severity="critical")
    info = _make_finding(file="a.py", line=5, title="same title", severity="info")

    result = _deduplicate_findings([critical, info])
    assert len(result) == 1
    assert result[0].severity == "critical"


def test_deduplicate_title_comparison_is_case_insensitive() -> None:
    f1 = _make_finding(file="a.py", line=5, title="SAME TITLE", severity="warning")
    f2 = _make_finding(file="a.py", line=5, title="same title", severity="info")

    result = _deduplicate_findings([f1, f2])
    assert len(result) == 1
    assert result[0].severity == "warning"


def test_deduplicate_different_lines_kept_separately() -> None:
    f1 = _make_finding(file="a.py", line=5, title="same title")
    f2 = _make_finding(file="a.py", line=10, title="same title")

    result = _deduplicate_findings([f1, f2])
    assert len(result) == 2


def test_deduplicate_different_files_kept_separately() -> None:
    f1 = _make_finding(file="a.py", line=5, title="same title")
    f2 = _make_finding(file="b.py", line=5, title="same title")

    result = _deduplicate_findings([f1, f2])
    assert len(result) == 2


def test_deduplicate_warning_vs_info_keeps_warning() -> None:
    warning = _make_finding(severity="warning")
    info = _make_finding(severity="info")

    result = _deduplicate_findings([info, warning])
    assert result[0].severity == "warning"


# ---------------------------------------------------------------------------
# _parse_findings
# ---------------------------------------------------------------------------


def test_parse_findings_valid_json_array() -> None:
    raw = json.dumps([
        {
            "file": "src/auth.py",
            "line": 42,
            "title": "Hardcoded secret",
            "body": "The API key is hardcoded in the source.",
            "severity": "critical",
            "category": "security",
        }
    ])
    findings = _parse_findings(raw, "security")
    assert len(findings) == 1
    assert findings[0].file == "src/auth.py"
    assert findings[0].line == 42
    assert findings[0].title == "Hardcoded secret"
    assert findings[0].severity == "critical"
    assert findings[0].category == "security"
    assert findings[0].pass_name == "security"


def test_parse_findings_with_preamble() -> None:
    raw = "Here are my findings:\n\n" + json.dumps([
        {"file": "foo.py", "line": 1, "title": "Issue", "body": "...", "severity": "warning", "category": "quality"}
    ]) + "\n\nHope that helps!"
    findings = _parse_findings(raw, "quality")
    assert len(findings) == 1
    assert findings[0].title == "Issue"


def test_parse_findings_empty_array() -> None:
    findings = _parse_findings("[]", "security")
    assert findings == []


def test_parse_findings_invalid_json_returns_empty() -> None:
    findings = _parse_findings("not valid json at all", "quality")
    assert findings == []


def test_parse_findings_invalid_severity_normalized() -> None:
    raw = json.dumps([
        {"file": "a.py", "line": 1, "title": "Title", "body": "Body", "severity": "URGENT", "category": "security"}
    ])
    findings = _parse_findings(raw, "security")
    assert len(findings) == 1
    assert findings[0].severity == "info"  # unknown → normalized to "info"


def test_parse_findings_invalid_category_normalized() -> None:
    raw = json.dumps([
        {"file": "a.py", "line": 1, "title": "Title", "body": "Body", "severity": "warning", "category": "unknown_cat"}
    ])
    findings = _parse_findings(raw, "quality")
    assert len(findings) == 1
    assert findings[0].category == "quality"  # unknown → normalized to "quality"


def test_parse_findings_skips_missing_title() -> None:
    raw = json.dumps([
        {"file": "a.py", "line": 1, "title": "", "body": "Body", "severity": "warning", "category": "quality"},
        {"file": "b.py", "line": 2, "title": "Has title", "body": "Body", "severity": "info", "category": "quality"},
    ])
    findings = _parse_findings(raw, "quality")
    assert len(findings) == 1
    assert findings[0].title == "Has title"


def test_parse_findings_non_integer_line_defaults_to_zero() -> None:
    raw = json.dumps([
        {"file": "a.py", "line": "not a number", "title": "Title", "body": "Body", "severity": "info", "category": "quality"}
    ])
    findings = _parse_findings(raw, "quality")
    assert len(findings) == 1
    assert findings[0].line == 0


# ---------------------------------------------------------------------------
# _parse_quick_scan
# ---------------------------------------------------------------------------


def test_parse_quick_scan_valid() -> None:
    raw = json.dumps({"complexity": "trivial", "reasoning": "small change", "file_count": 2})
    result = _parse_quick_scan(raw)
    assert result["complexity"] == "trivial"
    assert result["reasoning"] == "small change"


def test_parse_quick_scan_with_preamble() -> None:
    raw = "The PR is small.\n\n" + json.dumps({"complexity": "standard", "reasoning": "moderate"})
    result = _parse_quick_scan(raw)
    assert result["complexity"] == "standard"


def test_parse_quick_scan_invalid_json_returns_default() -> None:
    result = _parse_quick_scan("this is not JSON at all")
    assert result["complexity"] == "standard"
    assert "parse error" in str(result.get("reasoning", ""))


def test_parse_quick_scan_complex() -> None:
    raw = json.dumps({"complexity": "complex", "reasoning": "large PR with auth changes"})
    result = _parse_quick_scan(raw)
    assert result["complexity"] == "complex"


# ---------------------------------------------------------------------------
# ReviewReport construction
# ---------------------------------------------------------------------------


def test_review_report_defaults() -> None:
    report = ReviewReport()
    assert report.findings == []
    assert report.summary == ""
    assert report.complexity == "standard"
    assert report.passes_run == []
    assert report.duration_s == 0.0
    assert report.cost_usd == 0.0


def test_review_report_with_findings() -> None:
    findings = [
        _make_finding(severity="critical"),
        _make_finding(severity="warning", title="Another issue"),
    ]
    report = ReviewReport(
        findings=findings,
        summary="2 findings",
        complexity="complex",
        passes_run=["security", "quality"],
        duration_s=3.5,
        cost_usd=0.0123,
    )
    assert len(report.findings) == 2
    assert report.complexity == "complex"
    assert report.cost_usd == pytest.approx(0.0123)
    assert "security" in report.passes_run


# ---------------------------------------------------------------------------
# ReviewFinding construction
# ---------------------------------------------------------------------------


def test_review_finding_fields() -> None:
    f = ReviewFinding(
        file="src/db.py",
        line=100,
        title="SQL injection risk",
        body="User input used directly in query",
        severity="critical",
        category="security",
        pass_name="security",
    )
    assert f.file == "src/db.py"
    assert f.line == 100
    assert f.severity == "critical"
    assert f.category == "security"


# ---------------------------------------------------------------------------
# _fetch_pr_* with mocked subprocess
# ---------------------------------------------------------------------------


def test_fetch_pr_info_success() -> None:
    mock_data = {"title": "Add auth", "body": "Implements auth", "number": 42, "state": "open", "files": [], "comments": []}
    with patch("golem.pr_review._run_gh") as mock_gh:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(mock_data)
        mock_gh.return_value = mock_result

        result = _fetch_pr_info(42, "owner/repo")
        assert result["title"] == "Add auth"
        assert result["number"] == 42


def test_fetch_pr_info_failure_raises() -> None:
    with patch("golem.pr_review._run_gh") as mock_gh:
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "not found"
        mock_gh.return_value = mock_result

        with pytest.raises(RuntimeError, match="gh pr view failed"):
            _fetch_pr_info(99, "owner/repo")


def test_fetch_pr_diff_success() -> None:
    diff_text = "diff --git a/foo.py b/foo.py\n+new line\n"
    with patch("golem.pr_review._run_gh") as mock_gh:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = diff_text
        mock_gh.return_value = mock_result

        result = _fetch_pr_diff(42, "owner/repo")
        assert result == diff_text


def test_fetch_pr_diff_failure_raises() -> None:
    with patch("golem.pr_review._run_gh") as mock_gh:
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "PR not found"
        mock_gh.return_value = mock_result

        with pytest.raises(RuntimeError, match="gh pr diff failed"):
            _fetch_pr_diff(99, "owner/repo")


def test_fetch_pr_comments_success() -> None:
    comments = [{"id": 1, "body": "Nice code", "path": "src/foo.py", "line": 5}]
    with patch("golem.pr_review._run_gh") as mock_gh:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(comments)
        mock_gh.return_value = mock_result

        result = _fetch_pr_comments(42, "owner/repo")
        assert len(result) == 1
        assert result[0]["body"] == "Nice code"


def test_fetch_pr_comments_failure_returns_empty() -> None:
    with patch("golem.pr_review._run_gh") as mock_gh:
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "API error"
        mock_gh.return_value = mock_result

        result = _fetch_pr_comments(99, "owner/repo")
        assert result == []


# ---------------------------------------------------------------------------
# Complexity gating: trivial skips deep_analysis
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_review_trivial_skips_deep(tmp_path: Path) -> None:
    """Trivial complexity should skip deep_analysis pass."""
    from golem.config import GolemConfig
    from golem.pr_review import run_review

    config = GolemConfig()

    pr_info = {"title": "Trivial fix", "body": "", "number": 1, "state": "open", "files": [], "comments": []}
    diff_text = "+ one line added\n"
    quick_scan_result = {"complexity": "trivial", "reasoning": "tiny change", "file_count": 1}

    with patch("golem.pr_review._fetch_pr_info", return_value=pr_info), \
         patch("golem.pr_review._fetch_pr_diff", return_value=diff_text), \
         patch("golem.pr_review._fetch_pr_comments", return_value=[]), \
         patch("golem.pr_review._run_quick_scan", return_value=(quick_scan_result, 0.0)), \
         patch("golem.pr_review._run_pass", return_value=("", [], 0.0)):
        report = await run_review(1, "owner/repo", config, passes=["quick_scan", "deep", "security"])

    # deep should be skipped when complexity=trivial
    assert "deep" not in report.passes_run
    assert report.complexity == "trivial"


@pytest.mark.asyncio
async def test_run_review_standard_runs_deep(tmp_path: Path) -> None:
    """Standard complexity should include deep_analysis pass."""
    from golem.config import GolemConfig
    from golem.pr_review import run_review

    config = GolemConfig()

    pr_info = {"title": "Standard PR", "body": "", "number": 2, "state": "open", "files": [], "comments": []}
    diff_text = "+ many lines\n" * 200
    quick_scan_result = {"complexity": "standard", "reasoning": "moderate change", "file_count": 5}

    with (
        patch("golem.pr_review._fetch_pr_info", return_value=pr_info),
        patch("golem.pr_review._fetch_pr_diff", return_value=diff_text),
        patch("golem.pr_review._fetch_pr_comments", return_value=[]),
        patch("golem.pr_review._run_quick_scan", return_value=(quick_scan_result, 0.0)),
        patch("golem.pr_review._run_pass", return_value=("", [], 0.0)),
    ):
        report = await run_review(2, "owner/repo", config, passes=["quick_scan", "deep", "security"])

    assert "deep" in report.passes_run
    assert report.complexity == "standard"


# ---------------------------------------------------------------------------
# Deduplication via run_review integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_review_deduplicates_across_passes() -> None:
    """run_review should deduplicate findings from multiple passes."""
    from golem.config import GolemConfig
    from golem.pr_review import run_review

    config = GolemConfig()

    pr_info = {"title": "PR", "body": "", "number": 3, "state": "open", "files": [], "comments": []}
    diff_text = "+ code\n"
    quick_scan_result = {"complexity": "standard", "reasoning": "ok", "file_count": 1}

    # Both passes emit the same finding (different severity — keep highest)
    shared_finding_security = json.dumps([{
        "file": "src/auth.py", "line": 10, "title": "Missing validation",
        "body": "from security pass", "severity": "critical", "category": "security",
    }])
    shared_finding_quality = json.dumps([{
        "file": "src/auth.py", "line": 10, "title": "Missing validation",
        "body": "from quality pass", "severity": "warning", "category": "quality",
    }])

    call_count = [0]

    async def mock_run_pass(pass_name: str, system_prompt: str, user_message: str, model: str):
        call_count[0] += 1
        if pass_name == "security":
            return (shared_finding_security, _parse_findings(shared_finding_security, "security"), 0.001)
        elif pass_name == "quality":
            return (shared_finding_quality, _parse_findings(shared_finding_quality, "quality"), 0.001)
        return ("", [], 0.0)

    with (
        patch("golem.pr_review._fetch_pr_info", return_value=pr_info),
        patch("golem.pr_review._fetch_pr_diff", return_value=diff_text),
        patch("golem.pr_review._fetch_pr_comments", return_value=[]),
        patch("golem.pr_review._run_quick_scan", return_value=(quick_scan_result, 0.0)),
        patch("golem.pr_review._run_pass", side_effect=mock_run_pass),
    ):
        report = await run_review(3, "owner/repo", config, passes=["quick_scan", "security", "quality"])

    # Should have exactly 1 finding after dedup (keep critical over warning)
    assert len(report.findings) == 1
    assert report.findings[0].severity == "critical"
