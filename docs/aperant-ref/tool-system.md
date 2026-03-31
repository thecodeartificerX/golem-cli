# Aperant Tool System — Reference Implementation

Source project: `F:\Tools\External\Aperant\apps\desktop\src\main\ai`

---

## 1. Core Types

**File:** `tools/types.ts`

### ToolContext

Every tool execution receives a `ToolContext`. It carries filesystem paths,
the security profile, an optional abort signal, and (critically) an
`allowedWritePaths` list that enforces write-path containment.

```ts
export interface ToolContext {
  cwd: string;               // CWD for the agent
  projectDir: string;        // Root of the project being worked on
  specDir: string;           // Spec dir, e.g. .auto-claude/specs/001-feature/
  securityProfile: SecurityProfile;
  abortSignal?: AbortSignal;
  allowedWritePaths?: string[]; // Write/Edit tools may only touch these dirs
}
```

### ToolPermission

```ts
export const ToolPermission = {
  Auto: 'auto',                          // runs without approval
  RequiresApproval: 'requires_approval', // needs user sign-off
  ReadOnly: 'read_only',                 // safe, no approval, skips security hooks
} as const;
export type ToolPermission = (typeof ToolPermission)[keyof typeof ToolPermission];
```

### ToolMetadata

```ts
export interface ToolMetadata {
  name: string;                       // e.g. 'Read', 'Bash', 'Glob'
  description: string;                // shown to the LLM
  permission: ToolPermission;
  executionOptions: ToolExecutionOptions;
}
```

### ToolExecutionOptions + Default

```ts
export interface ToolExecutionOptions {
  timeoutMs: number;      // 0 = no timeout
  allowBackground: boolean;
}

export const DEFAULT_EXECUTION_OPTIONS: ToolExecutionOptions = {
  timeoutMs: 120_000,
  allowBackground: false,
};
```

### ToolDefinitionConfig

```ts
export interface ToolDefinitionConfig<
  TInput extends z.ZodType = z.ZodType,
  TOutput = unknown,
> {
  metadata: ToolMetadata;
  inputSchema: TInput;   // Zod v3 schema
  execute: (
    input: z.infer<TInput>,
    context: ToolContext,
  ) => Promise<TOutput> | TOutput;
}
```

---

## 2. Tool Definition Wrapper (`Tool.define`)

**File:** `tools/define.ts`

### DefinedTool interface

```ts
export interface DefinedTool<
  TInput extends z.ZodType = z.ZodType,
  TOutput = unknown,
> {
  metadata: ToolMetadata;
  bind: (context: ToolContext) => AITool<z.infer<TInput>, TOutput>;
  config: ToolDefinitionConfig<TInput, TOutput>;
}
```

### `define()` — the factory

```ts
function define<TInput extends z.ZodType, TOutput>(
  config: ToolDefinitionConfig<TInput, TOutput>,
): DefinedTool<TInput, TOutput>
```

`define()` returns a `DefinedTool`. The tool is not bound to a context yet —
binding happens at call-site via `.bind(context)`.

### `.bind(context)` — what happens inside

When `.bind(context)` is called, an `executeWithHooks` wrapper is produced and
passed to the Vercel AI SDK `tool()` factory. The wrapper performs four steps
in order:

1. **File-path sanitization** — strips trailing JSON artifact characters
   (`'"}\],{`) from `input.file_path`. Some models (e.g., gpt-5.3-codex) leak
   these into string arguments.
2. **Security hooks** — runs `bashSecurityHook` for every non-ReadOnly tool.
   Throws if the hook returns a denial.
3. **Write-path containment** — for non-ReadOnly tools that have a `file_path`
   in their input, resolves it with `node:path resolve()` and checks whether it
   starts with any of `context.allowedWritePaths`. Throws if not.
4. **Safety-net truncation** — if the tool result is a string, applies
   `truncateToolOutput(..., SAFETY_NET_MAX_BYTES)` to prevent context overflow.

