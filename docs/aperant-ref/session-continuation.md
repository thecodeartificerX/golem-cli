# Aperant Session Continuation — Implementation Reference

Source files:
- `apps/desktop/src/main/ai/session/continuation.ts`
- `apps/desktop/src/main/ai/session/runner.ts`
- `apps/desktop/src/main/ai/session/stream-handler.ts`
- `apps/desktop/src/main/ai/session/types.ts`

---

## Overview

The continuation system transparently wraps a single-shot agent session runner
(`runAgentSession`) so that callers never need to know about context window
exhaustion. When a session hits 90% of its context window, the runner aborts
with a special `context_window` outcome. The continuation wrapper then:

1. Serializes the message history to text
2. Summarizes it with a cheap model (Haiku)
3. Injects the summary as the first user message of a fresh session
4. Re-runs the session from that starting point
5. Merges all metrics and returns a single unified result

Orchestration callers (`BuildOrchestrator`, `QALoop`) call `runSingleSession()`
which internally calls `runContinuableSession()`. They never interact with the
continuation loop directly.

---

## Constants

### runner.ts

```typescript
/** Context window usage threshold (85%) for reactive compaction warning */
const CONTEXT_WINDOW_THRESHOLD = 0.85;

/** Context window usage threshold (90%) for hard abort — triggers continuation */
const CONTEXT_WINDOW_ABORT_THRESHOLD = 0.90;

/** Unique reason string for context-window aborts */
const CONTEXT_WINDOW_ABORT_REASON = '__context_window_exhausted__';
```

### continuation.ts

```typescript
/** Maximum number of continuations before hard-stopping */
const DEFAULT_MAX_CONTINUATIONS = 5;

/** Maximum characters of conversation to send for summarization */
const MAX_SUMMARY_INPUT_CHARS = 30_000;

/** Target summary length in words */
const SUMMARY_TARGET_WORDS = 800;

/** Fallback: raw truncation length if summarization fails */
const RAW_TRUNCATION_CHARS = 3000;
```

---

## Types

### SessionOutcome (types.ts)

The `context_window` outcome is the signal from runner to continuation wrapper:

```typescript
export type SessionOutcome =
  | 'completed'        // Session finished normally
  | 'error'            // Unrecoverable error
  | 'rate_limited'     // 429 from provider
  | 'auth_failure'     // 401 from provider
  | 'cancelled'        // Aborted via AbortSignal
  | 'max_steps'        // Reached maxSteps limit
  | 'context_window';  // 90%+ context used, eligible for continuation
```

### SessionConfig (types.ts)

The config field that enables the context window guard:

```typescript
export interface SessionConfig {
  agentType: AgentType;
  model: LanguageModel;
  systemPrompt: string;
  initialMessages: SessionMessage[];  // Fresh session gets [continuationMessage] here
  toolContext: ToolContext;
  maxSteps: number;
  thinkingLevel?: ThinkingLevel;
  abortSignal?: AbortSignal;
  mcpClients?: McpClientResult[];
  specDir: string;
  projectDir: string;
  phase?: Phase;
  modelShorthand?: ModelShorthand;
  sessionNumber?: number;
  subtaskId?: string;
  contextWindowLimit?: number;  // Tokens — enables context window guard when set
  outputSchema?: ZodSchema;
}
```

### SessionMessage (types.ts)

```typescript
export interface SessionMessage {
  role: 'user' | 'assistant';
  content: string;
}
```

### ContinuationConfig (continuation.ts)

```typescript
export interface ContinuationConfig {
  maxContinuations?: number;        // Default 5
  contextWindowLimit: number;       // From model metadata, in tokens
  apiKey?: string;                  // For the summarizer model
  baseURL?: string;
  oauthTokenFilePath?: string;
}
```

### ContinuationResult (continuation.ts)

```typescript
export interface ContinuationResult extends SessionResult {
  continuationCount: number;    // 0 = no continuation needed
  cumulativeUsage: TokenUsage;  // Merged usage across all segments
}
```

### TokenUsage (types.ts)

