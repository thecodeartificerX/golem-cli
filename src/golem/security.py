"""Security validation module for Golem SDK sessions.

Implements a denylist-based security model for Bash tool calls:
  1. Static denylist (BLOCKED_COMMANDS) — always denied
  2. Per-command validators — detailed checks for rm, git, kill, shells, DBs
  3. Secret scanner — detects credentials in Write/Edit tool content
  4. Session-scoped allowlist — operator escape valve

Usage from PreToolUse hooks:
    from golem.security import validate_command, SecurityAllowlist
    allowed, reason = validate_command(command, allowlist=allowlist)
"""
from __future__ import annotations

import json
import re
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


# ---------------------------------------------------------------------------
# Core types
# ---------------------------------------------------------------------------

# (allowed, reason) — reason is empty string when allowed=True
ValidationResult = tuple[bool, str]

# A validator is a callable from a command segment string to ValidationResult
ValidatorFn = Callable[[str], ValidationResult]


# ---------------------------------------------------------------------------
# 1. Static denylist
# ---------------------------------------------------------------------------

BLOCKED_COMMANDS: frozenset[str] = frozenset({
    # System shutdown / reboot
    "shutdown", "reboot", "halt", "poweroff", "init",

    # Disk formatting / partition management
    "mkfs", "fdisk", "parted", "gdisk",
    "dd",  # raw disk write — catastrophic in autonomous context

    # Privilege escalation
    "sudo", "su", "doas",
    "chown",  # ownership changes require elevated context

    # Firewall / network infrastructure
    "iptables", "ip6tables", "nft", "ufw",

    # Network scanning
    "nmap",

    # System service management
    "systemctl", "service",

    # Scheduled tasks
    "crontab",

    # Mount / unmount
    "mount", "umount",

    # User / group account management
    "useradd", "userdel", "usermod",
    "groupadd", "groupdel",
    "passwd", "visudo",

    # Windows equivalents
    "net",       # net user, net localgroup, net stop
    "sc",        # service control
    "schtasks",  # scheduled task management
    "bcdedit",   # boot config — catastrophic
    "diskpart",  # disk partitioning
    "format",    # disk format
    "icacls",    # Windows ACL (analogous to chown)
    "takeown",   # ownership takeover
})


# ---------------------------------------------------------------------------
# 2. Shell command parser
# ---------------------------------------------------------------------------

def _contains_windows_path(command: str) -> bool:
    """Detect Windows-style paths like C:\\ or \\dir\\cmd."""
    return bool(re.search(r"[A-Za-z]:\\|\\[A-Za-z][A-Za-z0-9_\\/]", command))


_SHELL_KEYWORDS: frozenset[str] = frozenset({
    "if", "then", "else", "elif", "fi", "for", "while", "until",
    "do", "done", "case", "esac", "in", "function",
})


def _fallback_extract_commands(command: str) -> list[str]:
    """
    Regex-based command name extractor — used when shlex fails or when
    Windows paths are present. Splits on &&, ||, |, ; and takes the first
    token of each segment after stripping VAR=value prefixes.
    """
    commands: list[str] = []
    parts = re.split(r"\s*(?:&&|\|\||\|)\s*|;\s*", command)
    for part in parts:
        part = part.strip()
        if not part:
            continue
        # Strip leading VAR=value assignments
        while re.match(r"^[A-Za-z_][A-Za-z0-9_]*=\S*\s+", part):
            part = re.sub(r"^[A-Za-z_][A-Za-z0-9_]*=\S*\s+", "", part)
        if not part:
            continue
        m = re.match(r'^(?:"([^"]+)"|\'([^\']+)\'|(\S+))', part)
        if not m:
            continue
        token = m.group(1) or m.group(2) or m.group(3)
        if not token:
            continue
        # Basename, strip Windows extensions
        cmd = Path(token).name
        cmd = re.sub(r"\.(exe|cmd|bat|ps1|sh)$", "", cmd, flags=re.IGNORECASE)
        cmd = cmd.lstrip("\"'\\/")
        if "(" in cmd or ")" in cmd or "." in cmd:
            continue
        if cmd and cmd.lower() not in _SHELL_KEYWORDS:
            commands.append(cmd)
    return commands


_SHELL_OPERATORS: frozenset[str] = frozenset({"|", "||", "&&", "&"})
_SHELL_STRUCTURE: frozenset[str] = frozenset({
    "if", "then", "else", "elif", "fi", "for", "while", "until",
    "do", "done", "case", "esac", "in", "!", "{", "}", "(", ")", "function",
})
_REDIRECT_TOKENS: frozenset[str] = frozenset({"<<", "<<<", ">>", ">", "<", "2>", "2>&1", "&>"})


