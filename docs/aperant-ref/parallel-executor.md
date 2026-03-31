# Aperant Parallel Executor Reference

Source files:
- `apps/desktop/src/main/ai/orchestration/parallel-executor.ts`
- `apps/desktop/src/main/ai/orchestration/subagent-executor.ts`
- `apps/desktop/src/main/ai/tools/builtin/spawn-subagent.ts`

---

## 1. Constants

**File:** `parallel-executor.ts`

```ts
const DEFAULT_MAX_CONCURRENCY = 3;
const RATE_LIMIT_BASE_DELAY_MS = 30_000;   // 30 seconds
const RATE_LIMIT_MAX_DELAY_MS  = 300_000;  // 5 minutes (hard cap)
const STAGGER_DELAY_MS         = 1_000;    // 1 second between launches in a batch
```

**File:** `subagent-executor.ts`

```ts
const SUBAGENT_MAX_STEPS = 100;
```

---

## 2. Type Definitions

**File:** `parallel-executor.ts`

```ts
/** Configuration for parallel execution */
export interface ParallelExecutorConfig {
  maxConcurrency?: number;
  abortSignal?: AbortSignal;
  onSubtaskStart?: (subtask: SubtaskInfo) => void;
  onSubtaskComplete?: (subtask: SubtaskInfo, result: SessionResult) => void;
  onSubtaskFailed?: (subtask: SubtaskInfo, error: Error) => void;
  onRateLimited?: (delayMs: number) => void;
}

/** Function that runs a single subtask session */
export type SubtaskSessionRunner = (subtask: SubtaskInfo) => Promise<SessionResult>;

/** Result of a single parallel execution */
export interface ParallelSubtaskResult {
  subtaskId: string;
  success: boolean;
  result?: SessionResult;
  error?: string;
  rateLimited: boolean;
}

/** Result of the full parallel execution batch */
export interface ParallelExecutionResult {
  results: ParallelSubtaskResult[];
  successCount: number;
  failureCount: number;
  rateLimitedCount: number;
  cancelled: boolean;
}
```

---

## 3. Concurrent Session Runner — `executeParallel()`

**File:** `parallel-executor.ts`

The top-level entry point. Splits the subtask list into fixed-size batches, executes each batch with `Promise.allSettled()`, and tracks rate-limit backoff state between batches.

```ts
export async function executeParallel(
  subtasks: SubtaskInfo[],
  runSession: SubtaskSessionRunner,
  config: ParallelExecutorConfig = {},
): Promise<ParallelExecutionResult> {
  const maxConcurrency = config.maxConcurrency ?? DEFAULT_MAX_CONCURRENCY;

  if (subtasks.length === 0) {
    return { results: [], successCount: 0, failureCount: 0, rateLimitedCount: 0, cancelled: false };
  }

  // Split into batches based on concurrency limit
  const batches = createBatches(subtasks, maxConcurrency);
  const allResults: ParallelSubtaskResult[] = [];
  let rateLimitBackoff = 0;

  for (const batch of batches) {
    if (config.abortSignal?.aborted) break;  // Mark remaining as cancelled

    // Wait for rate limit back-off if needed
    if (rateLimitBackoff > 0) {
      config.onRateLimited?.(rateLimitBackoff);
      await delay(rateLimitBackoff, config.abortSignal);
      rateLimitBackoff = 0;
    }

    // Execute batch concurrently with staggered starts
    const batchPromises = batch.map((subtask, index) =>
      executeSingleSubtask(subtask, runSession, config, index * STAGGER_DELAY_MS),
    );

    const settled = await Promise.allSettled(batchPromises);

    for (const outcome of settled) {
      if (outcome.status === 'fulfilled') {
        allResults.push(outcome.value);

        // Detect rate limiting for back-off
        if (outcome.value.rateLimited) {
          rateLimitBackoff = Math.min(
            RATE_LIMIT_BASE_DELAY_MS * (2 ** allResults.filter((r) => r.rateLimited).length),
            RATE_LIMIT_MAX_DELAY_MS,
          );
        }
      } else {
        // Promise.allSettled rejection — unexpected throw
        allResults.push({
          subtaskId: 'unknown',
          success: false,
          error: outcome.reason instanceof Error ? outcome.reason.message : String(outcome.reason),
          rateLimited: false,
        });
      }
    }
  }

  const successCount = allResults.filter((r) => r.success).length;
  const rateLimitedCount = allResults.filter((r) => r.rateLimited).length;
  return {
    results: allResults,
    successCount,
    failureCount: allResults.length - successCount,
    rateLimitedCount,
    cancelled: config.abortSignal?.aborted ?? false,
  };
}
```

