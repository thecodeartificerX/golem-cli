from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from rich.console import Console
from rich.table import Table

from golem.client import GolemClient, find_server
from golem.conductor import ClassificationResult, classify_spec
from golem.config import GolemConfig, load_config, save_config
from golem.qa import detect_infrastructure_checks

if TYPE_CHECKING:
    from golem.events import EventBus
from golem.orchestrator import WaveExecutor, assign_waves, build_dag
from golem.planner import run_planner
from golem.progress import ProgressLogger
from golem.tech_lead import run_tech_lead
from golem.tickets import TicketStore
from golem.writer import spawn_junior_dev

app = typer.Typer(
    name="golem",
    help="Autonomous spec executor with ticket-driven agent hierarchy.",
    invoke_without_command=True,
    no_args_is_help=True,
)
console = Console()

server_app = typer.Typer(name="server", help="Manage the Golem server.")
app.add_typer(server_app, name="server")


@server_app.command()
def start(
    port: int = typer.Option(7665, "--port", help="Server port"),
    host: str = typer.Option("127.0.0.1", "--host", help="Server host"),
) -> None:
    """Start the Golem server as a background process."""
    import subprocess as sp

    project_root = _get_project_root()
    golem_dir = _get_golem_dir(project_root)
    golem_dir.mkdir(parents=True, exist_ok=True)
    server_json = golem_dir / "server.json"

    if server_json.exists():
        info = json.loads(server_json.read_text(encoding="utf-8"))
        console.print(f"Server already running (PID {info.get('pid')}, port {info.get('port')})")
        return

    proc = sp.Popen(
        ["uv", "run", "python", "-m", "uvicorn", "golem.server:create_app",
         "--factory", "--host", host, "--port", str(port)],
        cwd=str(project_root),
    )

    server_json.write_text(json.dumps({
        "pid": proc.pid,
        "port": port,
        "host": host,
    }, indent=2), encoding="utf-8")
    console.print(f"Server started (PID {proc.pid}, port {port})")


@server_app.command()
def stop() -> None:
    """Stop the running Golem server."""
    import signal

    project_root = _get_project_root()
    golem_dir = _get_golem_dir(project_root)
    server_json = golem_dir / "server.json"

    if not server_json.exists():
        console.print("No server running.")
        return

    info = json.loads(server_json.read_text(encoding="utf-8"))
    pid = info.get("pid")

    import os
    try:
        os.kill(pid, signal.SIGTERM)
        console.print(f"Server stopped (PID {pid})")
    except (ProcessLookupError, OSError):
        console.print(f"Server process {pid} not found (already stopped?)")

    server_json.unlink(missing_ok=True)


@server_app.command(name="status")
def server_status() -> None:
    """Show server status."""
    project_root = _get_project_root()
    golem_dir = _get_golem_dir(project_root)
    server_json = golem_dir / "server.json"

    if not server_json.exists():
        console.print("No server running.")
        return

    info = json.loads(server_json.read_text(encoding="utf-8"))
    console.print(f"Server running: PID={info.get('pid')}, port={info.get('port')}, host={info.get('host')}")


_GOLEM_DIR_NAME = ".golem"


def _resolve_spec_project_root(spec: Path) -> Path:
    """Walk up from the spec file to find the git root of the target project.

    Falls back to the spec file's parent directory if no .git directory is found.
    """
    candidate = spec.resolve().parent
    while candidate != candidate.parent:
        if (candidate / ".git").exists():
            return candidate
        candidate = candidate.parent
    return spec.resolve().parent



def _get_golem_dir(project_root: Path) -> Path:
    return project_root / _GOLEM_DIR_NAME


def _get_project_root() -> Path:
    return Path.cwd()


def _parse_cost_events(golem_dir: Path) -> list[dict[str, str]]:
    """Parse AGENT_COST events from progress.log."""
    log_path = golem_dir / "progress.log"
    if not log_path.exists():
        return []
    events: list[dict[str, str]] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if "AGENT_COST" not in line:
            continue
        parts = line.split("AGENT_COST", 1)[1].strip()
        event: dict[str, str] = {}
        for pair in parts.split():
            if "=" in pair:
                k, v = pair.split("=", 1)
                event[k] = v
        events.append(event)
    return events


def _create_golem_dirs(golem_dir: Path) -> None:
    for subdir in ("tickets", "research", "plans", "references", "reports", "worktrees", "handoffs"):
        (golem_dir / subdir).mkdir(parents=True, exist_ok=True)


def _ensure_server(project_root: Path) -> tuple[str, int]:
    """Find or auto-start server. Returns (host, port). Raises RuntimeError if unavailable."""
    import time as _time

    result = find_server(project_root)
    if result is not None:
        return result

    golem_dir = _get_golem_dir(project_root)
    golem_dir.mkdir(parents=True, exist_ok=True)
    import subprocess as _sp
    _sp.Popen(
        ["uv", "run", "golem", "server", "start"],
        cwd=str(project_root),
        stdout=_sp.DEVNULL,
        stderr=_sp.DEVNULL,
    )

    deadline = _time.monotonic() + 5.0
    while _time.monotonic() < deadline:
        _time.sleep(0.5)
        result = find_server(project_root)
        if result is not None:
            return result

    raise RuntimeError("Server did not start within 5 seconds. Try 'golem server start' manually.")


def _require_server(project_root: Path) -> GolemClient | None:
    """Return GolemClient if server is running, else print message and return None."""
    result = find_server(project_root)
    if result is None:
        console.print("Server not running. Start with 'golem server start' or use 'golem run --no-server' for direct execution.")
        return None
    host, port = result
    return GolemClient(host, port)


def _build_critique_prompt(golem_dir: Path) -> str:
    """Build the self-critique prompt by reading the generated plan files."""
    plans_dir = golem_dir / "plans"
    parts: list[str] = []

    overview_path = plans_dir / "overview.md"
    if overview_path.exists():
        parts.append(f"## plans/overview.md\n\n{overview_path.read_text(encoding='utf-8')}")

    for task_file in sorted(plans_dir.glob("task-*.md")):
        parts.append(f"## {task_file.name}\n\n{task_file.read_text(encoding='utf-8')}")

    plan_text = "\n\n---\n\n".join(parts) if parts else "(no plan files found)"

    return (
        "You are a senior technical reviewer. Read the following implementation plan carefully.\n\n"
        "Identify and document:\n"
        "1. Missing edge cases not covered by the acceptance criteria\n"
        "2. Under-specified acceptance criteria (too vague to verify)\n"
        "3. Security considerations that should be addressed\n"
        "4. Ordering risks — tasks that depend on each other but are not sequenced correctly\n\n"
        "Write your findings to `"
        + str(golem_dir / "plans" / "critique.md")
        + "` using the Write tool. Be concise and actionable.\n\n"
        "## Plan Files\n\n"
        + plan_text
    )


async def _run_self_critique(
    golem_dir: Path,
    config: GolemConfig,
    project_root: Path,
    event_bus: EventBus | None = None,
) -> None:
    """Spawn a short planner-model session to critique the generated plan.

    Reads plans/overview.md and all task-*.md files. Writes critique findings
    back to plans/critique.md. The Tech Lead prompt includes plans/critique.md
    if it exists. Advisory only — non-fatal if fails.
    """
    from golem.planner import _run_planner_session  # reuse session machinery

    critique_prompt = _build_critique_prompt(golem_dir)
    result = await _run_planner_session(critique_prompt, golem_dir, config, project_root, event_bus=event_bus)
    critique_path = golem_dir / "plans" / "critique.md"
    if not critique_path.exists() and result.result_text:
        critique_path.write_text(result.result_text, encoding="utf-8")


async def _stream_to_console(client: GolemClient, session_id: str) -> None:
    """Stream SSE events from server to console."""
    import httpx as _httpx
    try:
        async for event in client.stream_events(session_id):
            msg = event.get("message") or event.get("data") or str(event)
            console.print(str(msg))
    except (_httpx.ConnectError, _httpx.RemoteProtocolError, _httpx.ReadError):
        pass  # Server closed connection normally (session ended or server stopped)
    except Exception as exc:
        console.print(f"[red]Stream error: {exc}[/red]")
        raise


def _validate_spec(spec: Path) -> None:
    """Validate that a spec file exists and has meaningful content."""
    if not spec.exists():
        console.print(f"[red]Spec file not found: {spec}[/red]")
        raise typer.Exit(1)
    if not spec.suffix == ".md":
        console.print(f"[red]Spec must be a markdown file (.md), got: {spec.suffix}[/red]")
        raise typer.Exit(1)
    content = spec.read_text(encoding="utf-8").strip()
    if not content:
        console.print("[red]Spec file is empty.[/red]")
        raise typer.Exit(1)
    if len(content) < 50:
        console.print(f"[yellow]Warning: spec is very short ({len(content)} chars) — may not have enough detail for planning.[/yellow]")
    # Check for at least one heading or task marker
    has_structure = any(line.strip().startswith(("#", "**", "- [")) for line in content.splitlines())
    if not has_structure:
        console.print("[yellow]Warning: spec has no headings or task markers — Lead Architect may struggle to extract tasks.[/yellow]")


