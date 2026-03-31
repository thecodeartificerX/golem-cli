# Aperant Custom MCP Tools — Implementation Reference

Extracted from `F:\Tools\External\Aperant\apps\desktop\src\main\ai\tools\`.
All tool names follow the `mcp__auto-claude__*` convention.

---

## Directory Layout

```
src/main/ai/tools/
  define.ts               ← Tool.define() wrapper (Vercel AI SDK integration)
  types.ts                ← ToolContext, ToolPermission, ToolMetadata, ToolExecutionOptions
  registry.ts             ← ToolRegistry class + TOOL_* name constants
  build-registry.ts       ← buildToolRegistry() factory
  truncation.ts           ← Disk-spillover output truncation
  auto-claude/            ← Custom MCP tools for the build agent
    index.ts
    get-build-progress.ts
    get-session-context.ts
    record-discovery.ts
    record-gotcha.ts
    update-qa-status.ts
    update-subtask-status.ts
  builtin/                ← Standard filesystem/web tools
    bash.ts
    edit.ts
    glob.ts
    grep.ts
    read.ts
    write.ts
    spawn-subagent.ts
    web-fetch.ts
    web-search.ts
src/main/utils/
  json-repair.ts          ← safeParseJson() / repairJson() for LLM-produced JSON
```

---

## Core Infrastructure

### ToolContext (types.ts)

Every tool `execute` function receives this as its second argument:

```typescript
export interface ToolContext {
  cwd: string;           // Current working directory for the agent
  projectDir: string;    // Root directory of the project being worked on
  specDir: string;       // .auto-claude/specs/001-feature/ — home for implementation_plan.json
  securityProfile: SecurityProfile;
  abortSignal?: AbortSignal;
  allowedWritePaths?: string[];  // If set, Write/Edit can only write within these dirs
}
```

### ToolPermission (types.ts)

```typescript
export const ToolPermission = {
  Auto: 'auto',                    // Runs without approval
  RequiresApproval: 'requires_approval',
  ReadOnly: 'read_only',           // Safe to run automatically
} as const;
```

### DEFAULT_EXECUTION_OPTIONS (types.ts)

```typescript
export const DEFAULT_EXECUTION_OPTIONS: ToolExecutionOptions = {
  timeoutMs: 120_000,
  allowBackground: false,
};
```

### Tool.define() (define.ts)

All tools use this factory. It wraps the Vercel AI SDK `tool()` function with:
- Zod v3 input schema validation
- Security hook integration (pre-execution, skipped for ReadOnly tools)
- Write-path containment for Write/Edit tools
- Safety-net disk-spillover truncation on string outputs (100KB limit)
- `file_path` argument sanitization (strips trailing JSON artifacts like `'}},{`)

```typescript
function define<TInput extends z.ZodType, TOutput>(
  config: ToolDefinitionConfig<TInput, TOutput>,
): DefinedTool<TInput, TOutput>

// Usage:
const myTool = Tool.define({
  metadata: { name: 'MyTool', description: '...', permission: ToolPermission.Auto, executionOptions: DEFAULT_EXECUTION_OPTIONS },
  inputSchema: z.object({ ... }),
  execute: (input, context) => { ... },
});

// Bind to context for AI SDK consumption:
const aiTool = myTool.bind(toolContext);
```

### Tool Name Constants (registry.ts)

```typescript
export const TOOL_UPDATE_SUBTASK_STATUS = 'mcp__auto-claude__update_subtask_status';
export const TOOL_GET_BUILD_PROGRESS    = 'mcp__auto-claude__get_build_progress';
export const TOOL_RECORD_DISCOVERY      = 'mcp__auto-claude__record_discovery';
export const TOOL_RECORD_GOTCHA         = 'mcp__auto-claude__record_gotcha';
export const TOOL_GET_SESSION_CONTEXT   = 'mcp__auto-claude__get_session_context';
export const TOOL_UPDATE_QA_STATUS      = 'mcp__auto-claude__update_qa_status';
```

