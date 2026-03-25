from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class GolemConfig:
    max_parallel: int = 3
    max_retries: int = 3
    planner_model: str = "claude-opus-4-6"
    worker_model: str = "claude-opus-4-6"
    validator_model: str = "claude-sonnet-4-6"
    max_worker_turns: int = 50
    max_validator_turns: int = 20
    auto_pr: bool = True
    pr_target: str = "main"


def load_config(golem_dir: Path) -> GolemConfig:
    config_path = golem_dir / "config.json"
    if not config_path.exists():
        return GolemConfig()
    with open(config_path) as f:
        data = json.load(f)
    return GolemConfig(**{k: v for k, v in data.items() if k in GolemConfig.__dataclass_fields__})


def save_config(config: GolemConfig, golem_dir: Path) -> None:
    golem_dir.mkdir(parents=True, exist_ok=True)
    with open(golem_dir / "config.json", "w") as f:
        json.dump(asdict(config), f, indent=2)
