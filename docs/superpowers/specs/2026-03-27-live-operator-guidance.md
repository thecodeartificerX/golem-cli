# Live Operator Guidance

## Problem

Golem's UI dashboard is read-only during runs. The operator watches progress.log stream by and can see ticket status updates, but cannot course-correct the Tech Lead mid-run. If the Tech Lead is going down the wrong path (e.g., dispatching too many Junior Devs, misunderstanding a requirement), the operator's only option is to kill the process and start over.

ZeroShot solves this with `USER_GUIDANCE_CLUSTER` and `USER_GUIDANCE_AGENT` messages that inject operator text into running agents' context. Their guidance queue accumulates between agent turns and is prepended to the next context build.

## Design

### Ticket-based guidance (recommended approach)

Use the existing ticket system as the guidance channel. The ticket system is already the communication backbone between agents — extending it to carry operator guidance requires no new infrastructure.

**Flow:**
1. Operator types guidance in the UI dashboard and clicks SEND
2. `POST /api/guidance` creates (or appends to) a guidance ticket with `type="guidance"` and the operator's text as a history event
3. The Tech Lead prompt instructs it to check for guidance tickets at phase boundaries
4. When the Tech Lead finds a pending guidance ticket, it reads the operator's note, acknowledges it, and factors it into its next decision

**Why ticket-based over file-based:** Tickets give us acknowledgment tracking (status: `pending` → `acknowledged`), full history with timestamps, and the Tech Lead already has MCP tools to read tickets. File-based guidance would require teaching the agent to poll a specific file path.

**Why not SDK session injection:** The `claude-agent-sdk` `query()` API is a streaming generator with no documented method to inject messages mid-session. The Tech Lead's only real-time interface to external state is via file reads and MCP tool calls.

### Guidance ticket lifecycle

```
Operator sends guidance
  → guidance ticket created (status=pending, type=guidance)
  → Tech Lead checks list_tickets at phase boundary
  → Tech Lead reads guidance ticket (read_ticket)
  → Tech Lead acknowledges (update_ticket status=acknowledged)
  → Tech Lead factors guidance into next decision
```

Multiple guidance messages can accumulate — each becomes a new history event on the same guidance ticket.

### Tech Lead polling instruction

Add to `tech_lead.md` between phases:

```
Before starting each new phase (after reading plans, after dispatching
Junior Devs, after reviewing work), call mcp__golem__list_tickets and
check for any ticket with type=guidance and status=pending. If found,
read it fully, factor the guidance into your next decision, then update
its status to acknowledged with a note confirming you received it.
```

This is lightweight — one `list_tickets` call per phase boundary, not a continuous poll.

---

## Implementation

### Task 1: Add `log_guidance_received` to `progress.py`

**Files:**
- Modify: `src/golem/progress.py`

- [ ] **Step 1: Add guidance event method**

  Append this method to the `ProgressLogger` class, after `log_merge_complete`:

  ```python
  def log_guidance_received(self, note: str) -> None:
      """Log that operator guidance was received."""
      self._write(f"GUIDANCE_RECEIVED note={note}")
  ```

- [ ] **Step 2: Commit**

  ```bash
  git add src/golem/progress.py
  git commit -m "feat: add log_guidance_received to ProgressLogger"
  ```

#### Completion Gate
```bash
cd F:/Tools/Projects/golem-cli

# 1. Method exists in progress.py
python -c "from golem.progress import ProgressLogger; assert hasattr(ProgressLogger, 'log_guidance_received'), 'missing method'" && echo "METHOD: PASS" || echo "METHOD: FAIL"

# 2. Method writes GUIDANCE_RECEIVED to log
python -c "
import tempfile, pathlib
from golem.progress import ProgressLogger
with tempfile.TemporaryDirectory() as d:
    p = pathlib.Path(d)
    lg = ProgressLogger(p)
    lg.log_guidance_received('test note')
    content = (p / 'progress.log').read_text(encoding='utf-8')
    assert 'GUIDANCE_RECEIVED' in content, f'not found in: {content}'
    assert 'test note' in content, f'note not found in: {content}'
print('LOG_WRITE: PASS')
" || echo "LOG_WRITE: FAIL"
```

