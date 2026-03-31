from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


def _write_json_atomic(path: Path, data: list[dict[str, str]]) -> None:
    """Write a JSON array to path atomically via tmp+rename.

    Uses a sibling .tmp file in the same directory so rename stays on
    the same filesystem/volume (required for atomic rename on Windows).
    os.replace() is atomic on POSIX; best-effort on Windows NTFS within
    the same volume.
    """
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


@dataclass
class Repo:
    id: str  # derived from directory name (e.g., "golem-cli")
    path: str  # absolute path
    name: str  # display name (defaults to directory name)
    added_at: str  # ISO-8601 UTC

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "path": self.path,
            "name": self.name,
            "added_at": self.added_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> Repo:
        return cls(
            id=data["id"],
            path=data["path"],
            name=data["name"],
            added_at=data["added_at"],
        )


class RepoRegistry:
    def __init__(self, registry_path: Path) -> None:
        """Path to repos.json file."""
        self._path = registry_path
        self._lock = asyncio.Lock()

    def _read_all(self) -> list[Repo]:
        """Read repos.json and return all repos. Returns empty list if file doesn't exist."""
        if not self._path.exists():
            return []
        data: list[dict[str, str]] = json.loads(self._path.read_text(encoding="utf-8"))
        return [Repo.from_dict(entry) for entry in data]

    async def add(self, path: str, name: str | None = None) -> Repo:
        """Register a repo.

        Validates that path exists and is a directory.
        Derives the repo ID from the directory name.
        If a repo with the same path is already registered, returns the existing entry.
        """
        resolved = Path(path).resolve()
        if not resolved.exists() or not resolved.is_dir():
            raise ValueError(f"Path does not exist or is not a directory: {path}")

        async with self._lock:
            repos = self._read_all()

            # Deduplicate by resolved absolute path
            norm_path = str(resolved)
            for existing in repos:
                if Path(existing.path).resolve() == resolved:
                    return existing

            base_id = resolved.name
            repo_id = base_id
            existing_ids = {r.id for r in repos}
            if repo_id in existing_ids:
                import hashlib
                suffix = hashlib.sha1(str(resolved).encode()).hexdigest()[:6]
                repo_id = f"{base_id}-{suffix}"
            display_name = name if name is not None else resolved.name
            added_at = datetime.now(tz=UTC).isoformat()

            repo = Repo(id=repo_id, path=norm_path, name=display_name, added_at=added_at)
            repos.append(repo)

            self._path.parent.mkdir(parents=True, exist_ok=True)
            _write_json_atomic(self._path, [r.to_dict() for r in repos])

            return repo

    async def remove(self, repo_id: str) -> bool:
        """Remove a repo by ID. Returns True if the repo was found and removed."""
        async with self._lock:
            repos = self._read_all()
            filtered = [r for r in repos if r.id != repo_id]
            if len(filtered) == len(repos):
                return False
            _write_json_atomic(self._path, [r.to_dict() for r in filtered])
            return True

    async def list_repos(self) -> list[Repo]:
        """Return all registered repos."""
        async with self._lock:
            return self._read_all()

    async def get(self, repo_id: str) -> Repo | None:
        """Look up a repo by ID. Returns None if not found."""
        async with self._lock:
            repos = self._read_all()
            for repo in repos:
                if repo.id == repo_id:
                    return repo
            return None
