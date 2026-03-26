from __future__ import annotations

from golem.version import get_version_info


def test_get_version_info_has_all_keys() -> None:
    info = get_version_info()
    assert "version" in info
    assert "python" in info
    assert "platform" in info
    assert "architecture" in info


def test_get_version_info_architecture_is_v2() -> None:
    info = get_version_info()
    assert "v2" in info["architecture"]


def test_get_version_info_python_version_format() -> None:
    info = get_version_info()
    parts = info["python"].split(".")
    assert len(parts) == 3  # major.minor.patch