Expected output:
```
METHOD: PASS
LOG_WRITE: PASS
```

If any check fails, fix the issue and re-run ALL checks before proceeding.

---

### Task 2: Add `POST /api/guidance` endpoint to `ui.py`

**Files:**
- Modify: `src/golem/ui.py`

- [ ] **Step 1: Add `GuidanceRequest` Pydantic model at module level**

  After the existing `RunRequest` class (around line 48), add:

  ```python
  class GuidanceRequest(BaseModel):
      text: str
  ```

  The model must be at module level (not inside `create_app()`) to avoid FastAPI's annotation resolution bug with `from __future__ import annotations`.

- [ ] **Step 2: Add `POST /api/guidance` route inside `create_app()`**

  Add this route after the `/api/clean` route (after line 407), inside the `create_app()` function:

  ```python
  @app.post("/api/guidance")
  async def send_guidance(req: GuidanceRequest) -> dict[str, object]:
      """Send operator guidance to the running Tech Lead."""
      from golem.tickets import Ticket, TicketContext, TicketStore

      if not req.text.strip():
          from fastapi.responses import JSONResponse
          return JSONResponse({"error": "Guidance text cannot be empty"}, status_code=422)

      if current_process is None or current_process.returncode is not None:
          from fastapi.responses import JSONResponse
          return JSONResponse({"error": "No run in progress"}, status_code=400)

      golem_dir = Path(current_cwd) / ".golem" if current_cwd else None
      if not golem_dir or not golem_dir.exists():
          from fastapi.responses import JSONResponse
          return JSONResponse({"error": "No .golem directory found"}, status_code=400)

      store = TicketStore(golem_dir / "tickets")

      # Find existing pending guidance ticket or create new one
      tickets = await store.list_tickets()
      guidance_ticket = None
      for t in tickets:
          if t.type == "guidance" and t.status == "pending":
              guidance_ticket = t
              break

      if guidance_ticket:
          await store.update(
              ticket_id=guidance_ticket.id,
              status="pending",
              note=req.text,
              agent="operator",
          )
      else:
          ticket = Ticket(
              id="",
              type="guidance",
              title="Operator Guidance",
              status="pending",
              priority="high",
              created_by="operator",
              assigned_to="tech_lead",
              context=TicketContext(),
          )
          ticket_id = await store.create(ticket)
          await store.update(
              ticket_id=ticket_id,
              status="pending",
              note=req.text,
              agent="operator",
          )

      # Log to progress
      progress_path = golem_dir / "progress.log"
      if progress_path.exists():
          from golem.progress import ProgressLogger
          logger = ProgressLogger(golem_dir)
          logger.log_guidance_received(req.text[:100])

      return {"ok": True, "message": "Guidance sent"}
  ```

- [ ] **Step 3: Commit**

  ```bash
  git add src/golem/ui.py
  git commit -m "feat: add POST /api/guidance endpoint to UI server"
  ```

#### Completion Gate
```bash
cd F:/Tools/Projects/golem-cli

# 1. GuidanceRequest model exists at module level
python -c "from golem.ui import GuidanceRequest; assert GuidanceRequest.__module__ == 'golem.ui'" && echo "MODEL: PASS" || echo "MODEL: FAIL"

# 2. Route /api/guidance is registered
python -c "
from golem.ui import create_app
app = create_app()
paths = {r.path for r in app.routes}
assert '/api/guidance' in paths, f'missing from: {paths}'
print('ROUTE: PASS')
" || echo "ROUTE: FAIL"

# 3. Endpoint returns 400 when no run is in progress
python -c "
import golem.ui as ui_module
from golem.ui import create_app
from fastapi.testclient import TestClient
ui_module.current_process = None
app = create_app()
client = TestClient(app, raise_server_exceptions=False)
resp = client.post('/api/guidance', json={'text': 'hello'})
assert resp.status_code == 400, f'expected 400, got {resp.status_code}: {resp.text}'
print('IDLE_REJECT: PASS')
" || echo "IDLE_REJECT: FAIL"

# 4. Endpoint rejects empty text
python -c "
import golem.ui as ui_module
from golem.ui import create_app
from fastapi.testclient import TestClient
ui_module.current_process = None
app = create_app()
client = TestClient(app, raise_server_exceptions=False)
resp = client.post('/api/guidance', json={'text': ''})
assert resp.status_code in (400, 422), f'expected 400/422, got {resp.status_code}: {resp.text}'
print('EMPTY_REJECT: PASS')
" || echo "EMPTY_REJECT: FAIL"
```

