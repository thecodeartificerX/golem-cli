# Aperant Orchestrator Tiers — Reference

Extracted from `F:\Tools\External\Aperant\apps\desktop\src\main\ai\` on 2026-03-29.
Covers the two orchestrators (Spec + Build), complexity tier gating, phase configuration,
agent registry, context accumulation, and phase transition validation.

---

## 1. Complexity Tier System

**File:** `apps/desktop/src/main/ai/orchestration/spec-orchestrator.ts`

### Type

```typescript
export type ComplexityTier = 'simple' | 'standard' | 'complex';
```

### Phase Sets Per Tier

```typescript
const COMPLEXITY_PHASES: Record<ComplexityTier, SpecPhase[]> = {
  simple: ['quick_spec', 'validation'],
  standard: ['discovery', 'requirements', 'spec_writing', 'planning', 'validation'],
  complex: [
    'discovery',
    'requirements',
    'research',
    'context',
    'spec_writing',
    'self_critique',
    'planning',
    'validation',
  ],
} as const;
```

### Dynamic Phase Injection (Post-Assessment)

After the AI complexity assessment, two optional phases can be injected into any tier's
phase list based on the assessment result's `needs_research` and `needs_self_critique` flags:

```typescript
// Insert research before 'context' (or before 'spec_writing' if no context phase)
if (this.assessment?.needs_research && !phasesToRun.includes('research')) {
  const insertBefore = phasesToRun.indexOf('context') !== -1
    ? phasesToRun.indexOf('context')
    : phasesToRun.indexOf('spec_writing');
  if (insertBefore !== -1) {
    phasesToRun.splice(insertBefore, 0, 'research');
  }
}

// Insert self_critique before 'planning'
if (this.assessment?.needs_self_critique && !phasesToRun.includes('self_critique')) {
  const planningIdx = phasesToRun.indexOf('planning');
  if (planningIdx !== -1) {
    phasesToRun.splice(planningIdx, 0, 'self_critique');
  }
}
```

### Heuristic Fast-Path (Skips AI Assessment)

```typescript
private assessComplexityHeuristic(taskDescription: string): ComplexityTier | null {
  const desc = taskDescription.toLowerCase().trim();
  const wordCount = desc.split(/\s+/).length;

  // Very short descriptions (under 30 words) with simple signal words -> SIMPLE
  if (wordCount <= 30) {
    const simplePatterns = [
      /\b(change|rename|update|replace|swap|switch)\b.*\b(color|colour|name|text|label|title|string|value|icon|logo)\b/,
      /\b(fix|correct)\b.*\b(typo|spelling|grammar)\b/,
      /\b(bump|update)\b.*\b(version|dependency)\b/,
      /\b(remove|delete)\b.*\b(unused|dead|deprecated)\b/,
    ];
    if (simplePatterns.some(p => p.test(desc))) {
      return 'simple';
    }
  }
  return null; // Let AI decide
}
```

### Complexity Assessment Run Order

1. Check heuristic (`assessComplexityHeuristic`) — returns `'simple'` or `null`.
2. If `complexityOverride` provided — use it directly (skip AI).
3. If `useAiAssessment !== false` — run AI assessment as phase 1 (agent: `spec_gatherer`,
   specPhase: `complexity_assessment`, with `ComplexityAssessmentOutputSchema`).
4. Fallback on AI failure: `'standard'` with confidence `0.5`.

---

## 2. Spec Orchestrator Phase Definitions

**File:** `apps/desktop/src/main/ai/orchestration/spec-orchestrator.ts`

### All Spec Phases

```typescript
export type SpecPhase =
  | 'discovery'
  | 'requirements'
  | 'complexity_assessment'
  | 'historical_context'
  | 'research'
  | 'context'
  | 'spec_writing'
  | 'self_critique'
  | 'planning'
  | 'validation'
  | 'quick_spec';
