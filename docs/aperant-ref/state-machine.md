# Aperant State Machine Reference

Extracted from `F:\Tools\External\Aperant\apps\desktop\src\shared\state-machines\`

---

## 1. Task Machine (`task-machine.ts`)

**File:** `apps/desktop/src/shared/state-machines/task-machine.ts`

### Context

```ts
export interface TaskContext {
  reviewReason?: ReviewReason;
  error?: string;
}
```

`ReviewReason` values (from shared types): `'plan_review' | 'completed' | 'stopped' | 'qa_rejected' | 'errors'`

### Event Union

```ts
export type TaskEvent =
  | { type: 'PLANNING_STARTED' }
  | { type: 'PLANNING_COMPLETE'; hasSubtasks: boolean; subtaskCount: number; requireReviewBeforeCoding: boolean }
  | { type: 'PLAN_APPROVED' }
  | { type: 'CODING_STARTED'; subtaskId: string; subtaskDescription: string }
  | { type: 'SUBTASK_COMPLETED'; subtaskId: string; completedCount: number; totalCount: number }
  | { type: 'ALL_SUBTASKS_DONE'; totalCount: number }
  | { type: 'QA_STARTED'; iteration: number; maxIterations: number }
  | { type: 'QA_PASSED'; iteration: number; testsRun: Record<string, unknown> }
  | { type: 'QA_FAILED'; iteration: number; issueCount: number; issues: string[] }
  | { type: 'QA_FIXING_STARTED'; iteration: number }
  | { type: 'QA_FIXING_COMPLETE'; iteration: number }
  | { type: 'PLANNING_FAILED'; error: string; recoverable: boolean }
  | { type: 'CODING_FAILED'; subtaskId: string; error: string; attemptCount: number }
  | { type: 'QA_MAX_ITERATIONS'; iteration: number; maxIterations: number }
  | { type: 'QA_AGENT_ERROR'; iteration: number; consecutiveErrors: number }
  | { type: 'PROCESS_EXITED'; exitCode: number; signal?: string; unexpected?: boolean }
  | { type: 'USER_STOPPED'; hasPlan?: boolean }
  | { type: 'USER_RESUMED' }
  | { type: 'MARK_DONE' }
  | { type: 'CREATE_PR' }
  | { type: 'PR_CREATED'; prUrl: string };