```typescript
export interface TokenUsage {
  promptTokens: number;
  completionTokens: number;
  totalTokens: number;
  thinkingTokens?: number;        // Provider-specific reasoning tokens
  cacheReadTokens?: number;       // Anthropic prompt caching
  cacheCreationTokens?: number;   // Anthropic prompt caching
}
```

---

## Context Window Detection — runner.ts

Detection happens in two places inside `executeStream()`, both inside the
`prepareStep` callback which runs between every agentic step.

### Token tracking

After every `finish-step` stream event, `lastPromptTokens` is updated:

```typescript
// Inside emitEvent — called for every stream event
if (event.type === 'step-finish') {
  lastPromptTokens = event.usage.promptTokens;
}
```

`lastPromptTokens` reflects the cumulative prompt size after each step
because the AI SDK reports total prompt tokens (including history) not just
the delta.

### 85% threshold — warning injection (prepareStep)

```typescript
if (
  contextWindowLimit > 0 &&
  lastPromptTokens > 0 &&
  !contextWindowWarningInjected &&
  lastPromptTokens > contextWindowLimit * CONTEXT_WINDOW_THRESHOLD   // 0.85
) {
  contextWindowWarningInjected = true;
  const usagePct = Math.round((lastPromptTokens / contextWindowLimit) * 100);
  systemParts.push(
    `WARNING: You are approaching the context window limit (${usagePct}% used, ` +
    `${lastPromptTokens.toLocaleString()} of ${contextWindowLimit.toLocaleString()} tokens). ` +
    `Complete your current task and commit progress immediately. Do not start new subtasks.`,
  );
}
```

This injects a system message between steps nudging the agent to wrap up.
It fires once and never again (`contextWindowWarningInjected` flag).

### 90% threshold — hard abort (prepareStep, runs first)

```typescript
// Hard abort check runs BEFORE the warning check
if (
  contextWindowLimit > 0 &&
  lastPromptTokens > 0 &&
  lastPromptTokens > contextWindowLimit * CONTEXT_WINDOW_ABORT_THRESHOLD  // 0.90
) {
  contextWindowAbortController.abort(CONTEXT_WINDOW_ABORT_REASON);
  return {};
}
```

Aborts via a dedicated `AbortController`. The reason string is the
sentinel `'__context_window_exhausted__'` used downstream to distinguish
this from a user cancel.

### Abort signal wiring

Three signals are merged with `AbortSignal.any()`:

```typescript
const contextWindowAbortController = new AbortController();
const streamInactivityController = new AbortController();

const signals: AbortSignal[] = [
  contextWindowAbortController.signal,
  streamInactivityController.signal,
];
if (config.abortSignal) signals.push(config.abortSignal);
const mergedAbortSignal = AbortSignal.any(signals);
```

`mergedAbortSignal` is passed to `streamText()`. When the context window
abort fires, `streamText` throws, which is caught in the stream consumption
loop.

### Outcome routing in the catch block

```typescript
} catch (error: unknown) {
  const summary = streamHandler.getSummary();

  // Context window abort — eligible for continuation
  if (
    contextWindowAbortController.signal.aborted &&
    contextWindowAbortController.signal.reason === CONTEXT_WINDOW_ABORT_REASON
  ) {
    return {
      outcome: 'context_window',
      stepsExecuted: summary.stepsExecuted,
      usage: summary.usage,
      messages,
      toolCallCount: summary.toolCallCount,
    };
  }

  // User cancel
  if (config.abortSignal?.aborted) {
    return {
      outcome: 'cancelled',
      ...
    };
  }

  // Re-throw for classification by outer try/catch
  throw error;
}
```

The key is checking `signal.reason === CONTEXT_WINDOW_ABORT_REASON` — this
prevents false positives if the user's AbortSignal also fires.

---

## Continuation Wrapper — runContinuableSession (continuation.ts)

Full function signature:

```typescript
export async function runContinuableSession(
  config: SessionConfig,
  options: RunnerOptions = {},
  continuationConfig: ContinuationConfig,
): Promise<ContinuationResult>
```

### Loop structure