def extract_commands(command: str) -> list[str]:
    """
    Extract all command names from a compound shell string.
    Handles pipes, &&, ||, ;, and subshells.
    Falls back to regex extraction on Windows paths or shlex parse failure.
    Returns empty list only on total parse failure.
    """
    if not command.strip():
        return []

    if _contains_windows_path(command):
        result = _fallback_extract_commands(command)
        if result:
            return result

    commands: list[str] = []
    # Split on unquoted semicolons first
    segments = re.split(r'(?<!["\'])\s*;\s*(?!["\'])', command)

    for raw_seg in segments:
        seg = raw_seg.strip()
        if not seg:
            continue
        try:
            tokens = shlex.split(seg)
        except ValueError:
            fallback = _fallback_extract_commands(command)
            return fallback if fallback else []

        if not tokens:
            continue
        expect_command = True
        for token in tokens:
            if token in _SHELL_OPERATORS:
                expect_command = True
                continue
            if token in _SHELL_STRUCTURE:
                continue
            if token.startswith("-"):
                continue
            if "=" in token and not token.startswith("="):
                continue  # VAR=value assignment
            if token in _REDIRECT_TOKENS:
                continue
            if expect_command:
                cmd = Path(token).name
                commands.append(cmd)
                expect_command = False

    return commands


def split_command_segments(command: str) -> list[str]:
    """
    Split compound command into individual segments on &&, ||, ;.
    Used to find the specific segment that contains a given command name.
    """
    parts = re.split(r"\s*(?:&&|\|\|)\s*", command)
    result: list[str] = []
    for part in parts:
        for sub in re.split(r'(?<!["\'])\s*;\s*(?!["\'])', part):
            trimmed = sub.strip()
            if trimmed:
                result.append(trimmed)
    return result


def get_segment_for_command(cmd_name: str, segments: list[str]) -> str:
    """Return the first segment that contains cmd_name, or empty string."""
    for seg in segments:
        if cmd_name in extract_commands(seg):
            return seg
    return ""


# ---------------------------------------------------------------------------
# 3. Per-command validators
# ---------------------------------------------------------------------------

# --- rm validator ---

_DANGEROUS_RM_TARGETS: list[re.Pattern[str]] = [
    re.compile(r"^/$"),        # root
    re.compile(r"^\.\.$"),     # parent dir
    re.compile(r"^~$"),        # home dir
    re.compile(r"^\*$"),       # bare wildcard
    re.compile(r"^/\*$"),      # root wildcard
    re.compile(r"^\.\./"),     # escaping current dir
    re.compile(r"^/home$"),
    re.compile(r"^/usr$"),
    re.compile(r"^/etc$"),
    re.compile(r"^/var$"),
    re.compile(r"^/bin$"),
    re.compile(r"^/lib$"),
    re.compile(r"^/opt$"),
]


def validate_rm(segment: str) -> ValidationResult:
    """Block dangerous rm targets. Allows -r/-f/-rf; blocks --no-preserve-root."""
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return False, "Could not parse rm command"
    for token in tokens[1:]:
        if token == "--no-preserve-root":
            return False, "--no-preserve-root is not allowed for safety"
        if token.startswith("-"):
            continue
        for pat in _DANGEROUS_RM_TARGETS:
            if pat.search(token):
                return False, f"rm target '{token}' is not allowed for safety"
    return True, ""


# --- git validator ---

_BLOCKED_GIT_CONFIG_KEYS: frozenset[str] = frozenset({
    "user.name", "user.email",
    "author.name", "author.email",
    "committer.name", "committer.email",
    "credential.helper",
    "credential.username",
})

_GIT_READONLY_FLAGS: frozenset[str] = frozenset({
    "--get", "--get-all", "--get-regexp", "--list", "-l",
})


def _validate_git_config(tokens: list[str]) -> ValidationResult:
    """Block writes to identity and credential config keys."""
    # Allow read-only operations
    for tok in tokens[2:]:
        if tok in _GIT_READONLY_FLAGS:
            return True, ""
    # Find the config key (first non-flag token after 'config')
    config_key: str | None = None
    for tok in tokens[2:]:
        if tok.startswith("-"):
            continue
        config_key = tok.lower()
        break
    if config_key is None:
        return True, ""
    if config_key in _BLOCKED_GIT_CONFIG_KEYS:
        return (
            False,
            f"BLOCKED: Cannot modify git identity/credential configuration '{config_key}'. "
            "Git identity must inherit from the user's global config. "
            "Simply commit without setting user configuration.",
        )
    return True, ""


def _validate_git_inline_config(tokens: list[str]) -> ValidationResult:
    """Block identity/credential changes passed via git -c flag."""
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        pair: str | None = None
        if tok == "-c" and i + 1 < len(tokens):
            pair = tokens[i + 1]
            i += 2
        elif tok.startswith("-c") and len(tok) > 2:
            pair = tok[2:]
            i += 1
        else:
            i += 1
            continue
        if pair and "=" in pair:
            key = pair.split("=")[0].lower()
            if key in _BLOCKED_GIT_CONFIG_KEYS:
                return (
                    False,
                    f"BLOCKED: Cannot set git identity via -c flag '{pair}'. "
                    "Inline identity overrides are not permitted.",
                )
    return True, ""


