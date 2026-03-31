from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ResultMessage,
    AssistantMessage,
    TextBlock,
    query,
)

from golem.config import GolemConfig, sdk_env

_REVIEWER_PROMPT_TEMPLATE = Path(__file__).parent / "prompts" / "reviewer.md"


@dataclass
class ReviewVerdict:
    decision: str  # "approve" | "warning" | "block"
    critical_issues: list[str] = field(default_factory=list)
    important_issues: list[str] = field(default_factory=list)
    minor_issues: list[str] = field(default_factory=list)
    summary: str = ""


def _build_reviewer_prompt(
    diff_text: str,
    ticket_title: str,
    plan_section: str,
    acceptance_criteria: list[str],
    claude_md: str,
) -> str:
    """Build the reviewer prompt from the template and substitution values."""
    template = _REVIEWER_PROMPT_TEMPLATE.read_text(encoding="utf-8")
    acceptance_str = "\n".join(f"- {a}" for a in acceptance_criteria) if acceptance_criteria else "None specified"
    replacements = {
        "diff_text": diff_text,
        "ticket_title": ticket_title,
        "plan_section": plan_section,
        "acceptance_criteria": acceptance_str,
        "claude_md": claude_md if claude_md else "No project conventions file found.",
    }
    prompt = template
    for key, value in replacements.items():
        placeholder = "{" + key + "}"
        prompt = prompt.replace(placeholder, value)
    return prompt


def _parse_verdict(text: str) -> ReviewVerdict:
    """Parse structured reviewer output into a ReviewVerdict."""
    decision = "approve"
    critical: list[str] = []
    important: list[str] = []
    minor: list[str] = []
    summary = ""

    lines = text.strip().splitlines()
    current_section: str | None = None

    for line in lines:
        stripped = line.strip()
        upper = stripped.upper()

        if upper.startswith("DECISION:"):
            raw = stripped[len("DECISION:"):].strip().lower()
            if "block" in raw:
                decision = "block"
            elif "warning" in raw:
                decision = "warning"
            else:
                decision = "approve"
            current_section = None
            continue

        if upper.startswith("CRITICAL:"):
            current_section = "critical"
            # Check for inline content after the header
            rest = stripped[len("CRITICAL:"):].strip()
            if rest and rest != "-" and rest.lower() != "none":
                critical.append(rest.lstrip("- "))
            continue

        if upper.startswith("IMPORTANT:"):
            current_section = "important"
            rest = stripped[len("IMPORTANT:"):].strip()
            if rest and rest != "-" and rest.lower() != "none":
                important.append(rest.lstrip("- "))
            continue

        if upper.startswith("MINOR:"):
            current_section = "minor"
            rest = stripped[len("MINOR:"):].strip()
            if rest and rest != "-" and rest.lower() != "none":
                minor.append(rest.lstrip("- "))
            continue

        if upper.startswith("SUMMARY:"):
            summary = stripped[len("SUMMARY:"):].strip()
            current_section = None
            continue

        # Collect bullet items under current section
        if current_section and stripped.startswith("- "):
            item = stripped[2:].strip()
            if item.lower() in ("none", "none.", "(none)", "n/a"):
                continue
            if current_section == "critical":
                critical.append(item)
            elif current_section == "important":
                important.append(item)
            elif current_section == "minor":
                minor.append(item)

    return ReviewVerdict(
        decision=decision,
        critical_issues=critical,
        important_issues=important,
        minor_issues=minor,
        summary=summary,
    )


async def run_reviewer(
    worktree_path: Path,
    project_root: Path,
    ticket_title: str,
    plan_section: str,
    acceptance_criteria: list[str],
    config: GolemConfig,
    base_sha: str = "HEAD~1",
) -> ReviewVerdict:
    """Spawn a Reviewer sub-agent to review code changes before QA.

    A fresh sub-agent receives only the diff + requirements (no session history)
    to eliminate author-bias. Returns a structured ReviewVerdict.
    """
    # Get the diff
    diff_result = subprocess.run(
        ["git", "diff", base_sha, "HEAD"],
        cwd=str(worktree_path),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    diff_text = diff_result.stdout[:30_000]

    if not diff_text.strip():
        return ReviewVerdict(decision="approve", summary="No changes to review.")

    # Read project conventions from CLAUDE.md
    claude_md = ""
    claude_md_path = project_root / "CLAUDE.md"
    if claude_md_path.exists():
        claude_md = claude_md_path.read_text(encoding="utf-8")[:5_000]

    prompt = _build_reviewer_prompt(
        diff_text=diff_text,
        ticket_title=ticket_title,
        plan_section=plan_section,
        acceptance_criteria=acceptance_criteria,
        claude_md=claude_md,
    )

    result_text = ""
    async for message in query(
        prompt=prompt,
        options=ClaudeAgentOptions(
            model=config.reviewer_model,
            max_turns=1,
            max_budget_usd=config.reviewer_budget_usd,
            permission_mode="plan",
            env=sdk_env(),
        ),
    ):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    result_text = block.text
                    preview = block.text[:120].replace("\n", " ")
                    print(f"[REVIEWER] {preview}", file=sys.stderr)
        elif isinstance(message, ResultMessage):
            if message.result:
                result_text = message.result

    return _parse_verdict(result_text)
