from __future__ import annotations

import json
import tempfile
from pathlib import Path

from golem.config import GolemConfig, load_config, save_config, sdk_env


def test_default_setting_sources() -> None:
    """GolemConfig() must default setting_sources to ["project"] (no user hooks in SDK sessions)."""
    config = GolemConfig()
    assert config.setting_sources == ["project"]


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


def test_sdk_env_clears_anthropic_api_key() -> None:
    """sdk_env must return a dict that clears ANTHROPIC_API_KEY to prevent OAuth bypass."""
    env = sdk_env()
    assert "ANTHROPIC_API_KEY" in env
    assert env["ANTHROPIC_API_KEY"] == ""


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
    """setting_sources must survive a full save → load round-trip unchanged."""
    with tempfile.TemporaryDirectory() as tmpdir:
        golem_dir = Path(tmpdir)
        original = GolemConfig(setting_sources=["project", "local"])

        save_config(original, golem_dir)
        restored = load_config(golem_dir)

        assert restored.setting_sources == ["project", "local"]