Expected output:
```
MODEL: PASS
ROUTE: PASS
IDLE_REJECT: PASS
EMPTY_REJECT: PASS
```

If any check fails, fix the issue and re-run ALL checks before proceeding.

---

### Task 3: Add `## Operator Guidance` section to `tech_lead.md`

**Files:**
- Modify: `src/golem/prompts/tech_lead.md`

- [ ] **Step 1: Add operator guidance section**

  After the `## MCP Tool Discipline` section (after line 53 in the current file), insert the following new section:

  ```markdown
  ## Operator Guidance

  The operator may send guidance during your run via the ticket system.
  Before starting each new phase (after reading plans, after dispatching
  Junior Devs, after reviewing work), call `mcp__golem__list_tickets`
  and check for any ticket with `type=guidance` and `status=pending`.

  If you find a pending guidance ticket:
  1. Read it fully with `mcp__golem__read_ticket`
  2. Factor the operator's guidance into your next decision
  3. Update the ticket to `acknowledged` with a note confirming receipt

  Operator guidance takes priority over your default approach — the
  operator has context you do not have.

  ---
  ```

- [ ] **Step 2: Commit**

  ```bash
  git add src/golem/prompts/tech_lead.md
  git commit -m "feat: add operator guidance polling instruction to tech_lead.md"
  ```

#### Completion Gate
```bash
cd F:/Tools/Projects/golem-cli

# 1. Section header exists
python -c "
content = open('src/golem/prompts/tech_lead.md', encoding='utf-8').read()
assert '## Operator Guidance' in content, 'missing section header'
print('SECTION: PASS')
" || echo "SECTION: FAIL"

# 2. Polling instruction exists
python -c "
content = open('src/golem/prompts/tech_lead.md', encoding='utf-8').read()
assert 'mcp__golem__list_tickets' in content, 'missing list_tickets call'
assert 'type=guidance' in content, 'missing type=guidance filter'
assert 'status=pending' in content, 'missing status=pending filter'
assert 'acknowledged' in content, 'missing acknowledged status'
print('CONTENT: PASS')
" || echo "CONTENT: FAIL"

# 3. Priority note exists
python -c "
content = open('src/golem/prompts/tech_lead.md', encoding='utf-8').read()
assert 'takes priority' in content, 'missing priority note'
print('PRIORITY: PASS')
" || echo "PRIORITY: FAIL"
```

Expected output:
```
SECTION: PASS
CONTENT: PASS
PRIORITY: PASS
```

If any check fails, fix the issue and re-run ALL checks before proceeding.

---

### Task 4: Add guidance panel to `ui_template.html`

**Files:**
- Modify: `src/golem/ui_template.html`

