from __future__ import annotations

import dataclasses
import json
import tempfile
from pathlib import Path

from golem.config import GolemConfig, load_config, save_config, sdk_env


def test_golem_config_defaults() -> None:
    config = GolemConfig()
    assert config.max_parallel == 3
    assert config.max_retries == 2
    assert config.planner_model == "claude-opus-4-6"
    assert config.worker_model == "claude-opus-4-6"
    assert config.validator_model == "claude-sonnet-4-6"
    assert config.tech_lead_model == "claude-opus-4-6"
    assert config.max_worker_turns == 50
    assert config.pr_target == "main"


def test_default_setting_sources() -> None:
    """GolemConfig() must default setting_sources to ["project"] (no user hooks in SDK sessions)."""
    config = GolemConfig()
    assert config.setting_sources == ["project"]


def test_load_config_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        golem_dir = Path(tmpdir)
        config = GolemConfig(
            max_parallel=5, planner_model="claude-haiku-4-5-20251001", tech_lead_model="claude-opus-4-6"
        )
        save_config(config, golem_dir)
        loaded = load_config(golem_dir)
        assert loaded.max_parallel == 5
        assert loaded.planner_model == "claude-haiku-4-5-20251001"
        assert loaded.tech_lead_model == "claude-opus-4-6"


def test_load_config_overrides_setting_sources() -> None:
    """load_config must respect setting_sources from config.json when present."""
    with tempfile.TemporaryDirectory() as tmpdir:
        golem_dir = Path(tmpdir)
        config_data = {"setting_sources": ["project"]}
        config_path = golem_dir / "config.json"
        config_path.write_text(json.dumps(config_data), encoding="utf-8")

        result = load_config(golem_dir)

        assert result.setting_sources == ["project"]


def test_load_config_preserves_default_setting_sources() -> None:
    """load_config must preserve the default setting_sources when key is absent from config.json."""
    with tempfile.TemporaryDirectory() as tmpdir:
        golem_dir = Path(tmpdir)
        config_data = {"max_parallel": 5}
        config_path = golem_dir / "config.json"
        config_path.write_text(json.dumps(config_data), encoding="utf-8")

        result = load_config(golem_dir)

        assert result.setting_sources == ["project"]
        assert result.max_parallel == 5


def test_save_config_includes_setting_sources() -> None:
    """save_config must write setting_sources to config.json so it survives a round-trip."""
    with tempfile.TemporaryDirectory() as tmpdir:
        golem_dir = Path(tmpdir)
        config = GolemConfig(setting_sources=["user", "local"])

        save_config(config, golem_dir)

        config_path = golem_dir / "config.json"
        assert config_path.exists()
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        assert "setting_sources" in raw
        assert raw["setting_sources"] == ["user", "local"]


def test_sdk_env_clears_api_key() -> None:
    """sdk_env must return a dict that clears ANTHROPIC_API_KEY to prevent OAuth bypass."""
    env = sdk_env()
    assert isinstance(env, dict)
    assert env.get("ANTHROPIC_API_KEY") == ""


def test_load_config_no_file_returns_defaults() -> None:
    """load_config must return a default GolemConfig when config.json does not exist."""
    with tempfile.TemporaryDirectory() as tmpdir:
        golem_dir = Path(tmpdir)
        # Deliberately do NOT write a config.json

        result = load_config(golem_dir)

        assert result == GolemConfig()
        assert result.setting_sources == ["project"]


