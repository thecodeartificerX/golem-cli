# Spec 2: CLI-as-Client

> Part 2 of 5 in the Multi-Spec Orchestration series.
> Full design doc: `docs/superpowers/specs/2026-03-27-multi-spec-orchestration-design.md`
> **Depends on:** Spec 1 (Foundation + Server Core) merged to main.
> **Can run in parallel with:** Specs 3 and 4.

## Context

Spec 1 added the server, session manager, and REST API. This spec makes the CLI a thin client that routes all commands through that server. `golem run spec.md` auto-starts the server if needed, creates a session, and streams logs. New commands let users manage sessions, monitor costs, and control lifecycle from the terminal.

### Key Points

- `find_server()` reads `.golem/server.json` to discover the running server
- All session commands route through HTTP to the server API
- `--no-server` flag bypasses server for CI/debugging (preserves old behavior)
- `golem run spec1.md spec2.md` creates multiple sessions at once
- Server auto-starts on first `golem run` if not already running
- httpx is already a dev dependency — use it for the client

### Coding Conventions

- **Python 3.12+**, async-first, strict typing, no `Any`
- **Always `encoding="utf-8"`** on all file I/O
- **No emoji in CLI/TUI output** — Rich crashes on Windows cp1252
- **Formatter:** ruff, line length 120
- **Tests:** pytest, use `tmp_path` fixture, mock `find_server` and `GolemClient` in CLI tests

---

## Task 1: Client Module

**Files:**
- Create: `src/golem/client.py`

- [ ] **Step 1: Create client.py with server discovery and HTTP client**
  Create `src/golem/client.py` with:
  - `find_server(project_root: Path) -> tuple[str, int] | None` — reads `.golem/server.json`, verifies PID alive, returns (host, port) or None
  - `_pid_alive(pid: int) -> bool` — cross-platform PID check (Windows: `ctypes.windll.kernel32.OpenProcess`, Unix: `os.kill(pid, 0)`)
  - `GolemClient` class wrapping `httpx.AsyncClient`:
    - `__init__(host: str, port: int)`
    - `create_session(spec_path: str, project_root: str) -> dict`
    - `list_sessions() -> list[dict]`
    - `get_session(session_id: str) -> dict`
    - `pause_session(session_id: str) -> None`
    - `resume_session(session_id: str) -> None`
    - `kill_session(session_id: str) -> None`
    - `send_guidance(session_id: str, text: str) -> None`
    - `stream_events(session_id: str) -> AsyncIterator[dict]` — SSE consumer
    - `get_merge_queue() -> list[dict]`
    - `approve_merge(session_id: str) -> None`
    - `get_conflicts() -> list[dict]`
    - `get_stats() -> dict`
    - `stop_server() -> None`
  All methods use `encoding="utf-8"` for file I/O.

- [ ] **Step 2: Create test_client.py**
  Create `tests/test_client.py` with tests:
  - `test_find_server_no_file` — returns None
  - `test_find_server_stale_pid` — removes stale server.json, returns None
  - `test_find_server_valid` — returns (host, port)
  - `test_client_create_session` — mock httpx, verify POST
  - `test_client_list_sessions` — mock httpx, verify GET
  - `test_client_stream_events` — mock SSE stream

- [ ] **Step 3: Commit**
  ```bash
  git add src/golem/client.py tests/test_client.py
  git commit -m "feat: add GolemClient for CLI-to-server communication"
  ```

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. Client imports
uv run python -c "from golem.client import find_server, GolemClient; print('CLIENT_IMPORT: PASS')"

