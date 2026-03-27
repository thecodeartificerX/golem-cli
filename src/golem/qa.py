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
    cannot_validate: bool = False  # True when check failed due to environment, not code


@dataclass
class QAResult:
    passed: bool
    checks: list[QACheck] = field(default_factory=list)
    summary: str = ""
    cannot_validate: bool = False  # True when any check has cannot_validate=True
    stage: str = "complete"        # "infrastructure_failed" | "complete" | "crashed"


def detect_infrastructure_checks(project_root: Path) -> list[str]:
    """Detect available lint/type-check tools from project files."""
    checks: list[str] = []

    pyproject = project_root / "pyproject.toml"
    if pyproject.exists():
        content = pyproject.read_text(encoding="utf-8")
        if "[tool.ruff]" in content or "[tool.ruff." in content:
            checks.append("ruff check .")
        if "[tool.mypy]" in content or "[mypy]" in content:
            checks.append("mypy .")

    package_json = project_root / "package.json"
    if package_json.exists():
        try:
            data = json.loads(package_json.read_text(encoding="utf-8"))
            scripts = data.get("scripts", {})
            if "lint" in scripts:
                checks.append("npm run lint")
            if "test" in scripts:
                checks.append("npm test")
        except Exception:
            pass

    tsconfig = project_root / "tsconfig.json"
    if tsconfig.exists():
        checks.append("npx tsc --noEmit")

    cargo_toml = project_root / "Cargo.toml"
    if cargo_toml.exists():
        checks.append("cargo test")

    return checks


def _classify_check(cmd: str) -> str:
    if "ruff" in cmd or "lint" in cmd or "eslint" in cmd:
        return "lint"
    if "tsc" in cmd or "mypy" in cmd or "pyright" in cmd:
        return "lint"
    if "pytest" in cmd or "jest" in cmd or "npm test" in cmd:
        return "test"
    return "acceptance"


def _run_single_check(cmd: str, worktree_path: str) -> QACheck:
    check_type = _classify_check(cmd)
    try:
        result = subprocess.run(
            _normalize_cmd(cmd), shell=True, capture_output=True, text=True,
            encoding="utf-8", timeout=120, cwd=worktree_path, env=_subprocess_env(),
        )
        return QACheck(
            type=check_type, tool=cmd,
            passed=result.returncode == 0,
            stdout=result.stdout, stderr=result.stderr,
        )
    except subprocess.TimeoutExpired:
        return QACheck(
            type=check_type, tool=cmd, passed=False,
            stdout="", stderr="Command timed out after 120 seconds",
        )
    except (FileNotFoundError, PermissionError, OSError) as e:
        return QACheck(
            type=check_type, tool=cmd, passed=False,
            stdout="", stderr=f"Environment error: {e}",
            cannot_validate=True,
        )
    except Exception as e:
        return QACheck(
            type=check_type, tool=cmd, passed=False,
            stdout="", stderr=f"Unexpected error: {e}",
            cannot_validate=True,
        )


def _build_result(checks: list[QACheck], stage: str) -> QAResult:
    passed_count = sum(1 for c in checks if c.passed)
    failed = [c.tool for c in checks if not c.passed and not c.cannot_validate]
    env_failed = [c.tool for c in checks if c.cannot_validate]
    has_cannot_validate = any(c.cannot_validate for c in checks)

    parts = [f"{passed_count}/{len(checks)} checks passed"]
    if failed:
        parts.append(f"Failed: {failed}")
    if env_failed:
        parts.append(f"Environment errors: {env_failed}")
    if stage == "infrastructure_failed":
        parts.append("Spec checks skipped (infrastructure failed)")

    return QAResult(
        passed=len(failed) == 0 and not has_cannot_validate,
        checks=checks, summary=". ".join(parts),
        cannot_validate=has_cannot_validate, stage=stage,
    )


def run_qa(worktree_path: str, checks: list[str], infrastructure_checks: list[str] | None = None) -> QAResult:
    """Run infrastructure checks first (fast gate), then spec checks. Returns structured QAResult."""
    all_checks: list[QACheck] = []
    infra = infrastructure_checks or []

    # Phase 1: Infrastructure checks (fast gate)
    for cmd in infra:
        check = _run_single_check(cmd, worktree_path)
        all_checks.append(check)

    infra_failed = any(not c.passed for c in all_checks)
    if infra_failed:
        return _build_result(all_checks, stage="infrastructure_failed")

    # Phase 2: Spec checks (only if infra passed)
    for cmd in checks:
        check = _run_single_check(cmd, worktree_path)
        all_checks.append(check)

    return _build_result(all_checks, stage="complete")


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
