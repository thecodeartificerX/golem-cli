"""Spec complexity classification for adaptive pipeline scaling."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


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