### buildToolRegistry() (build-registry.ts)

```typescript
export function buildToolRegistry(): ToolRegistry {
  const registry = new ToolRegistry();
  registry.registerTool('Read',          asDefined(readTool));
  registry.registerTool('Write',         asDefined(writeTool));
  registry.registerTool('Edit',          asDefined(editTool));
  registry.registerTool('Bash',          asDefined(bashTool));
  registry.registerTool('Glob',          asDefined(globTool));
  registry.registerTool('Grep',          asDefined(grepTool));
  registry.registerTool('WebFetch',      asDefined(webFetchTool));
  registry.registerTool('WebSearch',     asDefined(webSearchTool));
  registry.registerTool('SpawnSubagent', asDefined(spawnSubagentTool));
  return registry;
}
```

The auto-claude tools are registered separately (not in buildToolRegistry) and injected when creating a build agent session.

---

## JSON Repair Utility (utils/json-repair.ts)

Used by all tools that read `implementation_plan.json` to tolerate LLM-mangled JSON.

```typescript
// Returns parsed object or null — never throws
export function safeParseJson<T = unknown>(raw: string): T | null

// Returns repaired JSON string — throws original SyntaxError if irreparable
export function repairJson(raw: string): string
```

Repairs applied in sequence:
1. Strip markdown code fences (` ```json ... ``` `)
2. Remove trailing commas before `}` or `]`
3. Add missing commas between array/object elements (newline-separated)
4. Aggressive: add missing commas even without newlines

---

## implementation_plan.json Schema

The file lives at `{specDir}/implementation_plan.json`.

```typescript
interface ImplementationPlan {
  phases?: PlanPhase[];
  qa_signoff?: QASignoff;
  last_updated?: string;
  // Frontend XState owns: status, planStatus, reviewReason
  // NEVER write those fields from tools — races with XState state machine
}

interface PlanPhase {
  id?: string;
  phase?: number;
  name?: string;
  subtasks?: PlanSubtask[];
}

interface PlanSubtask {
  id?: string;
  subtask_id?: string;  // alternate field name — tools check both
  title?: string;
  description?: string;
  status?: 'pending' | 'in_progress' | 'completed' | 'failed';
  notes?: string;
  updated_at?: string;  // ISO timestamp, set by update_subtask_status
}

interface QASignoff {
  status: 'pending' | 'in_review' | 'approved' | 'rejected' | 'fixes_applied';
  qa_session: number;   // increments on each in_review or rejected transition
  issues_found: QAIssue[];
  tests_passed: Record<string, unknown>;
  timestamp: string;    // ISO
  ready_for_qa_revalidation: boolean;  // true when status === 'fixes_applied'
}
```

### Atomic Write Pattern

All tools that mutate `implementation_plan.json` use a write-tmp-then-rename pattern:

```typescript
function writeJsonAtomic(filePath: string, data: unknown): void {
  const tmp = `${filePath}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(data, null, 2), 'utf-8');
  fs.renameSync(tmp, filePath);
}
```

---

## Session Memory Layout

Memory files live under `{specDir}/memory/`:

```
memory/
  codebase_map.json    ← discoveries (record_discovery writes, get_session_context reads)
  gotchas.md           ← append-only gotcha log (record_gotcha writes, get_session_context reads)
  patterns.md          ← code patterns (get_session_context reads; separate tooling writes)
```

### codebase_map.json Schema

```typescript
interface CodebaseMap {
  discovered_files: Record<string, {
    description: string;
    category: string;
    discovered_at: string;  // ISO timestamp
  }>;
  last_updated: string | null;
}
```

### gotchas.md Format

Append-only markdown file:

```markdown
# Gotchas & Pitfalls

Things to watch out for in this codebase.

## [2026-03-29 14:30]
Description of gotcha here.

