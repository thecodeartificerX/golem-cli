# Golem UI — Web Dashboard for Golem CLI

## Goal

Add a browser-based dashboard to golem that lets you pick a spec file, launch a run, and watch task progress + console output in real time. Replaces the need to stare at a terminal. Accessible via `golem ui`.

## Architecture

**FastAPI server** on port `9664` (configurable via `--port`). Single-page dashboard served at `/`. Golem runs as a subprocess; output is streamed to the browser via Server-Sent Events (SSE).

```
Browser (HTML/CSS/JS)
  ├── GET /              → serves dashboard HTML
  ├── GET /api/events    → SSE stream (progress.log + tasks.json)
  └── POST /api/run      → launches golem subprocess
         │
         ▼
FastAPI (ui.py)
  ├── spawns: uv run golem run <spec_filename> --force  (cwd = spec's parent dir)
  ├── tails: .golem/progress.log  (new lines → SSE "log" events)
  ├── polls: .golem/tasks.json    (state changes → SSE "tasks" events)
  └── monitors: subprocess exit   (→ SSE "status" event)
```

No WebSocket, no database, no template engine. One SSE connection, three event types.

## CLI Command

```
golem ui [--port PORT]
```

- **Default port:** `9664`
- **`--port`:** override port (e.g., `golem ui --port 5555`)
- Prints `Golem UI running at http://localhost:9664` on startup
- Opens browser automatically via `webbrowser.open()`
- Ctrl+C shuts down server and kills any active golem subprocess (via FastAPI lifespan `shutdown` event — terminates `current_process` if alive)
- Only one run at a time — second `POST /api/run` while running returns 409

## UI Layout

```
┌──────────────────────────────────────────────────────────────┐
│  GOLEM  │  SPEC [F:/projects/my-app/spec.md] [Browse]  [CONSTRUCT]  │
├────────────────────┬─────────────────────────────────────────┤
│  TASK TIMELINE     │  CONSOLE                                │
│                    │                                         │
│  ● Parse spec      │  [12:00:01] Initializing golem run...  │
│    completed 15s   │  [12:00:03] Planning — Opus session     │
│  ● Generate tasks  │  [12:00:15] COMPLETE parse-spec         │
│    completed 1s    │  [12:00:16] COMPLETE generate-tasks     │
│  ◉ Build auth      │  [12:00:17] START group-1               │
│    group-1 running │  [12:00:18] WORKING build-auth          │
│  ◉ Build API       │  [12:00:18] WORKING build-api           │
│    group-2 running │                                         │
│  ○ Write tests     │                                         │
│    blocked         │                                         │
│  ○ Final validation│                                         │
│    pending merge   │                                         │
│                    │                                         │
├────────────────────┴─────────────────────────────────────────┤
│  2 / 6 TASKS  [████████░░░░░░░░░░░░░░░░]  33%              │
└──────────────────────────────────────────────────────────────┘
```

## UI Components

### Control Bar (top)

- **Golem branding** — left-aligned, purple accent (`#c8a2ff`), letter-spacing
- **Spec path input** — editable text input showing the spec file path. Paste-friendly.
- **Browse button** — triggers a hidden `<input type="file" accept=".md">`. On file selection, populates the path input. Note: browser file picker returns a filename only (no full path for security). The input field is also directly editable so users can paste a full path.
- **CONSTRUCT button** — purple gradient (`#7c3aed` → `#a855f7`). Sends `POST /api/run` with the spec path. Disabled when no path entered. During a run: text changes to "RUNNING...", button disabled, subtle pulse animation.

### Task Timeline (left panel)