@app.command()
def run(
    specs: list[Path] = typer.Argument(..., help="Path(s) to spec markdown file(s)"),
    force: bool = typer.Option(False, "--force", help="Skip confirmation prompts (for CI/non-interactive)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Run planner only, skip Tech Lead execution"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose debug output"),
    no_classify: bool = typer.Option(False, "--no-classify", help="Skip complexity classification, run STANDARD pipeline"),
    tier: str = typer.Option("", "--tier", help="Override complexity tier: TRIVIAL|SIMPLE|STANDARD|CRITICAL"),
    session_id: str = typer.Option("", "--session-id", help="Session ID for multi-spec execution"),
    golem_dir_override: str = typer.Option("", "--golem-dir", help="Override .golem directory path"),
    no_server: bool = typer.Option(False, "--no-server", help="Run directly without server (current behavior)"),
    orchestrator: bool = typer.Option(False, "--orchestrator/--no-orchestrator",
                                      help="Use Python wave orchestrator instead of Tech Lead agent"),
    timeout: int = typer.Option(0, "--timeout", help="Kill run after N minutes (0 = no limit)"),
) -> None:
    """Full autonomous run: plan, orchestrate writers, validate, create PR.

    Example: golem run spec.md
    Example: golem run spec1.md spec2.md
    Example: golem run spec.md --force --dry-run
    """
    from golem import __version__

    if verbose:
        import os
        os.environ["GOLEM_DEBUG"] = "1"

    console.print(f"[bold cyan]Golem[/bold cyan] v{__version__} (v2 ticket-driven)")

    spec = specs[0]

    # Support reading spec from stdin via "-"
    if str(spec) == "-":
        import sys
        import tempfile

        stdin_content = sys.stdin.read()
        if not stdin_content.strip():
            console.print("[red]No spec content received from stdin.[/red]")
            raise typer.Exit(1)

        # Write to temp file
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8")
        tmp.write(stdin_content)
        tmp.close()
        spec = Path(tmp.name)
        console.print(f"[dim]Read spec from stdin ({len(stdin_content)} chars) → {spec}[/dim]")

    if not no_server:
        _validate_spec(spec)
        project_root = _get_project_root()
        try:
            host, port = _ensure_server(project_root)
        except RuntimeError as exc:
            console.print(f"[red]Failed to start server: {exc}[/red]")
            raise typer.Exit(1)
        client = GolemClient(host, port)
        session_ids: list[str] = []
        for s in specs:
            try:
                sess_result = asyncio.run(client.create_session(str(s.resolve()), str(project_root)))
                sid = str(sess_result.get("session_id", ""))
                session_ids.append(sid)
                console.print(f"Session: {sid} (streaming logs...)")
            except Exception as exc:
                console.print(f"[red]Failed to create session for {s}: {exc}[/red]")
                raise typer.Exit(1)
        if session_ids:
            try:
                asyncio.run(_stream_to_console(client, session_ids[0]))
            except KeyboardInterrupt:
                pass
        return

    _validate_spec(spec)
    project_root = _get_project_root()
    if golem_dir_override:
        golem_dir = Path(golem_dir_override)
    else:
        golem_dir = _get_golem_dir(project_root)

    # Warn if stale state exists from a previous run
    tickets_dir = golem_dir / "tickets"
    if tickets_dir.exists() and any(tickets_dir.glob("*.json")):
        if not force:
            console.print("[yellow]Warning: .golem/ has existing tickets from a previous run.[/yellow]")
            console.print("  Run 'golem clean' first, or use --force to overwrite.")
            raise typer.Exit(1)
        console.print("[yellow]--force: overwriting existing .golem/ state[/yellow]")
        shutil.rmtree(golem_dir, ignore_errors=True)

    _create_golem_dirs(golem_dir)

    config = load_config(golem_dir)
    if session_id:
        config.session_id = session_id
        config.branch_prefix = f"golem/{session_id}"
    spec_project_root = _resolve_spec_project_root(spec)
    config.infrastructure_checks = detect_infrastructure_checks(spec_project_root)

    if no_classify:
        config.conductor_enabled = False

    # Default classification (used when conductor_enabled is False)
    classification = ClassificationResult("STANDARD", "conductor disabled", 1.0)

    if tier:
        if tier not in ("TRIVIAL", "SIMPLE", "STANDARD", "CRITICAL"):
            console.print(f"[red]Unknown tier: {tier}. Must be TRIVIAL, SIMPLE, STANDARD, or CRITICAL.[/red]")
            raise typer.Exit(1)
        classification = ClassificationResult(tier, "CLI override", 1.0)
        config.apply_complexity_profile(tier)
    elif config.conductor_enabled and not force:
        spec_text = spec.read_text(encoding="utf-8")
        classification = classify_spec(spec_text, "")
        config.apply_complexity_profile(classification.complexity)

    save_config(config, golem_dir)

    progress = ProgressLogger(golem_dir)
    t0 = time.monotonic()

    console.print(
        f"  Complexity: [bold]{classification.complexity}[/bold] "
        f"({classification.reasoning}) "
        f"research={'off' if config.skip_research else 'on'} "
        f"qa={config.qa_depth} "
        f"retries={config.max_writer_retries}"
    )
    console.print(f"  Spec:    {spec.resolve()}")
    console.print(f"  Project: {spec_project_root}")
    console.print(f"  Models:  planner={config.planner_model}, tech_lead={config.tech_lead_model}, worker={config.worker_model}")
    console.print(f"  Limits:  parallel={config.max_parallel}, retries={config.max_retries}, worker_turns={config.max_worker_turns}")
    if config.infrastructure_checks:
        console.print(f"  Infra:   {', '.join(config.infrastructure_checks)}")

    async def _run_async() -> None:
        from golem.events import EventBus, FileBackend
        events_path = golem_dir / "events.jsonl"
        event_bus = EventBus(FileBackend(events_path), session_id=config.session_id)

        progress.log_session_start(
            session_id=config.session_id or "local",
            spec_path=str(spec.resolve()),
        )
        _session_status = "failed"
        try:
            progress.log_classification(classification.complexity, classification.reasoning)
            progress.log_planner_start()
            t_plan = time.monotonic()
            with console.status("[bold cyan]Planning...[/bold cyan] Lead Architect analyzing spec", spinner="dots"):
                planner_result = await run_planner(spec, golem_dir, config, project_root, event_bus=event_bus)
            ticket_id = planner_result.ticket_id
            plan_elapsed = time.monotonic() - t_plan
            plan_m, plan_s = divmod(int(plan_elapsed), 60)
            progress.log_planner_complete(ticket_id)
            console.print(f"  Lead Architect completed in {plan_m}m {plan_s}s -- ticket: {ticket_id}")

            # Show ticket summary before handing off
            store = TicketStore(golem_dir / "tickets")
            ticket = await store.read(ticket_id)
            console.print(f"  Title:     {ticket.title[:70]}")
            if ticket.context.plan_file:
                console.print(f"  Plan file: {ticket.context.plan_file}")
            if ticket.context.references:
                console.print(f"  References: {len(ticket.context.references)} file(s)")

            if dry_run:
                console.print("[bold yellow]--dry-run: Lead Architect done. Skipping Tech Lead.[/bold yellow]")
                _session_status = "done"
                return

            if config.self_critique_enabled:
                console.print("  Self-critique: reviewing plan for CRITICAL tier...")
                try:
                    await _run_self_critique(golem_dir, config, project_root, event_bus=event_bus)
                except Exception as critique_err:
                    console.print(f"  [dim]Self-critique failed (non-fatal): {critique_err}[/dim]")

            # Choose execution path: orchestrator vs Tech Lead vs trivial single-writer
            use_orchestrator = orchestrator or config.orchestrator_enabled

            if use_orchestrator:
                console.print("[bold cyan]Golem[/bold cyan] -- Orchestrator executing (wave-based)...")

                def _on_ticket_complete(ticket_id: str, outcome: str, completed: int, total: int) -> None:
                    pct = int(100 * completed / total) if total > 0 else 0
                    style = "[green]" if outcome == "passed" else "[yellow]" if outcome == "skipped" else "[red]"
                    console.print(f"  {style}{ticket_id}[/] {outcome} [{completed}/{total} tickets, {pct}%]")

                executor = WaveExecutor(
                    golem_dir=golem_dir,
                    project_root=spec_project_root,
                    config=config,
                    event_bus=event_bus,
                    on_ticket_complete=_on_ticket_complete,
                )
                orch_result = await executor.run()
                elapsed = time.monotonic() - t0
                mins, secs = divmod(int(elapsed), 60)
                progress.log_run_cost_summary(orch_result.total_cost_usd)
                if orch_result.total_cost_usd > 0:
                    console.print(f"[dim]Run cost: ${orch_result.total_cost_usd:.4f}[/dim]")
                console.print(
                    f"[bold]Orchestration complete in {mins}m {secs}s:[/bold] "
                    f"{orch_result.tickets_passed} passed, {orch_result.tickets_failed} failed, "
                    f"{orch_result.tickets_skipped} skipped across {orch_result.waves_completed} waves"
                )
                _session_status = "done"
                return

            # Check if Tech Lead should be skipped (TRIVIAL complexity)
            if config.skip_tech_lead:
                console.print("  [dim]TRIVIAL: skipping Tech Lead, dispatching single Junior Dev[/dim]")
                writer_result = await spawn_junior_dev(ticket, str(project_root), config, golem_dir, event_bus=event_bus)
                total_cost = (planner_result.cost_usd or 0.0) + (writer_result.cost_usd or 0.0)
                progress.log_run_cost_summary(total_cost)
                if total_cost > 0:
                    console.print(f"[dim]Run cost: ${total_cost:.4f}[/dim]")
                elapsed = time.monotonic() - t0
                mins, secs = divmod(int(elapsed), 60)
                console.print(f"[bold]Run complete in {mins}m {secs}s.[/bold]")
                _session_status = "done"
                return

            console.print("[bold cyan]Golem[/bold cyan] -- Tech Lead executing...")
            progress.log_tech_lead_start(ticket_id)
            tech_lead_result = await run_tech_lead(ticket_id, golem_dir, config, project_root, event_bus=event_bus)
            elapsed = time.monotonic() - t0
            mins, secs = divmod(int(elapsed), 60)
            progress.log_tech_lead_complete(elapsed_s=elapsed)
            total_cost = progress.sum_agent_costs()
            progress.log_run_cost_summary(total_cost)
            if total_cost > 0:
                console.print(f"[dim]Run cost: ${total_cost:.4f}[/dim]")
            console.print(f"[bold]Run complete in {mins}m {secs}s.[/bold]")

            # Final summary
            all_tickets = await store.list_tickets()

            # Check for escalation tickets that need operator attention
            escalations = [t for t in all_tickets if t.type == "escalation" and t.status == "pending"]
            if escalations:
                console.print(f"\n[red][!] {len(escalations)} escalation(s) need operator attention:[/red]")
                for esc in escalations:
                    console.print(f"  [red]{esc.id}[/red]: {esc.title}")

            # Check for unresolved blockers
            blockers = [t for t in all_tickets if t.type == "blocker" and t.status == "pending"]
            if blockers:
                console.print(f"\n[yellow][!] {len(blockers)} unresolved blocker(s):[/yellow]")
                for blk in blockers:
                    console.print(f"  [yellow]{blk.id}[/yellow]: {blk.title}")

            if all_tickets:
                by_status: dict[str, int] = {}
                for t in all_tickets:
                    by_status[t.status] = by_status.get(t.status, 0) + 1
                parts = [f"{count} {status}" for status, count in sorted(by_status.items())]
                console.print(f"  Tickets:    {', '.join(parts)}")

            plans_dir = golem_dir / "plans"
            research_dir = golem_dir / "research"
            refs_dir = golem_dir / "references"
            plan_count = len(list(plans_dir.glob("task-*.md"))) if plans_dir.exists() else 0
            research_count = len(list(research_dir.glob("*.md"))) if research_dir.exists() else 0
            ref_count = len(list(refs_dir.glob("*.md"))) if refs_dir.exists() else 0
            console.print(f"  Artifacts:  {plan_count} plans, {research_count} research, {ref_count} references")
            _session_status = "done"
        finally:
            progress.log_session_complete(
                session_id=config.session_id or "local",
                status=_session_status,
            )

    async def _run_with_timeout() -> None:
        if timeout > 0:
            try:
                await asyncio.wait_for(_run_async(), timeout=timeout * 60)
            except asyncio.TimeoutError:
                console.print(f"\n[red]Timeout: run exceeded {timeout} minute(s). Aborting.[/red]")
                raise typer.Exit(1)
        else:
            await _run_async()

    try:
        asyncio.run(_run_with_timeout())
    except RuntimeError as e:
        console.print(f"\n[red]Error: {e}[/red]")
        raise typer.Exit(1)


@app.command()
def plan(
    spec: Path = typer.Argument(..., help="Path to spec markdown file"),
) -> None:
    """Dry run — generate plans only, no Tech Lead execution.

    Example: golem plan spec.md
    """
    _validate_spec(spec)
    project_root = _get_project_root()
    golem_dir = _get_golem_dir(project_root)
    _create_golem_dirs(golem_dir)

    config = load_config(golem_dir)

    async def _plan_async() -> None:
        console.print("[bold cyan]Golem[/bold cyan] — Planning (dry run)...")
        planner_result = await run_planner(spec, golem_dir, config, project_root)
        ticket_id = planner_result.ticket_id
        console.print(f"[bold green]Plan complete.[/bold green] Ticket: {ticket_id}")

        # Show plan summary
        plans_dir = golem_dir / "plans"
        research_dir = golem_dir / "research"
        refs_dir = golem_dir / "references"
        task_plans = list(plans_dir.glob("task-*.md")) if plans_dir.exists() else []
        research_files = list(research_dir.glob("*.md")) if research_dir.exists() else []
        ref_files = list(refs_dir.glob("*.md")) if refs_dir.exists() else []
        has_overview = (plans_dir / "overview.md").exists() if plans_dir.exists() else False

        console.print(f"\n[bold]Plan Summary:[/bold]")
        console.print(f"  Overview:   {'yes' if has_overview else 'MISSING'}")
        console.print(f"  Task plans: {len(task_plans)}")
        console.print(f"  Research:   {len(research_files)} file(s)")
        console.print(f"  References: {len(ref_files)} file(s)")
        console.print(f"  Output:     {plans_dir}")

    try:
        asyncio.run(_plan_async())
    except RuntimeError as e:
        console.print(f"\n[red]Error: {e}[/red]")
        raise typer.Exit(1)


def _print_dag(tickets: list) -> None:
    """Print dependency graph as wave-grouped tree using Rich."""
    from rich.tree import Tree

    try:
        nodes = build_dag(tickets)
        waves = assign_waves(nodes)
    except Exception as e:
        console.print(f"[red]Could not build DAG: {e}[/red]")
        return

    root = Tree("[bold]Execution Waves[/bold]")
    for wave_num in sorted(waves.keys()):
        ticket_ids = waves[wave_num]
        wave_branch = root.add(f"[cyan]Wave {wave_num}[/cyan] ({len(ticket_ids)} tickets, parallel)")
        for tid in ticket_ids:
            node = nodes[tid]
            deps_str = f"  <- {', '.join(node.depends_on)}" if node.depends_on else ""
            wave_branch.add(f"[green]{tid}[/green]{deps_str}")

    console.print(root)


@app.command()
def status(
    session_id: str = typer.Argument("", help="Session ID for server-routed session detail"),
    dag: bool = typer.Option(False, "--dag", help="Print dependency graph as wave-grouped tree"),
) -> None:
    """Show current run progress from ticket store."""
    project_root = _get_project_root()

    if session_id:
        client = _require_server(project_root)
        if client is None:
            return
        async def _session_detail() -> None:
            try:
                data = await client.get_session(session_id)
                console.print(f"Session: {data.get('id')}")
                console.print(f"  Status:    {data.get('status')}")
                console.print(f"  Spec:      {data.get('spec_path')}")
                console.print(f"  PID:       {data.get('pid')}")
            except Exception as exc:
                console.print(f"[red]Failed to get session: {exc}[/red]")
        asyncio.run(_session_detail())
        return

    # Auto-detect running server (mirrors history/stats pattern)
    server_info = find_server(project_root)
    if server_info is not None:
        _srv_host, _srv_port = server_info
        async def _server_status() -> None:
            try:
                import httpx as _httpx
                async with _httpx.AsyncClient(base_url=f"http://{_srv_host}:{_srv_port}") as http:
                    resp = await http.get("/api/sessions", timeout=15.0)
                    resp.raise_for_status()
                    sessions = resp.json()
                if not sessions:
                    console.print("[dim]No active sessions on server.[/dim]")
                    return
                from rich.table import Table
                table = Table(title="Server Sessions")
                table.add_column("Session ID")
                table.add_column("Status")
                table.add_column("Spec")
                for s in sessions:
                    table.add_row(s.get("id", "?"), s.get("status", "?"), s.get("spec_path", "?"))
                console.print(table)
            except Exception as exc:
                console.print(f"[red]Failed to get server status: {exc}[/red]")
        asyncio.run(_server_status())
        return

    golem_dir = _get_golem_dir(project_root)
    tickets_dir = golem_dir / "tickets"

    if not tickets_dir.exists():
        console.print("[dim]No active run. Use 'golem run <spec>' to start one.[/dim]")
        return

    async def _status_async() -> None:
        store = TicketStore(tickets_dir)
        tickets = await store.list_tickets()

        if not tickets:
            console.print("[yellow]No tickets found.[/yellow]")
            return

        table = Table(title="Golem Status", show_header=True)
        table.add_column("ID", style="cyan")
        table.add_column("Status")
        table.add_column("Priority")
        table.add_column("Assigned")
        table.add_column("Title")
        table.add_column("Last Event", style="dim")

        status_styles: dict[str, str] = {
            "approved": "[green]approved[/green]",
            "done": "[green]done[/green]",
            "qa_passed": "[green]qa_passed[/green]",
            "blocked": "[red]blocked[/red]",
            "needs_work": "[yellow]needs_work[/yellow]",
            "in_progress": "[yellow]in_progress[/yellow]",
            "ready_for_review": "[cyan]ready_for_review[/cyan]",
            "pending": "[dim]pending[/dim]",
        }
        priority_styles: dict[str, str] = {
            "high": "[red]high[/red]",
            "medium": "[yellow]medium[/yellow]",
            "low": "[dim]low[/dim]",
        }
        for ticket in sorted(tickets, key=lambda t: t.id):
            styled_status = status_styles.get(ticket.status, ticket.status)
            styled_priority = priority_styles.get(ticket.priority, ticket.priority)
            last_event = ""
            if ticket.history:
                last = ticket.history[-1]
                last_event = f"{last.ts[:16]} {last.action}"
            table.add_row(
                ticket.id, styled_status, styled_priority,
                ticket.assigned_to, ticket.title[:50], last_event,
            )

        # Summary line
        total = len(tickets)
        done_count = sum(1 for t in tickets if t.status in ("done", "approved", "qa_passed", "ready_for_review"))
        in_prog = sum(1 for t in tickets if t.status == "in_progress")
        console.print(table)
        console.print(f"  {done_count}/{total} complete, {in_prog} in progress")

        if dag:
            _print_dag(tickets)

    asyncio.run(_status_async())


@app.command()
def resume(
    session_id: str = typer.Argument("", help="Session ID to resume via server (empty = resume local run)"),
) -> None:
    """Resume a session via server, or resume interrupted local run from ticket store."""
    project_root = _get_project_root()

    if session_id:
        client = _require_server(project_root)
        if client is None:
            return
        try:
            asyncio.run(client.resume_session(session_id))
            console.print(f"Session {session_id} resumed.")
        except Exception as exc:
            console.print(f"[red]Failed to resume session: {exc}[/red]")
            raise typer.Exit(1)
        return

    golem_dir = _get_golem_dir(project_root)
    tickets_dir = golem_dir / "tickets"

    if not tickets_dir.exists():
        console.print("[red]No tickets found.[/red]")
        raise typer.Exit(1)

    async def _resume_async() -> None:
        store = TicketStore(tickets_dir)
        all_tickets = await store.list_tickets()

        if not all_tickets:
            console.print("[yellow]No tickets found. Run 'golem run <spec>' first.[/yellow]")
            return

        # Prefer the tech_lead ticket; fall back to any pending/in_progress ticket
        tech_lead_tickets = [t for t in all_tickets if t.assigned_to == "tech_lead" and t.status not in ("done", "approved")]
        pending = [t for t in all_tickets if t.status in ("pending", "in_progress")]
        candidates = tech_lead_tickets or pending

        if not candidates:
            console.print("[yellow]All tickets are done or approved — nothing to resume.[/yellow]")
            return

        ticket = sorted(candidates, key=lambda t: t.id)[0]
        config = load_config(golem_dir)
        spec_project_root = _resolve_spec_project_root(Path(ticket.context.plan_file)) if ticket.context.plan_file else project_root
        config.infrastructure_checks = detect_infrastructure_checks(spec_project_root)

        progress = ProgressLogger(golem_dir)
        console.print(f"[bold cyan]Golem[/bold cyan] -- Resuming from ticket {ticket.id} ({ticket.title[:50]})...")
        progress.log_tech_lead_start(ticket.id)
        await run_tech_lead(ticket.id, golem_dir, config, project_root)
        progress.log_tech_lead_complete()
        console.print("[bold]Resume complete.[/bold]")

    try:
        asyncio.run(_resume_async())
    except RuntimeError as e:
        console.print(f"\n[red]Error: {e}[/red]")
        raise typer.Exit(1)


@app.command()
def version() -> None:
    """Show Golem version, Python version, and platform."""
    from golem.version import get_version_info

    info = get_version_info()
    console.print(f"[bold cyan]Golem[/bold cyan] v{info['version']}")
    console.print(f"Architecture {info['architecture']}")
    console.print(f"Python {info['python']}")
    console.print(f"Platform {info['platform']}")
    # Count test functions in tests/ directory
    tests_dir = Path(__file__).parent.parent.parent / "tests"
    if tests_dir.exists():
        test_count = sum(
            1 for p in tests_dir.glob("test_*.py")
            for line in p.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith("def test_") or line.strip().startswith("async def test_")
        )
        console.print(f"Tests {test_count}")


@app.command()
def ui(
    port: int = typer.Option(7665, help="Port to serve the dashboard on"),
    debug: bool = typer.Option(False, help="Enable debug logging to terminal"),
) -> None:
    """Launch the Golem web dashboard."""
    import webbrowser

    from golem.ui import configure_logging, start_server

    configure_logging(debug=debug)
    console.print(f"Golem UI running at http://localhost:{port}")
    webbrowser.open(f"http://localhost:{port}")
    start_server(host="127.0.0.1", port=port, debug=debug)


@app.command()
def history(
    session_id: str = typer.Argument("", help="Session ID for server-routed history"),
) -> None:
    """Show chronological event timeline across all tickets."""
    project_root = _get_project_root()

    server_info = find_server(project_root)
    if server_info is not None:
        _srv_host, _srv_port = server_info
        async def _server_history() -> None:
            try:
                import httpx as _httpx
                url = f"/api/history?session_id={session_id}" if session_id else "/api/history"
                async with _httpx.AsyncClient(base_url=f"http://{_srv_host}:{_srv_port}") as http:
                    resp = await http.get(url, timeout=15.0)
                    resp.raise_for_status()
                    items = resp.json()
                if not items:
                    console.print("[dim]No history entries found.[/dim]")
                    return
                table = Table(title="Session History", show_header=True)
                table.add_column("Session", style="cyan")
                table.add_column("Timestamp", style="dim")
                table.add_column("Message")
                for entry in items:
                    table.add_row(
                        str(entry.get("session_id", "")),
                        str(entry.get("timestamp", ""))[:19],
                        str(entry.get("message", "")),
                    )
                console.print(table)
            except Exception as exc:
                console.print(f"[red]Failed to get history: {exc}[/red]")
        asyncio.run(_server_history())
        return

    if session_id:
        console.print("[dim]Server not running — cannot fetch session history by ID.[/dim]")
        return

    golem_dir = _get_golem_dir(project_root)
    tickets_dir = golem_dir / "tickets"

    if not tickets_dir.exists():
        console.print("[dim]No active run. Use 'golem run <spec>' to start one.[/dim]")
        return

    async def _history_async() -> None:
        store = TicketStore(tickets_dir)
        tickets = await store.list_tickets()

        if not tickets:
            console.print("[yellow]No tickets found.[/yellow]")
            return

        # Flatten all events with their ticket ID, sort by timestamp
        events: list[tuple[str, str, str, str, str]] = []  # (ts, ticket_id, agent, action, note)
        for ticket in tickets:
            for event in ticket.history:
                events.append((event.ts, ticket.id, event.agent, event.action, event.note))

        if not events:
            console.print("[yellow]No events recorded yet.[/yellow]")
            return

        events.sort(key=lambda e: e[0])

        table = Table(title="Golem Event Timeline", show_header=True)
        table.add_column("Timestamp", style="dim")
        table.add_column("Ticket", style="cyan")
        table.add_column("Agent")
        table.add_column("Action")
        table.add_column("Note")

        for ts, tid, agent, action, note in events:
            short_note = note[:60] + "..." if len(note) > 60 else note
            table.add_row(ts[:19], tid, agent, action, short_note)

        console.print(table)
        console.print(f"  {len(events)} event(s) across {len(tickets)} ticket(s)")

    asyncio.run(_history_async())


@app.command()
def inspect(
    ticket_id: str = typer.Argument(..., help="Ticket ID to inspect (e.g. TICKET-001)"),
    session: str = typer.Option("", "--session", help="Session ID for server-routed ticket lookup"),
) -> None:
    """Show full details of a single ticket."""
    project_root = _get_project_root()

    if session:
        client = _require_server(project_root)
        if client is None:
            return
        async def _session_ticket() -> None:
            try:
                tickets = await client.get_session_tickets(session)
                for t in tickets:
                    if str(t.get("id", "")).upper() == ticket_id.upper():
                        console.print(f"\n[bold cyan]{t.get('id')}[/bold cyan] -- {t.get('title')}")
                        console.print(f"  Status: {t.get('status')}")
                        return
                console.print(f"[red]Ticket {ticket_id} not found in session {session}.[/red]")
            except Exception as exc:
                console.print(f"[red]Failed to inspect ticket: {exc}[/red]")
        asyncio.run(_session_ticket())
        return

    # Auto-detect running server for ticket lookup
    server_info = find_server(project_root)
    if server_info is not None:
        _srv_host, _srv_port = server_info
        async def _server_inspect() -> None:
            try:
                import httpx as _httpx
                async with _httpx.AsyncClient(base_url=f"http://{_srv_host}:{_srv_port}") as http:
                    # Try all sessions to find the ticket
                    resp = await http.get("/api/sessions", timeout=15.0)
                    resp.raise_for_status()
                    sessions = resp.json()
                    for s in sessions:
                        sid = s.get("id", "")
                        tresp = await http.get(f"/api/sessions/{sid}/tickets", timeout=15.0)
                        if tresp.status_code == 200:
                            tickets = tresp.json()
                            for t in tickets:
                                if str(t.get("id", "")).upper() == ticket_id.upper():
                                    console.print(f"\n[bold cyan]{t.get('id')}[/bold cyan] -- {t.get('title')}")
                                    console.print(f"  Type: {t.get('type')}  |  Status: {t.get('status')}  |  Priority: {t.get('priority')}")
                                    console.print(f"  Created by: {t.get('created_by')}  |  Assigned to: {t.get('assigned_to')}")
                                    return
                console.print(f"[yellow]Ticket {ticket_id} not found on server.[/yellow]")
            except Exception as exc:
                console.print(f"[red]Failed to inspect via server: {exc}[/red]")
        asyncio.run(_server_inspect())
        return

    golem_dir = _get_golem_dir(project_root)
    tickets_dir = golem_dir / "tickets"

    if not tickets_dir.exists():
        console.print("[dim]No active run. Use 'golem run <spec>' to start one.[/dim]")
        return

    import re
    if not re.match(r"^TICKET-\d+$", ticket_id, re.IGNORECASE):
        console.print(f"[red]Invalid ticket ID format: {ticket_id}. Expected TICKET-NNN.[/red]")
        raise typer.Exit(1)

    async def _inspect_async() -> None:
        store = TicketStore(tickets_dir)
        try:
            ticket = await store.read(ticket_id)
        except (FileNotFoundError, KeyError):
            console.print(f"[red]Ticket {ticket_id} not found.[/red]")
            raise typer.Exit(1)
        except json.JSONDecodeError:
            console.print(f"[red]Ticket {ticket_id} file is corrupt (invalid JSON).[/red]")
            raise typer.Exit(1)

        # Header
        console.print(f"\n[bold cyan]{ticket.id}[/bold cyan] -- {ticket.title}")
        console.print(f"  Type: {ticket.type}  |  Status: {ticket.status}  |  Priority: {ticket.priority}")
        console.print(f"  Created by: {ticket.created_by}  |  Assigned to: {ticket.assigned_to}")

        # Context
        ctx = ticket.context
        if ctx.plan_file:
            console.print(f"\n[bold]Plan file:[/bold] {ctx.plan_file}")
        if ctx.blueprint:
            console.print(f"\n[bold]Blueprint:[/bold]\n  {ctx.blueprint[:300]}")
        if ctx.acceptance:
            console.print("\n[bold]Acceptance criteria:[/bold]")
            for a in ctx.acceptance:
                console.print(f"  - {a}")
        if ctx.qa_checks:
            console.print("\n[bold]QA checks:[/bold]")
            for q in ctx.qa_checks:
                console.print(f"  - {q}")
        if ctx.references:
            console.print("\n[bold]References:[/bold]")
            for r in ctx.references:
                console.print(f"  - {r}")
        if ctx.files:
            console.print(f"\n[bold]Pre-loaded files:[/bold] {', '.join(ctx.files.keys())}")

        # History
        if ticket.history:
            console.print("\n[bold]Event history:[/bold]")
            for event in ticket.history:
                note_preview = event.note[:80] + "..." if len(event.note) > 80 else event.note
                console.print(f"  [{event.ts[:19]}] {event.agent}: {event.action} -- {note_preview}")
        else:
            console.print("\n[dim]No events recorded.[/dim]")
        console.print()

    asyncio.run(_inspect_async())


@app.command()
def logs(
    session_id: str = typer.Argument("", help="Session ID for server log streaming"),
    follow: bool = typer.Option(False, "--follow", "-f", help="Follow mode — tail new lines as they appear"),
    lines: int = typer.Option(20, "--lines", "-n", help="Number of recent lines to show"),
) -> None:
    """Show progress.log entries."""
    import time

    project_root = _get_project_root()

    if session_id:
        client = _require_server(project_root)
        if client is None:
            return
        if follow:
            try:
                asyncio.run(_stream_to_console(client, session_id))
            except KeyboardInterrupt:
                pass
        else:
            async def _get_session_logs() -> None:
                try:
                    data = await client.get_session(session_id)
                    console.print(f"Session {session_id}: {data.get('status')}")
                except Exception as exc:
                    console.print(f"[red]Failed to get logs: {exc}[/red]")
            asyncio.run(_get_session_logs())
        return

    golem_dir = _get_golem_dir(project_root)
    log_path = golem_dir / "progress.log"

    if not log_path.exists():
        console.print("[yellow]No progress.log found. Run 'golem run <spec>' first.[/yellow]")
        raise typer.Exit(1)

    all_lines = log_path.read_text(encoding="utf-8").splitlines()
    # Show last N lines
    recent = all_lines[-lines:] if len(all_lines) > lines else all_lines
    for line in recent:
        console.print(line)

    if not follow:
        return

    # Follow mode: poll for new lines
    console.print("[dim]-- following (Ctrl+C to stop) --[/dim]")
    seen = len(all_lines)
    try:
        while True:
            time.sleep(1)
            current = log_path.read_text(encoding="utf-8").splitlines()
            if len(current) > seen:
                for line in current[seen:]:
                    console.print(line)
                seen = len(current)
    except KeyboardInterrupt:
        pass


@app.command()
def clean(
    session_id: str = typer.Argument("", help="Session ID to clean via server (empty = clean all local state)"),
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation prompt"),
) -> None:
    """Remove .golem/ state, worktrees, and golem/* branches.

    Example: golem clean --force
    """
    project_root = _get_project_root()

    if session_id:
        client = _require_server(project_root)
        if client is None:
            return
        try:
            asyncio.run(client.kill_session(session_id))
            console.print(f"Session {session_id} cleaned.")
        except Exception as exc:
            console.print(f"[red]Failed to clean session: {exc}[/red]")
            raise typer.Exit(1)
        return

    golem_dir = _get_golem_dir(project_root)

    if not golem_dir.exists():
        console.print("[yellow].golem/ does not exist -- nothing to clean.[/yellow]")
        return

    if not force:
        typer.confirm("This will delete all .golem/ state and golem/* branches. Continue?", abort=True)

    # Count files before deleting
    tickets_dir = golem_dir / "tickets"
    ticket_count = len(list(tickets_dir.glob("*.json"))) if tickets_dir.exists() else 0
    research_dir = golem_dir / "research"
    research_count = len(list(research_dir.glob("*.md"))) if research_dir.exists() else 0
    plans_dir = golem_dir / "plans"
    plan_count = len(list(plans_dir.glob("*.md"))) if plans_dir.exists() else 0

    # Remove worktrees via git
    worktrees_dir = golem_dir / "worktrees"
    wt_count = 0
    if worktrees_dir.exists():
        for wt in worktrees_dir.iterdir():
            if wt.is_dir():
                result = subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=project_root, capture_output=True)
                if result.returncode == 0:
                    wt_count += 1

    shutil.rmtree(golem_dir, ignore_errors=True)

    # Clean up golem/* branches left behind by previous runs
    result = subprocess.run(
        ["git", "branch", "--list", "golem/*"],
        cwd=project_root, capture_output=True, text=True, encoding="utf-8",
    )
    golem_branches = [b.strip().lstrip("* ") for b in result.stdout.splitlines() if b.strip()]
    for branch in golem_branches:
        subprocess.run(
            ["git", "branch", "-D", branch],
            cwd=project_root, capture_output=True, text=True, encoding="utf-8",
        )

    console.print("[bold green]Cleaned:[/bold green]")
    console.print(f"  {ticket_count} ticket(s), {plan_count} plan(s), {research_count} research file(s)")
    if wt_count:
        console.print(f"  {wt_count} worktree(s)")
    if golem_branches:
        console.print(f"  {len(golem_branches)} golem branch(es)")