```

### States and Transitions

Initial state: `backlog`

#### `backlog`
| Event | Target | Guard | Actions |
|---|---|---|---|
| `PLANNING_STARTED` | `planning` | — | — |
| `CODING_STARTED` | `coding` | — | — (fallback: resumed task) |
| `USER_STOPPED` | `backlog` | — | — |

#### `planning`
| Event | Target | Guard | Actions |
|---|---|---|---|
| `PLANNING_COMPLETE` | `plan_review` | `requiresReview` | `setReviewReasonPlan` |
| `PLANNING_COMPLETE` | `coding` | — (else) | `clearReviewReason` |
| `CODING_STARTED` | `coding` | — | `clearReviewReason` (fallback) |
| `ALL_SUBTASKS_DONE` | `qa_review` | — | — (fallback) |
| `QA_STARTED` | `qa_review` | — | — (fallback) |
| `QA_PASSED` | `human_review` | — | `setReviewReasonCompleted` (fallback) |
| `PLANNING_FAILED` | `error` | — | `setReviewReasonErrors`, `setError` |
| `USER_STOPPED` | `backlog` | `noPlanYet` | `clearReviewReason` |
| `USER_STOPPED` | `human_review` | — (else) | `setReviewReasonStopped` |
| `PROCESS_EXITED` | `error` | `unexpectedExit` | `setReviewReasonErrors` |

#### `plan_review`
| Event | Target | Guard | Actions |
|---|---|---|---|
| `PLAN_APPROVED` | `coding` | — | `clearReviewReason` |
| `USER_STOPPED` | `backlog` | — | `clearReviewReason` |
| `PROCESS_EXITED` | `error` | `unexpectedExit` | `setReviewReasonErrors` |

#### `coding`
| Event | Target | Guard | Actions |
|---|---|---|---|
| `QA_STARTED` | `qa_review` | — | — |
| `ALL_SUBTASKS_DONE` | `qa_review` | — | — |
| `QA_PASSED` | `human_review` | — | `setReviewReasonCompleted` (fallback: missed QA_STARTED) |
| `CODING_FAILED` | `error` | — | `setReviewReasonErrors`, `setError` |
| `QA_MAX_ITERATIONS` | `error` | — | `setReviewReasonErrors` (fallback) |
| `QA_AGENT_ERROR` | `error` | — | `setReviewReasonErrors` (fallback) |
| `USER_STOPPED` | `human_review` | — | `setReviewReasonStopped` |
| `PROCESS_EXITED` | `error` | `unexpectedExit` | `setReviewReasonErrors` |

#### `qa_review`
| Event | Target | Guard | Actions |
|---|---|---|---|
| `QA_FAILED` | `qa_fixing` | — | — |
| `QA_PASSED` | `human_review` | — | `setReviewReasonCompleted` |
| `QA_MAX_ITERATIONS` | `error` | — | `setReviewReasonErrors` |
| `QA_AGENT_ERROR` | `error` | — | `setReviewReasonErrors` |
| `USER_STOPPED` | `human_review` | — | `setReviewReasonStopped` |
| `PROCESS_EXITED` | `error` | `unexpectedExit` | `setReviewReasonErrors` |

#### `qa_fixing`
| Event | Target | Guard | Actions |
|---|---|---|---|
| `QA_FIXING_COMPLETE` | `qa_review` | — | — |
| `QA_FAILED` | `human_review` | — | `setReviewReasonQaRejected` (back-to-back QA_FAILED = qa_rejected) |
| `QA_PASSED` | `human_review` | — | `setReviewReasonCompleted` |
| `QA_MAX_ITERATIONS` | `error` | — | `setReviewReasonErrors` |
| `QA_AGENT_ERROR` | `error` | — | `setReviewReasonErrors` |
| `USER_STOPPED` | `human_review` | — | `setReviewReasonStopped` |
| `PROCESS_EXITED` | `error` | `unexpectedExit` | `setReviewReasonErrors` |

#### `human_review`
| Event | Target | Guard | Actions |
|---|---|---|---|
| `CREATE_PR` | `creating_pr` | — | — |
| `MARK_DONE` | `done` | — | — |
| `USER_RESUMED` | `coding` | — | `clearReviewReason` |
| `PLANNING_STARTED` | `planning` | — | `clearReviewReason` (fallback: re-plan incomplete task) |

#### `error`
| Event | Target | Guard | Actions |
|---|---|---|---|
| `USER_RESUMED` | `coding` | — | `clearReviewReason` |
| `PLANNING_STARTED` | `planning` | — | `clearReviewReason` (re-plan after crash) |
| `MARK_DONE` | `done` | — | — |

#### `creating_pr`
| Event | Target | Guard | Actions |
|---|---|---|---|
| `PR_CREATED` | `pr_created` | — | — |

#### `pr_created`
| Event | Target | Guard | Actions |
|---|---|---|---|
| `MARK_DONE` | `done` | — | — |

#### `done`
- Type: `final` — no outgoing transitions.

### Guards

```ts
guards: {
  requiresReview: ({ event }) =>
    event.type === 'PLANNING_COMPLETE' && event.requireReviewBeforeCoding === true,

  noPlanYet: ({ event }) =>
    event.type === 'USER_STOPPED' && event.hasPlan === false,

  unexpectedExit: ({ event }) =>
    event.type === 'PROCESS_EXITED' && event.unexpected === true
}
```

### Actions

```ts
actions: {
  setReviewReasonPlan:      assign({ reviewReason: () => 'plan_review' }),
  setReviewReasonCompleted: assign({ reviewReason: () => 'completed' }),
  setReviewReasonStopped:   assign({ reviewReason: () => 'stopped' }),
  setReviewReasonQaRejected:assign({ reviewReason: () => 'qa_rejected' }),
  setReviewReasonErrors:    assign({ reviewReason: () => 'errors' }),
  clearReviewReason:        assign({ reviewReason: () => undefined, error: () => undefined }),
  setError: assign({
    error: ({ event }) => {
      if (event.type === 'PLANNING_FAILED') return event.error;
      if (event.type === 'CODING_FAILED') return event.error;
      return undefined;
    }
  })
}
```

### Machine Creation

```ts
import { assign, createMachine } from 'xstate';

export const taskMachine = createMachine(
  { id: 'task', initial: 'backlog', types: {} as { context: TaskContext; events: TaskEvent }, context: { ... }, states: { ... } },
  { guards: { ... }, actions: { ... } }
);
```

---

## 2. Task State Utils (`task-state-utils.ts`)

**File:** `apps/desktop/src/shared/state-machines/task-state-utils.ts`

### State Name Constants

```ts
export const TASK_STATE_NAMES = [
  'backlog', 'planning', 'plan_review', 'coding',
  'qa_review', 'qa_fixing', 'human_review', 'error',
  'creating_pr', 'pr_created', 'done'
] as const;