```ts
bind(context: ToolContext): AITool<z.infer<TInput>, TOutput> {
  const executeWithHooks = async (input: Input): Promise<TOutput> => {
    // Step 1: strip trailing JSON artifact chars from file_path
    sanitizeFilePathArg(input as Record<string, unknown>);

    // Step 2: security hooks (skip for ReadOnly)
    if (metadata.permission !== ToolPermission.ReadOnly) {
      runSecurityHooks(metadata.name, input as Record<string, unknown>, context);
    }

    // Step 3: write-path containment (non-ReadOnly + has file_path)
    if (context.allowedWritePaths?.length && metadata.permission !== ToolPermission.ReadOnly) {
      const writePath = (input as Record<string, unknown>).file_path as string | undefined;
      if (writePath) {
        const resolved = resolve(writePath);
        const allowed = context.allowedWritePaths.some(
          dir => resolved.startsWith(resolve(dir))
        );
        if (!allowed) {
          throw new Error(
            `Write denied: ${metadata.name} cannot write to ${writePath}. ` +
            `Allowed directories: ${context.allowedWritePaths.join(', ')}`,
          );
        }
      }
    }

    const result = await (execute(input as z.infer<TInput>, context) as Promise<TOutput>);

    // Step 4: safety-net truncation for string results
    if (typeof result === 'string') {
      const truncated = truncateToolOutput(result, metadata.name, context.projectDir, SAFETY_NET_MAX_BYTES);
      return truncated.content as TOutput;
    }
    return result;
  };

  return tool({
    description: metadata.description,
    inputSchema: inputSchema as any,
    execute: executeWithHooks as any,
  }) as AITool<Input, TOutput>;
}
```

### Security hook wiring

```ts
function runSecurityHooks(
  toolName: string,
  input: Record<string, unknown>,
  context: ToolContext,
): void {
  const result = bashSecurityHook(
    { toolName, toolInput: input, cwd: context.cwd },
    context.securityProfile,
  );
  if ('hookSpecificOutput' in result) {
    const reason = result.hookSpecificOutput.permissionDecisionReason;
    throw new Error(`Security hook denied ${toolName}: ${reason}`);
  }
}
```

### File-path artifact sanitization

```ts
const TRAILING_JSON_ARTIFACT_RE = /['"}\],{]+$/;

export function sanitizeFilePathArg(input: Record<string, unknown>): void {
  const filePath = input.file_path;
  if (typeof filePath !== 'string') return;
  const cleaned = filePath.replace(TRAILING_JSON_ARTIFACT_RE, '');
  if (cleaned !== filePath) {
    input.file_path = cleaned;
  }
}
```

Exported for unit testing. Mutates `input` in place.

### Exported namespace

```ts
export const Tool = { define } as const;
```

Usage:

```ts
import { Tool } from './define';

const myTool = Tool.define({
  metadata: { name: 'MyTool', description: '...', permission: ToolPermission.ReadOnly, executionOptions: DEFAULT_EXECUTION_OPTIONS },
  inputSchema: z.object({ file_path: z.string() }),
  execute: async (input, ctx) => { /* use ctx.cwd, ctx.projectDir, etc. */ },
});

// Later, at agent startup:
const aiTool = myTool.bind(toolContext);
```

---

## 3. Tool Registry

**File:** `tools/registry.ts`

### ToolRegistry class

```ts
export class ToolRegistry {
  private readonly tools = new Map<string, DefinedTool>();

  registerTool(name: string, definedTool: DefinedTool): void {
    this.tools.set(name, definedTool);
  }

  getTool(name: string): DefinedTool | undefined {
    return this.tools.get(name);
  }

  getRegisteredNames(): string[] {
    return Array.from(this.tools.keys());
  }

  /**
   * Returns a Record<string, AITool> filtered to the tools allowed by
   * AGENT_CONFIGS[agentType], with each tool bound to the provided context.
   * Pass the result directly to streamText/generateText `tools` param.
   */
  getToolsForAgent(
    agentType: AgentType,
    context: ToolContext,
  ): Record<string, AITool> {
    const config = getAgentConfig(agentType);
    const allowedNames = new Set(config.tools);
    const result: Record<string, AITool> = {};

    for (const [name, definedTool] of Array.from(this.tools.entries())) {
      if (allowedNames.has(name)) {
        result[name] = definedTool.bind(context);
      }
    }

    return result;
  }
}
```

