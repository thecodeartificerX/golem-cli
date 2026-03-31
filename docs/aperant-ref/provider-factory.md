# Aperant: Provider Factory, Auth Resolver, and Client Factory

Reference extracted from `F:\Tools\External\Aperant\apps\desktop\src\main\ai\`.

---

## File Index

| File | Purpose |
|------|---------|
| `providers/types.ts` | `SupportedProvider` enum, `ProviderConfig` interface |
| `providers/factory.ts` | `createProvider()`, `detectProviderFromModel()`, `createProviderFromModelId()` |
| `providers/oauth-fetch.ts` | `createOAuthProviderFetch()`, `ensureValidOAuthToken()` |
| `auth/types.ts` | `ResolvedAuth`, `QueueResolvedAuth`, `AuthSource`, env var maps |
| `auth/resolver.ts` | `resolveAuth()`, `resolveAuthFromQueue()`, `buildDefaultQueueConfig()` |
| `auth/codex-oauth.ts` | Full PKCE OAuth flow for OpenAI Codex subscriptions |
| `client/types.ts` | `AgentClientConfig`, `AgentClientResult`, `SimpleClientConfig`, `SimpleClientResult` |
| `client/factory.ts` | `createAgentClient()`, `createSimpleClient()` |
| `config/types.ts` | `ThinkingLevel`, `THINKING_BUDGET_MAP`, `MODEL_PROVIDER_MAP`, `buildThinkingProviderOptions()` |

---

## 1. Provider Types

**File:** `providers/types.ts`

```typescript
export const SupportedProvider = {
  Anthropic: 'anthropic',
  OpenAI: 'openai',
  Google: 'google',
  Bedrock: 'bedrock',
  Azure: 'azure',
  Mistral: 'mistral',
  Groq: 'groq',
  XAI: 'xai',
  OpenRouter: 'openrouter',
  ZAI: 'zai',
  Ollama: 'ollama',
} as const;

export type SupportedProvider = (typeof SupportedProvider)[keyof typeof SupportedProvider];

export interface ProviderConfig {
  provider: SupportedProvider;
  apiKey?: string;
  baseURL?: string;
  region?: string;              // Bedrock only
  deploymentName?: string;      // Azure only
  headers?: Record<string, string>;
  oauthTokenFilePath?: string;  // Codex file-based OAuth
}
```

---

## 2. Provider Factory

**File:** `providers/factory.ts`

### OAuth Token Detection

Anthropic OAuth access tokens carry a distinguishable prefix from API keys:

```typescript
function isOAuthToken(token: string | undefined): boolean {
  if (!token) return false;
  return token.startsWith('sk-ant-oa') || token.startsWith('sk-ant-ort');
}
```

### Per-Provider Instance Creation

Each provider has different constructor signatures and auth mechanisms:

```typescript
function createProviderInstance(config: ProviderConfig) {
  const { provider, apiKey, baseURL, headers } = config;

  switch (provider) {
    case SupportedProvider.Anthropic: {
      // OAuth tokens: use authToken (Authorization: Bearer) + required beta headers
      // API keys:     use apiKey (x-api-key header)
      if (isOAuthToken(apiKey)) {
        return createAnthropic({
          authToken: apiKey,
          baseURL,
          headers: {
            ...headers,
            'anthropic-beta': 'claude-code-20250219,oauth-2025-04-20,interleaved-thinking-2025-05-14',
          },
        });
      }
      return createAnthropic({ apiKey, baseURL, headers });
    }

    case SupportedProvider.OpenAI: {
      // File-based OAuth: use generic fetch interceptor for token injection + URL rewriting
      if (config.oauthTokenFilePath) {
        return createOpenAI({
          apiKey: apiKey ?? 'codex-oauth-placeholder',
          baseURL,
          headers,
          fetch: createOAuthProviderFetch(config.oauthTokenFilePath, 'openai'),
        });
      }
      return createOpenAI({ apiKey, baseURL, headers });
    }

    case SupportedProvider.Google:
      return createGoogleGenerativeAI({ apiKey, baseURL, headers });

    case SupportedProvider.Bedrock:
      return createAmazonBedrock({ region: config.region ?? 'us-east-1', apiKey });

    case SupportedProvider.Azure:
      return createAzure({ apiKey, baseURL, headers });

    case SupportedProvider.Mistral:
      return createMistral({ apiKey, baseURL, headers });

    case SupportedProvider.Groq:
      return createGroq({ apiKey, baseURL, headers });

    case SupportedProvider.XAI:
      return createXai({ apiKey, baseURL, headers });

    case SupportedProvider.OpenRouter:
      return createOpenRouter({ apiKey });

    case SupportedProvider.ZAI:
      return createOpenAICompatible({
        name: 'zai',
        apiKey,
        baseURL: baseURL ?? 'https://api.z.ai/api/paas/v4',
        headers,
      });

    case SupportedProvider.Ollama: {
      // Ensure /v1 suffix on base URL
      let ollamaBaseURL = baseURL ?? 'http://localhost:11434';
      if (!ollamaBaseURL.endsWith('/v1')) {
        ollamaBaseURL = ollamaBaseURL.replace(/\/+$/, '') + '/v1';
      }
      return createOpenAICompatible({
        name: 'ollama',
        apiKey: apiKey ?? 'ollama',
        baseURL: ollamaBaseURL,
        headers,
      });
    }

    default: {
      const _exhaustive: never = provider;
      throw new Error(`Unsupported provider: ${_exhaustive}`);
    }
  }
}
```

### Main Factory Function

```typescript
export interface CreateProviderOptions {
  config: ProviderConfig;
  modelId: string;  // e.g., 'claude-sonnet-4-5-20250929'
}