export type TaskStateName = typeof TASK_STATE_NAMES[number];
```

### Settled States (XState is source of truth — don't overwrite with stale agent events)

```ts
export const XSTATE_SETTLED_STATES: ReadonlySet<string> = new Set<TaskStateName>([
  'plan_review', 'human_review', 'error', 'creating_pr', 'pr_created', 'done'
]);
```

Note: `error` is included because stale `phase='failed'` execution-progress events may arrive after XState has transitioned to error. When user resumes (`USER_RESUMED`), XState transitions synchronously to `coding` before new agent events arrive, so the guard no longer blocks new events.

### Active States (process is running — process-exit handler needs to fire)

```ts
export const XSTATE_ACTIVE_STATES: ReadonlySet<string> = new Set<TaskStateName>([
  'planning', 'coding', 'qa_review', 'qa_fixing'
]);
```

### XState-to-ExecutionPhase Mapping

```ts
export const XSTATE_TO_PHASE: Record<TaskStateName, ExecutionPhase> & Record<string, ExecutionPhase | undefined> = {
  'backlog':      'idle',
  'planning':     'planning',
  'plan_review':  'planning',
  'coding':       'coding',
  'qa_review':    'qa_review',
  'qa_fixing':    'qa_fixing',
  'human_review': 'complete',
  'error':        'failed',
  'creating_pr':  'complete',
  'pr_created':   'complete',
  'done':         'complete'
};
```

### Legacy Status Mapping

```ts
export function mapStateToLegacy(
  state: string,
  reviewReason?: ReviewReason
): { status: TaskStatus; reviewReason?: ReviewReason } {
  switch (state) {
    case 'backlog':      return { status: 'backlog' };
    case 'planning':
    case 'coding':       return { status: 'in_progress' };
    case 'plan_review':  return { status: 'human_review', reviewReason: 'plan_review' };
    case 'qa_review':
    case 'qa_fixing':    return { status: 'ai_review' };
    case 'human_review': return { status: 'human_review', reviewReason: reviewReason ?? 'completed' };
    case 'error':        return { status: 'human_review', reviewReason: 'errors' };
    case 'creating_pr':  return { status: 'human_review', reviewReason: 'completed' };
    case 'pr_created':   return { status: 'pr_created' };
    case 'done':         return { status: 'done' };
    default:             return { status: 'backlog' };
  }
}
```

Key notes:
- `error` XState state maps to legacy `human_review` (not `error`) so the UI shows it in the review column
- `creating_pr` also maps to `human_review` with `completed` reason
- `qa_review` and `qa_fixing` both map to `ai_review`

---

## 3. Task State Manager (`task-state-manager.ts`)

**File:** `apps/desktop/src/main/task-state-manager.ts`

### Class Overview

`TaskStateManager` is the central coordinator between:
- Backend task events (from agent processes, via IPC/event bus)
- XState machine actors (one per task)
- The Electron renderer (status updates via IPC)

### Types

```ts
import { createActor } from 'xstate';
import type { ActorRefFrom } from 'xstate';

type TaskActor = ActorRefFrom<typeof taskMachine>;