```

### Phase -> Agent Type Map

```typescript
const PHASE_AGENT_MAP: Record<SpecPhase, AgentType> = {
  discovery:             'spec_discovery',
  requirements:          'spec_gatherer',
  complexity_assessment: 'spec_gatherer',
  historical_context:    'spec_context',
  research:              'spec_researcher',
  context:               'spec_context',
  spec_writing:          'spec_writer',
  self_critique:         'spec_critic',
  planning:              'planner',
  validation:            'spec_validation',
  quick_spec:            'spec_writer',
} as const;
```

### Phase -> Output Files Map

```typescript
const PHASE_OUTPUTS: Partial<Record<SpecPhase, string[]>> = {
  discovery:             ['context.json'],
  requirements:          ['requirements.json'],
  complexity_assessment: ['complexity_assessment.json'],
  research:              ['research.json'],
  context:               ['context.json'],
  spec_writing:          ['spec.md'],
  self_critique:         ['spec.md'],
  planning:              ['implementation_plan.json'],
  quick_spec:            ['spec.md', 'implementation_plan.json'],
};
```

### Phase Retry Constants

```typescript
const MAX_PHASE_RETRIES = 2;
const MAX_PHASE_OUTPUT_SIZE = 12_000; // characters
```

### Phases That Use Structured Output (Zod Schema)

- `planning` and `quick_spec` both pass `ImplementationPlanOutputSchema` to `runSession`.
- `complexity_assessment` passes `ComplexityAssessmentOutputSchema`.
- All other phases: no `outputSchema` (agent writes files via tool calls).

### Schema Validation Phases

Only `planning` and `quick_spec` phases run Zod schema validation after completion:

```typescript
private async validatePhaseSchema(phase: SpecPhase): Promise<{ valid: boolean; errors: string[] } | null> {
  if (phase === 'planning' || phase === 'quick_spec') {
    const planPath = join(this.config.specDir, 'implementation_plan.json');
    try {
      const result = await validateAndNormalizeJsonFile(planPath, ImplementationPlanSchema);
      return { valid: result.valid, errors: result.errors };
    } catch {
      return null; // File doesn't exist yet
    }
  }
  return null;
}
```

### Per-Phase Retry Logic (Simplified)

```typescript
for (let attempt = 0; attempt <= MAX_PHASE_RETRIES; attempt++) {
  // 1. Generate prompt (with schemaRetryContext or toolUseRetryContext if retry)
  // 2. runSession(...)
  // 3. If outcome === 'cancelled': return failure
  // 4. If outcome === 'completed'|'max_steps'|'context_window':
  //    a. If planning phase and structuredOutput present: write implementation_plan.json
  //    b. Check missing output files -> build toolUseRetryContext, continue if attempt < MAX
  //    c. Run schema validation -> build schemaRetryContext, continue if attempt < MAX
  //    d. Return success
  // 5. If outcome === 'auth_failure': return failure immediately (non-retryable)
  // 6. Otherwise: collect error, retry if attempt < MAX
}
```

---

## 3. Context Accumulation Between Phases

**File:** `apps/desktop/src/main/ai/orchestration/spec-orchestrator.ts`

### Mechanism

After every phase completes (including `complexity_assessment`), `capturePhaseOutput()` is called.
It reads each output file listed in `PHASE_OUTPUTS[phase]`, truncates to `MAX_PHASE_OUTPUT_SIZE`
(12,000 chars), and stores the content in `this.phaseSummaries` (a `Record<string, string>` keyed
by filename).

```typescript
private async capturePhaseOutput(phase: SpecPhase): Promise<void> {
  const outputFiles = PHASE_OUTPUTS[phase];
  if (!outputFiles?.length) return;

  for (const fileName of outputFiles) {
    try {
      const filePath = join(this.config.specDir, fileName);
      const content = await readFile(filePath, 'utf-8');
      if (content.trim()) {
        this.phaseSummaries[fileName] = content.length > MAX_PHASE_OUTPUT_SIZE
          ? content.slice(0, MAX_PHASE_OUTPUT_SIZE) + '\n... (truncated)'
          : content;
      }
    } catch {
      // File may not exist if phase didn't produce it
    }
  }
}
```

### Injection Into Next Phase

On every phase run attempt, `priorPhaseOutputs` is passed to both `generatePrompt()` and `runSession()`:

```typescript
const phaseOutputs = Object.keys(this.phaseSummaries).length > 0 ? { ...this.phaseSummaries } : undefined;