Key behaviors:
- `DEFAULT_MAX_CONCURRENCY = 3` is used when `config.maxConcurrency` is not provided.
- Batches are non-overlapping sequential windows: batch 0 fully completes before batch 1 starts.
- `Promise.allSettled()` is used instead of `Promise.all()` so that one rejection does not abort the batch.
- Rejected promises (from `executeSingleSubtask` itself throwing unexpectedly) are caught at the outer level and produce a `subtaskId: 'unknown'` result entry.

---

## 4. Stagger Delay Between Launches

**File:** `parallel-executor.ts`

Within each batch, subtasks are staggered by `index * STAGGER_DELAY_MS` (1 second each) to avoid a thundering-herd effect against the API:

```ts
const batchPromises = batch.map((subtask, index) =>
  executeSingleSubtask(subtask, runSession, config, index * STAGGER_DELAY_MS),
);
```

The stagger is applied inside `executeSingleSubtask`:

```ts
async function executeSingleSubtask(
  subtask: SubtaskInfo,
  runSession: SubtaskSessionRunner,
  config: ParallelExecutorConfig,
  staggerDelayMs: number,
): Promise<ParallelSubtaskResult> {
  if (staggerDelayMs > 0) {
    await delay(staggerDelayMs, config.abortSignal);
  }
  // ...
}
```

For a batch of 3, subtask 0 starts immediately, subtask 1 starts after 1s, subtask 2 starts after 2s.

---

## 5. Per-Call Failure Isolation Pattern

**File:** `parallel-executor.ts`

```ts
async function executeSingleSubtask(
  subtask: SubtaskInfo,
  runSession: SubtaskSessionRunner,
  config: ParallelExecutorConfig,
  staggerDelayMs: number,
): Promise<ParallelSubtaskResult> {
  if (staggerDelayMs > 0) {
    await delay(staggerDelayMs, config.abortSignal);
  }

  if (config.abortSignal?.aborted) {
    return { subtaskId: subtask.id, success: false, error: 'Cancelled', rateLimited: false };
  }

  config.onSubtaskStart?.(subtask);

  try {
    const result = await runSession(subtask);

    const rateLimited = result.outcome === 'rate_limited';
    const success = result.outcome === 'completed';

    if (success || rateLimited) {
      config.onSubtaskComplete?.(subtask, result);
    } else if (result.outcome === 'error' || result.outcome === 'auth_failure') {
      config.onSubtaskFailed?.(
        subtask,
        new Error(result.error?.message ?? `Session ended with outcome: ${result.outcome}`),
      );
    }

    return { subtaskId: subtask.id, success, result, rateLimited };
  } catch (error: unknown) {
    const message = error instanceof Error ? error.message : String(error);
    config.onSubtaskFailed?.(subtask, error instanceof Error ? error : new Error(message));

    return {
      subtaskId: subtask.id,
      success: false,
      error: message,
      rateLimited: isRateLimitError(message),
    };
  }
}
```

The isolation contract: this function never throws. All errors are caught and converted to a `ParallelSubtaskResult` with `success: false`. The catch block also checks whether the thrown error looks like a rate limit, allowing even unhandled API errors to feed into the backoff logic.

---

## 6. Rate Limit Detection

**File:** `parallel-executor.ts`

```ts
function isRateLimitError(message: string): boolean {
  const lower = message.toLowerCase();
  return lower.includes('429') || lower.includes('rate limit') || lower.includes('too many requests');
}
```

This is called in two places:
1. In `executeSingleSubtask` catch block — when `runSession()` throws.
2. In `executeParallel` — when `result.outcome === 'rate_limited'` (a first-class outcome from the session layer).

---

## 7. Rate Limit Backoff

**File:** `parallel-executor.ts`

The backoff is computed after each batch completes, applied before the next batch starts. It uses exponential backoff based on the total number of rate-limited results accumulated so far across all batches:

```ts
// After batch completes, for each fulfilled result:
if (outcome.value.rateLimited) {
  rateLimitBackoff = Math.min(
    RATE_LIMIT_BASE_DELAY_MS * (2 ** allResults.filter((r) => r.rateLimited).length),
    RATE_LIMIT_MAX_DELAY_MS,
  );
}
```

Applied before next batch:

```ts
if (rateLimitBackoff > 0) {
  config.onRateLimited?.(rateLimitBackoff);
  await delay(rateLimitBackoff, config.abortSignal);
  rateLimitBackoff = 0;
}
```