export function createProvider(options: CreateProviderOptions): LanguageModel {
  const { config, modelId } = options;
  const instance = createProviderInstance(config);

  // Azure: deployment-based routing, not model IDs
  if (config.provider === SupportedProvider.Azure) {
    const deploymentName = config.deploymentName ?? modelId;
    return (instance as ReturnType<typeof createAzure>).chat(deploymentName);
  }

  // OpenAI: Codex OAuth accounts rewrite ALL URLs to Responses endpoint.
  // Regular accounts use .responses() for Codex models, .chat() for everything else.
  if (config.provider === SupportedProvider.OpenAI) {
    if (config.oauthTokenFilePath || isCodexModel(modelId)) {
      return (instance as ReturnType<typeof createOpenAI>).responses(modelId);
    }
    return (instance as ReturnType<typeof createOpenAI>).chat(modelId);
  }

  // Generic path: call provider instance as function with model ID
  return (instance as ReturnType<typeof createAnthropic>)(modelId);
}
```

### Provider Auto-Detection from Model ID

Uses prefix matching via `MODEL_PROVIDER_MAP`:

```typescript
export function detectProviderFromModel(modelId: string): SupportedProvider | undefined {
  for (const [prefix, provider] of Object.entries(MODEL_PROVIDER_MAP)) {
    if (modelId.startsWith(prefix)) {
      return provider;
    }
  }
  return undefined;
}

export function createProviderFromModelId(
  modelId: string,
  overrides?: Partial<Omit<ProviderConfig, 'provider'>>,
): LanguageModel {
  const provider = detectProviderFromModel(modelId);
  if (!provider) {
    throw new Error(
      `Cannot detect provider for model "${modelId}". ` +
        `Known prefixes: ${Object.keys(MODEL_PROVIDER_MAP).join(', ')}`,
    );
  }
  return createProvider({ config: { provider, ...overrides }, modelId });
}
```

**Model prefix map** (`config/types.ts`):

```typescript
export const MODEL_PROVIDER_MAP: Record<string, SupportedProvider> = {
  'claude-':      'anthropic',
  'gpt-':         'openai',
  'o1-':          'openai',
  'o3-':          'openai',
  'o4-':          'openai',
  'codex-':       'openai',
  'gemini-':      'google',
  'mistral-':     'mistral',
  'codestral-':   'mistral',
  'llama-':       'groq',
  'grok-':        'xai',
  'glm-':         'zai',
} as const;
```

---

## 3. Auth Types

**File:** `auth/types.ts`

```typescript
export type AuthSource =
  | 'profile-oauth'    // OAuth token from claude-profile credential store
  | 'codex-oauth'      // OAuth token from OpenAI Codex PKCE flow
  | 'profile-api-key'  // API key stored in profile settings
  | 'environment'      // ANTHROPIC_API_KEY, OPENAI_API_KEY, etc.
  | 'default'          // No-auth providers (Ollama)
  | 'none';

export interface ResolvedAuth {
  apiKey: string;
  source: AuthSource;
  baseURL?: string;
  headers?: Record<string, string>;
  oauthTokenFilePath?: string;  // File-based OAuth path (Codex)
}

export interface AuthResolverContext {
  provider: SupportedProvider;
  profileId?: string;
  configDir?: string;  // CLAUDE_CONFIG_DIR for profile-specific keychain
}

// Extended result from queue-based resolution
export interface QueueResolvedAuth extends ResolvedAuth {
  accountId: string;
  resolvedProvider: SupportedProvider;
  resolvedModelId: string;        // May differ from requested (cross-provider equivalent)
  reasoningConfig: ReasoningConfig;
}
```

**Environment variable mappings:**

```typescript
export const PROVIDER_ENV_VARS: Record<SupportedProvider, string | undefined> = {
  anthropic:   'ANTHROPIC_API_KEY',
  openai:      'OPENAI_API_KEY',
  google:      'GOOGLE_GENERATIVE_AI_API_KEY',
  bedrock:     undefined,   // AWS credential chain
  azure:       'AZURE_OPENAI_API_KEY',
  mistral:     'MISTRAL_API_KEY',
  groq:        'GROQ_API_KEY',
  xai:         'XAI_API_KEY',
  openrouter:  'OPENROUTER_API_KEY',
  zai:         'ZHIPU_API_KEY',
  ollama:      undefined,   // No auth required
} as const;

export const PROVIDER_BASE_URL_ENV: Partial<Record<SupportedProvider, string>> = {
  anthropic: 'ANTHROPIC_BASE_URL',
  openai:    'OPENAI_BASE_URL',
  azure:     'AZURE_OPENAI_ENDPOINT',
} as const;

