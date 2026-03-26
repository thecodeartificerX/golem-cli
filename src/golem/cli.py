from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from golem.config import load_config, save_config
from golem.planner import run_planner
from golem.tech_lead import run_tech_lead
from golem.tickets import TicketStore

app = typer.Typer(name="golem", help="Autonomous spec executor with ticket-driven agent hierarchy.")
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


@app.command()
def run(
    spec: Path = typer.Argument(..., help="Path to spec markdown file"),
    force: bool = typer.Option(False, "--force", help="Skip confirmation prompts (for CI/non-interactive)"),
) -> None:
    """Full autonomous run: plan, orchestrate writers, validate, create PR."""
    project_root = _get_project_root()
    golem_dir = _get_golem_dir(project_root)
    _create_golem_dirs(golem_dir)

    config = load_config(golem_dir)
    spec_project_root = _resolve_spec_project_root(spec)
    config.infrastructure_checks = _detect_infrastructure_checks(spec_project_root)
    save_config(config, golem_dir)

    async def _run_async() -> None:
        console.print("[bold cyan]Golem[/bold cyan] — Planning...")
        ticket_id = await run_planner(spec, golem_dir, config, project_root)
        console.print(f"  Planner created ticket: {ticket_id}")

        console.print("[bold cyan]Golem[/bold cyan] — Tech Lead executing...")
        await run_tech_lead(ticket_id, golem_dir, config, project_root)
        console.print("[bold]Run complete.[/bold]")

    asyncio.run(_run_async())


@app.command()
def plan(
    spec: Path = typer.Argument(..., help="Path to spec markdown file"),
) -> None:
    """Dry run — generate plans only, no Tech Lead execution."""
    project_root = _get_project_root()
    golem_dir = _get_golem_dir(project_root)
    _create_golem_dirs(golem_dir)

    config = load_config(golem_dir)

    async def _plan_async() -> None:
        console.print("[bold cyan]Golem[/bold cyan] — Planning (dry run)...")
        ticket_id = await run_planner(spec, golem_dir, config, project_root)
        console.print(f"[bold green]Plan complete.[/bold green] Ticket: {ticket_id}")
        console.print(f"Plans written to: {golem_dir / 'plans'}")

    asyncio.run(_plan_async())


@app.command()
def status() -> None:
    """Show current run progress from ticket store."""
    project_root = _get_project_root()
    golem_dir = _get_golem_dir(project_root)
    tickets_dir = golem_dir / "tickets"

    if not tickets_dir.exists():
        console.print("[red]No tickets found. Run 'golem plan <spec>' first.[/red]")
        raise typer.Exit(1)

    async def _status_async() -> None:
        store = TicketStore(tickets_dir)
        tickets = await store.list_tickets()

        if not tickets:
            console.print("[yellow]No tickets found.[/yellow]")
            return

        table = Table(title="Golem Status", show_header=True)
        table.add_column("Ticket ID", style="cyan")
        table.add_column("Type")
        table.add_column("Status")
        table.add_column("Assigned To")
        table.add_column("Title")

        status_styles = {
            "approved": "[green]approved[/green]",
            "done": "[green]done[/green]",
            "blocked": "[red]blocked[/red]",
            "needs_work": "[yellow]needs_work[/yellow]",
            "in_progress": "[yellow]in_progress[/yellow]",
            "ready_for_review": "[cyan]ready_for_review[/cyan]",
            "pending": "pending",
        }
        for ticket in tickets:
            style = status_styles.get(ticket.status, ticket.status)
            table.add_row(ticket.id, ticket.type, style, ticket.assigned_to, ticket.title[:60])

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
        pending = await store.list_tickets(status_filter="pending")
        in_progress = await store.list_tickets(status_filter="in_progress")
        candidates = pending + in_progress

        if not candidates:
            console.print("[yellow]No pending or in-progress tickets found.[/yellow]")
            return

        # Re-spawn tech lead with the first pending ticket
        ticket_id = sorted(candidates, key=lambda t: t.id)[0].id
        config = load_config(golem_dir)
        console.print(f"[bold cyan]Golem[/bold cyan] — Resuming from ticket {ticket_id}...")
        await run_tech_lead(ticket_id, golem_dir, config, project_root)
        console.print("[bold]Resume complete.[/bold]")

    asyncio.run(_resume_async())


@app.command()
def version() -> None:
    """Show Golem version, Python version, and platform."""
    from golem.version import get_version_info

    info = get_version_info()
    console.print(f"[bold cyan]Golem[/bold cyan] v{info['version']}")
    console.print(f"Python {info['python']}")
    console.print(f"Platform {info['platform']}")


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
def clean() -> None:
    """Remove .golem/ state, worktrees, and branches."""
    project_root = _get_project_root()
    golem_dir = _get_golem_dir(project_root)

    if not golem_dir.exists():
        console.print("[yellow].golem/ does not exist — nothing to clean.[/yellow]")
        return

    # Remove worktrees via git
    worktrees_dir = golem_dir / "worktrees"
    if worktrees_dir.exists():
        for wt in worktrees_dir.iterdir():
            if wt.is_dir():
                subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=project_root, capture_output=True)

    shutil.rmtree(golem_dir, ignore_errors=True)
    console.print("[bold green]Cleaned .golem/ directory.[/bold green]")
