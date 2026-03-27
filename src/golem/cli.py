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

from golem.config import load_config, save_config

if TYPE_CHECKING:
    from golem.config import GolemConfig
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
    """Walk up from the spec file to find the git root of the target project.

    Falls back to the spec file's parent directory if no .git directory is found.
    """
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
        console.print("[yellow]Warning: spec has no headings or task markers — Lead Architect may struggle to extract tasks.[/yellow]")


@app.command()
def run(
    spec: Path = typer.Argument(..., help="Path to spec markdown file"),
    force: bool = typer.Option(False, "--force", help="Skip confirmation prompts (for CI/non-interactive)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Run planner only, skip Tech Lead execution"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose debug output"),
) -> None:
    """Full autonomous run: plan, orchestrate writers, validate, create PR.

    Example: golem run spec.md
    Example: golem run spec.md --force --dry-run
    """
    from golem import __version__

    if verbose:
        import os
        os.environ["GOLEM_DEBUG"] = "1"

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
    console.print(f"  Models:  planner={config.planner_model}, tech_lead={config.tech_lead_model}, worker={config.worker_model}")
    console.print(f"  Limits:  parallel={config.max_parallel}, retries={config.max_retries}, worker_turns={config.max_worker_turns}")
    if config.infrastructure_checks:
        console.print(f"  Infra:   {', '.join(config.infrastructure_checks)}")

    async def _run_async() -> None:
        console.print("[bold cyan]Golem[/bold cyan] -- Planning...")
        progress.log_planner_start()
        t_plan = time.monotonic()
        planner_result = await run_planner(spec, golem_dir, config, project_root)
        ticket_id = planner_result.ticket_id
        plan_elapsed = time.monotonic() - t_plan
        plan_m, plan_s = divmod(int(plan_elapsed), 60)
        progress.log_planner_complete(ticket_id)
        console.print(f"  Lead Architect completed in {plan_m}m {plan_s}s — ticket: {ticket_id}")

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
            return

        console.print("[bold cyan]Golem[/bold cyan] -- Tech Lead executing...")
        progress.log_tech_lead_start(ticket_id)
        tech_lead_result = await run_tech_lead(ticket_id, golem_dir, config, project_root)
        elapsed = time.monotonic() - t0
        mins, secs = divmod(int(elapsed), 60)
        progress.log_tech_lead_complete(elapsed_s=elapsed)
        total_cost = (planner_result.cost_usd or 0.0) + (tech_lead_result.cost_usd or 0.0)
        progress.log_run_cost_summary(total_cost)
        if total_cost > 0:
            console.print(f"[dim]Run cost: ${total_cost:.4f}[/dim]")
        console.print(f"[bold]Run complete in {mins}m {secs}s.[/bold]")

        # Final summary
        all_tickets = await store.list_tickets()
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
        done_count = sum(1 for t in tickets if t.status in ("done", "approved", "qa_passed", "ready_for_review"))
        in_prog = sum(1 for t in tickets if t.status == "in_progress")
        console.print(table)
        console.print(f"  {done_count}/{total} complete, {in_prog} in progress")

    asyncio.run(_status_async())


# Alias: golem tickets = golem status
app.command(name="tickets", hidden=True)(status)


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
    """Remove .golem/ state, worktrees, and golem/* branches.

    Example: golem clean --force
    """
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
    base: str = typer.Option("main", "--base", "-b", help="Base branch to diff against"),
) -> None:
    """Show git diff of changes from the last golem run."""
    project_root = _get_project_root()
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
    result = subprocess.run(["git", "--version"], capture_output=True, text=True)
    checks.append(("git", result.returncode == 0, result.stdout.strip() if result.returncode == 0 else "not found"))

    # Check uv
    result = subprocess.run(["uv", "--version"], capture_output=True, text=True)
    checks.append(("uv", result.returncode == 0, result.stdout.strip() if result.returncode == 0 else "not found"))

    # Check claude CLI
    result = subprocess.run(["claude", "--version"], capture_output=True, text=True)
    checks.append(("claude", result.returncode == 0, result.stdout.strip() if result.returncode == 0 else "not found"))

    # Check ripgrep
    result = subprocess.run(["rg", "--version"], capture_output=True, text=True)
    version_line = result.stdout.splitlines()[0] if result.returncode == 0 and result.stdout else "not found"
    checks.append(("rg (ripgrep)", result.returncode == 0, version_line))

    # Check gh CLI
    result = subprocess.run(["gh", "--version"], capture_output=True, text=True)
    version_line = result.stdout.splitlines()[0] if result.returncode == 0 and result.stdout else "not found (optional)"
    checks.append(("gh (GitHub CLI)", result.returncode == 0, version_line))

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