export const PROVIDER_SETTINGS_KEY: Partial<Record<SupportedProvider, string>> = {
  anthropic:   'globalAnthropicApiKey',
  openai:      'globalOpenAIApiKey',
  google:      'globalGoogleApiKey',
  groq:        'globalGroqApiKey',
  mistral:     'globalMistralApiKey',
  xai:         'globalXAIApiKey',
  azure:       'globalAzureApiKey',
  openrouter:  'globalOpenRouterApiKey',
  zai:         'globalZAIApiKey',
} as const;
```

---

## 4. Auth Resolver Chain

**File:** `auth/resolver.ts`

The resolver walks a 5-stage fallback chain in priority order.

### Public API

```typescript
export async function resolveAuth(ctx: AuthResolverContext): Promise<ResolvedAuth | null> {
  return (
    (await resolveFromProviderAccount(ctx)) ??  // Stage 0: unified ProviderAccount settings
    (await resolveFromProfileOAuth(ctx)) ??      // Stage 1: OAuth token (Anthropic only)
    resolveFromProfileApiKey(ctx) ??             // Stage 2: API key from app settings
    resolveFromEnvironment(ctx) ??               // Stage 3: environment variable
    resolveDefaultCredentials(ctx) ??            // Stage 4: no-auth providers (Ollama)
    null
  );
}

export async function hasCredentials(ctx: AuthResolverContext): Promise<boolean> {
  return (await resolveAuth(ctx)) !== null;
}
```

### Settings Accessor (dependency injection)

```typescript
type SettingsAccessor = (key: string) => string | undefined;
let _getSettingsValue: SettingsAccessor | null = null;

// Called once during app init to wire up settings access
export function registerSettingsAccessor(accessor: SettingsAccessor): void {
  _getSettingsValue = accessor;
}
```

### Stage 0: Unified Provider Account

```typescript
async function resolveFromProviderAccount(ctx: AuthResolverContext): Promise<ResolvedAuth | null> {
  // Read providerAccounts array from settings JSON
  const accounts = JSON.parse(_getSettingsValue('providerAccounts') ?? 'null');
  const account = accounts?.find(a => a.provider === ctx.provider && a.isActive);
  if (!account) return null;

  // File-based OAuth (OpenAI Codex subscription)
  if (account.authType === 'oauth' && account.provider === 'openai') {
    const tokenFilePath = path.join(app.getPath('userData'), 'codex-auth.json');
    const token = await ensureValidOAuthToken(tokenFilePath, 'openai');
    if (token) {
      return {
        apiKey: 'codex-oauth-placeholder',  // Real token injected by custom fetch
        source: 'codex-oauth',
        oauthTokenFilePath: tokenFilePath,
      };
    }
    return null;
  }

  // Anthropic OAuth: delegate to profile OAuth stage
  if (account.authType === 'oauth' && account.claudeProfileId) return null;

  // API key accounts
  if (account.authType === 'api-key' && account.apiKey) {
    const baseURL = account.provider === 'zai'
      ? (account.baseUrl || (account.billingModel === 'subscription' ? ZAI_CODING_API : ZAI_GENERAL_API))
      : account.baseUrl;
    return { apiKey: account.apiKey, source: 'profile-api-key', baseURL };
  }

  return null;
}
```

### Stage 1: Profile OAuth Token (Anthropic only)

```typescript
async function resolveFromProfileOAuth(ctx: AuthResolverContext): Promise<ResolvedAuth | null> {
  if (ctx.provider !== 'anthropic') return null;

  try {
    const tokenResult = await ensureValidToken(ctx.configDir);  // from claude-profile/token-refresh
    if (tokenResult.token) {
      return {
        apiKey: tokenResult.token,
        source: 'profile-oauth',
        headers: { 'anthropic-beta': 'claude-code-20250219,oauth-2025-04-20,interleaved-thinking-2025-05-14' },
        baseURL: process.env[PROVIDER_BASE_URL_ENV.anthropic],  // optional custom proxy
      };
    }
  } catch {
    // Token refresh failed — fall through
  }
  return null;
}

// Reactive refresh on 401 errors
export async function refreshOAuthTokenReactive(configDir: string | undefined): Promise<string | null> {
  try {
    const result = await reactiveTokenRefresh(configDir);
    return result.token ?? null;
  } catch {
    return null;
  }
}
```

### Stage 2: Profile API Key (from app settings)

```typescript
function resolveFromProfileApiKey(ctx: AuthResolverContext): ResolvedAuth | null {
  const settingsKey = PROVIDER_SETTINGS_KEY[ctx.provider];
  if (!settingsKey) return null;
  const apiKey = _getSettingsValue?.(settingsKey);
  if (!apiKey) return null;
  return {
    apiKey,
    source: 'profile-api-key',
    baseURL: process.env[PROVIDER_BASE_URL_ENV[ctx.provider] ?? ''],
  };
}
```

### Stage 3: Environment Variable

```typescript
function resolveFromEnvironment(ctx: AuthResolverContext): ResolvedAuth | null {
  const envVar = PROVIDER_ENV_VARS[ctx.provider];
  if (!envVar) return null;
  const apiKey = process.env[envVar];
  if (!apiKey) return null;
  return {
    apiKey,
    source: 'environment',
    baseURL: process.env[PROVIDER_BASE_URL_ENV[ctx.provider] ?? ''],
  };
}
```

### Stage 4: Default Credentials (no-auth providers)

```typescript
const NO_AUTH_PROVIDERS = new Set<SupportedProvider>(['ollama']);