_Context: When this gotcha applies_
```

---

## Auto-Claude Custom Tools

### 1. get_build_progress

**File:** `auto-claude/get-build-progress.ts`
**Permission:** ReadOnly
**Input:** `{}` (no parameters)

Reads `{specDir}/implementation_plan.json` and returns a human-readable progress summary.

```typescript
export const getBuildProgressTool = Tool.define({
  metadata: {
    name: 'mcp__auto-claude__get_build_progress',
    description: 'Get the current build progress including completed subtasks, pending subtasks, and next subtask to work on.',
    permission: ToolPermission.ReadOnly,
    executionOptions: DEFAULT_EXECUTION_OPTIONS,
  },
  inputSchema: z.object({}),
  execute: (_input, context) => {
    const planFile = path.join(context.specDir, 'implementation_plan.json');
    // ...
  },
});
```

**Output format:**
```
Build Progress: 3/10 subtasks (30%)

Status breakdown:
  Completed: 3
  In Progress: 1
  Pending: 6
  Failed: 0

Phases:
  Phase 1 - Setup: 2/3
  Phase 2 - Core: 1/4

Next subtask to work on:
  ID: subtask-003
  Phase: Phase 1 - Setup
  Description: Implement the configuration loader
```

When all complete: appends `All subtasks completed! Build is ready for QA.`

---

### 2. update_subtask_status

**File:** `auto-claude/update-subtask-status.ts`
**Permission:** Auto (no approval needed)

```typescript
const inputSchema = z.object({
  subtask_id: z.string().describe('ID of the subtask to update'),
  status: z.enum(['pending', 'in_progress', 'completed', 'failed']),
  notes: z.string().optional().describe('Optional notes about the completion or failure'),
});

export const updateSubtaskStatusTool = Tool.define({
  metadata: {
    name: 'mcp__auto-claude__update_subtask_status',
    description: 'Update the status of a subtask in implementation_plan.json. Use this when completing or starting a subtask.',
    permission: ToolPermission.Auto,
    executionOptions: DEFAULT_EXECUTION_OPTIONS,
  },
  inputSchema,
  execute: (input, context) => {
    const planFile = path.join(context.specDir, 'implementation_plan.json');
    // 1. safeParseJson the file
    // 2. Iterate phases[].subtasks[]; match subtask.id ?? subtask.subtask_id === subtask_id
    // 3. Set subtask.status, subtask.notes (if provided), subtask.updated_at = ISO timestamp
    // 4. Set plan.last_updated = ISO timestamp
    // 5. writeJsonAtomic
  },
});
```

**Key implementation detail:** The `updateSubtaskInPlan` helper checks both `subtask.id` and `subtask.subtask_id` for matching:

```typescript
function updateSubtaskInPlan(plan, subtaskId, status, notes): boolean {
  for (const phase of plan.phases ?? []) {
    for (const subtask of phase.subtasks ?? []) {
      const id = subtask.id ?? subtask.subtask_id;  // dual-field check
      if (id === subtaskId) {
        subtask.status = status;
        if (notes) subtask.notes = notes;
        subtask.updated_at = new Date().toISOString();
        plan.last_updated = new Date().toISOString();
        return true;
      }
    }
  }
  return false;
}
```

---

### 3. get_session_context

**File:** `auto-claude/get-session-context.ts`
**Permission:** ReadOnly
**Input:** `{}` (no parameters)

Reads all three memory files and returns combined context. Designed to be called at session start.

```typescript
export const getSessionContextTool = Tool.define({
  metadata: {
    name: 'mcp__auto-claude__get_session_context',
    description: 'Get context from previous sessions including codebase discoveries, gotchas, and patterns. Call this at the start of a session to pick up where the last session left off.',
    permission: ToolPermission.ReadOnly,
    executionOptions: DEFAULT_EXECUTION_OPTIONS,
  },
  inputSchema: z.object({}),
  execute: (_input, context) => {
    const memoryDir = path.join(context.specDir, 'memory');
    // Reads: codebase_map.json (max 20 entries), gotchas.md (last 1000 chars), patterns.md (last 1000 chars)
    // Returns combined markdown or 'No session context available yet.'
  },
});
```

**Limits applied to prevent context flooding:**
- `codebase_map.json`: up to 20 `discovered_files` entries
- `gotchas.md`: last 1000 characters only
- `patterns.md`: last 1000 characters only

**Output format:**
```markdown
## Codebase Discoveries
- `src/config/index.ts`: Main configuration module
- `src/db/client.ts`: Prisma database client singleton