interface TaskContextEntry {
  task: Task;
  project: Project;
}
```

### Internal State

```ts
private actors = new Map<string, TaskActor>();
private lastSequenceByTask = new Map<string, number>();
private lastStateByTask = new Map<string, string>();
private taskContextById = new Map<string, TaskContextEntry>();
private terminalEventSeen = new Set<string>();
private getMainWindow: (() => BrowserWindow | null) | null = null;
```

- `actors` — one running XState actor per taskId
- `lastSequenceByTask` — monotonic sequence number for deduplication (drops events with sequence < last seen)
- `lastStateByTask` — previous XState state value, used to skip no-op transitions
- `taskContextById` — cached task + project metadata needed for persistence and IPC
- `terminalEventSeen` — set of taskIds that have received a terminal event; suppresses `PROCESS_EXITED` handling after a clean terminal event
- `getMainWindow` — injected factory for getting the Electron BrowserWindow

### Terminal Events

```ts
const TERMINAL_EVENTS = new Set<string>([
  'QA_PASSED',
  'PLANNING_COMPLETE',
  'PLANNING_FAILED',
  'CODING_FAILED',
  'QA_MAX_ITERATIONS',
  'QA_AGENT_ERROR',
  'ALL_SUBTASKS_DONE'
]);
```

Purpose: when a terminal event is received, mark `terminalEventSeen.add(taskId)`. Then in `handleProcessExited`, if the task has already seen a terminal event, the process exit is silently ignored — it was a clean/expected exit.

### Public API

#### `configure(getMainWindow: () => BrowserWindow | null): void`
Must be called once at startup to inject the window factory before any events are handled.

#### `handleTaskEvent(taskId, event, task, project): boolean`

```ts
handleTaskEvent(taskId: string, event: TaskEventPayload, task: Task, project: Project): boolean
```

1. Calls `isNewSequence(taskId, event.sequence)` — drops duplicate/out-of-order events. Returns `false` if dropped.
2. Calls `setTaskContext(taskId, task, project)`.
3. Updates `lastSequenceByTask.set(taskId, event.sequence)`.
4. If `TERMINAL_EVENTS.has(event.type)`, marks `terminalEventSeen.add(taskId)`.
5. Gets or creates actor via `getOrCreateActor(taskId)`.
6. Sends the event to the actor.
7. Returns `true` (event was processed).

#### `handleProcessExited(taskId, exitCode, task?, project?): void`

```ts
handleProcessExited(
  taskId: string,
  exitCode: number | null,
  task?: Task,
  project?: Project
): void
```

1. If `task` and `project` are provided, sets context.
2. If `terminalEventSeen.has(taskId)`, returns early — clean exit after terminal event, no state change needed.
3. Determines `isUnexpected = exitCode !== 0`. Exit code 0 is always expected (spec done, plan created for review, etc.). Non-zero triggers `error` transition.
4. Sends `{ type: 'PROCESS_EXITED', exitCode: exitCode ?? -1, unexpected: isUnexpected }`.

#### `handleUiEvent(taskId, event, task, project): void`

```ts
handleUiEvent(taskId: string, event: TaskEvent, task: Task, project: Project): void
```

Routes UI-originated events (user clicks) directly to the actor. Bypasses sequence tracking.

#### `handleManualStatusChange(taskId, status, task, project): boolean`

```ts
handleManualStatusChange(taskId: string, status: TaskStatus, task: Task, project: Project): boolean
```

Maps legacy UI status changes to the correct XState events:

| `status` | Logic |
|---|---|
| `'done'` | sends `MARK_DONE` |
| `'pr_created'` | sends `PR_CREATED` with `task.metadata?.prUrl` |
| `'in_progress'` | checks `getCurrentState(taskId)`: if `plan_review` → `PLAN_APPROVED`; if `human_review` or `error` → `USER_RESUMED`; if no actor but `task.reviewReason === 'plan_review'` → `PLAN_APPROVED` (fallback after restart); else → `USER_RESUMED` |
| `'backlog'` | sends `USER_STOPPED` with `hasPlan: false` |
| `'human_review'` | calls `emitStatus` directly (no XState transition needed; used for stage-only merge that keeps task in review) |
| other | returns `false` |

#### `getCurrentState(taskId): string | undefined`
Returns current XState state string, or `undefined` if no actor exists.

#### `isInPlanReview(taskId): boolean`
Convenience wrapper: `getCurrentState(taskId) === 'plan_review'`. Used by `TASK_START` handler.

#### `prepareForRestart(taskId): void`
Called before restarting a task. Clears:
- `terminalEventSeen` (so next process exit is not swallowed)
- `lastSequenceByTask` (so events from new process are not dropped as duplicates)

Does NOT stop or remove the actor — callers may still need to send events to it.

#### `setLastSequence(taskId, sequence): void` / `getLastSequence(taskId): number | undefined`
Direct read/write of sequence tracking. Used to pre-load sequence from persisted plan file on startup.

#### `clearTask(taskId): void`
Full cleanup: removes actor (stops it), clears all Maps for the task.

#### `clearAllTasks(): void`
Stops and removes all actors. Preserves `lastSequenceByTask` (sequence numbers remain valid across UI refreshes to prevent duplicate processing). Clears everything else.

### Private: `getOrCreateActor(taskId): TaskActor`

```ts
private getOrCreateActor(taskId: string): TaskActor
```

1. Returns existing actor if present.
2. Otherwise, looks up `taskContextById.get(taskId)`.
3. If context exists, calls `buildSnapshotFromTask(task)` to get initial XState snapshot.
4. Creates actor: `createActor(taskMachine, { snapshot })` or `createActor(taskMachine)`.
5. Subscribes to actor state changes:
   - Skips if state unchanged (`lastStateByTask` guard).
   - Updates `lastStateByTask`.
   - Calls `mapStateToLegacy(stateValue, snapshot.context.reviewReason)` to get `{ status, reviewReason }`.
   - Calls `mapStateToExecutionPhase(stateValue)` for `ExecutionPhase`.
   - Calls `persistStatus(...)` to write to plan file (main + worktree).
   - Calls `emitStatus(...)` to send `TASK_STATUS_CHANGE` IPC to renderer.
6. Calls `actor.start()`, stores in `actors` map.

### Private: `buildSnapshotFromTask(task): XState snapshot`

```ts
private buildSnapshotFromTask(task: Task)
```

Reconstructs XState state from persisted task data (used after app restart):

| `task.status` | `task.reviewReason` / `executionPhase` | XState state |
|---|---|---|
| `'in_progress'` | `phase === 'planning'` | `planning` |
| `'in_progress'` | `phase === 'qa_review'` | `qa_review` |
| `'in_progress'` | `phase === 'qa_fixing'` | `qa_fixing` |
| `'in_progress'` | other | `coding` |
| `'ai_review'` | — | `qa_review` |
| `'human_review'` | `reviewReason === 'plan_review'` | `plan_review` |
| `'human_review'` | other | `human_review` |
| `'pr_created'` | — | `pr_created` |
| `'done'` | — | `done` |
| `'error'` | — | `error` (context gets `reviewReason ?? 'errors'`) |
| default | — | `backlog` |

Uses `taskMachine.resolveState({ value: stateValue, context: { reviewReason: contextReviewReason } })`.

### Private: `persistStatus(...)`

```ts
private persistStatus(
  task: Task,
  project: Project,
  status: TaskStatus,
  reviewReason?: ReviewReason,
  xstateState?: string,
  executionPhase?: string
): void
```

1. Computes main plan path via `getPlanPath(project, task)`.
2. Calls `persistPlanStatusAndReasonSync(mainPlanPath, status, reviewReason, project.id, xstateState, executionPhase)`.
3. Looks for worktree path via `findTaskWorktree(project.path, task.specId)`.
4. If worktree plan file exists, also persists there.

### Private: `emitStatus(...)`

```ts
private emitStatus(
  taskId: string,
  status: TaskStatus,
  reviewReason: ReviewReason | undefined,
  projectId?: string
): void
```

Calls `safeSendToRenderer(this.getMainWindow, IPC_CHANNELS.TASK_STATUS_CHANGE, taskId, status, projectId, reviewReason)`.

### Private: `isNewSequence(taskId, sequence): boolean`

```ts
private isNewSequence(taskId: string, sequence: number): boolean {
  const last = this.lastSequenceByTask.get(taskId);
  // Use >= to accept first event when sequence equals last (e.g., both are 0).
  // Handles case where lastSequence is reloaded from plan file and next event
  // has same sequence number.
  return last === undefined || sequence >= last;
}
```

### Singleton Export

```ts
export const taskStateManager = new TaskStateManager();
```

---

## 4. State Machine Index (`index.ts`)

**File:** `apps/desktop/src/shared/state-machines/index.ts`

Barrel re-exports:

```ts
export { taskMachine } from './task-machine';
export type { TaskContext, TaskEvent } from './task-machine';
export { TASK_STATE_NAMES, XSTATE_SETTLED_STATES, XSTATE_ACTIVE_STATES, XSTATE_TO_PHASE, mapStateToLegacy } from './task-state-utils';
export type { TaskStateName } from './task-state-utils';

