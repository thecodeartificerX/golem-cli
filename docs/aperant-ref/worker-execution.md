# Aperant Worker Execution Reference

Extracted from `apps/desktop/src/main/ai/agent/` and `apps/desktop/src/main/agent/`.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Type Definitions](#type-definitions)
3. [Worker Thread Isolation Pattern](#worker-thread-isolation-pattern)
4. [Message Passing Protocol](#message-passing-protocol)
5. [WorkerBridge — AgentManagerEvents Translation](#workerbridge--agentmanagerevents-translation)
6. [AgentExecutor Lifecycle](#agentexecutor-lifecycle)
7. [Token Refresh Mid-Session](#token-refresh-mid-session)
8. [ProgressTracker](#progresstracker)
9. [AgentManager Facade and Modular Components](#agentmanager-facade-and-modular-components)
10. [Auto-Swap Restart Mechanism](#auto-swap-restart-mechanism)
11. [spawnWorkerProcess — Full Integration Path](#spawnworkerprocess--full-integration-path)
12. [Worker Entry Point Routing](#worker-entry-point-routing)
13. [SecurityProfile Serialization Across Worker Boundaries](#securityprofile-serialization-across-worker-boundaries)

---

## Architecture Overview

```
AgentManager (facade, EventEmitter)
  └── AgentProcessManager
        └── spawnWorkerProcess()
              └── WorkerBridge (EventEmitter, main thread)
                    └── Worker thread (worker.ts via worker_threads)
                          ├── runBuildOrchestrator()
                          ├── runQALoop()
                          ├── runSpecOrchestrator()
                          └── runDefaultSession()
                                └── runContinuableSession()
```

**Key invariant:** The UI and event consumers cannot distinguish between a Python subprocess (legacy) and a TS worker thread. Both paths emit identical `AgentManagerEvents` (`log`, `error`, `exit`, `execution-progress`, `task-event`).

---

## Type Definitions

**File:** `apps/desktop/src/main/ai/agent/types.ts`

### WorkerConfig

Passed to the worker thread via `workerData`. Must be fully serializable — no class instances, functions, or LanguageModel objects.

```ts
export interface WorkerConfig {
  taskId: string;
  projectId?: string;
  processType: ProcessType;
  session: SerializableSessionConfig;
}
```

### SerializableSessionConfig

The worker cannot receive a live `LanguageModel` instance. Instead it receives provider/model identifiers and reconstructs the model internally.

```ts
export interface SerializableSessionConfig {
  agentType: SessionConfig['agentType'];
  systemPrompt: string;
  initialMessages: SessionConfig['initialMessages'];
  maxSteps: number;
  specDir: string;
  projectDir: string;
  sourceSpecDir?: string;           // for worktree → main sync
  phase?: SessionConfig['phase'];
  modelShorthand?: SessionConfig['modelShorthand'];
  thinkingLevel?: SessionConfig['thinkingLevel'];
  sessionNumber?: SessionConfig['sessionNumber'];
  subtaskId?: SessionConfig['subtaskId'];
  provider: string;                 // e.g. 'anthropic', 'openai'
  modelId: string;                  // e.g. 'claude-opus-4-6'
  apiKey?: string;
  baseURL?: string;
  configDir?: string;               // OAuth profile directory
  oauthTokenFilePath?: string;      // pre-resolved path for file-based OAuth
  mcpOptions?: {
    context7Enabled?: boolean;
    memoryEnabled?: boolean;
    linearEnabled?: boolean;
    electronMcpEnabled?: boolean;
    puppeteerMcpEnabled?: boolean;
    projectCapabilities?: { is_electron?: boolean; is_web_frontend?: boolean };
    agentMcpAdd?: string;
    agentMcpRemove?: string;
  };
  useAgenticOrchestration?: boolean;
  toolContext: {
    cwd: string;
    projectDir: string;
    specDir: string;
    securityProfile?: SerializedSecurityProfile; // Sets serialized to arrays
  };
}
```

### WorkerMessage (worker → main, discriminated union)

```ts
export type WorkerMessage =
  | WorkerLogMessage          // { type: 'log',               taskId, data: string,                  projectId? }
  | WorkerErrorMessage        // { type: 'error',             taskId, data: string,                  projectId? }
  | WorkerProgressMessage     // { type: 'execution-progress', taskId, data: ExecutionProgressData,   projectId? }
  | WorkerStreamEventMessage  // { type: 'stream-event',      taskId, data: StreamEvent,             projectId? }
  | WorkerResultMessage       // { type: 'result',            taskId, data: SessionResult,           projectId? }
  | WorkerTaskEventMessage;   // { type: 'task-event',        taskId, data: Record<string, unknown>, projectId? }
```

### MainToWorkerMessage (main → worker)

```ts
export type MainToWorkerMessage =
  | { type: 'abort' };
```

### AgentExecutorConfig

```ts
export interface AgentExecutorConfig {
  taskId: string;
  projectId?: string;
  processType: ProcessType;
  session: SerializableSessionConfig;
  onAuthRefresh?: RunnerOptions['onAuthRefresh'];
}
```

### SerializedSecurityProfile

`SecurityProfile` uses `Set` objects which cannot cross worker boundaries. Serialized to arrays before transfer, reconstructed in the worker.

```ts
export interface SerializedSecurityProfile {
  baseCommands: string[];
  stackCommands: string[];
  scriptCommands: string[];
  customCommands: string[];
  customScripts: { shellScripts: string[] };
}
```

---

## Worker Thread Isolation Pattern

**File:** `apps/desktop/src/main/ai/agent/worker.ts`

### Entry point validation

The worker validates at module load time that it is running inside a `worker_thread` and that `workerData` is well-formed:

```ts
import { parentPort, workerData } from 'worker_threads';

if (!parentPort) {
  throw new Error('worker.ts must be run inside a worker_thread');
}

const config = workerData as WorkerConfig;
if (!config?.taskId || !config?.session) {
  throw new Error('worker.ts requires valid WorkerConfig via workerData');
}
```

### TaskLogWriter (module-scope, shared across all sessions in the worker)

```ts
const logWriter = config.session.specDir
  ? new TaskLogWriter(config.session.specDir, basename(config.session.specDir))
  : null;
```

All planning / coding / QA phases accumulate into a single `task_logs.json` in the worker's lifetime.

### Messaging helpers (module-scope functions)

```ts
function postMessage(message: WorkerMessage): void {
  parentPort!.postMessage(message);
}

function postLog(data: string): void {
  postMessage({ type: 'log', taskId: config.taskId, data, projectId: config.projectId });
}

function postError(data: string): void {
  postMessage({ type: 'error', taskId: config.taskId, data, projectId: config.projectId });
}

function postTaskEvent(eventType: string, extra?: Record<string, unknown>): void {
  parentPort?.postMessage({
    type: 'task-event',
    taskId: config.taskId,
    projectId: config.projectId,
    data: {
      type: eventType,
      taskId: config.taskId,
      specId: config.session.specDir ? basename(config.session.specDir) : config.taskId,
      projectId: config.projectId ?? '',
      timestamp: new Date().toISOString(),
      eventId: `${config.taskId}-${eventType}-${Date.now()}`,
      sequence: Date.now(),
      ...extra,
    },
  } satisfies WorkerTaskEventMessage);
}
```

### Abort signal (module-scope AbortController)

```ts
const abortController = new AbortController();

parentPort.on('message', (msg: MainToWorkerMessage) => {
  if (msg.type === 'abort') {
    abortController.abort();
  }
});
```

The `abortController.signal` is threaded into every `SessionConfig` and `BuildOrchestrator` / `QALoop` / `SpecOrchestrator` so they all stop on abort.

### Worker path resolution

**File:** `apps/desktop/src/main/ai/agent/worker-bridge.ts`

```ts
function resolveWorkerPath(): string {
  if (app.isPackaged) {
    // Production: inside app.asar
    return path.join(process.resourcesPath, 'app.asar', 'out', 'main', 'ai', 'agent', 'worker.js');
  }
  // Dev: electron-vite outputs to out/main/, __dirname resolves there
  return path.join(__dirname, 'ai', 'agent', 'worker.js');
}
```

The Rollup input key must be `'ai/agent/worker'` so electron-vite emits the file at the expected subpath.

### SecurityProfile reconstruction in worker

`Set` objects are not transferable, so the main thread serializes them to arrays (`SerializedSecurityProfile`) and the worker reconstructs:

```ts
function buildSecurityProfile(session: SerializableSessionConfig): SecurityProfile {
  const serialized = session.toolContext.securityProfile;
  return {
    baseCommands: new Set(serialized?.baseCommands ?? []),
    stackCommands: new Set(serialized?.stackCommands ?? []),
    scriptCommands: new Set(serialized?.scriptCommands ?? []),
    customCommands: new Set(serialized?.customCommands ?? []),
    customScripts: { shellScripts: serialized?.customScripts?.shellScripts ?? [] },
    getAllAllowedCommands() {
      return new Set([
        ...this.baseCommands,
        ...this.stackCommands,
        ...this.scriptCommands,
        ...this.customCommands,
      ]);
    },
  };
}
```

The main thread serializes with:

```ts
// File: apps/desktop/src/main/agent/agent-manager.ts
private serializeSecurityProfile(projectDir: string): SerializedSecurityProfile {
  const profile = getSecurityProfile(projectDir);
  return {
    baseCommands: [...profile.baseCommands],
    stackCommands: [...profile.stackCommands],
    scriptCommands: [...profile.scriptCommands],
    customCommands: [...profile.customCommands],
    customScripts: { shellScripts: profile.customScripts.shellScripts },
  };
}
```

---

## Message Passing Protocol

**File:** `apps/desktop/src/main/ai/agent/worker-bridge.ts`

All communication is unidirectional by design:

- **Worker → Main:** `parentPort.postMessage(WorkerMessage)` — carries log, error, stream-event, execution-progress, task-event, result
- **Main → Worker:** `worker.postMessage(MainToWorkerMessage)` — only carries `{ type: 'abort' }`

### Message flow for a normal session

```
Main thread                          Worker thread
    |                                    |
    | new Worker(path, { workerData })   |
    |─────────────────────────────────>  |
    |                                    | start run()
    |  <── { type: 'log', ... } ─────── |
    |  <── { type: 'stream-event', .. } |
    |  <── { type: 'execution-progress' }|
    |  <── { type: 'task-event', ... }  |
    |  <── { type: 'result', ... } ─── |
    | (bridge emits 'exit' and cleans up)|
```

### Message flow for abort

```
Main thread                          Worker thread
    |                                    |
    | worker.postMessage({ type:'abort'})|
    |─────────────────────────────────>  |
    |                                    | abortController.abort()
    |                                    | (session stops at next check)
    | worker.terminate()                 |
    |─────────────────────────────────>  | (force-terminate after grace period)
```

---

## WorkerBridge — AgentManagerEvents Translation

**File:** `apps/desktop/src/main/ai/agent/worker-bridge.ts`

`WorkerBridge extends EventEmitter` and implements the `AgentManagerEvents` interface so it is transparent to all consumers.

### Class structure

```ts
export class WorkerBridge extends EventEmitter {
  private worker: Worker | null = null;
  private progressTracker: ProgressTracker = new ProgressTracker();
  private taskId: string = '';
  private projectId: string | undefined;
  private processType: ProcessType = 'task-execution';

  spawn(config: AgentExecutorConfig): void { ... }
  async terminate(): Promise<void> { ... }
  get isActive(): boolean { return this.worker !== null; }
  get workerInstance(): Worker | null { return this.worker; }
}
```

### spawn()

```ts
spawn(config: AgentExecutorConfig): void {
  if (this.worker) {
    throw new Error('WorkerBridge already has an active worker. Call terminate() first.');
  }

  this.taskId = config.taskId;
  this.projectId = config.projectId;
  this.processType = config.processType;
  this.progressTracker = new ProgressTracker();

  const workerConfig: WorkerConfig = {
    taskId: config.taskId,
    projectId: config.projectId,
    processType: config.processType,
    session: config.session,
  };

  this.worker = new Worker(resolveWorkerPath(), { workerData: workerConfig });

  this.worker.on('message', (message: WorkerMessage) => {
    this.handleWorkerMessage(message);
  });

  this.worker.on('error', (error: Error) => {
    this.emitTyped('error', this.taskId, error.message, this.projectId);
    this.cleanup();
  });

  this.worker.on('exit', (code: number) => {
    if (this.worker) {
      this.emitTyped('exit', this.taskId, code === 0 ? 0 : code, this.processType, this.projectId);
      this.cleanup();
    }
  });
}
```

### terminate()

```ts
async terminate(): Promise<void> {
  if (!this.worker) return;

  // Graceful abort first
  try {
    this.worker.postMessage({ type: 'abort' });
  } catch { /* Worker may already be dead */ }

  const worker = this.worker;
  this.cleanup(); // null out this.worker first

  try {
    await worker.terminate(); // Force-terminate
  } catch { /* Already terminated */ }
}
```

### handleWorkerMessage() — the translation switch

```ts
private handleWorkerMessage(message: WorkerMessage): void {
  switch (message.type) {
    case 'log':
      this.emitTyped('log', message.taskId, message.data, message.projectId);
      break;

    case 'error':
      this.emitTyped('error', message.taskId, message.data, message.projectId);
      break;

    case 'execution-progress':
      this.emitTyped('execution-progress', message.taskId, message.data, message.projectId);
      break;

    case 'stream-event':
      // Feed ProgressTracker to derive structured phase data
      this.progressTracker.processEvent(message.data);
      this.emitProgressFromTracker(message.taskId, message.projectId);
      // Also forward raw text as a log entry
      if (message.data.type === 'text-delta') {
        this.emitTyped('log', message.taskId, message.data.text, message.projectId);
      }
      break;

    case 'task-event':
      this.emitTyped('task-event', message.taskId, message.data as TaskEventPayload, message.projectId);
      break;

    case 'result':
      this.handleResult(message.taskId, message.data, message.projectId);
      break;
  }
}
```

### emitProgressFromTracker()

Converts `ProgressTracker` state into the `ExecutionProgressData` shape expected by the UI:

```ts
private emitProgressFromTracker(taskId: string, projectId?: string): void {
  const state = this.progressTracker.state;
  const progressData: ExecutionProgressData = {
    phase: state.currentPhase,
    phaseProgress: 0,
    overallProgress: 0,
    currentSubtask: state.currentSubtask ?? undefined,
    message: state.currentMessage,
    completedPhases: state.completedPhases as ExecutionProgressData['completedPhases'],
  };
  this.emitTyped('execution-progress', taskId, progressData, projectId);
}
```

### handleResult() — outcome to exit code mapping

```ts
private handleResult(taskId: string, result: SessionResult, projectId?: string): void {
  const exitCode =
    result.outcome === 'completed' ||
    result.outcome === 'max_steps' ||
    result.outcome === 'context_window'
      ? 0 : 1;

  const summary = `Session complete: outcome=${result.outcome}, steps=${result.stepsExecuted}, tools=${result.toolCallCount}, duration=${result.durationMs}ms`;
  this.emitTyped('log', taskId, summary, projectId);

  if (result.error) {
    this.emitTyped('error', taskId, result.error.message, projectId);
  }

  this.emitTyped('exit', taskId, exitCode, this.processType, projectId);
  this.cleanup();
}
```

### emitTyped() — type-safe emit

```ts
private emitTyped<K extends keyof AgentManagerEvents>(
  event: K,
  ...args: Parameters<AgentManagerEvents[K]>
): void {
  this.emit(event, ...args);
}
```

---

## AgentExecutor Lifecycle

**File:** `apps/desktop/src/main/ai/agent/executor.ts`

`AgentExecutor` wraps `WorkerBridge` and provides a clean start/stop/retry API. It also extends `EventEmitter` and forwards all bridge events.

### Class

```ts
export class AgentExecutor extends EventEmitter {
  private bridge: WorkerBridge | null = null;
  private config: AgentExecutorConfig;

  constructor(config: AgentExecutorConfig) {
    super();
    this.config = config;
  }

  get isRunning(): boolean { return this.bridge?.isActive ?? false; }
  get taskId(): string { return this.config.taskId; }
}
```

### start()

```ts
start(): void {
  if (this.bridge?.isActive) {
    throw new Error(`Agent executor for task ${this.config.taskId} is already running`);
  }

  this.bridge = new WorkerBridge();
  this.forwardEvents(this.bridge);
  this.bridge.spawn(this.config);
}
```

### stop()

```ts
async stop(): Promise<void> {
  if (!this.bridge) return;
  await this.bridge.terminate();
  this.bridge = null;
}
```

### retry()

```ts
async retry(): Promise<void> {
  await this.stop();
  this.start();
}
```

### updateConfig()

Can update config between retries without affecting the currently running session:

```ts
updateConfig(config: Partial<AgentExecutorConfig>): void {
  this.config = { ...this.config, ...config };
}
```

### forwardEvents()

```ts
private forwardEvents(bridge: WorkerBridge): void {
  const events: (keyof AgentManagerEvents)[] = [
    'log',
    'error',
    'exit',
    'execution-progress',
    'task-event',
  ];

  for (const event of events) {
    bridge.on(event, (...args: unknown[]) => {
      this.emit(event, ...args);
    });
  }

  // Auto-null bridge on exit
  bridge.on('exit', () => {
    this.bridge = null;
  });
}
```

---

## Token Refresh Mid-Session

**File:** `apps/desktop/src/main/ai/agent/worker.ts`

Token refresh is wired through `runContinuableSession` via two callbacks in `RunnerOptions`:

### onAuthRefresh

Called when the provider returns a 401. Re-fetches the OAuth token reactively:

```ts
onAuthRefresh: session.configDir
  ? () => refreshOAuthTokenReactive(session.configDir as string)
  : undefined,
```

`refreshOAuthTokenReactive` (from `apps/desktop/src/main/ai/auth/resolver.ts`) reads the updated token from the config directory and returns it.

### onModelRefresh

Called with the new token value to rebuild the model instance without restarting the session:

```ts
onModelRefresh: session.configDir
  ? (newToken: string) => createProvider({
      config: {
        provider: session.provider as SupportedProvider,
        apiKey: newToken,
        baseURL: session.baseURL,
      },
      modelId: phaseModelId,
    })
  : undefined,
```

This pattern is used identically in both `runSingleSession` (multi-phase) and `runDefaultSession` (single phase). The session runner calls `onAuthRefresh()` first, then passes the new token to `onModelRefresh()` to get a refreshed model instance, and then continues the session from where it left off.

### configDir drives the entire refresh path

If `session.configDir` is `undefined` (queue-based auth, not OAuth profile), both callbacks are `undefined` and the session fails hard on 401. Queue-based auth handles rotation externally.

---

## ProgressTracker

**File:** `apps/desktop/src/main/ai/session/progress-tracker.ts`

`ProgressTracker` converts raw `StreamEvent` objects (from the AI SDK) into structured phase transitions. Used inside `WorkerBridge` to derive `ExecutionProgressData` from `stream-event` messages.

### State

```ts
export interface ProgressTrackerState {
  currentPhase: ExecutionPhase;   // 'idle' | 'planning' | 'coding' | 'qa_review' | 'qa_fixing' | 'complete' | 'failed'
  currentMessage: string;
  currentSubtask: string | null;
  completedPhases: ExecutionPhase[];
}
```

### Class

```ts
export class ProgressTracker {
  private _currentPhase: ExecutionPhase = 'idle';
  private _currentMessage = '';
  private _currentSubtask: string | null = null;
  private _completedPhases: ExecutionPhase[] = [];

  get state(): ProgressTrackerState { ... }
  get currentPhase(): ExecutionPhase { return this._currentPhase; }

  processEvent(event: StreamEvent): PhaseDetection | null { ... }
  forcePhase(phase: ExecutionPhase, message: string, subtask?: string): void { ... }
  reset(): void { ... }
}
```

### processEvent() — detection priority

```ts
processEvent(event: StreamEvent): PhaseDetection | null {
  switch (event.type) {
    case 'tool-call':   return this.processToolCall(event);    // Highest priority — deterministic
    case 'tool-result': return this.processToolResult(event);  // Medium priority
    case 'text-delta':  return this.processTextDelta(event.text); // Fallback — heuristic
    default:            return null;
  }
}
```

### Tool call phase patterns

```ts
// File path → phase mapping
const TOOL_FILE_PHASE_PATTERNS = [
  { pattern: /implementation_plan\.json$/, phase: 'planning', message: 'Creating implementation plan...' },
  { pattern: /qa_report\.md$/,             phase: 'qa_review', message: 'Writing QA report...' },
  { pattern: /QA_FIX_REQUEST\.md$/,        phase: 'qa_fixing', message: 'Processing QA fix request...' },
];

// Tool name → phase mapping
const TOOL_NAME_PHASE_PATTERNS = [
  { toolName: 'update_subtask_status', phase: 'coding',    message: 'Implementing subtask...' },
  { toolName: 'update_qa_status',      phase: 'qa_review', message: 'Updating QA status...' },
];
```

### Text pattern fallback

```ts
const TEXT_PHASE_PATTERNS = [
  { pattern: /qa\s*fix/i,                   phase: 'qa_fixing', message: 'Fixing QA issues...' },
  { pattern: /fixing\s+issues/i,            phase: 'qa_fixing', message: 'Fixing QA issues...' },
  { pattern: /qa\s*review/i,                phase: 'qa_review', message: 'Running QA review...' },
  { pattern: /starting\s+qa/i,              phase: 'qa_review', message: 'Running QA review...' },
  { pattern: /acceptance\s+criteria/i,      phase: 'qa_review', message: 'Checking acceptance criteria...' },
  { pattern: /implementing\s+subtask/i,     phase: 'coding',    message: 'Implementing code changes...' },
  { pattern: /starting\s+coder/i,           phase: 'coding',    message: 'Implementing code changes...' },
  { pattern: /coder\s+agent/i,              phase: 'coding',    message: 'Implementing code changes...' },
  { pattern: /creating\s+implementation\s+plan/i, phase: 'planning', message: 'Creating implementation plan...' },
  { pattern: /planner\s+agent/i,            phase: 'planning',  message: 'Creating implementation plan...' },
  { pattern: /breaking.*into\s+subtasks/i,  phase: 'planning',  message: 'Breaking down into subtasks...' },
];
```

### Regression prevention

```ts
private tryTransition(phase, message, source): PhaseDetection | null {
  if (isTerminalPhase(this._currentPhase)) return null;       // Terminal = locked
  if (wouldPhaseRegress(this._currentPhase, phase)) return null; // No backward transitions
  if (this._currentPhase === phase && this._currentMessage === message) return null; // No-op
  this.transitionTo(phase, message);
  return { phase, message, currentSubtask: this._currentSubtask ?? undefined, source };
}
```

`wouldPhaseRegress` and `PHASE_ORDER_INDEX` come from `apps/desktop/src/shared/constants/phase-protocol.ts`.

### Subtask tracking (during coding phase)

```ts
// Tool call path
if (this._currentPhase === 'coding') {
  const subtaskId = this.extractSubtaskId(event.args); // args.subtask_id ?? args.subtaskId
  if (subtaskId && subtaskId !== this._currentSubtask) {
    this._currentSubtask = subtaskId;
    ...
  }
}

// Text delta path
const subtaskMatch = text.match(/subtask[:\s]+(\d+(?:\/\d+)?|\w+[-_]\w+)/i);
```

### Completed phases tracking

```ts
private transitionTo(phase, message, subtask?): void {
  if (
    this._currentPhase !== 'idle' &&
    this._currentPhase !== phase &&
    !this._completedPhases.includes(this._currentPhase)
  ) {
    this._completedPhases.push(this._currentPhase);
  }
  this._currentPhase = phase;
  this._currentMessage = message;
  if (subtask !== undefined) this._currentSubtask = subtask;
}
```

---

## AgentManager Facade and Modular Components

**File:** `apps/desktop/src/main/agent/agent-manager.ts`

`AgentManager` is a slim facade that delegates to four focused modules. It extends `EventEmitter` to propagate all events from the subsystems to the application layer.

### Modular composition

```ts
export class AgentManager extends EventEmitter {
  private state: AgentState;
  private events: AgentEvents;
  private processManager: AgentProcessManager;
  private queueManager: AgentQueueManager;

  // Per-task context stored for restarts
  private taskExecutionContext: Map<string, {
    projectPath: string;
    specId: string;
    options: TaskExecutionOptions;
    isSpecCreation?: boolean;
    taskDescription?: string;
    specDir?: string;
    metadata?: SpecCreationMetadata;
    baseBranch?: string;
    swapCount: number;     // auto-swap loop protection (max 2)
    projectId?: string;
    generation: number;    // incremented per restart to invalidate stale cleanup callbacks
  }> = new Map();

  constructor() {
    super();
    this.state = new AgentState();
    this.events = new AgentEvents();
    this.processManager = new AgentProcessManager(this.state, this.events, this);
    this.queueManager = new AgentQueueManager(this.state, this.events, this.processManager, this);
    ...
  }
}
```

### Component responsibilities

| Component | File | Responsibility |
|---|---|---|
| `AgentState` | `agent-state.ts` | Process map, spawn ID generation, kill tracking, profile assignment |
| `AgentEvents` | `agent-events.ts` | Phase parsing from stdout (legacy path), overall progress calculation |
| `AgentProcessManager` | `agent-process.ts` | Worker/subprocess spawning, kill, env setup, rate limit detection |
| `AgentQueueManager` | `agent-queue.ts` | Roadmap and ideation queue management |

### Public API (facade methods)

```ts
// Spec creation (spec_orchestrator agent)
async startSpecCreation(taskId, projectPath, taskDescription, specDir?, metadata?, baseBranch?, projectId?): Promise<void>

// Task execution (build_orchestrator agent)
async startTaskExecution(taskId, projectPath, specId, options?, projectId?): Promise<void>

// QA (qa_reviewer agent)
async startQAProcess(taskId, projectPath, specId, projectId?): Promise<void>

// Roadmap / ideation (delegated to queueManager)
startRoadmapGeneration(projectId, projectPath, refresh?, enableCompetitorAnalysis?, refreshCompetitorAnalysis?, config?): void
startIdeationGeneration(projectId, projectPath, config, refresh?): void

// Lifecycle
killTask(taskId): boolean
async killAll(): Promise<void>
isRunning(taskId): boolean
getRunningTasks(): string[]

// Profile assignment / auto-swap
assignProfileToTask(taskId, profileId, profileName, reason): void
getTaskProfileAssignment(taskId): { profileId, profileName, reason } | undefined
restartTask(taskId, newProfileId?): boolean

// Session tracking
updateTaskSession(taskId, sessionId): void
getTaskSessionId(taskId): string | undefined

// Queue routing
getRunningTasksByProfile(): { byProfile: Record<string, string[]>; totalRunning: number }
```

### Auth resolution (provider queue with legacy fallback)

```ts
private async resolveAuthFromProviderQueue(
  requestedModel: string,
  preferredProvider?: string | null,
): Promise<{
  auth: { apiKey?: string; baseURL?: string; oauthTokenFilePath?: string } | null;
  provider: string;
  modelId: string;
  configDir?: string;
}> {
  const settings = readSettingsFile();
  const accounts = settings?.providerAccounts ?? [];
  const priorityOrder = settings?.globalPriorityOrder ?? [];

  if (accounts.length > 0 && priorityOrder.length > 0) {
    // Build ordered queue, optionally reordering for preferredProvider
    const resolved = await resolveAuthFromQueue(requestedModel, orderedQueue);
    if (resolved) return { auth: resolved, provider: resolved.resolvedProvider, modelId: resolved.resolvedModelId };
  }

  // Fallback: legacy Claude profile system
  const auth = await resolveAuth({ provider: 'anthropic', configDir });
  return { auth, provider, modelId: requestedModel, configDir };
}
```

### startTaskExecution() — full worktree + session config assembly

```ts
async startTaskExecution(taskId, projectPath, specId, options = {}, projectId?): Promise<void> {
  // 1. Auth check
  // 2. Resolve specDir from specId
  // 3. resolveTaskModelId() — reads task_metadata.json
  // 4. resolveAuthFromProviderQueue()
  // 5. createOrGetWorktree() — git worktree for isolation (non-fatal fallback)
  // 6. Build SerializableSessionConfig (provider, modelId, auth, toolContext, mcpOptions)
  // 7. Build AgentExecutorConfig
  // 8. storeTaskContext() — for restart
  // 9. registerTaskWithOperationRegistry() — for proactive swap
  // 10. processManager.spawnWorkerProcess()
}
```

The `SerializableSessionConfig` built in step 6 fully specifies the session so the worker never needs to call back into the main thread for configuration.

---

## Auto-Swap Restart Mechanism

**Files:** `apps/desktop/src/main/agent/agent-manager.ts`, `apps/desktop/src/main/agent/agent-process.ts`

### Overview

When a rate limit or auth failure is detected in a worker's output:
1. `AgentProcessManager.handleProcessFailure()` checks for rate limit / auth failure
2. If auto-swap is enabled and another profile is available, it calls `profileManager.setActiveProfile(newProfileId)` and emits `'auto-swap-restart-task'`
3. `AgentManager` listens for `'auto-swap-restart-task'` and calls `restartTask()`

### Rate limit auto-swap (in AgentProcessManager)

```ts
private handleRateLimitWithAutoSwap(taskId, rateLimitDetection, processType): boolean {
  const autoSwitchSettings = profileManager.getAutoSwitchSettings();
  if (!autoSwitchSettings.enabled || !autoSwitchSettings.autoSwitchOnRateLimit) return false;

  const bestProfile = profileManager.getBestAvailableProfile(currentProfileId);
  if (!bestProfile) return false; // Single account — backend handles with intelligent pause

  profileManager.setActiveProfile(bestProfile.id);

  const rateLimitInfo = createSDKRateLimitInfo(source, rateLimitDetection, { taskId });
  rateLimitInfo.wasAutoSwapped = true;
  rateLimitInfo.swappedToProfile = { id: bestProfile.id, name: bestProfile.name };
  rateLimitInfo.swapReason = 'reactive';

  this.emitter.emit('sdk-rate-limit', rateLimitInfo);
  this.emitter.emit('auto-swap-restart-task', taskId, bestProfile.id);
  return true;
}
```

### Auth failure auto-swap (in AgentProcessManager)

```ts
private handleAuthFailureWithAutoSwap(taskId, authFailureDetection): boolean {
  if (!autoSwitchSettings.enabled || !autoSwitchSettings.autoSwitchOnAuthFailure) return false;
  const bestProfile = profileManager.getBestAvailableProfile(currentProfileId);
  if (!bestProfile || !bestProfile.isAuthenticated) return false;

  profileManager.setActiveProfile(bestProfile.id);
  this.emitter.emit('auth-failure', taskId, { ..., wasAutoSwapped: true, swappedToProfile: { id, name } });
  this.emitter.emit('auto-swap-restart-task', taskId, bestProfile.id);
  return true;
}
```

### restartTask() — the restart handler (in AgentManager)

```ts
restartTask(taskId: string, newProfileId?: string): boolean {
  const context = this.taskExecutionContext.get(taskId);
  if (!context) return false;

  // Loop protection: max 2 swaps per task
  if (context.swapCount >= 2) return false;
  context.swapCount++;

  if (newProfileId) {
    const profileManager = getClaudeProfileManager();
    if (profileManager.getActiveProfile()?.id !== newProfileId) {
      profileManager.setActiveProfile(newProfileId);
    }
  }

  this.killTask(taskId);

  setTimeout(async () => {
    // Reset stuck subtasks before restart
    if (context.specId || context.specDir) {
      await resetStuckSubtasks(planPath);
    }

    if (context.isSpecCreation) {
      this.startSpecCreation(taskId, context.projectPath, context.taskDescription!, ...);
    } else {
      this.startTaskExecution(taskId, context.projectPath, context.specId, context.options, context.projectId);
    }
  }, 500); // Grace period for current process cleanup

  return true;
}
```

### stale-exit cleanup via generation counter

`setTimeout` in the `exit` event handler uses a generation counter to detect if the task was restarted between exit and cleanup:

```ts
// On 'exit' event
const generationAtExit = contextAtExit?.generation;
setTimeout(() => {
  const context = this.taskExecutionContext.get(taskId);
  if (!context) return;
  // If context.generation changed, a restart incremented it — skip cleanup
  if (generationAtExit !== undefined && context.generation !== generationAtExit) return;
  if (code === 0) { this.taskExecutionContext.delete(taskId); return; }
  if (context.swapCount >= 2) this.taskExecutionContext.delete(taskId);
}, 1000);
```

`storeTaskContext()` increments `generation` on each call:

```ts
private storeTaskContext(...): void {
  const existing = this.taskExecutionContext.get(taskId);
  const generation = (existing?.generation ?? 0) + 1;
  this.taskExecutionContext.set(taskId, { ..., generation });
}
```

### Event chain summary

```
Worker exits with code != 0
  -> AgentProcessManager.handleProcessFailure()
    -> detectRateLimit() / detectAuthFailure()
      -> handleRateLimitWithAutoSwap() / handleAuthFailureWithAutoSwap()
        -> profileManager.setActiveProfile(bestProfile.id)
        -> emitter.emit('sdk-rate-limit' | 'auth-failure', ..., wasAutoSwapped=true)
        -> emitter.emit('auto-swap-restart-task', taskId, bestProfile.id)
          -> AgentManager listener
            -> AgentManager.restartTask(taskId, newProfileId)
              -> killTask(taskId)
              -> setTimeout(500ms) -> resetStuckSubtasks() -> startTaskExecution()/startSpecCreation()
```

---

## spawnWorkerProcess — Full Integration Path

**File:** `apps/desktop/src/main/agent/agent-process.ts`

This is where `AgentProcessManager` creates the `WorkerBridge` and wires all event forwarding. It is called by `AgentManager.startTaskExecution()`, `startSpecCreation()`, and `startQAProcess()`.

```ts
async spawnWorkerProcess(
  taskId: string,
  executorConfig: AgentExecutorConfig,
  extraEnv: Record<string, string> = {},
  processType: ProcessType = 'task-execution',
  projectId?: string
): Promise<void> {
  this.killProcess(taskId);

  const spawnId = this.state.generateSpawnId();

  // Track IMMEDIATELY before async work (prevents getRunningTasks() race)
  this.state.addProcess(taskId, {
    taskId,
    process: null,
    startedAt: new Date(),
    spawnId,
    worker: null, // Filled after bridge.spawn()
  });

  // Kill-during-setup check #1
  if (this.state.wasSpawnKilled(spawnId)) {
    this.state.deleteProcess(taskId); this.state.clearKilledSpawn(spawnId); return;
  }

  const bridge = new WorkerBridge();

  // Wire all bridge events to main emitter
  bridge.on('log', (tId, log, pId) => this.emitter.emit('log', tId, log, pId));
  bridge.on('error', (tId, error, pId) => this.emitter.emit('error', tId, error, pId));
  bridge.on('execution-progress', (tId, progress, pId) => this.emitter.emit('execution-progress', tId, progress, pId));
  bridge.on('task-event', (tId, event, pId) => this.emitter.emit('task-event', tId, event, pId));

  bridge.on('exit', (tId, code, pType, pId) => {
    this.state.deleteProcess(tId);

    if (this.state.wasSpawnKilled(spawnId)) {
      this.state.clearKilledSpawn(spawnId); return;
    }

    if (code !== 0) {
      this.emitter.emit('execution-progress', tId, {
        phase: 'failed', phaseProgress: 0, overallProgress: 0,
        message: `Worker exited with code ${code}`,
      }, pId);
    }

    this.emitter.emit('exit', tId, code, pType, pId);
  });

  // Spawn the worker
  try {
    bridge.spawn(executorConfig);
  } catch (err) {
    this.state.deleteProcess(taskId);
    this.emitter.emit('error', taskId, err instanceof Error ? err.message : String(err), projectId);
    throw err;
  }

  // Store Worker reference for kill support
  this.state.updateProcess(taskId, { worker: bridge.workerInstance });

  // Kill-during-setup check #2
  const currentSpawnId = this.state.getProcess(taskId)?.spawnId ?? spawnId;
  if (this.state.wasSpawnKilled(currentSpawnId)) {
    await bridge.terminate();
    this.state.deleteProcess(taskId); this.state.clearKilledSpawn(currentSpawnId); return;
  }

  // Emit initial progress event
  this.emitter.emit('execution-progress', taskId, {
    phase: 'planning', phaseProgress: 0, overallProgress: 0,
    message: 'Starting AI agent session...',
  }, projectId);
}
```

### killProcess() — handles both worker and subprocess

```ts
killProcess(taskId: string): boolean {
  const agentProcess = this.state.getProcess(taskId);
  if (!agentProcess) return false;

  this.state.markSpawnAsKilled(agentProcess.spawnId);

  if (!agentProcess.process && !agentProcess.worker) {
    // Still in async setup — the wasSpawnKilled() check after spawn() will clean up
    this.state.deleteProcess(taskId);
    return true;
  }

  if (agentProcess.worker) {
    try { agentProcess.worker.terminate(); } catch { /* already dead */ }
    this.state.deleteProcess(taskId);
    return true;
  }

  if (agentProcess.process) {
    killProcessGracefully(agentProcess.process, { debugPrefix: '[AgentProcess]', debug: ... });
  }

  this.state.deleteProcess(taskId);
  return true;
}
```

---

## Worker Entry Point Routing

**File:** `apps/desktop/src/main/ai/agent/worker.ts`

The `run()` function is the top-level async entry point. It routes to the appropriate orchestrator based on `session.agentType`:

```ts
async function run(): Promise<void> {
  const { session } = config;
  postLog(`Starting agent session: type=${session.agentType}, model=${session.modelId}`);

  try {
    const securityProfile = buildSecurityProfile(session);
    const toolContext = buildToolContext(session, securityProfile);
    const registry = buildToolRegistry();

    // Initialize MCP clients
    mcpClients = await createMcpClientsForAgent(session.agentType, session.mcpOptions);

    if (session.agentType === 'build_orchestrator') {
      await runBuildOrchestrator(session, toolContext, registry);
      return;
    }
    if (session.agentType === 'qa_reviewer') {
      await runQALoop(session, toolContext, registry);
      return;
    }
    if (session.agentType === 'spec_orchestrator') {
      if (session.useAgenticOrchestration) {
        await runAgenticSpecOrchestrator(session, toolContext, registry);
      } else {
        await runSpecOrchestrator(session, toolContext, registry);
      }
      return;
    }

    // Default: single session for all other agent types
    await runDefaultSession(session, toolContext, registry);

  } catch (error: unknown) {
    const message = error instanceof Error ? error.message : String(error);
    postError(`Agent session failed: ${message}`);
  } finally {
    if (mcpClients.length > 0) {
      await closeAllMcpClients(mcpClients);
    }
  }
}
```

### runBuildOrchestrator() — BuildOrchestrator wiring

The orchestrator is created with callbacks that call `runSingleSession()` internally. Phase change events are translated into `execution-progress` and `task-event` messages:

```ts
async function runBuildOrchestrator(session, toolContext, registry): Promise<void> {
  const orchestrator = new BuildOrchestrator({
    specDir: session.specDir,
    projectDir: session.projectDir,
    sourceSpecDir: session.sourceSpecDir,
    abortSignal: abortController.signal,

    generatePrompt: async (agentType, _phase, context) => { ... },

    runSession: async (runConfig) => {
      return runSingleSession(
        runConfig.agentType, runConfig.phase, runConfig.systemPrompt,
        runConfig.specDir, runConfig.projectDir,
        runConfig.sessionNumber, runConfig.subtaskId,
        session, toolContext, registry,
        kickoffMessage,
        true, // skipPhaseLogging
        runConfig.outputSchema,
      );
    },
  });

  orchestrator.on('phase-change', (phase, message) => {
    // Emit task events for XState machine transitions
    if (phase === 'coding')    postTaskEvent('CODING_STARTED', ...);
    if (phase === 'qa_review') postTaskEvent('QA_STARTED', ...);
    if (phase === 'qa_fixing') postTaskEvent('QA_FIXING_STARTED', ...);

    // Emit execution-progress for UI updates
    postMessage({ type: 'execution-progress', taskId: config.taskId, data: { phase, ... } });
  });

  const outcome = await orchestrator.run();

  // Post final result
  postMessage({ type: 'result', taskId: config.taskId, data: { outcome: ..., ... } });
}
```

### runSingleSession() — multi-phase runner core

```ts
async function runSingleSession(
  agentType: AgentType,
  phase: Phase,
  systemPrompt: string,
  specDir: string,
  projectDir: string,
  sessionNumber: number,
  subtaskId: string | undefined,
  baseSession: SerializableSessionConfig,
  toolContext: ToolContext,
  registry: ToolRegistry,
  initialUserMessage?: string,
  skipPhaseLogging = false,
  outputSchema?: import('zod').ZodSchema,
): Promise<SessionResult> {
  const phaseModelId = baseSession.modelId; // Already resolved by main thread

  const model = createProvider({
    config: {
      provider: baseSession.provider as SupportedProvider,
      apiKey: baseSession.apiKey,
      baseURL: baseSession.baseURL,
      oauthTokenFilePath: baseSession.oauthTokenFilePath,
    },
    modelId: phaseModelId,
  });

  const tools = {
    ...registry.getToolsForAgent(agentType, toolContext),
    ...(mergeMcpTools(mcpClients) as Record<string, AITool>),
  };

  const sessionConfig: SessionConfig = {
    agentType, model, systemPrompt,
    initialMessages: initialUserMessage
      ? [{ role: 'user', content: initialUserMessage }]
      : baseSession.initialMessages,
    toolContext,
    maxSteps: baseSession.maxSteps,
    thinkingLevel: phaseThinking,
    abortSignal: abortController.signal,
    specDir, projectDir, phase,
    contextWindowLimit: getModelContextWindow(phaseModelId),
    sessionNumber, subtaskId, outputSchema,
  };

  const runnerOptions = {
    tools,
    onEvent: (event: StreamEvent) => {
      if (logWriter) logWriter.processEvent(event, phase);
      postMessage({ type: 'stream-event', taskId: config.taskId, data: event, projectId: config.projectId });
    },
    onAuthRefresh: baseSession.configDir
      ? () => refreshOAuthTokenReactive(baseSession.configDir!)
      : undefined,
    onModelRefresh: baseSession.configDir
      ? (newToken: string) => createProvider({ config: { provider: ..., apiKey: newToken, baseURL: ... }, modelId: phaseModelId })
      : undefined,
  };

  const sessionResult = await runContinuableSession(sessionConfig, runnerOptions, {
    contextWindowLimit,
    apiKey: baseSession.apiKey,
    baseURL: baseSession.baseURL,
    oauthTokenFilePath: baseSession.oauthTokenFilePath,
  });

  return sessionResult;
}
```

---

## Key Implementation Notes for Reimplementation

1. **Worker serialization boundary is strict.** Nothing with methods, `Set`, `Map`, or closures can cross `workerData`. Reconstruct all non-primitive types inside the worker.

2. **Track processes immediately, before async work.** `state.addProcess()` is called before `bridge.spawn()`. This prevents `getRunningTasks()` races on slow machines. Use a `spawnId` to handle kill-before-spawn races.

3. **Two-phase kill check.** Check `wasSpawnKilled(spawnId)` both before and after `bridge.spawn()`. The window between is where a kill call arrives during async setup.

4. **Worker bridge null-out on exit.** `WorkerBridge.cleanup()` sets `this.worker = null` so that `worker.on('exit', ...)` guards (`if (this.worker)`) correctly prevent double-emit.

5. **`result` message beats `exit` event.** The bridge emits `exit` from the `result` handler and calls `cleanup()` before the worker thread's native `exit` event fires. The native `exit` handler checks `if (this.worker)` (already null) and skips duplicate emission.

6. **Auto-swap fires before exit cleanup.** The `auto-swap-restart-task` listener in `AgentManager` runs synchronously when the event is emitted. The `exit` cleanup uses `setTimeout(1000ms)` specifically to let the restart complete first, then uses `generation` to detect whether the cleanup is stale.

7. **ProgressTracker is per-spawn, not per-task.** `WorkerBridge.spawn()` creates a fresh `ProgressTracker`. On restart (new bridge), the tracker starts from `idle` again.

8. **Phase regression is enforced.** Once a task enters `qa_review`, it cannot fall back to `coding` via text patterns. Only `forcePhase()` bypasses this, and it is used only for authoritative structured protocol events.
