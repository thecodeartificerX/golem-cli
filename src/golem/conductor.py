"""Spec complexity classification for adaptive pipeline scaling."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from golem.config import GolemConfig


@dataclass
class ClassificationResult:
    complexity: str  # TRIVIAL | SIMPLE | STANDARD | CRITICAL
    reasoning: str
    confidence: float  # 0.0-1.0


# Keyword indicators (case-insensitive)
_TRIVIAL_KEYWORDS = {"typo", "cosmetic", "readme", "comment", "docstring", "version bump", "changelog"}
_SIMPLE_KEYWORDS = {"config", "env", "rename", "move", "delete file", "add field", "update dependency"}
_CRITICAL_KEYWORDS = {
    "auth", "authentication", "authorization", "security", "migration",
    "database schema", "payment", "billing", "encryption", "credentials",
    "production", "deploy", "infrastructure",
}

# File count patterns
_FILE_MENTION_PATTERN = re.compile(r"(?:modify|create|edit|update|change|add to|delete)\s+[`'\"]?[\w/.-]+\.[a-z]+", re.I)


def classify_spec(spec_text: str, project_context: str = "") -> ClassificationResult:
    """Classify spec complexity using heuristics.

    Returns a ClassificationResult with the complexity level, reasoning, and confidence.
    """
    text = (spec_text + " " + project_context).lower()
    spec_length = len(spec_text)

    # Count file mentions
    file_mentions = len(_FILE_MENTION_PATTERN.findall(spec_text))

    # Score keywords
    trivial_hits = sum(1 for kw in _TRIVIAL_KEYWORDS if kw in text)
    simple_hits = sum(1 for kw in _SIMPLE_KEYWORDS if kw in text)
    critical_hits = sum(1 for kw in _CRITICAL_KEYWORDS if kw in text)

    # Decision logic
    reasons = []

    if critical_hits >= 2:
        reasons.append(f"{critical_hits} critical keywords detected")
        return ClassificationResult("CRITICAL", "; ".join(reasons), 0.8)

    if file_mentions <= 2 and spec_length < 500 and trivial_hits > 0:
        reasons.append(f"{file_mentions} files, {spec_length} chars, trivial keywords")
        return ClassificationResult("TRIVIAL", "; ".join(reasons), 0.7)

    if file_mentions <= 3 and spec_length < 1500:
        reasons.append(f"{file_mentions} files, {spec_length} chars")
        if simple_hits > 0:
            reasons.append(f"{simple_hits} simple keywords")
        return ClassificationResult("SIMPLE", "; ".join(reasons), 0.6)

    if file_mentions > 10 or spec_length > 5000 or critical_hits >= 1:
        reasons.append(f"{file_mentions} files, {spec_length} chars, {critical_hits} critical keywords")
        return ClassificationResult("CRITICAL", "; ".join(reasons), 0.5)

    reasons.append(f"{file_mentions} files, {spec_length} chars (default)")
    return ClassificationResult("STANDARD", "; ".join(reasons), 0.5)


def derive_agent_topology(config: GolemConfig) -> dict[str, object]:
    """Derive the agent tree that would spawn for the given config."""
    return {
        "planner": {
            "model": config.planner_model,
            "max_turns": config.planner_max_turns,
            "mcp_server": "golem",
            "mcp_tools": ["create_ticket", "update_ticket", "read_ticket", "list_tickets",
                           "run_qa", "create_worktree", "merge_branches", "commit_worktree"],
            "stall_warn": int(config.planner_max_turns * 0.6),
            "stall_kill": int(config.planner_max_turns * 0.8),
            "sub_agents": [
                {"role": "explorer", "model": "claude-haiku-4-5"},
                {"role": "researcher", "model": "claude-sonnet-4-6"},
            ],
        },
        "tech_lead": {
            "model": config.tech_lead_model,
            "max_turns": config.max_tech_lead_turns,
            "mcp_server": "golem",
            "mcp_tools": ["create_ticket", "update_ticket", "read_ticket", "list_tickets",
                           "run_qa", "create_worktree", "merge_branches", "commit_worktree"],
            "stall_warn": int(config.max_tech_lead_turns * 0.3),
            "stall_kill": int(config.max_tech_lead_turns * 0.5),
        },
        "junior_dev": {
            "model": config.worker_model,
            "max_turns": config.max_worker_turns,
            "mcp_server": "golem-junior-dev",
            "mcp_tools": ["run_qa", "update_ticket", "read_ticket"],
            "stall_warn": int(config.max_worker_turns * 0.3),
            "stall_kill": int(config.max_worker_turns * 0.5),
            "dispatch_jitter_max": config.dispatch_jitter_max,
        },
        "skip_tech_lead": config.skip_tech_lead,
    }


def predict_conflicts(spec_paths: list[Path]) -> list[dict[str, object]]:
    """Parse specs for file references and predict cross-spec conflicts."""
    if len(spec_paths) < 2:
        return []

    # Collect file references per spec
    spec_files: dict[str, set[str]] = {}
    for spec_path in spec_paths:
        name = spec_path.stem
        content = spec_path.read_text(encoding="utf-8")
        files: set[str] = set()
        # Match backtick-quoted paths and src/golem/*.py or tests/test_*.py patterns
        for match in re.finditer(r'`([^`]*\.(py|ts|js|md|html))`', content):
            files.add(match.group(1))
        for match in re.finditer(r'(?:src/golem/|tests/test_)\S+\.py', content):
            files.add(match.group(0))
        spec_files[name] = files

    # Find overlaps
    conflicts: list[dict[str, object]] = []
    all_files: set[str] = set()
    for files in spec_files.values():
        all_files |= files

    for file in sorted(all_files):
        specs_touching = [name for name, files in spec_files.items() if file in files]
        if len(specs_touching) > 1:
            conflicts.append({"file": file, "specs": specs_touching})

    return conflicts
