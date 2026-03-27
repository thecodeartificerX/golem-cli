#!/usr/bin/env python3
"""Block destructive git operations in SDK sessions."""
import json, os, re, sys

if os.environ.get("GOLEM_SDK_SESSION") != "1":
    sys.exit(0)

hook_input = json.loads(sys.stdin.read())
tool_name = hook_input.get("tool_name", "")
tool_input = hook_input.get("tool_input", {})

if tool_name != "Bash":
    sys.exit(0)

command = tool_input.get("command", "")
DANGEROUS_PATTERNS = [
    (r"git\s+stash", "Use git worktree operations instead of stash"),
    (r"git\s+checkout\s+--\s", "Do not discard uncommitted changes"),
    (r"git\s+checkout\s+-f", "Do not force-checkout"),
    (r"git\s+checkout\s+\.", "Do not discard all changes"),
    (r"git\s+reset\s+--hard", "Do not hard-reset — changes will be lost"),
    (r"git\s+push\s+--force", "Do not force-push — use --force-with-lease if needed"),
    (r"git\s+push\s+-f\b", "Do not force-push"),
    (r"git\s+clean\s+-f", "Do not clean untracked files"),
    (r"git\s+branch\s+-D\s", "Use -d (safe delete) instead of -D (force delete)"),
    (r"git\s+rebase\s+-i", "Interactive rebase requires user input — not available in headless mode"),
    (r"git\s+add\s+-i", "Interactive add requires user input — not available in headless mode"),
    (r"git\s+add\s+-p", "Patch mode requires user input — not available in headless mode"),
]

for pattern, reason in DANGEROUS_PATTERNS:
    if re.search(pattern, command):
        result = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"[GIT-SAFE BLOCKED] {reason}. "
                    f"Attempted command: {command}. "
                    "NO BYPASS EXISTS."
                ),
            }
        }
        print(json.dumps(result))
        sys.exit(0)

sys.exit(0)