function resolveDefaultCredentials(ctx: AuthResolverContext): ResolvedAuth | null {
  if (!NO_AUTH_PROVIDERS.has(ctx.provider)) return null;
  return { apiKey: '', source: 'default' };
}
```

---

## 5. Queue-Based Resolution (Global Priority Queue)

**File:** `auth/resolver.ts`

Enables multi-account failover: walks a priority-ordered list of `ProviderAccount` objects, skipping unavailable/excluded accounts and mapping the requested model to the right model ID per provider.

```typescript
export async function resolveAuthFromQueue(
  requestedModel: string,
  queue: ProviderAccount[],
  options?: {
    excludeAccountIds?: string[];
    userModelOverrides?: Record<string, Partial<Record<BuiltinProvider, ProviderModelSpec>>>;
    autoSwitchSettings?: ClaudeAutoSwitchSettings;
  }
): Promise<QueueResolvedAuth | null> {
  const excludeSet = new Set(options?.excludeAccountIds ?? []);

  for (const account of queue) {
    if (excludeSet.has(account.id)) continue;

    const { available } = scoreProviderAccount(account, settings);
    if (!available) continue;

    const supportedProvider = BUILTIN_TO_SUPPORTED[account.provider];
    if (!supportedProvider) continue;

    // Try cross-provider equivalence table first
    const modelSpec = resolveModelEquivalent(requestedModel, account.provider, options?.userModelOverrides);

    if (!modelSpec) {
      // No cross-provider equivalent: only proceed if model is native to this provider
      const nativeProvider = detectProviderFromModel(requestedModel);
      if (nativeProvider !== supportedProvider && supportedProvider !== 'ollama') continue;
      // Ollama: pass arbitrary unrecognized models through; but reject known non-Ollama models
      if (supportedProvider === 'ollama' && nativeProvider && nativeProvider !== 'ollama') continue;
    }

    const resolvedModelId = modelSpec?.modelId ?? requestedModel;
    const auth = await resolveCredentialsForAccount(account, supportedProvider);
    if (!auth) continue;

    return {
      ...auth,
      accountId: account.id,
      resolvedProvider: supportedProvider,
      resolvedModelId,
      reasoningConfig: modelSpec?.reasoning ?? { type: 'none' },
    };
  }

  return null;
}
```

### Build Default Queue from Settings

```typescript
export function buildDefaultQueueConfig(
  requestedModel: string,
): { queue: ProviderAccount[]; requestedModel: string } | undefined {
  const accountsRaw = _getSettingsValue?.('providerAccounts');
  if (!accountsRaw) return undefined;

  const accounts: ProviderAccount[] = JSON.parse(accountsRaw);
  if (!accounts.length) return undefined;

  const priorityOrder: string[] = JSON.parse(_getSettingsValue?.('globalPriorityOrder') ?? '[]');

  // Sort by explicit priority order; unordered accounts go to end
  const sorted = [...accounts].sort((a, b) => {
    const idxA = priorityOrder.indexOf(a.id);
    const idxB = priorityOrder.indexOf(b.id);
    return (idxA === -1 ? Infinity : idxA) - (idxB === -1 ? Infinity : idxB);
  });

  return { queue: sorted, requestedModel };
}
```

### Provider Account Credential Resolution

```typescript
async function resolveCredentialsForAccount(
  account: ProviderAccount,
  provider: SupportedProvider,
): Promise<ResolvedAuth | null> {
  // No-auth providers (Ollama)
  if (NO_AUTH_PROVIDERS.has(provider)) {
    return { apiKey: '', source: 'default', baseURL: account.baseUrl };
  }

  // File-based OAuth (OpenAI Codex)
  if (account.authType === 'oauth' && account.provider === 'openai') {
    const tokenFilePath = path.join(app.getPath('userData'), 'codex-auth.json');
    const token = await ensureValidOAuthToken(tokenFilePath, 'openai');
    if (token) return { apiKey: 'codex-oauth-placeholder', source: 'codex-oauth', oauthTokenFilePath: tokenFilePath };
    return null;
  }

  // Anthropic OAuth via claude-profile system
  if (account.authType === 'oauth' && account.provider === 'anthropic' && account.claudeProfileId) {
    return resolveAuth({ provider, profileId: account.claudeProfileId });
  }

  // API key
  if (account.authType === 'api-key' && account.apiKey) {
    const baseURL = account.provider === 'zai' ? resolveZaiBaseUrl(account) : account.baseUrl;
    return { apiKey: account.apiKey, source: 'profile-api-key', baseURL };
  }

  return null;
}
```

---

## 6. OAuth Fetch Interceptor

**File:** `providers/oauth-fetch.ts`

Data-driven fetch interceptor for file-based OAuth providers. Adding a new OAuth provider = adding an entry to `OAUTH_PROVIDER_REGISTRY`.

```typescript
const CODEX_API_ENDPOINT = 'https://chatgpt.com/backend-api/codex/responses';

