"""AI-powered changelog and commit message generation.

Provides two main capabilities:
- generate_changelog(): git log + diff -> Keep-a-Changelog formatted entry
- generate_commit_message(): git diff -> conventional commit message

Both use a single-turn Claude (Haiku) call for low-cost generation.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, query

if TYPE_CHECKING:
    from golem.config import GolemConfig

_MAX_DIFF_CHARS = 20_000
_MAX_LOG_CHARS = 10_000

_CHANGELOG_SYSTEM_PROMPT = (
    "You are a changelog writer that produces Keep-a-Changelog formatted entries. "
    "Given git log lines and a diff stat, categorize each change into Added/Changed/Fixed/Removed. "
    "Return ONLY valid JSON — no markdown fences, no explanation.\n\n"
    "JSON shape:\n"
    "{\n"
    '  "added": ["description of new feature or file", ...],\n'
    '  "changed": ["description of changed behaviour", ...],\n'
    '  "fixed": ["description of bug fix", ...],\n'
    '  "removed": ["description of removed feature", ...],\n'
    '  "summary": "one-sentence overview of the release"\n'
    "}"
)

_COMMIT_SYSTEM_PROMPT = (
    "You are a conventional commit message generator. "
    "Given a git diff, produce a structured conventional commit message. "
    "Return ONLY valid JSON — no markdown fences, no explanation.\n\n"
    "Allowed types: feat, fix, refactor, test, docs, chore, perf, ci, build, style.\n\n"
    "JSON shape:\n"
    "{\n"
    '  "type": "feat",\n'
    '  "scope": "cli",\n'
    '  "description": "short imperative description (max 72 chars total with type+scope)",\n'
    '  "body": "optional longer explanation (empty string if not needed)",\n'
    '  "breaking": false\n'
    "}"
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ChangelogEntry:
    version: str
    date: str
    added: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    fixed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    summary: str = ""


@dataclass
class CommitMessage:
    type: str
    scope: str
    description: str
    body: str
    breaking: bool = False


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def format_changelog(entry: ChangelogEntry) -> str:
    """Render a ChangelogEntry to Keep-a-Changelog markdown format.

    Produces a ## [version] - date header followed by ### sections for
    each non-empty category.
    """
    lines: list[str] = []
    lines.append(f"## [{entry.version}] - {entry.date}")
    if entry.summary:
        lines.append("")
        lines.append(entry.summary)

    for section, items in (
        ("Added", entry.added),
        ("Changed", entry.changed),
        ("Fixed", entry.fixed),
        ("Removed", entry.removed),
    ):
        if items:
            lines.append("")
            lines.append(f"### {section}")
            lines.append("")
            for item in items:
                lines.append(f"- {item}")

    return "\n".join(lines)


def format_commit_message(msg: CommitMessage) -> str:
    """Render a CommitMessage to conventional commit format.

    Format: type(scope): description
    Optionally followed by a blank line and body.
    Breaking changes add '!' after the scope.
    """
    breaking_marker = "!" if msg.breaking else ""
    scope_part = f"({msg.scope})" if msg.scope else ""
    header = f"{msg.type}{scope_part}{breaking_marker}: {msg.description}"

    if msg.body:
        return f"{header}\n\n{msg.body}"
    return header


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _run_git(args: list[str], cwd: str | None = None) -> str:
    """Run a git command and return stdout. Returns empty string on failure."""
    try:
        result = subprocess.run(
            ["git"] + args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            cwd=cwd,
        )
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        return ""


def _resolve_since(since: str, cwd: str | None = None) -> str:
    """Resolve --since to a git ref. Returns first commit SHA if no tags exist."""
    if since:
        return since

    # Try to find the latest tag
    latest_tag = _run_git(["describe", "--tags", "--abbrev=0"], cwd=cwd)
    if latest_tag:
        return latest_tag

    # Fall back to the first commit
    first_commit = _run_git(["rev-list", "--max-parents=0", "HEAD"], cwd=cwd)
    return first_commit or ""


# ---------------------------------------------------------------------------
# Response parsers
# ---------------------------------------------------------------------------


def _parse_changelog_response(raw: str, version: str, date: str) -> ChangelogEntry:
    """Parse model JSON into ChangelogEntry. Returns empty entry on any error."""
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return ChangelogEntry(version=version, date=date)

    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return ChangelogEntry(version=version, date=date)

    if not isinstance(data, dict):
        return ChangelogEntry(version=version, date=date)

    def _str_list(key: str) -> list[str]:
        items = data.get(key, [])
        if not isinstance(items, list):
            return []
        return [str(i) for i in items if i]

    return ChangelogEntry(
        version=version,
        date=date,
        added=_str_list("added"),
        changed=_str_list("changed"),
        fixed=_str_list("fixed"),
        removed=_str_list("removed"),
        summary=str(data.get("summary", "")),
    )


def _parse_commit_response(raw: str) -> CommitMessage:
    """Parse model JSON into CommitMessage. Returns a generic chore on error."""
    _fallback = CommitMessage(
        type="chore",
        scope="",
        description="update code",
        body="",
        breaking=False,
    )

    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return _fallback

    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return _fallback

    if not isinstance(data, dict):
        return _fallback

    _valid_types = {"feat", "fix", "refactor", "test", "docs", "chore", "perf", "ci", "build", "style"}
    commit_type = str(data.get("type", "chore"))
    if commit_type not in _valid_types:
        commit_type = "chore"

    return CommitMessage(
        type=commit_type,
        scope=str(data.get("scope", "")),
        description=str(data.get("description", "update code")),
        body=str(data.get("body", "")),
        breaking=bool(data.get("breaking", False)),
    )


# ---------------------------------------------------------------------------
# Async generation functions
# ---------------------------------------------------------------------------


async def generate_changelog(
    since: str,
    version: str,
    config: GolemConfig,
    previous_changelog: str = "",
    cwd: str | None = None,
) -> ChangelogEntry:
    """Generate a Keep-a-Changelog entry from git history.

    Runs git log and git diff --stat between `since` and HEAD, then asks
    Claude (Haiku) to categorize the changes. Falls back gracefully on
    any error.

    Args:
        since: Git ref (tag or SHA) to compare from. Empty = auto-resolve.
        version: Version label for the entry header (e.g. "1.2.0" or "Unreleased").
        config: GolemConfig (provides model name and SDK env).
        previous_changelog: Optional existing CHANGELOG.md content for style matching.
        cwd: Working directory for git commands. Defaults to CWD.
    """
    from golem.config import sdk_env

    date = datetime.now(tz=UTC).strftime("%Y-%m-%d")
    resolved_since = _resolve_since(since, cwd=cwd)

    try:
        # Get commit list
        log_range = f"{resolved_since}..HEAD" if resolved_since else "HEAD"
        log_output = _run_git(["log", "--oneline", log_range], cwd=cwd)
        if len(log_output) > _MAX_LOG_CHARS:
            log_output = log_output[:_MAX_LOG_CHARS] + "\n[... log truncated ...]"

        # Get diff stat
        if resolved_since:
            diff_stat = _run_git(["diff", f"{resolved_since}..HEAD", "--stat"], cwd=cwd)
        else:
            diff_stat = _run_git(["diff", "--stat", "HEAD~1", "HEAD"], cwd=cwd)
        if len(diff_stat) > _MAX_DIFF_CHARS:
            diff_stat = diff_stat[:_MAX_DIFF_CHARS] + "\n[... diff truncated ...]"

        if not log_output and not diff_stat:
            return ChangelogEntry(version=version, date=date, summary="No changes detected.")

        prompt_parts: list[str] = [
            f"Version: {version}",
            f"Date: {date}",
            "",
            "## Git log (one commit per line)",
            log_output or "(no commits)",
            "",
            "## Diff stat",
            diff_stat or "(no diff)",
        ]
        if previous_changelog:
            style_sample = previous_changelog[:2000]
            prompt_parts += [
                "",
                "## Existing CHANGELOG.md (style reference -- match this tone)",
                style_sample,
            ]
        prompt_parts += ["", "Categorize these changes into Added/Changed/Fixed/Removed JSON:"]
        prompt = "\n".join(prompt_parts)

        options = ClaudeAgentOptions(
            system_prompt=_CHANGELOG_SYSTEM_PROMPT,
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

        return _parse_changelog_response(raw_response, version, date)

    except Exception as exc:
        print(
            f"[CHANGELOG] Warning: changelog generation failed: {exc}",
            file=sys.stderr,
        )
        return ChangelogEntry(version=version, date=date)


async def generate_commit_message(
    diff: str,
    config: GolemConfig,
) -> CommitMessage:
    """Generate a conventional commit message from a git diff.

    Sends the diff to Claude (Haiku) with a conventional commits prompt.
    Falls back to a generic 'chore: update code' on any error.

    Args:
        diff: The output of `git diff --cached` (staged changes).
        config: GolemConfig (provides model name and SDK env).
    """
    from golem.config import sdk_env

    try:
        if not diff.strip():
            return CommitMessage(
                type="chore",
                scope="",
                description="no staged changes",
                body="",
                breaking=False,
            )

        diff_input = diff
        if len(diff_input) > _MAX_DIFF_CHARS:
            diff_input = diff_input[:_MAX_DIFF_CHARS] + "\n\n[... diff truncated ...]"

        prompt = (
            "## Staged git diff\n\n"
            + diff_input
            + "\n\nGenerate a conventional commit message JSON for these changes:"
        )

        options = ClaudeAgentOptions(
            system_prompt=_COMMIT_SYSTEM_PROMPT,
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

        return _parse_commit_response(raw_response)

    except Exception as exc:
        print(
            f"[CHANGELOG] Warning: commit message generation failed: {exc}",
            file=sys.stderr,
        )
        return CommitMessage(
            type="chore",
            scope="",
            description="update code",
            body="",
            breaking=False,
        )
