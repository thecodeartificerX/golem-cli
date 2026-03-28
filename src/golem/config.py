from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Known Claude model name patterns — warn (don't error) on unrecognized names
_KNOWN_MODEL_PREFIXES = ("claude-opus-", "claude-sonnet-", "claude-haiku-")


@dataclass
class ComplexityProfile:
    planner_model: str = "claude-opus-4-6"
    planner_max_turns: int = 50
    tech_lead_model: str = "claude-opus-4-6"
    tech_lead_max_turns: int = 100
    worker_model: str = "claude-opus-4-6"
    worker_max_turns: int = 50
    skip_tech_lead: bool = False


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
    dispatch_jitter_max: float = 5.0  # Max seconds of random jitter before writer spawn
    pr_target: str = "main"
    session_id: str = ""
    branch_prefix: str = "golem"
    merge_auto_rebase: bool = True
    archive_delay_minutes: int = 30
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
    # Conductor
    conductor_enabled: bool = True
    skip_tech_lead: bool = False
    planner_max_turns: int = 50  # FIX: was hardcoded in planner.py
    # Complexity profiles (defaults provided, operator can override)
    complexity_profiles: dict[str, dict] = field(default_factory=lambda: {
        "TRIVIAL": {"planner_model": "claude-haiku-4-5-20251001", "planner_max_turns": 10,
                    "tech_lead_model": "", "tech_lead_max_turns": 0,
                    "worker_model": "claude-sonnet-4-6", "worker_max_turns": 20,
                    "skip_tech_lead": True},
        "SIMPLE": {"planner_model": "claude-sonnet-4-6", "planner_max_turns": 20,
                   "tech_lead_model": "claude-sonnet-4-6", "tech_lead_max_turns": 30,
                   "worker_model": "claude-sonnet-4-6", "worker_max_turns": 30,
                   "skip_tech_lead": False},
        "STANDARD": {"planner_model": "claude-opus-4-6", "planner_max_turns": 50,
                     "tech_lead_model": "claude-opus-4-6", "tech_lead_max_turns": 100,
                     "worker_model": "claude-opus-4-6", "worker_max_turns": 50,
                     "skip_tech_lead": False},
        "CRITICAL": {"planner_model": "claude-opus-4-6", "planner_max_turns": 80,
                     "tech_lead_model": "claude-opus-4-6", "tech_lead_max_turns": 150,
                     "worker_model": "claude-opus-4-6", "worker_max_turns": 80,
                     "skip_tech_lead": False},
    })

    def apply_complexity_profile(self, complexity: str) -> None:
        """Mutate config fields based on the complexity profile."""
        profile_dict = self.complexity_profiles.get(complexity)
        if not profile_dict:
            return  # STANDARD defaults already set
        self.planner_model = profile_dict.get("planner_model", self.planner_model)
        self.planner_max_turns = profile_dict.get("planner_max_turns", self.planner_max_turns)
        self.tech_lead_model = profile_dict.get("tech_lead_model", self.tech_lead_model)
        self.max_tech_lead_turns = profile_dict.get("tech_lead_max_turns", self.max_tech_lead_turns)
        self.worker_model = profile_dict.get("worker_model", self.worker_model)
        self.max_worker_turns = profile_dict.get("worker_max_turns", self.max_worker_turns)
        self.skip_tech_lead = profile_dict.get("skip_tech_lead", False)

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
    return {"ANTHROPIC_API_KEY": "", "CLAUDECODE": "", "GOLEM_SDK_SESSION": "1"}


def resolve_agent_options(
    config: GolemConfig,
    role: str,
    golem_mcp: object,
    golem_mcp_name: str = "golem",
) -> tuple[list[str], dict[str, object]]:
    """Return (setting_sources, mcp_servers) for the given agent role."""
    sources = config.agent_setting_sources.get(role, config.setting_sources)
    mcps: dict[str, object] = {golem_mcp_name: golem_mcp}
    mcps.update(config.extra_mcp_servers.get(role, {}))
    return sources, mcps


def run_preflight_checks(
    config: GolemConfig,
    project_root: Path,
) -> tuple[list[str], list[str], list[str]]:
    """Run pre-flight pitfall detection. Returns (errors, warnings, infos)."""
    import os
    import shutil
    import subprocess

    errors: list[str] = []
    warnings_list: list[str] = []
    infos: list[str] = []

    # MCP name collision with golem built-ins
    builtin_names = {"golem", "golem-writer", "golem-qa"}
    for role, servers in config.extra_mcp_servers.items():
        for name in servers:
            if name in builtin_names:
                errors.append(
                    f"extra_mcp_servers[{role}].{name} collides with golem built-in MCP name"
                )

    # Stdio MCP command not found on PATH
    for role, servers in config.extra_mcp_servers.items():
        for name, srv in servers.items():
            if isinstance(srv, dict) and "command" in srv:
                cmd = srv["command"]
                if not shutil.which(cmd):
                    errors.append(f"extra_mcp_servers[{role}].{name}: command {cmd!r} not found on PATH")

    # CLAUDECODE env var (running inside Claude Code)
    if os.environ.get("CLAUDECODE"):
        errors.append("CLAUDECODE env var is set -- running inside Claude Code session")

    # ANTHROPIC_API_KEY set (overrides OAuth)
    if os.environ.get("ANTHROPIC_API_KEY"):
        warnings_list.append("ANTHROPIC_API_KEY env var is set -- may override OAuth")

    # "user" in setting sources
    all_sources = list(config.setting_sources)
    for role_sources in config.agent_setting_sources.values():
        all_sources.extend(role_sources)
    if "user" in all_sources:
        warnings_list.append("'user' in setting sources -- user-level plugins/hooks may interfere")

    # claude-mem detection — warn if any role with "user" sources would load it
    if "user" in all_sources:
        user_settings_path = Path.home() / ".claude" / "settings.json"
        if user_settings_path.exists():
            try:
                user_data = json.loads(user_settings_path.read_text(encoding="utf-8"))
                enabled = user_data.get("enabledPlugins", {})
                for plugin_key, val in enabled.items():
                    if "claude-mem" in plugin_key and val is True:
                        warnings_list.append(
                            f"{plugin_key} is enabled at user level -- SessionEnd hook may interfere"
                        )
                        break
            except Exception:
                pass

    # No .claude/settings.json in project
    if not (project_root / ".claude" / "settings.json").exists():
        if "project" in config.setting_sources:
            infos.append("No .claude/settings.json found in project root")

    # .env files present in project root
    env_files = [f.name for f in project_root.glob(".env*") if f.is_file()]
    if env_files:
        infos.append(f".env files present in project root: {', '.join(sorted(env_files))}")

    # Dirty git working tree
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=project_root, capture_output=True, text=True, timeout=10,
        )
        if result.stdout.strip():
            warnings_list.append("Dirty git working tree (uncommitted changes)")
    except (subprocess.SubprocessError, FileNotFoundError):
        pass

    # Existing golem/* branches
    try:
        result = subprocess.run(
            ["git", "branch", "--list", "golem/*"],
            cwd=project_root, capture_output=True, text=True, timeout=10,
        )
        if result.stdout.strip():
            warnings_list.append("Existing golem/* branches from previous run")
    except (subprocess.SubprocessError, FileNotFoundError):
        pass

    return errors, warnings_list, infos


