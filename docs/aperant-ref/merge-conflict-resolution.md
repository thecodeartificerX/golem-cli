# Aperant — Merge Conflict Resolution System

Complete implementation reference for the intent-aware merge system.
All source files are in `apps/desktop/src/main/ai/merge/`.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Core Types](#core-types) — `types.ts`
3. [Merge Pipeline](#merge-pipeline) — `orchestrator.ts`
4. [Semantic Analyzer](#semantic-analyzer) — `semantic-analyzer.ts`
5. [File Evolution Tracker](#file-evolution-tracker) — `file-evolution.ts`
6. [Conflict Detector](#conflict-detector) — `conflict-detector.ts`
7. [Auto Merger — All 8 Strategies](#auto-merger--all-8-strategies) — `auto-merger.ts`
8. [Timeline Tracker](#timeline-tracker) — `timeline-tracker.ts`
9. [MergeReport Structure](#mergereport-structure)
10. [Progress Tracking](#progress-tracking)
11. [Public API](#public-api) — `index.ts`

---

## Architecture Overview

The merge system is a five-stage pipeline:

```
file-evolution.ts         → load baseline + task diffs from git
semantic-analyzer.ts      → diff → SemanticChange[] (regex-based)
conflict-detector.ts      → FileAnalysis[] → ConflictRegion[] (80+ rules)
auto-merger.ts            → ConflictRegion → merged content (8 deterministic strategies)
orchestrator.ts           → coordinates all stages, calls AI for hard conflicts
```

The **MergeOrchestrator** is the single entry point. It delegates:
- `FileEvolutionTracker` — tracks what each task changed per file
- `SemanticAnalyzer` — converts raw before/after content into typed `SemanticChange` objects
- `ConflictDetector` — uses a rule index to determine if overlapping changes are compatible
- `AutoMerger` — applies the correct deterministic merge strategy
- An injectable `AiResolverFn` — called only when no deterministic strategy applies

Storage layout:
```
.auto-claude/
  baselines/         ← per-task baseline snapshots (<taskId>/<safe_file_name>.baseline)
  file_evolution.json ← persisted FileEvolution[] map
  merge_output/      ← writeMergedFiles() output
  merge_reports/     ← JSON report per merge run
  timelines/         ← FileTimeline[] (index.json + per-file .json)
  worktrees/<taskId> ← git worktrees
```

---

## Core Types

**File:** `apps/desktop/src/main/ai/merge/types.ts`

### ChangeType enum (35 values)

```typescript
export enum ChangeType {
  // Import changes
  ADD_IMPORT = 'add_import',
  REMOVE_IMPORT = 'remove_import',
  MODIFY_IMPORT = 'modify_import',

  // Function/method changes
  ADD_FUNCTION = 'add_function',
  REMOVE_FUNCTION = 'remove_function',
  MODIFY_FUNCTION = 'modify_function',
  RENAME_FUNCTION = 'rename_function',

  // React/JSX specific
  ADD_HOOK_CALL = 'add_hook_call',
  REMOVE_HOOK_CALL = 'remove_hook_call',
  WRAP_JSX = 'wrap_jsx',
  UNWRAP_JSX = 'unwrap_jsx',
  ADD_JSX_ELEMENT = 'add_jsx_element',
  MODIFY_JSX_PROPS = 'modify_jsx_props',

  // Variable/constant changes
  ADD_VARIABLE = 'add_variable',
  REMOVE_VARIABLE = 'remove_variable',
  MODIFY_VARIABLE = 'modify_variable',
  ADD_CONSTANT = 'add_constant',

  // Class changes
  ADD_CLASS = 'add_class',
  REMOVE_CLASS = 'remove_class',
  MODIFY_CLASS = 'modify_class',
  ADD_METHOD = 'add_method',
  REMOVE_METHOD = 'remove_method',
  MODIFY_METHOD = 'modify_method',
  ADD_PROPERTY = 'add_property',

  // Type changes (TypeScript)
  ADD_TYPE = 'add_type',
  MODIFY_TYPE = 'modify_type',
  ADD_INTERFACE = 'add_interface',
  MODIFY_INTERFACE = 'modify_interface',

  // Python specific
  ADD_DECORATOR = 'add_decorator',
  REMOVE_DECORATOR = 'remove_decorator',

  // Generic
  ADD_COMMENT = 'add_comment',
  MODIFY_COMMENT = 'modify_comment',
  FORMATTING_ONLY = 'formatting_only',
  UNKNOWN = 'unknown',
}
```

### MergeStrategy enum

```typescript
export enum MergeStrategy {
  COMBINE_IMPORTS = 'combine_imports',
  HOOKS_FIRST = 'hooks_first',
  HOOKS_THEN_WRAP = 'hooks_then_wrap',
  APPEND_STATEMENTS = 'append_statements',
  APPEND_FUNCTIONS = 'append_functions',
  APPEND_METHODS = 'append_methods',
  COMBINE_PROPS = 'combine_props',
  ORDER_BY_DEPENDENCY = 'order_by_dependency',
  ORDER_BY_TIME = 'order_by_time',
  AI_REQUIRED = 'ai_required',     // fallback: use AI resolver
  HUMAN_REQUIRED = 'human_required', // fallback: needs human
}
```

### MergeDecision enum

```typescript
export enum MergeDecision {
  AUTO_MERGED = 'auto_merged',         // deterministic strategy succeeded
  AI_MERGED = 'ai_merged',             // AI resolver was used
  NEEDS_HUMAN_REVIEW = 'needs_human_review', // AI unavailable or failed
  FAILED = 'failed',                   // error during merge
  DIRECT_COPY = 'direct_copy',         // single task, no conflicts — copy from worktree
}
```

### SemanticChange interface

```typescript
export interface SemanticChange {
  changeType: ChangeType;
  target: string;      // function name, import string, class name, etc.
  location: string;    // e.g. 'file_top', 'function:MyComponent', 'class:MyClass'
  lineStart: number;
  lineEnd: number;
  contentBefore?: string;  // old content snippet
  contentAfter?: string;   // new content snippet
  metadata: Record<string, unknown>;
}
```

The `location` field is the conflict grouping key. Changes at the same `location`
from different tasks are analyzed for compatibility.

### FileAnalysis interface

```typescript
export interface FileAnalysis {
  filePath: string;
  changes: SemanticChange[];
  functionsModified: Set<string>;
  functionsAdded: Set<string>;
  importsAdded: Set<string>;
  importsRemoved: Set<string>;
  classesModified: Set<string>;
  totalLinesChanged: number;
}
```

Created by `createFileAnalysis(filePath)`. The orchestrator populates this from
`TaskSnapshot.semanticChanges`.

### ConflictRegion interface

```typescript
export interface ConflictRegion {
  filePath: string;
  location: string;         // location key (same as SemanticChange.location)
  tasksInvolved: string[];  // taskIds that changed this location
  changeTypes: ChangeType[];
  severity: ConflictSeverity;  // NONE | LOW | MEDIUM | HIGH | CRITICAL
  canAutoMerge: boolean;
  mergeStrategy?: MergeStrategy;  // set when canAutoMerge = true
  reason: string;               // human-readable explanation
}
```

### TaskSnapshot interface

```typescript
export interface TaskSnapshot {
  taskId: string;
  taskIntent: string;
  startedAt: Date;
  completedAt?: Date;
  contentHashBefore: string;   // sha256[:16] of file before task
  contentHashAfter: string;    // sha256[:16] of file after task
  semanticChanges: SemanticChange[];
  rawDiff?: string;            // raw unified diff output from git
}
```

`taskSnapshotHasModifications(snapshot)` returns true when `semanticChanges.length > 0`
OR when `contentHashBefore !== contentHashAfter`.

### FileEvolution interface

```typescript
export interface FileEvolution {
  filePath: string;
  baselineCommit: string;
  baselineCapturedAt: Date;
  baselineContentHash: string;
  baselineSnapshotPath: string;  // relative to storageDir
  taskSnapshots: TaskSnapshot[];  // ordered by startedAt
}
```

Persisted per-file in `.auto-claude/file_evolution.json`.

### MergeResult interface

```typescript
export interface MergeResult {
  decision: MergeDecision;
  filePath: string;
  mergedContent?: string;          // final merged text (absent for NEEDS_HUMAN_REVIEW/FAILED)
  conflictsResolved: ConflictRegion[];
  conflictsRemaining: ConflictRegion[];
  aiCallsMade: number;
  tokensUsed: number;
  explanation: string;
  error?: string;
}
```

### Utility functions in types.ts

```typescript
// True when change only adds new code (no modifications/removals)
export function isAdditiveChange(change: SemanticChange): boolean

// True when two changes overlap by location string OR line range
export function overlapsWithChange(a: SemanticChange, b: SemanticChange): boolean

// True when every change in analysis is additive
export function isAdditiveOnly(analysis: FileAnalysis): boolean

// Get all location strings touched by this analysis
export function locationsChanged(analysis: FileAnalysis): Set<string>

// Filter changes to a specific location
export function getChangesAtLocation(analysis: FileAnalysis, location: string): SemanticChange[]

// sha256[:16] of content string
export function computeContentHash(content: string): string

// Convert file path to safe storage name (slashes and dots become underscores)
export function sanitizePathForStorage(filePath: string): string

// Add/update snapshot in evolution (deduplicated by taskId, kept sorted by startedAt)
export function addTaskSnapshot(evolution: FileEvolution, snapshot: TaskSnapshot): void
```

---

## Merge Pipeline

**File:** `apps/desktop/src/main/ai/merge/orchestrator.ts`

### MergeOrchestrator constructor

```typescript
export class MergeOrchestrator {
  readonly evolutionTracker: FileEvolutionTracker;
  readonly conflictDetector: ConflictDetector;
  readonly autoMerger: AutoMerger;

  constructor(options: {
    projectDir: string;
    storageDir?: string;         // default: <projectDir>/.auto-claude
    enableAi?: boolean;          // default: true
    aiResolver?: AiResolverFn;   // injectable AI call function
    dryRun?: boolean;            // default: false — skips saveReport/applyToProject
  })
}
```

### AiResolverFn type

```typescript
export type AiResolverFn = (system: string, user: string) => Promise<string>;
```

The caller provides this bridge to whatever LLM client is available. The orchestrator
constructs system + user prompts and passes them. Returns the merged file content as plain text.

### Single-task merge: `mergeTask()`

```typescript
async mergeTask(
  taskId: string,
  worktreePath?: string,   // auto-discovered if omitted from .auto-claude/worktrees/<taskId>
  targetBranch = 'main',
  progressCallback?: ProgressCallback,
): Promise<MergeReport>
```

Step-by-step execution:

1. **Find worktree** — looks in `.auto-claude/worktrees/<taskId>` and `.auto-claude/worktrees/tasks/<taskId>`
2. **`evolutionTracker.refreshFromGit(taskId, worktreePath, targetBranch)`** — discovers changed files from git diff
3. **`evolutionTracker.getTaskModifications(taskId)`** — returns `Array<[filePath, TaskSnapshot]>`
4. For each file: calls `mergeFile(filePath, [snapshot], targetBranch)` (see below)
5. If result is `DIRECT_COPY`: reads actual content from `path.join(worktreePath, filePath)`
6. Accumulates into `MergeReport` and calls `saveReport()`

### Multi-task merge: `mergeTasks()`

```typescript
async mergeTasks(
  requests: TaskMergeRequest[],  // { taskId, worktreePath?, priority }
  targetBranch = 'main',
  progressCallback?: ProgressCallback,
): Promise<MergeReport>
```

Sorted by `priority` descending before processing. All tasks' worktrees are
`refreshFromGit`-ed first, then files are processed per-file with all relevant
`TaskSnapshot[]` passed to `mergeFile()`.

### Per-file merge logic: `mergeFile()` (private)

```typescript
private async mergeFile(
  filePath: string,
  taskSnapshots: TaskSnapshot[],
  targetBranch: string,
): Promise<MergeResult>
```

Decision tree:

```
baseline content (from evolutionTracker or git show <branch>:<file>)
    |
    v
build FileAnalysis per task (from TaskSnapshot.semanticChanges)
    |
    v
conflictDetector.detectConflicts(taskAnalyses)
    |
    +-- no conflicts AND single task  -->  DIRECT_COPY
    |
    +-- all conflicts canAutoMerge AND no hard conflicts
    |       -->  autoMerger.merge(context, strategy)  -->  AUTO_MERGED
    |
    +-- hard conflicts AND enableAi AND aiResolver present
    |       -->  mergeWithAi()  -->  AI_MERGED or NEEDS_HUMAN_REVIEW
    |
    +-- hard conflicts, no AI
    |       -->  NEEDS_HUMAN_REVIEW
    |
    +-- no conflicts at all (multi-task)
            -->  DIRECT_COPY
```

The strategy comes from `autoMergeableConflicts[0].mergeStrategy`.

### AI merge: `mergeWithAi()` (module-level function)

```typescript
async function mergeWithAi(
  aiResolver: AiResolverFn,
  filePath: string,
  baselineContent: string,
  taskContents: string[],
  conflicts: ConflictRegion[],
): Promise<MergeResult>
```

System prompt: `"You are a code merge expert. ... Return ONLY the merged file content, no explanation."`

User prompt format:
```
Merge the following versions of <filePath>:

BASELINE:
```<baseline>```

TASK 1 VERSION:
```<content>```

TASK 2 VERSION:
```<content>```

CONFLICTS TO RESOLVE:
- <location>: <reason> (severity: <severity>)

Return the merged file content:
```

Returns `AI_MERGED` on success, `NEEDS_HUMAN_REVIEW` on exception or empty response.

### Preview and output methods

```typescript
// Returns a JSON-serializable preview without doing any merging
previewMerge(taskIds: string[]): Record<string, unknown>

// Write merged files to outputDir (default: .auto-claude/merge_output/)
writeMergedFiles(report: MergeReport, outputDir?: string): string[]

// Overwrite project files in-place with merged content
applyToProject(report: MergeReport): boolean
```

### Git utility functions (module-level)

```typescript
// git show <branch>:<filePath> in projectDir
function getFileFromBranch(projectDir, filePath, branch): string | undefined

// Looks for worktree at .auto-claude/worktrees/<taskId> or .auto-claude/worktrees/tasks/<taskId>
function findWorktree(projectDir, taskId): string | undefined
```

### Stats aggregation: `updateStats()`

```typescript
function updateStats(stats: MergeStats, result: MergeResult): void
// filesAutoMerged incremented for AUTO_MERGED and DIRECT_COPY
// filesAiMerged incremented for AI_MERGED
// conflictsAiResolved incremented from conflictsResolved.length when AI_MERGED
```

---

## Semantic Analyzer

**File:** `apps/desktop/src/main/ai/merge/semantic-analyzer.ts`

The analyzer converts before/after file content into `SemanticChange[]` using
language-aware regex patterns. No AST parsing — purely regex-based.

### Language-specific patterns

**Import patterns** (`getImportPattern(ext)`):

| Extension | Pattern |
|-----------|---------|
| `.py` | `/^(?:from\s+\S+\s+)?import\s+/` |
| `.js`, `.jsx`, `.ts`, `.tsx` | `/^import\s+/` |

**Function patterns** (`getFunctionPattern(ext)`):

| Extension | Pattern |
|-----------|---------|
| `.py` | `/def\s+(\w+)\s*\(/g` |
| `.js`, `.jsx` | `/(?:function\s+(\w+)\|(?:const\|let\|var)\s+(\w+)\s*=\s*(?:async\s+)?(?:function\|\([^)]*\)\s*=>))/g` |
| `.ts`, `.tsx` | `/(?:function\s+(\w+)\|(?:const\|let\|var)\s+(\w+)\s*(?::\s*\w+)?\s*=\s*(?:async\s+)?(?:function\|\([^)]*\)\s*=>))/g` |

The TS/TSX pattern has a type annotation group `(?::\s*\w+)?` that JS patterns lack.

### Diff algorithm

`parseUnifiedDiff(before, after)` computes an LCS-based diff internally:

```typescript
function computeSimpleDiff(before: string[], after: string[]): DiffOp[]
// Returns array of: 'equal' | 'insert' | 'delete' | 'replace'
// Uses O(n*m) LCS table with backtracking
```

The result feeds `DiffLine[]` arrays: `{ lineNum: number, content: string }`.

### Main analysis function

```typescript
export function analyzeWithRegex(
  filePath: string,
  before: string,
  after: string,
): FileAnalysis
```

Detection steps, in order:

1. **Import detection** — scan added/removed lines against `getImportPattern(ext)`:
   - Added import line → `ADD_IMPORT`, `location: 'file_top'`
   - Removed import line → `REMOVE_IMPORT`, `location: 'file_top'`

2. **Function addition/removal** — compare `Set<string>` of function names before vs after:
   - In after but not before → `ADD_FUNCTION`, `location: 'function:<name>'`
   - In before but not after → `REMOVE_FUNCTION`, `location: 'function:<name>'`

3. **Function modification** — for functions present in both before and after:
   - Extracts function body via `extractFunctionBody(content, funcName, ext)`
   - If bodies differ → calls `classifyFunctionModification(beforeBody, afterBody, ext)`

### Function body extraction

```typescript
function extractFunctionBody(content: string, funcName: string, ext: string): string | null
```

- **Python:** matches `def <name>(...) [-> type]:` up to the next `def` or `class`
- **JS/TS:** matches `function <name>` or `const <name> = [async] (function|(args) => {`

### Function modification classification

```typescript
function classifyFunctionModification(before: string, after: string, ext: string): ChangeType
```

Classification priority order:

1. **React hooks** — `/\buse[A-Z]\w*\s*\(/g` scan:
   - New hooks in after → `ADD_HOOK_CALL`
   - Hooks removed → `REMOVE_HOOK_CALL`

2. **JSX wrapping** — count `/<[A-Z]\w*/g` matches:
   - More in after → `WRAP_JSX`
   - Fewer in after → `UNWRAP_JSX`

3. **JSX props only** (`.jsx`, `.tsx`) — strip prop values and compare structure:
   - `before.replace(/=\{[^}]+\}|="[^"]*"/g, '=...')` — if structures equal → `MODIFY_JSX_PROPS`

4. Fallback → `MODIFY_FUNCTION`

### SemanticAnalyzer class

```typescript
export class SemanticAnalyzer {
  analyzeDiff(filePath: string, before: string, after: string): FileAnalysis
  analyzeFile(filePath: string, content: string): FileAnalysis  // before = ''
}
```

---

## File Evolution Tracker

**File:** `apps/desktop/src/main/ai/merge/file-evolution.ts`

Manages the persistence layer. Baseline snapshots are stored as plain text files.
The main evolution map is serialized as JSON.

### Tracked extensions

```typescript
export const DEFAULT_EXTENSIONS = new Set([
  '.py', '.js', '.ts', '.tsx', '.jsx',
  '.json', '.yaml', '.yml', '.toml',
  '.md', '.txt', '.html', '.css', '.scss',
  '.go', '.rs', '.java', '.kt', '.swift',
]);
```

### FileEvolutionTracker class

```typescript
export class FileEvolutionTracker {
  constructor(
    projectDir: string,
    storageDir?: string,              // default: <projectDir>/.auto-claude
    semanticAnalyzer?: SemanticAnalyzer,
  )
}
```

### captureBaselines()

```typescript
captureBaselines(
  taskId: string,
  files?: string[],   // default: all git-tracked files with DEFAULT_EXTENSIONS
  intent = '',
): Map<string, FileEvolution>
```

Iterates files, reads current content, stores to `.auto-claude/baselines/<taskId>/<safe_name>.baseline`,
creates a `TaskSnapshot` with `contentHashAfter = ''` (not yet modified).
Persists `file_evolution.json`.

### recordModification()

```typescript
recordModification(
  taskId: string,
  filePath: string,
  oldContent: string,
  newContent: string,
  rawDiff?: string,
  skipSemanticAnalysis = false,
): TaskSnapshot | undefined
```

Runs `analyzer.analyzeDiff()` unless `skipSemanticAnalysis = true`.
Updates the task's snapshot in the evolution and persists.

### refreshFromGit() — key method

```typescript
refreshFromGit(
  taskId: string,
  worktreePath: string,
  targetBranch?: string,         // auto-detected if omitted
  analyzeOnlyFiles?: Set<string>, // restrict semantic analysis to these files
): void
```

Discovers changed files in three passes to catch all states:

1. `git diff --name-only <mergeBase>..HEAD` — committed changes
2. `git diff --name-only HEAD` — unstaged changes
3. `git diff --name-only --cached HEAD` — staged but not committed

The merge base is computed with `git merge-base <branch> HEAD`. If that fails
(branch doesn't exist in repo), falls back to the project's current HEAD.

For each changed file:
- Old content: `git show <mergeBase>:<file>` (empty string if new file)
- New content: reads from `worktreePath/<file>` on disk
- Diff: `git diff <mergeBase> -- <file>`

Creates `FileEvolution` entry if missing, then calls `recordModification()`.

### Query methods

```typescript
getFileEvolution(filePath: string): FileEvolution | undefined
getBaselineContent(filePath: string): string | undefined
getTaskModifications(taskId: string): Array<[string, TaskSnapshot]>
getFilesModifiedByTasks(taskIds: string[]): Map<string, string[]>  // file -> [taskIds]
getConflictingFiles(taskIds: string[]): string[]  // files touched by 2+ tasks
getActiveTasks(): Set<string>                     // taskIds with no completedAt
getEvolutionSummary(): Record<string, unknown>
```

### Lifecycle methods

```typescript
markTaskCompleted(taskId: string): void        // sets completedAt on all snapshots
cleanupTask(taskId: string, removeBaselines = true): void  // removes snapshots + baseline dir
```

### Path handling note

`getRelativePath(filePath)` — if path is already relative (as git always outputs),
normalizes slashes only. If absolute, uses `path.relative(projectDir, ...)`.
Critical: never call `path.relative()` on a relative path — it resolves against CWD
producing incorrect traversal paths.

---

## Conflict Detector

**File:** `apps/desktop/src/main/ai/merge/conflict-detector.ts`

### CompatibilityRule interface

```typescript
export interface CompatibilityRule {
  changeTypeA: ChangeType;
  changeTypeB: ChangeType;
  compatible: boolean;
  strategy?: MergeStrategy;    // the merge strategy to use when compatible
  reason: string;
  bidirectional: boolean;      // if true, also indexes (B, A) pair
}
```

The rule index is keyed as `"<changeTypeA>::<changeTypeB>"`.
Bidirectional rules are indexed under both orderings.

### ConflictDetector class

```typescript
export class ConflictDetector {
  constructor()  // builds 80+ default rules

  detectConflicts(taskAnalyses: Map<string, FileAnalysis>): ConflictRegion[]
  analyzeCompatibility(changeA, changeB): [boolean, MergeStrategy | undefined, string]
  addRule(rule: CompatibilityRule): void  // extend at runtime
  getCompatiblePairs(): Array<[ChangeType, ChangeType, MergeStrategy]>
  explainConflict(conflict: ConflictRegion): string
}
```

### Detection algorithm

```typescript
function detectConflictsInternal(
  taskAnalyses: Map<string, FileAnalysis>,
  ruleIndex: RuleIndex,
): ConflictRegion[]
```

1. Build `locationChanges: Map<location, Array<[taskId, SemanticChange]>>`
2. For each location with 2+ changes from different tasks:
   - Call `analyzeLocationConflict()` → `ConflictRegion | null`
3. Return all non-null conflicts

`analyzeLocationConflict()` logic:
- If targets are different (different function names at same location) → `null` (compatible, no conflict)
- Otherwise: cross-product all change type pairs → look up rule → aggregate compatible/incompatible
- If no rule found for a pair → `allCompatible = false`
- Final `canAutoMerge = allCompatible`, strategy from last compatible rule found

### Severity assessment

```typescript
function assessSeverity(changeTypes: ChangeType[], changes: SemanticChange[]): ConflictSeverity
```

| Condition | Severity |
|-----------|----------|
| 2+ MODIFY_FUNCTION/METHOD/CLASS AND line ranges overlap | CRITICAL |
| Any WRAP_JSX, UNWRAP_JSX, REMOVE_FUNCTION, REMOVE_CLASS | HIGH |
| 1+ MODIFY_FUNCTION/METHOD/CLASS | MEDIUM |
| Default | LOW |

Compatible conflicts get `ConflictSeverity.NONE`.

### Complete rule table (80+ rules)

**Import rules:**

| A | B | Compatible | Strategy |
|---|---|-----------|----------|
| ADD_IMPORT | ADD_IMPORT | YES | COMBINE_IMPORTS |
| ADD_IMPORT | REMOVE_IMPORT | NO | AI_REQUIRED |
| REMOVE_IMPORT | REMOVE_IMPORT | YES | COMBINE_IMPORTS |
| ADD_IMPORT | MODIFY_IMPORT | NO | AI_REQUIRED |
| MODIFY_IMPORT | MODIFY_IMPORT | NO | AI_REQUIRED |
| ADD_IMPORT | ADD_FUNCTION | YES | COMBINE_IMPORTS |
| ADD_IMPORT | ADD_CLASS | YES | COMBINE_IMPORTS |
| ADD_IMPORT | ADD_VARIABLE | YES | COMBINE_IMPORTS |
| ADD_IMPORT | MODIFY_FUNCTION | YES | COMBINE_IMPORTS |

**Function rules:**

| A | B | Compatible | Strategy |
|---|---|-----------|----------|
| ADD_FUNCTION | ADD_FUNCTION | YES | APPEND_FUNCTIONS |
| ADD_FUNCTION | MODIFY_FUNCTION | YES | APPEND_FUNCTIONS |
| MODIFY_FUNCTION | MODIFY_FUNCTION | NO | AI_REQUIRED |
| ADD_FUNCTION | REMOVE_FUNCTION | NO | AI_REQUIRED |
| REMOVE_FUNCTION | REMOVE_FUNCTION | YES | APPEND_FUNCTIONS |
| REMOVE_FUNCTION | MODIFY_FUNCTION | NO | AI_REQUIRED |
| ADD_FUNCTION | RENAME_FUNCTION | NO | AI_REQUIRED |
| RENAME_FUNCTION | RENAME_FUNCTION | NO | AI_REQUIRED |

**React hook rules:**

| A | B | Compatible | Strategy |
|---|---|-----------|----------|
| ADD_HOOK_CALL | ADD_HOOK_CALL | YES | ORDER_BY_DEPENDENCY |
| ADD_HOOK_CALL | WRAP_JSX | YES | HOOKS_THEN_WRAP |
| ADD_HOOK_CALL | MODIFY_FUNCTION | YES | HOOKS_FIRST |
| ADD_HOOK_CALL | REMOVE_HOOK_CALL | NO | AI_REQUIRED |
| REMOVE_HOOK_CALL | REMOVE_HOOK_CALL | YES | HOOKS_FIRST |
| ADD_HOOK_CALL | ADD_FUNCTION | YES | HOOKS_FIRST |
| ADD_HOOK_CALL | ADD_VARIABLE | YES | HOOKS_FIRST |
| ADD_HOOK_CALL | MODIFY_JSX_PROPS | YES | HOOKS_FIRST |

**JSX rules:**

| A | B | Compatible | Strategy |
|---|---|-----------|----------|
| WRAP_JSX | WRAP_JSX | YES | ORDER_BY_DEPENDENCY |
| WRAP_JSX | ADD_JSX_ELEMENT | YES | APPEND_STATEMENTS |
| MODIFY_JSX_PROPS | MODIFY_JSX_PROPS | YES | COMBINE_PROPS |
| WRAP_JSX | UNWRAP_JSX | NO | AI_REQUIRED |
| UNWRAP_JSX | UNWRAP_JSX | NO | AI_REQUIRED |
| ADD_JSX_ELEMENT | ADD_JSX_ELEMENT | YES | APPEND_STATEMENTS |
| WRAP_JSX | MODIFY_FUNCTION | NO | AI_REQUIRED |

**Class/method rules:**

| A | B | Compatible | Strategy |
|---|---|-----------|----------|
| ADD_METHOD | ADD_METHOD | YES | APPEND_METHODS |
| MODIFY_METHOD | MODIFY_METHOD | NO | AI_REQUIRED |
| ADD_CLASS | MODIFY_CLASS | YES | APPEND_FUNCTIONS |
| ADD_CLASS | ADD_CLASS | YES | APPEND_FUNCTIONS |
| MODIFY_CLASS | MODIFY_CLASS | NO | AI_REQUIRED |
| REMOVE_CLASS | MODIFY_CLASS | NO | AI_REQUIRED |
| ADD_METHOD | MODIFY_METHOD | YES | APPEND_METHODS |
| REMOVE_METHOD | MODIFY_METHOD | NO | AI_REQUIRED |
| ADD_PROPERTY | ADD_PROPERTY | YES | APPEND_STATEMENTS |
| ADD_METHOD | ADD_FUNCTION | YES | APPEND_FUNCTIONS |

**Variable rules:**

| A | B | Compatible | Strategy |
|---|---|-----------|----------|
| ADD_VARIABLE | ADD_VARIABLE | YES | APPEND_STATEMENTS |
| ADD_CONSTANT | ADD_VARIABLE | YES | APPEND_STATEMENTS |
| ADD_CONSTANT | ADD_CONSTANT | YES | APPEND_STATEMENTS |
| MODIFY_VARIABLE | MODIFY_VARIABLE | NO | AI_REQUIRED |
| ADD_VARIABLE | MODIFY_VARIABLE | YES | APPEND_STATEMENTS |
| REMOVE_VARIABLE | MODIFY_VARIABLE | NO | AI_REQUIRED |
| ADD_VARIABLE | ADD_FUNCTION | YES | APPEND_STATEMENTS |
| ADD_VARIABLE | MODIFY_FUNCTION | YES | APPEND_STATEMENTS |

**TypeScript type rules:**

| A | B | Compatible | Strategy |
|---|---|-----------|----------|
| ADD_TYPE | ADD_TYPE | YES | APPEND_FUNCTIONS |
| ADD_INTERFACE | ADD_INTERFACE | YES | APPEND_FUNCTIONS |
| MODIFY_INTERFACE | MODIFY_INTERFACE | NO | AI_REQUIRED |
| ADD_TYPE | MODIFY_TYPE | YES | APPEND_FUNCTIONS |
| MODIFY_TYPE | MODIFY_TYPE | NO | AI_REQUIRED |
| ADD_INTERFACE | MODIFY_INTERFACE | YES | APPEND_FUNCTIONS |
| ADD_TYPE | ADD_INTERFACE | YES | APPEND_FUNCTIONS |
| ADD_TYPE | ADD_FUNCTION | YES | APPEND_FUNCTIONS |
| ADD_INTERFACE | ADD_FUNCTION | YES | APPEND_FUNCTIONS |

**Python decorator rules:**

| A | B | Compatible | Strategy |
|---|---|-----------|----------|
| ADD_DECORATOR | ADD_DECORATOR | YES | ORDER_BY_DEPENDENCY |
| REMOVE_DECORATOR | REMOVE_DECORATOR | YES | ORDER_BY_DEPENDENCY |
| ADD_DECORATOR | REMOVE_DECORATOR | NO | AI_REQUIRED |
| ADD_DECORATOR | MODIFY_FUNCTION | YES | ORDER_BY_DEPENDENCY |

**Comment and formatting rules:**

| A | B | Compatible | Strategy |
|---|---|-----------|----------|
| ADD_COMMENT | ADD_COMMENT | YES | APPEND_STATEMENTS |
| ADD_COMMENT | MODIFY_COMMENT | YES | APPEND_STATEMENTS |
| ADD_COMMENT | ADD_FUNCTION | YES | APPEND_FUNCTIONS |
| FORMATTING_ONLY | FORMATTING_ONLY | YES | ORDER_BY_TIME |
| FORMATTING_ONLY | ADD_FUNCTION | YES | ORDER_BY_TIME |
| FORMATTING_ONLY | MODIFY_FUNCTION | YES | ORDER_BY_TIME |

---

## Auto Merger — All 8 Strategies

**File:** `apps/desktop/src/main/ai/merge/auto-merger.ts`

### MergeContext interface

```typescript
export interface MergeContext {
  filePath: string;
  baselineContent: string;
  taskSnapshots: TaskSnapshot[];
  conflict: ConflictRegion;  // the conflict being resolved
}
```

### AutoMerger class

```typescript
export class AutoMerger {
  constructor()  // registers all 9 strategy handlers (including APPEND_STATEMENTS)

  merge(context: MergeContext, strategy: MergeStrategy): MergeResult
  canHandle(strategy: MergeStrategy): boolean
}
```

`merge()` dispatches to the appropriate handler via a `Map<MergeStrategy, StrategyHandler>`.
Wraps handler in try/catch; returns `FAILED` on exception with error message.

---

### Strategy 1: COMBINE_IMPORTS

**Function:** `executeImportStrategy(context)`

For all `ADD_IMPORT` changes across all task snapshots, deduplicates against:
- existing imports already in the file
- imports being removed by other tasks

```typescript
function executeImportStrategy(context: MergeContext): MergeResult {
  const lines = context.baselineContent.split(/\r?\n/);
  const ext = getExtension(context.filePath);

  const importsToAdd: string[] = [];
  const importsToRemove = new Set<string>();

  for (const snapshot of context.taskSnapshots) {
    for (const change of snapshot.semanticChanges) {
      if (change.changeType === ChangeType.ADD_IMPORT && change.contentAfter) {
        importsToAdd.push(change.contentAfter.trim());
      } else if (change.changeType === ChangeType.REMOVE_IMPORT && change.contentBefore) {
        importsToRemove.add(change.contentBefore.trim());
      }
    }
  }

  const importEndLine = findImportSectionEnd(lines, ext);
  // ...build existingImports Set from first importEndLine lines...
  // deduplicate importsToAdd against existingImports and importsToRemove

  // Remove imports scheduled for removal
  const resultLines = lines.filter((line) => !importsToRemove.has(line.trim()));

  if (newImports.length > 0) {
    const insertPos = findImportSectionEnd(resultLines, ext);
    // splice newImports in reverse order at insertPos
  }
}
```

`findImportSectionEnd(lines, ext)` scans until it finds a non-comment, non-import
line after the first import, returns that line index. Handles `.py` (`import`/`from`)
and JS/TS (`import`/`export`).

---

### Strategy 2: HOOKS_FIRST

**Function:** `executeHooksStrategy(context)`

Inserts React hook calls at the start of a named function body.

```typescript
function executeHooksStrategy(context: MergeContext): MergeResult {
  let content = context.baselineContent;
  const hooks: string[] = [];

  for (const snapshot of context.taskSnapshots) {
    for (const change of snapshot.semanticChanges) {
      if (change.changeType === ChangeType.ADD_HOOK_CALL) {
        const hookContent = extractHookCall(change);  // extracts `const {x} = useX(...)` pattern
        if (hookContent) hooks.push(hookContent);
      }
    }
  }

  const funcLocation = context.conflict.location;  // e.g. "function:MyComponent"
  if (funcLocation.startsWith('function:')) {
    const funcName = funcLocation.split(':')[1];
    content = insertHooksIntoFunction(content, funcName, hooks);
  }
}
```

`extractHookCall(change)` — matches `/(const\s+\{[^}]+\}\s*=\s*)?use\w+\([^)]*\);?/` in
`change.contentAfter`.

`insertHooksIntoFunction(content, funcName, hooks)` — finds the opening `{` of the
function body using three regex patterns (named function, arrow const, function const),
inserts `\n  ` + hooks joined with `\n  ` immediately after the opening brace.

---

### Strategy 3: HOOKS_THEN_WRAP

**Function:** `executeHooksThenWrapStrategy(context)`

Combines `HOOKS_FIRST` and JSX wrapping in a single pass.

```typescript
function executeHooksThenWrapStrategy(context: MergeContext): MergeResult {
  // Collect hooks from ADD_HOOK_CALL changes
  // Collect wrappers from WRAP_JSX changes via extractJsxWrapper(change)
  //   → matches /<(\w+)([^>]*)>/ in contentAfter → [wrapperName, wrapperProps]

  if (funcName) {
    if (hooks.length > 0) {
      content = insertHooksIntoFunction(content, funcName, hooks);
    }
    for (const [wrapperName, wrapperProps] of wraps) {
      content = wrapFunctionReturn(content, funcName, wrapperName, wrapperProps);
    }
  }
}
```

`wrapFunctionReturn(content, _funcName, wrapperName, wrapperProps)` — finds
`return (\n  <SomeJsx` via `/(return\s*\(\s*)(<[^>]+>)/`, wraps with
`<WrapperName props>` around it:
```
return (
  <WrapperName props>
    <OriginalJsx
```

---

### Strategy 4: APPEND_FUNCTIONS

**Function:** `executeAppendFunctionsStrategy(context)`

Appends new function bodies before any `module.exports`/`export default` statement,
or at end of file if none found.

```typescript
function executeAppendFunctionsStrategy(context: MergeContext): MergeResult {
  const newFunctions: string[] = [];

  for (const snapshot of context.taskSnapshots) {
    for (const change of snapshot.semanticChanges) {
      if (change.changeType === ChangeType.ADD_FUNCTION && change.contentAfter) {
        newFunctions.push(change.contentAfter);
      }
    }
  }

  const insertPos = findFunctionInsertPosition(content);
  // findFunctionInsertPosition scans lines backwards for 'module.exports' or 'export default'

  if (insertPos !== null) {
    // splice each function at offset, tracking offset increment per function
  } else {
    for (const func of newFunctions) {
      content += `\n\n${func}`;
    }
  }
}
```

---

### Strategy 5: APPEND_METHODS

**Function:** `executeAppendMethodsStrategy(context)`

Groups new methods by class name, inserts each group inside the appropriate class body.

```typescript
function executeAppendMethodsStrategy(context: MergeContext): MergeResult {
  const newMethods: Map<string, string[]> = new Map();  // className -> method bodies

  for (const snapshot of context.taskSnapshots) {
    for (const change of snapshot.semanticChanges) {
      if (change.changeType === ChangeType.ADD_METHOD && change.contentAfter) {
        // className extracted from change.target: "ClassName.methodName" → "ClassName"
        const className = change.target.includes('.') ? change.target.split('.')[0] : null;
        if (className) {
          newMethods.get(className)!.push(change.contentAfter);
        }
      }
    }
  }

  for (const [className, methods] of newMethods) {
    content = insertMethodsIntoClass(content, className, methods);
  }
}
```

`insertMethodsIntoClass(content, className, methods)`:
1. Regex: `class\s+<ClassName>\s*(?:extends\s+\w+)?\s*\{`
2. Walk from opening `{` counting braces to find the closing `}`
3. Insert `\n\n  ` + methods joined with `\n\n  ` before the closing `}`

---

### Strategy 6: COMBINE_PROPS

**Function:** `executeCombinePropsStrategy(context)`

Takes the last task snapshot's last change and applies a simple string substitution.

```typescript
function executeCombinePropsStrategy(context: MergeContext): MergeResult {
  // Takes last snapshot's last change
  const lastSnapshot = context.taskSnapshots[context.taskSnapshots.length - 1];
  const lastChange = lastSnapshot.semanticChanges[lastSnapshot.semanticChanges.length - 1];

  if (lastChange.contentAfter) {
    content = applyContentChange(content, lastChange.contentBefore, lastChange.contentAfter);
  }
}
```

`applyContentChange(content, oldContent, newContent)` — `content.replace(oldContent, newContent)`
if `oldContent` exists in content, otherwise returns content unchanged.

Note: This strategy takes a simple "last wins" approach for prop merging rather than
doing a true attribute-level merge.

---

### Strategy 7: ORDER_BY_DEPENDENCY

**Function:** `executeOrderByDependencyStrategy(context)`

Topologically sorts all `SemanticChange` objects across all snapshots by change type
priority, then applies them in order.

```typescript
function topologicalSortChanges(snapshots: TaskSnapshot[]): SemanticChange[] {
  const priority: Partial<Record<ChangeType, number>> = {
    [ChangeType.ADD_IMPORT]: 0,
    [ChangeType.ADD_HOOK_CALL]: 1,
    [ChangeType.ADD_VARIABLE]: 2,
    [ChangeType.ADD_CONSTANT]: 2,
    [ChangeType.WRAP_JSX]: 3,
    [ChangeType.ADD_JSX_ELEMENT]: 4,
    [ChangeType.MODIFY_FUNCTION]: 5,
    [ChangeType.MODIFY_JSX_PROPS]: 5,
  };
  // All other change types get priority 10 (applied last)
  return allChanges.sort((a, b) => (priority[a.changeType] ?? 10) - (priority[b.changeType] ?? 10));
}
```

Application logic:
- `ADD_HOOK_CALL` → `insertHooksIntoFunction()` with hook extracted via `extractHookCall()`
- `WRAP_JSX` → `wrapFunctionReturn()` with wrapper from `extractJsxWrapper()`
- Other change types → currently no-op (would need additional handlers)

---

### Strategy 8: ORDER_BY_TIME

**Function:** `executeOrderByTimeStrategy(context)`

Sorts task snapshots by `startedAt` ascending and applies changes in chronological order.

```typescript
function executeOrderByTimeStrategy(context: MergeContext): MergeResult {
  const sortedSnapshots = [...context.taskSnapshots].sort(
    (a, b) => a.startedAt.getTime() - b.startedAt.getTime(),
  );

  for (const snapshot of sortedSnapshots) {
    for (const change of snapshot.semanticChanges) {
      if (change.contentBefore && change.contentAfter) {
        content = applyContentChange(content, change.contentBefore, change.contentAfter);
      }
    }
  }
}
```

### Bonus Strategy: APPEND_STATEMENTS

**Function:** `executeAppendStatementsStrategy(context)`

Appends all additive changes (see `isAdditiveChange()` — 12 types) to end of file.

```typescript
function executeAppendStatementsStrategy(context: MergeContext): MergeResult {
  for (const snapshot of context.taskSnapshots) {
    for (const change of snapshot.semanticChanges) {
      if (isAdditiveChange(change) && change.contentAfter) {
        additions.push(change.contentAfter);
      }
    }
  }
  for (const addition of additions) {
    content += `\n${addition}`;
  }
}
```

`isAdditiveChange()` returns true for: `ADD_IMPORT`, `ADD_FUNCTION`, `ADD_HOOK_CALL`,
`ADD_VARIABLE`, `ADD_CONSTANT`, `ADD_CLASS`, `ADD_METHOD`, `ADD_PROPERTY`, `ADD_TYPE`,
`ADD_INTERFACE`, `ADD_DECORATOR`, `ADD_JSX_ELEMENT`, `ADD_COMMENT`.

---

## Timeline Tracker

**File:** `apps/desktop/src/main/ai/merge/timeline-tracker.ts`

Orthogonal to the merge pipeline — tracks "drift" between tasks and main branch
for providing context to merge decisions. Not called by `MergeOrchestrator` directly.

### Key interfaces

```typescript
export interface BranchPoint {
  commitHash: string;
  content: string;     // file content at the branch point
  timestamp: Date;
}

export interface TaskFileView {
  taskId: string;
  branchPoint: BranchPoint;
  taskIntent: TaskIntent;
  worktreeState?: WorktreeState;  // { content, lastModified }
  commitsBehinMain: number;
  status: 'active' | 'merged' | 'abandoned';
  mergedAt?: Date;
}

export interface FileTimeline {
  filePath: string;
  taskViews: Map<string, TaskFileView>;   // taskId -> view
  mainBranchEvents: MainBranchEvent[];    // ordered list of main branch changes
}

export interface MergeTimelineContext {
  filePath: string;
  taskId: string;
  taskIntent: TaskIntent;
  taskBranchPoint: BranchPoint;
  mainEvolution: MainBranchEvent[];       // events on main since branch point
  taskWorktreeContent: string;
  currentMainContent: string;
  currentMainCommit: string;
  otherPendingTasks: Array<{
    taskId: string;
    intent: string;
    branchPoint: string;
    commitsBehind: number;
  }>;
  totalCommitsBehind: number;
  totalPendingTasks: number;
}
```

### FileTimelineTracker class

```typescript
export class FileTimelineTracker {
  constructor(projectPath: string, storagePath?: string)

  // Lifecycle event handlers
  onTaskStart(taskId, filesToModify, filesToCreate?, branchPointCommit?, taskIntent?, taskTitle?): void
  onMainBranchCommit(commitHash: string): void
  onTaskWorktreeChange(taskId, filePath, newContent): void
  onTaskMerged(taskId, mergeCommit): void
  onTaskAbandoned(taskId): void

  // Queries
  getMergeContext(taskId, filePath): MergeTimelineContext | undefined
  getFilesForTask(taskId): string[]
  getPendingTasksForFile(filePath): TaskFileView[]
  getTaskDrift(taskId): Map<string, number>  // filePath -> commitsBehinMain

  // Capture
  captureWorktreeState(taskId, worktreePath): void
  initializeFromWorktree(taskId, worktreePath, taskIntent?, taskTitle?, targetBranch?): void
}
```

`initializeFromWorktree()` — the main entry point for wiring a task worktree into
the timeline system. Calls `getBranchPoint()`, `getChangedFilesInWorktree()`,
`onTaskStart()`, `captureWorktreeState()`, and calculates drift with
`countCommitsBetween(branchPoint, targetBranch, worktreePath)`.

Storage: `.auto-claude/timelines/` with one JSON file per tracked file path
(named `<safe_path>.json`) and an `index.json` listing all tracked paths.

---

## MergeReport Structure

**File:** `apps/desktop/src/main/ai/merge/orchestrator.ts`

```typescript
export interface MergeReport {
  success: boolean;          // true if filesFailed === 0
  startedAt: Date;
  completedAt?: Date;
  tasksMerged: string[];     // taskIds that were merged
  fileResults: Map<string, MergeResult>;  // filePath -> MergeResult
  stats: MergeStats;
  error?: string;            // top-level error message if pipeline threw
}

export interface MergeStats {
  filesProcessed: number;
  filesAutoMerged: number;      // AUTO_MERGED + DIRECT_COPY
  filesAiMerged: number;        // AI_MERGED
  filesNeedReview: number;      // NEEDS_HUMAN_REVIEW
  filesFailed: number;          // FAILED
  conflictsDetected: number;    // total: resolved + remaining across all files
  conflictsAutoResolved: number;
  conflictsAiResolved: number;
  aiCallsMade: number;
  estimatedTokensUsed: number;  // always 0 currently (AI resolver must set it)
  durationMs: number;
}
```

Reports are persisted to `.auto-claude/merge_reports/<taskId>_<timestamp>.json`
(JSON-serializable form — `fileResults` becomes a plain object, `Map` entries
are serialized with decision/explanation/counts only).

```typescript
// Serialized file_results entry:
{
  decision: string,
  explanation: string,
  error: string | undefined,
  conflicts_resolved: number,
  conflicts_remaining: number,
}
```

---

## Progress Tracking

**File:** `apps/desktop/src/main/ai/merge/orchestrator.ts`

```typescript
export type ProgressStage =
  | 'analyzing'
  | 'detecting_conflicts'
  | 'resolving'
  | 'validating'
  | 'complete'
  | 'error';

export type ProgressCallback = (
  stage: ProgressStage,
  percent: number,       // 0-100
  message: string,
  details?: Record<string, unknown>,
) => void;
```

Progress callback is optional on both `mergeTask()` and `mergeTasks()`.

Progress sequence for `mergeTask()`:

| Stage | % | Message |
|-------|---|---------|
| analyzing | 0 | "Starting merge analysis" |
| analyzing | 5 | "Loading file evolution data" |
| analyzing | 15 | "Running semantic analysis" |
| analyzing | 25 | "Found N modified files" |
| detecting_conflicts | 25 | "Detecting conflicts" |
| resolving | 50-75 | "Merging file N/total" (per-file, linear) |
| validating | 75 | "Validating merge results" (with conflicts_found/resolved in details) |
| validating | 90 | "Validation complete" |
| complete | 100 | "Merge complete for <taskId>" (with stats in details) |
| error | 0 | "Merge failed: <message>" |

`details` on the `resolving` stage carries `{ current_file: string }`.
`details` on `validating` and `complete` carry `{ conflicts_found, conflicts_resolved }`.

---

## Public API

**File:** `apps/desktop/src/main/ai/merge/index.ts`

```typescript
export * from './types';
export * from './semantic-analyzer';
export * from './auto-merger';
export * from './conflict-detector';
export * from './file-evolution';
export * from './timeline-tracker';
export * from './orchestrator';
```

Everything is re-exported from the index. The primary consumer entry points are:

```typescript
import {
  MergeOrchestrator,
  type MergeReport,
  type MergeStats,
  type ProgressCallback,
  type ProgressStage,
  type AiResolverFn,
  type TaskMergeRequest,
} from './merge';
```

Typical usage:

```typescript
const orchestrator = new MergeOrchestrator({
  projectDir: '/path/to/project',
  enableAi: true,
  aiResolver: async (system, user) => {
    // call your LLM here, return merged content string
    return await callLlm(system, user);
  },
});

// Single task
const report = await orchestrator.mergeTask(
  'TASK-001',
  '/path/to/worktree',
  'main',
  (stage, percent, message, details) => {
    console.log(`[${stage}] ${percent}% — ${message}`);
  },
);

// Multi-task
const report = await orchestrator.mergeTasks(
  [
    { taskId: 'TASK-001', worktreePath: '/worktrees/001', priority: 1 },
    { taskId: 'TASK-002', worktreePath: '/worktrees/002', priority: 2 },
  ],
  'main',
  progressCallback,
);

// Preview without merging
const preview = orchestrator.previewMerge(['TASK-001', 'TASK-002']);

// Apply results
orchestrator.applyToProject(report);
// or
const writtenPaths = orchestrator.writeMergedFiles(report, '/output/dir');
```