def test_load_config_empty_setting_sources() -> None:
    """load_config must accept an empty list for setting_sources (clean session override)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        golem_dir = Path(tmpdir)
        config_data = {"setting_sources": []}
        config_path = golem_dir / "config.json"
        config_path.write_text(json.dumps(config_data), encoding="utf-8")

        result = load_config(golem_dir)

        assert result.setting_sources == []


def test_save_and_load_roundtrip_setting_sources() -> None:
    """setting_sources must survive a full save -> load round-trip unchanged."""
    with tempfile.TemporaryDirectory() as tmpdir:
        golem_dir = Path(tmpdir)
        original = GolemConfig(setting_sources=["project", "local"])

        save_config(original, golem_dir)
        restored = load_config(golem_dir)

        assert restored.setting_sources == ["project", "local"]


def test_validate_defaults_no_warnings() -> None:
    config = GolemConfig()
    assert config.validate() == []


def test_validate_unknown_model_warns() -> None:
    config = GolemConfig(planner_model="gpt-4o")
    warnings = config.validate()
    assert any("planner_model" in w for w in warnings)
    assert any("gpt-4o" in w for w in warnings)


def test_validate_bad_max_parallel_warns() -> None:
    config = GolemConfig(max_parallel=0)
    warnings = config.validate()
    assert any("max_parallel" in w for w in warnings)


def test_validate_negative_max_retries_warns() -> None:
    config = GolemConfig(max_retries=-1)
    warnings = config.validate()
    assert any("max_retries" in w for w in warnings)


def test_validate_bad_max_worker_turns_warns() -> None:
    config = GolemConfig(max_worker_turns=0)
    warnings = config.validate()
    assert any("max_worker_turns" in w for w in warnings)


def test_validate_unknown_setting_source_warns() -> None:
    config = GolemConfig(setting_sources=["project", "typo"])
    warnings = config.validate()
    assert any("typo" in w for w in warnings)


def test_validate_valid_setting_sources_no_unknown_source_warnings() -> None:
    config = GolemConfig(setting_sources=["project", "user"])
    warnings = config.validate()
    assert not any("unknown setting_source" in w.lower() for w in warnings)


def test_validate_known_models_no_warnings() -> None:
    config = GolemConfig(
        planner_model="claude-opus-4-6",
        worker_model="claude-sonnet-4-6",
        validator_model="claude-haiku-4-5-20251001",
        tech_lead_model="claude-opus-4-6",
    )
    assert config.validate() == []


def test_save_config_sorted_keys() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        golem_dir = Path(tmpdir)
        save_config(GolemConfig(), golem_dir)
        raw = (golem_dir / "config.json").read_text(encoding="utf-8")
        data = json.loads(raw)
        keys = list(data.keys())
        assert keys == sorted(keys)


def test_load_config_ignores_unknown_fields() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        golem_dir = Path(tmpdir)
        config_path = golem_dir / "config.json"
        config_path.write_text(
            json.dumps({"max_parallel": 2, "unknown_future_field": "some_value"}),
            encoding="utf-8",
        )
        loaded = load_config(golem_dir)
        assert loaded.max_parallel == 2
        assert not hasattr(loaded, "unknown_future_field")


# --- resolve_agent_options tests ---


def test_resolve_agent_options_defaults() -> None:
    """resolve_agent_options falls back to base setting_sources when role not in overrides."""
    from golem.config import resolve_agent_options

    config = GolemConfig()
    mock_mcp = object()
    sources, mcps = resolve_agent_options(config, "planner", mock_mcp)
    assert sources == ["project"]
    assert "golem" in mcps
    assert mcps["golem"] is mock_mcp


def test_resolve_agent_options_with_role_override() -> None:
    """resolve_agent_options uses role-specific sources when configured."""
    from golem.config import resolve_agent_options

    config = GolemConfig(agent_setting_sources={"writer": ["project", "user"]})
    mock_mcp = object()
    sources, mcps = resolve_agent_options(config, "writer", mock_mcp)
    assert sources == ["project", "user"]


def test_resolve_agent_options_with_extra_mcps() -> None:
    """resolve_agent_options merges extra MCPs with golem MCP."""
    from golem.config import resolve_agent_options

    config = GolemConfig(
        extra_mcp_servers={
            "planner": {"context7": {"command": "npx", "args": ["-y", "ctx7"]}},
        },
    )
    mock_mcp = object()
    sources, mcps = resolve_agent_options(config, "planner", mock_mcp)
    assert "golem" in mcps
    assert "context7" in mcps
    assert mcps["context7"]["command"] == "npx"


def test_resolve_agent_options_custom_mcp_name() -> None:
    """resolve_agent_options uses custom golem_mcp_name for writer."""
    from golem.config import resolve_agent_options

    config = GolemConfig()
    mock_mcp = object()
    sources, mcps = resolve_agent_options(
        config, "writer", mock_mcp, golem_mcp_name="golem-writer",
    )
    assert "golem-writer" in mcps
    assert "golem" not in mcps


# --- New field defaults ---


def test_golem_config_new_field_defaults() -> None:
    config = GolemConfig()
    assert config.agent_setting_sources == {}
    assert config.extra_mcp_servers == {}


# ---------------------------------------------------------------------------
# Spec 07: apply_complexity_profile new fields
# ---------------------------------------------------------------------------


def test_apply_complexity_profile_sets_skip_research_trivial_simple() -> None:
    """TRIVIAL and SIMPLE profiles set skip_research=True."""
    for tier in ("TRIVIAL", "SIMPLE"):
        config = GolemConfig()
        config.apply_complexity_profile(tier)
        assert config.skip_research is True, f"{tier} should set skip_research=True"


def test_apply_complexity_profile_sets_skip_research_standard_critical() -> None:
    """STANDARD and CRITICAL profiles set skip_research=False."""
    for tier in ("STANDARD", "CRITICAL"):
        config = GolemConfig()
        config.apply_complexity_profile(tier)
        assert config.skip_research is False, f"{tier} should set skip_research=False"


def test_apply_complexity_profile_sets_max_writer_retries_all_tiers() -> None:
    """Each tier sets max_writer_retries to the expected value."""
    expected = {"TRIVIAL": 1, "SIMPLE": 2, "STANDARD": 3, "CRITICAL": 5}
    for tier, retries in expected.items():
        config = GolemConfig()
        config.apply_complexity_profile(tier)
        assert config.max_writer_retries == retries, f"{tier}: expected {retries}, got {config.max_writer_retries}"


def test_apply_complexity_profile_sets_qa_depth() -> None:
    """TRIVIAL -> minimal, SIMPLE/STANDARD -> standard, CRITICAL -> strict."""
    expected = {"TRIVIAL": "minimal", "SIMPLE": "standard", "STANDARD": "standard", "CRITICAL": "strict"}
    for tier, depth in expected.items():
        config = GolemConfig()
        config.apply_complexity_profile(tier)
        assert config.qa_depth == depth, f"{tier}: expected qa_depth={depth!r}, got {config.qa_depth!r}"


def test_apply_complexity_profile_sets_self_critique_enabled() -> None:
    """Only CRITICAL enables self_critique_enabled."""
    for tier in ("TRIVIAL", "SIMPLE", "STANDARD"):
        config = GolemConfig()
        config.apply_complexity_profile(tier)
        assert config.self_critique_enabled is False, f"{tier} should not enable self_critique"

    config = GolemConfig()
    config.apply_complexity_profile("CRITICAL")
    assert config.self_critique_enabled is True


def test_apply_complexity_profile_sets_max_parallel_writers() -> None:
    """Each tier sets max_parallel_writers to the expected value."""
    expected = {"TRIVIAL": 1, "SIMPLE": 2, "STANDARD": 3, "CRITICAL": 2}
    for tier, parallel in expected.items():
        config = GolemConfig()
        config.apply_complexity_profile(tier)
        assert config.max_parallel_writers == parallel, f"{tier}: expected {parallel}, got {config.max_parallel_writers}"


def test_validate_rejects_unknown_qa_depth() -> None:
    """qa_depth='turbo' produces a validation warning."""
    config = GolemConfig(qa_depth="turbo")
    warnings = config.validate()
    assert any("qa_depth" in w for w in warnings)


def test_validate_rejects_max_writer_retries_less_than_1() -> None:
    """max_writer_retries=0 produces a validation warning."""
    config = GolemConfig(max_writer_retries=0)
    warnings = config.validate()
    assert any("max_writer_retries" in w for w in warnings)


def test_validate_rejects_max_parallel_writers_less_than_1() -> None:
    """max_parallel_writers=0 produces a validation warning."""
    config = GolemConfig(max_parallel_writers=0)
    warnings = config.validate()
    assert any("max_parallel_writers" in w for w in warnings)


def test_new_fields_serialise_round_trip(tmp_path: "Path") -> None:
    """New tier-gating fields survive a save -> load round-trip."""
    config = GolemConfig(
        qa_depth="strict",
        max_writer_retries=5,
        skip_research=True,
        self_critique_enabled=True,
        max_parallel_writers=2,
    )
    save_config(config, tmp_path)
    loaded = load_config(tmp_path)
    assert loaded.qa_depth == "strict"
    assert loaded.max_writer_retries == 5
    assert loaded.skip_research is True
    assert loaded.self_critique_enabled is True
    assert loaded.max_parallel_writers == 2


# --- Roundtrip with new fields ---


def test_save_load_roundtrip_new_fields(tmp_path: Path) -> None:
    config = GolemConfig(
        agent_setting_sources={"writer": ["project", "user"]},
        extra_mcp_servers={"planner": {"ctx7": {"command": "npx"}}},
    )
    save_config(config, tmp_path)
    loaded = load_config(tmp_path)
    assert loaded.agent_setting_sources == {"writer": ["project", "user"]}
    assert loaded.extra_mcp_servers == {"planner": {"ctx7": {"command": "npx"}}}


# --- sdk_env CLAUDECODE fix ---


def test_sdk_env_clears_claudecode() -> None:
    env = sdk_env()
    assert env.get("CLAUDECODE") == ""


# --- Validation new fields ---


def test_validate_unknown_role_in_agent_setting_sources() -> None:
    config = GolemConfig(agent_setting_sources={"unknown_role": ["project"]})
    warnings = config.validate()
    assert any("unknown_role" in w for w in warnings)


def test_validate_unknown_role_in_extra_mcp_servers() -> None:
    config = GolemConfig(
        extra_mcp_servers={"bad_role": {"ctx7": {"command": "npx"}}},
    )
    warnings = config.validate()
    assert any("bad_role" in w for w in warnings)


def test_validate_mcp_missing_command_or_url() -> None:
    config = GolemConfig(
        extra_mcp_servers={"planner": {"bad": {"args": ["x"]}}},
    )
    warnings = config.validate()
    assert any("command" in w and "url" in w for w in warnings)


def test_validate_user_in_sources_warns() -> None:
    config = GolemConfig(setting_sources=["project", "user"])
    warnings = config.validate()
    assert any("user" in w.lower() and "plugin" in w.lower() for w in warnings)


def test_validate_user_in_agent_sources_warns() -> None:
    config = GolemConfig(agent_setting_sources={"writer": ["project", "user"]})
    warnings = config.validate()
    assert any("user" in w.lower() for w in warnings)


def test_resolve_agent_options_empty_list_override() -> None:
    """Empty list [] is a valid override meaning 'no setting sources' — not a fallback."""
    from golem.config import resolve_agent_options

    config = GolemConfig(
        setting_sources=["project"],
        agent_setting_sources={"writer": []},
    )
    mock_mcp = object()
    sources, mcps = resolve_agent_options(config, "writer", mock_mcp)
    assert sources == []


def test_apply_complexity_profile_trivial() -> None:
    cfg = GolemConfig()
    cfg.apply_complexity_profile("TRIVIAL")
    assert cfg.planner_max_turns == 10
    assert "haiku" in cfg.planner_model
    assert cfg.skip_tech_lead is True


def test_apply_complexity_profile_unknown() -> None:
    cfg = GolemConfig()
    cfg.apply_complexity_profile("UNKNOWN")
    assert cfg.planner_max_turns == 50
    assert cfg.skip_tech_lead is False


def test_complexity_profiles_roundtrip() -> None:
    cfg = GolemConfig()
    as_dict = dataclasses.asdict(cfg)
    assert set(as_dict["complexity_profiles"].keys()) == {"TRIVIAL", "SIMPLE", "STANDARD", "CRITICAL"}


def test_dispatch_jitter_max_default() -> None:
    config = GolemConfig()
    assert config.dispatch_jitter_max == 5.0


def test_dispatch_jitter_max_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        golem_dir = Path(tmpdir)
        config = GolemConfig(dispatch_jitter_max=3.5)
        save_config(config, golem_dir)
        loaded = load_config(golem_dir)
        assert loaded.dispatch_jitter_max == 3.5


def test_resolve_plugins_for_role_reads_project(tmp_path: Path) -> None:
    """resolve_plugins_for_role reads project .claude/settings.json."""
    from golem.config import resolve_plugins_for_role

    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text(
        '{"enabledPlugins": {"frontend-design@official": true, "disabled@x": false}}',
        encoding="utf-8",
    )
    config = GolemConfig(setting_sources=["project"])
    proj, usr = resolve_plugins_for_role(config, "writer", tmp_path)
    assert "frontend-design@official" in proj
    assert "disabled@x" not in proj


def test_session_id_default() -> None:
    """GolemConfig.session_id defaults to empty string."""
    config = GolemConfig()
    assert config.session_id == ""


def test_branch_prefix_default() -> None:
    """GolemConfig.branch_prefix defaults to 'golem'."""
    config = GolemConfig()
    assert config.branch_prefix == "golem"


def test_merge_auto_rebase_default() -> None:
    """GolemConfig.merge_auto_rebase defaults to True."""
    config = GolemConfig()
    assert config.merge_auto_rebase is True


def test_archive_delay_minutes_default() -> None:
    """GolemConfig.archive_delay_minutes defaults to 30."""
    config = GolemConfig()
    assert config.archive_delay_minutes == 30


def test_session_fields_roundtrip() -> None:
    """Session-related config fields survive save/load cycle."""
    with tempfile.TemporaryDirectory() as tmpdir:
        golem_dir = Path(tmpdir)
        config = GolemConfig(session_id="auth-flow-1", branch_prefix="golem/auth-flow-1")
        save_config(config, golem_dir)
        loaded = load_config(golem_dir)
        assert loaded.session_id == "auth-flow-1"
        assert loaded.branch_prefix == "golem/auth-flow-1"


# ---------------------------------------------------------------------------
# Phase 3 budget fields
# ---------------------------------------------------------------------------


def test_budget_field_defaults() -> None:
    """Budget fields must have the correct default values."""
    config = GolemConfig()
    assert config.planner_budget_usd == 2.0
    assert config.tech_lead_budget_usd == 5.0
    assert config.worker_budget_usd == 1.0
    assert config.fallback_model == "claude-sonnet-4-6"


def test_apply_complexity_profile_budget_trivial() -> None:
    """TRIVIAL profile sets minimal budget caps."""
    config = GolemConfig()
    config.apply_complexity_profile("TRIVIAL")
    assert config.planner_budget_usd == 0.10
    assert config.tech_lead_budget_usd == 0.0
    assert config.worker_budget_usd == 0.25


def test_apply_complexity_profile_budget_simple() -> None:
    """SIMPLE profile sets moderate budget caps."""
    config = GolemConfig()
    config.apply_complexity_profile("SIMPLE")
    assert config.planner_budget_usd == 0.50
    assert config.tech_lead_budget_usd == 1.0
    assert config.worker_budget_usd == 0.50


def test_apply_complexity_profile_budget_standard() -> None:
    """STANDARD profile sets default budget caps (matching GolemConfig defaults)."""
    config = GolemConfig()
    config.apply_complexity_profile("STANDARD")
    assert config.planner_budget_usd == 2.0
    assert config.tech_lead_budget_usd == 5.0
    assert config.worker_budget_usd == 1.0


def test_apply_complexity_profile_budget_critical() -> None:
    """CRITICAL profile sets elevated budget caps."""
    config = GolemConfig()
    config.apply_complexity_profile("CRITICAL")
    assert config.planner_budget_usd == 5.0
    assert config.tech_lead_budget_usd == 10.0
    assert config.worker_budget_usd == 2.0


def test_budget_fields_roundtrip(tmp_path: Path) -> None:
    """Budget fields survive a full save -> load round-trip."""
    config = GolemConfig(planner_budget_usd=3.5, tech_lead_budget_usd=7.0, worker_budget_usd=1.5)
    save_config(config, tmp_path)
    loaded = load_config(tmp_path)
    assert loaded.planner_budget_usd == 3.5
    assert loaded.tech_lead_budget_usd == 7.0
    assert loaded.worker_budget_usd == 1.5


def test_fallback_model_default() -> None:
    """fallback_model must default to claude-sonnet-4-6."""
    config = GolemConfig()
    assert config.fallback_model == "claude-sonnet-4-6"


def test_fallback_model_roundtrip(tmp_path: Path) -> None:
    """fallback_model survives a full save -> load round-trip."""
    config = GolemConfig(fallback_model="claude-haiku-4-5-20251001")
    save_config(config, tmp_path)
    loaded = load_config(tmp_path)
    assert loaded.fallback_model == "claude-haiku-4-5-20251001"


def test_sdk_env_with_session_id_and_golem_dir() -> None:
    """sdk_env with session_id and golem_dir includes those values in the returned dict."""
    env = sdk_env(session_id="test-123", golem_dir="/tmp/golem")
    assert env.get("GOLEM_SESSION_ID") == "test-123"
    assert env.get("GOLEM_DIR") == "/tmp/golem"
    assert env.get("ANTHROPIC_API_KEY") == ""
    assert env.get("GOLEM_SDK_SESSION") == "1"


def test_sdk_env_without_optional_args() -> None:
    """sdk_env() with no args does not include GOLEM_SESSION_ID or GOLEM_DIR."""
    env = sdk_env()
    assert "GOLEM_SESSION_ID" not in env
    assert "GOLEM_DIR" not in env


# ---------------------------------------------------------------------------
# Task 228: extra_qa_checks
# ---------------------------------------------------------------------------


def test_extra_qa_checks_default_empty() -> None:
    """extra_qa_checks must default to an empty list."""
    config = GolemConfig()
    assert config.extra_qa_checks == []


def test_extra_qa_checks_roundtrip(tmp_path: Path) -> None:
    """extra_qa_checks survives a full save -> load round-trip."""
    config = GolemConfig(extra_qa_checks=["make lint", "cargo clippy"])
    save_config(config, tmp_path)
    loaded = load_config(tmp_path)
    assert loaded.extra_qa_checks == ["make lint", "cargo clippy"]


def test_extra_qa_checks_empty_roundtrip(tmp_path: Path) -> None:
    """Empty extra_qa_checks survives round-trip."""
    config = GolemConfig(extra_qa_checks=[])
    save_config(config, tmp_path)
    loaded = load_config(tmp_path)
    assert loaded.extra_qa_checks == []


# ---------------------------------------------------------------------------
# Spec 6.1: WaveExecutor enabled by default
# ---------------------------------------------------------------------------


def test_orchestrator_enabled_default_true() -> None:
    """Default GolemConfig must have orchestrator_enabled=True."""
    config = GolemConfig()
    assert config.orchestrator_enabled is True


def test_orchestrator_enabled_critical_false() -> None:
    """CRITICAL tier keeps orchestrator_enabled=False for maximum Tech Lead flexibility."""
    config = GolemConfig()
    config.apply_complexity_profile("CRITICAL")
    assert config.orchestrator_enabled is False


def test_orchestrator_enabled_trivial_simple_standard_true() -> None:
    """TRIVIAL, SIMPLE, and STANDARD tiers all have orchestrator_enabled=True."""
    for tier in ("TRIVIAL", "SIMPLE", "STANDARD"):
        config = GolemConfig()
        config.apply_complexity_profile(tier)
        assert config.orchestrator_enabled is True, f"{tier} should have orchestrator_enabled=True"


def test_orchestrator_enabled_roundtrip(tmp_path: Path) -> None:
    """orchestrator_enabled survives a full save -> load round-trip."""
    config = GolemConfig(orchestrator_enabled=False)
    save_config(config, tmp_path)
    loaded = load_config(tmp_path)
    assert loaded.orchestrator_enabled is False


def test_max_writer_retries_valid_no_warning() -> None:
    """max_writer_retries >= 1 should not produce a warning."""
    config = GolemConfig(max_writer_retries=5)
    warnings = config.validate()
    assert not any("max_writer_retries" in w for w in warnings)
