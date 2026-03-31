# Aperant Worktree Management — Reference

Extracted from the Aperant desktop app (Electron/TypeScript). Covers every
pattern needed to reimplement robust worktree lifecycle management.

## Source Files

| File | Purpose |
|------|---------|
| `apps/desktop/src/main/ai/worktree/worktree-manager.ts` | Idempotent worktree creation |
| `apps/desktop/src/main/utils/worktree-cleanup.ts` | Cross-platform cleanup + retry |
| `apps/desktop/src/main/utils/git-isolation.ts` | Env isolation, branch detection |
| `apps/desktop/src/main/worktree-paths.ts` | Path helpers + traversal prevention |
| `apps/desktop/src/main/ipc-handlers/task/worktree-handlers.ts` | Branch validation (issue #1479) |
| `apps/desktop/src/main/ipc-handlers/terminal/worktree-handlers.ts` | Terminal worktrees, dependency strategies |

---

## 1. Worktree Creation — Idempotent Pattern

**File:** `apps/desktop/src/main/ai/worktree/worktree-manager.ts`

### Public Interface

```typescript
export interface WorktreeResult {
  worktreePath: string;  // Absolute resolved path to the worktree directory
  branch: string;        // Git branch name checked out in the worktree
}

export async function createOrGetWorktree(
  projectPath: string,
  specId: string,
  baseBranch = 'main',
  useLocalBranch = false,
  pushNewBranches = true,
  autoBuildPath?: string,
): Promise<WorktreeResult>
```

### Directory and Branch Naming Convention

```
worktreePath : {projectPath}/.auto-claude/worktrees/tasks/{specId}
branchName   : auto-claude/{specId}
```

### Step-by-Step Flow (7 steps)

**Step 1 — Prune stale references first (non-fatal)**
```typescript
await git(['worktree', 'prune'], projectPath, /* allowFailure */ true);
```
Always prune before any check. Clears `.git/worktrees/` entries that point to
deleted directories, preventing false "already registered" results.

**Step 2 — Early return if worktree exists AND is registered**
```typescript
if (existsSync(worktreePath)) {
  const isRegistered = await isWorktreeRegistered(worktreePath, projectPath);
  if (isRegistered) {
    return { worktreePath: resolve(worktreePath), branch: branchName };
  }
  // ... else fall through to step 3
}
```

**Step 3 — Remove stale directory git no longer tracks**
```typescript
await rm(worktreePath, { recursive: true, force: true });
if (existsSync(worktreePath)) {
  throw new Error(`Stale worktree directory still exists after removal: ${worktreePath}. ` +
    'This may be due to permission issues or file locks.');
}
```
Directory exists but git doesn't know about it — remove so we can recreate cleanly.

**Step 4 — Check if branch already exists locally**
```typescript
const branchListOutput = await git(
  ['branch', '--list', branchName],
  projectPath,
  /* allowFailure */ true,
);
const branchExists = branchListOutput.includes(branchName);
```

**Step 5 — Fetch from remote (non-fatal)**
```typescript
await git(['fetch', 'origin', baseBranch], projectPath, /* allowFailure */ true);
```

**Step 6 — Create the worktree (two paths)**

If branch already exists (resume scenario):
```typescript
await git(['worktree', 'add', worktreePath, branchName], projectPath);
```

If branch is new (first-time scenario):
```typescript
// Prefer origin/{baseBranch} as start point if it exists
const remoteRef = `origin/${baseBranch}`;
const remoteExists = await git(['rev-parse', '--verify', remoteRef], projectPath, true);
const startPoint = remoteExists ? remoteRef : baseBranch;

await git(
  ['worktree', 'add', '-b', branchName, '--no-track', worktreePath, startPoint],
  projectPath,
);
```
`--no-track` prevents the new branch from inheriting upstream tracking from the
base branch — tracking is set explicitly in step 6b.

Optional upstream setup (best-effort, non-fatal):
```typescript
if (pushNewBranches) {
  const hasOrigin = await git(['remote', 'get-url', 'origin'], projectPath, true);
  if (hasOrigin) {
    await git(['push', '--set-upstream', 'origin', branchName], worktreePath);
  }
}
```

**Step 7 — Copy gitignored spec dir into worktree**
```typescript
const sourceSpecDir = join(projectPath, specsRelDir, specId);
const destSpecDir   = join(worktreePath, specsRelDir, specId);
if (existsSync(sourceSpecDir) && !existsSync(destSpecDir)) {
  mkdirSync(join(worktreePath, specsRelDir), { recursive: true });
  await cp(sourceSpecDir, destSpecDir, { recursive: true });
}
```
Gitignored files are absent from the fresh worktree checkout. Copy them
explicitly so agents can read spec files.

### Registration Check Helper

```typescript
async function isWorktreeRegistered(
  worktreePath: string,
  projectPath: string,
): Promise<boolean> {
  const output = await git(
    ['worktree', 'list', '--porcelain'],
    projectPath,
    /* allowFailure */ true,
  );
  if (!output) return false;

  const normalizedTarget = resolve(worktreePath);
  return output
    .split(/\r?\n/)
    .some((line) => {
      if (!line.startsWith('worktree ')) return false;
      const listed = line.slice('worktree '.length).trim();
      return resolve(listed) === normalizedTarget;  // resolve() normalizes separators
    });
}
```
Uses `path.resolve()` on both sides so Windows path separator differences
(`/` vs `\`) don't produce false negatives.

### Git Helper Wrapper

```typescript
const execFileAsync = promisify(execFile);

async function git(
  args: string[],
  cwd: string,
  allowFailure = false,
): Promise<string> {
  try {
    const { stdout } = await execFileAsync('git', args, { cwd });
    return stdout.trim();
  } catch (err: unknown) {
    if (allowFailure) return '';
    const message = err instanceof Error ? err.message : String(err);
    throw new Error(`git ${args[0]} failed: ${message}`);
  }
}
```

---

## 2. Cross-Platform Cleanup with Windows File Locking

**File:** `apps/desktop/src/main/utils/worktree-cleanup.ts`

### Why `git worktree remove --force` Is Not Used

On Windows, `git worktree remove --force` fails when the worktree contains
untracked files (node_modules, build artifacts). The workaround is to:
1. Delete the directory manually (with retry for file locks)
2. Run `git worktree prune` to clean up `.git/worktrees/` metadata
3. Optionally delete the branch

Related issue: `https://github.com/AndyMik90/Auto-Claude/issues/1539`

### Interface

```typescript
export interface WorktreeCleanupOptions {
  worktreePath: string;    // Absolute path to the worktree directory
  projectPath: string;     // Absolute path to the main project
  specId: string;          // e.g. "001-my-feature"
  logPrefix?: string;      // e.g. "[TASK_DELETE]" (default: "[WORKTREE_CLEANUP]")
  deleteBranch?: boolean;  // Default: true
  branchName?: string;     // Override auto-detected branch name
  timeout?: number;        // Git op timeout ms (default: 30000)
  maxRetries?: number;     // Directory delete retries (default: 3)
  retryDelay?: number;     // Base delay between retries ms (default: 500)
}

export interface WorktreeCleanupResult {
  success: boolean;
  branch?: string;
  warnings: string[];      // Non-fatal issues (prune failed, branch not found, etc.)
}

export async function cleanupWorktree(
  options: WorktreeCleanupOptions
): Promise<WorktreeCleanupResult>
```

### Cleanup Flow (4 steps)

**Step 0 — Path traversal security check (hard reject)**
```typescript
const taskBase     = getTaskWorktreeDir(projectPath);
const terminalBase = getTerminalWorktreeDir(projectPath);
const isValidPath  = isPathWithinBase(worktreePath, taskBase)
                  || isPathWithinBase(worktreePath, terminalBase);

if (!isValidPath) {
  return { success: false, warnings: ['Invalid worktree path'] };
}
```
Prevents accidental or malicious deletion of paths outside worktree directories.

**Step 1 — Detect branch before deleting directory**
```typescript
function getWorktreeBranch(
  worktreePath: string,
  specId: string,
  timeout: number,
  explicitBranchName?: string
): string | null {
  if (existsSync(worktreePath)) {
    try {
      const branch = execFileSync(getToolPath('git'), ['rev-parse', '--abbrev-ref', 'HEAD'], {
        cwd: worktreePath, encoding: 'utf-8', env: getIsolatedGitEnv(), timeout
      }).trim();
      if (branch && branch !== 'HEAD') return branch;
    } catch { /* fall through */ }
  }
  if (explicitBranchName) return explicitBranchName;
  return `auto-claude/${specId}`;  // Naming convention fallback
}
```

**Step 2 — Delete directory with retry (critical — failure aborts cleanup)**
```typescript
async function deleteDirectoryWithRetry(
  dirPath: string,
  maxRetries: number,
  retryDelay: number,
  logPrefix: string
): Promise<void> {
  let lastError: Error | null = null;

  for (let attempt = 1; attempt <= maxRetries; attempt++) {
    try {
      await rm(dirPath, { recursive: true, force: true });
      return;
    } catch (error) {
      lastError = error instanceof Error ? error : new Error(String(error));
      if (attempt < maxRetries) {
        const waitTime = retryDelay * attempt;  // Linear backoff
        await delay(waitTime);
      }
    }
  }

  // Unix fallback: Node.js rm() can fail with ENOTEMPTY on macOS .app bundles
  if (process.platform !== 'win32') {
    try {
      execFileSync('/bin/rm', ['-rf', dirPath], { timeout: 60000 });
      return;
    } catch { /* fall through */ }
  }

  throw lastError || new Error('Failed to delete directory after retries');
}
```
Linear backoff formula: `waitTime = retryDelay * attempt` (500ms, 1000ms, 1500ms
with defaults). Fallback to `/bin/rm -rf` on non-Windows handles macOS `.app`
bundles that cause `ENOTEMPTY`.

**Step 3 — Prune git references (non-fatal, adds to warnings)**
```typescript
try {
  execFileSync(getToolPath('git'), ['worktree', 'prune'], {
    cwd: projectPath, encoding: 'utf-8', env: getIsolatedGitEnv(), timeout
  });
} catch (pruneError) {
  warnings.push(`Worktree prune failed: ${msg}`);
}
```

**Step 4 — Delete branch (non-fatal, adds to warnings)**
```typescript
if (deleteBranch && branch) {
  try {
    execFileSync(getToolPath('git'), ['branch', '-D', branch], {
      cwd: projectPath, encoding: 'utf-8', env: getIsolatedGitEnv(), timeout
    });
  } catch (branchError) {
    warnings.push(`Branch deletion failed: ${msg}`);
  }
}
```

---

## 3. Branch Name Validation — Security Issue #1479

**Files:**
- `apps/desktop/src/main/ipc-handlers/task/worktree-handlers.ts`
- `apps/desktop/src/main/utils/git-isolation.ts`
- `apps/desktop/src/main/ipc-handlers/task/__tests__/worktree-branch-validation.test.ts`

### The Bug (Issue #1479)

When a worktree directory is corrupted or orphaned, `git rev-parse
--abbrev-ref HEAD` walks up the directory tree and returns the **main
project's** current branch (e.g. `feature/xstate-task-machine`) instead of
the worktree's branch. Without validation this would delete the wrong branch.

### Regex for Valid Branch Names

```typescript
// Allows: alphanumeric, dots, slashes, dashes, underscores
// Must start and end with alphanumeric
// Also accepts single-char names (second alternative)
export const GIT_BRANCH_REGEX = /^[a-zA-Z0-9][a-zA-Z0-9._/-]*[a-zA-Z0-9]$|^[a-zA-Z0-9]$/;

// Terminal worktrees use lowercase-only names
const WORKTREE_NAME_REGEX = /^[a-z0-9][a-z0-9_-]*[a-z0-9]$|^[a-z0-9]$/;
```

### Safe Branch Validation Function (task/worktree-handlers.ts)

```typescript
export function validateWorktreeBranch(
  detectedBranch: string | null,
  expectedBranch: string
): { branchToDelete: string; usedFallback: boolean; reason: string } {
  // Detection failed entirely
  if (detectedBranch === null) {
    return { branchToDelete: expectedBranch, usedFallback: true, reason: 'detection_failed' };
  }

  // Ideal case: exact match
  if (detectedBranch === expectedBranch) {
    return { branchToDelete: detectedBranch, usedFallback: false, reason: 'exact_match' };
  }

  // Accept any auto-claude/{specId} branch (handles renamed specs)
  // Requires non-empty specId after the slash
  if (detectedBranch.startsWith('auto-claude/') && detectedBranch.length > 'auto-claude/'.length) {
    return { branchToDelete: detectedBranch, usedFallback: false, reason: 'pattern_match' };
  }

  // Security: detected branch doesn't look like a task branch — use known-safe fallback
  return { branchToDelete: expectedBranch, usedFallback: true, reason: 'invalid_pattern' };
}
```

Branches rejected and falling back to `expectedBranch`:
- `main`, `master`, `develop`
- `feature/anything`, `fix/anything`  (main project branches)
- `HEAD` (detached state)
- `auto-claude` (no slash / empty specId)
- `auto-claude/` (slash but empty specId)

### Strict-Match Variant (git-isolation.ts)

`detectWorktreeBranch()` in `git-isolation.ts` uses **exact match only** (does
not allow the `auto-claude/` prefix match), to prevent accidentally deleting a
*different* task's branch:

```typescript
export function detectWorktreeBranch(
  worktreePath: string,
  specId: string,
  options: { timeout?: number; logPrefix?: string } = {}
): WorktreeBranchDetectionResult {
  const { timeout = 30000, logPrefix = '[WORKTREE_BRANCH_DETECTION]' } = options;
  const expectedBranch = `auto-claude/${specId}`;
  let branch = expectedBranch;
  let usingFallback = false;

  try {
    const detectedBranch = execFileSync(getToolPath('git'), ['rev-parse', '--abbrev-ref', 'HEAD'], {
      cwd: worktreePath, encoding: 'utf-8', timeout, env: getIsolatedGitEnv()
    }).trim();

    // STRICT: exact match only. Prefix match risks deleting a different task's branch.
    if (detectedBranch === expectedBranch) {
      branch = detectedBranch;
    } else {
      console.warn(`${logPrefix} Detected '${detectedBranch}' != expected '${expectedBranch}', using fallback`);
      usingFallback = true;
    }
  } catch {
    usingFallback = true;
  }

  return { branch, usingFallback };
}
```

---

## 4. Path Traversal Prevention

**File:** `apps/desktop/src/main/worktree-paths.ts`

### Core Predicate

```typescript
export function isPathWithinBase(resolvedPath: string, basePath: string): boolean {
  const normalizedPath = path.resolve(resolvedPath);
  const normalizedBase = path.resolve(basePath);
  // Append path.sep to base to prevent prefix-matching attacks
  // e.g. "/foo/bar-evil" would incorrectly match base "/foo/bar" without sep
  return normalizedPath.startsWith(normalizedBase + path.sep)
      || normalizedPath === normalizedBase;
}
```

### Path Constants

```typescript
export const TASK_WORKTREE_DIR     = '.auto-claude/worktrees/tasks';
export const TERMINAL_WORKTREE_DIR = '.auto-claude/worktrees/terminal';
export const LEGACY_WORKTREE_DIR   = '.worktrees';  // Backwards compat

export function getTaskWorktreeDir(projectPath: string): string {
  return path.join(projectPath, TASK_WORKTREE_DIR);
}
export function getTerminalWorktreeDir(projectPath: string): string {
  return path.join(projectPath, TERMINAL_WORKTREE_DIR);
}
```

### Safe Path Resolution with Traversal Detection

```typescript
export function findTaskWorktree(projectPath: string, specId: string): string | null {
  if (!projectPath || typeof projectPath !== 'string') return null;
  if (!specId    || typeof specId    !== 'string') return null;

  const normalizedProject = path.resolve(projectPath);

  // New location
  const newPath         = path.join(projectPath, TASK_WORKTREE_DIR, specId);
  const resolvedNewPath = path.resolve(newPath);

  if (!isPathWithinBase(resolvedNewPath, normalizedProject)) {
    console.error(`Path traversal detected: specId "${specId}" resolves outside project`);
    return null;
  }
  if (existsSync(resolvedNewPath)) return resolvedNewPath;

  // Legacy fallback
  const legacyPath         = path.join(projectPath, LEGACY_WORKTREE_DIR, specId);
  const resolvedLegacyPath = path.resolve(legacyPath);

  if (!isPathWithinBase(resolvedLegacyPath, normalizedProject)) {
    console.error(`Path traversal detected (legacy): specId "${specId}" resolves outside project`);
    return null;
  }
  if (existsSync(resolvedLegacyPath)) return resolvedLegacyPath;

  return null;
}
```

Key points:
- Resolve before checking to canonicalize `..` segments
- Check containment **before** checking `existsSync`
- Always validate both the primary and legacy paths
- Defensive null/type guards on every input

### Dependency Path Validation Pattern (terminal worktree handler)

When loading dependency paths from a project index JSON file, additional
validation layers are applied before using any path:

```typescript
// 1. Reject absolute paths
if (path.isAbsolute(relPath)) continue;

// 2. Reject traversal components in both slash styles
if (relPath.split('/').includes('..') || relPath.split('\\').includes('..')) continue;

// 3. Defense-in-depth: resolved-path containment check
const resolved = path.resolve(projectPath, relPath);
if (!resolved.startsWith(path.resolve(projectPath) + path.sep)) continue;
```

---

## 5. Stale Reference Pruning

### When to Prune

Pruning removes `.git/worktrees/<name>/` entries that point to directories
that no longer exist. It must be called:

1. **Before creating** a worktree — prevents false "already registered" hits
2. **After deleting** a worktree directory — syncs git's internal state

Both `worktree-manager.ts` and `worktree-cleanup.ts` call `git worktree prune`
with `allowFailure=true` / non-throwing, because:
- The operation is metadata cleanup only — the worktree work is already done
- It can fail in read-only or network-mounted repos without breaking the flow

### Prune in Creation Context (non-fatal)

```typescript
// worktree-manager.ts — called with allowFailure=true
await git(['worktree', 'prune'], projectPath, /* allowFailure */ true);
```

### Prune in Cleanup Context (non-fatal, warnings collected)

```typescript
// worktree-cleanup.ts — called synchronously after directory deletion
try {
  execFileSync(getToolPath('git'), ['worktree', 'prune'], {
    cwd: projectPath,
    encoding: 'utf-8',
    env: getIsolatedGitEnv(),
    timeout
  });
} catch (pruneError) {
  warnings.push(`Worktree prune failed: ${msg}`);
  // Does NOT set success=false — directory is already gone
}
```

---

## 6. Git Environment Isolation

**File:** `apps/desktop/src/main/utils/git-isolation.ts`

### Why This Matters

When running git commands inside a worktree, inherited env vars like
`GIT_DIR` cause git to operate on the wrong repository. This is especially
problematic in long-lived processes (Electron app, Claude Code) where a
parent git session may have set these vars.

### Variables Cleared

```typescript
export const GIT_ENV_VARS_TO_CLEAR = [
  'GIT_DIR',
  'GIT_WORK_TREE',
  'GIT_INDEX_FILE',
  'GIT_OBJECT_DIRECTORY',
  'GIT_ALTERNATE_OBJECT_DIRECTORIES',
  'GIT_AUTHOR_NAME',
  'GIT_AUTHOR_EMAIL',
  'GIT_AUTHOR_DATE',
  'GIT_COMMITTER_NAME',
  'GIT_COMMITTER_EMAIL',
  'GIT_COMMITTER_DATE',
] as const;
```

### Isolated Environment Factory

```typescript
export function getIsolatedGitEnv(
  baseEnv: NodeJS.ProcessEnv = process.env
): Record<string, string | undefined> {
  const env: Record<string, string | undefined> = { ...baseEnv };
  for (const varName of GIT_ENV_VARS_TO_CLEAR) {
    delete env[varName];
  }
  env.HUSKY = '0';  // Disable user pre-commit hooks in automated contexts
  return env;
}
```

### Convenience Spawn Options Builder

```typescript
export function getIsolatedGitSpawnOptions(
  cwd: string,
  additionalOptions: Record<string, unknown> = {}
): Record<string, unknown> {
  return {
    cwd,
    env: getIsolatedGitEnv(),
    encoding: 'utf-8',
    ...additionalOptions,
  };
}
```

### Usage Pattern (all cleanup/creation code follows this)

```typescript
execFileSync(getToolPath('git'), ['branch', '-D', branch], {
  cwd: projectPath,
  encoding: 'utf-8',
  env: getIsolatedGitEnv(),
  timeout
});
```

---

## 7. Misconfigured Bare Repository Auto-Fix

**Files:** Both `task/worktree-handlers.ts` and `terminal/worktree-handlers.ts`

### The Problem

Git worktree operations can incorrectly set `core.bare=true` in the main
repo's config. Subsequent git commands then fail with "operation must be run
in a work tree". Auto-fix detects this and unsets the flag.

### Detection and Fix

```typescript
function fixMisconfiguredBareRepo(projectPath: string): boolean {
  try {
    const bareConfig = execFileSync(
      getToolPath('git'), ['config', '--get', 'core.bare'],
      { cwd: projectPath, encoding: 'utf-8', stdio: ['pipe', 'pipe', 'pipe'],
        env: getIsolatedGitEnv() }
    ).trim().toLowerCase();

    if (bareConfig !== 'true') return false;

    // Only fix if source files exist (not a legitimately bare repo)
    const EXACT_MARKERS = [
      'package.json', 'apps', 'src', 'pyproject.toml', 'setup.py',
      'requirements.txt', 'Pipfile', 'Cargo.toml', 'go.mod', 'go.sum',
      'cmd', 'main.go', 'pom.xml', 'build.gradle', 'build.gradle.kts',
      'Gemfile', 'Rakefile', 'composer.json', 'Makefile',
      'CMakeLists.txt', 'README.md', 'LICENSE'
    ];
    const GLOB_MARKERS = ['*.csproj', '*.sln', '*.fsproj'];

    const hasExactMatch = EXACT_MARKERS.some(m => existsSync(path.join(projectPath, m)));

    if (!hasExactMatch) {
      // Lazy-load directory listing; cap at 500 entries for performance
      const files = readdirSync(projectPath).slice(0, 500);
      const hasGlobMatch = GLOB_MARKERS.some(pattern =>
        files.some(file => minimatch(file, pattern, { nocase: true }))
      );
      if (!hasGlobMatch) return false;  // Legitimately bare
    }

    execFileSync(getToolPath('git'), ['config', '--unset', 'core.bare'], {
      cwd: projectPath, encoding: 'utf-8', stdio: ['pipe', 'pipe', 'pipe'],
      env: getIsolatedGitEnv()
    });
    return true;
  } catch {
    return false;  // Always non-fatal
  }
}
```

---

## 8. Default Branch Detection

**File:** `apps/desktop/src/main/ipc-handlers/terminal/worktree-handlers.ts`

Resolution order (first hit wins):

1. Project settings store (`project.settings.mainBranch`)
2. `.auto-claude/.env` file (`DEFAULT_BRANCH` key)
3. Auto-detect: try `main` then `master` via `git rev-parse --verify`
4. Current branch via `git rev-parse --abbrev-ref HEAD`
5. Hard fallback: `'main'`

```typescript
function getDefaultBranch(projectPath: string): string {
  const project = projectStore.getProjects().find(p => p.path === projectPath);
  if (project?.settings?.mainBranch) return project.settings.mainBranch;

  const envPath = path.join(projectPath, '.auto-claude', '.env');
  if (existsSync(envPath)) {
    const vars = parseEnvFile(readFileSync(envPath, 'utf-8'));
    if (vars['DEFAULT_BRANCH']) return vars['DEFAULT_BRANCH'];
  }

  for (const branch of ['main', 'master']) {
    try {
      execFileSync(getToolPath('git'), ['rev-parse', '--verify', branch], {
        cwd: projectPath, encoding: 'utf-8', stdio: ['pipe', 'pipe', 'pipe'],
        env: getIsolatedGitEnv(),
      });
      return branch;
    } catch { /* try next */ }
  }

  try {
    return execFileSync(getToolPath('git'), ['rev-parse', '--abbrev-ref', 'HEAD'], {
      cwd: projectPath, encoding: 'utf-8', stdio: ['pipe', 'pipe', 'pipe'],
      env: getIsolatedGitEnv(),
    }).trim();
  } catch {
    return 'main';
  }
}
```

---

## 9. Key Design Decisions Summary

| Decision | Rationale |
|----------|-----------|
| Prune before every create | Prevents stale `.git/worktrees/` from blocking creation |
| `git worktree prune` not `remove --force` for cleanup | `remove --force` fails on Windows with untracked files |
| Linear retry backoff (`retryDelay * attempt`) | Simple, predictable delay growth for file lock waits |
| `/bin/rm -rf` as Unix fallback | Node `rm()` fails with `ENOTEMPTY` on macOS `.app` bundles |
| `path.sep` appended to base in `isPathWithinBase` | Prevents `/foo/bar` from matching `/foo/bar-evil` |
| Resolve paths before containment check | Canonicalizes `..` segments before comparing |
| `auto-claude/` prefix validation before branch delete | Prevents deleting main project branch from corrupted worktree (issue #1479) |
| Exact-match in `detectWorktreeBranch` (strict variant) | Prevents deleting a *different* task's branch via prefix |
| `GIT_DIR`/`GIT_WORK_TREE` cleared in every subprocess | Prevents env contamination between worktrees in long-lived processes |
| `HUSKY=0` in isolated env | Prevents double-execution of user pre-commit hooks |
| Branch get-before-delete | Directory must exist to read HEAD; get branch name before deleting dir |
| `--no-track` on new branch creation | Worktree branch should not inherit upstream tracking from base |