```typescript
const maxContinuations = continuationConfig.maxContinuations ?? DEFAULT_MAX_CONTINUATIONS;

let currentConfig = config;
let continuationCount = 0;
let totalStepsExecuted = 0;
let totalToolCallCount = 0;
let totalDurationMs = 0;
const cumulativeUsage: TokenUsage = {
  promptTokens: 0,
  completionTokens: 0,
  totalTokens: 0,
};

for (let i = 0; i <= maxContinuations; i++) {
  const result = await runAgentSession(currentConfig, options);

  // Accumulate metrics from this segment
  totalStepsExecuted += result.stepsExecuted;
  totalToolCallCount += result.toolCallCount;
  totalDurationMs += result.durationMs;
  addUsage(cumulativeUsage, result.usage);

  // If not context_window, return merged result
  if (result.outcome !== 'context_window') {
    return {
      ...result,
      stepsExecuted: totalStepsExecuted,
      toolCallCount: totalToolCallCount,
      durationMs: totalDurationMs,
      usage: cumulativeUsage,
      continuationCount,
      cumulativeUsage,
    };
  }

  // Hard cap on continuations
  if (i >= maxContinuations) {
    return {
      ...result,
      outcome: 'completed',  // Treat as completed — agent did useful work
      stepsExecuted: totalStepsExecuted,
      ...
    };
  }

  // Check if cancelled before compacting
  if (config.abortSignal?.aborted) {
    return { ...result, outcome: 'cancelled', ... };
  }

  // Compact and build fresh config
  continuationCount++;
  const summary = await compactSessionMessages(
    result.messages,
    continuationConfig,
    config.abortSignal,
  );

  const continuationMessage: SessionMessage = {
    role: 'user',
    content: buildContinuationPrompt(summary, continuationCount),
  };

  currentConfig = {
    ...config,
    initialMessages: [continuationMessage],
  };
}
```

Key points:
- Loop bound is `i <= maxContinuations` (inclusive), but there is an early
  return when `i >= maxContinuations` before compaction. So the max actual
  sessions run is `maxContinuations + 1` (original + N continuations).
- `currentConfig` is rebuilt each iteration. Only `initialMessages` changes;
  all other config (model, system prompt, tools, limits) is preserved.
- When the limit is hit, outcome is forced to `'completed'` rather than
  `'context_window'` so callers treat it as a normal completion.

---

## Message Compaction — compactSessionMessages (continuation.ts)

```typescript
async function compactSessionMessages(
  messages: SessionMessage[],
  continuationConfig: ContinuationConfig,
  abortSignal?: AbortSignal,
): Promise<string>
```

### Steps

1. Serialize all messages to text with `serializeMessages()`
2. Truncate input to `MAX_SUMMARY_INPUT_CHARS` (30,000) with a marker
3. Check abort signal — return raw truncation immediately if aborted
4. Create a Haiku model via dynamic import of the provider factory
5. Call `generateText()` with the summarizer system prompt and focused prompt
6. Return the trimmed text, or fall back to `rawTruncation()` on any error

### Serialization

```typescript
function serializeMessages(messages: SessionMessage[]): string {
  return messages
    .map((msg) => `[${msg.role.toUpperCase()}]\n${msg.content}`)
    .join('\n\n---\n\n');
}
```

Produces blocks like:
```
[USER]
Do X

---

[ASSISTANT]
I will do X by ...
```

### Summarizer model creation

```typescript
const { createProviderFromModelId } = await import('../providers/factory');
const summarizerModel = createProviderFromModelId('claude-haiku-4-5-20251001', {
  apiKey: continuationConfig.apiKey,
  baseURL: continuationConfig.baseURL,
  oauthTokenFilePath: continuationConfig.oauthTokenFilePath,
});
```

Dynamic import avoids circular dependencies at module load time.

### Summarizer system prompt

```
You are a concise technical summarizer. Given a conversation between an AI agent
and its tools, extract the key information needed to continue the work. Focus on:
what has been accomplished, what files were modified, what remains to be done,
and any critical decisions or findings. Use bullet points. Be thorough but concise.
```

### Summarizer user prompt structure

