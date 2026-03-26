from __future__ import annotations

import os
import sys


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


def _normalize_cmd(cmd: str) -> str:
    """On Windows, convert single quotes to double quotes for cmd.exe compatibility."""
    if sys.platform == "win32":
        return cmd.replace("'", '"')
    return cmd
