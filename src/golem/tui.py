from __future__ import annotations

from typing import Callable

from rich.console import Console
from rich.live import Live
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskID, TextColumn
from rich.prompt import Prompt
from rich.table import Table

from golem.config import GolemConfig

console = Console()


def force_defaults(config: GolemConfig | None = None) -> GolemConfig:
    """Return defaults without any TUI interaction."""
    return config or GolemConfig()


class PreRunScreen:
    """Interactive pre-run settings screen."""

    def __init__(self, group_count: int, task_count: int, groups_summary: list[str]) -> None:
        self._group_count = group_count
        self._task_count = task_count
        self._groups_summary = groups_summary

    def run(self, config: GolemConfig) -> tuple[GolemConfig, str]:
        """
        Show pre-run TUI. Returns (config, action) where action is one of:
        'run', 'dry_run', 'quit'.
        """
        from golem import __version__
        console.print(f"\n[bold]Golem v{__version__}[/bold]\n")
        console.print(f"Found [bold]{self._task_count}[/bold] tasks across [bold]{self._group_count}[/bold] parallel groups:\n")
        for summary in self._groups_summary:
            console.print(f"  {summary}")

        while True:
            console.print("\n[bold]Settings:[/bold]")
            console.print(f"  [1] Max parallel worktrees:  {config.max_parallel}")
            console.print(f"  [2] Max retries per task:    {config.max_retries}")
            console.print(f"  [3] Planner model:           {config.planner_model}")
            console.print(f"  [4] Worker model:            {config.worker_model}")
            console.print(f"  [5] Validator model:         {config.validator_model}")
            console.print("\n  [Enter] Start execution")
            console.print("  [e] Edit settings")
            console.print("  [d] Dry run (show tasks.json only)")
            console.print("  [q] Quit")

            choice = Prompt.ask("Choice", default="")

            if choice == "" or choice.lower() == "enter":
                return config, "run"
            elif choice.lower() == "e":
                config = self._edit_settings(config)
            elif choice.lower() == "d":
                return config, "dry_run"
            elif choice.lower() == "q":
                return config, "quit"

    def _edit_settings(self, config: GolemConfig) -> GolemConfig:
        setting = Prompt.ask("Setting to change [1-5]", default="1")
        if setting == "1":
            val = Prompt.ask("Max parallel worktrees", default=str(config.max_parallel))
            config.max_parallel = int(val)
        elif setting == "2":
            val = Prompt.ask("Max retries per task", default=str(config.max_retries))
            config.max_retries = int(val)
        elif setting == "3":
            config.planner_model = Prompt.ask("Planner model", default=config.planner_model)
        elif setting == "4":
            config.worker_model = Prompt.ask("Worker model", default=config.worker_model)
        elif setting == "5":
            config.validator_model = Prompt.ask("Validator model", default=config.validator_model)
        return config


class LiveDashboard:
    """Real-time execution dashboard using rich Live."""

    def __init__(self, groups: list[str], task_counts: dict[str, int]) -> None:
        self._groups = groups
        self._task_counts = task_counts
        self._completed: dict[str, int] = {g: 0 for g in groups}
        self._current_task: dict[str, str] = {g: "" for g in groups}
        self._task_status: dict[str, str] = {g: "pending" for g in groups}
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
        )
        self._progress_tasks: dict[str, TaskID] = {}
        self._live = Live(self._render(), refresh_per_second=4)

    def __enter__(self) -> "LiveDashboard":
        self._live.start()
        for group_id in self._groups:
            total = self._task_counts.get(group_id, 1)
            tid = self._progress.add_task(f"[{group_id}]", total=total)
            self._progress_tasks[group_id] = tid
        return self

    def __exit__(self, *args: object) -> None:
        self._live.stop()

    def _render(self) -> Table:
        table = Table(show_header=False, box=None, padding=(0, 1))
        table.add_column("Group", style="cyan")
        table.add_column("Status")
        table.add_column("Current Task", style="dim")
        for group_id in self._groups:
            completed = self._completed.get(group_id, 0)
            total = self._task_counts.get(group_id, 1)
            status = self._task_status.get(group_id, "pending")
            current = self._current_task.get(group_id, "")
            status_str = f"{completed}/{total}"
            table.add_row(f"[{group_id}]", status_str, f"{status}: {current}")
        return table

    def update_task_status(self, group_id: str, task_id: str, status: str, current_task_name: str) -> None:
        self._task_status[group_id] = status
        self._current_task[group_id] = current_task_name
        if status == "completed":
            self._completed[group_id] = self._completed.get(group_id, 0) + 1
            if group_id in self._progress_tasks:
                self._progress.advance(self._progress_tasks[group_id])
        self._live.update(self._render())

    def get_callback(self) -> Callable[[str, str, str, str], None]:
        return self.update_task_status
