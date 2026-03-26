from __future__ import annotations

import json
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


def detect_infrastructure_checks(project_root: Path) -> list[str]:
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

    return checks


def run_qa(worktree_path: str, checks: list[str], infrastructure_checks: list[str]) -> QAResult:
    """Run infrastructure checks first, then spec checks. Returns structured QAResult."""
    env = _subprocess_env()
    all_checks: list[QACheck] = []
    failed_tools: list[str] = []

    for cmd in infrastructure_checks + checks:
        normalized = _normalize_cmd(cmd)
        try:
            result = subprocess.run(
                normalized,
                shell=True,
                cwd=worktree_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=env,
                timeout=120,
            )
            passed = result.returncode == 0
        except subprocess.TimeoutExpired:
            passed = False
            result = type("R", (), {"returncode": -1, "stdout": "", "stderr": f"Timed out after 120s: {cmd}"})()  # type: ignore[assignment]
        # Determine check type from command
        if "ruff" in cmd or "lint" in cmd or "eslint" in cmd:
            check_type = "lint"
        elif "tsc" in cmd or "mypy" in cmd or "pyright" in cmd:
            check_type = "lint"
        elif "pytest" in cmd or "jest" in cmd or "npm test" in cmd:
            check_type = "test"
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
    """Run autofix tools (ruff, prettier) before counting a QA retry.

    Scans infrastructure_checks for known tool names and runs their fix commands:
    - ruff: runs `ruff check --fix .` then `ruff format .`
    - prettier: runs `npx prettier --write .`

    No-op if no matching tools are found in the checks list.
    """
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