## Gotchas
[last 1000 chars of gotchas.md]

## Patterns
[last 1000 chars of patterns.md]
```

---

### 4. record_discovery

**File:** `auto-claude/record-discovery.ts`
**Permission:** Auto

```typescript
const inputSchema = z.object({
  file_path: z.string().describe('Path to the file or module being documented'),
  description: z.string().describe('What was discovered about this file or module'),
  category: z.string().optional().describe('Category: "api", "config", "ui", "general"'),
});

export const recordDiscoveryTool = Tool.define({
  metadata: {
    name: 'mcp__auto-claude__record_discovery',
    description: 'Record a codebase discovery to session memory. Use this when you learn something important about the codebase structure or behavior.',
    permission: ToolPermission.Auto,
    executionOptions: DEFAULT_EXECUTION_OPTIONS,
  },
  inputSchema,
  execute: (input, context) => {
    const memoryDir = path.join(context.specDir, 'memory');
    // fs.mkdirSync(memoryDir, { recursive: true })
    // Read-or-create codebase_map.json via safeParseJson
    // codebaseMap.discovered_files[file_path] = { description, category, discovered_at: ISO }
    // codebaseMap.last_updated = ISO
    // writeJsonAtomic (tmp + rename)
  },
});
```

**Codebase map entry:**
```json
{
  "src/config/index.ts": {
    "description": "Main configuration module, reads .env via dotenv",
    "category": "config",
    "discovered_at": "2026-03-29T14:30:00.000Z"
  }
}
```

---

### 5. record_gotcha

**File:** `auto-claude/record-gotcha.ts`
**Permission:** Auto

```typescript
const inputSchema = z.object({
  gotcha: z.string().describe('Description of the gotcha or pitfall to record'),
  context: z.string().optional().describe('Additional context about when this gotcha applies'),
});

export const recordGotchaTool = Tool.define({
  metadata: {
    name: 'mcp__auto-claude__record_gotcha',
    description: 'Record a gotcha or pitfall to avoid. Use this when you encounter something that future sessions should know about to avoid repeating mistakes.',
    permission: ToolPermission.Auto,
    executionOptions: DEFAULT_EXECUTION_OPTIONS,
  },
  inputSchema,
  execute: (input, context) => {
    const memoryDir = path.join(context.specDir, 'memory');
    // fs.mkdirSync(memoryDir, { recursive: true })
    // stat gotchasFile to detect new vs. existing (avoids double existsSync)
    // If new: write header + entry with flag 'w'
    // If existing: append entry with flag 'a'
    // Timestamp format: "YYYY-MM-DD HH:MM" (UTC)
  },
});
```

**Timestamp generation (custom, no date library):**
```typescript
const now = new Date();
const timestamp = `${now.getUTCFullYear()}-${String(now.getUTCMonth() + 1).padStart(2, '0')}-${String(now.getUTCDate()).padStart(2, '0')} ${String(now.getUTCHours()).padStart(2, '0')}:${String(now.getUTCMinutes()).padStart(2, '0')}`;
```

**Write strategy (new file):**
```typescript
let isNew: boolean;
try {
  const stat = fs.statSync(gotchasFile);
  isNew = stat.size === 0;
} catch (err) {
  if ((err as NodeJS.ErrnoException).code !== 'ENOENT') throw err;
  isNew = true;
}
const header = isNew ? '# Gotchas & Pitfalls\n\nThings to watch out for in this codebase.\n' : '';
let entry = `\n## [${timestamp}]\n${gotcha}`;
if (ctx) entry += `\n\n_Context: ${ctx}_`;
entry += '\n';
fs.writeFileSync(gotchasFile, header + entry, { flag: isNew ? 'w' : 'a', encoding: 'utf-8' });
```

---

### 6. update_qa_status

**File:** `auto-claude/update-qa-status.ts`
**Permission:** Auto

**Critical constraint:** Do NOT write `plan["status"]` or `plan["planStatus"]`. The frontend XState state machine owns those fields. Writing them races with XState and corrupts `reviewReason`.

```typescript
const inputSchema = z.object({
  status: z.enum(['pending', 'in_review', 'approved', 'rejected', 'fixes_applied']),
  issues: z.string().optional().describe('JSON array of issues found, or plain text. Use [] for no issues.'),
  tests_passed: z.string().optional().describe('JSON object of test results e.g. {"unit": "pass", "e2e": "pass"}'),
});