export { prReviewMachine } from './pr-review-machine';
export type { PRReviewContext, PRReviewEvent } from './pr-review-machine';
export { PR_REVIEW_STATE_NAMES, PR_REVIEW_SETTLED_STATES, mapPRReviewStateToLegacy } from './pr-review-state-utils';
export type { PRReviewStateName } from './pr-review-state-utils';

export { terminalMachine } from './terminal-machine';
export type { TerminalContext, TerminalEvent } from './terminal-machine';

export { roadmapGenerationMachine } from './roadmap-generation-machine';
export type { RoadmapGenerationContext, RoadmapGenerationEvent } from './roadmap-generation-machine';

export { roadmapFeatureMachine } from './roadmap-feature-machine';
export type { RoadmapFeatureContext, RoadmapFeatureEvent } from './roadmap-feature-machine';

export { GENERATION_STATE_NAMES, FEATURE_STATE_NAMES, GENERATION_SETTLED_STATES, FEATURE_SETTLED_STATES, mapGenerationStateToPhase, mapFeatureStateToStatus } from './roadmap-state-utils';
export type { GenerationStateName, FeatureStateName } from './roadmap-state-utils';
```

---

## 5. PR Review Machine (`pr-review-machine.ts`)

**File:** `apps/desktop/src/shared/state-machines/pr-review-machine.ts`

### Context

```ts
export interface PRReviewContext {
  prNumber: number | null;
  projectId: string | null;
  startedAt: string | null;
  isFollowup: boolean;
  progress: PRReviewProgress | null;
  result: PRReviewResult | null;
  previousResult: PRReviewResult | null;
  error: string | null;
  isExternalReview: boolean;
}
```

### Events

```ts
export type PRReviewEvent =
  | { type: 'START_REVIEW'; prNumber: number; projectId: string }
  | { type: 'START_FOLLOWUP_REVIEW'; prNumber: number; projectId: string; previousResult: PRReviewResult }
  | { type: 'SET_PROGRESS'; progress: PRReviewProgress }
  | { type: 'REVIEW_COMPLETE'; result: PRReviewResult }
  | { type: 'REVIEW_ERROR'; error: string }
  | { type: 'CANCEL_REVIEW' }
  | { type: 'DETECT_EXTERNAL_REVIEW' }
  | { type: 'CLEAR_REVIEW' };