const prompt = await this.config.generatePrompt(agentType, phase, {
  // ...
  priorPhaseOutputs: phaseOutputs,
});

const result = await this.config.runSession({
  // ...
  priorPhaseOutputs: phaseOutputs,
  projectIndex: this.config.projectIndex,
});
```

The caller is responsible for embedding `priorPhaseOutputs` into the kickoff message. This
eliminates redundant file re-reads between agents in sequential phases.

---

## 4. Build Orchestrator

**File:** `apps/desktop/src/main/ai/orchestration/build-orchestrator.ts`

### Build Phases

```typescript
type BuildPhase = 'planning' | 'coding' | 'qa_review' | 'qa_fixing';

const PHASE_AGENT_MAP: Record<BuildPhase, AgentType> = {
  planning:  'planner',
  coding:    'coder',
  qa_review: 'qa_reviewer',
  qa_fixing: 'qa_fixer',
} as const;

// Maps BuildPhase -> Phase (the 4-bucket config key: spec/planning/coding/qa)
const PHASE_CONFIG_MAP: Record<BuildPhase, Phase> = {
  planning:  'planning',
  coding:    'coding',
  qa_review: 'qa',
  qa_fixing: 'qa',
} as const;
```

### Full Run Sequence (`BuildOrchestrator.run()`)

```typescript
async run(): Promise<BuildOutcome> {
  // 1. Check if implementation_plan.json exists
  const isFirstRun = await this.isFirstRun();

  if (isFirstRun) {
    // 2. Run planning phase
    const planResult = await this.runPlanningPhase();
    if (!planResult.success) return this.buildOutcome(false, ...);

    // 3. Reset all subtask statuses to "pending"
    await this.resetSubtaskStatuses();
  }

  // 4. Validate + normalize plan with Zod schema
  const preCodingValidation = await validateAndNormalizeJsonFile(planPath, ImplementationPlanSchema);
  if (!preCodingValidation.valid) return this.buildOutcome(false, ...);

  // 5. Check if already complete (all subtasks = "completed")
  if (await this.isBuildComplete()) { ... return success; }

  // 6. Coding phase (subtask iterator)
  const codingResult = await this.runCodingPhase();
  if (!codingResult.success) return this.buildOutcome(false, ...);

  // 7. QA phase (review + optional fix loop)
  const qaResult = await this.runQAPhase();
  return this.buildOutcome(qaResult.success, ...);
}
```

### Planning Phase with Lightweight LLM Repair

```typescript
private async runPlanningPhase(): Promise<{ success: boolean; error?: string }> {
  // MAX_PLANNING_VALIDATION_RETRIES = 3
  for (let attempt = 0; attempt < MAX_PLANNING_VALIDATION_RETRIES + 1; attempt++) {
    // Run planner session with ImplementationPlanOutputSchema
    const result = await this.config.runSession({ outputSchema: ImplementationPlanOutputSchema, ... });

    // If structured output present, write to file directly
    if (result.structuredOutput) { await writeFile(planPath, ...); }

    // Validate with Zod schema
    const validation = await validateAndNormalizeJsonFile(planPath, ImplementationPlanSchema);
    if (validation.valid) {
      this.markPhaseCompleted('planning');
      return { success: true };
    }

    // Try lightweight LLM repair first (single generateText call, no tools)
    if (this.config.getModel) {
      const model = await this.config.getModel('planner');
      const repairResult = await repairJsonWithLLM(planPath, ImplementationPlanSchema, ImplementationPlanOutputSchema, model, validation.errors, IMPLEMENTATION_PLAN_SCHEMA_HINT);
      if (repairResult.valid) {
        this.markPhaseCompleted('planning');
        return { success: true };
      }
    }

    // Fallback: full re-plan with retry context injected into prompt
    if (validationFailures >= MAX_PLANNING_VALIDATION_RETRIES) return { success: false, error: ... };
    planningRetryContext = buildValidationRetryPrompt('implementation_plan.json', validation.errors, IMPLEMENTATION_PLAN_SCHEMA_HINT);
  }
}
```

### QA Phase Loop

```typescript
private async runQAPhase(): Promise<{ success: boolean; error?: string }> {
  const maxQACycles = 3;
  for (let cycle = 0; cycle < maxQACycles; cycle++) {
    // Run qa_reviewer agent
    const reviewResult = await this.config.runSession({ agentType: 'qa_reviewer', phase: 'qa', ... });
    const qaStatus = await this.readQAStatus(); // reads qa_report.md

    if (qaStatus === 'passed') {
      this.markPhaseCompleted('qa_review');
      this.transitionPhase('complete', 'Build complete - QA passed');
      return { success: true };
    }

    if ((qaStatus === 'failed' || qaStatus === 'unknown') && cycle < maxQACycles - 1) {
      this.markPhaseCompleted('qa_review');
      this.transitionPhase('qa_fixing', 'Fixing QA issues');

      // Run qa_fixer agent
      await this.config.runSession({ agentType: 'qa_fixer', phase: 'qa', ... });
      this.markPhaseCompleted('qa_fixing');

      // Delete qa_report.md so reviewer writes a clean verdict
      await this.resetQAReport();

      this.transitionPhase('qa_review', 'Re-running QA review after fixes');
      continue; // Loop back to review
    }

    this.transitionPhase('failed', 'QA review failed after maximum fix cycles');
    return { success: false, error: 'QA review failed after maximum fix cycles' };
  }
}
```

### QA Status Detection

```typescript
private async readQAStatus(): Promise<'passed' | 'failed' | 'unknown'> {
  const content = await readFile(join(this.config.specDir, 'qa_report.md'), 'utf-8');
  const lower = content.toLowerCase();
  if (lower.includes('status: passed') || lower.includes('status: approved')) return 'passed';
  if (lower.includes('status: failed') || lower.includes('status: rejected') || lower.includes('status: needs changes')) return 'failed';
  if (content.trim().length > 0) return 'unknown'; // Intermediate state (FIXES_APPLIED, etc.)
  return 'unknown';
}
```

### Build Phase Constants

```typescript
const AUTO_CONTINUE_DELAY_MS = 3_000;
const MAX_PLANNING_VALIDATION_RETRIES = 3;
const MAX_SUBTASK_RETRIES = 3;
const ERROR_RETRY_DELAY_MS = 5_000;
```

---

## 5. Phase Transition Validation (`isValidPhaseTransition`)

**File:** `apps/desktop/src/shared/constants/phase-protocol.ts`

### All Execution Phases

```typescript
export const EXECUTION_PHASES = [
  'idle', 'planning', 'coding',
  'rate_limit_paused', 'auth_failure_paused',
  'qa_review', 'qa_fixing', 'complete', 'failed'
] as const;

