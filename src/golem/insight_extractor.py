"""Post-session insight extractor.

Runs after each writer (junior dev) session completes. Extracts structured
insights from the git diff using a cheap model (Haiku) and writes them to
the session memory directory.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, query

if TYPE_CHECKING:
    from golem.config import GolemConfig

_MAX_DIFF_CHARS = 30_000
_VALID_CATEGORIES = frozenset({"pattern", "gotcha", "convention", "dependency"})


@dataclass
class FileInsight:
    path: str
    observation: str
    category: str  # one of: pattern, gotcha, convention, dependency


@dataclass
class InsightResult:
    file_insights: list[FileInsight] = field(default_factory=list)
    patterns_discovered: list[str] = field(default_factory=list)
    gotchas_discovered: list[str] = field(default_factory=list)
    approach_outcome: str = ""
    recommendations: list[str] = field(default_factory=list)


_EXTRACTION_SYSTEM_PROMPT = (
    "You are a codebase insight extractor. "
    "Given a git diff, extract structured learnings for future coding sessions. "
    "Return ONLY valid JSON — no markdown, no explanation.\n\n"
    "JSON shape:\n"
    "{\n"
    '  "file_insights": [\n'
    '    {"path": "src/foo.py", "observation": "...", "category": "pattern|gotcha|convention|dependency"}\n'
    "  ],\n"
    '  "patterns_discovered": ["pattern description", ...],\n'
    '  "gotchas_discovered": ["gotcha description", ...],\n'
    '  "approach_outcome": "one-sentence summary of what was accomplished",\n'
    '  "recommendations": ["recommendation for future sessions", ...]\n'
    "}"
)


def _build_extraction_prompt(diff_stat: str, diff_body: str, ticket_id: str) -> str:
    parts: list[str] = [
        f"Ticket: {ticket_id}",
        "",
        "## Diff stat",
        diff_stat or "(no stat output)",
        "",
        "## Full diff",
    ]
    body = diff_body or "(no diff output)"
    if len(body) > _MAX_DIFF_CHARS:
        body = body[:_MAX_DIFF_CHARS] + "\n\n[... diff truncated ...]"
    parts.append(body)
    parts.append("")
    parts.append("Extract insights as JSON:")
    return "\n".join(parts)


def _parse_response(raw: str) -> InsightResult:
    """Parse JSON response from the model into InsightResult.

    Tries to extract a JSON object even if the model adds surrounding text.
    Falls back to empty result on any parse error.
    """
    # Find first '{' and last '}' to handle model preamble/postamble
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return InsightResult()

    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return InsightResult()

    if not isinstance(data, dict):
        return InsightResult()

    file_insights: list[FileInsight] = []
    for item in data.get("file_insights", []):
        if not isinstance(item, dict):
            continue
        path = str(item.get("path", ""))
        observation = str(item.get("observation", ""))
        category = str(item.get("category", "pattern"))
        if category not in _VALID_CATEGORIES:
            category = "pattern"
        if path and observation:
            file_insights.append(FileInsight(path=path, observation=observation, category=category))

    patterns: list[str] = [str(p) for p in data.get("patterns_discovered", []) if p]
    gotchas: list[str] = [str(g) for g in data.get("gotchas_discovered", []) if g]
    approach_outcome = str(data.get("approach_outcome", ""))
    recommendations: list[str] = [str(r) for r in data.get("recommendations", []) if r]

    return InsightResult(
        file_insights=file_insights,
        patterns_discovered=patterns,
        gotchas_discovered=gotchas,
        approach_outcome=approach_outcome,
        recommendations=recommendations,
    )


async def extract_insights(
    worktree_path: Path,
    ticket_id: str,
    config: GolemConfig,
) -> InsightResult:
    """Extract structured insights from the git diff in a worktree.

    Runs git diff HEAD~1 (stat + full) in the worktree, then spawns a
    single-turn Haiku session to extract structured JSON insights.

    Falls back gracefully on any error — returns an empty InsightResult
    and logs a warning to stderr so writer session failure is never caused
    by insight extraction.
    """
    from golem.config import sdk_env

    try:
        # Run git diff --stat
        stat_proc = subprocess.run(
            ["git", "diff", "HEAD~1", "--stat"],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        diff_stat = stat_proc.stdout.strip()

        # Run full git diff
        diff_proc = subprocess.run(
            ["git", "diff", "HEAD~1"],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        diff_body = diff_proc.stdout.strip()

        if not diff_stat and not diff_body:
            # Nothing changed — no insights to extract
            return InsightResult()

        prompt = _build_extraction_prompt(diff_stat, diff_body, ticket_id)

        # Spawn a single-turn Haiku session
        options = ClaudeAgentOptions(
            system_prompt=_EXTRACTION_SYSTEM_PROMPT,
            model=config.insight_model,
            max_turns=3,
            permission_mode="bypassPermissions",
            env=sdk_env(),
        )

        raw_response = ""
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, ResultMessage) and message.result:
                raw_response = message.result
            elif isinstance(message, AssistantMessage):
                for block in message.content:
                    if hasattr(block, "text"):
                        raw_response += block.text

        return _parse_response(raw_response)

    except Exception as exc:
        print(
            f"[INSIGHT] Warning: insight extraction failed for {ticket_id}: {exc}",
            file=sys.stderr,
        )
        return InsightResult()


def write_insights(result: InsightResult, memory_dir: Path) -> None:
    """Persist an InsightResult to the session memory directory.

    - Appends each gotcha to memory/gotchas.md
    - Merges file insights into memory/codebase_map.json
    - Writes patterns to memory/patterns.json
    """
    memory_dir.mkdir(parents=True, exist_ok=True)

    # --- gotchas.md ---
    if result.gotchas_discovered:
        gotchas_file = memory_dir / "gotchas.md"
        now = datetime.now(tz=UTC)
        timestamp = f"{now.year}-{now.month:02d}-{now.day:02d} {now.hour:02d}:{now.minute:02d}"

        is_new = not gotchas_file.exists() or gotchas_file.stat().st_size == 0
        header = "# Gotchas & Pitfalls\n\nThings to watch out for in this codebase.\n" if is_new else ""

        entries: list[str] = []
        for gotcha in result.gotchas_discovered:
            entries.append(f"\n## [{timestamp}] (insight)\n{gotcha}\n")

        with open(gotchas_file, "a" if not is_new else "w", encoding="utf-8") as fh:
            fh.write(header + "".join(entries))

    # --- codebase_map.json ---
    if result.file_insights:
        map_file = memory_dir / "codebase_map.json"
        codebase_map: dict[str, object] = {"discovered_files": {}, "last_updated": None}
        if map_file.exists():
            try:
                raw = map_file.read_text(encoding="utf-8")
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    codebase_map = parsed
            except (json.JSONDecodeError, OSError):
                pass

        discovered: dict[str, object] = codebase_map.get("discovered_files", {})  # type: ignore[assignment]
        if not isinstance(discovered, dict):
            discovered = {}
        for insight in result.file_insights:
            discovered[insight.path] = {
                "description": insight.observation,
                "category": insight.category,
                "discovered_at": datetime.now(tz=UTC).isoformat(),
            }
        codebase_map["discovered_files"] = discovered
        codebase_map["last_updated"] = datetime.now(tz=UTC).isoformat()

        _write_json_atomic(map_file, codebase_map)

    # --- patterns.json ---
    if result.patterns_discovered or result.recommendations or result.approach_outcome:
        patterns_file = memory_dir / "patterns.json"
        existing: dict[str, object] = {"patterns": [], "recommendations": [], "outcomes": []}
        if patterns_file.exists():
            try:
                raw = patterns_file.read_text(encoding="utf-8")
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    existing = parsed
            except (json.JSONDecodeError, OSError):
                pass

        patterns_list: list[str] = existing.get("patterns", [])  # type: ignore[assignment]
        if not isinstance(patterns_list, list):
            patterns_list = []
        patterns_list.extend(result.patterns_discovered)

        recs_list: list[str] = existing.get("recommendations", [])  # type: ignore[assignment]
        if not isinstance(recs_list, list):
            recs_list = []
        recs_list.extend(result.recommendations)

        outcomes_list: list[str] = existing.get("outcomes", [])  # type: ignore[assignment]
        if not isinstance(outcomes_list, list):
            outcomes_list = []
        if result.approach_outcome:
            outcomes_list.append(result.approach_outcome)

        existing["patterns"] = patterns_list
        existing["recommendations"] = recs_list
        existing["outcomes"] = outcomes_list
        existing["last_updated"] = datetime.now(tz=UTC).isoformat()

        _write_json_atomic(patterns_file, existing)


def _write_json_atomic(path: Path, data: dict[str, object]) -> None:
    """Write JSON to path atomically via tmp+rename."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
