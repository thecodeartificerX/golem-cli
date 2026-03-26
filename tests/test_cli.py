from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from click.exceptions import Exit as ClickExit

from golem.cli import _detect_infrastructure_checks, _validate_spec


def test_validate_spec_nonexistent_exits() -> None:
    with pytest.raises(ClickExit):
        _validate_spec(Path("/nonexistent/spec.md"))


def test_validate_spec_wrong_extension_exits() -> None:
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
        f.write(b"some content here with enough length to pass size check")
    with pytest.raises(ClickExit):
        _validate_spec(Path(f.name))


def test_validate_spec_empty_exits() -> None:
    with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w", encoding="utf-8") as f:
        f.write("")
    with pytest.raises(ClickExit):
        _validate_spec(Path(f.name))


def test_validate_spec_valid_passes() -> None:
    with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w", encoding="utf-8") as f:
        f.write("# My Spec\n\n## Task 1\n\nDo the thing.\n\n## Task 2\n\nDo the other thing.\n")
    # Should not raise
    _validate_spec(Path(f.name))


def test_validate_spec_short_warns(capsys: pytest.CaptureFixture[str]) -> None:
    with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w", encoding="utf-8") as f:
        f.write("# Short\nTiny spec.")  # 19 chars, < 50
    # Should not raise, just warn
    _validate_spec(Path(f.name))
    captured = capsys.readouterr()
    assert "very short" in captured.out


def test_validate_spec_no_structure_warns(capsys: pytest.CaptureFixture[str]) -> None:
    with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w", encoding="utf-8") as f:
        f.write("This is just a paragraph of text without any headings or task markers at all and it is long enough.")
    _validate_spec(Path(f.name))
    captured = capsys.readouterr()
    assert "no headings" in captured.out


def test_detect_infrastructure_checks_finds_ruff() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        pyproject = Path(tmpdir) / "pyproject.toml"
        pyproject.write_text("[tool.ruff]\nline-length = 120\n", encoding="utf-8")
        checks = _detect_infrastructure_checks(Path(tmpdir))
        assert "ruff check ." in checks


def test_detect_infrastructure_checks_finds_npm_lint() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        pkg = Path(tmpdir) / "package.json"
        pkg.write_text(json.dumps({"scripts": {"lint": "eslint ."}}), encoding="utf-8")
        checks = _detect_infrastructure_checks(Path(tmpdir))
        assert "npm run lint" in checks


def test_detect_infrastructure_checks_finds_tsc() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tsconfig = Path(tmpdir) / "tsconfig.json"
        tsconfig.write_text("{}", encoding="utf-8")
        checks = _detect_infrastructure_checks(Path(tmpdir))
        assert "tsc --noEmit" in checks


def test_detect_infrastructure_checks_empty_project() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        checks = _detect_infrastructure_checks(Path(tmpdir))
        assert checks == []