def validate_git(segment: str) -> ValidationResult:
    """
    Main git validator:
      1. Block inline -c identity overrides on any git command.
      2. Block destructive operations: push --force, reset --hard,
         clean -f, checkout --, branch -D.
      3. Block interactive commands (require user input).
      4. Block git config writes to identity/credential keys.
    """
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return False, "Could not parse git command"
    if not tokens or tokens[0] != "git":
        return True, ""
    if len(tokens) < 2:
        return True, ""

    # Check inline -c flags first (applies to all subcommands)
    ok, reason = _validate_git_inline_config(tokens)
    if not ok:
        return False, reason

    # Find the subcommand (skip global options)
    subcommand: str | None = None
    skip_next = False
    for tok in tokens[1:]:
        if skip_next:
            skip_next = False
            continue
        if tok in {"-c", "-C", "--git-dir", "--work-tree"}:
            skip_next = True
            continue
        if tok.startswith("-"):
            continue
        subcommand = tok
        break

    if subcommand is None:
        return True, ""

    if subcommand == "config":
        return _validate_git_config(tokens)

    if subcommand == "push":
        if "--force" in tokens or "-f" in tokens:
            # Allow --force-with-lease (safer alternative)
            if "--force-with-lease" not in tokens:
                return False, "git push --force is not allowed. Use --force-with-lease if needed."

    if subcommand == "reset":
        if "--hard" in tokens:
            return False, "git reset --hard is not allowed — uncommitted changes will be lost."

    if subcommand == "clean":
        if "-f" in tokens or "--force" in tokens:
            return False, "git clean -f is not allowed — untracked files will be lost."

    if subcommand == "checkout":
        # git checkout -- <file>  or  git checkout -f  or  git checkout .
        if "--" in tokens:
            return False, "git checkout -- is not allowed — use git restore instead."
        if "-f" in tokens or "--force" in tokens:
            return False, "git checkout -f is not allowed."
        if "." in tokens:
            return False, "git checkout . is not allowed — discards all changes."

    if subcommand == "branch":
        if "-D" in tokens:
            return False, "git branch -D is not allowed. Use -d (safe delete) instead."

    if subcommand == "rebase":
        if "-i" in tokens or "--interactive" in tokens:
            return False, "git rebase -i requires interactive input — not available in headless mode."

    if subcommand == "add":
        if "-i" in tokens or "--interactive" in tokens:
            return False, "git add -i requires interactive input — not available in headless mode."
        if "-p" in tokens or "--patch" in tokens:
            return False, "git add -p requires interactive input — not available in headless mode."

    if subcommand == "stash":
        return False, "git stash is not allowed. Use git worktree operations instead."

    return True, ""


# --- process validators ---

_BLOCKED_PROCESS_NAMES: frozenset[str] = frozenset({
    # OS init / system
    "systemd", "launchd", "init", "loginwindow", "kernel_task",
    "containerd", "dockerd",
    # Linux desktop / display
    "Xorg", "Xwayland", "gnome-shell", "kwin", "plasmashell", "mutter",
    "gdm", "lightdm", "sddm", "pulseaudio", "pipewire", "wireplumber",
    "dbus-daemon", "polkitd", "networkmanager", "NetworkManager",
    "wpa_supplicant",
    # macOS desktop
    "Finder", "Dock", "WindowServer", "SystemUIServer",
    "mds", "mds_stores", "coreaudiod", "securityd", "opendirectoryd",
    # Windows critical
    "explorer.exe", "dwm.exe", "csrss.exe", "winlogon.exe",
    "lsass.exe", "services.exe", "svchost.exe", "smss.exe", "wininit.exe",
    # Remote access
    "sshd", "ssh-agent",
    # Self-protection: don't kill the agent's own host
    "electron", "Electron", "claude",
})


def validate_pkill(segment: str) -> ValidationResult:
    """Block killing system-critical processes or killing by user (-u flag)."""
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return False, "Could not parse pkill command"
    if not tokens:
        return False, "Empty pkill command"

    flags: list[str] = []
    args: list[str] = []
    for tok in tokens[1:]:
        if tok.startswith("-"):
            flags.append(tok)
        else:
            args.append(tok)

    for flag in flags:
        if flag in {"-u", "--euid"} or (flag.startswith("-u") and flag != "-u"):
            return False, "pkill -u (kill by user) is not allowed — too broad"

    if not args:
        return False, "pkill requires a process name"

    target = args[-1].split()[0]  # -f matches full cmdline; take first word
    if target in _BLOCKED_PROCESS_NAMES:
        return (
            False,
            f"Cannot kill system-critical process '{target}'. "
            "Killing OS daemons or desktop environment processes could crash the system.",
        )
    return True, ""