const OAUTH_PROVIDER_REGISTRY: Record<string, OAuthProviderSpec> = {
  openai: {
    tokenEndpoint: 'https://auth.openai.com/oauth/token',
    clientId: 'app_EMoamEEZ73f0CkXaXp7hrann',
    rewriteUrl: (url: string) => {
      const parsed = new URL(url);
      if (parsed.pathname.includes('/chat/completions') || parsed.pathname.includes('/v1/responses')) {
        return CODEX_API_ENDPOINT;
      }
      return url;
    },
  },
};
```

### Token Validation and Refresh

```typescript
export async function ensureValidOAuthToken(
  tokenFilePath: string,
  provider?: string,
): Promise<string | null> {
  const stored = readTokenFile(tokenFilePath);
  if (!stored) return null;

  const expiresIn = stored.expires_at - Date.now();
  if (expiresIn > REFRESH_THRESHOLD_MS) {  // REFRESH_THRESHOLD_MS = 5 minutes
    return stored.access_token;
  }

  // Near expiry — auto-refresh
  const providerSpec = OAUTH_PROVIDER_REGISTRY[provider ?? 'openai'];
  if (!providerSpec) return null;

  return await refreshOAuthToken(stored.refresh_token, providerSpec, tokenFilePath);
}
```

### Custom Fetch Function

```typescript
export function createOAuthProviderFetch(
  tokenFilePath: string,
  provider?: string,
): typeof globalThis.fetch {
  const providerSpec = OAUTH_PROVIDER_REGISTRY[provider ?? 'openai'];

  return async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    // 1. Get valid token (auto-refresh if near expiry)
    const token = await ensureValidOAuthToken(tokenFilePath, provider);
    if (!token) throw new Error('OAuth: No valid token available. Please re-authenticate.');

    // 2. Strip dummy Authorization, inject real Bearer token
    const headers = new Headers(init?.headers);
    headers.delete('authorization');
    headers.delete('Authorization');
    headers.set('Authorization', `Bearer ${token}`);

    // 3. Resolve URL from input (string | URL | Request)
    const url = typeof input === 'string' ? input
      : input instanceof URL ? input.toString()
      : input instanceof Request ? input.url
      : String(input);

    // 4. Rewrite URL if provider specifies (Codex: /chat/completions → /backend-api/codex/responses)
    const finalUrl = providerSpec?.rewriteUrl ? providerSpec.rewriteUrl(url) : url;

    return globalThis.fetch(finalUrl, { ...init, headers });
  };
}
```

---

## 7. OpenAI Codex PKCE OAuth Flow

**File:** `auth/codex-oauth.ts`

Full PKCE (Proof Key for Code Exchange) OAuth 2.0 flow for OpenAI Codex subscriptions.

### Constants

```typescript
const CLIENT_ID = 'app_EMoamEEZ73f0CkXaXp7hrann';
const AUTH_ENDPOINT = 'https://auth.openai.com/oauth/authorize';
const TOKEN_ENDPOINT = 'https://auth.openai.com/oauth/token';
const REDIRECT_URI = 'http://localhost:1455/auth/callback';
const SCOPES = 'openid profile email offline_access';
const REFRESH_THRESHOLD_MS = 5 * 60 * 1000;   // 5 minutes before expiry
const OAUTH_FLOW_TIMEOUT_MS = 30 * 60 * 1000; // 30 minutes
```

### Types

```typescript
export interface CodexAuthResult {
  accessToken: string;
  refreshToken: string;
  expiresAt: number;  // unix ms
  email?: string;
}

export interface CodexAuthState {
  isAuthenticated: boolean;
  expiresAt?: number;
}

// Token file schema (stored at userData/codex-auth.json, chmod 600)
interface StoredTokens {
  access_token: string;
  refresh_token: string;
  expires_at: number;  // unix ms
}
```

### Full PKCE Flow

```typescript
export async function startCodexOAuthFlow(): Promise<CodexAuthResult> {
  const codeVerifier = crypto.randomBytes(32).toString('base64url');
  const codeChallenge = crypto.createHash('sha256').update(codeVerifier).digest('base64url');
  const state = crypto.randomBytes(16).toString('hex');

  const authUrl = new url.URL(AUTH_ENDPOINT);
  authUrl.searchParams.set('client_id', CLIENT_ID);
  authUrl.searchParams.set('redirect_uri', REDIRECT_URI);
  authUrl.searchParams.set('response_type', 'code');
  authUrl.searchParams.set('scope', SCOPES);
  authUrl.searchParams.set('state', state);
  authUrl.searchParams.set('code_challenge', codeChallenge);
  authUrl.searchParams.set('code_challenge_method', 'S256');
  authUrl.searchParams.set('originator', 'auto-claude');
  authUrl.searchParams.set('codex_cli_simplified_flow', 'true');

  return new Promise<CodexAuthResult>((resolve, reject) => {
    const server = http.createServer((req, res) => {
      const parsedUrl = new url.URL(req.url!, 'http://localhost:1455');
      if (parsedUrl.pathname !== '/auth/callback') { res.writeHead(404).end(); return; }

      const code = parsedUrl.searchParams.get('code');
      const returnedState = parsedUrl.searchParams.get('state');

      // CSRF protection: verify state matches
      if (returnedState !== state) {
        reject(new Error('OAuth error: State parameter mismatch — possible CSRF attack'));
        return;
      }

      res.writeHead(200, { 'Content-Type': 'text/html' }).end(successHtml);
      cleanup();

      exchangeCodeForTokens(code!, codeVerifier)
        .then(async (result) => {
          await writeStoredTokens({ access_token: result.accessToken, refresh_token: result.refreshToken, expires_at: result.expiresAt });
          resolve(result);
        })
        .catch(reject);
    });

    server.listen(1455, '127.0.0.1', () => {
      shell.openExternal(authUrl.toString());
      setTimeout(() => reject(new Error('OAuth flow timed out')), OAUTH_FLOW_TIMEOUT_MS);
    });
  });
}
```

### Token Exchange

```typescript
async function exchangeCodeForTokens(code: string, codeVerifier: string): Promise<CodexAuthResult> {
  const body = new URLSearchParams({
    grant_type: 'authorization_code',
    code,
    redirect_uri: REDIRECT_URI,
    client_id: CLIENT_ID,
    code_verifier: codeVerifier,
  });

  const response = await fetch(TOKEN_ENDPOINT, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: body.toString(),
  });

  const data = await response.json();
  const expiresAt = Date.now() + (data.expires_in ?? 3600) * 1000;
  const email = typeof data.id_token === 'string' ? getEmailFromIdToken(data.id_token) : undefined;

  return { accessToken: data.access_token, refreshToken: data.refresh_token, expiresAt, email };
}
```

### Token Refresh

```typescript
export async function refreshCodexToken(refreshToken: string): Promise<CodexAuthResult> {
  const body = new URLSearchParams({
    grant_type: 'refresh_token',
    refresh_token: refreshToken,
    client_id: CLIENT_ID,
  });

  const response = await fetch(TOKEN_ENDPOINT, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: body.toString(),
  });

  const data = await response.json();
  // Token rotation: new refresh token may be issued; fall back to existing one
  const newRefreshToken = typeof data.refresh_token === 'string' ? data.refresh_token : refreshToken;
  const expiresAt = Date.now() + (data.expires_in ?? 3600) * 1000;

  await writeStoredTokens({ access_token: data.access_token, refresh_token: newRefreshToken, expires_at: expiresAt });
  return { accessToken: data.access_token, refreshToken: newRefreshToken, expiresAt };
}
```

### Token Validation

```typescript
// Returns null if no stored tokens
// Auto-refreshes if token expires within 5 minutes
// Returns valid access token
export async function ensureValidCodexToken(tokenFilePath?: string): Promise<string | null> {
  const stored = await readStoredTokens(tokenFilePath);
  if (!stored) return null;

  const expiresIn = stored.expires_at - Date.now();
  if (expiresIn > REFRESH_THRESHOLD_MS) return stored.access_token;

  try {
    const refreshed = await refreshCodexToken(stored.refresh_token);
    return refreshed.accessToken;
  } catch {
    return null;
  }
}