The delay helper supports abort signal cancellation — the timer is cleared and the promise resolves immediately if abort fires:

```ts
function delay(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise<void>((resolve) => {
    if (signal?.aborted) { resolve(); return; }
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener('abort', () => { clearTimeout(timer); resolve(); }, { once: true });
  });
}
```

Backoff schedule (base 30s, doubling):

| Rate-limited count | Delay          |
|--------------------|----------------|
| 1                  | 60s            |
| 2                  | 120s           |
| 3                  | 240s           |
| 4+                 | 300s (capped)  |

---

## 8. Batch Splitting Helper

**File:** `parallel-executor.ts`

```ts
function createBatches<T>(items: T[], batchSize: number): T[][] {
  const batches: T[][] = [];
  for (let i = 0; i < items.length; i += batchSize) {
    batches.push(items.slice(i, i + batchSize));
  }
  return batches;
}
```

For 7 subtasks at `maxConcurrency = 3`: batches are `[0,1,2]`, `[3,4,5]`, `[6]`.

---

## 9. SubagentExecutorImpl — Spawning with `generateText()`

**File:** `subagent-executor.ts`

```ts
export class SubagentExecutorImpl implements SubagentExecutor {
  private readonly config: SubagentExecutorConfig;

  constructor(config: SubagentExecutorConfig) {
    this.config = config;
  }

  async spawn(params: SubagentSpawnParams): Promise<SubagentResult> {
    const startTime = Date.now();
    const agentType = resolveAgentType(params.agentType);
    const promptName = resolvePromptName(params.agentType);

    this.config.onSubagentEvent?.(params.agentType, 'spawning');

    try {
      // 1. Load system prompt for the subagent
      const systemPrompt = await this.config.loadPrompt(promptName);

      // 2. Build tool set — exclude SpawnSubagent to prevent recursion
      const subagentToolContext: ToolContext = {
        ...this.config.baseToolContext,
        abortSignal: this.config.abortSignal,
      };

      const tools: Record<string, AITool> = {};
      const agentConfig = getAgentConfig(agentType);
      for (const toolName of agentConfig.tools) {
        if (toolName === 'SpawnSubagent') continue; // No recursion
        const definedTool = this.config.registry.getTool(toolName);
        if (definedTool) {
          tools[toolName] = definedTool.bind(subagentToolContext);
        }
      }

      // 3. Build the user message with task + context
      let userMessage = `Your task: ${params.task}`;
      if (params.context) {
        userMessage += `\n\nContext:\n${params.context}`;
      }

      // 4. Determine if we should use structured output
      const outputSchema = params.expectStructuredOutput
        ? STRUCTURED_OUTPUT_AGENTS[params.agentType]
        : undefined;

      // 5. Run generateText() (non-streaming)
      const generateOptions: any = {
        model: this.config.model,
        system: systemPrompt,
        messages: [{ role: 'user' as const, content: userMessage }],
        tools,
        stopWhen: stepCountIs(SUBAGENT_MAX_STEPS),
        abortSignal: this.config.abortSignal,
        ...(outputSchema
          ? { output: Output.object({ schema: outputSchema }) }
          : {}),
      };

      const result = await generateText(generateOptions);

      this.config.onSubagentEvent?.(params.agentType, 'completed');

      // 6. Extract results
      const resultAny = result as any;
      const structuredOutput =
        outputSchema && resultAny.output != null
          ? (resultAny.output as Record<string, unknown>)
          : undefined;

      return {
        text: result.text || undefined,
        structuredOutput,
        stepsExecuted: result.steps?.length ?? 1,
        durationMs: Date.now() - startTime,
      };
    } catch (error) {
      this.config.onSubagentEvent?.(params.agentType, 'failed');
      const message = error instanceof Error ? error.message : String(error);
      return {
        error: message,
        stepsExecuted: 0,
        durationMs: Date.now() - startTime,
      };
    }
  }
}
```

Key design decisions (from file docstring):
- Uses `generateText()` not `streamText()` because subagent output goes back to the orchestrator's context, not the UI stream.
- Inherits `allowedWritePaths` from parent context for write containment.
- Step budget is capped at `SUBAGENT_MAX_STEPS = 100` via `stopWhen: stepCountIs(100)`.

---

## 10. SubagentExecutorConfig

**File:** `subagent-executor.ts`

```ts
export interface SubagentExecutorConfig {
  model: LanguageModel;
  registry: ToolRegistry;
  baseToolContext: ToolContext;
  loadPrompt: (promptName: string) => Promise<string>;
  abortSignal?: AbortSignal;
  onSubagentEvent?: (agentType: string, event: string) => void;
}
```

