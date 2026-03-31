from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from golem.validator import _normalize_cmd, _subprocess_env


@dataclass
class QACheck:
    type: str
    tool: str
    passed: bool
    stdout: str
    stderr: str


@dataclass
class QAResult:
    passed: bool
    checks: list[QACheck] = field(default_factory=list)
    summary: str = ""


@dataclass
class QAFailureClassification:
    """Classifies a QA failure to help determine whether it is actionable."""

    category: str  # "regression" | "new_test_failure" | "pre_existing" | "flaky"
    test_name: str
    error_summary: str


def _detect_playwright(project_root: Path) -> list[str]:
    """Detect Playwright config and return test commands if found."""
    for ext in (".mjs", ".ts", ".js"):
        if (project_root / f"playwright.config{ext}").exists():
            return ["npx playwright test --reporter=list"]
    return []


_FAILED_TEST_PATTERNS: list[re.Pattern[str]] = [
    # pytest:  FAILED tests/test_foo.py::test_bar - AssertionError: ...
    re.compile(r"FAILED\s+([\w/\\.:]+)(?:\s*-\s*(.+))?"),
    # jest/playwright:  FAIL  src/foo.test.ts > test name
    re.compile(r"^[ \t]*FAIL\s+([\w/\\.:> ]+)", re.MULTILINE),
]


def _extract_failed_tests(output: str) -> list[tuple[str, str]]:
    """Extract (test_name, error_summary) pairs from combined stdout+stderr."""
    results: list[tuple[str, str]] = []
    seen: set[str] = set()
    for pattern in _FAILED_TEST_PATTERNS:
        for match in pattern.finditer(output):
            test_name = match.group(1).strip()
            error_summary = match.group(2).strip() if match.lastindex and match.lastindex >= 2 else ""
            if test_name not in seen:
                seen.add(test_name)
                results.append((test_name, error_summary))
    return results


def classify_failures(
    before_output: str,
    after_output: str,
) -> list[QAFailureClassification]:
    """Compare test output before and after changes to classify failures.

    - pre_existing: test failed both before and after
    - regression: test passed before but fails after
    - new_test_failure: test not present before but fails after (new test added)
    """
    before_failures = {name for name, _ in _extract_failed_tests(before_output)}
    after_failures = _extract_failed_tests(after_output)

    classifications: list[QAFailureClassification] = []
    for test_name, error_summary in after_failures:
        if test_name in before_failures:
            category = "pre_existing"
        else:
            # Check if this test existed in before_output at all
            if test_name in before_output:
                category = "regression"
            else:
                category = "new_test_failure"
        classifications.append(QAFailureClassification(
            category=category,
            test_name=test_name,
            error_summary=error_summary,
        ))
    return classifications


def detect_infrastructure_checks(project_root: Path, *, skip_playwright: bool = True) -> list[str]:
    """Detect available lint/type-check tools from project files."""
    checks: list[str] = []

    pyproject = project_root / "pyproject.toml"
    if pyproject.exists():
        content = pyproject.read_text(encoding="utf-8")
        if "[tool.ruff]" in content or "[tool.ruff." in content:
            checks.append("ruff check .")

    package_json = project_root / "package.json"
    if package_json.exists():
        try:
            data = json.loads(package_json.read_text(encoding="utf-8"))
            scripts = data.get("scripts", {})
            if "lint" in scripts:
                checks.append("npm run lint")
        except Exception:
            pass

    tsconfig = project_root / "tsconfig.json"
    if tsconfig.exists():
        checks.append("npx tsc --noEmit")

    if not skip_playwright:
        checks.extend(_detect_playwright(project_root))

    return checks


def run_qa(worktree_path: str, checks: list[str], infrastructure_checks: list[str]) -> QAResult:
    """Run infrastructure checks first, then spec checks. Returns structured QAResult."""
    env = _subprocess_env()
    all_checks: list[QACheck] = []
    failed_tools: list[str] = []

    for cmd in infrastructure_checks + checks:
        normalized = _normalize_cmd(cmd)
        result = subprocess.run(
            normalized,
            shell=True,
            cwd=worktree_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
        )
        passed = result.returncode == 0
        # Determine check type from command
        if "ruff" in cmd or "lint" in cmd or "eslint" in cmd:
            check_type = "lint"
        elif "tsc" in cmd or "mypy" in cmd or "pyright" in cmd:
            check_type = "lint"
        elif "pytest" in cmd or "jest" in cmd or "npm test" in cmd:
            check_type = "test"
        elif "playwright" in cmd:
            check_type = "e2e"
        else:
            check_type = "acceptance"

        all_checks.append(QACheck(
            type=check_type,
            tool=cmd,
            passed=passed,
            stdout=result.stdout,
            stderr=result.stderr,
        ))
        if not passed:
            failed_tools.append(cmd)

    total = len(all_checks)
    passed_count = sum(1 for c in all_checks if c.passed)
    if failed_tools:
        summary = f"{passed_count}/{total} checks passed. Failed: {failed_tools}"
    else:
        summary = f"{passed_count}/{total} checks passed."

    return QAResult(
        passed=len(failed_tools) == 0,
        checks=all_checks,
        summary=summary,
    )


def run_autofix(worktree_path: str, infrastructure_checks: list[str]) -> None:
    """Run autofix tools (ruff, prettier) before counting a retry."""
    env = _subprocess_env()
    all_checks = infrastructure_checks

    if any("ruff" in c for c in all_checks):
        subprocess.run(
            "ruff check --fix .",
            shell=True,
            cwd=worktree_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
        )
        subprocess.run(
            "ruff format .",
            shell=True,
            cwd=worktree_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
        )

    if any("prettier" in c for c in all_checks):
        subprocess.run(
            "npx prettier --write .",
            shell=True,
            cwd=worktree_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
        )