- [ ] **Step 1: Add CSS for the guidance panel**

  After the `#error-banner.visible` rule (around line 302), add:

  ```css
  /* -----------------------------------------------------------------------
     Guidance Panel (shown during running state)
  ----------------------------------------------------------------------- */
  #guidance-panel {
    display: none;
    padding: 6px 16px;
    background: var(--panel);
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
  }

  #guidance-panel.visible {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  #guidance-input {
    flex: 1;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 4px;
    color: var(--body-text);
    font-family: var(--font-mono);
    font-size: 11px;
    padding: 5px 10px;
    outline: none;
    min-width: 0;
    transition: border-color 0.15s ease;
  }

  #guidance-input::placeholder {
    color: var(--dim-text);
  }

  #guidance-input:focus {
    border-color: var(--accent-end);
  }

  #guidance-send {
    background: transparent;
    border: 1px solid var(--border-bright);
    border-radius: 4px;
    color: var(--muted-light);
    font-family: var(--font-mono);
    font-size: 11px;
    letter-spacing: 0.05em;
    padding: 5px 14px;
    cursor: pointer;
    white-space: nowrap;
    flex-shrink: 0;
    transition: border-color 0.15s ease, color 0.15s ease, background 0.15s ease;
  }

  #guidance-send:hover {
    border-color: var(--muted);
    color: var(--body-text);
    background: rgba(255, 255, 255, 0.03);
  }
  ```

- [ ] **Step 2: Add guidance panel HTML element**

  After the `<div id="error-banner"></div>` element (line 728), add:

  ```html
  <!-- =====================================================================
       GUIDANCE PANEL (shown during running state)
  ====================================================================== -->
  <div id="guidance-panel">
    <input type="text" id="guidance-input" placeholder="Send guidance to Tech Lead..." autocomplete="off">
    <button id="guidance-send" onclick="sendGuidance()">SEND</button>
  </div>
  ```

- [ ] **Step 3: Add `sendGuidance()` and `updateGuidancePanel()` JavaScript functions**

  Before the closing `</script>` tag (at the end of the script block), add:

  ```javascript
  // -------------------------------------------------------------------------
  // Operator Guidance
  // -------------------------------------------------------------------------
  function updateGuidancePanel(state) {
    const panel = document.getElementById('guidance-panel');
    if (!panel) return;
    if (state === 'running') {
      panel.classList.add('visible');
    } else {
      panel.classList.remove('visible');
      const input = document.getElementById('guidance-input');
      if (input) input.value = '';
    }
  }

  function sendGuidance() {
    const input = document.getElementById('guidance-input');
    if (!input) return;
    const text = input.value.trim();
    if (!text) return;

    fetch('/api/guidance', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({text}),
    })
    .then(r => r.json())
    .then(data => {
      if (data.ok) {
        input.value = '';
        addLog({type: 'log', data: {content: '[OPERATOR] Guidance sent: ' + text, level: 'info'}});
      } else {
        addLog({type: 'log', data: {content: '[ERROR] Guidance rejected: ' + (data.error || 'unknown'), level: 'error'}});
      }
    })
    .catch(err => addLog({type: 'log', data: {content: '[ERROR] Failed to send guidance: ' + err, level: 'error'}}));
  }

  document.addEventListener('DOMContentLoaded', function() {
    const guidanceInput = document.getElementById('guidance-input');
    if (guidanceInput) {
      guidanceInput.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') sendGuidance();
      });
    }
  });
  ```

- [ ] **Step 4: Call `updateGuidancePanel(state)` from `setState()`**

  In the existing `setState(state, detail)` function, add `updateGuidancePanel(state);` as the first statement of the function body, before the `currentState = state;` line.

- [ ] **Step 5: Commit**

  ```bash
  git add src/golem/ui_template.html
  git commit -m "feat: add guidance panel to UI dashboard (visible during running state)"
  ```

