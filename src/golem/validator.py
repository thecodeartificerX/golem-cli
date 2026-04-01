from __future__ import annotations

import os
import sys
from pathlib import Path


def _subprocess_env() -> dict[str, str]:
    """Return environment dict for subprocess calls. On Windows, refreshes PATH from registry."""
    env = os.environ.copy()
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                user_path, _ = winreg.QueryValueEx(key, "Path")
            env["PATH"] = user_path + os.pathsep + env.get("PATH", "")
        except Exception:
            pass
    return env


def _find_bash() -> str | None:
    """Locate bash.exe on Windows. Returns absolute path or None if not found."""
    import shutil

    if sys.platform != "win32":
        return shutil.which("bash")

    env = _subprocess_env()
    # Prefer Git Bash over WSL bash
    candidate = shutil.which("bash", path=env.get("PATH", ""))
    if candidate and "git" in candidate.lower():
        return candidate

    _GIT_BASH_CANDIDATES: list[str] = [
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
    ]
    for path in _GIT_BASH_CANDIDATES:
        if Path(path).is_file():
            return path

    # Fallback: any bash on PATH (WSL, MSYS2, etc.)
    return shutil.which("bash", path=env.get("PATH", ""))


def _normalize_cmd(cmd: str) -> str:
    """On Windows, convert single quotes to double quotes for cmd.exe compatibility."""
    if sys.platform == "win32":
        return cmd.replace("'", '"')
    return cmd