export async function getCodexAuthState(): Promise<CodexAuthState> {
  const stored = await readStoredTokens();
  if (!stored) return { isAuthenticated: false };
  return { isAuthenticated: Date.now() < stored.expires_at, expiresAt: stored.expires_at };
}

export async function clearCodexAuth(): Promise<void> {
  fs.unlinkSync(await getTokenFilePath());
}
```

### Token Storage

Tokens stored at `userData/codex-auth.json` with `chmod 600` (best-effort on Windows):

```typescript
async function writeStoredTokens(tokens: StoredTokens): Promise<void> {
  const filePath = path.join(app.getPath('userData'), 'codex-auth.json');
  // Sanitize before write (CodeQL compliance)
  const safeTokens: StoredTokens = {
    access_token: typeof tokens.access_token === 'string' ? tokens.access_token : '',
    refresh_token: typeof tokens.refresh_token === 'string' ? tokens.refresh_token : '',
    expires_at: typeof tokens.expires_at === 'number' ? tokens.expires_at : 0,
  };
  fs.writeFileSync(filePath, JSON.stringify(safeTokens, null, 2), 'utf8');
  try { fs.chmodSync(filePath, 0o600); } catch { /* non-critical on Windows */ }
}
```

---

## 8. Thinking Token Configuration

**File:** `config/types.ts`

### Thinking Level Types and Budgets

```typescript
export type ThinkingLevel = 'low' | 'medium' | 'high' | 'xhigh';

export const THINKING_BUDGET_MAP: Record<ThinkingLevel, number> = {
  low:    1024,
  medium: 4096,
  high:   16384,
  xhigh:  32768,
} as const;

// Models supporting adaptive thinking (both max_thinking_tokens and effort_level)
export const ADAPTIVE_THINKING_MODELS: ReadonlySet<string> = new Set([
  'claude-opus-4-6',
]);
```

### Per-Provider Thinking Options Builder

```typescript
export function buildThinkingProviderOptions(
  modelId: string,
  thinkingLevel: ThinkingLevel,
): Record<string, Record<string, unknown>> | undefined {
  const provider = detectProviderFromModelId(modelId);
  const budgetTokens = THINKING_BUDGET_MAP[thinkingLevel];

  switch (provider) {
    case 'anthropic':
      return { anthropic: { thinking: { type: 'enabled', budgetTokens } } };

    case 'openai':
      // o1/o3/o4 series: use reasoning effort, not token budget
      if (modelId.startsWith('o1-') || modelId.startsWith('o3-') || modelId.startsWith('o4-')) {
        const effortMap: Record<ThinkingLevel, string> = { low: 'low', medium: 'medium', high: 'high', xhigh: 'high' };
        return { openai: { reasoningEffort: effortMap[thinkingLevel] } };
      }
      return undefined;

    case 'google':
      return { google: { thinkingConfig: { thinkingBudget: budgetTokens } } };

    case 'zai':
      // openaiCompatible merges providerOptions.openaiCompatible into request body
      return { openaiCompatible: { thinking: { type: 'enabled', clear_thinking: false } } };

    default:
      return undefined;
  }
}
```

### Reasoning Config Resolution (from queue)

```typescript
export function resolveReasoningParams(config: ReasoningConfig): Record<string, unknown> {
  switch (config.type) {
    case 'thinking_tokens':
      return { maxThinkingTokens: THINKING_BUDGET_MAP[config.level ?? 'medium'] };
    case 'adaptive_effort':
      return {
        maxThinkingTokens: THINKING_BUDGET_MAP[config.level ?? 'high'],
        effortLevel: config.level ?? 'high',
      };
    case 'reasoning_effort':
      return { reasoningEffort: config.level ?? 'medium' };
    case 'thinking_toggle':
      return { thinking: config.level !== undefined };
    case 'none':
      return {};
  }
}
```

### Beta Headers

```typescript
// 1M context window beta header
export const MODEL_BETAS_MAP: Partial<Record<ModelShorthand, string[]>> = {
  'opus-1m': ['context-1m-2025-08-07'],
} as const;

