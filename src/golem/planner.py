from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    query,
)
from claude_agent_sdk._internal.query import Query as _Query  # noqa: PLC2701

from golem.config import GolemConfig, sdk_env

# The SDK hardcodes a 60s initialize timeout with no public API to override it.
# Claude CLI can take longer when loading MCP servers, hooks, etc. Bump to 180s.
# Defaults tuple is (None, None, None, 60.0, None) — index 3 is initialize_timeout.
_defaults = list(_Query.__init__.__defaults__ or ())  # type: ignore[attr-defined]
_defaults[3] = 180.0
_Query.__init__.__defaults__ = tuple(_defaults)  # type: ignore[attr-defined]
from golem.tasks import TasksFile, write_tasks

_PLANNER_PROMPT_TEMPLATE = Path(__file__).parent / "prompts" / "planner.md"

_TASKS_JSON_SCHEMA = """
{
  "spec": "path/to/spec.md",
  "created": "ISO-8601 timestamp",
  "project": "project name",
  "branch": "golem/<spec-slug>",
  "models": {
    "planner": "opus model id",
    "worker": "opus model id",
    "validator": "sonnet model id"
  },
  "config": {
    "max_retries": 2,
    "max_parallel": 3,
    "max_worker_turns": 50,
    "max_validator_turns": 20
  },
  "blueprint": "Multi-line string with ALL cross-cutting contracts: DOM IDs, CSS classes, API signatures, data schemas, naming conventions. Injected into every worker and validator prompt. Empty string if no cross-cutting concerns.",
  "groups": [
    {
      "id": "group-slug",
      "description": "Human-readable group description",
      "worktree_branch": "golem/<spec-slug>/<group-slug>",
      "tasks": [
        {
          "id": "task-001",
          "description": "What to implement",
          "files_create": ["path/to/new/file"],
          "files_modify": ["path/to/existing/file"],
          "depends_on": [],
          "acceptance": ["Specific verifiable criterion"],
          "validation_commands": ["bash command that returns 0 on success"],
          "reference_docs": [],
          "status": "pending",
          "retries": 0,
          "last_feedback": null,
          "blocked_reason": null,
          "completed_at": null
        }
      ]
    }
  ],
  "final_validation": {
    "depends_on_all": true,
    "commands": ["validation commands to run after merge"]
  }
}
"""


def _extract_json(text: str) -> str:
    """Extract JSON from text, stripping markdown fences if present."""
    text = text.strip()
    # Strip ```json fences
    fence_match = re.search(r"```(?:json)?\s*\n([\s\S]*?)\n```", text)
    if fence_match:
        return fence_match.group(1).strip()
    # Try to find the outermost JSON object
    start = text.find("{")
    if start == -1:
        raise ValueError("No JSON object found in planner output")
    end = text.rfind("}")
    if end == -1:
        raise ValueError("No closing } found in planner output")
    return text[start : end + 1]


async def run_planner(spec_path: Path, golem_dir: Path, config: GolemConfig, repo_root: Path | None = None) -> TasksFile:
    """Spawn Opus planner session, parse tasks.json output, write to golem_dir/tasks.json."""
    spec_content = spec_path.read_text(encoding="utf-8")

    # Gather project context
    project_context = ""
    cwd = repo_root or spec_path.parent
    for name in ("CLAUDE.md", "README.md", "README"):
        candidate = cwd / name
        if candidate.exists():
            project_context += f"## {name}\n{candidate.read_text(encoding='utf-8')[:4000]}\n\n"
            break

    template = _PLANNER_PROMPT_TEMPLATE.read_text(encoding="utf-8")
    prompt = template.replace("{spec_content}", spec_content)
    prompt = prompt.replace("{project_context}", project_context or "(none)")
    prompt = prompt.replace("{tasks_json_schema}", _TASKS_JSON_SCHEMA)

    result_text = ""
    all_text_chunks: list[str] = []
    async for message in query(
        prompt=prompt,
        options=ClaudeAgentOptions(
            model=config.planner_model,
            cwd=str(cwd),
            tools={"type": "preset", "preset": "claude_code"},
            setting_sources=config.setting_sources,
            max_turns=30,
            permission_mode="bypassPermissions",
            env=sdk_env(),
        ),
    ):
        if isinstance(message, ResultMessage):
            result_text = message.result or ""
            print(f"[PLANNER] result: {result_text[:200]}...", file=sys.stderr, flush=True)
        elif isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    all_text_chunks.append(block.text)
                    print(f"[PLANNER] {block.text[:300]}", file=sys.stderr, flush=True)
                elif isinstance(block, ToolUseBlock):
                    input_preview = json.dumps(block.input, default=str)[:200]
                    print(f"[PLANNER] tool: {block.name}({input_preview})", file=sys.stderr, flush=True)
                elif isinstance(block, ToolResultBlock):
                    content_str = str(block.content or "")[:200]
                    err = " ERROR" if block.is_error else ""
                    print(f"[PLANNER] result{err}: {content_str}", file=sys.stderr, flush=True)

    # Prefer ResultMessage, fall back to concatenated assistant text
    output = result_text if result_text.strip() else "\n".join(all_text_chunks)
    raw_json = _extract_json(output)
    data = json.loads(raw_json)

    if not data.get("blueprint"):
        print("[PLANNER] WARNING: blueprint field missing or empty — cross-cutting checks may be weaker", file=sys.stderr, flush=True)

    # Inject runtime metadata
    spec_slug = spec_path.stem.lower().replace("_", "-").replace(" ", "-")
    data.setdefault("spec", str(spec_path))
    data.setdefault("created", datetime.now(tz=timezone.utc).isoformat())
    data.setdefault("project", cwd.name)
    data.setdefault("branch", f"golem/{spec_slug}")
    data.setdefault("models", {
        "planner": config.planner_model,
        "worker": config.worker_model,
        "validator": config.validator_model,
    })
    data.setdefault("config", {
        "max_retries": config.max_retries,
        "max_parallel": config.max_parallel,
        "max_worker_turns": config.max_worker_turns,
        "max_validator_turns": config.max_validator_turns,
    })

    tasks_file = TasksFile.from_dict(data)
    tasks_path = golem_dir / "tasks.json"
    await write_tasks(tasks_file, tasks_path)
    return tasks_file
