"""AI-powered codebase ideation runner.

Runs targeted Claude analysis passes against a codebase to surface actionable
improvement ideas across 6 categories:

  code_improvements       - dead code, simplification, better abstractions
  ui_ux_improvements      - CLI output, error messages, help text
  documentation_gaps      - missing docstrings, outdated comments, README gaps
  security_hardening      - input validation, hardcoded values, info leaks
  performance_optimizations - unnecessary I/O, blocking calls, cache opportunities
  code_quality            - inconsistent patterns, missing types, naming issues

Usage:
    from golem.ideation import run_ideation, IdeaCategory
    result = await run_ideation("code_improvements", project_root, config)
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, query

if TYPE_CHECKING:
    from golem.config import GolemConfig

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

IdeaCategory = Literal[
    "code_improvements",
    "ui_ux_improvements",
    "documentation_gaps",
    "security_hardening",
    "performance_optimizations",
    "code_quality",
]

ALL_CATEGORIES: tuple[IdeaCategory, ...] = (
    "code_improvements",
    "ui_ux_improvements",
    "documentation_gaps",
    "security_hardening",
    "performance_optimizations",
    "code_quality",
)

_VALID_PRIORITIES = frozenset({"high", "medium", "low"})
_VALID_EFFORTS = frozenset({"small", "medium", "large"})


@dataclass
class Idea:
    title: str
    description: str
    category: IdeaCategory
    file: str          # target file if applicable, empty string if N/A
    priority: str      # high | medium | low
    effort: str        # small | medium | large


@dataclass
class IdeationResult:
    category: IdeaCategory
    ideas: list[Idea] = field(default_factory=list)
    summary: str = ""
    duration_s: float = 0.0


# ---------------------------------------------------------------------------
# Category prompts
# ---------------------------------------------------------------------------

_PROMPTS: dict[str, str] = {
    "code_improvements": (
        "You are a senior engineer performing a codebase improvement audit.\n\n"
        "Analyze the provided code context and identify concrete improvement opportunities:\n"
        "- Dead code that can be removed (unused functions, imports, variables)\n"
        "- Simplification opportunities (loops that could be comprehensions, nested ifs that could be flattened)\n"
        "- Better abstractions (duplicated logic that should be extracted)\n"
        "- Redundant logic (computations repeated that could be cached or unified)\n"
        "- Overcomplicated implementations where simpler alternatives exist\n\n"
        "Return ONLY valid JSON — no markdown, no explanation.\n\n"
        "JSON shape:\n"
        "{\n"
        '  "ideas": [\n'
        "    {\n"
        '      "title": "short imperative title",\n'
        '      "description": "concrete actionable description of what to improve and why",\n'
        '      "file": "src/example.py or empty string if cross-cutting",\n'
        '      "priority": "high|medium|low",\n'
        '      "effort": "small|medium|large"\n'
        "    }\n"
        "  ],\n"
        '  "summary": "one-sentence summary of the biggest code improvement opportunity"\n'
        "}"
    ),
    "ui_ux_improvements": (
        "You are a CLI/UX specialist auditing developer tooling.\n\n"
        "Analyze the provided code context and identify UI/UX improvement opportunities:\n"
        "- CLI output clarity (confusing messages, missing context, inconsistent formatting)\n"
        "- Dashboard usability (missing affordances, hard-to-read layouts)\n"
        "- Error messages (too terse, missing remediation hints, missing context)\n"
        "- Help text (missing --help descriptions, unclear option names)\n"
        "- Progress feedback (missing spinners, no ETA, silent operations)\n"
        "- Output verbosity (too noisy, not enough detail at right log level)\n\n"
        "Return ONLY valid JSON — no markdown, no explanation.\n\n"
        "JSON shape:\n"
        "{\n"
        '  "ideas": [\n'
        "    {\n"
        '      "title": "short imperative title",\n'
        '      "description": "concrete actionable description of what to improve and why",\n'
        '      "file": "src/example.py or empty string if cross-cutting",\n'
        '      "priority": "high|medium|low",\n'
        '      "effort": "small|medium|large"\n'
        "    }\n"
        "  ],\n"
        '  "summary": "one-sentence summary of the biggest UX improvement opportunity"\n'
        "}"
    ),
    "documentation_gaps": (
        "You are a technical writer auditing a Python codebase for documentation quality.\n\n"
        "Analyze the provided code context and identify documentation gaps:\n"
        "- Missing module-level docstrings\n"
        "- Functions/classes missing docstrings, especially public APIs\n"
        "- Outdated comments that no longer reflect the code\n"
        "- Undocumented edge cases or failure modes\n"
        "- Missing type annotations on public functions\n"
        "- README gaps (features not mentioned, missing examples, stale instructions)\n"
        "- TODOs/FIXMEs that should be tracked as issues\n\n"
        "Return ONLY valid JSON — no markdown, no explanation.\n\n"
        "JSON shape:\n"
        "{\n"
        '  "ideas": [\n'
        "    {\n"
        '      "title": "short imperative title",\n'
        '      "description": "concrete actionable description of what to document and why",\n'
        '      "file": "src/example.py or empty string if cross-cutting",\n'
        '      "priority": "high|medium|low",\n'
        '      "effort": "small|medium|large"\n'
        "    }\n"
        "  ],\n"
        '  "summary": "one-sentence summary of the most critical documentation gap"\n'
        "}"
    ),
    "security_hardening": (
        "You are a security engineer auditing a Python codebase.\n\n"
        "Analyze the provided code context and identify security hardening opportunities:\n"
        "- Input validation gaps (missing length checks, unvalidated user input)\n"
        "- Hardcoded credentials or secrets in source code\n"
        "- Error info leaks (stack traces or internal details exposed to end users)\n"
        "- Path traversal risks (user-controlled paths used in file operations)\n"
        "- Command injection risks (user input in subprocess calls)\n"
        "- Dependency risks (pinned to vulnerable versions or unpinned)\n"
        "- Insecure defaults (permissive file permissions, no timeouts)\n\n"
        "Return ONLY valid JSON — no markdown, no explanation.\n\n"
        "JSON shape:\n"
        "{\n"
        '  "ideas": [\n'
        "    {\n"
        '      "title": "short imperative title",\n'
        '      "description": "concrete actionable description of the risk and how to fix it",\n'
        '      "file": "src/example.py or empty string if cross-cutting",\n'
        '      "priority": "high|medium|low",\n'
        '      "effort": "small|medium|large"\n'
        "    }\n"
        "  ],\n"
        '  "summary": "one-sentence summary of the most critical security concern"\n'
        "}"
    ),
    "performance_optimizations": (
        "You are a performance engineer auditing a Python async codebase.\n\n"
        "Analyze the provided code context and identify performance optimization opportunities:\n"
        "- Unnecessary I/O (reading files multiple times, redundant subprocess calls)\n"
        "- Blocking calls in async context (time.sleep instead of asyncio.sleep, sync file I/O)\n"
        "- Cache opportunities (repeated computations, repeated lookups)\n"
        "- Batch operation opportunities (N+1 patterns, sequential calls that could be parallel)\n"
        "- Large data loading (reading full files when only parts are needed)\n"
        "- Missing early returns (processing continues past definitive failure)\n"
        "- Expensive operations in hot paths (JSON serialization in tight loops)\n\n"
        "Return ONLY valid JSON — no markdown, no explanation.\n\n"
        "JSON shape:\n"
        "{\n"
        '  "ideas": [\n'
        "    {\n"
        '      "title": "short imperative title",\n'
        '      "description": "concrete actionable description of the bottleneck and how to optimize",\n'
        '      "file": "src/example.py or empty string if cross-cutting",\n'
        '      "priority": "high|medium|low",\n'
        '      "effort": "small|medium|large"\n'
        "    }\n"
        "  ],\n"
        '  "summary": "one-sentence summary of the most impactful performance improvement"\n'
        "}"
    ),
    "code_quality": (
        "You are a senior engineer auditing a Python codebase for code quality.\n\n"
        "Analyze the provided code context and identify code quality issues:\n"
        "- Inconsistent naming conventions (mixed snake_case/camelCase, unclear abbreviations)\n"
        "- Missing or incorrect type annotations (especially on public APIs)\n"
        "- Test coverage gaps (uncovered code paths, missing edge case tests)\n"
        "- Error handling gaps (bare except clauses, swallowed exceptions)\n"
        "- Code organization issues (large functions that should be split, wrong module placement)\n"
        "- Magic numbers/strings that should be named constants\n"
        "- Inconsistent patterns (same operation done differently in different places)\n\n"
        "Return ONLY valid JSON — no markdown, no explanation.\n\n"
        "JSON shape:\n"
        "{\n"
        '  "ideas": [\n'
        "    {\n"
        '      "title": "short imperative title",\n'
        '      "description": "concrete actionable description of what to improve and why",\n'
        '      "file": "src/example.py or empty string if cross-cutting",\n'
        '      "priority": "high|medium|low",\n'
        '      "effort": "small|medium|large"\n'
        "    }\n"
        "  ],\n"
        '  "summary": "one-sentence summary of the most impactful code quality improvement"\n'
        "}"
    ),
}


def _get_prompt_for_category(category: IdeaCategory) -> str:
    """Return the analysis system prompt for the given category."""
    return _PROMPTS[category]


# ---------------------------------------------------------------------------
# Context gathering
# ---------------------------------------------------------------------------

# rg patterns to search per category — targets the most relevant code
_CATEGORY_RG_PATTERNS: dict[str, list[str]] = {
    "code_improvements": [
        r"def\s+\w+",           # function definitions
        r"TODO|FIXME|HACK|XXX", # markers of known issues
        r"pass\s*$",            # empty function bodies
    ],
    "ui_ux_improvements": [
        r"console\.print|typer\.|rich\.|click\.",
        r"help=|--help",
        r"error|warning|Error|Warning",
    ],
    "documentation_gaps": [
        r'"""',                 # existing docstrings
        r"def\s+[a-z_]+\(",     # function definitions (look for missing docstrings)
        r"class\s+\w+",         # class definitions
        r"TODO|FIXME",
    ],
    "security_hardening": [
        r"subprocess|os\.system|shell=True",
        r"password|secret|token|key|credential",
        r"open\(|read_text|write_text",
        r"input\(|sys\.argv",
    ],
    "performance_optimizations": [
        r"time\.sleep|asyncio\.sleep",
        r"subprocess\.run|subprocess\.Popen",
        r"for\s+\w+\s+in\s+\w+",  # loops that might be optimizable
        r"\.read_text\(|\.write_text\(",
    ],
    "code_quality": [
        r"except\s*:",           # bare except
        r"Any\b|# type: ignore",
        r"def\s+\w+\([^)]*\)\s*->",  # typed functions
        r"def\s+\w+\([^)]*\)\s*:",   # untyped functions
    ],
}

