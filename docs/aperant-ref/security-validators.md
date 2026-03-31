# Aperant Security Validators — Reference

Extracted from `F:\Tools\External\Aperant\apps\desktop\src\main\ai\security\`.
All snippets are verbatim from the source. Use this to reimplement the same
security model in Python (or any language).

---

## Table of Contents

1. [Security Model Overview](#1-security-model-overview)
2. [ValidationResult Pattern](#2-validationresult-pattern)
3. [Denylist — BLOCKED_COMMANDS](#3-denylist--blocked_commands)
4. [Command Parser](#4-command-parser)
5. [Main Security Hook — bashSecurityHook](#5-main-security-hook--bashsecurityhook)
6. [Validators Registry](#6-validators-registry)
7. [Filesystem Validators (rm, chmod)](#7-filesystem-validators-rm-chmod)
8. [Git Validators](#8-git-validators)
9. [Process Validators (pkill, kill, killall)](#9-process-validators-pkill-kill-killall)
10. [Shell Interpreter Validators (bash, sh, zsh -c)](#10-shell-interpreter-validators-bash-sh-zsh--c)
11. [Database Validators](#11-database-validators)
12. [Path Containment](#12-path-containment)
13. [Config Path Validator](#13-config-path-validator)
14. [Tool Input Validator](#14-tool-input-validator)
15. [Secret Scanner](#15-secret-scanner)
16. [Security Profile](#16-security-profile)
17. [API Key Validation Service](#17-api-key-validation-service)

---

## 1. Security Model Overview

**File:** `bash-validator.ts`

```
Security model: DENYLIST-based (allow-by-default)
- All commands are allowed unless explicitly blocked
- A static set of truly dangerous commands (BLOCKED_COMMANDS) is always denied
- Per-command validators run for known sensitive commands to validate
  dangerous usage patterns within otherwise-allowed commands

Flow:
  Command comes in ->
    1. Is command name in BLOCKED_COMMANDS? -> DENY with reason
    2. Does command have a validator in VALIDATORS? -> Run validator -> DENY or ALLOW
    3. Otherwise -> ALLOW
```

The old model was allowlist-based (SecurityProfile with command sets). The
`SecurityProfile` type is kept for backward compatibility but is no longer used
for allow/deny decisions. The profile argument in all public functions is
ignored (`_profile`).

---

## 2. ValidationResult Pattern

**File:** `bash-validator.ts` and `denylist.ts`

```typescript
/** Validation result: [isAllowed, reason] */
export type ValidationResult = [boolean, string];

/** A validator function that checks a command segment */
export type ValidatorFunction = (commandSegment: string) => ValidationResult;
```

Convention:
- `[true, '']` — allowed (empty reason string)
- `[false, 'Human-readable reason']` — denied

Used consistently across every validator function.

**Tool-level variant** (`tool-input-validator.ts`):

```typescript
/** Result: [isValid, errorMessage | null] */
export type ToolValidationResult = [boolean, string | null];
```

---

## 3. Denylist — BLOCKED_COMMANDS

**File:** `denylist.ts`

```typescript
/**
 * Commands that are never permitted regardless of project profile.
 * Criteria: system destruction, privilege escalation, firewall/network
 * infrastructure, OS service/scheduler/user-account management, physical
 * machine control.
 */
export const BLOCKED_COMMANDS: Set<string> = new Set([
  // System shutdown / reboot
  'shutdown', 'reboot', 'halt', 'poweroff', 'init',

  // Disk formatting / partition management (catastrophic data loss)
  'mkfs', 'fdisk', 'parted', 'gdisk',
  'dd', // raw disk write — too dangerous for autonomous agents

  // Privilege escalation
  'sudo', 'su', 'doas',
  'chown', // changing file ownership requires elevated context

  // Firewall / network infrastructure
  'iptables', 'ip6tables', 'nft', 'ufw',

  // Network scanning / exploitation primitives
  'nmap',

  // System service management
  'systemctl', 'service',

  // Scheduled tasks
  'crontab',

  // Mount / unmount
  'mount', 'umount',

  // User / group account management
  'useradd', 'userdel', 'usermod',
  'groupadd', 'groupdel',
  'passwd', 'visudo',
]);

/**
 * Check whether a command is blocked by the static denylist.
 * Returns [false, reason] if blocked, [true, ''] if allowed.
 */
export function isCommandBlocked(command: string): ValidationResult {
  if (BLOCKED_COMMANDS.has(command)) {
    return [
      false,
      `Command '${command}' is blocked for security reasons (system-level command not permitted for autonomous agents)`,
    ];
  }
  return [true, ''];
}
```

---

## 4. Command Parser

**File:** `command-parser.ts`

### 4.1 Cross-platform basename

```typescript
/**
 * Extract the basename from a path in a cross-platform way.
 * Handles both Windows paths (C:\dir\cmd.exe) and POSIX paths (/dir/cmd)
 * regardless of the current platform.
 */
