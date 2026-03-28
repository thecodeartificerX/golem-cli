"""Multi-pass PR review engine.

Runs 6 specialized AI review passes against a GitHub PR, deduplicates findings,
and optionally posts comments back to the PR.

Usage:
    from golem.pr_review import run_review
    report = await run_review(pr_number=42, repo="owner/repo", config=config)
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, query

if TYPE_CHECKING:
    from golem.config import GolemConfig

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

_VALID_SEVERITIES = frozenset({"critical", "warning", "info"})
_VALID_CATEGORIES = frozenset({"security", "quality", "logic", "structural", "noise"})
_ALL_PASS_NAMES = ("quick_scan", "security", "quality", "deep", "structural", "triage")

_SEVERITY_RANK: dict[str, int] = {"critical": 2, "warning": 1, "info": 0}


@dataclass
class ReviewFinding:
    file: str
    line: int
    title: str
    body: str
    severity: str   # critical | warning | info
    category: str   # security | quality | logic | structural | noise
    pass_name: str


@dataclass
class ReviewReport:
    findings: list[ReviewFinding] = field(default_factory=list)
    summary: str = ""
    complexity: str = "standard"   # trivial | standard | complex
    passes_run: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    cost_usd: float = 0.0


# ---------------------------------------------------------------------------
# GitHub helpers (subprocess-based, fast)
# ---------------------------------------------------------------------------


def _run_gh(args: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    """Run a gh CLI command and return the CompletedProcess result."""
    return subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
    )


def _fetch_pr_diff(pr_number: int, repo: str) -> str:
    """Fetch the unified diff for a PR via `gh pr diff`."""
    result = _run_gh(["pr", "diff", str(pr_number), "-R", repo])
    if result.returncode != 0:
        raise RuntimeError(
            f"gh pr diff failed for PR #{pr_number} in {repo}: {result.stderr.strip()}"
        )
    return result.stdout


def _fetch_pr_info(pr_number: int, repo: str) -> dict[str, object]:
    """Fetch PR metadata (title, body, files, comments) via `gh pr view`."""
    result = _run_gh([
        "pr", "view", str(pr_number), "-R", repo,
        "--json", "title,body,files,comments,number,state",
    ])
    if result.returncode != 0:
        raise RuntimeError(
            f"gh pr view failed for PR #{pr_number} in {repo}: {result.stderr.strip()}"
        )
    try:
        return json.loads(result.stdout)  # type: ignore[return-value]
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Failed to parse gh pr view JSON: {exc}") from exc


def _fetch_pr_comments(pr_number: int, repo: str) -> list[dict[str, object]]:
    """Fetch inline review comments for a PR via `gh api`."""
    result = _run_gh([
        "api", f"repos/{repo}/pulls/{pr_number}/comments",
        "--paginate",
    ])
    if result.returncode != 0:
        # Non-fatal — return empty list if comments can't be fetched
        print(
            f"[REVIEW] Warning: could not fetch PR comments: {result.stderr.strip()}",
            file=sys.stderr,
        )
        return []
    try:
        data = json.loads(result.stdout)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def _post_review_comments(
    findings: list[ReviewFinding],
    pr_number: int,
    repo: str,
) -> int:
    """Post review findings as PR comments via `gh api`.

    Returns the number of comments successfully posted.
    Only posts findings with severity critical or warning to reduce noise.
    """
    postable = [f for f in findings if f.severity in ("critical", "warning")]
    posted = 0

    # First get the latest commit SHA for the PR
    result = _run_gh(["pr", "view", str(pr_number), "-R", repo, "--json", "headRefOid"])
    if result.returncode != 0:
        print(
            f"[REVIEW] Warning: could not get head SHA for PR #{pr_number}: {result.stderr.strip()}",
            file=sys.stderr,
        )
        return 0

    try:
        head_sha = json.loads(result.stdout).get("headRefOid", "")
    except json.JSONDecodeError:
        return 0

    if not head_sha:
        return 0

    for finding in postable:
        body = f"**[{finding.severity.upper()}] {finding.title}**\n\n{finding.body}\n\n*Pass: {finding.pass_name} | Category: {finding.category}*"

        payload: dict[str, object] = {
            "body": body,
            "commit_id": head_sha,
            "path": finding.file,
            "line": finding.line if finding.line > 0 else 1,
            "side": "RIGHT",
        }

        post_result = _run_gh([
            "api", f"repos/{repo}/pulls/{pr_number}/comments",
            "--method", "POST",
            "--input", "-",
        ])
        # gh api --input reads from stdin; use a separate approach via --field
        post_result = subprocess.run(
            [
                "gh", "api", f"repos/{repo}/pulls/{pr_number}/comments",
                "--method", "POST",
                f"--field=body={body}",
                f"--field=commit_id={head_sha}",
                f"--field=path={finding.file}",
                f"--field=line={finding.line if finding.line > 0 else 1}",
                "--field=side=RIGHT",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        if post_result.returncode == 0:
            posted += 1
        else:
            print(
                f"[REVIEW] Warning: failed to post comment for {finding.file}:{finding.line}: "
                f"{post_result.stderr.strip()[:200]}",
                file=sys.stderr,
            )

    return posted


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _parse_findings(raw: str, pass_name: str) -> list[ReviewFinding]:
    """Parse a JSON array of finding dicts from a model response.

    Tolerates model preamble/postamble around the JSON array.
    Returns an empty list on any parse failure.
    """
    start = raw.find("[")
    end = raw.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []

    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return []

    if not isinstance(data, list):
        return []

    findings: list[ReviewFinding] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        file_ = str(item.get("file", "")).strip()
        line_raw = item.get("line", 0)
        try:
            line = int(line_raw)
        except (TypeError, ValueError):
            line = 0
        title = str(item.get("title", "")).strip()[:200]
        body = str(item.get("body", "")).strip()
        severity = str(item.get("severity", "info")).strip().lower()
        category = str(item.get("category", "quality")).strip().lower()

        if severity not in _VALID_SEVERITIES:
            severity = "info"
        if category not in _VALID_CATEGORIES:
            category = "quality"
        if not title:
            continue  # skip malformed entries

        findings.append(ReviewFinding(
            file=file_,
            line=line,
            title=title,
            body=body,
            severity=severity,
            category=category,
            pass_name=pass_name,
        ))

    return findings


def _parse_quick_scan(raw: str) -> dict[str, object]:
    """Parse the quick_scan JSON object from a model response."""
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {"complexity": "standard", "reasoning": "parse error"}
    try:
        data = json.loads(raw[start : end + 1])
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    return {"complexity": "standard", "reasoning": "parse error"}


# ---------------------------------------------------------------------------
# Individual review passes
# ---------------------------------------------------------------------------


async def _run_pass(
    pass_name: str,
    system_prompt: str,
    user_message: str,
    model: str,
) -> tuple[str, list[ReviewFinding], float]:
    """Run a single review pass and return (raw_response, findings, cost_usd)."""
    from golem.config import sdk_env

    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        model=model,
        max_turns=3,
        permission_mode="bypassPermissions",
        env=sdk_env(),
    )

    raw_response = ""
    cost_usd = 0.0

    try:
        async for message in query(prompt=user_message, options=options):
            if isinstance(message, ResultMessage):
                if message.result:
                    raw_response = message.result
                usage = message.usage or {}
                input_tokens = int(usage.get("input_tokens", 0))
                output_tokens = int(usage.get("output_tokens", 0))
                # Rough cost estimate (Sonnet pricing)
                cost_usd += (input_tokens * 3.0 + output_tokens * 15.0) / 1_000_000
            elif isinstance(message, AssistantMessage):
                for block in message.content:
                    if hasattr(block, "text"):
                        raw_response += block.text
    except Exception as exc:
        print(
            f"[REVIEW] Warning: pass '{pass_name}' failed: {exc}",
            file=sys.stderr,
        )
        return "", [], 0.0

    findings = _parse_findings(raw_response, pass_name)
    return raw_response, findings, cost_usd


async def _run_quick_scan(
    diff_text: str,
    pr_info: dict[str, object],
    model: str,
) -> tuple[dict[str, object], float]:
    """Run the quick_scan pass. Returns (scan_result_dict, cost_usd)."""
    from golem.prompts.review_passes import QUICK_SCAN_PROMPT
    from golem.config import sdk_env

    title = str(pr_info.get("title", ""))
    body = str(pr_info.get("body", ""))
    diff_preview = diff_text[:8000] if len(diff_text) > 8000 else diff_text

    user_message = (
        f"PR #{pr_info.get('number', '?')}: {title}\n\n"
        f"Description:\n{body[:500] if body else '(no description)'}\n\n"
        f"## Diff (first 8000 chars)\n\n{diff_preview}"
    )

    options = ClaudeAgentOptions(
        system_prompt=QUICK_SCAN_PROMPT,
        model=model,
        max_turns=3,
        permission_mode="bypassPermissions",
        env=sdk_env(),
    )

    raw_response = ""
    cost_usd = 0.0

    try:
        async for message in query(prompt=user_message, options=options):
            if isinstance(message, ResultMessage):
                if message.result:
                    raw_response = message.result
                usage = message.usage or {}
                input_tokens = int(usage.get("input_tokens", 0))
                output_tokens = int(usage.get("output_tokens", 0))
                cost_usd += (input_tokens * 3.0 + output_tokens * 15.0) / 1_000_000
            elif isinstance(message, AssistantMessage):
                for block in message.content:
                    if hasattr(block, "text"):
                        raw_response += block.text
    except Exception as exc:
        print(f"[REVIEW] Warning: quick_scan failed: {exc}", file=sys.stderr)
        return {"complexity": "standard", "reasoning": "quick_scan failed"}, 0.0

    result = _parse_quick_scan(raw_response)
    return result, cost_usd


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


def _deduplicate_findings(findings: list[ReviewFinding]) -> list[ReviewFinding]:
    """Deduplicate findings by (file, line, title) key, keeping the highest severity."""
    seen: dict[tuple[str, int, str], ReviewFinding] = {}

    for finding in findings:
        key = (finding.file, finding.line, finding.title.lower())
        if key not in seen:
            seen[key] = finding
        else:
            existing = seen[key]
            if _SEVERITY_RANK.get(finding.severity, 0) > _SEVERITY_RANK.get(existing.severity, 0):
                seen[key] = finding

    return list(seen.values())


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------

_MAX_DIFF_CHARS = 80_000


async def run_review(
    pr_number: int,
    repo: str,
    config: GolemConfig,
    passes: list[str] | None = None,
) -> ReviewReport:
    """Run the full multi-pass review pipeline against a GitHub PR.

    Args:
        pr_number: GitHub PR number.
        repo: Repository in OWNER/REPO format.
        config: GolemConfig instance (used for model selection).
        passes: Optional list of pass names to run. Defaults to all passes.

    Returns:
        ReviewReport with deduplicated findings, summary, and metadata.
    """
    from golem.prompts.review_passes import (
        SECURITY_PASS_PROMPT,
        QUALITY_PASS_PROMPT,
        DEEP_ANALYSIS_PROMPT,
        STRUCTURAL_PASS_PROMPT,
        AI_TRIAGE_PROMPT,
    )

    if passes is None:
        passes = list(_ALL_PASS_NAMES)

    t0 = time.monotonic()
    model = config.validator_model  # Use Sonnet for review passes

    # --- Fetch PR data ---
    pr_info = _fetch_pr_info(pr_number, repo)
    diff_text = _fetch_pr_diff(pr_number, repo)
    existing_comments = _fetch_pr_comments(pr_number, repo)

    # Truncate diff if too large
    if len(diff_text) > _MAX_DIFF_CHARS:
        diff_text = diff_text[:_MAX_DIFF_CHARS] + "\n\n[... diff truncated ...]"

    title = str(pr_info.get("title", f"PR #{pr_number}"))
    pr_body = str(pr_info.get("body", ""))

    # Build shared diff context header
    diff_header = (
        f"PR #{pr_number}: {title}\n\n"
        f"Description:\n{pr_body[:1000] if pr_body else '(no description)'}\n\n"
        f"## Diff\n\n{diff_text}"
    )

    # --- Pass 1: quick_scan (always runs first) ---
    complexity = "standard"
    total_cost = 0.0
    passes_run: list[str] = []

    if "quick_scan" in passes:
        scan_result, scan_cost = await _run_quick_scan(diff_text, pr_info, model)
        total_cost += scan_cost
        passes_run.append("quick_scan")
        complexity = str(scan_result.get("complexity", "standard")).lower()
        if complexity not in ("trivial", "standard", "complex"):
            complexity = "standard"
        print(
            f"[REVIEW] quick_scan: complexity={complexity} "
            f"({scan_result.get('reasoning', '')})",
            file=sys.stderr,
        )

    # --- Passes 2-6: run in parallel ---
    parallel_tasks: list[tuple[str, str, str]] = []  # (pass_name, system_prompt, user_message)

    if "security" in passes:
        parallel_tasks.append(("security", SECURITY_PASS_PROMPT, diff_header))

    if "quality" in passes:
        parallel_tasks.append(("quality", QUALITY_PASS_PROMPT, diff_header))

    if "deep" in passes and complexity != "trivial":
        parallel_tasks.append(("deep", DEEP_ANALYSIS_PROMPT, diff_header))
    elif "deep" in passes and complexity == "trivial":
        print("[REVIEW] deep_analysis: skipped (trivial complexity)", file=sys.stderr)

    if "structural" in passes:
        parallel_tasks.append(("structural", STRUCTURAL_PASS_PROMPT, diff_header))

    if "triage" in passes and existing_comments:
        # Build triage message with existing comments
        comments_text = "\n\n".join(
            f"- File: {c.get('path', '?')} Line: {c.get('original_line', c.get('line', '?'))}\n"
            f"  Body: {str(c.get('body', ''))[:300]}"
            for c in existing_comments[:20]  # limit to 20 comments
        )
        triage_message = (
            f"## Existing Review Comments\n\n{comments_text}\n\n"
            f"## Diff\n\n{diff_text[:20000]}"
        )
        parallel_tasks.append(("triage", AI_TRIAGE_PROMPT, triage_message))
    elif "triage" in passes:
        print("[REVIEW] ai_triage: skipped (no existing AI comments)", file=sys.stderr)

    # Run all parallel passes concurrently
    all_findings: list[ReviewFinding] = []

    if parallel_tasks:
        coroutines = [
            _run_pass(name, system_prompt, user_msg, model)
            for name, system_prompt, user_msg in parallel_tasks
        ]
        results = await asyncio.gather(*coroutines, return_exceptions=True)

        for i, result in enumerate(results):
            pass_name = parallel_tasks[i][0]
            if isinstance(result, Exception):
                print(
                    f"[REVIEW] pass '{pass_name}' raised exception: {result}",
                    file=sys.stderr,
                )
                continue
            _raw, findings, pass_cost = result
            total_cost += pass_cost
            passes_run.append(pass_name)
            all_findings.extend(findings)
            print(
                f"[REVIEW] {pass_name}: {len(findings)} finding(s), cost=${pass_cost:.4f}",
                file=sys.stderr,
            )

    # --- Deduplicate ---
    deduplicated = _deduplicate_findings(all_findings)

    # --- Build summary ---
    critical_count = sum(1 for f in deduplicated if f.severity == "critical")
    warning_count = sum(1 for f in deduplicated if f.severity == "warning")
    info_count = sum(1 for f in deduplicated if f.severity == "info")
    summary = (
        f"PR #{pr_number} reviewed with {len(passes_run)} passes. "
        f"Found {len(deduplicated)} finding(s): "
        f"{critical_count} critical, {warning_count} warning, {info_count} info."
    )

    duration_s = time.monotonic() - t0

    return ReviewReport(
        findings=deduplicated,
        summary=summary,
        complexity=complexity,
        passes_run=passes_run,
        duration_s=duration_s,
        cost_usd=total_cost,
    )