def validate_kill(segment: str) -> ValidationResult:
    """Block kill -1 (broadcast) and kill 0 (process group)."""
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return False, "Could not parse kill command"
    for tok in tokens[1:]:
        if tok in {"-1", "0", "-0"}:
            return False, "kill -1 and kill 0 are not allowed (affects all processes)"
    return True, ""


def validate_killall(segment: str) -> ValidationResult:
    """Delegate to pkill validator (same rules)."""
    return validate_pkill(segment)


# --- shell interpreter bypass validator ---

_SHELL_INTERPRETERS: frozenset[str] = frozenset({"bash", "sh", "zsh", "python", "python3"})


def _extract_c_argument(segment: str) -> str | None:
    """
    Extract the command string from a shell -c invocation.
    Returns the inner string, or None if no -c flag found.
    Raises ValueError on parse failure.
    """
    tokens = shlex.split(segment)
    for i, tok in enumerate(tokens):
        is_c = tok == "-c" or (
            tok.startswith("-") and not tok.startswith("--") and "c" in tok[1:]
        )
        if is_c and i + 1 < len(tokens):
            return tokens[i + 1]
    return None


def validate_shell_c(segment: str) -> ValidationResult:
    """
    Validate commands inside bash/sh/zsh -c '...' strings.
    Checks each inner command against BLOCKED_COMMANDS.
    Recursively validates nested shells.
    """
    try:
        inner = _extract_c_argument(segment)
    except ValueError:
        return False, "Could not parse shell command"

    if inner is None:
        # Not a -c invocation — block process substitution
        for pat in ["<(", ">("]:
            if pat in segment:
                return False, f"Process substitution '{pat}' is not allowed"
        return True, ""

    inner_cmds = extract_commands(inner)
    if not inner_cmds:
        if not inner.strip():
            return True, ""
        return False, f"Could not parse commands inside shell -c: {inner!r}"

    for cmd_name in inner_cmds:
        cmd_base = Path(cmd_name).stem
        if cmd_base in BLOCKED_COMMANDS:
            return (
                False,
                f"Command '{cmd_name}' inside shell -c is blocked: "
                "system-level command not permitted for autonomous agents.",
            )

    # Recursive check for nested shell interpreters
    inner_segments = split_command_segments(inner)
    for inner_seg in inner_segments:
        seg_cmds = extract_commands(inner_seg)
        if seg_cmds and seg_cmds[0] in _SHELL_INTERPRETERS:
            ok, reason = validate_shell_c(inner_seg)
            if not ok:
                return False, f"Nested shell command not allowed: {reason}"

    return True, ""


# --- database validators ---

_DESTRUCTIVE_SQL_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bDROP\s+(DATABASE|SCHEMA|TABLE|INDEX|VIEW|FUNCTION|PROCEDURE|TRIGGER)\b", re.IGNORECASE),
    re.compile(r"\bTRUNCATE\s+(TABLE\s+)?\w+", re.IGNORECASE),
    re.compile(r"\bDELETE\s+FROM\s+\w+\s*(;|$)", re.IGNORECASE),  # no WHERE clause
    re.compile(r"\bDROP\s+ALL\b", re.IGNORECASE),
]

_SAFE_DB_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^test", re.IGNORECASE), re.compile(r"_test$", re.IGNORECASE),
    re.compile(r"^dev",  re.IGNORECASE), re.compile(r"_dev$",  re.IGNORECASE),
    re.compile(r"^local", re.IGNORECASE), re.compile(r"_local$", re.IGNORECASE),
    re.compile(r"^tmp",  re.IGNORECASE), re.compile(r"_tmp$",  re.IGNORECASE),
    re.compile(r"^temp", re.IGNORECASE), re.compile(r"_temp$", re.IGNORECASE),
    re.compile(r"^scratch",  re.IGNORECASE),
    re.compile(r"^sandbox",  re.IGNORECASE),
    re.compile(r"^mock",     re.IGNORECASE), re.compile(r"_mock$", re.IGNORECASE),
]


def _is_safe_db_name(name: str) -> bool:
    return any(p.search(name) for p in _SAFE_DB_PATTERNS)


def _contains_destructive_sql(sql: str) -> tuple[bool, str]:
    for pat in _DESTRUCTIVE_SQL_PATTERNS:
        m = pat.search(sql)
        if m:
            return True, m.group(0)
    return False, ""


def _extract_flag_arg(tokens: list[str], flags: set[str]) -> str | None:
    """Return the argument immediately following any token in flags."""
    for i, tok in enumerate(tokens):
        if tok in flags and i + 1 < len(tokens):
            return tokens[i + 1]
        for flag in flags:
            if tok.startswith(flag) and len(tok) > len(flag):
                return tok[len(flag):]
    return None


def validate_psql(segment: str) -> ValidationResult:
    """Block destructive SQL in psql -c '...' invocations."""
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return False, "Could not parse psql command"
    sql = _extract_flag_arg(tokens, {"-c", "--command"})
    if sql:
        bad, matched = _contains_destructive_sql(sql)
        if bad:
            return False, f"psql contains destructive SQL '{matched}' — requires manual confirmation"
    return True, ""