#### Completion Gate
```bash
cd F:/Tools/Projects/golem-cli

# 1. Guidance panel HTML element exists
python -c "
content = open('src/golem/ui_template.html', encoding='utf-8').read()
assert 'id=\"guidance-panel\"' in content, 'missing guidance-panel div'
assert 'id=\"guidance-input\"' in content, 'missing guidance-input'
assert 'id=\"guidance-send\"' in content, 'missing guidance-send button'
print('HTML: PASS')
" || echo "HTML: FAIL"

# 2. JavaScript functions exist
python -c "
content = open('src/golem/ui_template.html', encoding='utf-8').read()
assert 'function sendGuidance()' in content, 'missing sendGuidance function'
assert 'function updateGuidancePanel(' in content, 'missing updateGuidancePanel function'
assert \"fetch('/api/guidance'\" in content, 'missing fetch call'
print('JS: PASS')
" || echo "JS: FAIL"

# 3. setState calls updateGuidancePanel
python -c "
content = open('src/golem/ui_template.html', encoding='utf-8').read()
assert 'updateGuidancePanel(state)' in content, 'setState must call updateGuidancePanel(state)'
print('STATE_HOOK: PASS')
" || echo "STATE_HOOK: FAIL"

# 4. Enter key support exists
python -c "
content = open('src/golem/ui_template.html', encoding='utf-8').read()
assert 'Enter' in content and 'sendGuidance' in content, 'missing Enter key handler'
print('ENTER_KEY: PASS')
" || echo "ENTER_KEY: FAIL"

# 5. CSS for guidance panel exists
python -c "
content = open('src/golem/ui_template.html', encoding='utf-8').read()
assert '#guidance-panel' in content, 'missing #guidance-panel CSS'
assert '#guidance-input' in content, 'missing #guidance-input CSS'
print('CSS: PASS')
" || echo "CSS: FAIL"
```

Expected output:
```
HTML: PASS
JS: PASS
STATE_HOOK: PASS
ENTER_KEY: PASS
CSS: PASS
```

If any check fails, fix the issue and re-run ALL checks before proceeding.

---

### Task 5: Write tests for `test_progress.py`

**Files:**
- Modify: `tests/test_progress.py`

- [ ] **Step 1: Add `test_log_guidance_received` test**

  Append this test to `tests/test_progress.py`:

  ```python
  def test_log_guidance_received() -> None:
      with tempfile.TemporaryDirectory() as tmpdir:
          logger = ProgressLogger(Path(tmpdir))
          logger.log_guidance_received("adjust scope")
          content = (Path(tmpdir) / "progress.log").read_text(encoding="utf-8")
          assert "GUIDANCE_RECEIVED" in content
          assert "adjust scope" in content
  ```

- [ ] **Step 2: Commit**

  ```bash
  git add tests/test_progress.py
  git commit -m "test: add test_log_guidance_received to test_progress.py"
  ```

#### Completion Gate
```bash
cd F:/Tools/Projects/golem-cli

# 1. Test function exists
python -c "
content = open('tests/test_progress.py', encoding='utf-8').read()
assert 'def test_log_guidance_received' in content, 'missing test'
assert 'GUIDANCE_RECEIVED' in content, 'missing assert on GUIDANCE_RECEIVED'
print('TEST_EXISTS: PASS')
" || echo "TEST_EXISTS: FAIL"

# 2. Test passes
uv run pytest tests/test_progress.py::test_log_guidance_received -v 2>&1 | tail -5 && echo "TEST_PASS: PASS" || echo "TEST_PASS: FAIL"
```

Expected output:
```
TEST_EXISTS: PASS
...
PASSED
TEST_PASS: PASS
```

If any check fails, fix the issue and re-run ALL checks before proceeding.

---

### Task 6: Write tests for `test_ui.py`

**Files:**
- Modify: `tests/test_ui.py`

