# Phase 5: CLI + Client Refactor

## Gotchas
- CLI uses `find_server()` to check if a server is running — the edict commands should also use this pattern
- `_ensure_server()` auto-starts the server if not running — edict commands should reuse this
- `typer.Exit` raises `click.exceptions.Exit` in tests, not `SystemExit`
- CLI must work both with and without a running server — edict commands require server (no `--no-server` fallback for edicts)
- `_server_request()` uses urllib (not httpx) for simple server calls — keep consistent
- GolemClient uses httpx with per-call clients — follow the same pattern for new methods
- Old session commands should print deprecation warnings, not be removed immediately

## Files
```
src/golem/
├── cli.py                # MODIFY — add edict/repo commands, deprecate session commands
├── client.py             # MODIFY — add edict/repo client methods
tests/
├── test_cli.py           # MODIFY — add edict command tests
├── test_client.py        # MODIFY — add edict client method tests
```

---

## Task 5.1: Add Edict and Repo Client Methods

**Skills to load:** `superpowers:test-driven-development`

**Architecture notes:**

Add to `GolemClient`:

```python
# Repo methods
async def list_repos(self) -> list[dict]: ...           # GET /api/repos
async def add_repo(self, path: str) -> dict: ...        # POST /api/repos
async def remove_repo(self, repo_id: str) -> None: ...  # DELETE /api/repos/{id}

# Edict methods
async def list_edicts(self, repo_id: str) -> list[dict]: ...                    # GET /api/repos/{id}/edicts
async def create_edict(self, repo_id: str, title: str, body: str) -> dict: ...  # POST /api/repos/{id}/edicts
async def get_edict(self, repo_id: str, edict_id: str) -> dict: ...             # GET /api/repos/{id}/edicts/{eid}
async def update_edict(self, repo_id: str, edict_id: str, **kwargs) -> dict: ...# PATCH
async def delete_edict(self, repo_id: str, edict_id: str) -> None: ...          # DELETE
async def start_edict(self, repo_id: str, edict_id: str) -> dict: ...           # POST .../start
async def pause_edict(self, repo_id: str, edict_id: str) -> None: ...           # POST .../pause
async def resume_edict(self, repo_id: str, edict_id: str) -> None: ...          # POST .../resume
async def kill_edict(self, repo_id: str, edict_id: str) -> None: ...            # POST .../kill
async def send_edict_guidance(self, repo_id: str, edict_id: str, text: str) -> None: ...  # POST .../guidance
async def get_edict_board(self, repo_id: str, edict_id: str) -> dict: ...       # GET .../board
async def get_edict_tickets(self, repo_id: str, edict_id: str) -> list[dict]: ...# GET .../tickets
async def get_edict_cost(self, repo_id: str, edict_id: str) -> dict: ...        # GET .../cost
async def get_edict_diff(self, repo_id: str, edict_id: str) -> dict: ...        # GET .../diff
async def get_edict_plan(self, repo_id: str, edict_id: str) -> dict: ...        # GET .../plan
async def stream_edict_events(self, repo_id: str, edict_id: str) -> AsyncGenerator[dict, None]: ...  # GET .../observe SSE
async def stream_edict_logs(self, repo_id: str, edict_id: str) -> AsyncGenerator[dict, None]: ...    # GET .../logs SSE
```

All follow the existing pattern: fresh `httpx.AsyncClient` per call, `raise_for_status()`, return JSON.

**Files to modify:**
- `src/golem/client.py` — add ~20 new methods

**Validation command:** `uv run pytest tests/test_client.py -v`

**Tests to write:**
- Each new method calls the correct endpoint with correct HTTP method
- Error handling (404, 400) propagates correctly
- SSE streaming methods parse events correctly

---

## Task 5.2: Add Edict CLI Commands

**Skills to load:** None (follows existing CLI patterns exactly)

**Architecture notes:**

New typer sub-app: `edict_app` mounted as `golem edict`:

```
golem edict create TITLE [--body TEXT] [--repo REPO_ID]  → POST create edict, optionally start
golem edict list [--repo REPO_ID]                        → GET list edicts
golem edict show EDICT_ID [--repo REPO_ID]               → GET edict detail
golem edict start EDICT_ID [--repo REPO_ID]              → POST start pipeline
golem edict pause EDICT_ID [--repo REPO_ID]              → POST pause
golem edict resume EDICT_ID [--repo REPO_ID]             → POST resume
golem edict kill EDICT_ID [--repo REPO_ID]               → POST kill
golem edict guidance EDICT_ID TEXT [--repo REPO_ID]      → POST guidance
golem edict board EDICT_ID [--repo REPO_ID]              → GET board, render as table
golem edict cost EDICT_ID [--repo REPO_ID]               → GET cost breakdown
golem edict diff EDICT_ID [--repo REPO_ID]               → GET diff
golem edict logs EDICT_ID [--follow/-f] [--repo REPO_ID] → stream SSE logs
```

New typer sub-app: `repo_app` mounted as `golem repo`:

```
golem repo add PATH [--name NAME]   → POST add repo
golem repo list                     → GET list repos
golem repo remove REPO_ID           → DELETE remove repo
```

**Repo ID resolution:** If `--repo` is not provided, derive repo_id from the current working directory name. This makes `golem edict create "Fix bug"` work without specifying `--repo` when run from within a repo.

**Auto-server:** All edict commands call `_ensure_server()` first — no `--no-server` fallback.

**Output format:** Use Rich tables for list/board commands. Use Rich panels for detail views.

**Deprecation:** Add deprecation warnings to existing session commands (`run`, `status`, `pause`, `resume`, `kill`, `guidance`, `tickets`, `cost`, `diff`). Print: `"[DEPRECATED] Use 'golem edict ...' instead. Session commands will be removed in v0.6.0."`

**Files to modify:**
- `src/golem/cli.py` — add edict_app, repo_app sub-typers, deprecation warnings on old commands

**Validation command:** `uv run pytest tests/test_cli.py -v`

**Tests to write:**
- `golem repo add` with valid path
- `golem repo list` output format
- `golem edict create` creates edict via client
- `golem edict list` shows table
- `golem edict start` starts pipeline
- `golem edict board` renders column layout
- Repo ID auto-derivation from CWD
- Deprecated commands print warning

---

## Task 5.3: Add `golem run` Edict Bridge

**Skills to load:** None

**Architecture notes:**

Update `golem run` to create an Edict under the hood:
1. If server is running: create edict from spec file (title = spec filename, body = spec content)
2. Start the edict pipeline
3. Stream logs to console
4. This makes `golem run spec.md` work exactly as before but uses the Edict system

This preserves backward compatibility — users can keep using `golem run spec.md` and it transparently creates an Edict.

If `--no-server` is passed, fall back to the old direct-execution path (unchanged).

**Files to modify:**
- `src/golem/cli.py` — update `run` command to bridge through Edicts when server is available

**Validation command:** `uv run pytest tests/test_cli.py -k "run" -v`

**Tests to write:**
- `golem run spec.md` creates edict and starts pipeline when server running
- `golem run spec.md --no-server` uses direct execution (backward compat)
