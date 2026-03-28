#!/usr/bin/env python3
"""Block dangerous git and system commands in SDK sessions.
Delegates to golem.security for structured validation."""
import json
import os
import sys

if os.environ.get("GOLEM_SDK_SESSION") != "1":
    sys.exit(0)

hook_input = json.loads(sys.stdin.read())
tool_name = hook_input.get("tool_name", "")
tool_input = hook_input.get("tool_input", {})

if tool_name != "Bash":
    sys.exit(0)

command = tool_input.get("command", "")
if not command:
    sys.exit(0)

# Load allowlist for this session if present
try:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
    from pathlib import Path
    from golem.security import SecurityAllowlist, load_allowlist, validate_command

    allowlist = None
    golem_dir_env = os.environ.get("GOLEM_DIR", "")
    session_id = os.environ.get("GOLEM_SESSION_ID", "")
    if golem_dir_env and session_id:
        allowlist = load_allowlist(Path(golem_dir_env), session_id)

    allowed, reason = validate_command(command, allowlist=allowlist)
    if not allowed:
        result = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason + " NO BYPASS EXISTS.",
            }
        }
        print(json.dumps(result))
        sys.exit(0)

except ImportError:
    # Fallback: golem package not importable (running outside uv env).
    # Keep the legacy pattern list as a safety net.
    import re
    DANGEROUS_PATTERNS = [
        (r"git\s+stash", "Use git worktree operations instead of stash"),
        (r"git\s+checkout\s+--\s", "Do not discard uncommitted changes"),
        (r"git\s+checkout\s+-f", "Do not force-checkout"),
        (r"git\s+checkout\s+\.", "Do not discard all changes"),
        (r"git\s+reset\s+--hard", "Do not hard-reset - changes will be lost"),
        (r"git\s+push\s+--force\b", "Do not force-push"),
        (r"git\s+push\s+-f\b", "Do not force-push"),
        (r"git\s+clean\s+-f", "Do not clean untracked files"),
        (r"git\s+branch\s+-D\s", "Use -d (safe delete)"),
        (r"git\s+rebase\s+-i", "Interactive rebase not available in headless mode"),
        (r"git\s+add\s+-[ip]", "Interactive add not available in headless mode"),
    ]
    for pattern, msg in DANGEROUS_PATTERNS:
        if re.search(pattern, command):
            result = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        f"[GIT-SAFE BLOCKED] {msg}. Command: {command}. NO BYPASS EXISTS."
                    ),
                }
            }
            print(json.dumps(result))
            sys.exit(0)

sys.exit(0)