```

### States

Initial: `idle`

| State | On | Target |
|---|---|---|
| `idle` | `START_REVIEW` | `reviewing` + `setReviewStart` |
| `idle` | `START_FOLLOWUP_REVIEW` | `reviewing` + `setFollowupReviewStart` |
| `reviewing` | `SET_PROGRESS` | self + `setProgress` |
| `reviewing` | `REVIEW_COMPLETE` | `completed` + `setResult` |
| `reviewing` | `REVIEW_ERROR` | `error` + `setError` |
| `reviewing` | `CANCEL_REVIEW` | `error` + `setCancelledError` |
| `reviewing` | `CLEAR_REVIEW` | `idle` + `clearContext` |
| `reviewing` | `DETECT_EXTERNAL_REVIEW` | `externalReview` + `setExternalReview` |
| `externalReview` | `REVIEW_COMPLETE` | `completed` + `setResult` |
| `externalReview` | `REVIEW_ERROR` | `error` + `setError` |
| `externalReview` | `CANCEL_REVIEW` | `error` + `setCancelledError` |
| `externalReview` | `CLEAR_REVIEW` | `idle` + `clearContext` |
| `completed` | `START_REVIEW` | `reviewing` + `setReviewStart` |
| `completed` | `START_FOLLOWUP_REVIEW` | `reviewing` + `setFollowupReviewStart` |
| `completed` | `REVIEW_COMPLETE` | self + `setResult` (update result in place) |
| `completed` | `CLEAR_REVIEW` | `idle` + `clearContext` |
| `error` | `START_REVIEW` | `reviewing` + `setReviewStart` |
| `error` | `START_FOLLOWUP_REVIEW` | `reviewing` + `setFollowupReviewStart` |
| `error` | `CLEAR_REVIEW` | `idle` + `clearContext` |

### Settled States

```ts
export const PR_REVIEW_SETTLED_STATES: ReadonlySet<string> = new Set<PRReviewStateName>([
  'completed', 'error'
]);
```

### Legacy Mapping

```ts
export function mapPRReviewStateToLegacy(state: string): 'idle' | 'reviewing' | 'completed' | 'error' {
  // 'externalReview' maps to 'reviewing'
  // all others map 1:1
}
```

---

## 6. Terminal Machine (`terminal-machine.ts`)

**File:** `apps/desktop/src/shared/state-machines/terminal-machine.ts`

### Context

```ts
export interface TerminalContext {
  claudeSessionId?: string;
  profileId?: string;
  swapTargetProfileId?: string;
  swapPhase?: 'capturing' | 'migrating' | 'recreating' | 'resuming';
  isBusy: boolean;
  error?: string;
}
```

### Events

```ts
export type TerminalEvent =
  | { type: 'SHELL_READY' }
  | { type: 'CLAUDE_START'; profileId: string }
  | { type: 'CLAUDE_ACTIVE'; claudeSessionId?: string }
  | { type: 'CLAUDE_BUSY'; isBusy: boolean }
  | { type: 'CLAUDE_EXITED'; exitCode?: number; error?: string }
  | { type: 'SWAP_INITIATED'; targetProfileId: string }
  | { type: 'SWAP_SESSION_CAPTURED'; claudeSessionId: string }
  | { type: 'SWAP_MIGRATED' }
  | { type: 'SWAP_TERMINAL_RECREATED' }
  | { type: 'SWAP_RESUME_COMPLETE'; claudeSessionId?: string; profileId: string }
  | { type: 'SWAP_FAILED'; error: string }
  | { type: 'RESUME_REQUESTED'; claudeSessionId: string }
  | { type: 'RESUME_COMPLETE'; claudeSessionId?: string }
  | { type: 'RESUME_FAILED'; error: string }
  | { type: 'SHELL_EXITED'; exitCode?: number; signal?: string }
  | { type: 'RESET' };