export const updateQaStatusTool = Tool.define({
  metadata: {
    name: 'mcp__auto-claude__update_qa_status',
    description: 'Update the QA sign-off status in implementation_plan.json. Use this after completing a QA review to record the outcome.',
    permission: ToolPermission.Auto,
    executionOptions: DEFAULT_EXECUTION_OPTIONS,
  },
  inputSchema,
  execute: (input, context) => { ... },
});
```

**qa_session increment logic:**
```typescript
const current = plan.qa_signoff;
let qaSession = current?.qa_session ?? 0;
if (status === 'in_review' || status === 'rejected') {
  qaSession++;  // new review round
}

plan.qa_signoff = {
  status,
  qa_session: qaSession,
  issues_found: issues,           // parsed from issuesStr via safeParseJson
  tests_passed: testsPassed,      // parsed from testsStr via safeParseJson
  timestamp: new Date().toISOString(),
  ready_for_qa_revalidation: status === 'fixes_applied',
};
plan.last_updated = new Date().toISOString();
```

**issues parsing (tolerant):**
```typescript
let issues: QAIssue[] = [];
if (issuesStr) {
  const parsed = safeParseJson<QAIssue[]>(issuesStr);
  if (parsed !== null && Array.isArray(parsed)) {
    issues = parsed;
  } else {
    issues = [{ description: issuesStr }];  // fallback: treat as plain text
  }
}
```

---

## Builtin Tools

### Read (builtin/read.ts)

**Permission:** ReadOnly

```typescript
const inputSchema = z.object({
  file_path: z.string(),       // absolute path
  offset: z.number().optional(),
  limit: z.number().optional(),
  pages: z.string().optional(), // PDF only: "1-5", "3", "10-20"
});
```

- Default line limit: 2000; max line display length: 2000 chars (truncates with `... (truncated)`)
- Uses `fs.openSync` once per call to avoid TOCTOU race (single fd for stat + read)
- Image files (`.png .jpg .jpeg .gif .bmp .webp .svg .ico`): returns base64 as `data:<mime>;base64,...`
- PDF files: returns size hint; actual extraction deferred to external tooling
- Output format: `cat -n` style with `<lineNum>\t<content>` per line
- Returns `[File exists but is empty]` for zero-length files
- Security: `assertPathContained(file_path, context.projectDir)` — rejects escapes

---

### Write (builtin/write.ts)

**Permission:** RequiresApproval

```typescript
const inputSchema = z.object({
  file_path: z.string(),   // absolute path
  content: z.string(),
});
```

- Creates parent directories via `fs.mkdirSync(parentDir, { recursive: true })`
- Overwrites existing files without warning
- Returns `Successfully wrote ${lineCount} lines to ${file_path}`
- Security: `assertPathContained` + `allowedWritePaths` check in Tool.define()

---

### Edit (builtin/edit.ts)

**Permission:** RequiresApproval

```typescript
const inputSchema = z.object({
  file_path: z.string(),
  old_string: z.string(),
  new_string: z.string(),
  replace_all: z.boolean().default(false),
});
```

- Fails with error message if `old_string === new_string`
- Fails if `old_string` not found in file
- Fails if `old_string` appears multiple times and `replace_all` is false (reports count)
- `replace_all: true` uses `.split(old_string).join(new_string)` — all occurrences
- Single replace: `indexOf` + slice — replaces first occurrence only
- Returns count of replacements when `replace_all: true`

---

### Bash (builtin/bash.ts)

**Permission:** RequiresApproval

```typescript
const inputSchema = z.object({
  command: z.string(),
  timeout: z.number().optional(),          // max 600000ms
  run_in_background: z.boolean().optional(),
  description: z.string().optional(),
});
```

- Default timeout: 120,000ms; max: 600,000ms
- Max output length: 30,000 chars (truncates with total char count hint)
- Max buffer: 10MB
- Shell resolution: on Windows, prefers Git Bash (`bash`), falls back to `cmd.exe`; on Unix: `/bin/bash`
- Windows cmd.exe uses `/c` flag; bash uses `-c` flag
- Background mode: fire-and-forget, returns `Command started in background: ...`
- Security: validated through `bashSecurityHook(context.securityProfile)` before execution
- Output: stdout + `STDERR:\n<stderr>` + `Exit code: <n>` (only non-zero exit shown)

---

### Glob (builtin/glob.ts)

**Permission:** ReadOnly

```typescript
const inputSchema = z.object({
  pattern: z.string(),
  path: z.string().optional(),  // defaults to context.cwd
});
```

- Uses Node.js 22+ `fs.globSync` with `node_modules` and `.git` excluded
- Returns absolute paths sorted by modification time (most recent first)
- Max results: 2000 (truncates with hint to narrow pattern)
- Applies disk-spillover truncation (50KB limit) via `truncateToolOutput`
- Security: `assertPathContained(searchDir, context.projectDir)`

---

### Grep (builtin/grep.ts)

**Permission:** ReadOnly

```typescript
const inputSchema = z.object({
  pattern: z.string(),
  path: z.string().optional(),
  output_mode: z.enum(['content', 'files_with_matches', 'count']).optional(),
  context: z.number().optional(),   // lines before+after match (rg -C)
  type: z.string().optional(),      // rg --type
  glob: z.string().optional(),      // rg --glob
});
```

- Wraps `rg` (ripgrep) via `execFile`; `findExecutable('rg')` locates it on PATH
- Default mode: `files_with_matches`
- `content` mode: adds `--line-number`; with `context` param adds `-C <n>`
- Always appends: `--no-heading --color never`
- Exit code 1 with no stderr = no matches (not an error)
- Max output: 30,000 chars (truncates with char count hint)
- Security: `assertPathContained(searchPath, context.projectDir)`

---

### WebFetch (builtin/web-fetch.ts)

**Permission:** ReadOnly

```typescript
const inputSchema = z.object({
  url: z.string().url(),
  prompt: z.string(),   // describes what information to extract from the fetched content
});
```

- Timeout: 30,000ms
- Default provider: Jina Reader (`r.jina.ai`) — returns clean markdown
- Pluggable via `createBrowseProvider()` from `tools/providers`
- Output: `URL: ...\nPrompt: ...\n\n--- Fetched Content ---\n<markdown>`
- On timeout: returns `Error: Request timed out after 30000ms fetching <url>`

---

### WebSearch (builtin/web-search.ts)

**Permission:** ReadOnly

```typescript
const inputSchema = z.object({
  query: z.string().min(2),
  allowed_domains: z.array(z.string()).optional(),
  blocked_domains: z.array(z.string()).optional(),
});
```

- Timeout: 15,000ms; max results: 10; snippet length: 300 chars
- Default provider: Tavily (requires `TAVILY_API_KEY`)
- Pluggable via `createSearchProvider()` from `tools/providers`
- Output: numbered list `1. <title>\n   URL: <url>\n   <snippet>`

---

### SpawnSubagent (builtin/spawn-subagent.ts)

**Permission:** Auto
**Timeout:** 600,000ms (10 minutes)

Only available to orchestrator agent types. Subagents cannot spawn sub-subagents.

```typescript
const inputSchema = z.object({
  agent_type: z.enum([
    'complexity_assessor', 'spec_discovery', 'spec_gatherer', 'spec_researcher',
    'spec_writer', 'spec_critic', 'spec_validation',
    'planner', 'coder', 'qa_reviewer', 'qa_fixer',
  ]),
  task: z.string(),
  context: z.string().nullable(),
  expect_structured_output: z.boolean(),
});

