#!/usr/bin/env python3
"""Block golem CLI commands in SDK sessions."""
import json, os, re, sys

if os.environ.get("GOLEM_SDK_SESSION") != "1":
    sys.exit(0)

hook_input = json.loads(sys.stdin.read())
tool_name = hook_input.get("tool_name", "")
tool_input = hook_input.get("tool_input", {})

if tool_name != "Bash":
    sys.exit(0)

command = tool_input.get("command", "")
BLOCKED_PATTERNS = [
    r"\bgolem\s+(clean|reset-ticket|export|status|run|resume|ui|doctor)\b",
    r"\buv\s+run\s+golem\s+(clean|reset-ticket|export|status|run|resume|ui|doctor)\b",
]

for pattern in BLOCKED_PATTERNS:
    if re.search(pattern, command):
        result = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"BLOCKED: '{command}' — You are a subprocess of Golem. "
                    "Running golem CLI commands destroys the runtime state you depend on. "
                    "Use your MCP tools for ticket/worktree operations instead. "
                    "NO BYPASS EXISTS."
                ),
            }
        }
        print(json.dumps(result))
        sys.exit(0)

sys.exit(0)