@app.command()
def diff(
    session_id: str = typer.Argument("", help="Session ID for server-routed diff"),
    base: str = typer.Option("main", "--base", "-b", help="Base branch to diff against"),
) -> None:
    """Show git diff of changes from the last golem run."""
    project_root = _get_project_root()

    if session_id:
        client = _require_server(project_root)
        if client is None:
            return
        async def _session_diff() -> None:
            try:
                data = await client.get_session_diff(session_id)
                diff_text = data.get("diff", "")
                if diff_text:
                    console.print(str(diff_text))
                else:
                    console.print("[dim]No diff available.[/dim]")
            except Exception as exc:
                console.print(f"[red]Failed to get diff: {exc}[/red]")
        asyncio.run(_session_diff())
        return

    result = subprocess.run(
        ["git", "diff", base],
        cwd=project_root, capture_output=True, text=True, encoding="utf-8",
    )
    if result.returncode != 0:
        console.print(f"[red]git diff failed: {result.stderr.strip()}[/red]")
        raise typer.Exit(1)
    if not result.stdout.strip():
        console.print("[dim]No differences from {base}.[/dim]")
        return
    console.print(result.stdout)


@app.command()
def stats() -> None:
    """Show statistics from the current run's tickets."""
    project_root = _get_project_root()

    server_info = find_server(project_root)
    if server_info is not None:
        _srv_host, _srv_port = server_info
        async def _server_stats() -> None:
            try:
                import httpx as _httpx
                async with _httpx.AsyncClient(base_url=f"http://{_srv_host}:{_srv_port}") as http:
                    resp = await http.get("/api/stats", timeout=15.0)
                    resp.raise_for_status()
                    data = resp.json()
                console.print("[bold]Aggregate Server Statistics[/bold]\n")
                counts = data.get("session_counts", {})
                for status, count in sorted(counts.items()):
                    console.print(f"  {status}: {count}")
                console.print(f"\n  Total cost: ${data.get('total_cost', 0.0):.4f}")
                tc = data.get("ticket_counts", {})
                console.print(f"  Tickets: {tc.get('done', 0)} done / {tc.get('failed', 0)} failed / {tc.get('total', 0)} total")
                console.print(f"  Pass rate: {data.get('ticket_pass_rate', 0.0) * 100:.0f}%")
                console.print(f"  Active sessions: {data.get('active_sessions', 0)}")
            except Exception as exc:
                console.print(f"[red]Failed to get stats: {exc}[/red]")
        asyncio.run(_server_stats())
        return

    golem_dir = _get_golem_dir(project_root)
    tickets_dir = golem_dir / "tickets"

    if not tickets_dir.exists():
        console.print("[dim]No active run. Use 'golem run <spec>' to start one.[/dim]")
        return

    async def _stats_async() -> None:
        store = TicketStore(tickets_dir)
        tickets = await store.list_tickets()

        if not tickets:
            console.print("[yellow]No tickets found.[/yellow]")
            return

        total = len(tickets)
        by_status: dict[str, int] = {}
        for t in tickets:
            by_status[t.status] = by_status.get(t.status, 0) + 1

        done = by_status.get("done", 0) + by_status.get("approved", 0) + by_status.get("qa_passed", 0)
        failed = by_status.get("needs_work", 0) + by_status.get("blocked", 0)
        pass_rate = (done / total * 100) if total > 0 else 0

        console.print("[bold]Golem Run Statistics[/bold]\n")
        console.print(f"  Total tickets:  {total}")
        for status, count in sorted(by_status.items()):
            console.print(f"    {status}: {count}")
        console.print(f"\n  Pass rate:      {pass_rate:.0f}% ({done}/{total})")
        if failed:
            console.print(f"  Failed/blocked: {failed}")

        # Event count
        event_count = sum(len(t.history) for t in tickets)
        console.print(f"  Total events:   {event_count}")

        cost_events = _parse_cost_events(golem_dir)
        if cost_events:
            cost_table = Table(title="Run Economics", show_header=True, header_style="bold cyan")
            cost_table.add_column("Role", style="dim")
            cost_table.add_column("Cost", justify="right")
            cost_table.add_column("Details", style="dim")

            role_totals: dict[str, float] = {}
            role_details: dict[str, dict[str, int]] = {}
            for event in cost_events:
                role = event.get("role", "unknown")
                cost_str = event.get("cost", "$0").lstrip("$")
                try:
                    cost = float(cost_str)
                except ValueError:
                    cost = 0.0
                role_totals[role] = role_totals.get(role, 0.0) + cost
                if role not in role_details:
                    role_details[role] = {"input_tokens": 0, "output_tokens": 0, "turns": 0}
                try:
                    role_details[role]["input_tokens"] += int(event.get("input_tokens", 0))
                    role_details[role]["output_tokens"] += int(event.get("output_tokens", 0))
                    role_details[role]["turns"] += int(event.get("turns", 0))
                except ValueError:
                    pass

            run_total = sum(role_totals.values())
            for role, cost in sorted(role_totals.items()):
                d = role_details.get(role, {})
                in_k = d.get("input_tokens", 0) / 1000
                out_k = d.get("output_tokens", 0) / 1000
                turns = d.get("turns", 0)
                details = f"{in_k:.1f}K in / {out_k:.1f}K out / {turns} turns"
                cost_table.add_row(role, f"${cost:.4f}", details)
            cost_table.add_row("Total", f"${run_total:.4f}", "", style="bold")
            console.print(cost_table)

    asyncio.run(_stats_async())


