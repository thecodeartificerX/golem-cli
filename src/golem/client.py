from __future__ import annotations

import json
import os
import sys
from collections.abc import AsyncGenerator
from pathlib import Path

import httpx


def _pid_alive(pid: int) -> bool:
    """Cross-platform PID liveness check."""
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x100000, False, pid)
        if handle == 0:
            return False
        kernel32.CloseHandle(handle)
        return True
    else:
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False
        except OSError:
            return False


def find_server(project_root: Path) -> tuple[str, int] | None:
    """Read .golem/server.json, verify PID is alive, return (host, port) or None."""
    server_json = project_root / ".golem" / "server.json"
    if not server_json.exists():
        return None
    try:
        data = json.loads(server_json.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    pid = data.get("pid")
    host = data.get("host", "127.0.0.1")
    port = data.get("port", 9664)

    if pid is None:
        return None

    if not _pid_alive(int(pid)):
        try:
            server_json.unlink()
        except OSError:
            pass
        return None

    return (str(host), int(port))


class GolemClient:
    """HTTP client for the Golem server API."""

    def __init__(self, host: str, port: int) -> None:
        self._base_url = f"http://{host}:{port}"

    async def create_session(self, spec_path: str, project_root: str) -> dict[str, object]:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._base_url}/api/sessions",
                json={"spec_path": spec_path, "project_root": project_root},
            )
            resp.raise_for_status()
            return resp.json()  # type: ignore[no-any-return]

    async def list_sessions(self) -> list[dict[str, object]]:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self._base_url}/api/sessions")
            resp.raise_for_status()
            return resp.json()  # type: ignore[no-any-return]

    async def get_session(self, session_id: str) -> dict[str, object]:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self._base_url}/api/sessions/{session_id}")
            resp.raise_for_status()
            return resp.json()  # type: ignore[no-any-return]

    async def get_session_diff(self, session_id: str) -> dict[str, object]:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self._base_url}/api/sessions/{session_id}/diff")
            resp.raise_for_status()
            return resp.json()  # type: ignore[no-any-return]

    async def get_session_tickets(self, session_id: str) -> list[dict[str, object]]:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self._base_url}/api/sessions/{session_id}/tickets")
            resp.raise_for_status()
            return resp.json()  # type: ignore[no-any-return]

    async def pause_session(self, session_id: str) -> None:
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{self._base_url}/api/sessions/{session_id}/pause")
            resp.raise_for_status()

    async def resume_session(self, session_id: str) -> None:
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{self._base_url}/api/sessions/{session_id}/resume")
            resp.raise_for_status()

    async def kill_session(self, session_id: str) -> None:
        async with httpx.AsyncClient() as client:
            resp = await client.delete(f"{self._base_url}/api/sessions/{session_id}")
            resp.raise_for_status()

    async def send_guidance(self, session_id: str, text: str) -> None:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._base_url}/api/sessions/{session_id}/guidance",
                json={"text": text},
            )
            resp.raise_for_status()

    async def stream_events(self, session_id: str) -> AsyncGenerator[dict[str, object], None]:
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("GET", f"{self._base_url}/api/sessions/{session_id}/events") as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        data_str = line[6:]
                        if data_str:
                            try:
                                yield json.loads(data_str)
                            except json.JSONDecodeError:
                                pass

    async def get_merge_queue(self) -> list[dict[str, object]]:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self._base_url}/api/merge-queue")
            resp.raise_for_status()
            return resp.json()  # type: ignore[no-any-return]

    async def approve_merge(self, session_id: str) -> None:
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{self._base_url}/api/merge-queue/{session_id}/approve")
            resp.raise_for_status()

    async def get_conflicts(self) -> list[dict[str, object]]:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self._base_url}/api/conflicts")
            resp.raise_for_status()
            return resp.json()  # type: ignore[no-any-return]

    async def get_stats(self) -> dict[str, object]:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self._base_url}/api/server/status")
            resp.raise_for_status()
            return resp.json()  # type: ignore[no-any-return]

    async def get_session_cost(self, session_id: str) -> dict[str, object]:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self._base_url}/api/sessions/{session_id}/cost")
            resp.raise_for_status()
            return resp.json()  # type: ignore[no-any-return]

    async def stop_server(self) -> None:
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{self._base_url}/api/server/stop")
            resp.raise_for_status()