def validate_mysql(segment: str) -> ValidationResult:
    """Block destructive SQL in mysql -e '...' invocations."""
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return False, "Could not parse mysql command"
    sql = _extract_flag_arg(tokens, {"-e", "--execute"})
    if sql:
        bad, matched = _contains_destructive_sql(sql)
        if bad:
            return False, f"mysql contains destructive SQL '{matched}' — requires manual confirmation"
    return True, ""


def validate_mysqladmin(segment: str) -> ValidationResult:
    """Block drop/shutdown/kill operations in mysqladmin."""
    _DANGEROUS_OPS: frozenset[str] = frozenset({"drop", "shutdown", "kill"})
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return False, "Could not parse mysqladmin command"
    for tok in tokens[1:]:
        if tok.lower() in _DANGEROUS_OPS:
            return False, f"mysqladmin '{tok}' is blocked — requires manual confirmation"
    return True, ""


def validate_redis_cli(segment: str) -> ValidationResult:
    """Block destructive Redis commands."""
    _DANGEROUS_REDIS: frozenset[str] = frozenset({
        "FLUSHALL", "FLUSHDB", "DEBUG", "SHUTDOWN",
        "SLAVEOF", "REPLICAOF", "CONFIG", "BGSAVE",
        "BGREWRITEAOF", "CLUSTER",
    })
    _FLAGS_WITH_ARGS: frozenset[str] = frozenset({"-h", "-p", "-a", "-n", "--pass", "--user", "-u"})
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return False, "Could not parse redis-cli command"
    skip_next = False
    for tok in tokens[1:]:
        if skip_next:
            skip_next = False
            continue
        if tok in _FLAGS_WITH_ARGS:
            skip_next = True
            continue
        if tok.startswith("-"):
            continue
        if tok.upper() in _DANGEROUS_REDIS:
            return False, f"redis-cli '{tok}' is blocked — destructive operation"
        break  # first non-flag token is the Redis command
    return True, ""


def validate_mongosh(segment: str) -> ValidationResult:
    """Block destructive Mongo operations in mongosh/mongo --eval."""
    _DANGEROUS_MONGO: list[re.Pattern[str]] = [
        re.compile(r"\.dropDatabase\s*\(", re.IGNORECASE),
        re.compile(r"\.drop\s*\(", re.IGNORECASE),
        re.compile(r"\.deleteMany\s*\(\s*\{\s*\}\s*\)", re.IGNORECASE),
        re.compile(r"\.remove\s*\(\s*\{\s*\}\s*\)", re.IGNORECASE),
        re.compile(r"db\.dropAllUsers\s*\(", re.IGNORECASE),
        re.compile(r"db\.dropAllRoles\s*\(", re.IGNORECASE),
    ]
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return False, "Could not parse mongosh command"
    eval_script = _extract_flag_arg(tokens, {"--eval"})
    if eval_script:
        for pat in _DANGEROUS_MONGO:
            if pat.search(eval_script):
                return False, "mongosh --eval contains destructive operation — requires manual confirmation"
    return True, ""


def validate_dropdb(segment: str) -> ValidationResult:
    """Only allow dropping test/dev databases."""
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return False, "Could not parse dropdb command"
    _FLAGS_WITH_ARGS: set[str] = {"-h", "--host", "-p", "--port", "-U", "--username", "--maintenance-db"}
    skip_next = False
    db_name: str | None = None
    for tok in tokens[1:]:
        if skip_next:
            skip_next = False
            continue
        if tok in _FLAGS_WITH_ARGS:
            skip_next = True
            continue
        if tok in {"-w", "--no-password", "-W", "--password"}:
            continue
        if tok.startswith("-"):
            continue
        db_name = tok
    if db_name is None:
        return False, "dropdb requires a database name"
    if _is_safe_db_name(db_name):
        return True, ""
    return (
        False,
        f"dropdb '{db_name}' is blocked. "
        "Only test/dev databases may be dropped autonomously. "
        "Safe name patterns: test*, *_test, dev*, *_dev, local*, tmp*, temp*, scratch*, sandbox*, mock*",
    )


def validate_dropuser(segment: str) -> ValidationResult:
    """Only allow dropping test/dev users."""
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return False, "Could not parse dropuser command"
    _FLAGS_WITH_ARGS: set[str] = {"-h", "--host", "-p", "--port", "-U", "--username"}
    skip_next = False
    username: str | None = None
    for tok in tokens[1:]:
        if skip_next:
            skip_next = False
            continue
        if tok in _FLAGS_WITH_ARGS:
            skip_next = True
            continue
        if tok in {"-w", "--no-password", "-W", "--password"}:
            continue
        if tok.startswith("-"):
            continue
        username = tok
    if username is None:
        return False, "dropuser requires a username"
    if _is_safe_db_name(username):  # same safe-name patterns apply
        return True, ""
    return (
        False,
        f"dropuser '{username}' is blocked. "
        "Only test/dev users may be dropped autonomously.",
    )