The `loadPrompt` function loads and assembles a system prompt by name. This is injected at construction time, allowing different prompt loading strategies.

---

## 11. Tool Registry Filtering for Subagents

**File:** `subagent-executor.ts`

Subagents receive a filtered subset of the tool registry. Two levels of filtering are applied:

1. **Agent-type allowlist** — `getAgentConfig(agentType).tools` returns only the tool names registered for that agent type.
2. **Recursion guard** — `SpawnSubagent` is unconditionally excluded regardless of whether it appears in the agent config.

```ts
const agentConfig = getAgentConfig(agentType);
for (const toolName of agentConfig.tools) {
  if (toolName === 'SpawnSubagent') continue; // No recursion
  const definedTool = this.config.registry.getTool(toolName);
  if (definedTool) {
    tools[toolName] = definedTool.bind(subagentToolContext);
  }
}
```

Tools are bound to the subagent's `ToolContext` (which has the parent's `abortSignal` merged in) rather than the parent's context.

---

## 12. Subagent Type Map

**File:** `subagent-executor.ts`

```ts
function resolveAgentType(subagentType: string): AgentType {
  const directMap: Record<string, AgentType> = {
    complexity_assessor: 'spec_gatherer',  // reuses spec_gatherer tools
    spec_discovery:      'spec_discovery',
    spec_gatherer:       'spec_gatherer',
    spec_researcher:     'spec_researcher',
    spec_writer:         'spec_writer',
    spec_critic:         'spec_critic',
    spec_validation:     'spec_validation',
    planner:             'planner',
    coder:               'coder',
    qa_reviewer:         'qa_reviewer',
    qa_fixer:            'qa_fixer',
  };
  return directMap[subagentType] ?? 'spec_gatherer';
}

function resolvePromptName(subagentType: string): string {
  const promptMap: Record<string, string> = {
    complexity_assessor: 'complexity_assessor',
    spec_discovery:      'spec_gatherer',       // shares gatherer prompt
    spec_gatherer:       'spec_gatherer',
    spec_researcher:     'spec_researcher',
    spec_writer:         'spec_writer',
    spec_critic:         'spec_critic',
    spec_validation:     'spec_writer',          // shares writer prompt
    planner:             'planner',
    coder:               'coder',
    qa_reviewer:         'qa_reviewer',
    qa_fixer:            'qa_fixer',
  };
  return promptMap[subagentType] ?? 'spec_writer';
}
```

Note: `resolveAgentType` governs which tools the subagent gets; `resolvePromptName` governs which system prompt it uses. They can diverge (e.g. `complexity_assessor` gets `spec_gatherer` tools but the `complexity_assessor` prompt).

---

## 13. Structured Output Support

**File:** `subagent-executor.ts`

```ts
const STRUCTURED_OUTPUT_AGENTS: Partial<Record<string, ZodSchema>> = {
  complexity_assessor: ComplexityAssessmentOutputSchema,
};
```

When `params.expectStructuredOutput === true` and the agent type has a registered schema, `Output.object({ schema })` is passed to `generateText()` and `result.output` is returned as `structuredOutput`. All other agent types return plain text via `result.text`.

---

## 14. SpawnSubagent Tool Definition

**File:** `spawn-subagent.ts`

The tool that orchestrator agents call to spawn subagents. It is a thin wrapper that delegates to `SubagentExecutor` injected via `ToolContext`.

```ts
export const spawnSubagentTool = Tool.define({
  metadata: {
    name: 'SpawnSubagent',
    permission: ToolPermission.Auto,
    executionOptions: {
      ...DEFAULT_EXECUTION_OPTIONS,
      timeoutMs: 600_000, // 10 minutes
    },
  },
  inputSchema: SpawnSubagentInputSchema,
  execute: async (input: SpawnSubagentInput, context: ToolContext): Promise<string> => {
    const executor = (context as ToolContext & { subagentExecutor?: SubagentExecutor })
      .subagentExecutor;

    if (!executor) {
      return 'Error: SpawnSubagent is not available in this session. ...';
    }

    try {
      const result = await executor.spawn({
        agentType: input.agent_type,
        task: input.task,
        context: input.context ?? undefined,
        expectStructuredOutput: input.expect_structured_output,
      });

      if (result.error) {
        return `Subagent (${input.agent_type}) failed: ${result.error}`;
      }
      if (result.structuredOutput) {
        return `Subagent (${input.agent_type}) completed successfully.\n\nStructured output:\n\`\`\`json\n${JSON.stringify(result.structuredOutput, null, 2)}\n\`\`\``;
      }
      return `Subagent (${input.agent_type}) completed successfully.\n\nOutput:\n${result.text ?? '(no text output)'}`;
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      return `Subagent (${input.agent_type}) execution error: ${message}`;
    }
  },
});
```

### Input Schema

```ts
const SpawnSubagentInputSchema = z.object({
  agent_type: z.enum([
    'complexity_assessor', 'spec_discovery', 'spec_gatherer', 'spec_researcher',
    'spec_writer', 'spec_critic', 'spec_validation',
    'planner', 'coder', 'qa_reviewer', 'qa_fixer',
  ]),
  task: z.string(),
  context: z.string().nullable(),
  expect_structured_output: z.boolean(),
});
```

### SubagentExecutor Interface

```ts
export interface SubagentExecutor {
  spawn(params: SubagentSpawnParams): Promise<SubagentResult>;
}

