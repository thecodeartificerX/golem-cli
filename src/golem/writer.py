"""Backward-compatibility shim — all logic now lives in golem.junior_dev."""
from __future__ import annotations

from golem.junior_dev import (  # noqa: F401
    JuniorDevResult,
    WriterResult,
    _build_worktree_isolation_warning,
    _get_rework_info,
    build_writer_prompt,
    spawn_junior_dev,
    spawn_writer_pair,
)

__all__ = [
    "JuniorDevResult",
    "WriterResult",
    "_build_worktree_isolation_warning",
    "_get_rework_info",
    "build_writer_prompt",
    "spawn_junior_dev",
    "spawn_writer_pair",
]
