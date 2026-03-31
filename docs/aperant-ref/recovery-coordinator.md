# Aperant — Recovery & Rate-Limit Reference

Extracted from `F:\Tools\External\Aperant\apps\desktop\src\main\`.
Use this as an implementation blueprint for Golem's own error-recovery / profile-swap system.

---

## File Index

| File | Purpose |
|------|---------|
| `main/rate-limit-detector.ts` | All regex patterns + primary detection functions |
| `main/claude-profile/rate-limit-manager.ts` | Per-profile event recording + `isProfileRateLimited()` |
| `main/claude-profile/usage-parser.ts` | `parseResetTime()`, `classifyRateLimitType()`, `/usage` output parser |
| `main/claude-profile/usage-monitor.ts` | Proactive polling loop, `calculateAvailabilityScore()`, proactive swap |
| `main/claude-profile/operation-registry.ts` | `ClaudeOperationRegistry` — canonical replacement for recovery coordinator |
| `main/services/sdk-session-recovery-coordinator.ts` | **Deprecated** — kept for reference; superseded by `operation-registry.ts` |
| `main/ai/orchestration/recovery-manager.ts` | `RecoveryManager` — checkpoint/resume + failure classification for build pipeline |
| `renderer/stores/rate-limit-store.ts` | Zustand store for UI modal state |

---

## 1. Rate Limit Detection Patterns

**File:** `apps/desktop/src/main/rate-limit-detector.ts`

### Primary pattern — Claude Code CLI output

```typescript
// Matches: "Limit reached · resets Dec 17 at 6am (Europe/Oslo)"
// Both middle-dot (·) and bullet (•) are handled.
const RATE_LIMIT_PATTERN = /Limit reached\s*[·•]\s*resets\s+(.+?)(?:\s*$|\n)/im;
```

### Codex / OpenAI pattern

```typescript
// Matches: "usage_limit_exceeded" or "UsageLimitExceeded" with optional reset info
const CODEX_RATE_LIMIT_PATTERN = /(?:usage_limit_exceeded|UsageLimitExceeded)(?:.*?reset(?:s|_at)?\s*[:\s]*(.+?))?(?:\s*$|\n)/im;
```

### Secondary indicators (fallback scan)

```typescript
const RATE_LIMIT_INDICATORS = [
  /rate\s*limit/i,
  /usage\s*limit/i,
  /limit\s*reached/i,
  /exceeded.*limit/i,
  /too\s*many\s*requests/i,
  /usage_limit_exceeded/i,
  /UsageLimitExceeded/,
  /codex.*rate\s*limit/i,
];
```

### Auth failure patterns

These are intentionally specific to avoid matching AI-generated content discussing authentication:

```typescript
const AUTH_FAILURE_PATTERNS = [
  /["']?type["']?\s*:\s*["']?authentication_error["']?/i,  // JSON error type
  /API\s*Error:\s*401/i,
  /oauth\s*token\s+has\s+expired/i,
  /please\s+(obtain\s+a\s+new|refresh\s+your)\s+(existing\s+)?token/i,
  /\[.*\]\s*authentication\s*(is\s*)?required/i,           // CLI context markers
  /\[.*\]\s*not\s*(yet\s*)?authenticated/i,
  /\[.*\]\s*login\s*(is\s*)?required/i,
  /status[:\s]+401/i,
  /HTTP\s*401/i,
  /Error:\s*.*(?:unauthorized|authentication|invalid\s*token)/i,
  /·\s*Please\s+run\s+\/login/i,
];
```

### Billing failure patterns

```typescript
const BILLING_FAILURE_PATTERNS = [
  /credit\s*balance\s*(is\s+)?(too\s+)?(insufficient|low|empty|zero|exhausted)/i,
  /insufficient\s*credit(s)?/i,
  /no\s*(remaining\s*)?credit(s)?/i,
  /credit(s)?\s*(are\s*)?(exhausted|depleted|used\s*up)/i,
  /out\s*of\s*credit(s)?/i,
  /credit\s*limit\s*(reached|exceeded)/i,
  /billing\s*(error|issue|problem|failure)/i,
  /payment\s*(required|failed|issue|problem)/i,
  /subscription\s*(expired|inactive|cancelled|canceled)/i,
  /account\s*(suspended|inactive)\s*(due\s*to\s*billing)?/i,
  /usage\s*quota\s*(exceeded|reached)/i,
  /monthly\s*(usage\s*)?(limit|quota)\s*(exceeded|reached)/i,
  /plan\s*(limit|quota)\s*(exceeded|reached)/i,
  /["']?type["']?\s*:\s*["']?billing_error["']?/i,
  /["']?type["']?\s*:\s*["']?insufficient_credits["']?/i,
  /["']?error["']?\s*:\s*["']?insufficient_credits["']?/i,
  /extra_usage\s*(exceeded|limit|error)?/i,
  /(?:HTTP|status|code|error)\s*:?\s*402\b/i,
  /\b402\s+payment\s+required/i,
  /API\s*Error:\s*402/i,
  /insufficient\s*(funds|balance)/i,
  /balance\s*(is\s*)?(zero|empty|insufficient)/i,
  /please\s*(add|purchase)\s*(more\s*)?(credits?|funds)/i,
  /top\s*up\s*(your\s*)?(account|credits|balance)/i,
];
```

---

## 2. Rate Limit Type Classification

**File:** `apps/desktop/src/main/rate-limit-detector.ts`

```typescript
// Weekly limits contain a date like "Dec 17"; session limits are time-only like "11:59pm"
function classifyLimitType(resetTimeStr: string): 'session' | 'weekly' {
  const hasDate = /[A-Za-z]{3}\s+\d+/i.test(resetTimeStr);
  const hasWeeklyIndicator = resetTimeStr.toLowerCase().includes('week');
  return (hasDate || hasWeeklyIndicator) ? 'weekly' : 'session';
}
```

Same logic duplicated in `usage-parser.ts` as `classifyRateLimitType()` — keep these in sync.

---

## 3. Primary Detection Function

**File:** `apps/desktop/src/main/rate-limit-detector.ts`

```typescript
export function detectRateLimit(
  output: string,
  profileId?: string
): RateLimitDetectionResult {
  // 1. Try primary Claude pattern
  const match = output.match(RATE_LIMIT_PATTERN);
  if (match) {
    const resetTime = match[1].trim();
    const limitType = classifyLimitType(resetTime);
    const profileManager = getClaudeProfileManager();
    const effectiveProfileId = profileId || profileManager.getActiveProfile().id;

    profileManager.recordRateLimitEvent(effectiveProfileId, resetTime);

    const bestProfile = profileManager.getBestAvailableProfile(effectiveProfileId);
    return {
      isRateLimited: true,
      resetTime,
      limitType,
      profileId: effectiveProfileId,
      suggestedProfile: bestProfile ? { id: bestProfile.id, name: bestProfile.name } : undefined,
      originalError: sanitizeErrorOutput(output)   // truncated to MAX_ERROR_LENGTH = 500
    };
  }

  // 2. Try Codex pattern (same structure, resetTime may be undefined)
  const codexMatch = output.match(CODEX_RATE_LIMIT_PATTERN);
  if (codexMatch) { /* same flow */ }

  // 3. Scan secondary indicators (no resetTime extracted)
  for (const pattern of RATE_LIMIT_INDICATORS) {
    if (pattern.test(output)) {
      // returns isRateLimited: true without resetTime or limitType
    }
  }

  return { isRateLimited: false };
}
```

**Return type:**
```typescript
export interface RateLimitDetectionResult {
  isRateLimited: boolean;
  resetTime?: string;                            // e.g. "Dec 17 at 6am (Europe/Oslo)"
  limitType?: 'session' | 'weekly';
  profileId?: string;
  suggestedProfile?: { id: string; name: string };
  originalError?: string;                        // truncated to 500 chars
}
```

**Detection priority order:**
1. Auth failure check runs before billing check — each function first checks the others to avoid double-classification.
2. `detectAuthFailure()` calls `detectRateLimit()` first and returns `{ isAuthFailure: false }` if it's a rate limit.
3. `detectBillingFailure()` calls both `detectRateLimit()` and `detectAuthFailure()` first.

---

## 4. Auth & Billing Failure Classification

**File:** `apps/desktop/src/main/rate-limit-detector.ts`

```typescript
function classifyAuthFailureType(output: string): 'missing' | 'invalid' | 'expired' | 'unknown' {
  const lower = output.toLowerCase();
  if (/missing|not\s*(yet\s*)?authenticated|required/.test(lower)) return 'missing';
  if (/expired|session\s*expired|obtain\s*(a\s*)?new\s*token|refresh\s*(your\s*)?(existing\s*)?token/.test(lower)) return 'expired';
  if (/invalid|unauthorized|denied|401|authentication_error/.test(lower)) return 'invalid';
  return 'unknown';
}

function classifyBillingFailureType(output: string): 'insufficient_credits' | 'payment_required' | 'subscription_inactive' | 'unknown' {
  const lower = output.toLowerCase();
  if (/credit\s*(balance|s)?|insufficient\s*(credit|funds|balance)|out\s*of\s*credit|no\s*(remaining\s*)?credit|extra_usage/.test(lower))
    return 'insufficient_credits';
  if (/subscription\s*(expired|inactive|cancelled|canceled)|account\s*(suspended|inactive)/.test(lower))
    return 'subscription_inactive';
  if (/payment\s*(required|failed)|402|billing\s*(error|issue|problem|failure)/.test(lower))
    return 'payment_required';
  return 'unknown';
}
```

**Result types:**
```typescript
export interface AuthFailureDetectionResult {
  isAuthFailure: boolean;
  profileId?: string;
  failureType?: 'missing' | 'invalid' | 'expired' | 'unknown';
  message?: string;       // user-friendly string
  originalError?: string;
}

export interface BillingFailureDetectionResult {
  isBillingFailure: boolean;
  profileId?: string;
  failureType?: 'insufficient_credits' | 'payment_required' | 'subscription_inactive' | 'unknown';
  message?: string;
  originalError?: string;
}
```

---

## 5. Per-Profile Rate Limit Event Storage

**File:** `apps/desktop/src/main/claude-profile/rate-limit-manager.ts`

```typescript
export function recordRateLimitEvent(
  profile: ClaudeProfile,
  resetTimeStr: string
): ClaudeRateLimitEvent {
  const event: ClaudeRateLimitEvent = {
    type: classifyRateLimitType(resetTimeStr),  // from usage-parser.ts
    hitAt: new Date(),
    resetAt: parseResetTime(resetTimeStr),       // from usage-parser.ts
    resetTimeString: resetTimeStr
  };

  // Keep the last 10 events, newest first
  profile.rateLimitEvents = [
    event,
    ...(profile.rateLimitEvents || []).slice(0, 9)
  ];

  return event;
}

export function isProfileRateLimited(
  profile: ClaudeProfile
): { limited: boolean; type?: 'session' | 'weekly'; resetAt?: Date } {
  if (!profile?.rateLimitEvents?.length) return { limited: false };

  const now = new Date();
  const latestEvent = profile.rateLimitEvents[0];  // newest first

  if (latestEvent.resetAt > now) {
    return { limited: true, type: latestEvent.type, resetAt: latestEvent.resetAt };
  }
  return { limited: false };
}

export function clearRateLimitEvents(profile: ClaudeProfile): void {
  profile.rateLimitEvents = [];
}
```

---

## 6. Reset Time Parsing

**File:** `apps/desktop/src/main/claude-profile/usage-parser.ts`

```typescript
export function parseResetTime(resetTimeStr: string): Date {
  const now = new Date();

  // Format: "Dec 17 at 6am (Europe/Oslo)" or "Nov 1, 10:59am"
  const dateMatch = resetTimeStr.match(/([A-Za-z]+)\s+(\d+)(?:,|\s+at)?\s*(\d+)?:?(\d+)?(am|pm)?/i);
  if (dateMatch) {
    const [, month, day, hour = '0', minute = '0', ampm = ''] = dateMatch;
    const monthMap: Record<string, number> = {
      'jan': 0, 'feb': 1, 'mar': 2, 'apr': 3, 'may': 4, 'jun': 5,
      'jul': 6, 'aug': 7, 'sep': 8, 'oct': 9, 'nov': 10, 'dec': 11
    };
    const monthNum = monthMap[month.toLowerCase()] ?? now.getMonth();
    let hourNum = parseInt(hour, 10);
    if (ampm.toLowerCase() === 'pm' && hourNum < 12) hourNum += 12;
    if (ampm.toLowerCase() === 'am' && hourNum === 12) hourNum = 0;

    const resetDate = new Date(now.getFullYear(), monthNum, parseInt(day, 10), hourNum, parseInt(minute, 10));
    if (resetDate < now) resetDate.setFullYear(resetDate.getFullYear() + 1);  // wrap to next year
    return resetDate;
  }

  // Format: "11:59pm" (today or tomorrow)
  const timeOnlyMatch = resetTimeStr.match(/(\d+):?(\d+)?\s*(am|pm)/i);
  if (timeOnlyMatch) {
    const [, hour, minute = '0', ampm] = timeOnlyMatch;
    let hourNum = parseInt(hour, 10);
    if (ampm.toLowerCase() === 'pm' && hourNum < 12) hourNum += 12;
    if (ampm.toLowerCase() === 'am' && hourNum === 12) hourNum = 0;

    const resetDate = new Date(now.getFullYear(), now.getMonth(), now.getDate(), hourNum, parseInt(minute, 10));
    if (resetDate < now) resetDate.setDate(resetDate.getDate() + 1);  // push to tomorrow
    return resetDate;
  }

  // Fallback heuristics
  const isWeekly = resetTimeStr.toLowerCase().includes('week') || /[a-z]{3}\s+\d+/i.test(resetTimeStr);
  if (isWeekly) return new Date(now.getTime() + 7 * 24 * 60 * 60 * 1000);
  return new Date(now.getTime() + 5 * 60 * 60 * 1000);  // 5 hours for session
}
```

---

## 7. Profile Scoring Algorithm

**File:** `apps/desktop/src/main/services/sdk-session-recovery-coordinator.ts`

```typescript
// Scoring constants
const OPERATION_PENALTY_POINTS = 15;  // per active operation on a profile
const RATE_LIMIT_PENALTY_POINTS = 5;  // per historical rate limit hit

async selectBestProfile(
  excludeProfileId?: string
): Promise<{ profileId: string; profileName: string } | null> {
  const allProfilesUsage = await usageMonitor.getAllProfilesUsage();

  const now = new Date();
  const candidates: Array<{ profileId: string; profileName: string; score: number }> = [];

  for (const profile of allProfilesUsage.allProfiles) {
    if (excludeProfileId && profile.profileId === excludeProfileId) continue;
    if (!profile.isAuthenticated) continue;
    if (profile.isRateLimited) continue;

    // Skip profiles in coordinator-tracked cooldown
    const cooldown = this.profileCooldowns.get(profile.profileId);
    if (cooldown && cooldown.cooldownUntil > now) continue;

    // Skip profiles that exceeded max consecutive rate limits
    if (cooldown && cooldown.rateLimitCount >= this.config.maxConsecutiveRateLimits) continue;

    const operationsOnProfile = this.getOperationsByProfile(profile.profileId).length;

    // score = baseAvailabilityScore - (activeOps * 15) - (pastRateLimits * 5)
    let score = profile.availabilityScore;
    score -= operationsOnProfile * OPERATION_PENALTY_POINTS;
    score -= (cooldown?.rateLimitCount ?? 0) * RATE_LIMIT_PENALTY_POINTS;

    candidates.push({ profileId: profile.profileId, profileName: profile.profileName, score });
  }

  // Highest score wins
  candidates.sort((a, b) => b.score - a.score);
  return candidates[0] ?? null;
}
```

**`availabilityScore`** is computed by `UsageMonitor.calculateAvailabilityScore()` (100 = fully available, 0 = rate limited or unauthenticated). Exact formula not included here but the field is populated in `ProfileUsageSummary` for each profile.

---

## 8. Centralized Recovery Coordinator (Deprecated)

**File:** `apps/desktop/src/main/services/sdk-session-recovery-coordinator.ts`

> **Note:** This class is deprecated. The canonical replacement is `ClaudeOperationRegistry` in `operation-registry.ts`. Keep the architecture for reference; use the registry pattern for new code.

### Configuration

```typescript
export interface RecoveryCoordinatorConfig {
  cooldownPeriodMs: number;            // default 60_000 (1 minute)
  maxConsecutiveRateLimits: number;    // default 3
  notificationBatchWindowMs: number;  // default 2_000
  maxNotificationsPerBatch: number;   // default 5
}
```

### Cooldown tracking

```typescript
interface ProfileCooldown {
  profileId: string;
  rateLimitedAt: Date;
  cooldownUntil: Date;          // rateLimitedAt + cooldownPeriodMs
  rateLimitCount: number;       // increments each time this profile hits a limit
}

private recordProfileCooldown(profileId: string): void {
  const existing = this.profileCooldowns.get(profileId);
  const now = new Date();
  const cooldown: ProfileCooldown = {
    profileId,
    rateLimitedAt: now,
    cooldownUntil: new Date(now.getTime() + this.config.cooldownPeriodMs),
    rateLimitCount: (existing?.rateLimitCount ?? 0) + 1,
  };
  this.profileCooldowns.set(profileId, cooldown);
}
```

### Rate limit handling flow

```typescript
async handleRateLimit(
  operationId: string,
  rateLimitedProfileId: string
): Promise<{ profileId: string; profileName: string; reason: ProfileAssignmentReason } | null> {
  // 1. Record cooldown for the offending profile
  this.recordProfileCooldown(rateLimitedProfileId);

  // 2. Select best alternative profile (excludes rate-limited profile)
  const newProfile = await this.selectBestProfile(rateLimitedProfileId);

  if (!newProfile) {
    // All profiles blocked — emit 'queue-blocked' event and notify UI
    this.queueNotification('blocked', { reason: 'All profiles at capacity or in cooldown', ... });
    this.emit('queue-blocked', { reason: 'no_profiles_available', operationId });
    return null;
  }

  // 3. Update operation's profile assignment
  operation.profileId = newProfile.profileId;
  operation.profileName = newProfile.profileName;

  // 4. Queue swap notification (batched, not immediate)
  this.queueNotification('profile-swap', { operationId, fromProfileId: rateLimitedProfileId, ... });

  return { profileId: newProfile.profileId, profileName: newProfile.profileName, reason: 'reactive' };
}
```

---

## 9. Notification Batching

**File:** `apps/desktop/src/main/services/sdk-session-recovery-coordinator.ts`

Prevents UI spam when many operations hit rate limits simultaneously.

```typescript
private queueNotification(type: NotificationType, data: unknown): void {
  this.pendingNotifications.push({ type, data, timestamp: new Date() });

  // Single timer; ignored if already running
  if (!this.notificationBatchTimeout) {
    this.notificationBatchTimeout = setTimeout(
      () => this.flushNotifications(),
      this.config.notificationBatchWindowMs   // 2 seconds
    );
  }
}

private flushNotifications(): void {
  this.notificationBatchTimeout = null;
  const swaps = this.pendingNotifications.filter(n => n.type === 'profile-swap');
  const blocked = this.pendingNotifications.filter(n => n.type === 'blocked');

  // Cap swap notifications to maxNotificationsPerBatch (default 5)
  const toSend = swaps.slice(0, this.config.maxNotificationsPerBatch);
  for (const notification of toSend) {
    safeSendToRenderer(this.getMainWindow, IPC_CHANNELS.QUEUE_PROFILE_SWAPPED, notification.data);
  }

  // Only send the most recent 'blocked' notification
  if (blocked.length > 0) {
    safeSendToRenderer(this.getMainWindow, IPC_CHANNELS.QUEUE_BLOCKED_NO_PROFILES, blocked[blocked.length - 1].data);
  }

  this.pendingNotifications = [];
}
```

**Notification types:**
- `'profile-swap'` — sent via `IPC_CHANNELS.QUEUE_PROFILE_SWAPPED`
- `'blocked'` — sent via `IPC_CHANNELS.QUEUE_BLOCKED_NO_PROFILES`
- `'rate-limit'` — defined but not sent in `flushNotifications()` (captured as event instead)

---

## 10. ClaudeOperationRegistry (Current Replacement)

**File:** `apps/desktop/src/main/claude-profile/operation-registry.ts`

This is the canonical implementation. It adds restart functions and event-driven profile migration.

### Operation type

```typescript
export type OperationType =
  | 'spec-creation' | 'task-execution' | 'pr-review' | 'mr-review'
  | 'insights' | 'roadmap' | 'changelog' | 'ideation' | 'triage' | 'other';

export interface RegisteredOperation {
  id: string;
  type: OperationType;
  profileId: string;
  profileName: string;
  startedAt: Date;
  metadata?: Record<string, unknown>;
  /** Must return true on success. May re-register the operation (creates new object). */
  restartFn: (newProfileId: string) => boolean | Promise<boolean>;
  stopFn?: () => void | Promise<void>;
}
```

### Registration

```typescript
registerOperation(
  id: string,
  type: OperationType,
  profileId: string,
  profileName: string,
  restartFn: RegisteredOperation['restartFn'],
  options?: { stopFn?: ...; metadata?: Record<string, unknown> }
): void
```

### Profile migration (proactive swap)

```typescript
async restartOperationsOnProfile(
  oldProfileId: string,
  newProfileId: string,
  newProfileName: string
): Promise<number> {
  const operations = this.getOperationsByProfile(oldProfileId);

  for (const op of operations) {
    // 1. Call stopFn if provided
    if (op.stopFn) await op.stopFn();

    // 2. Call restartFn — may re-register the operation internally
    const success = await op.restartFn(newProfileId);

    if (success) {
      // 3. Update profile on registry entry (handles both re-register and in-place patterns)
      this.updateOperationProfile(op.id, newProfileId, newProfileName);

      this.emit('operation-restarted', op.id, oldProfileId, newProfileId);
    }
  }

  this.emit('operations-restarted', restartedCount, oldProfileId, newProfileId);
  return restartedCount;
}
```

**Object reference stability note:** `restartFn` implementations (like `AgentManager`) may call `registerOperation()` again, which replaces the `Map` entry with a new object. Always use `getOperation(id)` after a restart; never hold long-lived references to `RegisteredOperation` objects.

### Events emitted

```typescript
'operation-registered'    // (operation: RegisteredOperation)
'operation-unregistered'  // (operationId: string, type: OperationType)
'operation-restarted'     // (operationId: string, oldProfileId: string, newProfileId: string)
'operations-restarted'    // (count: number, oldProfileId: string, newProfileId: string)
'operation-profile-updated' // (operationId: string, oldProfileId: string, newProfileId: string)
```

Type-safe subscription helpers: `onOperationRegistered`, `onOperationUnregistered`, `onOperationRestarted`, `onOperationsRestarted`, `onOperationProfileUpdated` — all return an unsubscribe function `() => void`.

---

## 11. Build Pipeline Failure Classification (RecoveryManager)

**File:** `apps/desktop/src/main/ai/orchestration/recovery-manager.ts`

Used by the autonomous build pipeline (not profile management). Relevant for Golem's own ticket-failure logic.

```typescript
export type FailureType =
  | 'broken_build'         // syntax error, compilation error, module not found, parse error
  | 'verification_failed'  // expected, assertion, test failed, status code
  | 'circular_fix'         // same error repeated >= 3 times (detected separately via isCircularFix())
  | 'context_exhausted'    // context, token limit, maximum length
  | 'rate_limited'         // 429, rate limit, too many requests
  | 'auth_failure'         // 401, unauthorized, auth
  | 'unknown';

classifyFailure(error: string, subtaskId: string): FailureType {
  const lower = error.toLowerCase();

  const buildErrors = ['syntax error', 'compilation error', 'module not found',
    'import error', 'cannot find module', 'unexpected token', 'indentation error', 'parse error'];
  if (buildErrors.some(e => lower.includes(e))) return 'broken_build';

  const verificationErrors = ['verification failed', 'expected', 'assertion', 'test failed', 'status code'];
  if (verificationErrors.some(e => lower.includes(e))) return 'verification_failed';

  if (lower.includes('context') || lower.includes('token limit') || lower.includes('maximum length'))
    return 'context_exhausted';

  if (lower.includes('429') || lower.includes('rate limit') || lower.includes('too many requests'))
    return 'rate_limited';

  if (lower.includes('401') || lower.includes('unauthorized') || lower.includes('auth'))
    return 'auth_failure';

  return 'unknown';
}
```

### Recovery action decision table

```typescript
async determineRecoveryAction(
  subtaskId: string,
  error: string,
  maxRetries: number,
): Promise<RecoveryAction> {
  const failureType = this.classifyFailure(error, subtaskId);
  const attemptCount = await this.getAttemptCount(subtaskId);
  const circular = await this.isCircularFix(subtaskId);

  if (circular)               → escalate  ("same error repeated 3+ times")
  if (attemptCount >= maxRetries) → skip
  if (failureType === 'rate_limited')     → retry   ("back-off")
  if (failureType === 'auth_failure')     → escalate ("requires credential refresh")
  if (failureType === 'context_exhausted') → retry  ("fresh context")
  default                     → retry
}
```

**RecoveryAction shape:**
```typescript
export interface RecoveryAction {
  action: 'rollback' | 'retry' | 'skip' | 'escalate';
  target: string;   // subtaskId or commit hash
  reason: string;
}
```

### Circular fix detection

```typescript
const ATTEMPT_WINDOW_MS = 2 * 60 * 60 * 1_000;   // 2 hours
const MAX_ATTEMPTS_PER_SUBTASK = 50;
const CIRCULAR_FIX_THRESHOLD = 3;                  // same error hash 3+ times = circular

// Hash function (not cryptographic — just for dedup)
function simpleHash(str: string): string {
  let hash = 0;
  const normalized = str.toLowerCase().trim();
  for (let i = 0; i < normalized.length; i++) {
    const char = normalized.charCodeAt(i);
    hash = ((hash << 5) - hash + char) | 0;
  }
  return hash.toString(36);
}

async isCircularFix(subtaskId: string): Promise<boolean> {
  const recent = attempts.filter(a => new Date(a.timestamp).getTime() > (Date.now() - ATTEMPT_WINDOW_MS));
  const hashCounts = new Map<string, number>();
  for (const attempt of recent) {
    const count = (hashCounts.get(attempt.errorHash) ?? 0) + 1;
    hashCounts.set(attempt.errorHash, count);
    if (count >= CIRCULAR_FIX_THRESHOLD) return true;
  }
  return false;
}
```

### Checkpoint format (build-progress.txt)

```
# Build Progress Checkpoint
# Generated: 2026-03-29T00:00:00.000Z

spec_id: spec-001
phase: implementation
last_completed_subtask: SUBTASK-003
total_subtasks: 12
completed_subtasks: 3
stuck_subtasks: none
is_complete: false
```

Attempt history stored as JSON at `<specDir>/memory/attempt_history.json`:
```json
{
  "subtasks": {
    "SUBTASK-001": [
      { "timestamp": "...", "error": "...(500 char max)...", "failureType": "broken_build", "errorHash": "a3f9b" }
    ]
  },
  "stuckSubtasks": [],
  "metadata": { "createdAt": "...", "lastUpdated": "..." }
}
```

---

## 12. Proactive Swap Logic

**File:** `apps/desktop/src/main/claude-profile/usage-monitor.ts`

The `UsageMonitor` polls usage at 60-second intervals. When an active profile nears limits, it calls `restartOperationsOnProfile()` on the registry.

Key thresholds (from `AutoSwitchSettings`):
- `sessionThreshold: 95` — swap when session usage >= 95%
- `weeklyThreshold: 99` — swap when weekly usage >= 99%
- `enabled: true` — master toggle
- `proactiveSwapEnabled: true` — enables before-limit swapping
- `autoSwitchOnRateLimit: true` — enables reactive swapping on detected rate limits
- `usageCheckInterval: 30000` — polling interval in ms (overridden by 60s in production)

**Adaptive cache TTL:** When active profile usage is high (session > 80% or weekly > 90%), inactive profile usage cache TTL drops from 5 minutes to 60 seconds to ensure swap candidates have fresh availability data.

**Request coalescing:** `getAllProfilesUsage()` stores an in-flight promise and returns it to all concurrent callers to avoid burst API calls.

**API cooldowns tracked in UsageMonitor:**
- General API failures: `API_FAILURE_COOLDOWN_MS = 2 minutes`
- Auth failures (swap loop protection): `AUTH_FAILURE_COOLDOWN_MS = 5 minutes`
- HTTP 429 rate-limit errors: `RATE_LIMIT_COOLDOWN_MS = 10 minutes`

---

## 13. Clean Profile Environment

**File:** `apps/desktop/src/main/rate-limit-detector.ts`

Critical for multi-account switching — must clear cached credentials when switching profiles:

```typescript
export function ensureCleanProfileEnv(env: Record<string, string>): Record<string, string> {
  if (env.CLAUDE_CONFIG_DIR) {
    // Clear both to prevent SDK from using stale credentials from parent shell
    return {
      ...env,
      CLAUDE_CODE_OAUTH_TOKEN: '',   // Must be empty string, not deleted
      ANTHROPIC_API_KEY: ''          // Prevents API-key mode from overriding config dir
    };
  }
  return env;
}
```

**Why both must be cleared:** `CLAUDE_CODE_OAUTH_TOKEN` contains cached OAuth tokens that bypass the config dir. `ANTHROPIC_API_KEY` from the parent shell would cause Claude to operate in API-key mode instead of Claude Max/OAuth mode.

---

## 14. getBestAvailableProfileEnv() — Pre-flight Profile Selection

**File:** `apps/desktop/src/main/rate-limit-detector.ts`

Call this before spawning any SDK subprocess to automatically select the best available profile and persist the swap:

```typescript
export function getBestAvailableProfileEnv(): BestProfileEnvResult {
  const activeProfile = profileManager.getActiveProfile();

  // Check for explicit rate limit (from previous API error recording)
  const rateLimitStatus = profileManager.isProfileRateLimited(activeProfile.id);

  // Check for capacity (100% weekly usage)
  const isAtCapacity = activeProfile.usage?.weeklyUsagePercent !== undefined &&
                       activeProfile.usage.weeklyUsagePercent >= 100;

  if (rateLimitStatus.limited || isAtCapacity) {
    const bestProfile = profileManager.getBestAvailableProfile(activeProfile.id);

    if (bestProfile) {
      // Persist the swap — updates global active profile
      profileManager.setActiveProfile(bestProfile.id);

      // Fire-and-forget usage refresh to update UI
      usageMonitor.getAllProfilesUsage(true).then(...);

      return {
        env: ensureCleanProfileEnv(profileManager.getProfileEnv(bestProfile.id)),
        profileId: bestProfile.id,
        profileName: bestProfile.name,
        wasSwapped: true,
        swapReason: rateLimitStatus.limited ? 'rate_limited' : 'at_capacity',
        originalProfile: { id: activeProfile.id, name: activeProfile.name }
      };
    }
  }

  return {
    env: ensureCleanProfileEnv(profileManager.getActiveProfileEnv()),
    profileId: activeProfile.id,
    profileName: activeProfile.name,
    wasSwapped: false
  };
}
```

**BestProfileEnvResult:**
```typescript
export interface BestProfileEnvResult {
  env: Record<string, string>;
  profileId: string;
  profileName: string;
  wasSwapped: boolean;
  swapReason?: 'rate_limited' | 'at_capacity' | 'proactive';
  originalProfile?: { id: string; name: string };
}
```

---

## 15. UI Rate Limit Store

**File:** `apps/desktop/src/renderer/stores/rate-limit-store.ts`

Zustand store managing two separate modal states: one for terminal (CLI subprocess) rate limits and one for SDK (background operation) rate limits. Both modals remain dismissible with the rate limit info persisted so the user can reopen via a sidebar indicator.

```typescript
interface RateLimitState {
  isModalOpen: boolean;
  rateLimitInfo: RateLimitInfo | null;
  isSDKModalOpen: boolean;
  sdkRateLimitInfo: SDKRateLimitInfo | null;
  hasPendingRateLimit: boolean;         // persists after close for sidebar indicator
  pendingRateLimitType: 'terminal' | 'sdk' | null;
}
```

`SDKRateLimitInfo` (from `rate-limit-detector.ts`):
```typescript
export interface SDKRateLimitInfo {
  source: 'changelog' | 'task' | 'roadmap' | 'ideation' | 'title-generator' | 'other';
  projectId?: string;
  taskId?: string;
  resetTime?: string;
  limitType?: 'session' | 'weekly';
  profileId: string;
  profileName?: string;
  suggestedProfile?: { id: string; name: string };
  detectedAt: Date;
  originalError?: string;
  wasAutoSwapped?: boolean;
  swappedToProfile?: { id: string; name: string };
  swapReason?: 'proactive' | 'reactive';
}
```

---

## 16. Key Test Patterns

**File:** `apps/desktop/src/main/__tests__/rate-limit-auto-recovery.test.ts`

### Auto-swap conditions (all must be true for reactive auto-swap)

```typescript
const shouldAutoRecover = settings.enabled && settings.autoSwitchOnRateLimit;
// AND: exit code must be non-zero
// AND: a best alternative profile must exist
```

### Swap count enforcement (prevent infinite loops)

```typescript
const MAX_SWAPS = 2;
// taskContext.swapCount is incremented each swap
// if swapCount >= MAX_SWAPS: stop retrying, show manual intervention modal
```

### Event chain: rate-limit → swap → restart

```
'sdk-rate-limit' emitted (with rateLimitInfo, wasAutoSwapped, swapReason)
  → 'auto-swap-restart-task' emitted (taskId, newProfileId)
```

---

## Summary — Golem Adaptation Notes

| Aperant concept | Golem equivalent |
|----------------|-----------------|
| `ClaudeProfile` with `rateLimitEvents[]` | `config.json` + per-session metadata |
| `UsageMonitor` polling loop | Could be a background thread in `supervisor.py` |
| `ClaudeOperationRegistry` with `restartFn` | `supervisor.py` `supervised_session()` retry logic |
| `ProfileCooldown` tracking | Add to session state in `.golem/sessions/<id>/session.json` |
| Notification batching | Extend `EventBus` with a debounce/batch backend |
| `classifyFailure()` in `RecoveryManager` | Extend `FailureType` in `supervisor.py` stall detection |
| `simpleHash()` + circular fix detection | Add to `supervisor.py` circuit breaker logic |
| `build-progress.txt` checkpoint | `.golem/sessions/<id>/progress.log` already exists |
| `attempt_history.json` | Could be stored alongside `events.jsonl` |
| `ensureCleanProfileEnv()` | `sdk_env()` in `config.py` (already clears `ANTHROPIC_API_KEY`) |