### Tool name constants (re-exported from `agent-configs`)

```ts
export const BASE_READ_TOOLS  = ['Read', 'Glob', 'Grep'] as const;
export const BASE_WRITE_TOOLS = ['Write', 'Edit', 'Bash'] as const;
export const WEB_TOOLS        = ['WebFetch', 'WebSearch'] as const;

// MCP tool names (prefixed mcp__<server>__<name>)
export const TOOL_UPDATE_SUBTASK_STATUS = 'mcp__auto-claude__update_subtask_status';
export const TOOL_GET_BUILD_PROGRESS    = 'mcp__auto-claude__get_build_progress';
export const TOOL_RECORD_DISCOVERY      = 'mcp__auto-claude__record_discovery';
export const TOOL_RECORD_GOTCHA         = 'mcp__auto-claude__record_gotcha';
export const TOOL_GET_SESSION_CONTEXT   = 'mcp__auto-claude__get_session_context';
export const TOOL_UPDATE_QA_STATUS      = 'mcp__auto-claude__update_qa_status';
```

### `getRequiredMcpServers` — dynamic server selection for an agent

```ts
export function getRequiredMcpServers(
  agentType: AgentType,
  options: {
    projectCapabilities?: ProjectCapabilities;
    linearEnabled?: boolean;
    memoryEnabled?: boolean;
    graphitiEnabled?: boolean; // @deprecated — use memoryEnabled
    mcpConfig?: McpConfig;
  } = {},
): string[]
```

Logic summary:
- Starts from `AGENT_CONFIGS[agentType].mcpServers`.
- Removes `context7` if `mcpConfig.CONTEXT7_ENABLED === 'false'`.
- Adds `linear` if in `mcpServersOptional` AND `linearEnabled && LINEAR_MCP_ENABLED !== 'false'`.
- Resolves the abstract `browser` entry to `electron` (if `is_electron` and
  `ELECTRON_MCP_ENABLED === 'true'`) or `puppeteer` (if `is_web_frontend &&
  PUPPETEER_MCP_ENABLED === 'true'`).
- Removes `memory` if `!memoryEnabled`.
- Applies per-agent `AGENT_MCP_<agentType>_ADD` / `AGENT_MCP_<agentType>_REMOVE`
  overrides from `mcpConfig`, resolved through `mapMcpServerName()`.
- `auto-claude` is protected and cannot be removed via the REMOVE override.

### McpConfig / ProjectCapabilities

```ts
export interface McpConfig {
  CONTEXT7_ENABLED?: string;
  LINEAR_MCP_ENABLED?: string;
  ELECTRON_MCP_ENABLED?: string;
  PUPPETEER_MCP_ENABLED?: string;
  CUSTOM_MCP_SERVERS?: Array<{ id: string }>;
  [key: string]: unknown; // AGENT_MCP_<type>_ADD / AGENT_MCP_<type>_REMOVE
}

export interface ProjectCapabilities {
  is_electron?: boolean;
  is_web_frontend?: boolean;
}
```

---

## 4. Build Tool Registry

**File:** `tools/build-registry.ts`

Single factory that creates and populates the registry with all builtin tools.
Used by worker threads, runners, and the client factory.

```ts
export function buildToolRegistry(): ToolRegistry {
  const registry = new ToolRegistry();
  registry.registerTool('Read',           asDefined(readTool));
  registry.registerTool('Write',          asDefined(writeTool));
  registry.registerTool('Edit',           asDefined(editTool));
  registry.registerTool('Bash',           asDefined(bashTool));
  registry.registerTool('Glob',           asDefined(globTool));
  registry.registerTool('Grep',           asDefined(grepTool));
  registry.registerTool('WebFetch',       asDefined(webFetchTool));
  registry.registerTool('WebSearch',      asDefined(webSearchTool));
  registry.registerTool('SpawnSubagent',  asDefined(spawnSubagentTool));
  return registry;
}
```

Each builtin lives in `tools/builtin/<name>.ts` and is defined with
`Tool.define(...)`. The `asDefined` cast works around the generic type mismatch
between `DefinedTool<specific>` and the `DefinedTool` (open generic) that the
registry stores.

---

## 5. MCP Server Registry

