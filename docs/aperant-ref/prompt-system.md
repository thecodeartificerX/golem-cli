# Aperant Prompt System — Reference

Source files:
- `apps/desktop/src/main/ai/prompts/prompt-loader.ts`
- `apps/desktop/src/main/ai/prompts/subtask-prompt-generator.ts`
- `apps/desktop/src/main/ai/prompts/types.ts`

---

## Overview

The prompt system loads `.md` prompt files from disk, injects dynamic context
(project location, worktree isolation warnings, recovery hints, project
instructions), and generates focused per-subtask prompts that are ~80% smaller
than a single mega-prompt. Each subtask gets ~100 lines of targeted context
instead of a 900-line generic prompt.

---

## 1. Prompt File Loading

### Path Resolution

**File:** `apps/desktop/src/main\ai/prompts/prompt-loader.ts`

A module-level cache (`_resolvedPromptsDir`) avoids repeated resolution.

```ts
export function resolvePromptsDir(): string
```

**Priority order:**

1. **Production (Electron packaged):** `process.resourcesPath/prompts/` — loaded
   via dynamic `require('electron')` inside a try/catch so worker threads
   (which can't import electron) don't crash.
2. **Dev — multiple candidate traversals from `__dirname`:**
   - `__dirname/../../../../prompts` (worker thread: `out/main/ai/agent/`)
   - `__dirname/../../../prompts` (worker thread dev: `src/main/ai/agent/`)
   - `__dirname/../../prompts` (2 levels from `src/main/ai/prompts/`)
   - `__dirname/../prompts`
   - `__dirname/prompts`
   - `__dirname/../../../../../apps/desktop/prompts` (repo root traversal)
   - `__dirname/../../../../apps/desktop/prompts`

Each candidate is validated by checking if `planner.md` exists in it. First
match wins. Falls back to `candidateBases[0]` if none found, so errors surface
at read time.

### Core Loader Functions

```ts
export function loadPrompt(promptName: string): string
```
Loads `<promptsDir>/<promptName>.md`. Throws with a descriptive error including
the resolved directory if the file is missing. `promptName` supports
subdirectory notation (e.g., `"mcp_tools/electron_validation"`).

```ts
export function tryLoadPrompt(promptName: string): string | null
```
Wraps `loadPrompt`, returns `null` on any error instead of throwing.

### Expected Prompt Files (startup validation)

The following filenames are validated at startup via `validatePromptFiles()`:

```
planner.md, coder.md, coder_recovery.md, followup_planner.md,
qa_reviewer.md, qa_fixer.md, spec_gatherer.md, spec_researcher.md,
spec_writer.md, spec_critic.md, complexity_assessor.md, validation_fixer.md
```

```ts
export function validatePromptFiles(): PromptValidationResult
// Returns: { valid: boolean, missingFiles: string[], promptsDir: string }
```

---

## 2. Project Instructions Loading

```ts
export async function loadProjectInstructions(
  projectDir: string
): Promise<ProjectInstructionsResult | null>

export interface ProjectInstructionsResult {
  content: string;
  source: string; // e.g., "AGENTS.md" or "CLAUDE.md"
}
```

Tries candidates in order: `AGENTS.md`, `agents.md`, `CLAUDE.md`, `claude.md`.
Returns the first found, trimmed. `AGENTS.md` is the canonical provider-agnostic
file; `CLAUDE.md` is a backward-compatibility fallback. Only one file is ever
loaded.

```ts
// Deprecated wrappers kept for backward compat:
export async function loadClaudeMd(projectDir: string): Promise<string | null>
export async function loadAgentsMd(projectDir: string): Promise<string | null>
```

---

## 3. Context Injection

```ts
export function injectContext(
  promptTemplate: string,
  context: PromptContext
): string
```

Assembles sections in this order, joined with no extra separator:

1. **SPEC LOCATION header** — only if `context.specDir` is set; lists paths for
   `spec.md`, `implementation_plan.json`, `build-progress.txt`, `qa_report.md`,
   `QA_FIX_REQUEST.md`, and `context.projectDir`.
2. **Recovery context** — raw string prepended before human input.
3. **Human input** — wrapped in `## HUMAN INPUT (READ THIS FIRST!)` with
   instruction to delete `HUMAN_INPUT.md` after addressing it.
4. **Project instructions** — wrapped in `## PROJECT INSTRUCTIONS` + `---`.
5. **Base prompt template** — appended last.

The spec location header builder is private:

```ts
function buildSpecLocationHeader(context: PromptContext): string
```

---

## 4. QA Tools Section (Capability-Gated)

```ts
export function getQaToolsSection(capabilities: ProjectCapabilities): string
```

Selects MCP tool documentation `.md` files based on detected capabilities and
concatenates them with `---` separators under `## PROJECT-SPECIFIC VALIDATION
TOOLS`. Returns empty string if no capabilities match or no tool files load.

Capability-to-file mapping:

| Capability | Loaded file |
|---|---|
| `is_electron` | `mcp_tools/electron_validation.md` |
| `is_tauri` | `mcp_tools/tauri_validation.md` |
| `is_web_frontend` (and NOT electron) | `mcp_tools/puppeteer_browser.md` |
| `has_database` | `mcp_tools/database_validation.md` |
| `has_api` | `mcp_tools/api_validation.md` |

---

## 5. Base Branch Detection

```ts
export function detectBaseBranch(specDir: string, projectDir: string): string
```

Priority:
1. `task_metadata.json` in `specDir` — reads `baseBranch` field.
2. `DEFAULT_BRANCH` environment variable — verified via `git rev-parse --verify`.
3. Auto-detect by trying `git rev-parse --verify` on `main`, `master`, `develop`
   in order.
4. Hard fallback: `"main"`.

Branch names are validated with:

```ts
function validateBranchName(branch: string | null | undefined): string | null
// Rules: non-empty, <= 255 chars, contains [a-zA-Z0-9], only [A-Za-z0-9._/-]
```

---

## 6. ProjectCapabilities Detection

### Interface

```ts
// File: apps/desktop/src/main/ai/prompts/types.ts

export interface ProjectCapabilities {
  is_electron: boolean;
  is_tauri: boolean;
  is_expo: boolean;
  is_react_native: boolean;
  is_web_frontend: boolean;
  is_nextjs: boolean;
  is_nuxt: boolean;
  has_api: boolean;
  has_database: boolean;
}
```

### Loading and Detection

```ts
export function loadProjectIndex(projectDir: string): Record<string, unknown>
// Reads: <projectDir>/.auto-claude/project_index.json
// Returns {} on missing or parse error.

export function detectProjectCapabilities(
  projectIndex: Record<string, unknown>
): ProjectCapabilities
```

Detection logic reads `projectIndex.services` (accepts both array and
object-of-services). For each service it unions `dependencies` and
`dev_dependencies` into a lowercase set and reads `framework`.

Detection rules:

- **Electron:** `deps.has('electron')` OR any dep starting with `@electron`
- **Tauri:** `deps.has('@tauri-apps/api')` OR `deps.has('tauri')`
- **Expo:** `deps.has('expo')`
- **React Native:** `deps.has('react-native')`
- **Web frontend:** framework in `{react, vue, svelte, angular, solid}` OR
  `next`/`next.js`/`nextjs`/`deps.has('next')` (sets `is_nextjs` too) OR
  `nuxt`/`nuxt.js`/`deps.has('nuxt')` (sets `is_nuxt` too) OR `deps.has('vite')`
  (when not electron)
- **API:** `service.api` exists and has a `routes` field
- **Database:** `service.database` is truthy OR deps contain one of
  `{prisma, drizzle-orm, typeorm, sequelize, mongoose, sqlalchemy, alembic,
  django, peewee}`

---

## 7. PromptContext Interface

```ts
// File: apps/desktop/src/main/ai/prompts/types.ts

export interface PromptContext {
  specDir: string;           // Absolute path to spec directory
  projectDir: string;        // Absolute path to project root
  projectInstructions?: string | null;  // From AGENTS.md or CLAUDE.md
  baseBranch?: string;       // e.g., "main", "develop"
  humanInput?: string | null;           // From HUMAN_INPUT.md
  recoveryContext?: string | null;      // From attempt_history.json
  subtask?: SubtaskPromptInfo;          // For targeted coder prompts
  attemptCount?: number;     // 0 = first try
  recoveryHints?: string[];  // Hints from previous failed attempts
  planningRetryContext?: string;        // Injected on replan after validation failure
}
```

---

## 8. Worktree Isolation Detection and Warning Injection

### Detection

```ts
// File: apps/desktop/src/main/ai/prompts/subtask-prompt-generator.ts

const WORKTREE_PATH_PATTERNS = [
  /[/\\]\.auto-claude[/\\]worktrees[/\\]tasks[/\\]/,
  /[/\\]\.auto-claude[/\\]github[/\\]pr[/\\]worktrees[/\\]/,
  /[/\\]\.worktrees[/\\]/,
];

function detectWorktreeIsolation(projectDir: string): [boolean, string | null]
// Returns [isWorktree, parentProjectPath]
// parentProjectPath = everything before the matched pattern
```

### Warning Generation

```ts
export function generateWorktreeIsolationWarning(
  projectDir: string,
  parentProjectPath: string,
): string
```

Produces a `## ISOLATED WORKTREE - CRITICAL` block containing:
- Current location (`projectDir`)
- Forbidden path (`parentProjectPath`)
- 3 rules (never `cd` to parent, never absolute parent paths, all files
  accessible via relative paths)
- Explanation of why escape matters (wrong branch, breaks isolation)
- Code example showing correct vs wrong path usage
- Instruction to convert absolute spec paths to relative ones

### Injection into Environment Context

```ts
function generateEnvironmentContext(projectDir: string, specDir: string): string
```

Calls `detectWorktreeIsolation`. If in a worktree, prepends the isolation
warning block before the `## YOUR ENVIRONMENT` section. The environment section
also sets an `**Isolation Mode:** WORKTREE` note in that case.

The environment section always lists:
- Working directory
- Spec location (relative)
- Isolation mode (if applicable)
- Critical `pwd` reminder before any git/file op after `cd`
- Important files: `spec.md`, `implementation_plan.json`,
  `build-progress.txt`, `context.json`

---

## 9. Subtask Prompt Generation (Token Reduction Strategy)

**~80% token reduction** vs a single 900-line mega-prompt. Each subtask gets a
~100-line prompt with only what it needs.

### Interfaces

```ts
// File: apps/desktop/src/main/ai/prompts/types.ts

export interface SubtaskPromptInfo {
  id: string;
  description: string;
  phaseName?: string;
  service?: string;
  filesToCreate?: string[];
  filesToModify?: string[];
  patternsFrom?: string[];     // Reference files to study for style
  verification?: SubtaskVerification;
  status?: string;
}

export interface SubtaskVerification {
  type?: 'command' | 'api' | 'browser' | 'e2e' | 'manual';
  command?: string;
  expected?: string;
  method?: string;
  url?: string;
  body?: Record<string, unknown>;
  expected_status?: number;
  checks?: string[];
  steps?: string[];
  instructions?: string;
}

export interface SubtaskPromptConfig {
  specDir: string;
  projectDir: string;
  subtask: SubtaskPromptInfo;
  phase?: { id?: string; name?: string };
  attemptCount?: number;
  recoveryHints?: string[];
  projectInstructions?: string | null;
}

export interface SubtaskContext {
  patterns: Record<string, string>;       // Pattern file contents keyed by relative path
  filesToModify: Record<string, string>;  // Files to modify keyed by relative path
  specExcerpt?: string | null;
}
```

### Main Generator

```ts
// File: apps/desktop/src/main/ai/prompts/subtask-prompt-generator.ts

export async function generateSubtaskPrompt(
  config: SubtaskPromptConfig
): Promise<string>
```

Assembles sections in this order:

1. **Environment context** — `generateEnvironmentContext(projectDir, specDir)`
   (includes worktree warning if applicable)
2. **Header** — subtask ID, phase, service, description
3. **Retry context** — only if `attemptCount > 0`; shows attempt number, demands
   a different approach, lists `recoveryHints` as bullet points
4. **Files section** — files to modify, files to create, pattern files (study
   these first)
5. **Verification** — rendered based on `verification.type`:
   - `command`: bash block with expected output
   - `api`: curl command with method, URL, optional body, expected status
   - `browser`: URL + checklist of visual checks
   - `e2e`: numbered step list
   - `manual` / fallback: raw instructions text
6. **Instructions** — 6-step checklist: read patterns, read files to modify,
   implement, run verification, commit with message format
   `auto-claude: <id> - <description[:50]>`, update plan status
7. **Quality checklist** — 5-item before-complete checklist
8. **Project instructions** — injected if provided
9. **File context** — loaded via `loadSubtaskContext` and formatted; non-fatal
   if loading fails

### Planner Prompt Generator

```ts
export async function generatePlannerPrompt(
  config: PlannerPromptConfig
): Promise<string>

export interface PlannerPromptConfig {
  specDir: string;
  projectDir: string;
  projectInstructions?: string | null;
  planningRetryContext?: string;
  attemptCount?: number;
}
```

Sections in order:
1. `generateEnvironmentContext(projectDir, specDir)`
2. Spec location header (relative path + artifact locations)
3. Project instructions (if provided)
4. Planning retry context (if replanning)
5. `loadPrompt('planner')` — base prompt template

---

## 10. Subtask Context Loading

```ts
export async function loadSubtaskContext(
  specDir: string,
  projectDir: string,
  subtask: SubtaskPromptInfo,
  maxFileLines = 200,
): Promise<SubtaskContext>
```

Loads pattern files and files-to-modify into a `SubtaskContext` dict. Each file
is:
1. Path-traversal validated (`validateAndResolvePath` — must stay within
   `projectDir`)
2. Read with `readFileTruncated` (truncates at `maxFileLines` with a
   `... (truncated, N more lines)` note)
3. On missing files, `fuzzyFindFile` is attempted before giving up

Files that cannot be read get the placeholder `"(Could not read file)"`.

### Fuzzy File Finder

```ts
async function fuzzyFindFile(
  projectDir: string,
  targetPath: string,
): Promise<string | null>
```

When a referenced file doesn't exist at the expected path, collects up to 5000
files via breadth-first walk (max depth 8, skips `node_modules`, `.git`,
`__pycache__`, `.venv`, `venv`, `dist`, `build`, `out`, `.cache`) and finds the
best filename match using:

```ts
function stringSimilarity(a: string, b: string): number
// Returns 0–1. Threshold to accept a match: 0.6
// Exact match → 1.0
// Case-insensitive exact → 0.99
// b.includes(a) → 0.8
// a.includes(b) → 0.7
// Otherwise: 1 - (levenshteinDistance / maxLen)

function levenshteinDistance(a: string, b: string): number
// Standard DP with flat array: O(m*n) time, O(m*n) space
```

### Context Formatter

```ts
function formatContextForPrompt(context: SubtaskContext): string
```

Renders pattern files under `## Reference Files (Patterns to Follow)` and
files-to-modify under `## Current File Contents (To Modify)`, each as a fenced
code block with the relative path as the heading.

---

## 11. Recovery Hint Injection

Recovery hints appear in two places:

**In `generateSubtaskPrompt`** (section 3 — Retry context):
```
## RETRY ATTEMPT (N)

This subtask has been attempted N time(s) before without success.
You MUST use a DIFFERENT approach than previous attempts.
**Previous attempt insights:**
- <hint 1>
- <hint 2>
```

Only injected when `attemptCount > 0`. `recoveryHints` are passed in via
`SubtaskPromptConfig.recoveryHints`.

**In `injectContext`** (from `prompt-loader.ts`):
`context.recoveryContext` is a pre-formatted string injected verbatim before the
human input section. This is used for the recovery context loaded from
`attempt_history.json` in the coder flow.

---

## 12. Reimplementation Checklist

To reimplement this system in Python (or another language):

1. **Path resolver** — check `app.isPackaged` equivalent for production bundle
   path, then try multiple `__file__`-relative candidate paths, validate by
   probing `planner.md`, cache the result.

2. **`loadPrompt(name)`** — join prompts dir + `name + ".md"`, read as UTF-8,
   raise with path info on missing.

3. **`loadProjectInstructions(projectDir)`** — try `AGENTS.md`, `agents.md`,
   `CLAUDE.md`, `claude.md` in order, return first found + source filename.

4. **`injectContext(template, context)`** — assemble: spec location header,
   recovery context, human input block, project instructions block, base
   template.

5. **`detectWorktreeIsolation(projectDir)`** — regex match on 3 patterns,
   extract parent path from match index.

6. **`generateWorktreeIsolationWarning(projectDir, parentPath)`** — produce the
   `## ISOLATED WORKTREE - CRITICAL` block with rules, rationale, code
   examples.

7. **`generateEnvironmentContext(projectDir, specDir)`** — compute relative spec
   path, call worktree detection, build `## YOUR ENVIRONMENT` block.

8. **`detectProjectCapabilities(projectIndex)`** — iterate services, collect
   deps, match framework strings and dependency names to boolean flags.

9. **`getQaToolsSection(capabilities)`** — map capabilities to tool `.md` file
   names, load each, concatenate with `---` separators.

10. **`generatePlannerPrompt(config)`** — env context + spec location header +
    project instructions + retry context + base planner template.

11. **`generateSubtaskPrompt(config)`** — env context + header + retry block +
    files + verification (type-specific rendering) + instructions + quality
    checklist + project instructions + loaded file context.

12. **`loadSubtaskContext(specDir, projectDir, subtask, maxFileLines=200)`** —
    load patterns and files-to-modify with path traversal guard, truncation, and
    fuzzy fallback.

13. **`fuzzyFindFile(projectDir, targetPath)`** — BFS up to 5000 files/depth 8,
    Levenshtein similarity with 0.6 threshold.

14. **`validatePromptFiles()`** — check all 12 expected filenames exist in
    resolved prompts dir.