- [ ] **Step 1: Add guidance endpoint tests**

  Append the following tests to `tests/test_ui.py`. These tests cover all four acceptance criteria scenarios for the guidance endpoint:

  ```python
  # ---------------------------------------------------------------------------
  # POST /api/guidance — operator guidance endpoint
  # ---------------------------------------------------------------------------


  def test_guidance_endpoint_rejects_when_idle(client: TestClient) -> None:
      """POST /api/guidance with no running process must return 400."""
      # current_process is None (reset by autouse fixture)
      resp = client.post("/api/guidance", json={"text": "please slow down"})
      assert resp.status_code == 400
      assert "No run" in resp.json().get("error", "")


  def test_guidance_endpoint_rejects_empty_text(client: TestClient) -> None:
      """POST /api/guidance with empty text must return 422."""
      resp = client.post("/api/guidance", json={"text": ""})
      # Empty text rejected either by Pydantic (422) or by endpoint logic (422)
      assert resp.status_code in (400, 422)


  def test_guidance_endpoint_creates_ticket(tmp_path: Path) -> None:
      """POST /api/guidance with a running process must create a guidance ticket."""
      import asyncio
      from unittest.mock import MagicMock

      import golem.ui as ui_module
      from golem.tickets import TicketStore
      from golem.ui import create_app

      # Set up .golem dir with tickets subdir
      golem_dir = tmp_path / ".golem"
      (golem_dir / "tickets").mkdir(parents=True)
      # Create progress.log so the logger path check passes
      (golem_dir / "progress.log").write_text("", encoding="utf-8")

      # Simulate running process
      mock_proc = MagicMock()
      mock_proc.returncode = None
      ui_module.current_process = mock_proc  # type: ignore[assignment]
      ui_module.current_cwd = str(tmp_path)

      app = create_app()
      client = TestClient(app, raise_server_exceptions=True)
      resp = client.post("/api/guidance", json={"text": "focus on the auth module"})

      # Restore state
      ui_module.current_process = None
      ui_module.current_cwd = None

      assert resp.status_code == 200
      assert resp.json().get("ok") is True

      # Verify ticket was created
      store = TicketStore(golem_dir / "tickets")
      tickets = asyncio.get_event_loop().run_until_complete(store.list_tickets())
      guidance_tickets = [t for t in tickets if t.type == "guidance"]
      assert len(guidance_tickets) == 1
      assert guidance_tickets[0].status == "pending"
      assert guidance_tickets[0].assigned_to == "tech_lead"


  def test_guidance_endpoint_appends_to_existing(tmp_path: Path) -> None:
      """POST /api/guidance twice must produce one ticket with 2+ history events."""
      import asyncio
      from unittest.mock import MagicMock

      import golem.ui as ui_module
      from golem.tickets import TicketStore
      from golem.ui import create_app

      golem_dir = tmp_path / ".golem"
      (golem_dir / "tickets").mkdir(parents=True)
      (golem_dir / "progress.log").write_text("", encoding="utf-8")

      mock_proc = MagicMock()
      mock_proc.returncode = None
      ui_module.current_process = mock_proc  # type: ignore[assignment]
      ui_module.current_cwd = str(tmp_path)

      app = create_app()
      client = TestClient(app, raise_server_exceptions=True)
      client.post("/api/guidance", json={"text": "first message"})
      client.post("/api/guidance", json={"text": "second message"})

      ui_module.current_process = None
      ui_module.current_cwd = None

      store = TicketStore(golem_dir / "tickets")
      tickets = asyncio.get_event_loop().run_until_complete(store.list_tickets())
      guidance_tickets = [t for t in tickets if t.type == "guidance"]

      # Should be exactly one ticket (second call appends to existing)
      assert len(guidance_tickets) == 1
      # History: created + first update + second update = at least 3 events
      assert len(guidance_tickets[0].history) >= 3
  ```

- [ ] **Step 2: Commit**

  ```bash
  git add tests/test_ui.py
  git commit -m "test: add guidance endpoint tests to test_ui.py"
  ```

#### Completion Gate
```bash
cd F:/Tools/Projects/golem-cli

# 1. All four test functions exist
python -c "
content = open('tests/test_ui.py', encoding='utf-8').read()
tests = [
    'test_guidance_endpoint_rejects_when_idle',
    'test_guidance_endpoint_rejects_empty_text',
    'test_guidance_endpoint_creates_ticket',
    'test_guidance_endpoint_appends_to_existing',
]
for t in tests:
    assert f'def {t}' in content, f'missing: {t}'
print('TESTS_EXIST: PASS')
" || echo "TESTS_EXIST: FAIL"

# 2. All new guidance tests pass
uv run pytest tests/test_ui.py -k "guidance" -v 2>&1 | tail -10 && echo "TESTS_PASS: PASS" || echo "TESTS_PASS: FAIL"
```