**File:** `mcp/registry.ts`

### McpRegistryOptions

```ts
export interface McpRegistryOptions {
  specDir?: string;         // passed to auto-claude server as SPEC_DIR env var
  memoryMcpUrl?: string;    // HTTP URL for memory/Graphiti sidecar
  linearApiKey?: string;    // injected into linear server process env
  env?: Record<string, string>; // fallback env for any server
}
```

### Static server configs

All `McpServerConfig` objects have shape:

```ts
// from mcp/types.ts (not read directly but inferred from usage)
interface McpServerConfig {
  id: string;
  name: string;
  description: string;
  enabledByDefault: boolean;
  transport: StdioTransportConfig | StreamableHttpTransportConfig;
}
```

| Server ID     | Transport    | Command / URL                                   | Default |
|---------------|--------------|--------------------------------------------------|---------|
| `context7`    | stdio        | `npx -y @upstash/context7-mcp@latest`            | true    |
| `linear`      | stdio        | `npx -y @linear/mcp-server`                      | false   |
| `memory`      | streamable-http | `options.memoryMcpUrl` (Graphiti sidecar)     | false   |
| `electron`    | stdio        | `npx -y electron-mcp-server`                     | false   |
| `puppeteer`   | stdio        | `npx -y @anthropic-ai/puppeteer-mcp-server`      | false   |
| `auto-claude` | stdio        | `node auto-claude-mcp-server.js` + `SPEC_DIR`    | true    |

### `auto-claude` factory (env injection pattern)

```ts
function createAutoClaudeServer(specDir: string): McpServerConfig {
  return {
    id: 'auto-claude',
    name: 'Aperant',
    description: 'Build management tools (progress tracking, session context)',
    enabledByDefault: true,
    transport: {
      type: 'stdio',
      command: 'node',
      args: ['auto-claude-mcp-server.js'],
      env: { SPEC_DIR: specDir },   // <-- context injected via env var
    },
  };
}
```

### `memory` factory (dynamic URL)

```ts
function createMemoryServer(url: string): McpServerConfig {
  return {
    id: 'memory',
    name: 'Memory',
    description: 'Knowledge graph memory for cross-session insights',
    enabledByDefault: false,
    transport: { type: 'streamable-http', url },
  };
}
```

### `getMcpServerConfig` — switch-based resolver

```ts
export function getMcpServerConfig(
  serverId: McpServerId | string,
  options: McpRegistryOptions = {},
): McpServerConfig | null
```

- Returns `null` (not an error) for unrecognized IDs or when required
  credentials are absent (`linear` without `LINEAR_API_KEY`, `memory` without
  a URL). Callers filter nulls out.
- `linear` spreads the base config and patches the transport env with the API
  key so the server process receives it.

### `resolveMcpServers` — list to configs

```ts
export function resolveMcpServers(
  serverIds: string[],
  options: McpRegistryOptions = {},
): McpServerConfig[]
```

Iterates `serverIds`, calls `getMcpServerConfig` per ID, skips nulls. Returns
only successfully resolved configs.

---

## 6. MCP Client

**File:** `mcp/client.ts`

### Transport creation

```ts
function createTransport(
  config: McpServerConfig,
): StdioClientTransport | { type: 'sse'; url: string; headers?: Record<string, string> }
```

- `stdio` → `new StdioClientTransport({ command, args, env: { ...process.env, ...config.env }, cwd })` from `@modelcontextprotocol/sdk/client/stdio.js`
- `streamable-http` → returns `{ type: 'sse', url, headers }` (the `@ai-sdk/mcp` SSE transport object)

### Single-server client

```ts
export async function createMcpClient(config: McpServerConfig): Promise<McpClientResult>
```

```ts
const client = await createMCPClient({ transport });  // from @ai-sdk/mcp
const tools  = await client.tools();
return {
  serverId: config.id,
  tools,                     // Record<string, AITool> — merge directly
  close: async () => { await client.close(); },
};
```

`McpClientResult` shape (inferred from usage):

```ts
interface McpClientResult {
  serverId: string;
  tools: Record<string, unknown>;  // AI SDK tools, keyed by tool name
  close: () => Promise<void>;
}
```