_MAX_CONTEXT_CHARS = 30_000
_MAX_FILE_CHARS = 3_000   # max chars per individual file snippet


def _gather_codebase_context(project_root: Path, category: IdeaCategory) -> str:
    """Gather relevant code snippets from the project for the given analysis category.

    Uses ripgrep to locate relevant code patterns, then reads surrounding context
    from matched files. Falls back to listing .py files if rg is unavailable.
    Truncates combined context to _MAX_CONTEXT_CHARS.
    """
    patterns = _CATEGORY_RG_PATTERNS.get(category, [r"def\s+\w+"])
    src_dir = project_root / "src"
    search_root = str(src_dir) if src_dir.exists() else str(project_root)

    # Collect matched files via rg
    matched_files: set[str] = set()
    for pattern in patterns[:2]:  # limit to first 2 patterns to keep context focused
        try:
            result = subprocess.run(
                ["rg", "--files-with-matches", "--glob", "*.py", pattern, search_root],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=15,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    matched_files.add(line.strip())
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass

    # Fallback: list .py files if rg found nothing
    if not matched_files:
        try:
            glob_root = src_dir if src_dir.exists() else project_root
            for py_file in sorted(glob_root.rglob("*.py"))[:20]:
                matched_files.add(str(py_file))
        except OSError:
            pass

    # Build context snippets from matched files
    parts: list[str] = [
        f"# Codebase context for analysis category: {category}\n"
        f"# Project root: {project_root}\n"
    ]
    total_chars = len(parts[0])
    budget = _MAX_CONTEXT_CHARS - total_chars

    for file_path_str in sorted(matched_files)[:30]:
        if budget <= 0:
            break
        try:
            content = Path(file_path_str).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        if not content.strip():
            continue

        # Truncate individual file to avoid one file dominating
        snippet = content[:_MAX_FILE_CHARS]
        if len(content) > _MAX_FILE_CHARS:
            snippet += f"\n... [{len(content) - _MAX_FILE_CHARS} more chars truncated] ..."

        rel_path = file_path_str
        try:
            rel_path = str(Path(file_path_str).relative_to(project_root))
        except ValueError:
            pass

        entry = f"\n## {rel_path}\n\n{snippet}\n"
        if total_chars + len(entry) > _MAX_CONTEXT_CHARS:
            # Take as much as fits
            remaining = _MAX_CONTEXT_CHARS - total_chars - 50
            if remaining > 200:
                entry = f"\n## {rel_path}\n\n{snippet[:remaining]}\n... [truncated]\n"
                parts.append(entry)
            break

        parts.append(entry)
        total_chars += len(entry)
        budget -= len(entry)

    return "".join(parts)


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _parse_ideas(raw: str, category: IdeaCategory) -> tuple[list[Idea], str]:
    """Parse ideas and summary from a model JSON response.

    Tolerates model preamble/postamble around the JSON object.
    Returns (ideas, summary). Returns ([], "") on any parse failure.
    """
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        print(f"[IDEATION] Failed to parse {category}: no JSON object found in response ({len(raw)} chars)", file=sys.stderr)
        return [], ""

    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError as exc:
        print(f"[IDEATION] Failed to parse {category}: {exc}", file=sys.stderr)
        return [], ""

    if not isinstance(data, dict):
        print(f"[IDEATION] Failed to parse {category}: expected dict, got {type(data).__name__}", file=sys.stderr)
        return [], ""

    summary = str(data.get("summary", "")).strip()
    raw_ideas = data.get("ideas", [])
    if not isinstance(raw_ideas, list):
        return [], summary

    ideas: list[Idea] = []
    for item in raw_ideas:
        if not isinstance(item, dict):
            continue

        title = str(item.get("title", "")).strip()[:200]
        description = str(item.get("description", "")).strip()
        file_ = str(item.get("file", "")).strip()
        priority = str(item.get("priority", "medium")).strip().lower()
        effort = str(item.get("effort", "medium")).strip().lower()

        if priority not in _VALID_PRIORITIES:
            priority = "medium"
        if effort not in _VALID_EFFORTS:
            effort = "medium"

        if not title:
            continue  # skip malformed entries without a title

        ideas.append(Idea(
            title=title,
            description=description,
            category=category,
            file=file_,
            priority=priority,
            effort=effort,
        ))

    return ideas, summary


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


async def run_ideation(
    category: IdeaCategory,
    project_root: Path,
    config: GolemConfig,
    max_ideas: int = 10,
) -> IdeationResult:
    """Run a single-category ideation pass against the codebase.

    Gathers relevant code context via rg, sends it to Claude (Sonnet) with a
    category-specific prompt, and parses the structured JSON response into
    an IdeationResult.

    Args:
        category:     One of the 6 IdeaCategory literals.
        project_root: Root of the project to analyze.
        config:       GolemConfig (used for model selection).
        max_ideas:    Maximum ideas to include in result (truncates after parsing).

    Returns:
        IdeationResult with parsed ideas and summary.
    """
    from golem.config import sdk_env

    t0 = time.monotonic()

    context = _gather_codebase_context(project_root, category)
    system_prompt = _get_prompt_for_category(category)

    user_message = (
        f"Analyze the following codebase context and identify up to {max_ideas} "
        f"high-value improvement ideas for category: {category}.\n\n"
        f"Focus on concrete, actionable suggestions. "
        f"Prioritize issues with the highest impact.\n\n"
        f"## Codebase Context\n\n{context}"
    )

    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        model=config.validator_model,  # Sonnet — fast, capable, cost-effective
        max_turns=3,
        permission_mode="bypassPermissions",
        env=sdk_env(),
    )

    raw_response = ""

    try:
        async for message in query(prompt=user_message, options=options):
            if isinstance(message, ResultMessage):
                if message.result:
                    raw_response += message.result
            elif isinstance(message, AssistantMessage):
                for block in message.content:
                    if hasattr(block, "text"):
                        raw_response += block.text
    except Exception as exc:
        print(
            f"[IDEATION] Warning: ideation pass '{category}' failed: {exc}",
            file=sys.stderr,
        )
        return IdeationResult(
            category=category,
            ideas=[],
            summary=f"Analysis failed: {exc}",
            duration_s=time.monotonic() - t0,
        )

    if not raw_response.strip():
        print(f"[IDEATION] Warning: empty response for category '{category}'", file=sys.stderr)
    else:
        print(f"[IDEATION] Parsing response for '{category}' ({len(raw_response)} chars)", file=sys.stderr)
    ideas, summary = _parse_ideas(raw_response, category)
    # Apply max_ideas cap
    ideas = ideas[:max_ideas]

    duration_s = time.monotonic() - t0
    print(
        f"[IDEATION] {category}: {len(ideas)} idea(s) in {duration_s:.1f}s",
        file=sys.stderr,
    )

    return IdeationResult(
        category=category,
        ideas=ideas,
        summary=summary,
        duration_s=duration_s,
    )


async def run_all_ideation(
    project_root: Path,
    config: GolemConfig,
    max_ideas: int = 10,
) -> list[IdeationResult]:
    """Run all 6 ideation categories sequentially and return combined results.

    Runs categories one at a time to avoid rate limits. Each pass is
    independent — a failure in one category does not abort the others.

    Args:
        project_root: Root of the project to analyze.
        config:       GolemConfig (used for model selection).
        max_ideas:    Maximum ideas per category.

    Returns:
        List of IdeationResult, one per category (always 6 entries).
    """
    results: list[IdeationResult] = []
    for category in ALL_CATEGORIES:
        result = await run_ideation(category, project_root, config, max_ideas=max_ideas)
        results.append(result)
    return results
