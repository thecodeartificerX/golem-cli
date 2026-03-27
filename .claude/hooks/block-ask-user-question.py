#!/usr/bin/env python3
"""Block AskUserQuestion in headless SDK sessions."""
import json, os, sys

if os.environ.get("GOLEM_SDK_SESSION") != "1":
    sys.exit(0)

hook_input = json.loads(sys.stdin.read())
tool_name = hook_input.get("tool_name", "")

if tool_name != "AskUserQuestion":
    sys.exit(0)

result = {
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": (
            "BLOCKED: AskUserQuestion — You are running in a headless SDK session. "
            "There is no user to respond. Make autonomous decisions. "
            "If unsure, choose the option that maintains code quality and correctness. "
            "If blocked, update your ticket to needs_work with details."
        ),
    }
}
print(json.dumps(result))
sys.exit(0)