@app.command()
def export(
    session_id: str = typer.Argument("", help="Session ID to export (empty = export all local state)"),
    output: Path = typer.Option(Path("golem-export.zip"), "--output", "-o", help="Output zip file path"),
) -> None:
    """Export .golem/ run artifacts as a zip archive."""
    import zipfile

    project_root = _get_project_root()
    golem_dir = _get_golem_dir(project_root)

    if not golem_dir.exists():
        console.print("[yellow]No .golem/ directory found -- nothing to export.[/yellow]")
        return

    count = 0
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(golem_dir.rglob("*")):
            if f.is_file():
                arcname = f".golem/{f.relative_to(golem_dir)}"
                zf.write(f, arcname)
                count += 1

    console.print(f"[green]Exported {count} file(s) to {output}[/green]")


# --------------------------------------------------------------------------
# Session management commands (server-routed)
# --------------------------------------------------------------------------


@app.command()
def pause(
    session_id: str = typer.Argument(..., help="Session ID to pause"),
) -> None:
    """Pause a running session (routes to server)."""
    project_root = _get_project_root()
    client = _require_server(project_root)
    if client is None:
        return
    try:
        asyncio.run(client.pause_session(session_id))
        console.print(f"Session {session_id} paused.")
    except Exception as exc:
        console.print(f"[red]Failed to pause session: {exc}[/red]")
        raise typer.Exit(1)


