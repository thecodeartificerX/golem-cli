# Aperant Memory System — Reference Implementation

Source tree: `F:\Tools\External\Aperant\apps\desktop\src\main\ai\memory\`

---

## Architecture Overview

Three-layer memory system built on libSQL (Turso-compatible):

1. **Storage layer** — libSQL database with FTS5 and vector embedding tables
2. **Retrieval layer** — three parallel search paths fused via weighted RRF, then graph-boosted and cross-encoder reranked
3. **Injection layer** — context injected at session start (planner) or between agent steps (prepareStep callback)

**Concurrency model (Electron):** `MemoryObserver` and `MemoryService` run on the main thread. Agent sessions run in worker threads. Worker threads communicate via `WorkerObserverProxy` → `MessagePort` IPC.

---

## File Map

```
src/main/ai/memory/
  types.ts                    — All TypeScript types (Memory, MemoryType, etc.)
  schema.ts                   — SQL DDL (MEMORY_SCHEMA_SQL, MEMORY_PRAGMA_SQL)
  db.ts                       — libSQL client factory (local / Turso sync / in-memory)
  embedding-service.ts        — EmbeddingService (5-tier provider auto-detect)
  memory-service.ts           — MemoryServiceImpl (store, search, CRUD)
  index.ts                    — Barrel export

  retrieval/
    query-classifier.ts       — detectQueryType, QUERY_TYPE_WEIGHTS
    bm25-search.ts            — searchBM25 (FTS5 MATCH)
    dense-search.ts           — searchDense (vector_distance_cos + JS fallback)
    graph-search.ts           — searchGraph (file-scoped, co-access, closure)
    rrf-fusion.ts             — weightedRRF
    graph-boost.ts            — applyGraphNeighborhoodBoost
    reranker.ts               — Reranker (Ollama qwen3-reranker / Cohere / passthrough)
    context-packer.ts         — packContext (phase-aware token budgets + MMR dedup)
    hyde.ts                   — hydeSearch (HyDE fallback for sparse results)
    pipeline.ts               — RetrievalPipeline (full orchestrator)
    index.ts                  — Barrel export

  observer/
    scratchpad.ts             — Scratchpad (in-memory session accumulator)
    signals.ts                — 17 ObserverSignal types + SIGNAL_VALUES table
    trust-gate.ts             — applyTrustGate (anti-injection defense)
    promotion.ts              — PromotionPipeline (8-stage filter)
    memory-observer.ts        — MemoryObserver (passive, <2ms per event)
    dead-end-detector.ts      — detectDeadEnd (language pattern matching)

  injection/
    step-injection-decider.ts — StepInjectionDecider (3 triggers)
    step-memory-state.ts      — StepMemoryState (rolling tool call window)
    planner-memory-context.ts — buildPlannerMemoryContext (pre-session injection)
    memory-stop-condition.ts  — buildMemoryAwareStopCondition, getCalibrationFactor
    prefetch-builder.ts       — (prefetch pattern builder)
    qa-context.ts             — (QA phase context builder)

  ipc/
    worker-observer-proxy.ts  — WorkerObserverProxy (worker-side IPC bridge)

  tools/
    record-memory.ts          — createRecordMemoryTool / createRecordMemoryStub
    search-memory.ts          — createSearchMemoryTool / createSearchMemoryStub

  graph/                      — Knowledge graph (AST extraction, incremental indexer)

src/main/ipc-handlers/context/
  memory-service-factory.ts   — getMemoryService() singleton factory
```

---

## Memory Types

Defined in `types.ts`:

```typescript
export type MemoryType =
  // Core
  | 'gotcha'            // pitfall to avoid
  | 'decision'          // architectural choice
  | 'preference'        // user preference
  | 'pattern'           // reusable approach
  | 'requirement'       // constraint
  | 'error_pattern'     // recurring error
  | 'module_insight'    // non-obvious module behavior
  // Active loop
  | 'prefetch_pattern'  // files to read together
  | 'work_state'        // mid-session work state
  | 'causal_dependency' // file coupling / ordering
  | 'task_calibration'  // historical step count data
  // V3+
  | 'e2e_observation'
  | 'dead_end'          // failed approach
  | 'work_unit_outcome' // outcome of a work unit
  | 'workflow_recipe'   // proven multi-step approach
  | 'context_cost';     // token/context cost observation

export type MemorySource =
  | 'agent_explicit'    // agent called record_memory tool
  | 'observer_inferred' // promoted from behavioral signals
  | 'qa_auto'
  | 'mcp_auto'
  | 'commit_auto'
  | 'user_taught';      // /remember command

export type MemoryScope = 'global' | 'module' | 'work_unit' | 'session';
```

**Extended types** for specific memory kinds:

```typescript
export interface WorkflowRecipe extends Memory {
  type: 'workflow_recipe';
  taskPattern: string;
  steps: Array<{ order: number; description: string; canonicalFile?: string; canonicalLine?: number }>;
  lastValidatedAt: string;
  successCount: number;
  scope: 'global';
}

export interface DeadEndMemory extends Memory {
  type: 'dead_end';
  approachTried: string;
  whyItFailed: string;
  alternativeUsed: string;
  taskContext: string;
  decayHalfLifeDays: 90;
}

export interface PrefetchPattern extends Memory {
  type: 'prefetch_pattern';
  alwaysReadFiles: string[];
  frequentlyReadFiles: string[];
  moduleTrigger: string;
  sessionCount: number;
  scope: 'module';
}