export type ExecutionPhase = (typeof EXECUTION_PHASES)[number];
export type CompletablePhase = 'planning' | 'coding' | 'qa_review' | 'qa_fixing';
```

### Phase Order Index (For Regression Detection)

```typescript
export const PHASE_ORDER_INDEX: Readonly<Record<ExecutionPhase, number>> = {
  idle: -1,
  planning: 0,
  coding: 1,
  rate_limit_paused: 1,   // same level as coding (pauses during coding)
  auth_failure_paused: 1, // same level as coding
  qa_review: 2,
  qa_fixing: 3,
  complete: 4,
  failed: 99,
} as const;
```

### Terminal and Pause Phase Sets

```typescript
export const TERMINAL_PHASES: ReadonlySet<ExecutionPhase> = new Set(['complete', 'failed']);
export const PAUSE_PHASES: ReadonlySet<ExecutionPhase> = new Set(['rate_limit_paused', 'auth_failure_paused']);
```

### `isValidPhaseTransition()` Full Signature and Logic

```typescript
export function isValidPhaseTransition(
  currentPhase: ExecutionPhase,
  newPhase: ExecutionPhase,
  completedPhases: CompletablePhase[] = []
): boolean {
  // Terminal phases cannot transition out
  if (isTerminalPhase(currentPhase)) return false;

  // idle can transition to any backend phase
  if (currentPhase === 'idle') return BACKEND_PHASES.includes(newPhase as BackendPhase);

  // Same phase = progress update, always valid
  if (currentPhase === newPhase) return true;

  // Prerequisite map
  const phasePrerequisites: Record<ExecutionPhase, CompletablePhase[]> = {
    idle:                 [],
    planning:             [],
    coding:               ['planning'],
    rate_limit_paused:    [],
    auth_failure_paused:  [],
    qa_review:            ['coding'],
    qa_fixing:            ['qa_review'],
    complete:             ['qa_review', 'qa_fixing'],
    failed:               [],
  };

  // Special cases (no prerequisite check needed)
  if (newPhase === 'failed') return true;
  if (currentPhase === 'qa_fixing' && newPhase === 'qa_review') return true; // re-review loop
  if (currentPhase === 'coding' && isPausePhase(newPhase)) return true;     // pause during coding
  if (isPausePhase(currentPhase) && newPhase === 'coding') return true;     // resume from pause

  // Prerequisite check: at least one prerequisite must be in completedPhases
  const prerequisites = phasePrerequisites[newPhase];
  if (prerequisites.length === 0) return true;
  return prerequisites.some(p => completedPhases.includes(p));
}
```

### `transitionPhase()` in BuildOrchestrator

```typescript
private transitionPhase(phase: ExecutionPhase, message: string): void {
  // Cannot leave a terminal phase
  if (isTerminalPhase(this.currentPhase) && !isTerminalPhase(phase)) return;

  if (!isValidPhaseTransition(this.currentPhase, phase, this.completedPhases)) {
    this.emitTyped('log', `Blocked phase transition: ${this.currentPhase} -> ${phase}`);
    return;
  }

  this.currentPhase = phase;
  this.emitTyped('phase-change', phase, message);
}
```

---

## 6. Phase-Specific Model and Thinking Configuration

**File:** `apps/desktop/src/main/ai/config/phase-config.ts`
**File:** `apps/desktop/src/main/ai/config/types.ts`

### Model Shorthands -> Full IDs

```typescript
export const MODEL_ID_MAP: Record<ModelShorthand, string> = {
  opus:      'claude-opus-4-6',
  'opus-1m': 'claude-opus-4-6',
  'opus-4.5':'claude-opus-4-5-20251101',
  sonnet:    'claude-sonnet-4-6',
  haiku:     'claude-haiku-4-5-20251001',
} as const;