@app.command()
def kill(
    session_id: str = typer.Argument(..., help="Session ID to kill"),
) -> None:
    """Kill a running session (routes to server)."""
    project_root = _get_project_root()
    client = _require_server(project_root)
    if client is None:
        return
    try:
        asyncio.run(client.kill_session(session_id))
        console.print(f"Session {session_id} killed.")
    except Exception as exc:
        console.print(f"[red]Failed to kill session: {exc}[/red]")
        raise typer.Exit(1)


@app.command()
def guidance(
    session_id: str = typer.Argument(..., help="Session ID to send guidance to"),
    text: str = typer.Argument(..., help="Guidance text to send to the Tech Lead"),
) -> None:
    """Send operator guidance to a running session (routes to server)."""
    project_root = _get_project_root()
    client = _require_server(project_root)
    if client is None:
        return
    try:
        asyncio.run(client.send_guidance(session_id, text))
        console.print(f"Guidance sent to session {session_id}.")
    except Exception as exc:
        console.print(f"[red]Failed to send guidance: {exc}[/red]")
        raise typer.Exit(1)


@app.command(name="tickets")
def tickets_cmd(
    session_id: str = typer.Argument("", help="Session ID for server-routed tickets (empty = show local tickets)"),
) -> None:
    """Show tickets for a session or the current local run."""
    project_root = _get_project_root()

    if session_id:
        client = _require_server(project_root)
        if client is None:
            return
        async def _server_tickets() -> None:
            try:
                ticket_list = await client.get_session_tickets(session_id)
                if not ticket_list:
                    console.print("[dim]No tickets for this session.[/dim]")
                    return
                table = Table(title=f"Tickets: {session_id}", show_header=True)
                table.add_column("ID", style="cyan")
                table.add_column("Status")
                table.add_column("Title")
                for t in ticket_list:
                    table.add_row(str(t.get("id", "")), str(t.get("status", "")), str(t.get("title", "")))
                console.print(table)
            except Exception as exc:
                console.print(f"[red]Failed to get tickets: {exc}[/red]")
        asyncio.run(_server_tickets())
        return

    # Fallback: local ticket store (same as status)
    status(session_id="")