// OAuth + interleaved thinking beta (set on provider instance, not per-request)
const ANTHROPIC_OAUTH_BETA = 'claude-code-20250219,oauth-2025-04-20,interleaved-thinking-2025-05-14';
```

---

## 9. Client Factory

**File:** `client/factory.ts`

### Client Types

```typescript
export interface AgentClientConfig {
  agentType: AgentType;
  systemPrompt: string;
  toolContext: ToolContext;
  phase: Phase;
  modelShorthand?: ModelShorthand;
  thinkingLevel?: ThinkingLevel;
  maxSteps?: number;            // default: 200
  profileId?: string;
  abortSignal?: AbortSignal;
  additionalMcpServers?: string[];
  queueConfig?: {
    queue: ProviderAccount[];
    requestedModel: string;
    excludeAccountIds?: string[];
    userModelOverrides?: Record<string, Partial<Record<string, ProviderModelSpec>>>;
  };
}

export interface AgentClientResult {
  model: LanguageModel;
  tools: Record<string, AITool>;
  mcpClients: McpClientResult[];
  systemPrompt: string;
  maxSteps: number;
  thinkingLevel: ThinkingLevel;
  cleanup: () => Promise<void>;
  queueAuth?: QueueResolvedAuth;
}

export interface SimpleClientConfig {
  systemPrompt: string;
  modelShorthand?: ModelShorthand | string;  // default: 'haiku'
  thinkingLevel?: ThinkingLevel;              // default: 'low'
  profileId?: string;
  maxSteps?: number;                          // default: 1
  tools?: Record<string, AITool>;
  queueConfig?: { ... };
}