# ---------------------------------------------------------------------------
# 4. Validators registry
# ---------------------------------------------------------------------------

VALIDATORS: dict[str, ValidatorFn] = {
    # Filesystem
    "rm":         validate_rm,

    # Git
    "git":        validate_git,

    # Process management
    "kill":       validate_kill,
    "pkill":      validate_pkill,
    "killall":    validate_killall,

    # Shell interpreter bypass prevention
    "bash":       validate_shell_c,
    "sh":         validate_shell_c,
    "zsh":        validate_shell_c,

    # Databases
    "psql":       validate_psql,
    "mysql":      validate_mysql,
    "mysqladmin": validate_mysqladmin,
    "redis-cli":  validate_redis_cli,
    "mongosh":    validate_mongosh,
    "mongo":      validate_mongosh,
    "dropdb":     validate_dropdb,
    "dropuser":   validate_dropuser,
}


# ---------------------------------------------------------------------------
# 5. Path containment checker
# ---------------------------------------------------------------------------

def is_path_contained(file_path: str | Path, project_root: str | Path) -> tuple[bool, str]:
    """
    Check that file_path resolves within project_root.
    Resolves symlinks; falls back to parent dir if file doesn't exist yet.
    Returns (contained, reason_if_not).
    Case-insensitive on Windows.

    This is NOT called from the bash validator (it doesn't know the cwd for
    every tool call). It is called from the Write/Edit hooks when the path
    is available directly in tool_input.
    """
    def _resolve(p: Path) -> Path:
        try:
            return p.resolve()
        except OSError:
            try:
                return p.parent.resolve() / p.name
            except OSError:
                return p.absolute()

    root = _resolve(Path(project_root))
    fp = Path(file_path)
    target = _resolve(fp if fp.is_absolute() else root / fp)

    if sys.platform == "win32":
        root_s = str(root).lower()
        target_s = str(target).lower()
    else:
        root_s = str(root)
        target_s = str(target)

    sep = "\\" if sys.platform == "win32" else "/"
    root_prefix = root_s if root_s.endswith(sep) else root_s + sep

    if target_s == root_s or target_s.startswith(root_prefix):
        return True, ""
    return False, f"Path '{file_path}' resolves to '{target}' which is outside project root '{root}'"


# ---------------------------------------------------------------------------
# 6. Secret scanner
# ---------------------------------------------------------------------------

# Generic: variable assignments, bearer tokens, base64 blobs
GENERIC_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r'(?:api[_-]?key|apikey|api_secret|secret[_-]?key)\s*[:=]\s*["\']([a-zA-Z0-9_-]{32,})["\']',
                re.IGNORECASE), "Generic API key assignment"),
    (re.compile(r'(?:access[_-]?token|auth[_-]?token|bearer[_-]?token|token)\s*[:=]\s*["\']([a-zA-Z0-9_-]{32,})["\']',
                re.IGNORECASE), "Generic access token"),
    (re.compile(r'(?:password|passwd|pwd|pass)\s*[:=]\s*["\']([^"\']{8,})["\']', re.IGNORECASE),
     "Password assignment"),
    (re.compile(r'(?:secret|client_secret|app_secret)\s*[:=]\s*["\']([a-zA-Z0-9_/+=]{16,})["\']', re.IGNORECASE),
     "Secret assignment"),
    (re.compile(r'["\']?[Bb]earer\s+([a-zA-Z0-9_-]{20,})["\']?'), "Bearer token"),
    (re.compile(r'["\'][A-Za-z0-9+/]{64,}={0,2}["\']'), "Potential base64-encoded secret"),
]

