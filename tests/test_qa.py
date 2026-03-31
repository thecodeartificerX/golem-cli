from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from golem.qa import (
    QAFailureClassification,
    _detect_playwright,
    classify_failures,
    detect_infrastructure_checks,
    run_autofix,
    run_qa,
)


def test_run_qa_all_pass() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        result = run_qa(tmpdir, ["exit 0", "echo ok"], [])
        assert result.passed is True
        assert all(c.passed for c in result.checks)


def test_run_qa_one_fails() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        result = run_qa(
            tmpdir,
            [
                "exit 0",
                "exit 1",
            ],
            [],
        )
        assert result.passed is False
        passed = [c for c in result.checks if c.passed]
        failed = [c for c in result.checks if not c.passed]
        assert len(passed) == 1
        assert len(failed) == 1


def test_run_qa_captures_stdout_stderr() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        # Use a command that fails and produces output on Windows and Unix
        if sys.platform == "win32":
            cmd = 'cmd /c "echo hello_stdout && echo hello_stderr 1>&2 && exit 1"'
        else:
            cmd = "echo hello_stdout; echo hello_stderr >&2; exit 1"
        result = run_qa(tmpdir, [cmd], [])
        check = result.checks[0]
        assert check.passed is False
        assert "hello_stdout" in check.stdout or "hello_stderr" in check.stderr


def test_run_qa_summary_format() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        result = run_qa(tmpdir, ["exit 0", "exit 1", "exit 1"], [])
        assert "1/3" in result.summary
        assert "exit 1" in result.summary or "Failed" in result.summary


def test_run_qa_summary_all_pass() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        result = run_qa(tmpdir, ["exit 0", "echo ok"], [])
        assert "2/2" in result.summary
        # Should not mention "Failed" when all pass
        assert "Failed" not in result.summary or "0" in result.summary


def test_run_autofix_runs_ruff() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            run_autofix(tmpdir, ["ruff check ."])
            calls = [str(c.args[0]) for c in mock_run.call_args_list]
            assert any("ruff check --fix" in c for c in calls)
            assert any("ruff format" in c for c in calls)


def test_run_autofix_runs_prettier() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            run_autofix(tmpdir, ["npx prettier --check ."])
            calls = [str(c.args[0]) for c in mock_run.call_args_list]
            assert any("prettier --write" in c for c in calls)


def test_run_autofix_noop_no_matching_checks() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        with patch("subprocess.run") as mock_run:
            run_autofix(tmpdir, ["echo hello"])
            # Should not call subprocess at all — no ruff or prettier
            mock_run.assert_not_called()


def test_detect_infrastructure_checks_finds_ruff() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        pyproject = Path(tmpdir) / "pyproject.toml"
        pyproject.write_text("[tool.ruff]\nline-length = 120\n", encoding="utf-8")
        checks = detect_infrastructure_checks(Path(tmpdir))
        assert "ruff check ." in checks


def test_detect_infrastructure_checks_finds_npm_lint() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        pkg = Path(tmpdir) / "package.json"
        pkg.write_text(json.dumps({"scripts": {"lint": "eslint src"}}), encoding="utf-8")
        checks = detect_infrastructure_checks(Path(tmpdir))
        assert "npm run lint" in checks


def test_infrastructure_checks_run_first() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        infra = ["exit 0"]
        spec = ["echo spec_check"]
        result = run_qa(tmpdir, spec, infra)
        # infra check should appear before spec check in results
        assert result.checks[0].tool == infra[0]
        assert result.checks[1].tool == spec[0]


def test_run_qa_empty_checks() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        result = run_qa(tmpdir, [], [])
        assert result.passed is True
        assert result.checks == []
        assert "0/0" in result.summary or result.summary == ""


# --- Playwright detection tests ---


def test_detect_playwright_finds_ts_config(tmp_path: Path) -> None:
    (tmp_path / "playwright.config.ts").write_text("export default {}", encoding="utf-8")
    result = _detect_playwright(tmp_path)
    assert result == ["npx playwright test --reporter=list"]


