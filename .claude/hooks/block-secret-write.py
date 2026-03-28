#!/usr/bin/env python3
"""Block Write/Edit tool calls that would commit secrets to disk."""
import json
import os
import sys

if os.environ.get("GOLEM_SDK_SESSION") != "1":
    sys.exit(0)

hook_input = json.loads(sys.stdin.read())
tool_name = hook_input.get("tool_name", "")
tool_input = hook_input.get("tool_input", {})

if tool_name not in {"Write", "Edit"}:
    sys.exit(0)

file_path = tool_input.get("file_path", "")
# Write has 'content'; Edit has 'new_string'
content = tool_input.get("content") or tool_input.get("new_string") or ""
if not content or not file_path:
    sys.exit(0)

try:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
    from pathlib import Path
    from golem.security import SecurityAllowlist, load_allowlist, validate_write_content

    allowlist = None
    golem_dir_env = os.environ.get("GOLEM_DIR", "")
    session_id = os.environ.get("GOLEM_SESSION_ID", "")
    if golem_dir_env and session_id:
        allowlist = load_allowlist(Path(golem_dir_env), session_id)

    allowed, reason = validate_write_content(content, file_path, allowlist=allowlist)
    if not allowed:
        result = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
        print(json.dumps(result))
        sys.exit(0)

except ImportError:
    pass  # Can't scan without golem package; allow and rely on git hook

sys.exit(0)