# Service-specific known formats
SERVICE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"sk-[a-zA-Z0-9]{20,}"), "OpenAI/Anthropic-style API key"),
    (re.compile(r"sk-ant-[a-zA-Z0-9-]{20,}"), "Anthropic API key"),
    (re.compile(r"sk-proj-[a-zA-Z0-9-]{20,}"), "OpenAI project API key"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS Access Key ID"),
    (re.compile(r"(?:aws_secret_access_key|AWS_SECRET_ACCESS_KEY)\s*=\s*[A-Za-z0-9+/]{40}"),
     "AWS Secret Access Key"),
    (re.compile(r"AIza[0-9A-Za-z_-]{35}"), "Google API Key"),
    (re.compile(r"ya29\.[0-9A-Za-z_-]{68,}"), "Google OAuth token"),
    (re.compile(r"ghp_[A-Za-z0-9]{36,}"), "GitHub Personal Access Token"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{82}"), "GitHub Fine-Grained PAT"),
    (re.compile(r"ghs_[A-Za-z0-9]{36,}"), "GitHub Actions token"),
    (re.compile(r"sk_live_[a-zA-Z0-9]{24,}"), "Stripe live API key"),
    (re.compile(r"sk_test_[a-zA-Z0-9]{24,}"), "Stripe test API key"),
    (re.compile(r"xoxb-[0-9]+-[0-9]+-[a-zA-Z0-9]+"), "Slack Bot Token"),
    (re.compile(r"xoxp-[0-9]+-[0-9]+-[0-9]+-[a-zA-Z0-9]+"), "Slack User Token"),
    (re.compile(r"xapp-[0-9]+-[A-Z0-9]+-[0-9]+-[a-z0-9]+"), "Slack App Token"),
    (re.compile(r"[Dd]iscord[_-]?[Tt]oken\s*[:=]\s*[A-Za-z0-9_-]{59}"), "Discord Bot Token"),
    (re.compile(r"AC[a-z0-9]{32}"), "Twilio Account SID"),
    (re.compile(r"SG\.[a-zA-Z0-9_-]{22}\.[a-zA-Z0-9_-]{43}"), "SendGrid API Key"),
    (re.compile(r"lin_api_[a-zA-Z0-9]{40}"), "Linear API key"),
    (re.compile(r"dp\.pt\.[a-zA-Z0-9]{43}"), "Doppler service token"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "JWT token"),
]

# Private key PEM headers
PRIVATE_KEY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY-----"), "RSA Private Key"),
    (re.compile(r"-----BEGIN\s+OPENSSH\s+PRIVATE\s+KEY-----"), "OpenSSH Private Key"),
    (re.compile(r"-----BEGIN\s+EC\s+PRIVATE\s+KEY-----"), "EC Private Key"),
    (re.compile(r"-----BEGIN\s+DSA\s+PRIVATE\s+KEY-----"), "DSA Private Key"),
    (re.compile(r"-----BEGIN\s+PGP\s+PRIVATE\s+KEY\s+BLOCK-----"), "PGP Private Key"),
]

# Database connection strings with embedded credentials
DATABASE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"mongodb(?:\+srv)?://[^\s:\"]+:[^@\s\"]+@[^\s\"]+"),
     "MongoDB connection string with credentials"),
    (re.compile(r"postgres(?:ql)?://[^\s:\"]+:[^@\s\"]+@[^\s\"]+"),
     "PostgreSQL connection string with credentials"),
    (re.compile(r"mysql://[^\s:\"]+:[^@\s\"]+@[^\s\"]+"), "MySQL connection string with credentials"),
    (re.compile(r"redis://:[^@\s\"]+@[^\s\"]+"), "Redis connection string with credentials"),
    (re.compile(r"amqp://[^\s:\"]+:[^@\s\"]+@[^\s\"]+"), "RabbitMQ connection string with credentials"),
]

ALL_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = (
    GENERIC_PATTERNS + SERVICE_PATTERNS + PRIVATE_KEY_PATTERNS + DATABASE_PATTERNS
)

# False positive suppression
_FALSE_POSITIVE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"process\.env\."),
    re.compile(r"os\.environ"),
    re.compile(r"ENV\["),
    re.compile(r"\$\{[A-Z_]+\}"),
    re.compile(r"your[-_]?api[-_]?key", re.IGNORECASE),
    re.compile(r"xxx+", re.IGNORECASE),
    re.compile(r"placeholder", re.IGNORECASE),
    re.compile(r"example", re.IGNORECASE),
    re.compile(r"sample", re.IGNORECASE),
    re.compile(r"test[-_]?key", re.IGNORECASE),
    re.compile(r"<[A-Z_]+>"),
    re.compile(r"TODO|FIXME|CHANGEME"),
    re.compile(r"INSERT[-_]?YOUR", re.IGNORECASE),
    re.compile(r"REPLACE[-_]?WITH", re.IGNORECASE),
]

_ALWAYS_SKIP: list[re.Pattern[str]] = [
    re.compile(r"\.git[\\/]"),
    re.compile(r"node_modules[\\/]"),
    re.compile(r"\.venv[\\/]"),
    re.compile(r"__pycache__[\\/]"),
    re.compile(r"\.pyc$"),
    re.compile(r"dist[\\/]"),
    re.compile(r"build[\\/]"),
    re.compile(r"\.egg-info[\\/]"),
    re.compile(r"\.(example|sample|template)$"),
    re.compile(r"\.(md|rst|txt)$"),
    re.compile(r"(package-lock|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|Cargo\.lock)$"),
]

_BINARY_EXTENSIONS: frozenset[str] = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg",
    ".pdf", ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z",
    ".exe", ".dll", ".so", ".dylib",
    ".woff", ".woff2", ".ttf", ".eot",
    ".mp3", ".mp4", ".wav", ".avi",
    ".pyc", ".pyo",
})


@dataclass
class SecretMatch:
    file_path: str
    line_number: int
    pattern_name: str
    matched_text: str
    line_content: str  # trimmed, max 100 chars