```typescript
const prompt =
  `Summarize this AI agent conversation in approximately ${SUMMARY_TARGET_WORDS} words.\n\n` +
  `Focus on:\n` +
  `- What tasks/subtasks have been completed\n` +
  `- What files were created, modified, or read\n` +
  `- Key decisions made and their rationale\n` +
  `- What work remains to be done\n` +
  `- Any errors encountered and how they were resolved\n\n` +
  `## Conversation:\n${serialized}\n\n## Summary:`;
```

### Raw truncation fallback

If `generateText()` fails or returns empty:

```typescript
function rawTruncation(messages: SessionMessage[]): string {
  const lastMessages = messages.slice(-5);
  const text = serializeMessages(lastMessages);
  if (text.length <= RAW_TRUNCATION_CHARS) {
    return text;
  }
  return text.slice(-RAW_TRUNCATION_CHARS) + '\n\n[... truncated ...]';
}
```

Takes last 5 messages, then truncates to `RAW_TRUNCATION_CHARS` (3000) from
the end.

---

## Continuation Prompt Injection — buildContinuationPrompt (continuation.ts)

```typescript
function buildContinuationPrompt(summary: string, continuationNumber: number): string {
  return (
    `## Session Continuation (${continuationNumber})\n\n` +
    `You are continuing a previous session that ran out of context window space. ` +
    `Here is a summary of your prior work:\n\n` +
    `${summary}\n\n` +
    `Continue where you left off. Do NOT repeat completed work. ` +
    `Focus on what remains to be done.`
  );
}
```

This becomes the sole entry in `initialMessages` for the fresh session. The
original system prompt is unchanged; only the conversation history is replaced
by this single user message.

---

## Usage Accumulation — addUsage (continuation.ts)

```typescript
function addUsage(cumulative: TokenUsage, addition: TokenUsage): void {
  cumulative.promptTokens += addition.promptTokens;
  cumulative.completionTokens += addition.completionTokens;
  cumulative.totalTokens += addition.totalTokens;
  if (addition.thinkingTokens) {
    cumulative.thinkingTokens = (cumulative.thinkingTokens ?? 0) + addition.thinkingTokens;
  }
  if (addition.cacheReadTokens) {
    cumulative.cacheReadTokens = (cumulative.cacheReadTokens ?? 0) + addition.cacheReadTokens;
  }
  if (addition.cacheCreationTokens) {
    cumulative.cacheCreationTokens = (cumulative.cacheCreationTokens ?? 0) + addition.cacheCreationTokens;
  }
}
```

Optional fields are only set when the source has them; otherwise the
accumulated object has no key for them (avoids polluting the object with
`undefined` values when providers don't report these).

---

## Stream Handler — createStreamHandler (stream-handler.ts)

Processes raw AI SDK `fullStream` parts and emits `StreamEvent` objects.
The `step-finish` event is what feeds `lastPromptTokens` in the runner.

```typescript
export function createStreamHandler(onEvent: SessionEventCallback) {
  const state = createInitialState();

  function processPart(part: FullStreamPart): void {
    switch (part.type) {
      case 'text-delta':      handleTextDelta(part);    break;
      case 'reasoning-delta': handleReasoningDelta(part); break;
      case 'tool-call':       handleToolCall(part);     break;
      case 'tool-result':     handleToolResult(part);   break;
      case 'tool-error':      handleToolError(part);    break;
      case 'finish-step':     handleFinishStep(part);   break;
      case 'error':           handleError(part);        break;
      // All other parts ignored
    }
  }

  function handleFinishStep(part: FinishStepPart): void {
    state.stepNumber++;
    const promptTokens = part.usage?.promptTokens ?? 0;
    const completionTokens = part.usage?.completionTokens ?? 0;
    const totalTokens = promptTokens + completionTokens;

    state.cumulativeUsage.promptTokens += promptTokens;
    state.cumulativeUsage.completionTokens += completionTokens;
    state.cumulativeUsage.totalTokens += totalTokens;

    emit({ type: 'step-finish', stepNumber: state.stepNumber, usage: { promptTokens, completionTokens, totalTokens } });
    emit({ type: 'usage-update', usage: { ...state.cumulativeUsage } });
  }

  function getSummary() {
    return {
      stepsExecuted: state.stepNumber,
      toolCallCount: state.toolCallCount,
      usage: { ...state.cumulativeUsage },
    };
  }

  return { processPart, getSummary };
}
```

---

## Full Data Flow Diagram

```
runContinuableSession(config, options, continuationConfig)
  |
  +-- loop (i = 0 to maxContinuations)
  |     |
  |     +-- runAgentSession(currentConfig, options)
  |           |
  |           +-- executeStream(config, tools, onEvent, memoryContext)
  |                 |
  |                 +-- streamText({ model, system, messages, tools,
  |                 |                stopWhen, abortSignal,
  |                 |                prepareStep, onStepFinish })
  |                 |
  |                 +-- for await part in result.fullStream
  |                 |     streamHandler.processPart(part)
  |                 |       -> finish-step -> lastPromptTokens updated
  |                 |
  |                 +-- prepareStep (between each step):
  |                       if promptTokens > limit * 0.90:
  |                         contextWindowAbortController.abort('__context_window_exhausted__')
  |                       elif promptTokens > limit * 0.85:
  |                         inject warning system message
  |                 |
  |                 +-- catch (stream threw due to abort):
  |                       if contextWindowAbortController.signal.reason == sentinel:
  |                         return { outcome: 'context_window', messages, ... }
  |
  +-- if outcome != 'context_window': return merged result
  |
  +-- if i >= maxContinuations: return { outcome: 'completed', ... }
  |
  +-- compactSessionMessages(result.messages, continuationConfig)
  |     |
  |     +-- serialize messages to text
  |     +-- truncate to 30,000 chars
  |     +-- generateText(haiku, summarizer_system, focused_prompt)
  |     +-- fallback: rawTruncation(last 5 messages, 3000 chars)
  |
  +-- buildContinuationPrompt(summary, continuationCount)
  |     -> "## Session Continuation (N)\n\n...summary...\n\nContinue where you left off..."
  |
  +-- currentConfig = { ...config, initialMessages: [continuationMessage] }
  |
  +-- continue loop