export interface TaskCalibration extends Memory {
  type: 'task_calibration';
  module: string;
  methodology: string;
  averageActualSteps: number;
  averagePlannedSteps: number;
  ratio: number;
  sampleCount: number;
}
```

---

## Core Memory Interface

```typescript
// types.ts
export interface Memory {
  id: string;
  type: MemoryType;
  content: string;
  confidence: number;          // 0.0–1.0
  tags: string[];
  relatedFiles: string[];      // absolute file paths
  relatedModules: string[];
  createdAt: string;           // ISO string
  lastAccessedAt: string;
  accessCount: number;
  workUnitRef?: WorkUnitRef;
  scope: MemoryScope;

  // Provenance
  source: MemorySource;
  sessionId: string;
  commitSha?: string;
  provenanceSessionIds: string[];

  // Knowledge graph link
  targetNodeId?: string;
  impactedNodeIds?: string[];
  relations?: MemoryRelation[];

  // Decay
  decayHalfLifeDays?: number;

  // Trust
  needsReview?: boolean;
  userVerified?: boolean;
  citationText?: string;
  pinned?: boolean;
  methodology?: string;

  // Chunking metadata (for AST-chunked code memories)
  chunkType?: 'function' | 'class' | 'module' | 'prose';
  chunkStartLine?: number;
  chunkEndLine?: number;
  contextPrefix?: string;
  embeddingModelId?: string;

  // DB
  projectId: string;
  trustLevelScope?: string;
  deprecated?: boolean;
  deprecatedAt?: string;
  staleAt?: string;
}

export interface MemoryRelation {
  targetMemoryId?: string;
  targetFilePath?: string;
  relationType: 'required_with' | 'conflicts_with' | 'validates' | 'supersedes' | 'derived_from';
  confidence: number;
  autoExtracted: boolean;
}

export interface WorkUnitRef {
  methodology: string;
  hierarchy: string[];
  label: string;
}
```

---

## Database Schema (`schema.ts`)

**Three core tables + FTS5 + embedding cache:**

```sql
-- Main memory store
CREATE TABLE IF NOT EXISTS memories (
  id                    TEXT PRIMARY KEY,
  type                  TEXT NOT NULL,
  content               TEXT NOT NULL,
  confidence            REAL NOT NULL DEFAULT 0.8,
  tags                  TEXT NOT NULL DEFAULT '[]',      -- JSON array
  related_files         TEXT NOT NULL DEFAULT '[]',      -- JSON array
  related_modules       TEXT NOT NULL DEFAULT '[]',      -- JSON array
  created_at            TEXT NOT NULL,
  last_accessed_at      TEXT NOT NULL,
  access_count          INTEGER NOT NULL DEFAULT 0,
  session_id            TEXT,
  commit_sha            TEXT,
  scope                 TEXT NOT NULL DEFAULT 'global',
  work_unit_ref         TEXT,                            -- JSON
  methodology           TEXT,
  source                TEXT NOT NULL DEFAULT 'agent_explicit',
  target_node_id        TEXT,
  impacted_node_ids     TEXT DEFAULT '[]',
  relations             TEXT NOT NULL DEFAULT '[]',
  decay_half_life_days  REAL,
  provenance_session_ids TEXT DEFAULT '[]',
  needs_review          INTEGER NOT NULL DEFAULT 0,
  user_verified         INTEGER NOT NULL DEFAULT 0,
  citation_text         TEXT,
  pinned                INTEGER NOT NULL DEFAULT 0,
  deprecated            INTEGER NOT NULL DEFAULT 0,
  deprecated_at         TEXT,
  stale_at              TEXT,
  project_id            TEXT NOT NULL,
  trust_level_scope     TEXT DEFAULT 'personal',
  chunk_type            TEXT,
  chunk_start_line      INTEGER,
  chunk_end_line        INTEGER,
  context_prefix        TEXT,
  embedding_model_id    TEXT
);

-- Vector embeddings (1024-dim Float32 BLOBs)
CREATE TABLE IF NOT EXISTS memory_embeddings (
  memory_id   TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
  embedding   BLOB NOT NULL,
  model_id    TEXT NOT NULL,
  dims        INTEGER NOT NULL DEFAULT 1024,
  created_at  TEXT NOT NULL
);

-- FTS5 full-text search index
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
  memory_id UNINDEXED,
  content,
  tags,
  related_files,
  tokenize='porter unicode61'
);

-- Embedding cache (7-day TTL)
CREATE TABLE IF NOT EXISTS embedding_cache (
  key        TEXT PRIMARY KEY,            -- sha256(text:modelId:dims)
  embedding  BLOB NOT NULL,
  model_id   TEXT NOT NULL,
  dims       INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
);
```

**Observer tables** (used by MemoryObserver to track behavioral signals):

```sql
-- File access tracking
CREATE TABLE IF NOT EXISTS observer_file_nodes (
  file_path TEXT PRIMARY KEY, project_id TEXT NOT NULL,
  access_count INTEGER NOT NULL DEFAULT 0,
  last_accessed_at TEXT NOT NULL, session_count INTEGER NOT NULL DEFAULT 0
);

-- Co-access edges (files accessed together)
CREATE TABLE IF NOT EXISTS observer_co_access_edges (
  file_a TEXT NOT NULL, file_b TEXT NOT NULL, project_id TEXT NOT NULL,
  weight REAL NOT NULL DEFAULT 0.0, raw_count INTEGER NOT NULL DEFAULT 0,
  session_count INTEGER NOT NULL DEFAULT 0, avg_time_delta_ms REAL,
  directional INTEGER NOT NULL DEFAULT 0,
  task_type_breakdown TEXT DEFAULT '{}', last_observed_at TEXT NOT NULL,
  promoted_at TEXT, PRIMARY KEY (file_a, file_b, project_id)
);

