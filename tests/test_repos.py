from __future__ import annotations

from pathlib import Path

import pytest

from golem.repos import Repo, RepoRegistry


@pytest.mark.asyncio
async def test_add_repo_derives_id_from_dir_name(tmp_path: Path) -> None:
    """ID is derived from the directory name."""
    repo_dir = tmp_path / "my-project"
    repo_dir.mkdir()

    registry = RepoRegistry(tmp_path / "repos.json")
    repo = await registry.add(str(repo_dir))

    assert repo.id == "my-project"
    assert repo.name == "my-project"
    assert repo.path == str(repo_dir.resolve())


@pytest.mark.asyncio
async def test_add_repo_with_custom_name(tmp_path: Path) -> None:
    """Custom name overrides the default directory name."""
    repo_dir = tmp_path / "golem-cli"
    repo_dir.mkdir()

    registry = RepoRegistry(tmp_path / "repos.json")
    repo = await registry.add(str(repo_dir), name="Golem CLI")

    assert repo.id == "golem-cli"
    assert repo.name == "Golem CLI"


@pytest.mark.asyncio
async def test_add_duplicate_path_is_idempotent(tmp_path: Path) -> None:
    """Adding the same path twice returns the existing entry."""
    repo_dir = tmp_path / "test-repo"
    repo_dir.mkdir()

    registry = RepoRegistry(tmp_path / "repos.json")
    first = await registry.add(str(repo_dir))
    second = await registry.add(str(repo_dir))

    assert first.id == second.id
    assert first.added_at == second.added_at

    repos = await registry.list_repos()
    assert len(repos) == 1


@pytest.mark.asyncio
async def test_remove_repo_by_id(tmp_path: Path) -> None:
    """Removing an existing repo by ID returns True and deletes the entry."""
    repo_dir = tmp_path / "remove-me"
    repo_dir.mkdir()

    registry = RepoRegistry(tmp_path / "repos.json")
    repo = await registry.add(str(repo_dir))

    result = await registry.remove(repo.id)

    assert result is True
    repos = await registry.list_repos()
    assert len(repos) == 0


@pytest.mark.asyncio
async def test_remove_nonexistent_repo_returns_false(tmp_path: Path) -> None:
    """Removing a repo that does not exist returns False."""
    registry = RepoRegistry(tmp_path / "repos.json")

    result = await registry.remove("does-not-exist")

    assert result is False


@pytest.mark.asyncio
async def test_list_repos_returns_all_entries(tmp_path: Path) -> None:
    """list_repos returns every registered repo."""
    dirs = ["alpha", "beta", "gamma"]
    for name in dirs:
        (tmp_path / name).mkdir()

    registry = RepoRegistry(tmp_path / "repos.json")
    for name in dirs:
        await registry.add(str(tmp_path / name))

    repos = await registry.list_repos()
    assert len(repos) == 3
    ids = {r.id for r in repos}
    assert ids == set(dirs)


@pytest.mark.asyncio
async def test_get_repo_by_id(tmp_path: Path) -> None:
    """get returns the matching repo when the ID exists."""
    repo_dir = tmp_path / "lookup-me"
    repo_dir.mkdir()

    registry = RepoRegistry(tmp_path / "repos.json")
    added = await registry.add(str(repo_dir))

    found = await registry.get(added.id)

    assert found is not None
    assert found.id == added.id
    assert found.path == added.path


@pytest.mark.asyncio
async def test_get_nonexistent_repo_returns_none(tmp_path: Path) -> None:
    """get returns None when the ID does not exist."""
    registry = RepoRegistry(tmp_path / "repos.json")

    result = await registry.get("ghost")

    assert result is None


@pytest.mark.asyncio
async def test_registry_persists_across_instances(tmp_path: Path) -> None:
    """Data written by one RepoRegistry instance is readable by a new instance."""
    repo_dir = tmp_path / "persistent-repo"
    repo_dir.mkdir()
    registry_path = tmp_path / "repos.json"

    registry_a = RepoRegistry(registry_path)
    await registry_a.add(str(repo_dir))

    registry_b = RepoRegistry(registry_path)
    repos = await registry_b.list_repos()

    assert len(repos) == 1
    assert repos[0].id == "persistent-repo"


@pytest.mark.asyncio
async def test_invalid_path_raises_value_error(tmp_path: Path) -> None:
    """Adding a path that does not exist raises ValueError."""
    registry = RepoRegistry(tmp_path / "repos.json")
    nonexistent = str(tmp_path / "does-not-exist")

    with pytest.raises(ValueError, match="does not exist or is not a directory"):
        await registry.add(nonexistent)


@pytest.mark.asyncio
async def test_added_at_is_iso8601_utc(tmp_path: Path) -> None:
    """added_at field is a valid ISO-8601 UTC timestamp."""
    from datetime import UTC, datetime

    repo_dir = tmp_path / "timestamped"
    repo_dir.mkdir()

    registry = RepoRegistry(tmp_path / "repos.json")
    repo = await registry.add(str(repo_dir))

    # Must parse without error
    parsed = datetime.fromisoformat(repo.added_at)
    assert parsed.tzinfo is not None
    assert parsed.tzinfo == UTC or parsed.utcoffset().total_seconds() == 0  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_repo_to_dict_and_from_dict_roundtrip(tmp_path: Path) -> None:
    """Repo serializes and deserializes without data loss."""
    repo_dir = tmp_path / "roundtrip"
    repo_dir.mkdir()

    registry = RepoRegistry(tmp_path / "repos.json")
    original = await registry.add(str(repo_dir), name="Roundtrip Repo")

    as_dict = original.to_dict()
    restored = Repo.from_dict(as_dict)

    assert restored.id == original.id
    assert restored.path == original.path
    assert restored.name == original.name
    assert restored.added_at == original.added_at


@pytest.mark.asyncio
async def test_file_not_a_directory_raises_value_error(tmp_path: Path) -> None:
    """Adding a path that points to a file (not a directory) raises ValueError."""
    a_file = tmp_path / "not-a-dir.txt"
    a_file.write_text("content", encoding="utf-8")

    registry = RepoRegistry(tmp_path / "repos.json")

    with pytest.raises(ValueError, match="does not exist or is not a directory"):
        await registry.add(str(a_file))
