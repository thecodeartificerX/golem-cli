from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from golem.conductor import classify_spec, derive_agent_topology, predict_conflicts
from golem.config import GolemConfig, estimate_cost, run_environment_checks


def test_derive_topology_standard() -> None:
    """STANDARD config produces correct topology."""
    config = GolemConfig()
    topo = derive_agent_topology(config)
    assert "planner" in topo
    assert "tech_lead" in topo
    assert "junior_dev" in topo
    assert len(topo["planner"]["mcp_tools"]) == 8
    assert len(topo["junior_dev"]["mcp_tools"]) == 3
    assert topo["skip_tech_lead"] is False


def test_derive_topology_trivial() -> None:
    """TRIVIAL config has skip_tech_lead=True and haiku planner."""
    config = GolemConfig()
    config.skip_tech_lead = True
    config.planner_model = "claude-haiku-4-5"
    topo = derive_agent_topology(config)
    assert topo["skip_tech_lead"] is True
    assert topo["planner"]["model"] == "claude-haiku-4-5"


def test_derive_topology_critical() -> None:
    """CRITICAL config has opus everywhere with higher turn limits."""
    config = GolemConfig()
    config.planner_model = "claude-opus-4-6"
    config.tech_lead_model = "claude-opus-4-6"
    config.worker_model = "claude-opus-4-6"
    config.planner_max_turns = 100
    config.max_tech_lead_turns = 200
    topo = derive_agent_topology(config)
    assert topo["planner"]["model"] == "claude-opus-4-6"
    assert topo["tech_lead"]["model"] == "claude-opus-4-6"
    assert topo["planner"]["max_turns"] == 100
    assert topo["tech_lead"]["max_turns"] == 200


def test_predict_conflicts_overlap(tmp_path: Path) -> None:
    """Two specs referencing same file produce conflict entry."""
    spec_a = tmp_path / "spec-a.md"
    spec_b = tmp_path / "spec-b.md"
    spec_a.write_text("Modify `src/golem/server.py` and `tests/test_server.py`", encoding="utf-8")
    spec_b.write_text("Update `src/golem/server.py` for new feature", encoding="utf-8")
    conflicts = predict_conflicts([spec_a, spec_b])
    assert len(conflicts) >= 1
    file_names = [c["file"] for c in conflicts]
    assert "src/golem/server.py" in file_names


def test_predict_conflicts_no_overlap(tmp_path: Path) -> None:
    """Two specs with different files produce no conflicts."""
    spec_a = tmp_path / "spec-a.md"
    spec_b = tmp_path / "spec-b.md"
    spec_a.write_text("Modify `src/golem/planner.py`", encoding="utf-8")
    spec_b.write_text("Modify `src/golem/writer.py`", encoding="utf-8")
    conflicts = predict_conflicts([spec_a, spec_b])
    assert len(conflicts) == 0


def test_predict_conflicts_single_spec(tmp_path: Path) -> None:
    """Single spec produces no conflicts."""
    spec_a = tmp_path / "spec-a.md"
    spec_a.write_text("Modify `src/golem/server.py`", encoding="utf-8")
    conflicts = predict_conflicts([spec_a])
    assert len(conflicts) == 0


@pytest.mark.asyncio
async def test_environment_checks_all_pass(tmp_path: Path) -> None:
    """All environment checks pass when tools are present."""
    with patch("shutil.which", return_value="/usr/bin/tool"), \
         patch("subprocess.run") as mock_run, \
         patch("socket.socket"):
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = ""
        checks = await run_environment_checks(tmp_path)
    assert len(checks) == 5
    claude_check = next(c for c in checks if c["check"] == "claude CLI")
    assert claude_check["passed"] is True


@pytest.mark.asyncio
async def test_environment_checks_missing_rg(tmp_path: Path) -> None:
    """rg check fails when not on PATH."""
    def which_side_effect(name: str) -> str | None:
        if name == "rg":
            return None
        return "/usr/bin/" + name

    with patch("shutil.which", side_effect=which_side_effect), \
         patch("subprocess.run") as mock_run, \
         patch("socket.socket"):
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = ""
        checks = await run_environment_checks(tmp_path)
    rg_check = next(c for c in checks if c["check"] == "ripgrep (rg)")
    assert rg_check["passed"] is False


def test_cost_estimate_with_history(tmp_path: Path) -> None:
    """Historical AGENT_COST lines produce min/max estimates."""
    log_dir = tmp_path / "session1"
    log_dir.mkdir()
    log_file = log_dir / "progress.log"
    log_file.write_text(
        "[2026-03-28T00:00:00Z] AGENT_COST role=planner cost=$0.05 input_tokens=1000 output_tokens=500 cache_read=0 turns=10 duration=30s\n"
        "[2026-03-28T00:00:00Z] AGENT_COST role=planner cost=$0.08 input_tokens=2000 output_tokens=800 cache_read=0 turns=15 duration=45s\n"
        "[2026-03-28T00:00:00Z] AGENT_COST role=tech_lead cost=$0.10 input_tokens=5000 output_tokens=1000 cache_read=0 turns=20 duration=60s\n",
        encoding="utf-8",
    )
    config = GolemConfig()
    result = estimate_cost(config, history_dir=tmp_path)
    assert result["based_on"] == 3
    assert result["planner"]["min"] <= result["planner"]["max"]


def test_cost_estimate_no_history() -> None:
    """No history falls back to model-based estimates."""
    config = GolemConfig()
    result = estimate_cost(config, history_dir=None)
    assert result["based_on"] == 0
    assert "planner" in result
    assert "tech_lead" in result
    assert "total" in result
    assert result["total"]["min"] > 0


def test_preflight_endpoint_returns_full_analysis() -> None:
    """Preflight functions return expected structure."""
    config = GolemConfig()
    topo = derive_agent_topology(config)
    assert "planner" in topo
    assert "tech_lead" in topo
    assert "junior_dev" in topo
    est = estimate_cost(config)
    assert "total" in est
    assert "based_on" in est