export interface SimpleClientResult {
  model: LanguageModel;
  resolvedModelId: string;
  tools: Record<string, AITool>;
  systemPrompt: string;
  maxSteps: number;
  thinkingLevel: ThinkingLevel;
  queueAuth?: QueueResolvedAuth;
}
```

### createAgentClient

Full client with tools, MCP servers, queue-based auth.

```typescript
export async function createAgentClient(config: AgentClientConfig): Promise<AgentClientResult> {
  let model: LanguageModel;
  let resolvedThinkingLevel: ThinkingLevel;
  let queueAuth: QueueResolvedAuth | null = null;

  if (config.queueConfig) {
    // Queue-based resolution: use global priority queue
    queueAuth = await resolveAuthFromQueue(
      config.queueConfig.requestedModel,
      config.queueConfig.queue,
      { excludeAccountIds: config.queueConfig.excludeAccountIds },
    );
    if (!queueAuth) throw new Error('No available account in priority queue');

    // Use queue's resolved provider directly (bypasses prefix detection — critical for Ollama)
    model = createProvider({
      config: {
        provider: queueAuth.resolvedProvider,
        apiKey: queueAuth.apiKey,
        baseURL: queueAuth.baseURL,
        headers: queueAuth.headers,
        oauthTokenFilePath: queueAuth.oauthTokenFilePath,
      },
      modelId: queueAuth.resolvedModelId,
    });

    resolvedThinkingLevel = (queueAuth.reasoningConfig.level as ThinkingLevel)
      ?? config.thinkingLevel
      ?? getDefaultThinkingLevel(config.agentType);
  } else {
    // Legacy per-provider resolution
    const modelId = resolveModelId(config.modelShorthand ?? config.phase);
    const detectedProvider = detectProviderFromModel(modelId) ?? 'anthropic';
    const auth = await resolveAuth({ provider: detectedProvider, profileId: config.profileId });

    model = createProvider({
      config: {
        provider: detectedProvider,
        apiKey: auth?.apiKey,
        baseURL: auth?.baseURL,
        headers: auth?.headers,
        oauthTokenFilePath: auth?.oauthTokenFilePath,
      },
      modelId,
    });

    resolvedThinkingLevel = config.thinkingLevel ?? getDefaultThinkingLevel(config.agentType);
  }

  // Bind builtin tools via ToolRegistry
  const registry = buildToolRegistry();
  const tools: Record<string, AITool> = registry.getToolsForAgent(config.agentType, config.toolContext);

  // Initialize MCP servers and merge their tools
  const mcpServerIds = getRequiredMcpServers(config.agentType, {});
  if (config.additionalMcpServers) mcpServerIds.push(...config.additionalMcpServers);

  let mcpClients: McpClientResult[] = [];
  if (mcpServerIds.length > 0) {
    mcpClients = await createMcpClientsForAgent(config.agentType, {});
    Object.assign(tools, mergeMcpTools(mcpClients));
  }

  return {
    model,
    tools,
    mcpClients,
    systemPrompt: config.systemPrompt,
    maxSteps: config.maxSteps ?? 200,
    thinkingLevel: resolvedThinkingLevel,
    cleanup: async () => closeAllMcpClients(mcpClients),
    ...(queueAuth ? { queueAuth } : {}),
  };
}
```

### createSimpleClient

Lightweight utility client (no MCP, minimal tools).

```typescript
export async function createSimpleClient(config: SimpleClientConfig): Promise<SimpleClientResult> {
  // Auto-build queue config from settings if not explicitly provided
  const queueConfig = config.queueConfig
    ?? buildDefaultQueueConfig(resolveModelId(config.modelShorthand ?? 'haiku'));

  let model: LanguageModel;
  let resolvedModelId: string;
  let resolvedThinkingLevel: ThinkingLevel = config.thinkingLevel ?? 'low';
  let queueAuth: QueueResolvedAuth | null = null;

  if (queueConfig) {
    queueAuth = await resolveAuthFromQueue(queueConfig.requestedModel, queueConfig.queue);
    if (!queueAuth) throw new Error('No available account in priority queue');

    resolvedModelId = queueAuth.resolvedModelId;
    model = createProvider({
      config: {
        provider: queueAuth.resolvedProvider,
        apiKey: queueAuth.apiKey,
        baseURL: queueAuth.baseURL,
        headers: queueAuth.headers,
        oauthTokenFilePath: queueAuth.oauthTokenFilePath,
      },
      modelId: resolvedModelId,
    });

    resolvedThinkingLevel = (queueAuth.reasoningConfig.level as ThinkingLevel) ?? config.thinkingLevel ?? 'low';
  } else {
    resolvedModelId = resolveModelId(config.modelShorthand ?? 'haiku');
    const detectedProvider = detectProviderFromModel(resolvedModelId) ?? 'anthropic';
    const auth = await resolveAuth({ provider: detectedProvider, profileId: config.profileId });

    model = createProvider({
      config: {
        provider: detectedProvider,
        apiKey: auth?.apiKey,
        baseURL: auth?.baseURL,
        headers: auth?.headers,
        oauthTokenFilePath: auth?.oauthTokenFilePath,
      },
      modelId: resolvedModelId,
    });
  }

  return {
    model,
    resolvedModelId,
    tools: config.tools ?? {},
    systemPrompt: config.systemPrompt,
    maxSteps: config.maxSteps ?? 1,
    thinkingLevel: resolvedThinkingLevel,
    ...(queueAuth ? { queueAuth } : {}),
  };
}
```

---

## 10. Key Design Patterns and Gotchas

### OAuth Token vs API Key Routing for Anthropic

- API keys start with `sk-ant-api` — use `apiKey` param → `x-api-key` header
- OAuth tokens start with `sk-ant-oa` or `sk-ant-ort` — use `authToken` param → `Authorization: Bearer` header
- OAuth tokens **require** the `anthropic-beta` header: `claude-code-20250219,oauth-2025-04-20,interleaved-thinking-2025-05-14`

### OpenAI Codex OAuth vs Regular OpenAI

- Codex OAuth accounts: all requests routed through `.responses()` (not `.chat()`), URL rewritten to `https://chatgpt.com/backend-api/codex/responses`
- Regular API key + Codex model name: `.responses()` too
- Regular API key + non-Codex model: `.chat()`
- The custom `fetch` interceptor handles URL rewriting and `Authorization: Bearer` injection

### Azure Deployment Routing

- Azure doesn't use model IDs in the same way; uses deployment names
- Factory calls `.chat(deploymentName)` where `deploymentName = config.deploymentName ?? modelId`

### Ollama URL Normalization

- User configures base Ollama URL (e.g., `http://localhost:11434`)
- Factory appends `/v1` suffix if not present (OpenAI-compatible SDK requires it)
- Model names are arbitrary user-installed names (e.g., `llama3.1:8b`) — no predictable prefix
- Queue resolver special-cases Ollama: passes unrecognized model IDs through rather than skipping

### Queue Resolver: Why Not Just Detect Provider from Model ID?

```
// CRITICAL: Use createProvider() with the queue-resolved provider to avoid re-detecting
// from model ID prefix. This is critical for providers like Ollama whose models
// (e.g., 'llama3.1:8b') don't follow predictable prefix conventions.
```

The queue knows the correct provider; always pass `queueAuth.resolvedProvider` directly rather than re-running `detectProviderFromModel`.

### Z.AI Endpoint Routing

- Subscription / Coding Plan: `https://api.z.ai/api/coding/paas/v4`
- Pay-per-use: `https://api.z.ai/api/paas/v4`
- Explicit `baseUrl` on account always takes precedence

### Beta Header for Anthropic Thinking (Interleaved)

The `interleaved-thinking-2025-05-14` beta must be included in the provider instance headers (not per-request) when using OAuth tokens. This enables extended thinking with streaming.

### npm Package Mapping

| Provider | Package |
|----------|---------|
| Anthropic | `@ai-sdk/anthropic` |
| OpenAI | `@ai-sdk/openai` |
| Google | `@ai-sdk/google` |
| Bedrock | `@ai-sdk/amazon-bedrock` |
| Azure | `@ai-sdk/azure` |
| Mistral | `@ai-sdk/mistral` |
| Groq | `@ai-sdk/groq` |
| xAI | `@ai-sdk/xai` |
| OpenRouter | `@openrouter/ai-sdk-provider` |
| Z.AI / Ollama | `@ai-sdk/openai-compatible` |