```

---

## Key Implementation Notes for Reimplementation

1. **Separate abort controllers**: Use a dedicated `AbortController` for context
   window (not the user's signal). Distinguish via a sentinel reason string, not
   just `.aborted`. Both signals are merged with `AbortSignal.any()`.

2. **prepareStep runs before each step**: This is where detection and hard abort
   happen. The 90% check must come before the 85% warning check so the abort
   fires before the warning injection executes.

3. **`lastPromptTokens` is cumulative**: The AI SDK reports total prompt tokens
   per step (full history size), not deltas. Use the latest value directly.

4. **Config cloning pattern**: The continuation creates `{ ...config, initialMessages: [msg] }`.
   Everything else (model, system prompt, tools, maxSteps, contextWindowLimit)
   is preserved. The context window guard will fire again if the continuation
   also exhausts, enabling chained continuation.

5. **Max continuations is a soft cap**: When `i >= maxContinuations`, the outcome
   is forced to `'completed'` rather than surfacing `'context_window'` to callers.
   The agent did useful work and should be treated as done.

6. **Summarizer uses a cheap/fast model**: Haiku is used for compaction, not the
   same model as the session. This is cheap and fast. Auth credentials are passed
   through from `ContinuationConfig` so the same auth works for both.

7. **Message history is discarded in continuations**: Only the summary is
   injected. The previous session's messages are not forwarded. The fresh session
   starts with a clean context containing only the continuation prompt.

8. **Metrics are always accumulated**: Even if the session fails mid-continuation,
   the already-accumulated steps/tokens/duration from prior segments are included
   in the return value.

9. **Abort is checked before compaction**: If the user cancels during a
   `context_window` segment, the loop checks `config.abortSignal?.aborted`
   before calling `compactSessionMessages`. This prevents firing a summarization
   call on a cancelled run.

10. **Fallback is last-5-messages**: The raw truncation takes the last 5 messages
    (most recent context) and truncates from the end to `RAW_TRUNCATION_CHARS`.
    It prioritizes recency over completeness.