@app.command()
def cost(
    session_id: str = typer.Argument("", help="Session ID for server-routed cost (empty = show local cost)"),
) -> None:
    """Show run cost. Pass a session ID to query the server, or leave empty for local cost."""
    project_root = _get_project_root()

    if session_id:
        client = _require_server(project_root)
        if client is None:
            return
        async def _server_cost() -> None:
            try:
                data = await client.get_session_cost(session_id)
                total = data.get("total_cost_usd", 0.0)
                console.print(f"Session {session_id} cost: ${total:.6f}")
            except Exception as exc:
                console.print(f"[red]Failed to get cost: {exc}[/red]")
        asyncio.run(_server_cost())
        return

    golem_dir = _get_golem_dir(project_root)
    cost_events = _parse_cost_events(golem_dir)
    if not cost_events:
        console.print("[dim]No cost data found.[/dim]")
        return
    total = 0.0
    for event in cost_events:
        try:
            total += float(event.get("cost", "$0").lstrip("$"))
        except ValueError:
            pass
    console.print(f"Total run cost: ${total:.4f}")


@app.command()
def pr(
    title: str = typer.Option("", "--title", "-t", help="PR title (auto-generated if empty)"),
    draft: bool = typer.Option(False, "--draft", help="Create as draft PR"),
    base: str = typer.Option("main", "--base", "-b", help="Base branch for the PR"),
) -> None:
    """Create a GitHub PR from the current branch's changes.

    Example: golem pr --title "feat: implement auth" --draft
    """
    project_root = _get_project_root()

    # Get current branch
    result = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=project_root, capture_output=True, text=True, encoding="utf-8",
    )
    branch = result.stdout.strip()
    if not branch or branch in ("main", "master"):
        console.print("[red]Cannot create PR from main/master. Switch to a feature branch first.[/red]")
        raise typer.Exit(1)

    # Auto-generate title from branch name if not provided
    if not title:
        title = f"golem: {branch.replace('golem/', '').replace('/', ' ')}"

    # Build body from ticket summaries if available
    golem_dir = _get_golem_dir(project_root)
    body_parts: list[str] = [f"## Golem Run\n\nBranch: `{branch}`\n"]
    tickets_dir = golem_dir / "tickets"
    if tickets_dir.exists():

        async def _read_tickets() -> list[str]:
            store = TicketStore(tickets_dir)
            tickets = await store.list_tickets()
            return [f"- **{t.id}** {t.title} ({t.status})" for t in sorted(tickets, key=lambda x: x.id)]

        lines = asyncio.run(_read_tickets())
        if lines:
            body_parts.append("## Tickets\n\n" + "\n".join(lines))

    body = "\n\n".join(body_parts)

    cmd = ["gh", "pr", "create", "--title", title, "--body", body, "--base", base, "--head", branch]
    if draft:
        cmd.append("--draft")

    result = subprocess.run(cmd, cwd=project_root, capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        console.print(f"[red]gh pr create failed: {result.stderr.strip()}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]PR created: {result.stdout.strip()}[/green]")


@app.command()
def doctor() -> None:
    """Diagnose environment issues — check all required tools are installed."""
    checks: list[tuple[str, bool, str]] = []

    # Check git
    try:
        result = subprocess.run(["git", "--version"], capture_output=True, text=True)
        checks.append(("git", result.returncode == 0, result.stdout.strip() if result.returncode == 0 else "not found"))
    except FileNotFoundError:
        checks.append(("git", False, "not found"))

    # Check uv
    try:
        result = subprocess.run(["uv", "--version"], capture_output=True, text=True)
        checks.append(("uv", result.returncode == 0, result.stdout.strip() if result.returncode == 0 else "not found"))
    except FileNotFoundError:
        checks.append(("uv", False, "not found"))

    # Check claude CLI
    try:
        result = subprocess.run(["claude", "--version"], capture_output=True, text=True)
        checks.append(("claude", result.returncode == 0, result.stdout.strip() if result.returncode == 0 else "not found"))
    except FileNotFoundError:
        checks.append(("claude", False, "not found"))

    # Check ripgrep
    try:
        result = subprocess.run(["rg", "--version"], capture_output=True, text=True)
        version_line = result.stdout.splitlines()[0] if result.returncode == 0 and result.stdout else "not found"
        checks.append(("rg (ripgrep)", result.returncode == 0, version_line))
    except FileNotFoundError:
        checks.append(("rg (ripgrep)", False, "not found"))

    # Check gh CLI
    try:
        result = subprocess.run(["gh", "--version"], capture_output=True, text=True)
        version_line = result.stdout.splitlines()[0] if result.returncode == 0 and result.stdout else "not found (optional)"
        checks.append(("gh (GitHub CLI)", result.returncode == 0, version_line))
    except FileNotFoundError:
        checks.append(("gh (GitHub CLI)", False, "not found (optional)"))

    all_pass = True
    for name, ok, detail in checks:
        icon = "[green]PASS[/green]" if ok else "[red]FAIL[/red]"
        console.print(f"  {icon}  {name}: {detail}")
        if not ok and name not in ("gh (GitHub CLI)",):
            all_pass = False

    if all_pass:
        console.print("\n[green]All required tools found.[/green]")
    else:
        console.print("\n[yellow]Some required tools are missing. Install them before running golem.[/yellow]")


@app.command(name="reset-ticket")
def reset_ticket(
    ticket_id: str = typer.Argument(..., help="Ticket ID to reset (e.g. TICKET-001)"),
) -> None:
    """Reset a single ticket's status back to pending."""
    project_root = _get_project_root()
    golem_dir = _get_golem_dir(project_root)
    tickets_dir = golem_dir / "tickets"

    if not tickets_dir.exists():
        console.print("[dim]No active run. Use 'golem run <spec>' to start one.[/dim]")
        return

    async def _reset_async() -> None:
        store = TicketStore(tickets_dir)
        try:
            ticket = await store.read(ticket_id)
        except (FileNotFoundError, KeyError):
            console.print(f"[red]Ticket {ticket_id} not found.[/red]")
            raise typer.Exit(1)

        old_status = ticket.status
        await store.update(ticket_id, "pending", f"Reset from {old_status} to pending", agent="cli")
        console.print(f"[green]Reset {ticket_id} from {old_status} to pending.[/green]")

    asyncio.run(_reset_async())


@app.command()
def retry(
    ticket_id: str = typer.Argument(..., help="Ticket ID to retry (e.g. TICKET-001)"),
) -> None:
    """Re-run a specific failed ticket — reset to pending and dispatch to a fresh writer."""
    from golem.worktree import create_worktree, delete_worktree

    project_root = _get_project_root()
    golem_dir = _get_golem_dir(project_root)
    tickets_dir = golem_dir / "tickets"

    if not tickets_dir.exists():
        console.print("[dim]No active run. Use 'golem run <spec>' to start one.[/dim]")
        return

    config = load_config(project_root)
    progress = ProgressLogger(golem_dir)

    async def _retry_async() -> None:
        store = TicketStore(tickets_dir)
        try:
            ticket = await store.read(ticket_id)
        except (FileNotFoundError, KeyError):
            console.print(f"[red]Ticket {ticket_id} not found.[/red]")
            raise typer.Exit(1)

        # 1. Reset ticket to pending
        old_status = ticket.status
        await store.update(ticket_id, "pending", f"Retry requested (was {old_status})", agent="cli")
        ticket = await store.read(ticket_id)  # Refresh after update
        console.print(f"[dim]Reset {ticket_id} from {old_status} to pending.[/dim]")

        # 2. Create worktree
        branch_prefix = config.branch_prefix
        session_prefix = f"{branch_prefix}/{config.session_id}" if config.session_id else branch_prefix
        branch = f"{session_prefix}/{ticket_id.lower()}-retry"
        wt_path = golem_dir / "worktrees" / f"{ticket_id.lower()}-retry"

        # Clean up existing retry worktree if it exists
        if wt_path.exists():
            try:
                delete_worktree(wt_path, project_root)
            except Exception:
                pass

        try:
            create_worktree(
                group_id=ticket_id,
                branch=branch,
                base_branch=config.pr_target,
                path=wt_path,
                repo_root=project_root,
                branch_prefix=branch_prefix,
            )
            console.print(f"[dim]Created worktree at {wt_path}[/dim]")
        except Exception as e:
            console.print(f"[red]Failed to create worktree: {e}[/red]")
            raise typer.Exit(1)

        # 3. Dispatch to writer
        console.print(f"[bold]Retrying {ticket_id}...[/bold]")
        await store.update(ticket_id, "in_progress", "Dispatched to writer (retry)", agent="cli")

        try:
            result = await spawn_junior_dev(
                ticket=ticket,
                worktree_path=str(wt_path),
                config=config,
                golem_dir=golem_dir,
            )
            await store.update(
                ticket_id, "complete",
                f"Writer completed (retry): {result.num_turns} turns, ${result.cost_usd:.4f}",
                agent="cli",
            )
            console.print(f"[green]✓ {ticket_id} completed[/green]")
            console.print(f"  Turns: {result.num_turns} | Cost: ${result.cost_usd:.4f}")
        except RuntimeError as e:
            await store.update(ticket_id, "failed", f"Writer failed (retry): {e}", agent="cli")
            console.print(f"[red]✗ {ticket_id} failed: {e}[/red]")
            raise typer.Exit(1)
        finally:
            # Clean up worktree
            try:
                delete_worktree(wt_path, project_root)
            except Exception:
                pass

    asyncio.run(_retry_async())


@app.command(name="list-specs")
def list_specs() -> None:
    """List all .md files in the project that look like specs."""
    project_root = _get_project_root()
    skip = {".git", ".golem", ".venv", "node_modules", "__pycache__", ".claude"}
    specs: list[Path] = []
    for p in sorted(project_root.rglob("*.md")):
        parts = p.relative_to(project_root).parts
        if any(part.startswith(".") or part in skip for part in parts):
            continue
        specs.append(p)

    if not specs:
        console.print("[dim]No .md files found in project.[/dim]")
        return

    for spec in specs:
        rel = spec.relative_to(project_root)
        console.print(f"  {rel}")
    console.print(f"\n[dim]{len(specs)} spec(s) found.[/dim]")


@app.command()
def review(
    pr_number: int = typer.Argument(..., help="GitHub PR number to review"),
    repo: str = typer.Option("", "--repo", "-r", help="Repository in OWNER/REPO format (default: current repo)"),
    post: bool = typer.Option(False, "--post", help="Post findings as PR comments (default: print only)"),
    passes: str = typer.Option(
        "quick_scan,security,quality,deep,structural,triage",
        "--passes",
        help="Comma-separated list of passes to run: quick_scan,security,quality,deep,structural,triage",
    ),
) -> None:
    """Run multi-pass AI review against a GitHub PR.

    Example: golem review 42
    Example: golem review 42 --repo owner/repo --post
    Example: golem review 42 --passes quick_scan,security,quality
    """
    from golem.pr_review import _post_review_comments, run_review

    project_root = _get_project_root()
    golem_dir = _get_golem_dir(project_root)
    config = load_config(golem_dir)

    # Resolve repo
    if not repo:
        result = subprocess.run(
            ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
            cwd=project_root, capture_output=True, text=True, encoding="utf-8",
        )
        if result.returncode != 0:
            console.print("[red]Could not determine current repo. Pass --repo OWNER/REPO explicitly.[/red]")
            raise typer.Exit(1)
        repo = result.stdout.strip()
        if not repo:
            console.print("[red]Could not determine current repo. Pass --repo OWNER/REPO explicitly.[/red]")
            raise typer.Exit(1)

    # Parse passes
    pass_list = [p.strip() for p in passes.split(",") if p.strip()]
    valid_passes = {"quick_scan", "security", "quality", "deep", "structural", "triage"}
    for p in pass_list:
        if p not in valid_passes:
            console.print(f"[red]Unknown pass: {p!r}. Valid: {', '.join(sorted(valid_passes))}[/red]")
            raise typer.Exit(1)

    console.print(f"[bold cyan]Golem Review[/bold cyan] PR #{pr_number} in {repo}")
    console.print(f"  Passes: {', '.join(pass_list)}")
    console.print(f"  Model:  {config.validator_model}")

    _sev_rank: dict[str, int] = {"critical": 2, "warning": 1, "info": 0}
    _severity_styles: dict[str, str] = {
        "critical": "[red]CRITICAL[/red]",
        "warning": "[yellow]WARNING[/yellow]",
        "info": "[dim]INFO[/dim]",
    }

    async def _review_async() -> None:
        try:
            report = await run_review(pr_number, repo, config, passes=pass_list)
        except RuntimeError as exc:
            console.print(f"[red]Review failed: {exc}[/red]")
            raise typer.Exit(1)

        # Print summary
        console.print(f"\n[bold]Review Complete[/bold] ({report.duration_s:.1f}s, ${report.cost_usd:.4f})")
        console.print(f"  Complexity: {report.complexity}")
        console.print(f"  Passes run: {', '.join(report.passes_run)}")
        console.print(f"  {report.summary}")

        if not report.findings:
            console.print("\n[green]No findings.[/green]")
            return

        table = Table(title=f"PR #{pr_number} Review Findings", show_header=True)
        table.add_column("Severity", width=10)
        table.add_column("Category", width=12)
        table.add_column("File:Line", width=30)
        table.add_column("Title")
        table.add_column("Pass", width=12)

        sorted_findings = sorted(
            report.findings,
            key=lambda f: (
                -_sev_rank.get(f.severity, 0),
                f.category,
                f.file,
                f.line,
            ),
        )

        for finding in sorted_findings:
            styled_sev = _severity_styles.get(finding.severity, finding.severity)
            file_line = f"{finding.file}:{finding.line}" if finding.line > 0 else finding.file
            if len(file_line) > 28:
                file_line = "..." + file_line[-25:]
            table.add_row(
                styled_sev,
                finding.category,
                file_line,
                finding.title[:70],
                finding.pass_name,
            )

        console.print(table)

        # Verbose body output for critical findings
        critical_findings = [f for f in sorted_findings if f.severity == "critical"]
        if critical_findings:
            console.print("\n[bold red]Critical Finding Details:[/bold red]")
            for finding in critical_findings:
                console.print(f"\n  [bold]{finding.title}[/bold]")
                console.print(f"  File: {finding.file}:{finding.line}")
                console.print(f"  {finding.body[:400]}")

        if post:
            posted = _post_review_comments(report.findings, pr_number, repo)
            console.print(f"\n[green]Posted {posted} comment(s) to PR #{pr_number}.[/green]")

    asyncio.run(_review_async())


# --------------------------------------------------------------------------
# config subcommand group
# --------------------------------------------------------------------------

config_app = typer.Typer(name="config", help="View and manage Golem configuration.")
app.add_typer(config_app)


@config_app.command("show")
def config_show() -> None:
    """Print the current Golem config as pretty JSON."""
    from dataclasses import asdict

    project_root = _get_project_root()
    golem_dir = _get_golem_dir(project_root)
    config = load_config(golem_dir)
    console.print_json(json.dumps(asdict(config), indent=2, sort_keys=True))


@config_app.command("reset")
def config_reset(
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation"),
) -> None:
    """Reset config to defaults (delete .golem/config.json)."""
    project_root = _get_project_root()
    golem_dir = _get_golem_dir(project_root)
    config_path = golem_dir / "config.json"
    if not config_path.exists():
        console.print("[yellow]No config.json found -- already at defaults.[/yellow]")
        return
    if not force:
        typer.confirm("This will delete .golem/config.json and reset to defaults. Continue?", abort=True)
    config_path.unlink()
    console.print("[green]Config reset to defaults.[/green]")


@config_app.command("set")
def config_set(
    key: str = typer.Argument(
        ..., help="Config key (e.g. max_parallel, extra_mcp_servers.planner.context7)",
    ),
    value: str = typer.Argument(..., help="New value (JSON-parsed for nested keys)"),
) -> None:
    """Set a config value. Supports dot-notation for nested fields."""
    import json as _json
    from dataclasses import fields as dataclass_fields

    from golem.config import GolemConfig

    project_root = _get_project_root()
    golem_dir = _get_golem_dir(project_root)
    config = load_config(golem_dir)

    parts = key.split(".")
    top_key = parts[0]

    # Validate top-level key exists on GolemConfig
    valid_keys = {f.name for f in dataclass_fields(GolemConfig)}
    if top_key not in valid_keys:
        console.print(f"[red]Unknown config key: {top_key}[/red]")
        console.print(f"  Valid keys: {', '.join(sorted(valid_keys))}")
        raise typer.Exit(1)

    if len(parts) == 1:
        # Simple top-level key — existing behavior with JSON parsing attempt
        field_type = type(getattr(config, key))
        try:
            # Try JSON parse first for complex types
            typed_value = _json.loads(value)
        except (_json.JSONDecodeError, ValueError):
            # Fall back to type coercion for simple types
            try:
                if field_type is int:
                    typed_value = int(value)
                elif field_type is bool:
                    typed_value = value.lower() in ("true", "1", "yes")
                else:
                    typed_value = value
            except ValueError:
                console.print(
                    f"[red]Invalid value for {key}: expected"
                    f" {field_type.__name__}, got {value!r}[/red]"
                )
                raise typer.Exit(1)

        if typed_value is None:
            # Reset to default
            default_config = GolemConfig()
            typed_value = getattr(default_config, key)

        setattr(config, key, typed_value)
    else:
        # Dot-notation: traverse/create nested dicts
        try:
            parsed_value = _json.loads(value)
        except (_json.JSONDecodeError, ValueError):
            parsed_value = value

        obj = getattr(config, top_key)
        if not isinstance(obj, dict):
            console.print(
                f"[red]{top_key} is not a dict -- dot-notation requires a dict field[/red]"
            )
            raise typer.Exit(1)

        # Traverse/create intermediate dicts
        current = obj
        for part in parts[1:-1]:
            if part not in current or not isinstance(current[part], dict):
                current[part] = {}
            current = current[part]

        # Set or delete the final key
        final_key = parts[-1]
        if parsed_value is None:
            current.pop(final_key, None)
        else:
            current[final_key] = parsed_value

        setattr(config, top_key, obj)

    save_config(config, golem_dir)
    display = getattr(config, top_key) if len(parts) == 1 else value
    console.print(f"[green]Set {key} = {display}[/green]")


def _run_preflight_checks(
    config: GolemConfig,
    project_root: Path,
    spec: Path,
) -> tuple[list[str], list[str], list[str]]:
    """Returns (errors, warnings, infos). Delegates to shared run_preflight_checks."""
    from golem.config import run_preflight_checks

    return run_preflight_checks(config, project_root)


@app.command()
def preflight(
    spec: Path = typer.Argument(..., help="Path to spec markdown file"),
    force: bool = typer.Option(False, "--force", help="Proceed despite errors"),
) -> None:
    """Pre-flight check: resolve effective tool ecosystem and detect pitfalls."""
    from golem.config import GolemConfig

    _validate_spec(spec)
    project_root = _resolve_spec_project_root(spec)
    golem_dir = _get_golem_dir(project_root)
    config = (
        load_config(golem_dir)
        if (golem_dir / "config.json").exists()
        else GolemConfig()
    )

    console.print(f"\n[bold]Golem Pre-Flight[/bold] -- {spec.name}")
    console.print(f"Project: {project_root}\n")

    # Setting sources
    console.print("[bold]Setting Sources[/bold]")
    console.print(f"  Base: {config.setting_sources}")
    for role, sources in config.agent_setting_sources.items():
        role_label = role.replace("_", " ").title()
        console.print(f"  {role_label} override: {sources}")

    # Per-role summary
    golem_tools = {
        "planner": [
            "create_ticket", "update_ticket", "read_ticket", "list_tickets",
        ],
        "tech_lead": [
            "create_ticket", "update_ticket", "read_ticket", "list_tickets",
            "run_qa", "create_worktree", "merge_branches", "commit_worktree",
        ],
        "writer": ["run_qa", "update_ticket"],
    }

    for role in ("planner", "tech_lead", "writer"):
        role_label = role.replace("_", " ").title()
        sources = config.agent_setting_sources.get(role, config.setting_sources)
        extras = config.extra_mcp_servers.get(role, {})

        console.print(f"\n[bold]{role_label}[/bold]")
        console.print(f"  Setting sources: {sources}")
        console.print(f"  Golem MCP: {', '.join(golem_tools[role])}")

        if extras:
            for name, srv in extras.items():
                srv_type = (
                    "stdio"
                    if isinstance(srv, dict) and "command" in srv
                    else "sse/http"
                )
                cmd_or_url = (
                    srv.get("command", srv.get("url", "?"))
                    if isinstance(srv, dict)
                    else "?"
                )
                console.print(
                    f"  Extra MCP: {name} ({srv_type}: {cmd_or_url})"
                )
        else:
            console.print("  Extra MCPs: (none)")

        # Detect plugins per role
        from golem.config import resolve_plugins_for_role

        proj_plugins, usr_plugins = resolve_plugins_for_role(config, role, project_root)
        console.print(f"  Project plugins: {', '.join(proj_plugins) if proj_plugins else '(none)'}")
        if "user" in sources:
            console.print(f"  User plugins: {', '.join(usr_plugins) if usr_plugins else '(none)'}")

    # Pitfall detection
    errors, warnings_list, infos = _run_preflight_checks(
        config, project_root, spec,
    )

    console.print("\n[bold]Pitfalls[/bold]")
    for e in errors:
        console.print(f"  [red][ERROR][/red] {e}")
    for w in warnings_list:
        console.print(f"  [yellow][WARN][/yellow] {w}")
    for i in infos:
        console.print(f"  [blue][INFO][/blue] {i}")
    if not errors and not warnings_list and not infos:
        console.print("  (none detected)")

    total_errors = len(errors)
    console.print(
        f"\nResult: {total_errors} error{'s' if total_errors != 1 else ''}"
        f" -- {'ready to run' if total_errors == 0 else 'blocked'}"
    )

    if total_errors > 0 and not force:
        raise typer.Exit(1)


# --------------------------------------------------------------------------
# Server-facing helpers
# --------------------------------------------------------------------------


def _get_server_base_url() -> str:
    """Return the running server's base URL from server.json, or exit if not running."""
    import urllib.error

    project_root = _get_project_root()
    golem_dir = _get_golem_dir(project_root)
    server_json = golem_dir / "server.json"
    if not server_json.exists():
        console.print("[red]No server running. Start with 'golem server start'.[/red]")
        raise typer.Exit(1)
    info = json.loads(server_json.read_text(encoding="utf-8"))
    return f"http://{info['host']}:{info['port']}"


def _server_request(
    method: str,
    path: str,
    *,
    body: dict[str, str] | None = None,
) -> dict[str, object]:
    """Make an HTTP request to the running Golem server. Returns parsed JSON."""
    import urllib.error
    import urllib.request

    base_url = _get_server_base_url()
    url = f"{base_url}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))  # type: ignore[return-value]
    except urllib.error.HTTPError as exc:
        console.print(f"[red]Server error {exc.code}: {exc.reason}[/red]")
        raise typer.Exit(1)
    except urllib.error.URLError as exc:
        console.print(f"[red]Could not connect to server: {exc.reason}[/red]")
        raise typer.Exit(1)


# --------------------------------------------------------------------------
# Merge / approve / merge-queue / conflicts commands
# --------------------------------------------------------------------------


@app.command()
def merge(
    session_id: str = typer.Argument(..., help="Session ID to enqueue for merge"),
) -> None:
    """Enqueue a session for merge (POST /api/merge-queue/{id})."""
    result = _server_request("POST", f"/api/merge-queue/{session_id}")
    console.print(result)


@app.command()
def approve(
    session_id: str = typer.Argument(..., help="Session ID to approve and merge"),
) -> None:
    """Approve and merge a session's PR (POST /api/merge-queue/{id}/approve)."""
    result = _server_request("POST", f"/api/merge-queue/{session_id}/approve")
    console.print(result)


@app.command(name="merge-queue")
def merge_queue() -> None:
    """Show the current merge queue (GET /api/merge-queue)."""
    result = _server_request("GET", "/api/merge-queue")
    entries = result if isinstance(result, list) else []
    if not entries:
        console.print("[dim]Merge queue is empty.[/dim]")
        return
    table = Table(title="Merge Queue", show_header=True)
    table.add_column("Session ID")
    table.add_column("Status")
    table.add_column("PR #")
    table.add_column("Enqueued At")
    for entry in entries:
        if isinstance(entry, dict):
            table.add_row(
                str(entry.get("session_id", "")),
                str(entry.get("status", "")),
                str(entry.get("pr_number") or ""),
                str(entry.get("enqueued_at", ""))[:19],
            )
    console.print(table)


@app.command()
def conflicts() -> None:
    """Show detected file-level conflicts across sessions (GET /api/conflicts)."""
    result = _server_request("GET", "/api/conflicts")
    items = result if isinstance(result, list) else []
    if not items:
        console.print("[dim]No conflicts detected.[/dim]")
        return
    for item in items:
        if isinstance(item, dict):
            ticket_info = ""
            if item.get("ticket_a") or item.get("ticket_b"):
                ticket_info = f" (tickets: {item.get('ticket_a', '-')} vs {item.get('ticket_b', '-')})"
            console.print(
                f"{item.get('file_path', '')}: {item.get('session_a', '')} vs {item.get('session_b', '')}{ticket_info}"
            )


@app.command()
def changelog(
    since: str = typer.Option(
        "", "--since", help="Git ref (tag or SHA) to generate changelog from. Defaults to latest tag or first commit."
    ),
    version: str = typer.Option("Unreleased", "--version", help="Version label for the changelog entry header."),
    output: str = typer.Option("", "--output", "-o", help="Write output to this file instead of stdout."),
) -> None:
    """Generate a Keep-a-Changelog formatted entry from git history.

    Uses Claude (Haiku) to categorize commits into Added/Changed/Fixed/Removed.

    Example: golem changelog --since v1.0.0 --version 1.1.0
    Example: golem changelog --output CHANGELOG.md
    """
    from golem.changelog import format_changelog, generate_changelog

    project_root = _get_project_root()
    config = load_config(_get_golem_dir(project_root))

    changelog_path = project_root / "CHANGELOG.md"
    previous_changelog = ""
    if changelog_path.exists():
        previous_changelog = changelog_path.read_text(encoding="utf-8")

    async def _run() -> str:
        entry = await generate_changelog(
            since=since,
            version=version,
            config=config,
            previous_changelog=previous_changelog,
            cwd=str(project_root),
        )
        return format_changelog(entry)

    try:
        formatted = asyncio.run(_run())
    except Exception as exc:
        console.print(f"[red]Changelog generation failed: {exc}[/red]")
        raise typer.Exit(1)

    if output:
        out_path = Path(output)
        out_path.write_text(formatted + "\n", encoding="utf-8")
        console.print(f"Changelog written to {out_path}")
    else:
        console.print(formatted)


@app.command(name="commit-msg")
def commit_msg(
    apply: bool = typer.Option(False, "--apply", help="Create the commit with the generated message."),
) -> None:
    """Generate a conventional commit message from staged changes.

    Reads staged diff via git diff --cached and sends to Claude (Haiku).

    Example: golem commit-msg
    Example: golem commit-msg --apply
    """
    from golem.changelog import format_commit_message, generate_commit_message

    project_root = _get_project_root()
    config = load_config(_get_golem_dir(project_root))

    diff_result = subprocess.run(
        ["git", "diff", "--cached"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(project_root),
    )
    if diff_result.returncode != 0:
        console.print(f"[red]git diff --cached failed: {diff_result.stderr.strip()}[/red]")
        raise typer.Exit(1)

    staged_diff = diff_result.stdout

    async def _run() -> str:
        msg = await generate_commit_message(diff=staged_diff, config=config)
        return format_commit_message(msg)

    try:
        commit_text = asyncio.run(_run())
    except Exception as exc:
        console.print(f"[red]Commit message generation failed: {exc}[/red]")
        raise typer.Exit(1)

    console.print(commit_text)

    if apply:
        commit_result = subprocess.run(
            ["git", "commit", "-m", commit_text],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=str(project_root),
        )
        if commit_result.returncode != 0:
            console.print(f"[red]git commit failed: {commit_result.stderr.strip()}[/red]")
            raise typer.Exit(1)
        console.print("[green]Committed.[/green]")


@app.command()
def ideate(
    category: str = typer.Option(
        "code_improvements",
        "--type",
        "-t",
        help=(
            "Analysis category: code_improvements, ui_ux_improvements, documentation_gaps, "
            "security_hardening, performance_optimizations, code_quality"
        ),
    ),
    all_categories: bool = typer.Option(False, "--all", help="Run all 6 analysis categories"),
    max_ideas: int = typer.Option(10, "--max-ideas", "-n", help="Maximum ideas per category"),
) -> None:
    """Run AI-powered codebase ideation to surface improvement suggestions.

    Analyzes the codebase with a category-specific prompt and returns
    structured, prioritized improvement ideas.

    Example: golem ideate
    Example: golem ideate --type security_hardening
    Example: golem ideate --all --max-ideas 5
    """
    from golem.ideation import ALL_CATEGORIES, IdeaCategory, run_all_ideation, run_ideation

    project_root = _get_project_root()
    golem_dir = _get_golem_dir(project_root)
    config = load_config(golem_dir)

    valid_categories: set[str] = set(ALL_CATEGORIES)

    _priority_styles: dict[str, str] = {
        "high": "[red]high[/red]",
        "medium": "[yellow]medium[/yellow]",
        "low": "[dim]low[/dim]",
    }

    if not all_categories:
        if category not in valid_categories:
            console.print(
                f"[red]Unknown category: {category!r}. "
                f"Valid: {', '.join(sorted(valid_categories))}[/red]"
            )
            raise typer.Exit(1)

    async def _run() -> None:
        if all_categories:
            console.print(f"[bold cyan]Golem Ideate[/bold cyan] -- all 6 categories (max {max_ideas} ideas each)")
            results = await run_all_ideation(project_root, config, max_ideas=max_ideas)
        else:
            typed_category: IdeaCategory = category  # type: ignore[assignment]
            console.print(
                f"[bold cyan]Golem Ideate[/bold cyan] -- {typed_category} "
                f"(max {max_ideas} ideas)"
            )
            results = [await run_ideation(typed_category, project_root, config, max_ideas=max_ideas)]

        total_ideas = 0
        for result in results:
            if not result.ideas:
                console.print(f"\n[dim]{result.category}: no ideas generated.[/dim]")
                continue

            table = Table(
                title=f"{result.category.replace('_', ' ').title()} ({len(result.ideas)} ideas, {result.duration_s:.1f}s)",
                show_header=True,
            )
            table.add_column("Priority", width=8)
            table.add_column("Effort", width=7)
            table.add_column("File", width=28, no_wrap=True)
            table.add_column("Title")

            for idea in result.ideas:
                styled_priority = _priority_styles.get(idea.priority, idea.priority)
                file_display = idea.file
                if len(file_display) > 26:
                    file_display = "..." + file_display[-23:]
                table.add_row(styled_priority, idea.effort, file_display, idea.title[:80])

            console.print(table)
            if result.summary:
                console.print(f"  [dim]{result.summary}[/dim]")
            total_ideas += len(result.ideas)

        # Summary stats
        console.print(f"\n[bold]Total ideas:[/bold] {total_ideas}")
        if all_categories:
            total_duration = sum(r.duration_s for r in results)
            console.print(f"[dim]Analysis completed in {total_duration:.1f}s[/dim]")

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        console.print("\n[yellow]Ideation interrupted.[/yellow]")
    except Exception as exc:
        console.print(f"[red]Ideation failed: {exc}[/red]")
        raise typer.Exit(1)