# 2. Tests pass
uv run pytest tests/test_client.py -v --tb=short 2>&1 | tail -1
```

Expected:
```
CLIENT_IMPORT: PASS
[N] passed
```

---

## Task 2: CLI Command Routing

**Files:**
- Modify: `src/golem/cli.py`

- [ ] **Step 1: Implement run() server routing**
  Modify `run()`:
  - When `--no-server` is NOT set: call `find_server()`, auto-start if needed, call `client.create_session()`, stream SSE to console via `client.stream_events()`
  - Print session ID at start: `"Session: auth-flow-1 (streaming logs...)"`
  - When `--no-server` IS set: unchanged behavior

- [ ] **Step 2: Add session-aware commands**
  Add/modify commands to route through server when available:
  - `status [session_id]` — all sessions table or single session detail
  - `logs <session_id> [-f]` — stream from server SSE
  - `pause <session_id>` — POST to server
  - `resume <session_id>` — POST to server
  - `kill <session_id>` — DELETE to server
  - `guidance <session_id> <text>` — POST to server
  - `tickets <session_id>` — GET from server
  - `cost [session_id]` — aggregate or per-session
  - `diff <session_id>` — GET from server
  - `history [session_id]` — aggregate or per-session

- [ ] **Step 3: Modify existing commands for session awareness**
  - `clean [session_id]` — if session_id given, clean single session via server; else clean all
  - `export [session_id]` — if session_id given, export single session; else export all
  - `inspect <ticket_id> --session <id>` — route through server to correct session's ticket store

- [ ] **Step 4: Handle "server not running" gracefully**
  All server-dependent commands must check `find_server()` first. If None, print:
  `"Server not running. Start with 'golem server start' or use 'golem run --no-server' for direct execution."`

- [ ] **Step 5: Support multi-spec run**
  Modify `run()` to accept variadic spec paths: `golem run spec1.md spec2.md`. For each spec, create a session. Stream events from the first session by default, print IDs for all.

- [ ] **Step 6: Update CLI tests**
  In `tests/test_cli.py`: add tests for server routing (mock `find_server` and `GolemClient`), multi-spec run, session-aware commands, "server not running" error messages.

- [ ] **Step 7: Commit**
  ```bash
  git add src/golem/cli.py tests/test_cli.py
  git commit -m "feat: route CLI commands through server, add session-aware commands"
  ```

#### Completion Gate

```bash
cd F:/Tools/Projects/golem-cli

# 1. New commands appear in help
for cmd in pause resume kill tickets cost conflicts; do
  uv run golem $cmd --help 2>&1 | head -1 | grep -qi "usage\|error\|missing" && echo "$cmd: PASS" || echo "$cmd: FAIL"
done

# 2. Multi-spec argument accepted
uv run golem run --help 2>&1 | grep -qi "spec" && echo "MULTI_SPEC: PASS" || echo "MULTI_SPEC: FAIL"

# 3. CLI tests pass
uv run pytest tests/test_cli.py -v --tb=short 2>&1 | tail -1

# 4. Full suite passes
uv run pytest --tb=short -q 2>&1 | tail -1
```

Expected:
```
pause: PASS
resume: PASS
kill: PASS
tickets: PASS
cost: PASS
conflicts: PASS
MULTI_SPEC: PASS
[N] passed
[N] passed
```

---

## Phase 2 Completion Gate

**Phase 2 is NOT complete until every check below passes.** If any check fails, return to the responsible task, fix the issue, and re-run this entire gate.

### Gate 1: Client Module

```bash
cd F:/Tools/Projects/golem-cli
uv run python -c "from golem.client import find_server, GolemClient; print('CLIENT: PASS')"
uv run pytest tests/test_client.py -v --tb=short 2>&1 | tail -1
```

Expected: `CLIENT: PASS` + `[N] passed`

### Gate 2: CLI Commands

```bash
cd F:/Tools/Projects/golem-cli
for cmd in "server start" "server stop" "server status" pause resume kill tickets cost conflicts; do
  uv run golem $cmd --help >/dev/null 2>&1 && echo "$cmd: PASS" || echo "$cmd: FAIL"
done
```

Expected: all PASS

### Gate 3: Server Not Running Handling

```bash
cd F:/Tools/Projects/golem-cli
rm -f .golem/server.json 2>/dev/null
uv run golem status 2>&1 | grep -qi "not running\|no server\|start" && echo "GRACEFUL: PASS" || echo "GRACEFUL: FAIL"
```

Expected: `GRACEFUL: PASS`

### Gate 4: Full Test Suite

```bash
cd F:/Tools/Projects/golem-cli
uv run pytest -v --tb=short 2>&1 | tail -5
```

Expected: `[N] passed, 0 failed`

### Phase 2 Verdict

| Gate | Validates Tasks |
|------|----------------|
| Gate 1 | Task 1 (client module) |
| Gate 2 | Task 2 steps 2-3 (new commands) |
| Gate 3 | Task 2 step 4 (graceful handling) |
| Gate 4 | All tasks (regression) |
