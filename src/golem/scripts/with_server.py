"""Server lifecycle manager for Playwright UI testing.

Usage: python -m golem.scripts.with_server --backend "uvicorn main:app" --port 8000 -- npx playwright test

Starts a server subprocess, polls for HTTP 200, runs the test command,
then tears down the server on exit (including on crash).
Returns the test command's exit code.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
import urllib.error
import urllib.request


def _wait_for_server(port: int, timeout: float = 30.0, interval: float = 0.5) -> bool:
    """Poll localhost:<port> for HTTP 200. Returns True if server is ready."""
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/"
    while time.monotonic() < deadline:
        try:
            resp = urllib.request.urlopen(url, timeout=2)
            if resp.status == 200:
                return True
        except (urllib.error.URLError, OSError, ConnectionError):
            pass
        time.sleep(interval)
    return False


def _kill_process_tree(proc: subprocess.Popen[str]) -> None:
    """Terminate a process and all its children. On Windows, shell=True spawns
    cmd.exe which doesn't forward terminate signals to children, so we use
    taskkill /T to kill the entire process tree."""
    if sys.platform == "win32":
        subprocess.run(
            f"taskkill /T /F /PID {proc.pid}",
            shell=True,
            capture_output=True,
            encoding="utf-8",
        )
    else:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def main(argv: list[str] | None = None) -> int:
    """Entry point for the server lifecycle manager."""
    parser = argparse.ArgumentParser(description="Start a server, run a test command, then stop the server.")
    parser.add_argument("--backend", required=True, help="Server start command (e.g. 'uvicorn main:app')")
    parser.add_argument("--port", type=int, required=True, help="Port to poll for HTTP 200")
    parser.add_argument("--timeout", type=float, default=30.0, help="Seconds to wait for server startup")
    parser.add_argument("test_cmd", nargs="+", help="Test command to run after server is ready")

    args = parser.parse_args(argv)

    # On Windows, use CREATE_NEW_PROCESS_GROUP so we can kill the tree
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]

    server_proc: subprocess.Popen[str] | None = None
    try:
        server_proc = subprocess.Popen(
            args.backend,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            creationflags=creationflags,
        )

        if not _wait_for_server(args.port, timeout=args.timeout):
            print(f"Server failed to start on port {args.port} within {args.timeout}s", file=sys.stderr)
            return 1

        test_result = subprocess.run(
            " ".join(args.test_cmd),
            shell=True,
            encoding="utf-8",
        )
        return test_result.returncode

    finally:
        if server_proc is not None:
            _kill_process_tree(server_proc)


if __name__ == "__main__":
    sys.exit(main())