def test_detect_playwright_finds_js_config(tmp_path: Path) -> None:
    (tmp_path / "playwright.config.js").write_text("module.exports = {}", encoding="utf-8")
    result = _detect_playwright(tmp_path)
    assert result == ["npx playwright test --reporter=list"]


def test_detect_playwright_finds_mjs_config(tmp_path: Path) -> None:
    (tmp_path / "playwright.config.mjs").write_text("export default {}", encoding="utf-8")
    result = _detect_playwright(tmp_path)
    assert result == ["npx playwright test --reporter=list"]


def test_detect_playwright_no_config(tmp_path: Path) -> None:
    result = _detect_playwright(tmp_path)
    assert result == []


def test_detect_infrastructure_checks_includes_playwright_when_enabled(tmp_path: Path) -> None:
    (tmp_path / "playwright.config.ts").write_text("export default {}", encoding="utf-8")
    checks = detect_infrastructure_checks(tmp_path, skip_playwright=False)
    assert "npx playwright test --reporter=list" in checks


def test_detect_infrastructure_checks_skips_playwright_by_default(tmp_path: Path) -> None:
    (tmp_path / "playwright.config.ts").write_text("export default {}", encoding="utf-8")
    checks = detect_infrastructure_checks(tmp_path)
    assert "npx playwright test --reporter=list" not in checks


def test_run_qa_playwright_check_type() -> None:
    """Playwright commands should be classified as 'e2e' check type."""
    with tempfile.TemporaryDirectory() as tmpdir:
        result = run_qa(tmpdir, [], ["echo playwright"])
        assert result.checks[0].type == "e2e"


# --- QA failure classification tests ---


def test_classify_failures_regression() -> None:
    before = "PASSED tests/test_foo.py::test_bar\n"
    after = "FAILED tests/test_foo.py::test_bar - AssertionError: expected 1\n"
    classifications = classify_failures(before, after)
    assert len(classifications) == 1
    assert classifications[0].category == "regression"
    assert "test_bar" in classifications[0].test_name


def test_classify_failures_pre_existing() -> None:
    before = "FAILED tests/test_foo.py::test_old - ValueError: bad\n"
    after = "FAILED tests/test_foo.py::test_old - ValueError: bad\n"
    classifications = classify_failures(before, after)
    assert len(classifications) == 1
    assert classifications[0].category == "pre_existing"


def test_classify_failures_new_test() -> None:
    before = "PASSED tests/test_foo.py::test_bar\n"
    after = "FAILED tests/test_new.py::test_brand_new - TypeError: oops\n"
    classifications = classify_failures(before, after)
    assert len(classifications) == 1
    assert classifications[0].category == "new_test_failure"
    assert "test_brand_new" in classifications[0].test_name


def test_classify_failures_mixed() -> None:
    before = (
        "FAILED tests/test_a.py::test_existing - Error: old\n"
        "PASSED tests/test_b.py::test_regression\n"
    )
    after = (
        "FAILED tests/test_a.py::test_existing - Error: old\n"
        "FAILED tests/test_b.py::test_regression - AssertionError: new bug\n"
        "FAILED tests/test_c.py::test_new_one - RuntimeError: boom\n"
    )
    classifications = classify_failures(before, after)
    categories = {c.test_name: c.category for c in classifications}
    assert categories["tests/test_a.py::test_existing"] == "pre_existing"
    assert categories["tests/test_b.py::test_regression"] == "regression"
    assert categories["tests/test_c.py::test_new_one"] == "new_test_failure"


def test_classify_failures_empty_before() -> None:
    before = ""
    after = "FAILED tests/test_foo.py::test_bar - Error: fail\n"
    classifications = classify_failures(before, after)
    assert len(classifications) == 1
    assert classifications[0].category == "new_test_failure"


def test_classify_failures_no_failures() -> None:
    before = "PASSED all tests\n"
    after = "PASSED all tests\n"
    classifications = classify_failures(before, after)
    assert classifications == []


def test_qa_failure_classification_dataclass() -> None:
    """Verify QAFailureClassification fields are accessible."""
    c = QAFailureClassification(category="regression", test_name="test_foo", error_summary="assertion failed")
    assert c.category == "regression"
    assert c.test_name == "test_foo"
    assert c.error_summary == "assertion failed"
