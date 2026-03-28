from __future__ import annotations

from pathlib import Path

from golem.session import (
    SessionMetadata,
    create_session_dir,
    delete_session_dir,
    generate_session_id,
    read_session,
    write_session,
)

# Valid status transitions: from_status -> allowed next statuses
_VALID_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"running", "failed"},
    "running": {"awaiting_merge", "failed", "paused"},
    "paused": {"running", "failed"},
    "awaiting_merge": {"pr_open", "conflict", "failed"},
    "pr_open": {"merged", "conflict", "failed"},
    "conflict": {"awaiting_merge", "failed"},
    "merged": {"archived"},
    "archived": set(),
    "failed": set(),
}


def test_generate_session_id_basic(tmp_path: Path) -> None:
    """Slug from a simple spec filename."""
    spec = tmp_path / "auth-flow.md"
    spec.touch()
    sessions_dir = tmp_path / "sessions"
    result = generate_session_id(spec, sessions_dir)
    assert result == "auth-flow-1"


def test_generate_session_id_increment(tmp_path: Path) -> None:
    """Collision avoidance: existing session dirs bump the suffix."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "my-spec-1").mkdir()
    (sessions_dir / "my-spec-2").mkdir()
    spec = tmp_path / "my-spec.md"
    spec.touch()
    result = generate_session_id(spec, sessions_dir)
    assert result == "my-spec-3"


def test_generate_session_id_special_chars(tmp_path: Path) -> None:
    """Non-alphanumeric characters are stripped from the slug."""
    spec = tmp_path / "My Spec (v2)!.md"
    spec.touch()
    sessions_dir = tmp_path / "sessions"
    result = generate_session_id(spec, sessions_dir)
    assert "(" not in result
    assert ")" not in result
    assert "!" not in result
    assert result.startswith("my-spec-v2")


def test_generate_session_id_long_name(tmp_path: Path) -> None:
    """Long spec stems are truncated to 40 characters before the suffix."""
    long_name = "a" * 80
    spec = tmp_path / f"{long_name}.md"
    spec.touch()
    sessions_dir = tmp_path / "sessions"
    result = generate_session_id(spec, sessions_dir)
    slug_part = result.rsplit("-", 1)[0]
    assert len(slug_part) <= 40


def test_session_metadata_roundtrip(tmp_path: Path) -> None:
    """Write then read a SessionMetadata preserves all fields."""
    meta = SessionMetadata(
        id="auth-flow-1",
        spec_path="specs/auth-flow.md",
        status="running",
        complexity="STANDARD",
        pid=12345,
        cost_usd=1.24,
    )
    write_session(tmp_path, meta)
    loaded = read_session(tmp_path)
    assert loaded.id == "auth-flow-1"
    assert loaded.spec_path == "specs/auth-flow.md"
    assert loaded.status == "running"
    assert loaded.complexity == "STANDARD"
    assert loaded.pid == 12345
    assert loaded.cost_usd == 1.24


def test_create_session_dir_structure(tmp_path: Path) -> None:
    """create_session_dir creates all expected subdirectories."""
    sessions_dir = tmp_path / "sessions"
    spec = tmp_path / "test-spec.md"
    spec.write_text("# Test\n", encoding="utf-8")
    session_dir = create_session_dir(sessions_dir, "test-spec-1", spec)
    for subdir in ("tickets", "plans", "research", "references", "reports", "worktrees"):
        assert (session_dir / subdir).is_dir(), f"Missing subdir: {subdir}"
    assert (session_dir / "session.json").exists()


def test_create_session_dir_spec_copy(tmp_path: Path) -> None:
    """create_session_dir copies the spec as spec.md (immutable copy)."""
    sessions_dir = tmp_path / "sessions"
    spec = tmp_path / "my-feature.md"
    spec.write_text("# Feature\n\nSome content.", encoding="utf-8")
    session_dir = create_session_dir(sessions_dir, "my-feature-1", spec)
    copied = session_dir / "spec.md"
    assert copied.exists()
    assert copied.read_text(encoding="utf-8") == "# Feature\n\nSome content."


def test_status_transitions() -> None:
    """Verify the transition map covers all statuses and is internally consistent."""
    all_statuses = set(_VALID_TRANSITIONS.keys())
    assert _VALID_TRANSITIONS["archived"] == set()
    assert _VALID_TRANSITIONS["failed"] == set()
    for src, targets in _VALID_TRANSITIONS.items():
        for t in targets:
            assert t in all_statuses, f"Transition {src}->{t} targets unknown status"
    assert "running" in _VALID_TRANSITIONS["pending"]


def test_delete_session_dir(tmp_path: Path) -> None:
    """delete_session_dir removes the session directory."""
    sessions_dir = tmp_path / "sessions"
    spec = tmp_path / "spec.md"
    spec.write_text("# Test\n", encoding="utf-8")
    session_dir = create_session_dir(sessions_dir, "del-1", spec)
    assert session_dir.exists()

    result = delete_session_dir(sessions_dir, "del-1")
    assert result is True
    assert not session_dir.exists()


def test_delete_session_dir_nonexistent(tmp_path: Path) -> None:
    """delete_session_dir returns False for nonexistent session."""
    result = delete_session_dir(tmp_path, "nope")
    assert result is False