def _is_false_positive(line: str, matched_text: str) -> bool:
    for pat in _FALSE_POSITIVE_PATTERNS:
        if pat.search(line):
            return True
    # Pure type hint: `token: str`
    if re.match(r"^[a-z_]+:\s*str\s*$", line.strip(), re.IGNORECASE):
        return True
    # Comments: only flag if matched_text is 40+ chars
    stripped = line.strip()
    if stripped.startswith(("#", "//", "*")):
        if not re.search(r"[a-zA-Z0-9_-]{40,}", matched_text):
            return True
    return False


def _should_skip_file(file_path: str) -> bool:
    ext = Path(file_path).suffix.lower()
    if ext in _BINARY_EXTENSIONS:
        return True
    for pat in _ALWAYS_SKIP:
        if pat.search(file_path):
            return True
    return False


def scan_content(content: str, file_path: str = "<content>") -> list[SecretMatch]:
    """Scan string content for potential secrets. Returns list of matches."""
    matches: list[SecretMatch] = []
    for line_idx, line in enumerate(content.splitlines(), start=1):
        for pattern, pattern_name in ALL_SECRET_PATTERNS:
            for m in pattern.finditer(line):
                if _is_false_positive(line, m.group(0)):
                    continue
                matches.append(SecretMatch(
                    file_path=file_path,
                    line_number=line_idx,
                    pattern_name=pattern_name,
                    matched_text=m.group(0),
                    line_content=line.strip()[:100],
                ))
    return matches


def mask_secret(text: str, visible_chars: int = 8) -> str:
    """Mask a secret value, showing only the first few characters."""
    if len(text) <= visible_chars:
        return text
    return text[:visible_chars] + "***"


# ---------------------------------------------------------------------------
# 7. Allowlist support
# ---------------------------------------------------------------------------

@dataclass
class SecurityAllowlist:
    """
    Session-scoped allowlist for commands that would otherwise be blocked.
    Populated by the operator before session start via config or CLI.

    Example config.json fragment:
      "security_allowlist": {
        "commands": ["dropdb test_myapp", "git push --force-with-lease"]
      }
    """
    commands: list[str] = field(default_factory=list)

    def allows(self, command: str) -> bool:
        """Return True if the exact command string is in the allowlist."""
        return command.strip() in {c.strip() for c in self.commands}


def load_allowlist(golem_dir: Path, session_id: str) -> SecurityAllowlist:
    """
    Load allowlist from .golem/sessions/<session_id>/security_allowlist.json.
    Returns empty allowlist if file missing.
    """
    path = golem_dir / "sessions" / session_id / "security_allowlist.json"
    if not path.exists():
        return SecurityAllowlist()
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return SecurityAllowlist(commands=data.get("commands", []))


# ---------------------------------------------------------------------------
# 8. Main validation entrypoints
# ---------------------------------------------------------------------------

def validate_command(
    command: str,
    allowlist: SecurityAllowlist | None = None,
) -> ValidationResult:
    """
    Validate a shell command string against the full security model.

    Returns (True, '') if allowed, (False, reason) if blocked.
    Call this from PreToolUse hooks for the Bash tool.
    """
    if not command.strip():
        return True, ""  # empty command: allow

    # Check allowlist first (operator escape valve)
    if allowlist and allowlist.allows(command):
        return True, ""

    commands = extract_commands(command)
    if not commands:
        return False, f"Could not parse command for security validation: {command!r}"

    segments = split_command_segments(command)

    for cmd_name in commands:
        # Step 1: static denylist
        cmd_base = Path(cmd_name).stem  # strip .exe etc.
        if cmd_base in BLOCKED_COMMANDS:
            return (
                False,
                f"Command '{cmd_name}' is blocked: "
                "system-level command not permitted for autonomous agents.",
            )

        # Step 2: per-command validator
        validator = VALIDATORS.get(cmd_base) or VALIDATORS.get(cmd_name)
        if validator:
            segment = get_segment_for_command(cmd_name, segments) or command
            ok, reason = validator(segment)
            if not ok:
                return False, reason

    return True, ""


def validate_write_content(
    content: str,
    file_path: str,
    allowlist: SecurityAllowlist | None = None,
) -> ValidationResult:
    """
    Scan file content being written for secrets before it hits disk.
    Used in Write/Edit tool hooks. Returns (True, '') if clean.
    """
    if allowlist and allowlist.allows(f"write:{file_path}"):
        return True, ""
    if _should_skip_file(file_path):
        return True, ""
    matches = scan_content(content, file_path)
    if not matches:
        return True, ""
    lines = [
        f"  Line {m.line_number} [{m.pattern_name}]: {mask_secret(m.matched_text)}"
        for m in matches[:5]
    ]
    return (
        False,
        f"Secret detected in '{file_path}' — commit blocked.\n"
        + "\n".join(lines)
        + ("\n  ... and more" if len(matches) > 5 else ""),
    )
