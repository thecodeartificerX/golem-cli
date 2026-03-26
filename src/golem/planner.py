from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    CLIConnectionError,
    CLINotFoundError,
    ClaudeAgentOptions,
    ClaudeSDKError,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

_MAX_RETRIES = 2
_RETRY_DELAY_S = 10

from golem.config import GolemConfig, sdk_env
from golem.tickets import TicketStore
from golem.tools import create_golem_mcp_server

_PLANNER_PROMPT_TEMPLATE = Path(__file__).parent / "prompts" / "planner.md"

# Monkey-patch SDK initialize timeout from 60s to 180s for long-running planner sessions.
# The SDK hardcodes this value with no public API; we patch at import time.
try:
    from claude_agent_sdk._internal.client import Query  # type: ignore[import]

    _orig_defaults = list(Query.__init__.__defaults__ or ())
    _new_defaults = tuple(180 if v == 60 else v for v in _orig_defaults)
    if _new_defaults != tuple(_orig_defaults):
        Query.__init__.__defaults__ = _new_defaults
except Exception:
    pass


async def run_planner(
    spec_path: Path,
    golem_dir: Path,
    config: GolemConfig,
    repo_root: Path | None = None,
) -> str:
    """Spawn Opus planner session that writes plans/ + references/ and creates a ticket.

    Retries up to 2 times on CLIConnectionError/ClaudeSDKError with configurable delay.
    SDK initialize timeout is monkey-patched from 60s to config.sdk_timeout at import time.
    If the planner doesn't call create_ticket via MCP, a self-healing fallback creates
    a ticket programmatically from AssistantMessage text blocks.

    Returns the ticket_id string created by the planner.
    """
    try:
        spec_content = spec_path.read_text(encoding="utf-8")
    except PermissionError:
        raise RuntimeError(f"Cannot read spec file (permission denied): {spec_path}") from None
    except OSError as e:
        raise RuntimeError(f"Cannot read spec file: {spec_path} ({e})") from None

    # Gather project context
    project_context = ""
    cwd = repo_root or spec_path.parent
    for name in ("CLAUDE.md", "README.md", "README"):
        candidate = cwd / name
        if candidate.exists():
            project_context += f"## {name}\n{candidate.read_text(encoding='utf-8')[:4000]}\n\n"
            break

    # Create required directories
    (golem_dir / "research").mkdir(parents=True, exist_ok=True)
    (golem_dir / "plans").mkdir(parents=True, exist_ok=True)
    (golem_dir / "references").mkdir(parents=True, exist_ok=True)

    template = _PLANNER_PROMPT_TEMPLATE.read_text(encoding="utf-8")
    prompt = template.replace("{spec_content}", spec_content)
    prompt = prompt.replace("{project_context}", project_context or "(none)")
    prompt = prompt.replace("{golem_dir}", str(golem_dir))
    infra_checks_str = "\n".join(f"- `{c}`" for c in config.infrastructure_checks) if config.infrastructure_checks else "(none detected)"
    prompt = prompt.replace("{infrastructure_checks}", infra_checks_str)

    # Build in-process MCP server with ticket tools registered
    mcp_server = create_golem_mcp_server(golem_dir, config, cwd)

    last_error: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            async for message in query(
                prompt=prompt,
                options=ClaudeAgentOptions(
                    model=config.planner_model,
                    cwd=str(cwd),
                    tools={"type": "preset", "preset": "claude_code"},
                    mcp_servers={"golem": mcp_server},
                    max_turns=50,
                    permission_mode="bypassPermissions",
                    env=sdk_env(),
                ),
            ):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            preview = block.text[:120].replace("\n", " ")
                            print(f"[PLANNER] {preview}", file=sys.stderr)
                        elif isinstance(block, ToolUseBlock):
                            print(f"[PLANNER] tool: {block.name}({', '.join(f'{k}=' for k in list(block.input.keys())[:3])})", file=sys.stderr)
                elif isinstance(message, ResultMessage) and message.result:
                    preview = message.result[:120].replace("\n", " ")
                    print(f"[PLANNER] result: {preview}", file=sys.stderr)
            break  # Success — exit retry loop
        except CLINotFoundError:
            raise RuntimeError(
                "Planner failed: 'claude' CLI not found on PATH. Run 'claude login' to install and authenticate."
            ) from None
        except (CLIConnectionError, ClaudeSDKError) as e:
            last_error = e
            if attempt < _MAX_RETRIES:
                print(
                    f"[PLANNER] Attempt {attempt + 1} failed ({type(e).__name__}), retrying in {config.retry_delay}s...",
                    file=sys.stderr,
                )
                await asyncio.sleep(config.retry_delay)
            else:
                raise RuntimeError(
                    f"Planner failed after {_MAX_RETRIES + 1} attempts. Last error: {last_error}"
                ) from None

    # Verify plans/overview.md was created
    overview_path = golem_dir / "plans" / "overview.md"
    if not overview_path.exists():
        raise RuntimeError(
            f"Planner did not create plans/overview.md at {overview_path}. "
            "Check planner session output for errors."
        )

    # Find the most recently created ticket, or self-heal by creating one
    store = TicketStore(golem_dir / "tickets")
    all_tickets = await store.list_tickets()
    if not all_tickets:
        print("[PLANNER] Warning: planner did not call create_ticket — creating fallback ticket", file=sys.stderr)
        # Self-heal: create the ticket the planner should have created
        overview = golem_dir / "plans" / "overview.md"
        blueprint = overview.read_text(encoding="utf-8")[:500] if overview.exists() else ""
        plan_files = sorted((golem_dir / "plans").glob("task-*.md"))
        from golem.tickets import Ticket, TicketContext

        ticket = Ticket(
            id="",
            type="task",
            title=f"Tech Lead: Execute {overview}",
            status="pending",
            priority="high",
            created_by="planner-fallback",
            assigned_to="tech_lead",
            context=TicketContext(
                plan_file=str(overview),
                references=[str(p) for p in plan_files],
                blueprint=blueprint,
                acceptance=["All tasks completed", "All QA checks pass", "PR created"],
            ),
            history=[],
        )
        ticket_id = await store.create(ticket)
        return ticket_id

    # Return the last ticket created (by ID sort — TICKET-001, TICKET-002, etc.)
    last_ticket = sorted(all_tickets, key=lambda t: t.id)[-1]
    return last_ticket.id