-- Error pattern tracking
CREATE TABLE IF NOT EXISTS observer_error_patterns (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
  tool_name TEXT NOT NULL, error_fingerprint TEXT NOT NULL,
  error_message TEXT NOT NULL, occurrence_count INTEGER NOT NULL DEFAULT 1,
  last_seen_at TEXT NOT NULL, resolved_how TEXT, sessions TEXT DEFAULT '[]'
);
```

**Key indexes:**

```sql
CREATE INDEX IF NOT EXISTS idx_memories_project_type     ON memories(project_id, type);
CREATE INDEX IF NOT EXISTS idx_memories_project_scope    ON memories(project_id, scope);
CREATE INDEX IF NOT EXISTS idx_memories_not_deprecated   ON memories(project_id, deprecated) WHERE deprecated = 0;
CREATE INDEX IF NOT EXISTS idx_memories_needs_review     ON memories(needs_review) WHERE needs_review = 1;
CREATE INDEX IF NOT EXISTS idx_memories_type_conf        ON memories(project_id, type, confidence DESC);
```

**PRAGMA setup** (applied separately, before `executeMultiple`):

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
```

---

## libSQL Database Backend (`db.ts`)

Three deployment modes:

```typescript
// Local file (Electron, offline)
export async function getMemoryClient(
  tursoSyncUrl?: string,
  authToken?: string,
): Promise<Client> {
  if (_client) return _client;
  const { app } = await import('electron');
  const localPath = join(app.getPath('userData'), 'memory.db');
  _client = loadCreateClient()({
    url: `file:${localPath}`,
    ...(tursoSyncUrl && authToken
      ? { syncUrl: tursoSyncUrl, authToken, syncInterval: 60 }
      : {}),
  });
  // Apply PRAGMAs individually first
  for (const pragma of MEMORY_PRAGMA_SQL.split('\n').filter(l => l.trim())) {
    try { await _client.execute(pragma); } catch { }
  }
  await _client.executeMultiple(MEMORY_SCHEMA_SQL);
  return _client;
}

// In-memory (tests, no Electron dependency)
export async function getInMemoryClient(): Promise<Client> {
  const client = loadCreateClient()({ url: ':memory:' });
  await client.executeMultiple(MEMORY_SCHEMA_SQL);
  return client;
}
```

