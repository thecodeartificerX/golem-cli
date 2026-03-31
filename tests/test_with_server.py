"""Tests for golem.scripts.with_server — server lifecycle manager."""

from __future__ import annotations

import http.server
import socket
import sys
import threading
from pathlib import Path

from golem.scripts.with_server import _wait_for_server, main


def _free_port() -> int:
    """Find a free TCP port by binding to port 0."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_wait_for_server_success() -> None:
    """Start a real HTTP server and verify _wait_for_server detects it."""
    handler = http.server.SimpleHTTPRequestHandler
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert _wait_for_server(port, timeout=5.0, interval=0.1) is True
    finally:
        server.shutdown()


def test_wait_for_server_timeout() -> None:
    """Verify _wait_for_server returns False when nothing is listening."""
    port = _free_port()
    assert _wait_for_server(port, timeout=0.5, interval=0.1) is False


def test_main_starts_and_stops_server(tmp_path: Path) -> None:
    """Main should start a server, run a test command, then stop the server."""
    port = _free_port()
    python = sys.executable
    backend = f"{python} -m http.server {port} --bind 127.0.0.1"
    if sys.platform == "win32":
        test_cmd = "exit 0"
    else:
        test_cmd = "true"

    result = main(["--backend", backend, "--port", str(port), "--timeout", "15", test_cmd])
    assert result == 0


def test_main_returns_test_exit_code(tmp_path: Path) -> None:
    """Main should return the test command's exit code."""
    port = _free_port()
    python = sys.executable
    backend = f"{python} -m http.server {port} --bind 127.0.0.1"
    test_cmd = "exit 42"

    result = main(["--backend", backend, "--port", str(port), "--timeout", "15", test_cmd])
    assert result == 42


def test_main_server_fails_to_start() -> None:
    """Main should return 1 if the server never starts."""
    port = _free_port()
    # Use a command that exits immediately without serving
    if sys.platform == "win32":
        backend = "exit 0"
    else:
        backend = "true"

    result = main(["--backend", backend, "--port", str(port), "--timeout", "1", "echo should_not_run"])
    assert result == 1