- Populated from `tasks.json` via SSE `tasks` events
- Each task shows:
  - **Status dot:** green (#22c55e) = completed, yellow (#eab308) with glow = running, gray (#333) = pending, red (#ef4444) = failed/blocked
  - **Task name** — colored to match status
  - **Subtitle** — "completed in Xs" / "group-N · running..." / "blocked by X" / "retry #N" / "pending"
- Vertical timeline line (2px, `#2a2a3e`) connecting dots
- Tasks ordered by execution order from `tasks.json`

### Console (right panel)

- Streams `progress.log` lines via SSE `log` events
- **Color coding by verb** (parsed from `progress.log` format `[ISO_TIMESTAMP] VERB args`):
  - Green (`#22c55e`) — `COMPLETE`, `GROUP_COMPLETE`, `FINAL_VALIDATION` (when PASSED)
  - Yellow (`#eab308`) — `START`
  - Blue (`#3b82f6`) — informational lines (no recognized verb)
  - Red (`#ef4444`) — `BLOCKED`, `RETRY`, `FINAL_VALIDATION` (when FAILED)
  - Gray (`#555`) — everything else
- Monospace font, dark background (`#12121a`)
- Auto-scrolls to bottom on new lines
- User can scroll up without losing position (auto-scroll pauses when scrolled up, resumes when scrolled to bottom)
- Scrollbar styled to match dark theme

### Progress Bar (bottom)

- Left: `"X / Y TASKS"` counter with purple text
- Center: gradient progress bar (`#7c3aed` → `#a855f7`) with smooth width transition
- Right: percentage text
- Updates on each SSE `tasks` event

## Theme

Dark theme throughout. No light mode toggle. Color palette:

```
Background:      #0a0a0f
Panel:           #12121a
Border:          #1e1e2e
Muted text:      #555, #666
Label text:      #c8a2ff (purple accent)
Body text:       #8b8b9e
Success:         #22c55e
Warning/Active:  #eab308
Error:           #ef4444
Info:            #3b82f6
Accent gradient: #7c3aed → #a855f7
Font:            'JetBrains Mono', 'Fira Code', 'Cascadia Code', monospace
```

## API Endpoints

### `GET /`

Serves the dashboard HTML. The HTML template is read from `src/golem/ui_template.html` at startup with `encoding="utf-8"` (required for Windows — see CLAUDE.md) and served as a plain `HTMLResponse`. No template rendering.

### `POST /api/run`

**Request body:**
```json
{
  "spec_path": "F:/projects/my-app/spec.md"
}
```

**Behavior:**
1. Validates `spec_path` exists and is a `.md` file
2. Returns 409 if a run is already active
3. Resolves the spec's parent directory as the working directory
4. Runs `uv run golem clean` as a subprocess in the spec's parent directory to clear previous state (this properly removes git worktrees before deleting `.golem/`). If `.golem/` doesn't exist, this is a no-op.
5. Spawns `uv run golem run <spec_filename> --force` as an async subprocess with cwd set to spec's parent directory
6. Returns 200 immediately: `{"status": "started", "cwd": "<dir>"}`

**Response codes:**
- 200 — run started
- 400 — invalid path or not a .md file
- 404 — spec file not found
- 409 — run already in progress

### `GET /api/events`

**SSE stream.** Client connects with `EventSource('/api/events')`. Server sends three event types:

**`event: status`**
```json
{"state": "idle"}
{"state": "running", "spec": "spec.md", "cwd": "F:/projects/my-app"}
{"state": "done", "exit_code": 0}
{"state": "error", "exit_code": 1, "message": "Process crashed"}
```
Sent on: initial connection (current state), run start, run end.

**`event: log`**
```json
{"timestamp": "12:00:15", "verb": "COMPLETE", "message": "COMPLETE parse-spec", "raw": "[2026-03-25T12:00:15Z] COMPLETE parse-spec"}
```
Sent on: each new line appended to `.golem/progress.log`. Parsing rule: lines match regex `^\[(\d{4}-\d{2}-\d{2}T[\d:]+Z?)\]\s+(\w+)\s*(.*)$`. Group 1 = ISO timestamp (extract time portion `HH:MM:SS` for the `timestamp` field), group 2 = verb, group 3 = remainder. Lines that don't match the regex are sent with `verb: null` and the full line as `message`.

**`event: tasks`**
```json
{
  "tasks": [
    {"id": "task-1", "description": "Parse spec", "group": "planning", "status": "completed", "depends_on": []},
    {"id": "task-2", "description": "Build auth", "group": "group-1", "status": "in_progress", "depends_on": ["task-1"]}
  ],
  "completed": 2,
  "total": 6
}
```
Sent on: any change detected in `.golem/tasks.json` (polled every 1s). **Flattening:** `tasks.json` nests tasks inside group objects — the `group` field does not exist on the `Task` object. When building this SSE payload, iterate over groups and inject `"group": group["id"]` into each task dict before sending.

**Connection behavior:**
- Sends current state immediately on connect (status + any existing tasks + buffered log lines)
- Keeps connection alive with SSE comment heartbeat every 15s (`: heartbeat\n\n`)
- Closes when client disconnects

**SSE wire format:** Each event is a raw UTF-8 string following the SSE spec:
```
event: <type>\n
data: <json>\n
\n
```
The `event_stream()` async generator yields these formatted strings. Use `StreamingResponse(event_stream(), media_type="text/event-stream")` in FastAPI.

## Data Flow

### File Tailing Strategy

A background `asyncio.Task` runs while a golem subprocess is active:

1. **progress.log tailing** — opens `.golem/progress.log` with `encoding="utf-8"`, seeks to last known position, reads new lines every 500ms. Each new line is parsed and broadcast as an SSE `log` event. If the file does not exist yet, skip silently and retry on next poll.

2. **tasks.json polling** — reads `.golem/tasks.json` with `encoding="utf-8"` every 1s, compares to last known state (by hash or mtime). On change, broadcasts SSE `tasks` event with full task list + counts. If the file does not exist yet, skip silently (do not send an empty tasks event, do not crash).

3. **Subprocess monitoring** — `await process.wait()` in a separate task. On exit, sends final `status` event with exit code, stops tailing.

**No `watchdog` or `inotify`** — simple seek-and-read polling. Files are small (<10KB) and update infrequently (seconds between events).

### Working Directory Resolution

When the user provides a spec path like `F:/projects/my-app/spec.md`:
- `cwd` for the golem subprocess = `F:/projects/my-app/`
- `.golem/` directory is expected at `F:/projects/my-app/.golem/`
- The tailing tasks watch files in that `.golem/` directory

## UI States

| State | Control Bar | Timeline | Console | Progress |
|-------|------------|----------|---------|----------|
| **Idle** | CONSTRUCT disabled (no path) | Empty, "Select a spec to begin" | Empty | Hidden |
| **Ready** | CONSTRUCT enabled (path entered) | Empty | Empty | Hidden |
| **Running** | "RUNNING..." (disabled, pulse) | Tasks appear, dots animate | Lines stream in | Shows progress |
| **Done** | CONSTRUCT re-enabled | All dots green (or final state) | Final line visible | 100% or final |
| **Error** | CONSTRUCT re-enabled | Last active tasks shown red | Error visible | Stopped |

## File Structure

```
src/golem/
├── ui.py              ← NEW: FastAPI app, SSE endpoint, subprocess mgmt
├── ui_template.html   ← NEW: Single-file dashboard (HTML + CSS + JS)
├── cli.py             ← MODIFY: Add `golem ui` subcommand
```

### `ui.py` — Server Module

- `create_app()` → returns FastAPI instance with lifespan handler (on shutdown: kill `current_process` if alive)
- `run_golem(spec_path: str)` → runs `golem clean`, spawns `golem run` subprocess, starts tailing
- `event_stream()` → async generator yielding SSE events (replays `log_buffer` on connect, then streams new events)
- `tail_progress_log(golem_dir: Path)` → reads new lines from progress.log, appends to `log_buffer`
- `poll_tasks_json(golem_dir: Path)` → flattens group→task structure, detects state changes
- Module-level state: `current_process`, `current_cwd`, `event_queue` (asyncio.Queue), `log_buffer` (`collections.deque(maxlen=200)` of parsed log event dicts — used to replay recent history on SSE reconnect)

### `ui_template.html` — Dashboard

- Self-contained HTML + embedded `<style>` + embedded `<script>`
- No external dependencies (no CDN links, no npm)
- `EventSource` connects to `/api/events`
- DOM manipulation for task timeline, console, progress bar
- `<input type="file">` for browse, text input for paste

### `cli.py` — Changes

Add one new command:
```python
@app.command()
def ui(port: int = typer.Option(9664, help="Port to serve the dashboard on")):
    """Launch the Golem web dashboard."""
```

Imports `uvicorn` and `create_app` from `ui.py`. Calls `webbrowser.open(f"http://localhost:{port}")` first (spawns browser as a separate OS process, returns immediately), then calls `uvicorn.run(app, host="127.0.0.1", port=port)` which blocks until Ctrl+C.

## Dependencies

Add to `pyproject.toml`:
```
"fastapi>=0.115.0",
"uvicorn[standard]>=0.34.0",
```

## What We're NOT Doing

- No database or session persistence
- No multi-run history — one run at a time
- No authentication — localhost only
- No file upload — browser file picker is cosmetic; path input is the real mechanism
- No light theme toggle — dark only
- No custom settings in the UI
- No Jinja or template engine — raw HTML string
- No `watchdog` — polling is sufficient

## Testing Strategy

- **Unit test `ui.py`** — test SSE event parsing, progress.log line parsing, tasks.json diffing
- **No Playwright tests** — manual testing is sufficient for a dev tool UI
- **Smoke test:** `golem ui`, open browser, select a spec, hit CONSTRUCT, watch output stream

## Edge Cases

- **Spec path with spaces** — properly quoted when passing to subprocess
- **Golem not installed** — `uv run golem` resolves from the project's pyproject.toml; the UI server runs from the same project
- **`.golem/` doesn't exist yet** — tailing starts polling for the directory to appear (golem creates it on run start)
- **Browser closed mid-run** — golem subprocess continues, SSE just has no listeners. Reopening the browser reconnects and gets current state.
- **Port already in use** — uvicorn prints error and exits. User can retry with `--port`.
- **Large progress.log** — on reconnect, only send last 200 lines as buffer, then stream new ones
