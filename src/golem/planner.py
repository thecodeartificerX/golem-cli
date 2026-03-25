from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from golem.config import GolemConfig
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
    "max_retries": 3,
    "max_parallel": 3,
    "max_worker_turns": 50,
    "max_validator_turns": 20
  },
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
    spec_content = spec_path.read_text()

    # Gather project context
    project_context = ""
    cwd = repo_root or spec_path.parent
    for name in ("CLAUDE.md", "README.md", "README"):
        candidate = cwd / name
        if candidate.exists():
            project_context += f"## {name}\n{candidate.read_text()[:4000]}\n\n"
            break

    template = _PLANNER_PROMPT_TEMPLATE.read_text()
    prompt = template.replace("{spec_content}", spec_content)
    prompt = prompt.replace("{project_context}", project_context or "(none)")
    prompt = prompt.replace("{tasks_json_schema}", _TASKS_JSON_SCHEMA)

    result_text = ""
    async for message in query(
        prompt=prompt,
        options=ClaudeAgentOptions(
            model=config.planner_model,
            cwd=str(cwd),
            allowed_tools=["Read", "Glob", "Grep", "Bash"],
            max_turns=30,
            permission_mode="acceptEdits",
        ),
    ):
        if isinstance(message, ResultMessage):
            result_text = message.result or ""

    raw_json = _extract_json(result_text)
    data = json.loads(raw_json)

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
