from __future__ import annotations

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
    assert config.max_validator_turns == 20
    assert config.auto_pr is True
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


def test_max_writer_retries_default() -> None:
    """GolemConfig should have a max_writer_retries field defaulting to 3."""
    config = GolemConfig()
    assert config.max_writer_retries == 3


def test_max_writer_retries_validates() -> None:
    """max_writer_retries < 1 should produce a validation warning."""
    config = GolemConfig(max_writer_retries=0)
    warnings = config.validate()
    assert any("max_writer_retries" in w for w in warnings)


def test_max_writer_retries_valid_no_warning() -> None:
    """max_writer_retries >= 1 should not produce a warning."""
    config = GolemConfig(max_writer_retries=5)
    warnings = config.validate()
    assert not any("max_writer_retries" in w for w in warnings)


def test_max_writer_retries_roundtrip() -> None:
    """max_writer_retries should survive save -> load roundtrip."""
    with tempfile.TemporaryDirectory() as tmpdir:
        golem_dir = Path(tmpdir)
        config = GolemConfig(max_writer_retries=5)
        save_config(config, golem_dir)
        loaded = load_config(golem_dir)
        assert loaded.max_writer_retries == 5