```

### States

Initial: `idle`

`idle` → `shell_ready` on `SHELL_READY`

`shell_ready` → `claude_starting` on `CLAUDE_START` (sets `profileId`)
`shell_ready` → `claude_active` on `CLAUDE_ACTIVE` (sets `claudeSessionId`)
`shell_ready` → `pending_resume` on `RESUME_REQUESTED`
`shell_ready` → `exited` on `SHELL_EXITED`

`claude_starting` → `claude_active` on `CLAUDE_ACTIVE`
`claude_starting` → `shell_ready` on `CLAUDE_EXITED` (sets error, clears session)

`claude_active` → `swapping` on `SWAP_INITIATED` (guard: `hasActiveSession`, sets swap target + phase=capturing)
`claude_active` → `pending_resume` on `RESUME_REQUESTED`
`claude_active` → `shell_ready` on `CLAUDE_EXITED`
`claude_active` self-transition on `CLAUDE_ACTIVE` (updates session ID without resetting `isBusy`)
`claude_active` self-transition on `CLAUDE_BUSY` (sets `isBusy`)

`swapping` — four-phase sequence tracked via `swapPhase`:
- `SWAP_SESSION_CAPTURED` (guard: `isCapturingPhase`) → sets `claudeSessionId` + phase=migrating
- `SWAP_MIGRATED` (guard: `isMigratingPhase`) → phase=recreating
- `SWAP_TERMINAL_RECREATED` (guard: `isRecreatingPhase`) → phase=resuming
- `SWAP_RESUME_COMPLETE` (guard: `isResumingPhase`) → `claude_active`, applies new profileId/sessionId, clears swap state
- `SWAP_FAILED` → `shell_ready`, sets error, clears swap state

`pending_resume`:
- `CLAUDE_ACTIVE` → `claude_active`
- `RESUME_COMPLETE` → `claude_active`
- `RESUME_FAILED` → `shell_ready` (sets error, clears session)

`exited` → `shell_ready` on `SHELL_READY` (clears error)

Any state: `RESET` → `idle` (resets entire context)
Any active state: `SHELL_EXITED` → `exited` (clears session)

### Guards

```ts
guards: {
  hasActiveSession: ({ context }) => context.claudeSessionId !== undefined,
  isCapturingPhase:  ({ context }) => context.swapPhase === 'capturing',
  isMigratingPhase:  ({ context }) => context.swapPhase === 'migrating',
  isRecreatingPhase: ({ context }) => context.swapPhase === 'recreating',
  isResumingPhase:   ({ context }) => context.swapPhase === 'resuming',
}
```

### Key Action: `updateClaudeSessionId` vs `setClaudeSessionId`

- `setClaudeSessionId` — used on state transitions into `claude_active`: sets `claudeSessionId`, resets `isBusy` to `false`, clears error.
- `updateClaudeSessionId` — used for self-transition `CLAUDE_ACTIVE` within `claude_active`: updates session ID only, **preserves `isBusy`** so the busy indicator isn't reset when session ID refreshes mid-task.

---

## 7. Roadmap Generation Machine (`roadmap-generation-machine.ts`)

**File:** `apps/desktop/src/shared/state-machines/roadmap-generation-machine.ts`

### States

Initial: `idle`

```
idle → analyzing (START_GENERATION, sets startedAt, resets context)
analyzing → discovering (DISCOVERY_STARTED)
analyzing → error (GENERATION_ERROR)
discovering → generating (GENERATION_STARTED)
discovering → error (GENERATION_ERROR)
generating → complete (GENERATION_COMPLETE, sets completedAt, progress=100)
generating → error (GENERATION_ERROR)
complete → idle (RESET)
error → idle (RESET)
```

All active states accept `PROGRESS_UPDATE` (self-transition, updates progress 0-100 + message + lastActivityAt) and `STOP` (→ idle, resets).

### Settled States

```ts
export const GENERATION_SETTLED_STATES = new Set(['complete', 'error']);
```

---

## 8. Roadmap Feature Machine (`roadmap-feature-machine.ts`)

**File:** `apps/desktop/src/shared/state-machines/roadmap-feature-machine.ts`

### States

Initial: `under_review`

```
under_review → planned (PLAN)
under_review → in_progress (START_PROGRESS or LINK_SPEC)
under_review → done (MARK_DONE, TASK_COMPLETED, TASK_DELETED, TASK_ARCHIVED)