// beta headers required per shorthand
export const MODEL_BETAS_MAP: Partial<Record<ModelShorthand, string[]>> = {
  'opus-1m': ['context-1m-2025-08-07'],
} as const;
```

### Thinking Budget Tokens

```typescript
export const THINKING_BUDGET_MAP: Record<ThinkingLevel, number> = {
  low:    1024,
  medium: 4096,
  high:   16384,
  xhigh:  32768,
} as const;

export const EFFORT_LEVEL_MAP: Record<EffortLevel, string> = {
  low: 'low', medium: 'medium', high: 'high', xhigh: 'xhigh',
} as const;

// Only Opus 4.6 gets both maxThinkingTokens + effortLevel
export const ADAPTIVE_THINKING_MODELS: ReadonlySet<string> = new Set(['claude-opus-4-6']);
```

### Default Phase Models and Thinking Levels (Balanced Profile)

```typescript
// 4-bucket config: spec | planning | coding | qa
export const DEFAULT_PHASE_MODELS: PhaseModelConfig = {
  spec:     'sonnet',
  planning: 'sonnet',
  coding:   'sonnet',
  qa:       'sonnet',
};

export const DEFAULT_PHASE_THINKING: PhaseThinkingConfig = {
  spec:     'medium',
  planning: 'high',
  coding:   'medium',
  qa:       'high',
};
```

### Spec Phase Thinking Levels (Overrides the 4-bucket for spec sub-phases)

```typescript
export const SPEC_PHASE_THINKING_LEVELS: Record<string, ThinkingLevel> = {
  // Heavy phases
  discovery:             'high',
  spec_writing:          'high',
  self_critique:         'high',
  // Light phases
  requirements:          'medium',
  research:              'medium',
  context:               'medium',
  planning:              'medium',
  validation:            'medium',
  quick_spec:            'medium',
  historical_context:    'medium',
  complexity_assessment: 'medium',
};
```

### Model Resolution Priority (`getPhaseModel`)

```typescript
export async function getPhaseModel(specDir: string, phase: Phase, cliModel?: string | null): Promise<string> {
  if (cliModel) return resolveModelId(cliModel);                          // 1. CLI override

  const metadata = await loadTaskMetadata(specDir);                        // reads task_metadata.json
  if (metadata) {
    if (metadata.isAutoProfile && metadata.phaseModels) {
      return resolveModelId(metadata.phaseModels[phase] ?? DEFAULT_PHASE_MODELS[phase]); // 2. Auto profile per-phase
    }
    if (metadata.model) return resolveModelId(metadata.model);            // 3. Single model from metadata
  }

  return resolveModelId(DEFAULT_PHASE_MODELS[phase]);                      // 4. Default
}
```

Same priority for `getPhaseThinking()` (replaces model with thinkingLevel).

### Full Phase Config Tuple

```typescript
export async function getPhaseConfig(
  specDir: string,
  phase: Phase,
  cliModel?: string | null,
  cliThinking?: string | null,
): Promise<[string, string, number]> {
  const modelId       = await getPhaseModel(specDir, phase, cliModel);
  const thinkingLevel = await getPhaseThinking(specDir, phase, cliThinking);
  const thinkingBudget = getThinkingBudget(thinkingLevel);
  return [modelId, thinkingLevel, thinkingBudget];
}
```

### Thinking Kwargs (Adaptive vs Standard Models)

```typescript
export function getThinkingKwargsForModel(modelId: string, thinkingLevel: string): ThinkingKwargs {
  const kwargs: ThinkingKwargs = {
    maxThinkingTokens: getThinkingBudget(thinkingLevel),
  };
  if (isAdaptiveModel(modelId)) {           // Opus 4.6 only
    kwargs.effortLevel = EFFORT_LEVEL_MAP[thinkingLevel as ThinkingLevel] ?? 'medium';
  }
  return kwargs;
}
```

### Provider-Specific Thinking Options (`buildThinkingProviderOptions`)

```typescript
export function buildThinkingProviderOptions(modelId: string, thinkingLevel: ThinkingLevel) {
  // anthropic: { thinking: { type: 'enabled', budgetTokens } }
  // openai o1/o3/o4: { reasoningEffort: 'low'|'medium'|'high' }
  // google: { thinkingConfig: { thinkingBudget: N } }
  // zai (GLM): { openaiCompatible: { thinking: { type: 'enabled', clear_thinking: false } } }
}
```

---

## 7. Agent Configuration Registry

**File:** `apps/desktop/src/main/ai/config/agent-configs.ts`

### Tool Bundles

```typescript
const BASE_READ_TOOLS  = ['Read', 'Glob', 'Grep'] as const;
const BASE_WRITE_TOOLS = ['Write', 'Edit', 'Bash'] as const;
const WEB_TOOLS        = ['WebFetch', 'WebSearch'] as const;
const ALL_BUILTIN_TOOLS = [...BASE_READ_TOOLS, ...BASE_WRITE_TOOLS, ...WEB_TOOLS] as const;