### Multi-server client for an agent

```ts
export async function createMcpClientsForAgent(
  agentType: AgentType,
  resolveOptions: McpServerResolveOptions = {},
  registryOptions: McpRegistryOptions = {},
): Promise<McpClientResult[]>
```

1. Calls `getRequiredMcpServers(agentType, resolveOptions)` → list of IDs.
2. Calls `resolveMcpServers(serverIds, registryOptions)` → list of configs.
3. `Promise.allSettled(configs.map(createMcpClient))` — parallel init.
4. Collects `fulfilled` results, silently skips `rejected` ones (failed MCP
   connections are non-fatal).

### Tool merging

```ts
export function mergeMcpTools(clients: McpClientResult[]): Record<string, unknown> {
  const merged: Record<string, unknown> = {};
  for (const client of clients) {
    Object.assign(merged, client.tools);
  }
  return merged;
}
```

Later keys win on collision (last-writer wins).

### Cleanup

```ts
export async function closeAllMcpClients(clients: McpClientResult[]): Promise<void> {
  await Promise.allSettled(clients.map((c) => c.close()));
}
```

---

## 7. Full Data-Flow Summary

```
buildToolRegistry()
  └─ ToolRegistry { tools: Map<name, DefinedTool> }

// At agent startup:
const registry     = buildToolRegistry();
const toolContext  = { cwd, projectDir, specDir, securityProfile, allowedWritePaths };
const builtinTools = registry.getToolsForAgent(agentType, toolContext);
// → Record<string, AITool>  (each DefinedTool.bind(context) called internally)

const mcpClients = await createMcpClientsForAgent(agentType, resolveOptions, { specDir, memoryMcpUrl, linearApiKey });
const mcpTools   = mergeMcpTools(mcpClients);
// → Record<string, AITool>  (tool names are mcp__<serverId>__<name>)

const allTools = { ...builtinTools, ...mcpTools };

// Pass to Vercel AI SDK:
const result = await streamText({ model, messages, tools: allTools });

// Cleanup:
await closeAllMcpClients(mcpClients);
```

### Per-execution security chain (inside `.bind()`)

```
input arrives
  │
  ▼
sanitizeFilePathArg(input)           // strip trailing JSON artifact chars
  │
  ▼
[if !ReadOnly] runSecurityHooks()    // bashSecurityHook → throw on deny
  │
  ▼
[if !ReadOnly && allowedWritePaths]
  resolve(input.file_path)           // absolute path
  check against allowedWritePaths    // throw if outside
  │
  ▼
execute(input, context)              // actual tool logic
  │
  ▼
[if result is string]
  truncateToolOutput(result, ..., SAFETY_NET_MAX_BYTES)
  │
  ▼
return result
```

---

## 8. Key Design Decisions to Replicate

1. **DefinedTool is context-free until `.bind()`** — tools are defined once
   at module load time and bound to a fresh `ToolContext` for each agent
   session. Avoids stale references.

2. **`ToolPermission.ReadOnly` skips both security hooks and write-path
   containment** — the check is `permission !== ReadOnly`, so read-only tools
   (Read, Glob, Grep) never go through either guard.

3. **Write-path containment uses `node:path resolve()`** — both the candidate
   path and each allowed-dir are resolved before `startsWith` comparison. This
   handles relative paths and `..` traversal.

4. **`sanitizeFilePathArg` mutates in place** — no copy; the regex only fires
   if there is actually a match, so hot-path performance is unaffected.

5. **Failed MCP connections are non-fatal** — `Promise.allSettled` + filter
   means agents still run if an optional server (e.g., `linear`) fails to
   start.

6. **MCP tool names follow `mcp__<serverId>__<toolName>` convention** — this
   is the Vercel AI SDK naming scheme. Prompts must use the full prefixed name.
   The server ID is taken from `McpServerConfig.id`.

7. **`auto-claude` server receives `SPEC_DIR` via env** — the MCP server
   process reads this to locate `.auto-claude/specs/<id>/` at runtime; no
   hard-coding of paths in the binary.

8. **`ToolRegistry.getToolsForAgent` is the only call site that binds tools**
   — centralises the bind step; callers never call `definedTool.bind()` directly.