Expected output:
```
TESTS_EXIST: PASS
...
4 passed
TESTS_PASS: PASS
```

If any check fails, fix the issue and re-run ALL checks before proceeding.

---

## Phase Completion Gate

Run this after completing ALL tasks to verify the full feature is correctly implemented.

```bash
cd F:/Tools/Projects/golem-cli

echo "=== Task 1: progress.py ==="
python -c "from golem.progress import ProgressLogger; assert hasattr(ProgressLogger, 'log_guidance_received')" && echo "PROGRESS_METHOD: PASS" || echo "PROGRESS_METHOD: FAIL"

echo "=== Task 2: ui.py ==="
python -c "from golem.ui import GuidanceRequest; assert GuidanceRequest.__module__ == 'golem.ui'" && echo "UI_MODEL: PASS" || echo "UI_MODEL: FAIL"
python -c "from golem.ui import create_app; app = create_app(); paths = {r.path for r in app.routes}; assert '/api/guidance' in paths" && echo "UI_ROUTE: PASS" || echo "UI_ROUTE: FAIL"

echo "=== Task 3: tech_lead.md ==="
python -c "
c = open('src/golem/prompts/tech_lead.md', encoding='utf-8').read()
assert '## Operator Guidance' in c
assert 'mcp__golem__list_tickets' in c
assert 'type=guidance' in c
assert 'acknowledged' in c
" && echo "PROMPT: PASS" || echo "PROMPT: FAIL"

echo "=== Task 4: ui_template.html ==="
python -c "
c = open('src/golem/ui_template.html', encoding='utf-8').read()
assert 'id=\"guidance-panel\"' in c
assert 'function sendGuidance()' in c
assert 'function updateGuidancePanel(' in c
assert 'updateGuidancePanel(state)' in c
assert \"fetch('/api/guidance'\" in c
assert 'Enter' in c
" && echo "TEMPLATE: PASS" || echo "TEMPLATE: FAIL"

echo "=== Task 5: test_progress.py ==="
uv run pytest tests/test_progress.py::test_log_guidance_received -v 2>&1 | grep -E "PASSED|FAILED|ERROR" | head -3

echo "=== Task 6: test_ui.py guidance tests ==="
uv run pytest tests/test_ui.py -k "guidance" -v 2>&1 | grep -E "PASSED|FAILED|ERROR" | head -10

echo "=== Full test suite (no regressions) ==="
uv run pytest --tb=no -q 2>&1 | tail -5
```

Expected output:
```
=== Task 1: progress.py ===
PROGRESS_METHOD: PASS
=== Task 2: ui.py ===
UI_MODEL: PASS
UI_ROUTE: PASS
=== Task 3: tech_lead.md ===
PROMPT: PASS
=== Task 4: ui_template.html ===
TEMPLATE: PASS
=== Task 5: test_progress.py ===
PASSED
=== Task 6: test_ui.py guidance tests ===
PASSED (x4)
=== Full test suite (no regressions) ===
264 passed in ...
```

If any check fails, fix the issue and re-run ALL checks before proceeding.

---

## Acceptance Criteria

- [ ] `POST /api/guidance` creates or appends to a guidance ticket
- [ ] Guidance ticket has `type="guidance"`, `status="pending"`, `assigned_to="tech_lead"`
- [ ] Multiple guidance messages accumulate as history events on the same ticket
- [ ] `GUIDANCE_RECEIVED` event is logged to progress.log
- [ ] Tech Lead prompt includes guidance polling instruction at phase boundaries
- [ ] UI dashboard shows guidance input panel during running state
- [ ] Guidance input is hidden during idle/done/error states
- [ ] Enter key submits guidance
- [ ] Sent guidance appears in console log
- [ ] Endpoint returns 400 when no run is in progress
- [ ] All new tests pass
- [ ] Existing test suite passes (no regressions)