export interface SubagentSpawnParams {
  agentType: string;
  task: string;
  context?: string;
  expectStructuredOutput: boolean;
}

export interface SubagentResult {
  text?: string;
  structuredOutput?: Record<string, unknown>;
  error?: string;
  stepsExecuted: number;
  durationMs: number;
}
```

The executor is injected via the `ToolContext` extension pattern (`context as ToolContext & { subagentExecutor?: SubagentExecutor }`). Graceful degradation: if no executor is present, the tool returns an error string rather than throwing.

---

## 15. Architecture Summary

```
executeParallel()
  |
  +-- createBatches(subtasks, maxConcurrency=3)
  |     -> [[t0,t1,t2], [t3,t4,t5], ...]
  |
  +-- for each batch:
  |     |
  |     +-- rate limit backoff wait (if any)
  |     |
  |     +-- Promise.allSettled([
  |           executeSingleSubtask(t0, runner, config, 0ms),
  |           executeSingleSubtask(t1, runner, config, 1000ms),
  |           executeSingleSubtask(t2, runner, config, 2000ms),
  |         ])
  |           |
  |           +-- never throws (all errors caught -> ParallelSubtaskResult)
  |           +-- calls runSession(subtask) -> SessionResult
  |               outcome: 'completed' | 'rate_limited' | 'error' | 'auth_failure'
  |
  +-- aggregate results -> ParallelExecutionResult


runSession(subtask)
  |
  +-- spawns SubagentExecutorImpl.spawn()
        |
        +-- loadPrompt(promptName)
        +-- filter registry tools (exclude SpawnSubagent)
        +-- generateText({
              model,
              system: systemPrompt,
              messages: [{ role: 'user', content: task + context }],
              tools: filteredTools,
              stopWhen: stepCountIs(100),
              abortSignal,
              output?: Output.object({ schema })  // structured output agents only
            })
        +-- return SubagentResult { text, structuredOutput, stepsExecuted, durationMs }
```

---

## 16. Golem Adaptation Notes

The Aperant implementation is TypeScript using the Vercel AI SDK (`generateText`, `streamText`). Golem uses Python with the Claude Agent SDK. Equivalent mappings:

| Aperant concept | Golem equivalent |
|-----------------|------------------|
| `DEFAULT_MAX_CONCURRENCY = 3` | `config.max_parallel_sessions` |
| `Promise.allSettled()` | `asyncio.gather(*coros, return_exceptions=True)` |
| `STAGGER_DELAY_MS = 1000` | `asyncio.sleep(index * 1.0)` before each coroutine |
| `stepCountIs(SUBAGENT_MAX_STEPS)` | `max_turns=100` in SDK `query()` call |
| `generateText()` (non-streaming) | SDK `query()` consuming all messages then returning |
| `SpawnSubagent` tool exclusion (no recursion) | Strip `mcp__golem__spawn_*` from subagent MCP servers |
| `output: Output.object({ schema })` | Structured JSON parsing from `ResultMessage.result` |
| `result.outcome === 'rate_limited'` | Catch `CLIConnectionError` with 429 / rate limit text |
| `RATE_LIMIT_BASE_DELAY_MS = 30_000` | `asyncio.sleep(30 * (2 ** rate_limited_count))` |
| `RATE_LIMIT_MAX_DELAY_MS = 300_000` | `min(..., 300)` seconds |
| `AbortSignal` | `asyncio.Event` or `anyio.CancelScope` |
| `onSubtaskStart/Complete/Failed` | `EventBus.emit(SubtaskStarted / SubtaskCompleted / SubtaskFailed)` |