export function crossPlatformBasename(filePath: string): string {
  filePath = filePath.replace(/^['"]|['"]$/g, '');
  if (filePath.includes('\\') || (filePath.length >= 2 && filePath[1] === ':')) {
    return path.win32.basename(filePath);
  }
  return path.posix.basename(filePath);
}
```

### 4.2 Windows path detection

```typescript
/**
 * Windows paths with backslashes cause issues with shlex-style splitting
 * because backslashes are interpreted as escape characters in POSIX mode.
 */
export function containsWindowsPath(commandString: string): boolean {
  return /[A-Za-z]:\\|\\[A-Za-z][A-Za-z0-9_\\/]/.test(commandString);
}
```

### 4.3 shlex-style split

```typescript
/**
 * shlex-style split for shell command strings.
 * Respects single/double quotes and escape characters.
 * Throws on unclosed quotes (similar to Python's shlex.split).
 */
function shlexSplit(input: string): string[] {
  const tokens: string[] = [];
  let current = '';
  let i = 0;
  let inSingle = false;
  let inDouble = false;

  while (i < input.length) {
    const ch = input[i];

    if (inSingle) {
      if (ch === "'") { inSingle = false; } else { current += ch; }
      i++; continue;
    }
    if (inDouble) {
      if (ch === '\\' && i + 1 < input.length) {
        const next = input[i + 1];
        if (next === '"' || next === '\\' || next === '$' || next === '`' || next === '\n') {
          current += next; i += 2; continue;
        }
        current += ch; i++; continue;
      }
      if (ch === '"') { inDouble = false; } else { current += ch; }
      i++; continue;
    }

    if (ch === '\\' && i + 1 < input.length) { current += input[i + 1]; i += 2; continue; }
    if (ch === "'") { inSingle = true; i++; continue; }
    if (ch === '"') { inDouble = true; i++; continue; }
    if (ch === ' ' || ch === '\t' || ch === '\n') {
      if (current.length > 0) { tokens.push(current); current = ''; }
      i++; continue;
    }
    current += ch; i++;
  }

  if (inSingle || inDouble) throw new Error('Unclosed quote');
  if (current.length > 0) tokens.push(current);
  return tokens;
}
```

### 4.4 Fallback extractor (for malformed/Windows commands)

```typescript
/**
 * Fallback command extraction when shlexSplit fails.
 * Uses regex to extract command names from potentially malformed commands.
 * More permissive than shlex but ensures security validation can proceed.
 */
function fallbackExtractCommands(commandString: string): string[] {
  const commands: string[] = [];
  const parts = commandString.split(/\s*(?:&&|\|\||\|)\s*|;\s*/);

  for (let part of parts) {
    part = part.trim();
    if (!part) continue;

    // Skip variable assignments at the start (VAR=value cmd)
    while (/^[A-Za-z_][A-Za-z0-9_]*=\S*\s+/.test(part)) {
      part = part.replace(/^[A-Za-z_][A-Za-z0-9_]*=\S*\s+/, '');
    }
    if (!part) continue;

    const firstTokenMatch = part.match(/^(?:"([^"]+)"|'([^']+)'|([^\s]+))/);
    if (!firstTokenMatch) continue;

    const firstToken = firstTokenMatch[1] ?? firstTokenMatch[2] ?? firstTokenMatch[3];
    if (!firstToken) continue;

    let cmd = crossPlatformBasename(firstToken);
    cmd = cmd.replace(/\.(exe|cmd|bat|ps1|sh)$/i, ''); // Remove Windows extensions
    cmd = cmd.replace(/^["'\\/]+/, '');

    if (cmd.includes('(') || cmd.includes(')') || cmd.includes('.')) continue;
    if (cmd && !SHELL_KEYWORDS.has(cmd.toLowerCase())) commands.push(cmd);
  }

  return commands;
}
```

### 4.5 Split compound command into segments

```typescript
/**
 * Split a compound command into individual command segments.
 * Handles command chaining (&&, ||, ;) but not pipes.
 */
export function splitCommandSegments(commandString: string): string[] {
  const segments = commandString.split(/\s*(?:&&|\|\|)\s*/);
  const result: string[] = [];
  for (const segment of segments) {
    const subSegments = segment.split(/(?<!["'])\s*;\s*(?!["'])/);
    for (const sub of subSegments) {
      const trimmed = sub.trim();
      if (trimmed) result.push(trimmed);
    }
  }
  return result;
}
```

### 4.6 Extract command names from compound shell string

```typescript
const SHELL_OPERATORS = new Set(['|', '||', '&&', '&']);
const SHELL_STRUCTURE_TOKENS = new Set([
  'if', 'then', 'else', 'elif', 'fi', 'for', 'while', 'until',
  'do', 'done', 'case', 'esac', 'in', '!', '{', '}', '(', ')', 'function',
]);
const REDIRECT_TOKENS = new Set(['<<', '<<<', '>>', '>', '<', '2>', '2>&1', '&>']);

/**
 * Extract command names from a shell command string.
 * Handles pipes, &&, ||, ;, and subshells.
 * Returns the base command names (without paths).
 * Falls back to regex-based extraction on Windows or malformed commands.
 */
export function extractCommands(commandString: string): string[] {
  if (containsWindowsPath(commandString)) {
    const fallbackCommands = fallbackExtractCommands(commandString);
    if (fallbackCommands.length > 0) return fallbackCommands;
  }

  const commands: string[] = [];
  const segments = commandString.split(/(?<!["'])\s*;\s*(?!["'])/);

  for (const rawSegment of segments) {
    const segment = rawSegment.trim();
    if (!segment) continue;

    let tokens: string[];
    try {
      tokens = shlexSplit(segment);
    } catch {
      const fallbackCommands = fallbackExtractCommands(commandString);
      if (fallbackCommands.length > 0) return fallbackCommands;
      return [];
    }

    if (tokens.length === 0) continue;
    let expectCommand = true;

    for (const token of tokens) {
      if (SHELL_OPERATORS.has(token)) { expectCommand = true; continue; }
      if (SHELL_STRUCTURE_TOKENS.has(token)) continue;
      if (token.startsWith('-')) continue;
      if (token.includes('=') && !token.startsWith('=')) continue; // VAR=value
      if (REDIRECT_TOKENS.has(token)) continue;

      if (expectCommand) {
        const cmd = crossPlatformBasename(token);
        commands.push(cmd);
        expectCommand = false;
      }
    }
  }

  return commands;
}
```

### 4.7 Find segment containing a specific command

```typescript
/**
 * Find the specific command segment that contains the given command.
 */
export function getCommandForValidation(cmd: string, segments: string[]): string {
  for (const segment of segments) {
    const segmentCommands = extractCommands(segment);
    if (segmentCommands.includes(cmd)) return segment;
  }
  return '';
}
```

---

## 5. Main Security Hook — bashSecurityHook

**File:** `bash-validator.ts`

```typescript
export interface HookInputData {
  toolName?: string;
  toolInput?: Record<string, unknown> | null;
  cwd?: string;
}

interface HookDenyResult {
  hookSpecificOutput: {
    hookEventName: 'PreToolUse';
    permissionDecision: 'deny';
    permissionDecisionReason: string;
  };
}

type HookResult = Record<string, never> | HookDenyResult;

/**
 * Pre-tool-use hook that validates bash commands using a denylist model.
 * Empty object return means ALLOW. HookDenyResult return means DENY.
 */
export function bashSecurityHook(
  inputData: HookInputData,
  _profile?: SecurityProfile,
): HookResult {
  if (inputData.toolName !== 'Bash') {
    return {} as Record<string, never>;  // Allow non-Bash tools
  }

  const toolInput = inputData.toolInput;

  // Guard: null/undefined tool_input
  if (toolInput === null || toolInput === undefined) {
    return { hookSpecificOutput: { hookEventName: 'PreToolUse',
      permissionDecision: 'deny',
      permissionDecisionReason: 'Bash tool_input is null/undefined - malformed tool call' } };
  }

  // Guard: wrong type
  if (typeof toolInput !== 'object' || Array.isArray(toolInput)) {
    return { hookSpecificOutput: { hookEventName: 'PreToolUse',
      permissionDecision: 'deny',
      permissionDecisionReason: `Bash tool_input must be an object, got ${typeof toolInput}` } };
  }

  const command = typeof toolInput.command === 'string' ? toolInput.command : '';
  if (!command) return {} as Record<string, never>;  // Empty command: allow

  // Parse all commands in the string
  const commands = extractCommands(command);
  if (commands.length === 0) {
    return { hookSpecificOutput: { hookEventName: 'PreToolUse',
      permissionDecision: 'deny',
      permissionDecisionReason: `Could not parse command for security validation: ${command}` } };
  }

  const segments = splitCommandSegments(command);

  for (const cmd of commands) {
    // Step 1: Static denylist check
    const [notBlocked, blockReason] = isCommandBlocked(cmd);
    if (!notBlocked) {
      return { hookSpecificOutput: { hookEventName: 'PreToolUse',
        permissionDecision: 'deny',
        permissionDecisionReason: blockReason } };
    }

    // Step 2: Per-command validator
    const validator = VALIDATORS[cmd];
    if (validator) {
      const cmdSegment = getCommandForValidation(cmd, segments) ?? command;
      const [validatorAllowed, validatorReason] = validator(cmdSegment);
      if (!validatorAllowed) {
        return { hookSpecificOutput: { hookEventName: 'PreToolUse',
          permissionDecision: 'deny',
          permissionDecisionReason: validatorReason } };
      }
    }

    // Step 3: Otherwise allow
  }

  return {} as Record<string, never>;
}
```

**Debug helper** (for testing without hook wrapper):

```typescript
export function validateCommand(
  command: string,
  _profile?: SecurityProfile,
): ValidationResult {
  const commands = extractCommands(command);
  if (commands.length === 0) return [false, 'Could not parse command'];

  const segments = splitCommandSegments(command);
  for (const cmd of commands) {
    const [notBlocked, blockReason] = isCommandBlocked(cmd);
    if (!notBlocked) return [false, blockReason];

    const validator = VALIDATORS[cmd];
    if (validator) {
      const cmdSegment = getCommandForValidation(cmd, segments) ?? command;
      const [validatorAllowed, validatorReason] = validator(cmdSegment);
      if (!validatorAllowed) return [false, validatorReason];
    }
  }
  return [true, ''];
}
```

---

## 6. Validators Registry

**File:** `bash-validator.ts`

```typescript
/**
 * Central map of command names -> validator functions.
 * These validators run AFTER the denylist check and examine dangerous usage
 * patterns within otherwise-permitted commands.
 */
export const VALIDATORS: Record<string, ValidatorFunction> = {
  // Filesystem
  rm: validateRmCommand,
  chmod: validateChmodCommand,

  // Git
  git: validateGitCommand,

  // Process management
  pkill: validatePkillCommand,
  kill: validateKillCommand,
  killall: validateKillallCommand,

  // Shell interpreters — validate commands inside -c strings
  bash: validateShellCCommand,
  sh: validateShellCCommand,
  zsh: validateShellCCommand,

  // Databases
  psql: validatePsqlCommand,
  mysql: validateMysqlCommand,
  mysqladmin: validateMysqladminCommand,
  'redis-cli': validateRedisCliCommand,
  mongosh: validateMongoshCommand,
  mongo: validateMongoshCommand,
  dropdb: validateDropdbCommand,
  dropuser: validateDropuserCommand,
};

export function getValidator(commandName: string): ValidatorFunction | undefined {
  return VALIDATORS[commandName];
}
```

---

## 7. Filesystem Validators (rm, chmod)

**File:** `validators/filesystem-validators.ts`

### 7.1 Dangerous chmod patterns

```typescript
/**
 * Dangerous chmod mode patterns — setuid/setgid bits that enable
 * privilege escalation. All other modes (755, 644, 777, +x, o+w, etc.)
 * are allowed.
 */
const DANGEROUS_CHMOD_PATTERNS: RegExp[] = [
  /^[4267]\d{3}$/,  // Numeric modes with special bits: 4xxx (setuid), 2xxx (setgid), 6xxx (both)
  /[+]s/,           // Symbolic setuid/setgid
  /u[+]s/,
  /g[+]s/,
  /o[+]s/,
  /a[+]s/,
];
```

### 7.2 Dangerous rm target patterns

```typescript
const DANGEROUS_RM_PATTERNS: RegExp[] = [
  /^\/$/,        // Root
  /^\.\.$/,      // Parent directory
  /^~$/,         // Home directory
  /^\*$/,        // Wildcard only
  /^\/\*$/,      // Root wildcard
  /^\.\.\//,     // Escaping current directory
  /^\/home$/,    // /home
  /^\/usr$/,     // /usr
  /^\/etc$/,     // /etc
  /^\/var$/,     // /var
  /^\/bin$/,     // /bin
  /^\/lib$/,     // /lib
  /^\/opt$/,     // /opt
];
```

### 7.3 validateChmodCommand

```typescript
/**
 * Validate chmod commands — block setuid/setgid (privilege escalation).
 * Any mode is allowed UNLESS it sets the setuid or setgid special bits.
 */
export function validateChmodCommand(commandString: string): ValidationResult {
  const tokens = shellSplit(commandString);
  if (tokens === null) return [false, 'Could not parse chmod command'];
  if (tokens.length === 0 || tokens[0] !== 'chmod') return [false, 'Not a chmod command'];

  let mode: string | null = null;
  const files: string[] = [];

  for (const token of tokens.slice(1)) {
    if (token === '-R' || token === '--recursive') continue;
    if (token.startsWith('-')) {
      if (/^-[vcf]+$/.test(token)) continue;  // Allow -v -c -f flags
      return [false, `chmod flag '${token}' is not allowed`];
    }
    if (mode === null) { mode = token; }
    else { files.push(token); }
  }

  if (mode === null) return [false, 'chmod requires a mode'];
  if (files.length === 0) return [false, 'chmod requires at least one file'];

  for (const pattern of DANGEROUS_CHMOD_PATTERNS) {
    if (pattern.test(mode)) {
      return [false,
        `chmod mode '${mode}' is not allowed — setuid/setgid bits enable privilege escalation. ` +
        `Use standard permission modes (755, 644, +x, etc.) instead.`];
    }
  }

  return [true, ''];
}
```

### 7.4 validateRmCommand

```typescript
/**
 * Validate rm commands — prevent dangerous deletions.
 */
export function validateRmCommand(commandString: string): ValidationResult {
  const tokens = shellSplit(commandString);
  if (tokens === null) return [false, 'Could not parse rm command'];
  if (tokens.length === 0) return [false, 'Empty rm command'];

  for (const token of tokens.slice(1)) {
    if (token.startsWith('-')) {
      if (token === '--no-preserve-root') {
        return [false, '--no-preserve-root is not allowed for safety'];
      }
      continue;  // Allow -r, -f, -rf, -fr, -v, -i etc.
    }
    for (const pattern of DANGEROUS_RM_PATTERNS) {
      if (pattern.test(token)) {
        return [false, `rm target '${token}' is not allowed for safety`];
      }
    }
  }

  return [true, ''];
}
```

### 7.5 validateInitScript

```typescript
/**
 * Validate init.sh script execution — only allow ./init.sh.
 */
export function validateInitScript(commandString: string): ValidationResult {
  const tokens = shellSplit(commandString);
  if (tokens === null) return [false, 'Could not parse init script command'];
  if (tokens.length === 0) return [false, 'Empty command'];

  const script = tokens[0];
  if (script === './init.sh' || script.endsWith('/init.sh')) return [true, ''];
  return [false, `Only ./init.sh is allowed, got: ${script}`];
}
```

---

## 8. Git Validators

**File:** `validators/git-validators.ts`

### 8.1 Blocked git config keys

```typescript
/**
 * Git config keys that agents must NOT modify.
 * These are identity settings that should inherit from the user's global config.
 */
const BLOCKED_GIT_CONFIG_KEYS = new Set([
  'user.name', 'user.email',
  'author.name', 'author.email',
  'committer.name', 'committer.email',
]);
```

### 8.2 validateGitConfig (sub-validator)

```typescript
/**
 * Validate git config commands — block identity changes.
 */
function validateGitConfig(commandString: string): ValidationResult {
  const tokens = shellSplit(commandString);
  if (tokens === null) return [false, 'Could not parse git command'];
  if (tokens.length < 2 || tokens[0] !== 'git' || tokens[1] !== 'config') return [true, ''];

  // Read-only operations are always allowed
  const readOnlyFlags = new Set(['--get', '--get-all', '--get-regexp', '--list', '-l']);
  for (const token of tokens.slice(2)) {
    if (readOnlyFlags.has(token)) return [true, ''];
  }

  // Extract the config key (first non-option token after "config")
  let configKey: string | null = null;
  for (const token of tokens.slice(2)) {
    if (token.startsWith('-')) continue;
    configKey = token.toLowerCase();
    break;
  }

  if (!configKey) return [true, ''];

  if (BLOCKED_GIT_CONFIG_KEYS.has(configKey)) {
    return [false,
      `BLOCKED: Cannot modify git identity configuration\n\n` +
      `You attempted to set '${configKey}' which is not allowed.\n\n` +
      `WHY: Git identity (user.name, user.email) must inherit from the user's ` +
      `global git configuration. Setting fake identities like 'Test User' breaks ` +
      `commit attribution and causes serious issues.\n\n` +
      `WHAT TO DO: Simply commit without setting any user configuration. ` +
      `The repository will use the correct identity automatically.`];
  }

  return [true, ''];
}
```

### 8.3 validateGitInlineConfig (sub-validator for -c flag)

```typescript
/**
 * Check for blocked config keys passed via git -c flag.
 * Handles both '-c key=value' (space) and '-ckey=value' (no space).
 */
function validateGitInlineConfig(tokens: string[]): ValidationResult {
  let i = 1; // Start after 'git'
  while (i < tokens.length) {
    const token = tokens[i];

    if (token === '-c') {
      if (i + 1 < tokens.length) {
        const configPair = tokens[i + 1];
        if (configPair.includes('=')) {
          const configKey = configPair.split('=')[0].toLowerCase();
          if (BLOCKED_GIT_CONFIG_KEYS.has(configKey)) {
            return [false,
              `BLOCKED: Cannot set git identity via -c flag\n\n` +
              `You attempted to use '-c ${configPair}' which sets a blocked ` +
              `identity configuration.\n\n...`];
          }
        }
        i += 2; continue;
      }
    } else if (token.startsWith('-c') && token.length > 2) {
      // Handle -ckey=value format (no space)
      const configPair = token.slice(2);
      if (configPair.includes('=')) {
        const configKey = configPair.split('=')[0].toLowerCase();
        if (BLOCKED_GIT_CONFIG_KEYS.has(configKey)) {
          return [false, `BLOCKED: Cannot set git identity via -c flag\n\n...`];
        }
      }
    }
    i++;
  }
  return [true, ''];
}
```

### 8.4 validateGitCommand (main entry point)

```typescript
/**
 * Main git validator — checks all git security rules.
 * 1. git -c: Block identity changes via inline config on ANY git command
 * 2. git config: Block identity changes
 * (git commit secret scanning is handled at git hook layer)
 */
export function validateGitCommand(commandString: string): ValidationResult {
  const tokens = shellSplit(commandString);
  if (tokens === null) return [false, 'Could not parse git command'];
  if (tokens.length === 0 || tokens[0] !== 'git') return [true, ''];
  if (tokens.length < 2) return [true, ''];

  // Check -c flags on ANY git command (security bypass prevention)
  const [inlineValid, inlineError] = validateGitInlineConfig(tokens);
  if (!inlineValid) return [false, inlineError];

  // Find the actual subcommand (skip global options: -c, -C, --git-dir, --work-tree)
  let subcommand: string | null = null;
  let skipNext = false;
  for (const token of tokens.slice(1)) {
    if (skipNext) { skipNext = false; continue; }
    if (token === '-c' || token === '-C' || token === '--git-dir' || token === '--work-tree') {
      skipNext = true; continue;
    }
    if (token.startsWith('-')) continue;
    subcommand = token;
    break;
  }

  if (!subcommand) return [true, ''];

  if (subcommand === 'config') return validateGitConfig(commandString);

  return [true, ''];
}
```

---

## 9. Process Validators (pkill, kill, killall)

**File:** `validators/process-validators.ts`

### 9.1 Blocked process names

```typescript
/**
 * System-critical process names that must NEVER be killed by autonomous agents.
 * These are stable OS/desktop/infrastructure processes.
 */
const BLOCKED_PROCESS_NAMES = new Set([
  // OS init / system
  'systemd', 'launchd', 'init', 'loginwindow', 'kernel_task', 'kerneltask',
  'containerd', 'dockerd',

  // macOS desktop
  'Finder', 'Dock', 'WindowServer', 'SystemUIServer', 'NotificationCenter',
  'Spotlight', 'mds', 'mds_stores', 'coreaudiod', 'corebrightnessd',
  'securityd', 'opendirectoryd', 'diskarbitrationd',

  // Linux desktop / display
  'Xorg', 'Xwayland', 'gnome-shell', 'kwin', 'kwin_wayland', 'kwin_x11',
  'plasmashell', 'mutter', 'gdm', 'lightdm', 'sddm', 'pulseaudio',
  'pipewire', 'wireplumber', 'dbus-daemon', 'polkitd', 'networkmanager',
  'NetworkManager', 'wpa_supplicant',

  // Windows critical
  'explorer.exe', 'dwm.exe', 'csrss.exe', 'winlogon.exe', 'lsass.exe',
  'services.exe', 'svchost.exe', 'smss.exe', 'wininit.exe',

  // Remote access
  'sshd', 'ssh-agent',

  // Self-protection (don't let agent kill its own host)
  'electron', 'Electron', 'auto-claude', 'Aperant',
]);
```

### 9.2 validatePkillCommand

```typescript
/**
 * Validate pkill commands — block killing system-critical processes.
 * Denylist model: any process can be killed UNLESS it's system-critical.
 * Also blocks -u flag (kill by user — too broad).
 */
export function validatePkillCommand(commandString: string): ValidationResult {
  const tokens = shellSplit(commandString);
  if (tokens === null) return [false, 'Could not parse pkill command'];
  if (tokens.length === 0) return [false, 'Empty pkill command'];

  const flags: string[] = [];
  const args: string[] = [];
  for (const token of tokens.slice(1)) {
    if (token.startsWith('-')) flags.push(token);
    else args.push(token);
  }

  // Block -u (kill by user — too broad)
  for (const flag of flags) {
    if (flag === '-u' || flag.startsWith('-u') || flag === '--euid') {
      return [false, 'pkill -u (kill by user) is not allowed — too broad, affects all processes for a user'];
    }
  }

  if (args.length === 0) return [false, 'pkill requires a process name'];

  let target = args[args.length - 1];
  if (target.includes(' ')) target = target.split(' ')[0]; // -f flag: first word only

  if (BLOCKED_PROCESS_NAMES.has(target)) {
    return [false,
      `Cannot kill system-critical process '${target}'. ` +
      `Killing OS daemons, desktop environment, or remote access processes ` +
      `could crash the system or lock out the user.`];
  }

  return [true, ''];
}
```

### 9.3 validateKillCommand

```typescript
/**
 * Validate kill commands — allow killing by PID (user must know the PID).
 * Blocks: kill -1 (kill all), kill 0 / kill -0
 */
export function validateKillCommand(commandString: string): ValidationResult {
  const tokens = shellSplit(commandString);
  if (tokens === null) return [false, 'Could not parse kill command'];

  for (const token of tokens.slice(1)) {
    if (token === '-1' || token === '0' || token === '-0') {
      return [false, 'kill -1 and kill 0 are not allowed (affects all processes)'];
    }
  }

  return [true, ''];
}
```

### 9.4 validateKillallCommand

```typescript
/**
 * Validate killall commands — same rules as pkill.
 */
export function validateKillallCommand(commandString: string): ValidationResult {
  return validatePkillCommand(commandString);
}
```

---

## 10. Shell Interpreter Validators (bash, sh, zsh -c)

**File:** `validators/shell-validators.ts`

Closes the bypass where `bash -c "sudo ..."` would execute a blocked command.

### 10.1 extractCArgument helper

```typescript
/** Sentinel to distinguish "shellSplit parse failure" from "no -c flag found" */
const PARSE_FAILURE = Symbol('PARSE_FAILURE');

/**
 * Extract the command string from a shell -c invocation.
 * Handles: bash -c 'cmd', bash -c "cmd", combined flags (-xc, -ec, -ic).
 * Returns: string (found), null (no -c flag), PARSE_FAILURE (parse error)
 */
function extractCArgument(commandString: string): string | null | typeof PARSE_FAILURE {
  const tokens = shellSplit(commandString);
  if (tokens === null) return PARSE_FAILURE;
  if (tokens.length < 3) return null;

  for (let i = 0; i < tokens.length; i++) {
    const token = tokens[i];
    const isCFlag =
      token === '-c' ||
      (token.startsWith('-') && !token.startsWith('--') && token.slice(1).includes('c'));

    if (isCFlag && i + 1 < tokens.length) {
      return tokens[i + 1];
    }
  }
  return null;
}
```

### 10.2 validateShellCCommand

```typescript
const SHELL_INTERPRETERS = new Set(['bash', 'sh', 'zsh']);

/**
 * Validate commands inside bash/sh/zsh -c '...' strings.
 * Checks each inner command against BLOCKED_COMMANDS.
 * Recursively validates nested shell invocations.
 */
export function validateShellCCommand(commandString: string): ValidationResult {
  const innerCommand = extractCArgument(commandString);

  if (innerCommand === PARSE_FAILURE) {
    return [false, 'Could not parse shell command'];
  }

  if (innerCommand === null) {
    // Not a -c invocation — block dangerous shell constructs
    const dangerousPatterns = ['<(', '>('];
    for (const pattern of dangerousPatterns) {
      if (commandString.includes(pattern)) {
        return [false, `Process substitution '${pattern}' not allowed in shell commands`];
      }
    }
    return [true, ''];
  }

  const innerCommandNames = extractCommands(innerCommand);

  if (innerCommandNames.length === 0) {
    if (!innerCommand.trim()) return [true, ''];
    return [false, `Could not parse commands inside shell -c: ${innerCommand}`];
  }

  // Check each command against the denylist
  for (const cmdName of innerCommandNames) {
    const [notBlocked, blockReason] = isCommandBlocked(cmdName);
    if (!notBlocked) {
      return [false, `Command '${cmdName}' inside shell -c is blocked: ${blockReason}`];
    }
  }

  // Recursively validate nested shell invocations (e.g., bash -c "sh -c '...'")
  const innerSegments = splitCommandSegments(innerCommand);
  for (const segment of innerSegments) {
    const segmentCommands = extractCommands(segment);
    if (segmentCommands.length > 0) {
      const firstCmd = segmentCommands[0];
      const baseCmd = crossPlatformBasename(firstCmd);
      if (SHELL_INTERPRETERS.has(baseCmd)) {
        const [valid, err] = validateShellCCommand(segment);
        if (!valid) return [false, `Nested shell command not allowed: ${err}`];
      }
    }
  }

  return [true, ''];
}

// Aliases
export const validateBashSubshell = validateShellCCommand;
export const validateShSubshell = validateShellCCommand;
export const validateZshSubshell = validateShellCCommand;
```

---

## 11. Database Validators

**File:** `validators/database-validators.ts`

### 11.1 Destructive SQL patterns

```typescript
const DESTRUCTIVE_SQL_PATTERNS: RegExp[] = [
  /\bDROP\s+(DATABASE|SCHEMA|TABLE|INDEX|VIEW|FUNCTION|PROCEDURE|TRIGGER)\b/i,
  /\bTRUNCATE\s+(TABLE\s+)?\w+/i,
  /\bDELETE\s+FROM\s+\w+\s*(;|$)/i,  // DELETE without WHERE clause
  /\bDROP\s+ALL\b/i,
  /\bDESTROY\b/i,
];
```

### 11.2 Safe database name patterns

```typescript
const SAFE_DATABASE_PATTERNS: RegExp[] = [
  /^test/i, /_test$/i,
  /^dev/i,  /_dev$/i,
  /^local/i, /_local$/i,
  /^tmp/i,  /_tmp$/i,
  /^temp/i, /_temp$/i,
  /^scratch/i, /^sandbox/i,
  /^mock/i, /_mock$/i,
];

function isSafeDatabaseName(dbName: string): boolean {
  for (const pattern of SAFE_DATABASE_PATTERNS) {
    if (pattern.test(dbName)) return true;
  }
  return false;
}

// Returns [isDestructive, matchedText]
function containsDestructiveSql(sql: string): [boolean, string] {
  for (const pattern of DESTRUCTIVE_SQL_PATTERNS) {
    const match = sql.match(pattern);
    if (match) return [true, match[0]];
  }
  return [false, ''];
}
```

### 11.3 validateDropdbCommand

```typescript
/**
 * Validate dropdb commands — only allow dropping test/dev databases.
 */
export function validateDropdbCommand(commandString: string): ValidationResult {
  // Parse tokens, skip flags-with-args: -h/--host, -p/--port, -U/--username,
  // -w/--no-password, -W/--password, --maintenance-db
  // Last non-flag token is dbName
  // ...
  if (isSafeDatabaseName(dbName)) return [true, ''];
  return [false,
    `dropdb '${dbName}' blocked for safety. Only test/dev databases can be dropped autonomously. ` +
    `Safe patterns: test*, *_test, dev*, *_dev, local*, tmp*, temp*, scratch*, sandbox*, mock*`];
}
```

### 11.4 validateDropuserCommand

```typescript
/**
 * Validate dropuser commands — only allow dropping test/dev users.
 */
export function validateDropuserCommand(commandString: string): ValidationResult {
  // Parse tokens, skip flags-with-args: -h/--host, -p/--port, -U/--username,
  // -w/--no-password, -W/--password
  // Last non-flag token is username
  const safeUserPatterns = [
    /^test/i, /_test$/i, /^dev/i, /_dev$/i, /^tmp/i, /^temp/i, /^mock/i,
  ];
  for (const pattern of safeUserPatterns) {
    if (pattern.test(username)) return [true, ''];
  }
  return [false,
    `dropuser '${username}' blocked for safety. Only test/dev users can be dropped autonomously. ` +
    `Safe patterns: test*, *_test, dev*, *_dev, tmp*, temp*, mock*`];
}
```

### 11.5 validatePsqlCommand

```typescript
/**
 * Validate psql commands — block destructive SQL operations.
 * Looks for -c flag (command to execute). Handles both '-c SQL' and '-c"SQL"' formats.
 */
export function validatePsqlCommand(commandString: string): ValidationResult {
  // Look for -c flag
  let sqlCommand: string | null = null;
  for (let i = 0; i < tokens.length; i++) {
    if (tokens[i] === '-c' && i + 1 < tokens.length) { sqlCommand = tokens[i + 1]; break; }
    if (tokens[i].startsWith('-c') && tokens[i].length > 2) { sqlCommand = tokens[i].slice(2); break; }
  }

  if (sqlCommand) {
    const [isDestructive, matched] = containsDestructiveSql(sqlCommand);
    if (isDestructive) {
      return [false,
        `psql command contains destructive SQL: '${matched}'. ` +
        `DROP/TRUNCATE/DELETE operations require manual confirmation.`];
    }
  }
  return [true, ''];
}
```

### 11.6 validateMysqlCommand

```typescript
/**
 * Validate mysql commands — block destructive SQL.
 * Looks for -e / --execute flag.
 */
export function validateMysqlCommand(commandString: string): ValidationResult {
  // Look for: -e 'SQL', -e"SQL", --execute 'SQL'
  if (sqlCommand) {
    const [isDestructive, matched] = containsDestructiveSql(sqlCommand);
    if (isDestructive) {
      return [false,
        `mysql command contains destructive SQL: '${matched}'. ` +
        `DROP/TRUNCATE/DELETE operations require manual confirmation.`];
    }
  }
  return [true, ''];
}
```

### 11.7 validateMysqladminCommand

```typescript
/**
 * Validate mysqladmin commands — block destructive operations.
 */
export function validateMysqladminCommand(commandString: string): ValidationResult {
  const dangerousOps = new Set(['drop', 'shutdown', 'kill']);
  // Check each non-first token (lowercased) against dangerousOps
  for (const token of tokens.slice(1)) {
    if (dangerousOps.has(token.toLowerCase())) {
      return [false,
        `mysqladmin '${token}' is blocked for safety. ` +
        `Destructive operations require manual confirmation.`];
    }
  }
  return [true, ''];
}
```

### 11.8 validateRedisCliCommand

```typescript
/**
 * Validate redis-cli commands — block destructive operations.
 * Skips flags that take args: -h, -p, -a, -n, --pass, --user, -u
 * First non-flag token is the Redis command.
 */
export function validateRedisCliCommand(commandString: string): ValidationResult {
  const dangerousRedisCommands = new Set([
    'FLUSHALL',   // Deletes ALL data from ALL databases
    'FLUSHDB',    // Deletes all data from current database
    'DEBUG',      // Can crash the server
    'SHUTDOWN',   // Shuts down the server
    'SLAVEOF',    // Can change replication
    'REPLICAOF',  // Can change replication
    'CONFIG',     // Can modify server config
    'BGSAVE',     // Can cause disk issues
    'BGREWRITEAOF',
    'CLUSTER',    // Can modify cluster topology
  ]);
  // Check first non-flag token (uppercased) against dangerousRedisCommands
  return [true, ''];
}
```

### 11.9 validateMongoshCommand

```typescript
/**
 * Validate mongosh/mongo commands — block destructive operations.
 * Looks for --eval flag.
 */
export function validateMongoshCommand(commandString: string): ValidationResult {
  const dangerousMongoPatterns: RegExp[] = [
    /\.dropDatabase\s*\(/i,
    /\.drop\s*\(/i,
    /\.deleteMany\s*\(\s*\{\s*\}\s*\)/i,  // deleteMany({}) - deletes all
    /\.remove\s*\(\s*\{\s*\}\s*\)/i,       // remove({}) - deletes all (deprecated)
    /db\.dropAllUsers\s*\(/i,
    /db\.dropAllRoles\s*\(/i,
  ];
  // If --eval flag found, test evalScript against each pattern
  return [true, ''];
}
```

---

## 12. Path Containment

**File:** `path-containment.ts`

```typescript
export interface PathContainmentResult {
  contained: boolean;
  resolvedPath: string;
  reason?: string;
}

/**
 * Normalize a path for consistent comparison.
 * Resolves to absolute path, normalizes separators.
 * Lowercases on Windows for case-insensitive comparison.
 */
function normalizePath(filePath: string, projectDir: string): string {
  const resolved = path.isAbsolute(filePath)
    ? path.normalize(filePath)
    : path.normalize(path.resolve(projectDir, filePath));
  if (isWindows()) return resolved.toLowerCase();
  return resolved;
}

/**
 * Resolve symlinks — falls back to parent dir if file doesn't exist yet.
 */
function resolveSymlinks(filePath: string): string {
  try {
    return fs.realpathSync(filePath);
  } catch {
    const parentDir = path.dirname(filePath);
    try {
      const realParent = fs.realpathSync(parentDir);
      return path.join(realParent, path.basename(filePath));
    } catch {
      return path.normalize(filePath);
    }
  }
}

/**
 * Assert that a file path is contained within the project directory.
 * Blocks: paths outside projectDir (including ../ traversal) and symlink escapes.
 * Throws Error if path escapes the project boundary.
 */
export function assertPathContained(
  filePath: string,
  projectDir: string,
): PathContainmentResult {
  if (!filePath || !projectDir) {
    throw new Error('Path containment check requires both filePath and projectDir');
  }

  const resolvedProjectDir = resolveSymlinks(projectDir);
  const normalizedProjectDir = normalizePath(resolvedProjectDir, resolvedProjectDir);

  const absolutePath = path.isAbsolute(filePath)
    ? filePath
    : path.resolve(resolvedProjectDir, filePath);
  const resolvedPath = resolveSymlinks(absolutePath);
  const normalizedPath = normalizePath(resolvedPath, resolvedProjectDir);

  const projectDirWithSep = normalizedProjectDir.endsWith(path.sep)
    ? normalizedProjectDir
    : normalizedProjectDir + path.sep;

  const isContained =
    normalizedPath === normalizedProjectDir ||
    normalizedPath.startsWith(projectDirWithSep);

  if (!isContained) {
    const reason = `Path '${filePath}' resolves to '${resolvedPath}' which is outside the project directory '${resolvedProjectDir}'`;
    throw new Error(reason);
  }

  return { contained: true, resolvedPath };
}

/**
 * Non-throwing variant — returns result object instead.
 */
export function isPathContained(
  filePath: string,
  projectDir: string,
): PathContainmentResult {
  try {
    return assertPathContained(filePath, projectDir);
  } catch (error) {
    return {
      contained: false,
      resolvedPath: '',
      reason: error instanceof Error ? error.message : String(error),
    };
  }
}
```

---

## 13. Config Path Validator

**File:** `utils/config-path-validator.ts`

Prevents path traversal attacks where a malicious renderer could specify
arbitrary paths (e.g. `/etc` or `C:\Windows\System32\config`).

```typescript
import path from 'path';
import os from 'os';

/**
 * Validate that a config directory path is safe and within expected boundaries.
 *
 * @param configDir - The config directory path to validate (may contain ~)
 * @returns true if the path is safe, false otherwise
 */
export function isValidConfigDir(configDir: string): boolean {
  // Expand ~ to home directory
  const expandedPath = configDir.startsWith('~')
    ? path.join(os.homedir(), configDir.slice(1))
    : configDir;

  const normalizedPath = path.resolve(expandedPath);
  const homeDir = os.homedir();

  const allowedPrefixes = [
    homeDir,
    path.join(homeDir, '.claude'),
    path.join(homeDir, '.claude-profiles'),
  ];

  for (const prefix of allowedPrefixes) {
    const resolvedPrefix = path.resolve(prefix);
    // IMPORTANT: Check with path separator boundary to prevent attacks like
    // /home/alice-malicious passing validation for /home/alice
    if (normalizedPath === resolvedPrefix || normalizedPath.startsWith(resolvedPrefix + path.sep)) {
      return true;
    }
  }

  console.warn('[Config Path Validator] Rejected unsafe configDir path:', configDir,
    '(normalized:', normalizedPath, ')');
  return false;
}
```

Key security detail: always append `path.sep` to the prefix before the
`startsWith` check to prevent prefix-confusion attacks (e.g., `/home/alice`
matching `/home/alice-evil`).

---

## 14. Tool Input Validator

**File:** `tool-input-validator.ts`

```typescript
const TOOL_REQUIRED_KEYS: Record<string, string[]> = {
  Bash:      ['command'],
  Read:      ['file_path'],
  Write:     ['file_path', 'content'],
  Edit:      ['file_path', 'old_string', 'new_string'],
  Glob:      ['pattern'],
  Grep:      ['pattern'],
  WebFetch:  ['url'],
  WebSearch: ['query'],
};

export type ToolValidationResult = [boolean, string | null];

/**
 * Validate tool input structure.
 * Checks: null/undefined, wrong type, missing required keys, type of specific fields.
 */
export function validateToolInput(
  toolName: string,
  toolInput: unknown,
): ToolValidationResult {
  if (toolInput === null || toolInput === undefined) {
    return [false, `${toolName}: tool_input is None (malformed tool call)`];
  }
  if (typeof toolInput !== 'object' || Array.isArray(toolInput)) {
    return [false,
      `${toolName}: tool_input must be dict, got ${Array.isArray(toolInput) ? 'array' : typeof toolInput}`];
  }

  const input = toolInput as Record<string, unknown>;
  const requiredKeys = TOOL_REQUIRED_KEYS[toolName] ?? [];
  const missingKeys = requiredKeys.filter((key) => !(key in input));
  if (missingKeys.length > 0) {
    return [false, `${toolName}: missing required keys: ${missingKeys.join(', ')}`];
  }

  // Bash-specific: command must be non-empty string
  if (toolName === 'Bash') {
    const command = input.command;
    if (typeof command !== 'string') {
      return [false, `Bash: 'command' must be string, got ${typeof command}`];
    }
    if (!command.trim()) {
      return [false, "Bash: 'command' is empty"];
    }
  }

  return [true, null];
}

/**
 * Safely extract tool_input from a tool use block, defaulting to empty object.
 * Accepts either .input or .tool_input field name.
 */
export function getSafeToolInput(
  block: unknown,
  defaultValue: Record<string, unknown> = {},
): Record<string, unknown> {
  if (!block || typeof block !== 'object') return defaultValue;
  const blockObj = block as Record<string, unknown>;
  const toolInput = blockObj.input ?? blockObj.tool_input;
  if (toolInput === null || toolInput === undefined) return defaultValue;
  if (typeof toolInput !== 'object' || Array.isArray(toolInput)) return defaultValue;
  return toolInput as Record<string, unknown>;
}
```

---

## 15. Secret Scanner

**File:** `secret-scanner.ts`

### 15.1 Pattern categories

```typescript
// Generic high-entropy patterns (variable assignments, bearer tokens, base64)
export const GENERIC_PATTERNS: Array<[RegExp, string]> = [
  [/(?:api[_-]?key|apikey|api_secret|secret[_-]?key)\s*[:=]\s*["']([a-zA-Z0-9_-]{32,})["']/i, 'Generic API key assignment'],
  [/(?:access[_-]?token|auth[_-]?token|bearer[_-]?token|token)\s*[:=]\s*["']([a-zA-Z0-9_-]{32,})["']/i, 'Generic access token'],
  [/(?:password|passwd|pwd|pass)\s*[:=]\s*["']([^"']{8,})["']/i, 'Password assignment'],
  [/(?:secret|client_secret|app_secret)\s*[:=]\s*["']([a-zA-Z0-9_/+=]{16,})["']/i, 'Secret assignment'],
  [/["']?[Bb]earer\s+([a-zA-Z0-9_-]{20,})["']?/, 'Bearer token'],
  [/["'][A-Za-z0-9+/]{64,}={0,2}["']/, 'Potential base64-encoded secret'],
];

// Service-specific known formats (OpenAI, Anthropic, AWS, Google, GitHub,
// Stripe, Slack, Discord, Twilio, SendGrid, Mailchimp, NPM, PyPI,
// Supabase/JWT, Linear, Vercel, Heroku, Doppler)
export const SERVICE_PATTERNS: Array<[RegExp, string]> = [
  [/sk-[a-zA-Z0-9]{20,}/, 'OpenAI/Anthropic-style API key'],
  [/sk-ant-[a-zA-Z0-9-]{20,}/, 'Anthropic API key'],
  [/sk-proj-[a-zA-Z0-9-]{20,}/, 'OpenAI project API key'],
  [/AKIA[0-9A-Z]{16}/, 'AWS Access Key ID'],
  // ... (see source for full list)
];

// Private key PEM headers
export const PRIVATE_KEY_PATTERNS: Array<[RegExp, string]> = [
  [/-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY-----/, 'RSA Private Key'],
  [/-----BEGIN\s+OPENSSH\s+PRIVATE\s+KEY-----/, 'OpenSSH Private Key'],
  // ...
];

// Database connection strings with embedded credentials
export const DATABASE_PATTERNS: Array<[RegExp, string]> = [
  [/mongodb(?:\+srv)?:\/\/[^"\s:]+:[^@"\s]+@[^\s"]+/, 'MongoDB Connection String with credentials'],
  [/postgres(?:ql)?:\/\/[^"\s:]+:[^@"\s]+@[^\s"]+/, 'PostgreSQL Connection String with credentials'],
  [/mysql:\/\/[^"\s:]+:[^@"\s]+@[^\s"]+/, 'MySQL Connection String with credentials'],
  [/redis:\/\/[^"\s:]+:[^@"\s]+@[^\s"]+/, 'Redis Connection String with credentials'],
  [/amqp:\/\/[^"\s:]+:[^@"\s]+@[^\s"]+/, 'RabbitMQ Connection String with credentials'],
];

export const ALL_PATTERNS = [...GENERIC_PATTERNS, ...SERVICE_PATTERNS, ...PRIVATE_KEY_PATTERNS, ...DATABASE_PATTERNS];
```

### 15.2 False positive filter

```typescript
const FALSE_POSITIVE_PATTERNS: RegExp[] = [
  /process\.env\./,         // Environment variable references
  /os\.environ/,            // Python env references
  /ENV\[/,                  // Ruby/other env references
  /\$\{[A-Z_]+\}/,         // Shell variable substitution
  /your[-_]?api[-_]?key/i, // Placeholder values
  /xxx+/i,                  // Placeholder
  /placeholder/i,
  /example/i,
  /sample/i,
  /test[-_]?key/i,
  /<[A-Z_]+>/,              // Placeholder like <API_KEY>
  /TODO/, /FIXME/, /CHANGEME/,
  /INSERT[-_]?YOUR/i,
  /REPLACE[-_]?WITH/i,
];

export function isFalsePositive(line: string, matchedText: string): boolean {
  for (const pattern of FALSE_POSITIVE_PATTERNS) {
    if (pattern.test(line)) return true;
  }
  // Skip pure type hints
  if (/^[a-z_]+:\s*str\s*$/i.test(line.trim())) return true;
  // Comments only flagged if matched text is 40+ chars
  const stripped = line.trim();
  if (stripped.startsWith('#') || stripped.startsWith('//') || stripped.startsWith('*')) {
    if (!/[a-zA-Z0-9_-]{40,}/.test(matchedText)) return true;
  }
  return false;
}
```

### 15.3 Core scan functions

```typescript
export interface SecretMatch {
  filePath: string;
  lineNumber: number;
  patternName: string;
  matchedText: string;
  lineContent: string; // trimmed, max 100 chars
}

/** Mask a secret, showing only first few characters. */
export function maskSecret(text: string, visibleChars = 8): string {
  if (text.length <= visibleChars) return text;
  return text.slice(0, visibleChars) + '***';
}

/** Scan file content for potential secrets. */
export function scanContent(content: string, filePath: string): SecretMatch[] {
  const matches: SecretMatch[] = [];
  const lines = content.split('\n');
  for (let lineIdx = 0; lineIdx < lines.length; lineIdx++) {
    const line = lines[lineIdx];
    for (const [pattern, patternName] of ALL_PATTERNS) {
      // Use exec loop with global flag variant to find all matches per line
      const globalPattern = new RegExp(pattern.source,
        pattern.flags.includes('g') ? pattern.flags : pattern.flags + 'g');
      let match: RegExpExecArray | null;
      while ((match = globalPattern.exec(line)) !== null) {
        if (isFalsePositive(line, match[0])) continue;
        matches.push({ filePath, lineNumber: lineIdx + 1, patternName,
          matchedText: match[0], lineContent: line.trim().slice(0, 100) });
      }
    }
  }
  return matches;
}

/** Scan a list of files for secrets. Respects .secretsignore. */
export function scanFiles(files: string[], projectDir?: string): SecretMatch[] {
  const resolvedProjectDir = projectDir ?? process.cwd();
  const customIgnores = loadSecretsIgnore(resolvedProjectDir);
  const allMatches: SecretMatch[] = [];
  for (const filePath of files) {
    if (shouldSkipFile(filePath, customIgnores)) continue;
    const fullPath = path.join(resolvedProjectDir, filePath);
    try {
      const content = fs.readFileSync(fullPath, 'utf-8');
      allMatches.push(...scanContent(content, filePath));
    } catch (err: unknown) {
      const code = (err as NodeJS.ErrnoException).code;
      if (code !== 'ENOENT' && code !== 'EISDIR' && code !== 'EACCES') throw err;
    }
  }
  return allMatches;
}
```

### 15.4 File skip logic

```typescript
// Always-skip dirs/extensions
const DEFAULT_IGNORE_PATTERNS: RegExp[] = [
  /\.git\//, /node_modules\//, /\.venv\//, /venv\//, /__pycache__\//,
  /\.pyc$/, /dist\//, /build\//, /\.egg-info\//,
  /\.example$/, /\.sample$/, /\.template$/,
  /\.md$/, /\.rst$/, /\.txt$/,
  /package-lock\.json$/, /yarn\.lock$/, /pnpm-lock\.yaml$/,
  /Cargo\.lock$/, /poetry\.lock$/,
];

const BINARY_EXTENSIONS = new Set(['.png', '.jpg', '.jpeg', /* ... */]);

export function shouldSkipFile(filePath: string, customIgnores: RegExp[]): boolean {
  const ext = path.extname(filePath).toLowerCase();
  if (BINARY_EXTENSIONS.has(ext)) return true;
  for (const pattern of DEFAULT_IGNORE_PATTERNS) {
    if (pattern.test(filePath)) return true;
  }
  for (const pattern of customIgnores) {
    if (pattern.test(filePath)) return true;
  }
  return false;
}

// Load custom patterns from .secretsignore (one regex per line, # = comment)
export function loadSecretsIgnore(projectDir: string): RegExp[] { /* ... */ }
```

---

## 16. Security Profile

**File:** `security-profile.ts`

Profile loading is mtime-cached. Under the denylist model, profile command
sets are informational only — they do not affect allow/deny decisions.

```typescript
// Profile files
const PROFILE_FILENAME = '.auto-claude-security.json';
const ALLOWLIST_FILENAME = '.auto-claude-allowlist';

export interface SecurityProfile {
  baseCommands: Set<string>;
  stackCommands: Set<string>;
  scriptCommands: Set<string>;
  customCommands: Set<string>;
  customScripts: { shellScripts: string[] };
  getAllAllowedCommands(): Set<string>;
}

/**
 * Get the security profile for a project, using mtime-based cache.
 * Cache invalidated when: project directory changes, profile file modified,
 * or allowlist file created/modified/deleted.
 */
export function getSecurityProfile(projectDir: string): SecurityProfile {
  // Cache check: compare mtimes of profile + allowlist files
  // Load from .auto-claude-security.json or return createDefaultProfile()
  // Merge .auto-claude-allowlist commands into customCommands
}

export function resetProfileCache(): void { /* clear all cached state */ }
```

JSON profile format (`base_commands`, `stack_commands`, `script_commands`,
`custom_commands` arrays; `custom_scripts.shell_scripts` array).

Allowlist file: one command name per line, `#` for comments.

---

## 17. API Key Validation Service

**File:** `api-validation-service.ts`

```typescript
export interface ApiValidationResult {
  success: boolean;
  message: string;
  details?: {
    provider?: string;
    model?: string;
    latencyMs?: number;
  };
}

/** OpenAI: prefix check (sk- or sess-) + live GET /v1/models with 15s timeout.
 *  200 = valid, 401 = invalid key, 429 = rate-limited (key valid). */
export async function validateOpenAIApiKey(apiKey: string): Promise<ApiValidationResult>

/** Anthropic: prefix check only (sk-ant-). No live call. */
export async function validateAnthropicApiKey(apiKey: string): Promise<ApiValidationResult>

/** Google: prefix check only (AIza). No live call. */
export async function validateGoogleApiKey(apiKey: string): Promise<ApiValidationResult>

/**
 * Dispatcher: routes to per-provider validator.
 * Providers: 'openai', 'anthropic', 'google', 'ollama' (local, no key needed),
 *            'azure_openai' (presence check only).
 */
export async function validateLLMApiKey(
  provider: string,
  apiKey: string,
): Promise<ApiValidationResult>
```

---

## Implementation Notes for Python Port

1. **ValidationResult** — use a `tuple[bool, str]` return type throughout. Empty
   string means allowed. Never raise exceptions for validation failures; return
   `(False, reason)`.

2. **shellSplit** — Python's `shlex.split(posix=True)` is equivalent. On
   Windows, catch `ValueError` (unclosed quote) and fall through to the regex
   fallback.

3. **extractCommands** — detect Windows paths first (`re.search(r'[A-Za-z]:\\|
   \\[A-Za-z][A-Za-z0-9_\\/]', cmd)`), use fallback if so.

4. **BLOCKED_COMMANDS** — plain Python `frozenset` of strings.

5. **VALIDATORS** — plain `dict[str, Callable[[str], tuple[bool, str]]]`.

6. **Hook entry point** — receives `tool_name: str` and `tool_input: dict | None`.
   If `tool_name != "Bash"`, return allow immediately. Otherwise run the two-step
   (denylist, then per-command validator) loop.

7. **Path containment** — `os.path.realpath()` for symlink resolution.
   `os.path.normcase()` for Windows case-insensitive comparison. Check that
   normalized path starts with `project_dir + os.sep` (not just `project_dir`).

8. **Config path validator** — `pathlib.Path.resolve()`, compare with
   `Path(home)`, `Path(home) / ".claude"`, `Path(home) / ".claude-profiles"`.
   Use `is_relative_to()` (Python 3.9+) or manual startswith with separator.

9. **Secret scanner** — run `ALL_PATTERNS` per line per file. Use
   `re.finditer()` for global matches. Apply false-positive filter before
   appending. Skip binary files by extension and skip lock/example files by
   path pattern.

10. **Per-command validators all share the same local shellSplit helper**. In
    Python, define it once and import, or use `shlex.split` with error handling.
