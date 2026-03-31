from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from golem.config import load_config, save_config
from golem.planner import run_planner
from golem.progress import ProgressLogger
from golem.tech_lead import run_tech_lead
from golem.tickets import TicketStore

app = typer.Typer(
    name="golem",
    help="Autonomous spec executor with ticket-driven agent hierarchy.",
    invoke_without_command=True,
    no_args_is_help=True,
)
console = Console()

_GOLEM_DIR_NAME = ".golem"


def _resolve_spec_project_root(spec: Path) -> Path:
    """Walk up from the spec file to find the git root of the target project."""
    candidate = spec.resolve().parent
    while candidate != candidate.parent:
        if (candidate / ".git").exists():
            return candidate
        candidate = candidate.parent
    return spec.resolve().parent


def _detect_infrastructure_checks(project_root: Path) -> list[str]:
    """Auto-detect always-on infrastructure checks from project tooling files."""
    checks: list[str] = []

    # Python: ruff if pyproject.toml has [tool.ruff]
    pyproject = project_root / "pyproject.toml"
    if pyproject.exists():
        content = pyproject.read_text(encoding="utf-8")
        if "[tool.ruff]" in content or "[tool.ruff.lint]" in content:
            checks.append("ruff check .")
    ruff_toml = project_root / "ruff.toml"
    if ruff_toml.exists() and "ruff check ." not in checks:
        checks.append("ruff check .")

    # JavaScript/TypeScript: check for lint/typecheck scripts in package.json
    package_json = project_root / "package.json"
    if package_json.exists():
        try:
            pkg = json.loads(package_json.read_text(encoding="utf-8"))
            scripts = pkg.get("scripts", {})
            if "lint" in scripts:
                checks.append("npm run lint")
            if "typecheck" in scripts:
                checks.append("npm run typecheck")
        except (json.JSONDecodeError, KeyError):
            pass

    # TypeScript: tsconfig.json present
    tsconfig = project_root / "tsconfig.json"
    if tsconfig.exists():
        checks.append("tsc --noEmit")

    return checks


def _get_golem_dir(project_root: Path) -> Path:
    return project_root / _GOLEM_DIR_NAME


def _get_project_root() -> Path:
    return Path.cwd()


def _create_golem_dirs(golem_dir: Path) -> None:
    for subdir in ("tickets", "research", "plans", "references", "reports", "worktrees"):
        (golem_dir / subdir).mkdir(parents=True, exist_ok=True)


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
        console.print("[yellow]Warning: spec has no headings or task markers — planner may struggle to extract tasks.[/yellow]")


@app.command()
def run(
    spec: Path = typer.Argument(..., help="Path to spec markdown file"),
    force: bool = typer.Option(False, "--force", help="Skip confirmation prompts (for CI/non-interactive)"),
) -> None:
    """Full autonomous run: plan, orchestrate writers, validate, create PR."""
    from golem import __version__

    console.print(f"[bold cyan]Golem[/bold cyan] v{__version__} (v2 ticket-driven)")
    _validate_spec(spec)
    project_root = _get_project_root()
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
    spec_project_root = _resolve_spec_project_root(spec)
    config.infrastructure_checks = _detect_infrastructure_checks(spec_project_root)
    save_config(config, golem_dir)

    progress = ProgressLogger(golem_dir)
    t0 = time.monotonic()

    console.print(f"  Spec:    {spec.resolve()}")
    console.print(f"  Project: {spec_project_root}")
    if config.infrastructure_checks:
        console.print(f"  Infra:   {', '.join(config.infrastructure_checks)}")

    async def _run_async() -> None:
        console.print("[bold cyan]Golem[/bold cyan] -- Planning...")
        progress.log_planner_start()
        t_plan = time.monotonic()
        ticket_id = await run_planner(spec, golem_dir, config, project_root)
        plan_elapsed = time.monotonic() - t_plan
        plan_m, plan_s = divmod(int(plan_elapsed), 60)
        progress.log_planner_complete(ticket_id)
        console.print(f"  Planner completed in {plan_m}m {plan_s}s — ticket: {ticket_id}")

        # Show ticket summary before handing off
        store = TicketStore(golem_dir / "tickets")
        ticket = await store.read(ticket_id)
        console.print(f"  Title:     {ticket.title[:70]}")
        if ticket.context.plan_file:
            console.print(f"  Plan file: {ticket.context.plan_file}")
        if ticket.context.references:
            console.print(f"  References: {len(ticket.context.references)} file(s)")

        console.print("[bold cyan]Golem[/bold cyan] -- Tech Lead executing...")
        progress.log_tech_lead_start(ticket_id)
        await run_tech_lead(ticket_id, golem_dir, config, project_root)
        elapsed = time.monotonic() - t0
        mins, secs = divmod(int(elapsed), 60)
        progress.log_tech_lead_complete(elapsed_s=elapsed)
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

    try:
        asyncio.run(_run_async())
    except RuntimeError as e:
        console.print(f"\n[red]Error: {e}[/red]")
        raise typer.Exit(1)


@app.command()
def plan(
    spec: Path = typer.Argument(..., help="Path to spec markdown file"),
) -> None:
    """Dry run — generate plans only, no Tech Lead execution."""
    _validate_spec(spec)
    project_root = _get_project_root()
    golem_dir = _get_golem_dir(project_root)
    _create_golem_dirs(golem_dir)

    config = load_config(golem_dir)

    async def _plan_async() -> None:
        console.print("[bold cyan]Golem[/bold cyan] — Planning (dry run)...")
        ticket_id = await run_planner(spec, golem_dir, config, project_root)
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


@app.command()
def status() -> None:
    """Show current run progress from ticket store."""
    project_root = _get_project_root()
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
        done_count = sum(1 for t in tickets if t.status in ("done", "approved"))
        in_prog = sum(1 for t in tickets if t.status == "in_progress")
        console.print(table)
        console.print(f"  {done_count}/{total} complete, {in_prog} in progress")

        console.print(table)

    asyncio.run(_status_async())


@app.command()
def resume() -> None:
    """Resume interrupted run from ticket store."""
    project_root = _get_project_root()
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
        config.infrastructure_checks = _detect_infrastructure_checks(spec_project_root)

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
    port: int = typer.Option(9664, help="Port to serve the dashboard on"),
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
def history() -> None:
    """Show chronological event timeline across all tickets."""
    project_root = _get_project_root()
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
) -> None:
    """Show full details of a single ticket."""
    project_root = _get_project_root()
    golem_dir = _get_golem_dir(project_root)
    tickets_dir = golem_dir / "tickets"

    if not tickets_dir.exists():
        console.print("[dim]No active run. Use 'golem run <spec>' to start one.[/dim]")
        return

    async def _inspect_async() -> None:
        store = TicketStore(tickets_dir)
        try:
            ticket = await store.read(ticket_id)
        except (FileNotFoundError, KeyError):
            console.print(f"[red]Ticket {ticket_id} not found.[/red]")
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
    follow: bool = typer.Option(False, "--follow", "-f", help="Follow mode — tail new lines as they appear"),
    lines: int = typer.Option(20, "--lines", "-n", help="Number of recent lines to show"),
) -> None:
    """Show progress.log entries."""
    import time

    project_root = _get_project_root()
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
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation prompt"),
) -> None:
    """Remove .golem/ state, worktrees, and branches."""
    project_root = _get_project_root()
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
                subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=project_root, capture_output=True)
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