def resolve_plugins_for_role(
    config: GolemConfig,
    role: str,
    project_root: Path,
) -> tuple[list[str], list[str]]:
    """Return (project_plugins, user_plugins) that would load for this role."""
    sources = config.agent_setting_sources.get(role, config.setting_sources)
    project_plugins: list[str] = []
    user_plugins: list[str] = []

    if "project" in sources:
        settings_path = project_root / ".claude" / "settings.json"
        if settings_path.exists():
            try:
                data = json.loads(settings_path.read_text(encoding="utf-8"))
                enabled = data.get("enabledPlugins", {})
                project_plugins = [k for k, v in enabled.items() if v is True]
            except Exception:
                pass

    if "user" in sources:
        user_settings = Path.home() / ".claude" / "settings.json"
        if user_settings.exists():
            try:
                data = json.loads(user_settings.read_text(encoding="utf-8"))
                enabled = data.get("enabledPlugins", {})
                user_plugins = [k for k, v in enabled.items() if v is True]
            except Exception:
                pass

    return project_plugins, user_plugins


_EPHEMERAL_FIELDS = {"infrastructure_checks"}


def save_config(config: GolemConfig, golem_dir: Path) -> None:
    golem_dir.mkdir(parents=True, exist_ok=True)
    data = {k: v for k, v in asdict(config).items() if k not in _EPHEMERAL_FIELDS}
    with open(golem_dir / "config.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