export interface SubagentExecutor {
  spawn(params: SubagentSpawnParams): Promise<SubagentResult>;
}

export interface SubagentResult {
  text?: string;
  structuredOutput?: Record<string, unknown>;
  error?: string;
  stepsExecuted: number;
  durationMs: number;
}
```

The executor is injected via `ToolContext` extension: `(context as ToolContext & { subagentExecutor?: SubagentExecutor }).subagentExecutor`. If absent, returns a graceful error (non-orchestrator sessions degrade cleanly).

---

## Disk-Spillover Truncation (truncation.ts)

Large tool outputs spill to disk rather than truncating silently:

```typescript
export function truncateToolOutput(
  output: string,
  toolName: string,
  projectDir: string,
  maxBytes: number = 50_000,  // 50KB default; 100KB safety-net in Tool.define()
): TruncationResult

export interface TruncationResult {
  content: string;
  wasTruncated: boolean;
  originalSize: number;
  spilloverPath?: string;  // path to full output on disk
}
```

- Triggers on: `bytes > maxBytes` OR `lines > 2000`
- Spill destination: `{projectDir}/.auto-claude/tool-output/<ToolName>-<timestamp>.txt`
- Hint appended to truncated output points agent to Read the spill file
- If spill write fails: truncates without disk backup (graceful degradation)

---

## auto-claude/index.ts (Barrel Export)

```typescript
export { updateSubtaskStatusTool } from './update-subtask-status';
export { getBuildProgressTool }    from './get-build-progress';
export { recordDiscoveryTool }     from './record-discovery';
export { recordGotchaTool }        from './record-gotcha';
export { getSessionContextTool }   from './get-session-context';
export { updateQaStatusTool }      from './update-qa-status';
```

---

## Key Design Patterns

### Pattern: Atomic JSON Mutation

Every tool that writes `implementation_plan.json`:
1. Read with `safeParseJson` (repairs LLM-mangled JSON)
2. Mutate in memory
3. Write to `.tmp` file
4. `fs.renameSync(tmp, planFile)` — atomic on POSIX, best-effort on Windows

### Pattern: Memory Directory Bootstrap

Every memory-writing tool calls `fs.mkdirSync(memoryDir, { recursive: true })` before any read/write. This ensures the first call creates the directory silently.

### Pattern: Graceful Missing State

All tools return human-readable error strings (not thrown exceptions) for missing files:
- `implementation_plan.json` not found → `'No implementation plan found. Run the planner first.'`
- Memory dir absent → `'No session memory found. This appears to be the first session.'`
- Subtask not found → `'Error: Subtask \'<id>\' not found in implementation plan'`

### Pattern: XState Ownership Boundary

`update_qa_status` explicitly comments: do not write `plan["status"]` or `plan["planStatus"]`. The frontend XState task state machine owns those fields. Only `qa_signoff` is agent-writable.

### Pattern: Dual Field ID Lookup

`update_subtask_status` checks both `subtask.id` and `subtask.subtask_id` to handle variation across plan generators:
```typescript
const id = subtask.id ?? subtask.subtask_id;
```