**Native module loading gotcha:** `@libsql/client/sqlite3` uses platform-specific native binaries. In packaged Electron apps they live in `Resources/node_modules/`. CJS `require()` works but ESM `import()` does not (can't resolve from inside `app.asar`). Use lazy `createRequire(import.meta.url)` fallback.

---

## Embedding Service (`embedding-service.ts`)

Five-tier provider auto-detection:

| Priority | Provider | Condition |
|---|---|---|
| 1 | `openai` / `google` / `azure` / `voyage` | Cloud API key configured |
| 2 | `ollama-8b` | Ollama up + `qwen3-embedding:8b` + RAM > 32GB |
| 3 | `ollama-4b` | Ollama up + `qwen3-embedding:4b` |
| 4 | `ollama-0.6b` | Ollama up + `qwen3-embedding:0.6b` |
| 5 | `ollama-generic` | Any Ollama model with `embed`/`minilm`/`bge` in name |
| 6 | `none` | Hash-based pseudo-embeddings (no semantic similarity) |

**Key methods:**

```typescript
export class EmbeddingService {
  constructor(dbClient: Client, config?: EmbeddingConfig) {}

  async initialize(): Promise<void>
  getProvider(): EmbeddingProvider

  // dims: 256 for Stage 1 candidate gen, 1024 for storage
  async embed(text: string, dims: 256 | 1024 = 1024): Promise<number[]>
  async embedBatch(texts: string[], dims: 256 | 1024 = 1024): Promise<number[][]>
  async embedMemory(memory: Memory): Promise<number[]>    // uses contextual text
  async embedChunk(chunk: ASTChunk): Promise<number[]>    // uses contextual text
}
```

**Contextual text builders** (prepend file/type context before embedding):

```typescript
// For AST code chunks
export function buildContextualText(chunk: ASTChunk): string {
  const prefix = [
    `File: ${chunk.filePath}`,
    chunk.chunkType !== 'module' ? `${chunk.chunkType}: ${chunk.name ?? 'unknown'}` : null,
    `Lines: ${chunk.startLine}-${chunk.endLine}`,
  ].filter(Boolean).join(' | ');
  return `${prefix}\n\n${chunk.content}`;
}

// For Memory records
export function buildMemoryContextualText(memory: Memory): string {
  const parts = [
    memory.relatedFiles.length > 0 ? `Files: ${memory.relatedFiles.join(', ')}` : null,
    memory.relatedModules.length > 0 ? `Module: ${memory.relatedModules[0]}` : null,
    `Type: ${memory.type}`,
  ].filter(Boolean).join(' | ');
  return parts ? `${parts}\n\n${memory.content}` : memory.content;
}
```

**MRL (Matryoshka) truncation** — used for Ollama and Voyage models:

```typescript
function truncateToDim(embedding: number[], targetDim: number): number[] {
  if (embedding.length <= targetDim) return embedding;
  const slice = embedding.slice(0, targetDim);
  const norm = Math.sqrt(slice.reduce((s, v) => s + v * v, 0));
  if (norm === 0) return slice;
  return slice.map((v) => v / norm);  // L2-normalize per MRL spec
}
```

Embedding serialization: `Float32Array` stored as raw `BLOB` (little-endian, 4 bytes/float). `vector_distance_cos()` is the libSQL native function (no sqlite-vec extension needed).

---

## MemoryService Interface and Implementation

### Interface (`types.ts`)

```typescript
export interface MemoryService {
  store(entry: MemoryRecordEntry): Promise<string>;
  search(filters: MemorySearchFilters): Promise<Memory[]>;
  searchByPattern(pattern: string): Promise<Memory | null>;
  insertUserTaught(content: string, projectId: string, tags: string[]): Promise<string>;
  searchWorkflowRecipe(taskDescription: string, opts?: { limit?: number }): Promise<Memory[]>;
  updateAccessCount(memoryId: string): Promise<void>;
  deprecateMemory(memoryId: string): Promise<void>;
  verifyMemory(memoryId: string): Promise<void>;
  pinMemory(memoryId: string, pinned: boolean): Promise<void>;
  deleteMemory(memoryId: string): Promise<void>;
}
```

### Search Filters

```typescript
export interface MemorySearchFilters {
  query?: string;              // if present, uses retrieval pipeline
  types?: MemoryType[];
  sources?: MemorySource[];
  scope?: MemoryScope;
  relatedFiles?: string[];
  relatedModules?: string[];
  projectId?: string;
  phase?: UniversalPhase;
  minConfidence?: number;
  limit?: number;
  sort?: 'relevance' | 'recency' | 'confidence';
  excludeDeprecated?: boolean;
  filter?: (memory: Memory) => boolean;  // custom post-filter
}
```

### `store()` — Writes Three Tables Atomically

```typescript
async store(entry: MemoryRecordEntry): Promise<string> {
  const id = crypto.randomUUID();
  // Build contextual text, embed at 1024-dim
  const contextualText = buildMemoryContextualText(memoryForEmbedding);
  const embedding = await this.embeddingService.embed(contextualText, 1024);
  const embeddingBlob = Buffer.from(new Float32Array(embedding).buffer);

  await this.db.batch([
    { sql: `INSERT INTO memories (...) VALUES (...)`, args: [...] },
    { sql: `INSERT INTO memories_fts (memory_id, content, tags, related_files) VALUES (?, ?, ?, ?)`,
      args: [id, entry.content, (entry.tags ?? []).join(' '), (entry.relatedFiles ?? []).join(' ')] },
    { sql: `INSERT INTO memory_embeddings (memory_id, embedding, model_id, dims, created_at) VALUES (?, ?, ?, 1024, ?)`,
      args: [id, embeddingBlob, embeddingModelId, now] },
  ]);
  return id;
}
```

### `search()` — Pipeline vs Direct SQL

```typescript
async search(filters: MemorySearchFilters): Promise<Memory[]> {
  if (filters.query) {
    // Semantic search via retrieval pipeline
    const result = await this.retrievalPipeline.search(filters.query, {
      phase: filters.phase ?? 'explore',
      projectId: filters.projectId ?? '',
      maxResults: filters.limit ?? 8,
    });
    memories = result.memories;
  } else {
    // Direct SQL with structural filters (type, scope, source, confidence)
    memories = await this.directSearch(filters);
  }
  // Post-filter: minConfidence, excludeDeprecated, custom filter(), sort, limit
}
```

### `searchByPattern()` — BM25 Fast Lookup

```typescript
// Used by StepInjectionDecider for search_short_circuit trigger
async searchByPattern(pattern: string): Promise<Memory | null> {
  const results = await searchBM25(this.db, pattern, '', 1);
  if (results.length === 0) return null;
  const row = await this.db.execute({
    sql: 'SELECT * FROM memories WHERE id = ? AND deprecated = 0',
    args: [results[0].memoryId],
  });
  return rowToMemory(row.rows[0]);
}
```

---

## BM25 Search (`retrieval/bm25-search.ts`)

Uses SQLite FTS5 built-in BM25. FTS5 `bm25()` returns **negative** values — lower = better match.

```typescript
export async function searchBM25(
  db: Client,
  query: string,
  projectId: string,
  limit: number = 100,
): Promise<BM25Result[]> {
  const sanitizedQuery = sanitizeFtsQuery(query);
  const result = await db.execute({
    sql: `SELECT m.id, bm25(memories_fts) AS bm25_score
          FROM memories_fts
          JOIN memories m ON memories_fts.memory_id = m.id
          WHERE memories_fts MATCH ?
            AND m.project_id = ?
            AND m.deprecated = 0
          ORDER BY bm25_score   -- ascending = most negative first = best match
          LIMIT ?`,
    args: [sanitizedQuery, projectId, limit],
  });
  return result.rows.map(r => ({ memoryId: r.id as string, bm25Score: r.bm25_score as number }));
}

function sanitizeFtsQuery(query: string): string {
  const trimmed = query.trim();
  if (!trimmed) return '""';
  if (/^["(]/.test(trimmed)) return trimmed;       // already operator query
  if (/^[\w\s]+$/.test(trimmed)) return trimmed;   // simple word query
  const escaped = trimmed.replace(/"/g, '""');
  return `"${escaped}"`;                            // quote to prevent FTS5 parse errors
}
```

---

## Dense Vector Search (`retrieval/dense-search.ts`)

Primary: libSQL native `vector_distance_cos()`. Fallback: JS cosine similarity.

```typescript
export async function searchDense(
  db: Client,
  query: string,
  embeddingService: EmbeddingService,
  projectId: string,
  dims: 256 | 1024 = 256,  // 256 for fast candidate gen
  limit: number = 30,
): Promise<DenseResult[]> {
  const queryEmbedding = await embeddingService.embed(query, dims);
  try {
    const embeddingBlob = serializeEmbedding(queryEmbedding);
    const result = await db.execute({
      sql: `SELECT me.memory_id, vector_distance_cos(me.embedding, ?) AS distance
            FROM memory_embeddings me
            JOIN memories m ON me.memory_id = m.id
            WHERE m.project_id = ? AND m.deprecated = 0 AND me.dims = ?
            ORDER BY distance ASC
            LIMIT ?`,
      args: [embeddingBlob, projectId, dims, limit],
    });
    return result.rows.map(r => ({ memoryId: r.memory_id as string, distance: r.distance as number }));
  } catch {
    // JS-side fallback: fetch all embeddings, compute cosine in process
    return searchDenseJsFallback(db, queryEmbedding, projectId, dims, limit);
  }
}
```

Embedding serialization: `Float32Array` little-endian BLOB.

```typescript
function serializeEmbedding(embedding: number[]): Buffer {
  const buf = Buffer.allocUnsafe(embedding.length * 4);
  for (let i = 0; i < embedding.length; i++) buf.writeFloatLE(embedding[i], i * 4);
  return buf;
}
```

---

## Retrieval Pipeline (`retrieval/pipeline.ts`)

Four stages run on every semantic search:

### Stage 0: Query type classification

```typescript
export type QueryType = 'identifier' | 'semantic' | 'structural';

export const QUERY_TYPE_WEIGHTS: Record<QueryType, { fts: number; dense: number; graph: number }> = {
  identifier: { fts: 0.5, dense: 0.2, graph: 0.3 },  // camelCase / file paths / snake_case
  semantic:   { fts: 0.25, dense: 0.5, graph: 0.25 }, // natural language
  structural: { fts: 0.25, dense: 0.15, graph: 0.6 }, // after analyzeImpact/getDependencies
};

export function detectQueryType(query: string, recentToolCalls?: string[]): QueryType {
  if (/[a-z][A-Z]|_[a-z]/.test(query) || query.includes('/') || query.includes('.'))
    return 'identifier';
  if (recentToolCalls?.some(t => t === 'analyzeImpact' || t === 'getDependencies'))
    return 'structural';
  return 'semantic';
}
```

### Stage 1: Parallel candidate generation

```typescript
const [bm25Results, denseResults, graphResults] = await Promise.all([
  searchBM25(this.db, query, config.projectId, 20),
  searchDense(this.db, query, this.embeddingService, config.projectId, 256, 30), // 256-dim fast
  searchGraph(this.db, config.recentFiles ?? [], config.projectId, 15),
]);
```

### Stage 2a: Weighted RRF fusion

```typescript
// rrf-fusion.ts — score = weight / (k + rank + 1), k=60
export function weightedRRF(paths: RRFPath[], k: number = 60): RankedResult[] {
  const scores = new Map<string, { score: number; sources: Set<string> }>();
  for (const { results, weight, name } of paths) {
    results.forEach((r, rank) => {
      const contribution = weight / (k + rank + 1);
      const existing = scores.get(r.memoryId);
      if (existing) { existing.score += contribution; existing.sources.add(name); }
      else { scores.set(r.memoryId, { score: contribution, sources: new Set([name]) }); }
    });
  }
  return [...scores.entries()]
    .map(([memoryId, { score, sources }]) => ({ memoryId, score, sources }))
    .sort((a, b) => b.score - a.score);
}
```

### Stage 2b: Graph neighborhood boost

After RRF, boost candidates whose `related_files` overlap with 1-hop graph neighbors of the top-K results:

```typescript
// graph-boost.ts
const GRAPH_BOOST_FACTOR = 0.3;

export async function applyGraphNeighborhoodBoost(
  db: Client, rankedCandidates: RankedResult[], projectId: string, topK: number = 10,
): Promise<RankedResult[]> {
  // 1. Collect related_files from top-K results
  // 2. Query graph_closure for 1-hop file neighbors (depth=1)
  const neighbors = await db.execute({
    sql: `SELECT DISTINCT gn2.file_path
          FROM graph_closure gc
          JOIN graph_nodes gn ON gc.ancestor_id = gn.id
          JOIN graph_nodes gn2 ON gc.descendant_id = gn2.id
          WHERE gn.file_path IN (${filePlaceholders})
            AND gn.project_id = ? AND gc.depth = 1 AND gn2.file_path IS NOT NULL`,
    args: [...topFiles, projectId],
  });
  // 3. Boost = GRAPH_BOOST_FACTOR * (neighborOverlap / topFiles.length)
  // 4. Re-sort by boosted score
}
```

### Stage 3: Cross-encoder reranking

```typescript
// Reranker: Ollama qwen3-reranker:0.6b > Cohere rerank-v3.5 > passthrough
const reranked = await this.reranker.rerank(
  query,
  memories.map(m => ({
    memoryId: m.id,
    content: `[${m.type}] ${m.relatedFiles.join(', ')}: ${m.content}`,
  })),
  maxResults,  // default 8
);
```

**Reranker providers:**
- **Ollama:** Uses `qwen3-reranker:0.6b` with the ChatML relevance prompt format. Scores by L2 norm of the response embedding as a relevance proxy.
- **Cohere:** `rerank-v3.5` via REST API. ~$1/1K queries.
- **Passthrough:** Position-based scoring when neither is available.

**Qwen3 reranker prompt format:**
```
<|im_start|>system
Judge the relevance of the following document to the query. Answer "yes" if relevant, "no" if not.
<|im_end|>
<|im_start|>user
Query: {query}
Document: {document}
<|im_end|>
<|im_start|>assistant
<think>
```

### Stage 4: Phase-aware context packing

```typescript
// context-packer.ts
export function packContext(memories: Memory[], phase: UniversalPhase, config?: ContextPackingConfig): string

// Phase budgets (tokens):
// define: 2500  | workflow_recipe:30%, requirement:20%, decision:20%, dead_end:15%, task_calibration:10%
// implement: 3000 | gotcha:30%, error_pattern:25%, causal_dependency:15%, pattern:15%, dead_end:10%
// validate: 2500  | error_pattern:30%, requirement:25%, e2e_observation:25%, work_unit_outcome:15%
// refine: 2000    | error_pattern:35%, gotcha:25%, dead_end:20%, pattern:15%
// explore: 2000   | module_insight:40%, decision:25%, pattern:20%, causal_dependency:15%
// reflect: 1500   | work_unit_outcome:40%, task_calibration:35%, dead_end:15%
```

**MMR diversity filter** — Jaccard similarity > 0.85 triggers skip:

```typescript
function isTooSimilar(content: string, included: string[]): boolean {
  const newWords = new Set(tokenize(content));
  for (const existingContent of included) {
    const existingWords = new Set(tokenize(existingContent));
    const intersection = [...newWords].filter(w => existingWords.has(w)).length;
    const union = new Set([...newWords, ...existingWords]).size;
    if (intersection / union > 0.85) return true;
  }
  return false;
}
```

Output format: `## Relevant Context from Memory\n\n**{TypeLabel}** (file1, file2)\n{content}`

### Full pipeline call

```typescript
// From RetrievalPipeline.search():
async search(query: string, config: RetrievalConfig): Promise<RetrievalResult> {
  // config = { phase, projectId, recentFiles?, recentToolCalls?, maxResults? }
  // Returns: { memories: Memory[], formattedContext: string }
}
```

### HyDE fallback (`retrieval/hyde.ts`)

For underspecified queries, generate a hypothetical memory and embed that instead:

```typescript
export async function hydeSearch(
  query: string,
  embeddingService: EmbeddingService,
  model: LanguageModel,
): Promise<number[]> {
  const { text } = await generateText({
    model,
    prompt: `Write a 2-sentence memory entry that would perfectly answer this query: "${query}"...`,
    maxOutputTokens: 100,
  });
  return embeddingService.embed(text.trim() || query, 1024);
}
```

---

## Graph Search (`retrieval/graph-search.ts`)

Three sub-paths based on recently-accessed files:

```typescript
export async function searchGraph(
  db: Client, recentFiles: string[], projectId: string, limit: number = 15,
): Promise<GraphSearchResult[]>

// Sub-path 1: File-scoped (score=0.8) — memories directly tagged with related_files
// Uses json_each(m.related_files) to find overlap with recentFiles

// Sub-path 2: Co-access (score=weight*0.7) — files frequently co-accessed with recent files
// Queries observer_co_access_edges WHERE weight > 0.3

// Sub-path 3: Closure neighbors (score=0.6) — structural 1-hop dependencies
// Queries graph_closure WHERE depth=1 via ancestor graph_nodes
```

---

## Memory Injection

### Session-start injection (Planner) (`injection/planner-memory-context.ts`)

```typescript
export async function buildPlannerMemoryContext(
  taskDescription: string,
  relevantModules: string[],
  memoryService: MemoryService,
  projectId: string,
): Promise<string> {
  // Fetches in parallel: task_calibrations, dead_ends, causal_deps, work_unit_outcomes, workflow_recipes
  const [calibrations, deadEnds, causalDeps, outcomes, recipes] = await Promise.all([...]);

  // Returns formatted block:
  // === MEMORY CONTEXT FOR PLANNER ===
  // WORKFLOW RECIPES — Proven approaches for similar tasks: ...
  // TASK CALIBRATIONS — Historical step count data: ...
  // DEAD ENDS — Approaches that have failed before: ...
  // CAUSAL DEPENDENCIES — Known ordering constraints: ...
  // RECENT OUTCOMES — What happened in similar past work: ...
  // === END MEMORY CONTEXT ===
}
```

### Per-step injection (prepareStep callback) (`injection/step-injection-decider.ts`)

Called via `WorkerObserverProxy.requestStepInjection()` from the runner's `prepareStep` hook.

**Three triggers:**

```typescript
export class StepInjectionDecider {
  async decide(
    stepNumber: number,
    recentContext: RecentToolCallContext,
  ): Promise<StepInjection | null> {

    // Trigger 1: Gotcha injection
    // Agent just Read/Edited a file → search for unseen gotchas/error_patterns/dead_ends
    // related to those files (minConfidence: 0.65, limit: 4)
    // → type: 'gotcha_injection'
    // Format: "MEMORY ALERT — Gotchas for files you just accessed:\n- [type](file): content"

    // Trigger 2: Scratchpad reflection
    // New AcuteCandidate recorded since last step
    // → type: 'scratchpad_reflection'
    // Format: "MEMORY REFLECTION — New observations recorded this step:\n- [step N] signal: text"

    // Trigger 3: Search short-circuit
    // Agent recently used Grep/Glob → check searchByPattern() for known matches
    // → type: 'search_short_circuit'
    // Format: "MEMORY CONTEXT: {content}"
  }
}

export interface StepInjection {
  content: string;
  type: 'gotcha_injection' | 'scratchpad_reflection' | 'search_short_circuit';
  memoryIds: string[];
}
```

**Step state tracker** (`injection/step-memory-state.ts`):

```typescript
export class StepMemoryState {
  recordToolCall(toolName: string, args: Record<string, unknown>): void
  markInjected(memoryIds: string[]): void
  getRecentContext(windowSize = 5): RecentToolCallContext  // rolling 20-call window
  reset(): void
}
```

### Memory-aware stop condition (`injection/memory-stop-condition.ts`)

Adjusts `stopWhen` based on historical `task_calibration` memories:

```typescript
export function buildMemoryAwareStopCondition(
  baseMaxSteps: number,
  calibrationFactor: number | undefined,
) {
  const factor = Math.min(calibrationFactor ?? 1.0, 2.0); // cap at 2x
  const adjusted = Math.min(Math.ceil(baseMaxSteps * factor), 2000);
  return stepCountIs(adjusted);
}

export async function getCalibrationFactor(
  memoryService: MemoryService, modules: string[], projectId: string,
): Promise<number | undefined> {
  // Fetches task_calibration memories, parses JSON content for { ratio: number }
  // Returns average ratio across all matching calibrations
}
```

---

## Observer Pattern (`observer/memory-observer.ts`)

Passive behavioral observation. Runs on the main thread. All logic is synchronous and must complete in < 2ms.

```typescript
export class MemoryObserver {
  constructor(sessionId: string, sessionType: SessionType, projectId: string) {}

  // Called for every IPC message from worker thread — MUST complete in <2ms, NEVER awaits
  observe(message: MemoryIpcRequest): void

  getNewCandidatesSince(stepNumber: number): AcuteCandidate[]

  // Called AFTER session completes — may be slow
  async finalize(outcome: SessionOutcome): Promise<MemoryCandidate[]>
}
```

**Observable signals detected:**

| Signal | Detection method |
|---|---|
| `co_access` | Files accessed within 5-step window in same session |
| `error_retry` | Same error fingerprint occurring >= 2 times |
| `self_correction` | Regex patterns in reasoning text (e.g. `Actually, ... not ...`) |
| `backtrack` | Dead-end language in reasoning (e.g. `this approach won't work`) |
| `repeated_grep` | Same grep pattern used >= 3 times |

**Finalization** builds `MemoryCandidate[]` and applies trust gate + promotion limit per session type.

### Scratchpad (`observer/scratchpad.ts`)

In-memory accumulator for a session. Never writes to DB during execution.

```typescript
export class Scratchpad {
  // Key analytics tracked (all O(1)):
  analytics: ScratchpadAnalytics = {
    fileAccessCounts: Map<string, number>
    intraSessionCoAccess: Map<string, Set<string>>  // within 5-step window
    grepPatternCounts: Map<string, number>
    errorFingerprints: Map<string, number>           // fingerprinted with sha256 + normalization
    configFilesTouched: Set<string>
    selfCorrectionCount: number
    recentToolSequence: string[]   // circular buffer, last 8
    totalInputTokens: number
    peakContextTokens: number
  }
  acuteCandidates: AcuteCandidate[]  // backtrack / self_correction events

  // Crash recovery
  async checkpoint(workUnitRef: WorkUnitRef, dbClient: Client): Promise<void>
  static async restore(sessionId: string, dbClient: Client): Promise<Scratchpad | null>
}
```

Error fingerprinting:

```typescript
export function computeErrorFingerprint(errorMessage: string): string {
  const normalized = errorMessage
    .replace(/\/[^\s:'"]+/g, '<path>')      // strip file paths
    .replace(/:\d+(:\d+)?/g, '')            // strip line/col numbers
    .replace(/<uuid pattern>/gi, '<uuid>')  // strip UUIDs
    .replace(/\d{4}-\d{2}-\d{2}T.../g, '<ts>')  // strip timestamps
    .trim().toLowerCase();
  return createHash('sha256').update(normalized).digest('hex').slice(0, 16);
}
```

### Trust Gate (`observer/trust-gate.ts`)

Anti-injection defense — any signal derived after a `WebFetch`/`WebSearch` call is flagged:

```typescript
export function applyTrustGate(candidate: MemoryCandidate, externalToolCallStep: number | undefined): MemoryCandidate {
  if (externalToolCallStep !== undefined && candidate.originatingStep > externalToolCallStep) {
    return { ...candidate, needsReview: true, confidence: candidate.confidence * 0.7,
      trustFlags: { contaminated: true, contaminationSource: 'web_fetch' } };
  }
  return candidate;
}
```

### Promotion Pipeline (`observer/promotion.ts`)

8-stage filter applied to raw candidates after session completion:

```
Stage 1: Validation filter   — drop non-dead_end from failed sessions
Stage 2: Frequency filter    — require minSessions per signal class (from SIGNAL_VALUES)
Stage 3: Novelty filter      — drop confidence < 0.2 (placeholder; full version needs cosine similarity)
Stage 4: Trust gate          — contamination check
Stage 5: Scoring             — priority = candidatePriority*0.6 + signalScore*0.4
Stage 6: LLM synthesis       — single generateText call (caller's responsibility)
Stage 7: Embedding           — batch embed (caller's responsibility)
Stage 8: DB write            — single transaction (caller's responsibility)
```

**Session promotion limits:**

```typescript
export const SESSION_TYPE_PROMOTION_LIMITS: Record<SessionType, number> = {
  build: 20, insights: 5, roadmap: 3, terminal: 3,
  changelog: 0, spec_creation: 3, pr_review: 8,
};
```

**Signal values** (score formula: `diagnostic_value*0.5 + cross_session_relevance*0.3 + (1-false_positive_rate)*0.2`):

```typescript
export const SIGNAL_VALUES: Record<SignalType, SignalValueEntry> = {
  co_access:         { score: 0.91, promotesTo: ['causal_dependency', 'prefetch_pattern'], minSessions: 3 },
  self_correction:   { score: 0.88, promotesTo: ['gotcha', 'module_insight'],              minSessions: 1 },
  error_retry:       { score: 0.85, promotesTo: ['error_pattern', 'gotcha'],               minSessions: 2 },
  parallel_conflict: { score: 0.82, promotesTo: ['gotcha'],                                minSessions: 1 },
  read_abandon:      { score: 0.79, promotesTo: ['gotcha'],                                minSessions: 3 },
  repeated_grep:     { score: 0.76, promotesTo: ['module_insight', 'gotcha'],              minSessions: 2 },
  test_order:        { score: 0.74, promotesTo: ['task_calibration'],                      minSessions: 3 },
  tool_sequence:     { score: 0.73, promotesTo: ['workflow_recipe'],                       minSessions: 3 },
  file_access:       { score: 0.72, promotesTo: ['prefetch_pattern'],                      minSessions: 3 },
  step_overrun:      { score: 0.71, promotesTo: ['task_calibration'],                      minSessions: 3 },
  backtrack:         { score: 0.68, promotesTo: ['gotcha'],                                minSessions: 2 },
  config_touch:      { score: 0.66, promotesTo: ['causal_dependency'],                     minSessions: 2 },
  // ... (glob_ignore, context_token_spike, external_reference, import_chase, time_anomaly)
};
```

---

## Agent Tools

### `record_memory` tool (`tools/record-memory.ts`)

```typescript
export function createRecordMemoryTool(
  proxy: WorkerObserverProxy,
  projectId: string,
  sessionId: string,
): AITool<RecordMemoryInput, string>

// Input schema (Zod):
// type: enum ['gotcha','decision','pattern','error_pattern','module_insight','dead_end','causal_dependency','requirement']
// content: string (min 10, max 500)
// relatedFiles?: string[]
// relatedModules?: string[]
// confidence?: number (0-1, default 0.8)

// Creates entry: { source: 'agent_explicit', scope: 'module', needsReview: false }
// Posts to main thread via proxy.recordMemory(entry)
// Returns: "Memory recorded (id: {id.slice(0,8)}): {content}"
```

### `search_memory` tool (`tools/search-memory.ts`)

```typescript
export function createSearchMemoryTool(
  proxy: WorkerObserverProxy,
  projectId: string,
): AITool<SearchMemoryInput, string>

// Input schema:
// query: string
// types?: MemoryType[]
// relatedFiles?: string[]
// limit?: number (1-20, default 5)

// Always sets: { excludeDeprecated: true }
// Returns formatted string: "Memory search results for '{query}':\n\n1. [type][file] (confidence: XX%)\n   {content}"
```

---

## Worker-Main Thread IPC (`ipc/worker-observer-proxy.ts`)

All DB operations are proxied from worker thread to main thread via `MessagePort`.

```typescript
export class WorkerObserverProxy {
  constructor(port: MessagePort) {}

  // Fire-and-forget (synchronous, no response needed)
  onToolCall(toolName: string, args: Record<string, unknown>, stepNumber: number): void
  onToolResult(toolName: string, result: unknown, stepNumber: number): void
  onReasoning(text: string, stepNumber: number): void
  onStepComplete(stepNumber: number): void

  // Request/response with 3-second timeout — returns fallback on timeout
  async searchMemory(filters: MemorySearchFilters): Promise<Memory[]>
  async recordMemory(entry: MemoryRecordEntry): Promise<string | null>
  async requestStepInjection(stepNumber: number, recentContext: RecentToolCallContext): Promise<StepInjection | null>
}
```

IPC message flow:

```
Worker thread                              Main thread
─────────────────────────────────────────────────────
port.postMessage({type:'memory:tool-call'}) → MemoryObserver.observe()
port.postMessage({type:'memory:search',    → MemoryService.search()
  requestId, filters})                       port.postMessage({type:'memory:search-result', ...})
port.postMessage({type:'memory:record',    → MemoryService.store()
  requestId, entry})                         port.postMessage({type:'memory:stored', ...})
port.postMessage({type:'memory:step-      → StepInjectionDecider.decide()
  injection-request', requestId, ...})       port.postMessage({type:'memory:stored', id: JSON.stringify(injection)})
```

---

## Service Factory (`ipc-handlers/context/memory-service-factory.ts`)

Singleton initialization:

```typescript
export async function getMemoryService(): Promise<MemoryServiceImpl> {
  if (_instance) return _instance;
  _initPromise = (async () => {
    const db = await getMemoryClient();                          // local libSQL file
    const embeddingService = new EmbeddingService(db, buildEmbeddingConfig());
    await embeddingService.initialize();                         // auto-detect provider
    const reranker = new Reranker();
    await reranker.initialize();                                 // auto-detect provider
    const pipeline = new RetrievalPipeline(db, embeddingService, reranker);
    return new MemoryServiceImpl(db, embeddingService, pipeline);
  })();
  return _initPromise;
}
```

---

## Key Design Decisions

1. **libSQL everywhere** — same `@libsql/client` API works for local file, embedded replica with Turso sync, and pure cloud. No separate SQLite vs Turso codepaths.

2. **FTS5 BM25, not Tantivy** — libSQL exposes FTS5 natively. All modes use it. `bm25()` returns negative floats (ascending order = best match first).

3. **256-dim for Stage 1 candidate gen, 1024-dim for storage** — Qwen3 MRL models support this; truncation is L2-normalized. Faster candidate generation, full precision for final storage.

4. **Embeddings as raw BLOBs** — `Float32Array` little-endian, 4 bytes/float. `vector_distance_cos()` is the libSQL native cosine function. JS fallback fetches all BLOBs and computes in-process.

5. **Observer never writes DB** — all observation is synchronous, O(1), in-memory (Scratchpad). Promotion and DB writes only happen after session finalization.

6. **Trust gate** — memories captured after a WebFetch/WebSearch call are flagged `needsReview=true` and confidence reduced by 30% to defend against prompt injection.

7. **Injection via prepareStep callback** — three triggers: gotcha files, scratchpad reflection, search short-circuit. 50ms budget. Never disrupts the agent loop on failure.

8. **Application-side RRF** — no SQL `FULL OUTER JOIN`. All fusion is done in JS after parallel query results are collected.

9. **Phase-aware context packing** — per-phase token budgets and per-type allocation ratios prevent context bloat. Jaccard-based MMR dedup skips near-duplicate memories.

10. **Three graph search sub-paths** — file-scoped (0.8), co-access edges (weight×0.7), closure neighbor (0.6). Graph neighborhood boost promotes structurally-related memories that score poorly on text similarity.