// Spec pipeline: read + Write + web (no Edit, no Bash)
const SPEC_TOOLS = [...BASE_READ_TOOLS, 'Write', ...WEB_TOOLS] as const;
```

### Custom MCP Tools (auto-claude server)

```typescript
const TOOL_UPDATE_SUBTASK_STATUS = 'mcp__auto-claude__update_subtask_status';
const TOOL_GET_BUILD_PROGRESS    = 'mcp__auto-claude__get_build_progress';
const TOOL_RECORD_DISCOVERY      = 'mcp__auto-claude__record_discovery';
const TOOL_RECORD_GOTCHA         = 'mcp__auto-claude__record_gotcha';
const TOOL_GET_SESSION_CONTEXT   = 'mcp__auto-claude__get_session_context';
const TOOL_UPDATE_QA_STATUS      = 'mcp__auto-claude__update_qa_status';
```

### AgentConfig Interface

```typescript
export interface AgentConfig {
  tools: readonly string[];           // Built-in tools
  mcpServers: readonly string[];      // Always-started MCP servers
  mcpServersOptional?: readonly string[]; // Conditionally started
  autoClaudeTools: readonly string[]; // Custom MCP tools from auto-claude server
  thinkingDefault: ThinkingLevel;     // 'low'|'medium'|'high'
}
```

### Full Agent Registry

| Agent Type | Tools | MCP Servers | Optional MCP | Auto-Claude Tools | Thinking |
|---|---|---|---|---|---|
| `spec_gatherer` | SPEC_TOOLS | context7 | — | — | medium |
| `spec_researcher` | SPEC_TOOLS | context7 | — | — | medium |
| `spec_writer` | SPEC_TOOLS | context7 | — | — | high |
| `spec_critic` | SPEC_TOOLS | context7 | — | — | high |
| `spec_discovery` | SPEC_TOOLS | context7 | — | — | medium |
| `spec_context` | SPEC_TOOLS | context7 | — | — | medium |
| `spec_validation` | SPEC_TOOLS | context7 | — | — | high |
| `spec_compaction` | SPEC_TOOLS | context7 | — | — | medium |
| `spec_orchestrator` | ALL_BUILTIN + SpawnSubagent | context7 | — | — | high |
| `build_orchestrator` | ALL_BUILTIN + SpawnSubagent | context7, memory, auto-claude | linear | get_build_progress, get_session_context, record_discovery, update_subtask_status | high |
| `planner` | ALL_BUILTIN | context7, memory, auto-claude | linear | get_build_progress, get_session_context, record_discovery | high |
| `coder` | ALL_BUILTIN | context7, memory, auto-claude | linear | update_subtask_status, get_build_progress, record_discovery, record_gotcha, get_session_context | low |
| `qa_reviewer` | ALL_BUILTIN | context7, memory, auto-claude, browser | linear | get_build_progress, update_qa_status, get_session_context | high |
| `qa_fixer` | ALL_BUILTIN | context7, memory, auto-claude, browser | linear | update_subtask_status, get_build_progress, update_qa_status, record_gotcha | medium |
| `insights` | ALL_BUILTIN | — | — | — | low |
| `merge_resolver` | — | — | — | — | low |
| `commit_message` | — | — | — | — | low |
| `pr_template_filler` | ALL_BUILTIN | — | — | — | low |
| `pr_reviewer` | ALL_BUILTIN | context7 | — | — | high |
| `pr_orchestrator_parallel` | ALL_BUILTIN | context7 | — | — | high |
| `pr_followup_parallel` | ALL_BUILTIN | context7 | — | — | high |
| `pr_followup_extraction` | — | — | — | — | low |
| `pr_finding_validator` | ALL_BUILTIN | — | — | — | medium |
| `pr_security_specialist` | BASE_READ_TOOLS | — | — | — | medium |
| `pr_quality_specialist` | BASE_READ_TOOLS | — | — | — | medium |
| `pr_logic_specialist` | BASE_READ_TOOLS | — | — | — | medium |
| `pr_codebase_fit_specialist` | BASE_READ_TOOLS | — | — | — | medium |
| `analysis` | ALL_BUILTIN | context7 | — | — | medium |
| `batch_analysis` | ALL_BUILTIN | — | — | — | low |
| `batch_validation` | ALL_BUILTIN | — | — | — | low |
| `roadmap_discovery` | ALL_BUILTIN | context7 | — | — | high |
| `competitor_analysis` | ALL_BUILTIN | context7 | — | — | high |
| `ideation` | ALL_BUILTIN | — | — | — | high |

Notes:
- `browser` in `mcpServers` is a virtual name resolved at runtime to `electron` (if `is_electron + electronMcpEnabled`) or `puppeteer` (if `is_web_frontend + puppeteerMcpEnabled`).
- `memory` server is only started if `memoryEnabled` is true (i.e., `GRAPHITI_MCP_URL` is set).
- `linear` server is only started if in `mcpServersOptional` AND `linearEnabled` is true.

### `getRequiredMcpServers()` Resolution

```typescript
export function getRequiredMcpServers(agentType: AgentType, options: McpServerResolveOptions = {}): string[] {
  const servers = [...config.mcpServers];

  // 1. Remove context7 if explicitly disabled
  // 2. Add optional linear if linearEnabled
  // 3. Replace 'browser' with electron or puppeteer based on project caps
  // 4. Remove 'memory' if !memoryEnabled
  // 5. Apply agentMcpAdd (comma-sep server names appended)
  // 6. Apply agentMcpRemove (comma-sep, never removes 'auto-claude')
  return servers;
}
```

---

## 8. Key Configuration Interfaces

### SpecOrchestratorConfig (caller must implement)

```typescript
export interface SpecOrchestratorConfig {
  specDir: string;
  projectDir: string;
  taskDescription?: string;
  complexityOverride?: ComplexityTier;
  useAiAssessment?: boolean;           // default true
  projectIndex?: string;               // pre-generated project index JSON
  cliModel?: string;
  cliThinking?: string;
  abortSignal?: AbortSignal;
  generatePrompt: (agentType: AgentType, phase: SpecPhase, context: SpecPromptContext) => Promise<string>;
  runSession: (config: SpecSessionRunConfig) => Promise<SessionResult>;
}
```

### SpecPromptContext (passed to generatePrompt)

```typescript
export interface SpecPromptContext {
  phaseNumber: number;
  totalPhases: number;
  phaseName: SpecPhase;
  taskDescription?: string;
  complexity?: ComplexityTier;
  projectIndex?: string;
  priorPhaseOutputs?: Record<string, string>; // filename -> content
  attemptCount: number;
  schemaRetryContext?: string;                 // LLM-friendly error feedback for retry
}
```

### BuildOrchestratorConfig (caller must implement)

```typescript
export interface BuildOrchestratorConfig {
  specDir: string;
  projectDir: string;
  sourceSpecDir?: string;        // for worktree mode syncing
  cliModel?: string;
  cliThinking?: string;
  maxIterations?: number;        // 0 = unlimited
  abortSignal?: AbortSignal;
  generatePrompt: (agentType: AgentType, phase: BuildPhase, context: PromptContext) => Promise<string>;
  runSession: (config: SessionRunConfig) => Promise<SessionResult>;
  syncSpecToSource?: (specDir: string, sourceSpecDir: string) => Promise<boolean>;
  getModel?: (agentType: AgentType) => Promise<import('ai').LanguageModel | undefined>; // for lightweight repair
}
```

### TaskMetadataConfig (task_metadata.json schema)

```typescript
export interface TaskMetadataConfig {
  isAutoProfile?: boolean;
  phaseModels?: Partial<Record<Phase, string>>;    // per-phase model overrides
  phaseThinking?: Partial<Record<Phase, string>>;  // per-phase thinking overrides
  model?: string;                                   // single model (non-auto profile)
  thinkingLevel?: string;
  fastMode?: boolean;
  phaseProviders?: Partial<Record<Phase, string>>; // cross-provider Custom profile
}
```

---

## 9. Implementation Plan JSON Schema (Zod Coercion)

The `ImplementationPlanSchema` (used in both orchestrators) normalizes LLM field name variations:

- `subtask_id` or `task_id` -> `id`
- `title` or `name` -> `description`
- `phase_id` -> `id` (on phase)
- `file_paths` -> `files_to_modify`
- Status normalization: `done` -> `completed`, `todo` -> `pending`, etc.
- Missing `status` defaults to `"pending"`

After `validateAndNormalizeJsonFile()` succeeds, it writes back the canonical form to disk.

---

## 10. MCP Tool Naming Convention

All custom MCP tools follow the pattern: `mcp__<server-name>__<tool-name>`

Examples:
- `mcp__auto-claude__update_subtask_status`
- `mcp__context7__query-docs`
- `mcp__graphiti-memory__search_nodes`
- `mcp__puppeteer__puppeteer_navigate`
- `mcp__electron__take_screenshot`
- `mcp__linear-server__create_issue`

The `getRequiredMcpServers()` helper returns short server names (`'context7'`, `'memory'`, etc.).
The caller maps these to actual MCP server configurations.
