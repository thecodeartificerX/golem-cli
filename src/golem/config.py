from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class GolemConfig:
    max_parallel: int = 3
    max_retries: int = 2
    planner_model: str = "claude-opus-4-6"
    worker_model: str = "claude-opus-4-6"
    validator_model: str = "claude-sonnet-4-6"
    tech_lead_model: str = "claude-opus-4-6"
    max_worker_turns: int = 50
    max_validator_turns: int = 20
    auto_pr: bool = True
    pr_target: str = "main"
    # Exclude "user" to prevent user-level plugin hooks (e.g. claude-mem SessionEnd)
    # from firing in headless SDK sessions and killing them.
    setting_sources: list[str] = field(default_factory=lambda: ["project"])
    # Always-on deterministic checks (lint, syntax). Populated at runtime by auto-detection,
    # not persisted to config.json. Agents cannot skip these.
    infrastructure_checks: list[str] = field(default_factory=list)


def load_config(golem_dir: Path) -> GolemConfig:
    config_path = golem_dir / "config.json"
    if not config_path.exists():
        return GolemConfig()
    with open(config_path, encoding="utf-8") as f:
        data = json.load(f)
    return GolemConfig(**{k: v for k, v in data.items() if k in GolemConfig.__dataclass_fields__})


def sdk_env() -> dict[str, str]:
    """Environment overrides for Claude Agent SDK subprocess.

    Clears ANTHROPIC_API_KEY so the spawned claude CLI uses its own
    OAuth auth instead of treating the env var as an external API key.
    """
    return {"ANTHROPIC_API_KEY": ""}


_EPHEMERAL_FIELDS = {"infrastructure_checks"}


def save_config(config: GolemConfig, golem_dir: Path) -> None:
    golem_dir.mkdir(parents=True, exist_ok=True)
    data = {k: v for k, v in asdict(config).items() if k not in _EPHEMERAL_FIELDS}
    with open(golem_dir / "config.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
