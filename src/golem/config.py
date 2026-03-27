from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Known Claude model name patterns — warn (don't error) on unrecognized names
_KNOWN_MODEL_PREFIXES = ("claude-opus-", "claude-sonnet-", "claude-haiku-")


@dataclass
class GolemConfig:
    max_parallel: int = 3
    max_retries: int = 2
    planner_model: str = "claude-opus-4-6"
    worker_model: str = "claude-opus-4-6"
    validator_model: str = "claude-sonnet-4-6"
    tech_lead_model: str = "claude-opus-4-6"
    max_worker_turns: int = 50
    max_tech_lead_turns: int = 100
    sdk_timeout: int = 180
    retry_delay: int = 10
    pr_target: str = "main"
    # Exclude "user" to prevent user-level plugin hooks (e.g. claude-mem SessionEnd)
    # from firing in headless SDK sessions and killing them.
    setting_sources: list[str] = field(default_factory=lambda: ["project"])
    # Per-role setting source overrides (keys: "planner", "tech_lead", "writer")
    agent_setting_sources: dict[str, list[str]] = field(default_factory=dict)
    # Per-role extra MCP servers merged with golem's in-process MCP
    extra_mcp_servers: dict[str, dict[str, dict[str, object]]] = field(default_factory=dict)
    # Always-on deterministic checks (lint, syntax). Populated at runtime by auto-detection,
    # not persisted to config.json. Agents cannot skip these.
    infrastructure_checks: list[str] = field(default_factory=list)

    def validate(self) -> list[str]:
        """Validate config values. Returns list of warning messages (empty = all good)."""
        warnings: list[str] = []
        for field_name in ("planner_model", "worker_model", "validator_model", "tech_lead_model"):
            model = getattr(self, field_name)
            if not any(model.startswith(p) for p in _KNOWN_MODEL_PREFIXES):
                warnings.append(f"Unknown model for {field_name}: {model!r} — may fail at runtime")
        if self.max_parallel < 1:
            warnings.append(f"max_parallel must be >= 1, got {self.max_parallel}")
        if self.max_retries < 0:
            warnings.append(f"max_retries must be >= 0, got {self.max_retries}")
        if self.max_worker_turns < 1:
            warnings.append(f"max_worker_turns must be >= 1, got {self.max_worker_turns}")
        valid_sources = {"project", "user"}
        for src in self.setting_sources:
            if src not in valid_sources:
                warnings.append(f"Unknown setting_source: {src!r} — valid values are {sorted(valid_sources)}")

        # Validate agent_setting_sources
        valid_roles = {"planner", "tech_lead", "writer"}
        for role in self.agent_setting_sources:
            if role not in valid_roles:
                warnings.append(
                    f"Unknown role in agent_setting_sources: {role!r}"
                    f" — valid roles are {sorted(valid_roles)}"
                )
            for src in self.agent_setting_sources.get(role, []):
                if src not in valid_sources:
                    warnings.append(
                        f"Unknown setting_source in agent_setting_sources[{role!r}]: {src!r}"
                    )

        # Validate extra_mcp_servers
        for role in self.extra_mcp_servers:
            if role not in valid_roles:
                warnings.append(f"Unknown role in extra_mcp_servers: {role!r} — valid roles are {sorted(valid_roles)}")
            for name, server_config in self.extra_mcp_servers.get(role, {}).items():
                if not isinstance(server_config, dict):
                    warnings.append(
                        f"extra_mcp_servers[{role!r}][{name!r}] must be a dict,"
                        f" got {type(server_config).__name__}"
                    )
                elif "command" not in server_config and "url" not in server_config:
                    warnings.append(
                        f"extra_mcp_servers[{role!r}][{name!r}] must have"
                        " 'command' (stdio) or 'url' (sse/http)"
                    )

        # Warn if "user" appears in any setting sources (risk of plugin hooks)
        all_sources = list(self.setting_sources)
        for role_sources in self.agent_setting_sources.values():
            all_sources.extend(role_sources)
        if "user" in all_sources:
            warnings.append("'user' in sources — user-level plugins/hooks may interfere with SDK sessions")

        return warnings


def load_config(golem_dir: Path) -> GolemConfig:
    config_path = golem_dir / "config.json"
    if not config_path.exists():
        config = GolemConfig()
    else:
        with open(config_path, encoding="utf-8") as f:
            data = json.load(f)
        config = GolemConfig(**{k: v for k, v in data.items() if k in GolemConfig.__dataclass_fields__})
    warnings = config.validate()
    for w in warnings:
        print(f"[CONFIG] Warning: {w}", file=sys.stderr)
    return config


def sdk_env() -> dict[str, str]:
    """Environment overrides for Claude Agent SDK subprocess.

    Returns a *partial* dict (not a full env copy). The SDK merges these
    overrides into the inherited environment. Only keys that need to be
    changed are included.

    Clears ANTHROPIC_API_KEY so the spawned claude CLI uses its own
    OAuth auth instead of treating the env var as an external API key.
    """
    return {"ANTHROPIC_API_KEY": "", "CLAUDECODE": ""}


def resolve_agent_options(
    config: GolemConfig,
    role: str,
    golem_mcp: object,
    golem_mcp_name: str = "golem",
) -> tuple[list[str], dict[str, object]]:
    """Return (setting_sources, mcp_servers) for the given agent role."""
    sources = config.agent_setting_sources.get(role) or config.setting_sources
    mcps: dict[str, object] = {golem_mcp_name: golem_mcp}
    mcps.update(config.extra_mcp_servers.get(role, {}))
    return sources, mcps


_EPHEMERAL_FIELDS = {"infrastructure_checks"}


def save_config(config: GolemConfig, golem_dir: Path) -> None:
    golem_dir.mkdir(parents=True, exist_ok=True)
    data = {k: v for k, v in asdict(config).items() if k not in _EPHEMERAL_FIELDS}
    with open(golem_dir / "config.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