planned → in_progress (START_PROGRESS or LINK_SPEC)
planned → done (MARK_DONE, TASK_COMPLETED, TASK_DELETED, TASK_ARCHIVED)
planned → under_review (MOVE_TO_REVIEW)

in_progress → done (MARK_DONE, TASK_COMPLETED, TASK_DELETED, TASK_ARCHIVED)
in_progress → under_review (MOVE_TO_REVIEW, clears done context)
in_progress → planned (PLAN, clears done context)
in_progress self (LINK_SPEC, updates linkedSpecId)

done → in_progress (REVERT, guard: previousWasInProgress)
done → planned (REVERT, guard: previousWasPlanned)
done → under_review (REVERT, else)
done self (MARK_DONE, TASK_COMPLETED, TASK_DELETED, TASK_ARCHIVED — updates taskOutcome)
done → under_review (MOVE_TO_REVIEW)
done → planned (PLAN)
done → in_progress (START_PROGRESS)
```

Transitions to `done` save `previousStatus` (for REVERT) and `taskOutcome` (`completed | deleted | archived`).

### Settled States

```ts
export const FEATURE_SETTLED_STATES = new Set(['done']);
```

---

## 9. Key Design Patterns

### Actor-per-Entity

One XState actor per `taskId`, stored in a `Map<string, TaskActor>`. Actors are created lazily on first event. Snapshot is restored from persisted task data on creation (enabling cross-session state continuity after app restart).

### Sequence Deduplication

Every backend event payload carries a monotonic `sequence: number`. `TaskStateManager` tracks `lastSequenceByTask` and drops events where `sequence < last`. Uses `>=` to accept the first event when both are 0 (lenient for reload scenarios).

Sequence tracking is preserved across `clearAllTasks()` (UI refresh) but cleared by `prepareForRestart(taskId)` (process restart).

### Terminal Event Gate

`TERMINAL_EVENTS` set marks events that signal a clean process termination. Once a terminal event is seen for a task, subsequent `PROCESS_EXITED` calls are no-ops — the process exit was expected.

### Process Exit Semantics

```ts
const isUnexpected = exitCode !== 0;
```

Exit code 0 = normal/expected (plan created and waiting for review, spec done). Non-zero = crash/error. This prevents `plan_review → error` on a normal exit-after-planning.

### State Persistence

On every XState state transition, `persistStatus` writes the new status to the plan file (JSON) via `persistPlanStatusAndReasonSync`. This is synchronous (blocking) to guarantee persistence before any IPC is sent. Both the main repo and the worktree copy of the plan file are updated.

### IPC State Sync

After persisting, `emitStatus` calls `safeSendToRenderer(getMainWindow, IPC_CHANNELS.TASK_STATUS_CHANGE, ...)` to push the new `{ taskId, status, projectId, reviewReason }` tuple to the renderer process.

### Snapshot Restoration

On actor creation, if a `TaskContextEntry` exists, `buildSnapshotFromTask` maps the persisted `task.status` + `task.reviewReason` + `task.executionProgress.phase` to a concrete XState state value. This is passed to `createActor(taskMachine, { snapshot })` so the actor starts in the correct state rather than `backlog`.

### Fallback Transitions

Many states handle events they shouldn't normally see (e.g., `QA_PASSED` arriving while still in `coding` because `QA_STARTED` was missed). These "fallback" transitions are documented with comments in the machine and ensure the UI never gets stuck due to lost or out-of-order events.

### Manual Status Override Mapping

`handleManualStatusChange` uses the **current XState state** (not the legacy `task.status`) as the source of truth when deciding which event to send for `'in_progress'`. This avoids sending `USER_RESUMED` when the correct event is `PLAN_APPROVED`.
