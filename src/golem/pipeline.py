"""Pipeline coordinator — orchestrates an Edict through the full agent pipeline."""

from __future__ import annotations

import asyncio
import dataclasses
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from golem.config import GolemConfig
from golem.edict import (
    EDICT_DONE,
    EDICT_FAILED,
    EDICT_IN_PROGRESS,
    EDICT_NEEDS_ATTENTION,
    EDICT_PLANNING,
    Edict,
    EdictStore,
)
from golem.tickets import TicketStore

if TYPE_CHECKING:
    from golem.events import EventBus


@dataclass
class PipelineResult:
    edict_id: str = ""
    status: str = ""
    pr_url: str | None = None
    total_cost_usd: float = 0.0
    duration_s: float = 0.0
    tickets_passed: int = 0
    tickets_failed: int = 0
    error: str | None = None
    waves: list[list[str]] = field(default_factory=list)  # ticket ID waves from dependency DAG


class PipelineCoordinator:
    """Orchestrates an Edict through the full agent pipeline.

    Pipeline stages:
    1. Planning — run_planner() enriches edict with plans/references/tickets
    2. Execution — route to tech_lead, wave executor, or direct junior_dev based on config
    3. Completion — mark edict done, set PR URL
    """

    def __init__(
        self,
        edict: Edict,
        edict_store: EdictStore,
        ticket_store: TicketStore,
        config: GolemConfig,
        project_root: Path,
        golem_dir: Path,
        event_bus: EventBus | None = None,
        server_url: str = "",
    ) -> None:
        self._edict = edict
        self._edict_store = edict_store
        self._ticket_store = ticket_store
        self._config = config
        self._project_root = project_root
        self._golem_dir = golem_dir
        self._event_bus = event_bus
        self._server_url = server_url
        self._resume_event = asyncio.Event()
        self._resume_event.set()  # starts unpaused
        self._current_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._cost_usd: float = 0.0
        self._killed = False

    async def run(self) -> PipelineResult:
        """Run the full pipeline: planner -> tech_lead/junior_dev -> done.

        On pipeline failure (non-cancellation exceptions), retries up to
        ``config.edict_max_retries`` times.  Each retry transitions the edict
        through ``EDICT_FAILED -> EDICT_NEEDS_ATTENTION -> EDICT_PLANNING``
        before re-entering the main loop.  After exhausting retries the edict
        is left in ``EDICT_NEEDS_ATTENTION`` for human triage.
        """
        from golem.conductor import classify_spec
        from golem.events import SessionComplete, SessionStart
        from golem.planner import run_planner
        from golem.progress import ProgressLogger
        from golem.tech_lead import run_tech_lead
        from golem.junior_dev import spawn_junior_dev

        start_time = time.monotonic()
        result = PipelineResult(edict_id=self._edict.id)

        # Write the edict body as a spec file for the planner
        spec_path = self._golem_dir / "edicts" / self._edict.id / "spec.md"
        spec_path.parent.mkdir(parents=True, exist_ok=True)
        spec_path.write_text(self._edict.body or self._edict.title, encoding="utf-8")

        # Open session.log for pipeline diagnostics
        self._golem_dir.mkdir(parents=True, exist_ok=True)
        edict_dir = self._golem_dir / "edicts" / self._edict.id
        session_log_path = edict_dir / "session.log"
        session_log_file = open(session_log_path, "a", encoding="utf-8")  # noqa: SIM115

        def _log(msg: str) -> None:
            print(msg, file=sys.stderr)
            try:
                session_log_file.write(msg + "\n")
                session_log_file.flush()
            except (OSError, ValueError):
                pass

        # Ensure project-level memory directory exists (persists across edicts)
        project_memory_dir = Path(self._project_root) / ".golem" / "memory"
        project_memory_dir.mkdir(parents=True, exist_ok=True)

        # Create .golem/.gitignore if it doesn't exist
        golem_gitignore = Path(self._project_root) / ".golem" / ".gitignore"
        if not golem_gitignore.exists():
            golem_gitignore.write_text(
                "# Ephemeral runtime state -- do not commit\n"
                "edicts/\n"
                "worktrees/\n"
                "merge_staging/\n"
                "conflict-log.json\n"
                "config.json\n"
                "\n"
                "# Memory persists across edicts -- DO commit\n"
                "!memory/\n",
                encoding="utf-8",
            )

        retry_count = 0
        max_retries = self._config.edict_max_retries

        try:
            while True:
                try:
                    # Emit EdictCreated event (only on first attempt)
                    if retry_count == 0 and self._event_bus:
                        from golem.events import EdictCreated

                        await self._event_bus.emit(
                            EdictCreated(
                                edict_id=self._edict.id,
                                title=self._edict.title,
                                repo_path=self._edict.repo_path,
                            )
                        )

                    # -- Planning phase --
                    # On retry, the edict is already in EDICT_PLANNING (set by the
                    # retry path: failed -> needs_attention -> planning).
                    if retry_count == 0:
                        _log("[PIPELINE] Setting edict status to planning...")
                        await self._edict_store.update_status(self._edict.id, EDICT_PLANNING)
                        _log("[PIPELINE] Edict status set to planning")
                    else:
                        _log(f"[PIPELINE] Retry {retry_count}: edict already in planning")
                    if self._event_bus:
                        from golem.events import EdictUpdated

                        await self._event_bus.emit(
                            EdictUpdated(
                                edict_id=self._edict.id,
                                old_status=self._edict.status,
                                new_status=EDICT_PLANNING,
                            )
                        )

                    _log("[PIPELINE] Checking pause state (pre-execution)...")
                    await self._check_pause()
                    if self._killed:
                        result.status = EDICT_FAILED
                        result.error = "Killed by operator"
                        try:
                            await self._edict_store.update_status(
                                self._edict.id, EDICT_FAILED, error="Killed by operator",
                            )
                        except (ValueError, OSError):
                            pass
                        return result

                    # Conductor classification
                    if self._config.conductor_enabled:
                        classify_result = classify_spec(self._edict.body or self._edict.title)
                        self._config.apply_complexity_profile(classify_result.complexity)

                    # Create pipeline directories
                    for subdir in ("tickets", "plans", "research", "references"):
                        (edict_dir / subdir).mkdir(exist_ok=True)

                    progress = ProgressLogger(edict_dir)
                    progress.log_planner_start()

                    await self._event_bus.emit(SessionStart(
                        spec_path=str(spec_path),
                        complexity="",
                        config_snapshot=dataclasses.asdict(self._config),
                    )) if self._event_bus else None

                    _log(f"[PIPELINE] Starting planner for {self._edict.id} (conductor_enabled={self._config.conductor_enabled}, skip_tech_lead={self._config.skip_tech_lead})")
                    try:
                        self._current_task = asyncio.create_task(run_planner(
                            spec_path, edict_dir, self._config, self._project_root,
                            event_bus=self._event_bus, server_url=self._server_url,
                        ))
                        planner_result = await self._current_task
                    finally:
                        self._current_task = None
                    self._cost_usd += planner_result.cost_usd

                    # Multi-ticket awareness: find the integration/review ticket or fall back to last
                    planner_tickets = await self._ticket_store.list_tickets()
                    summary_ticket = next((t for t in planner_tickets if t.type == "review"), None)
                    ticket_id = summary_ticket.id if summary_ticket else planner_result.ticket_id

                    progress.log_planner_complete(ticket_id)
                    _log(f"[PIPELINE] Planner complete: ticket_ids={planner_result.ticket_ids}, dispatch={ticket_id}, cost=${planner_result.cost_usd:.4f}")

                    # Advance all planner-stage tickets to tech_lead stage for board visibility
                    for _planner_ticket in planner_tickets:
                        if _planner_ticket.pipeline_stage == "planner":
                            try:
                                await self._ticket_store.update(
                                    _planner_ticket.id,
                                    status="pending",
                                    note="Advancing to Tech Lead dispatch",
                                    pipeline_stage="tech_lead",
                                )
                            except (ValueError, OSError, FileNotFoundError):
                                pass

                    # Compute ticket waves from dependency DAG (informational in Phase 2)
                    from golem.tickets import compute_waves as _compute_waves
                    try:
                        _all_tickets = await self._ticket_store.list_tickets()
                        result.waves = _compute_waves(_all_tickets)
                        _log(f"[PIPELINE] Ticket waves: {len(result.waves)} waves, {[len(w) for w in result.waves]} tickets per wave")
                    except ValueError:
                        result.waves = []  # cycle detected, degrade gracefully
                        _log("[PIPELINE] Ticket dependency cycle detected — wave scheduling disabled")

                    # -- Execution phase --
                    _log("[PIPELINE] Setting edict status to in_progress...")
                    await self._edict_store.update_status(self._edict.id, EDICT_IN_PROGRESS)
                    _log("[PIPELINE] Edict status set to in_progress")
                    _log("[PIPELINE] Emitting EdictUpdated event...")
                    if self._event_bus:
                        from golem.events import EdictUpdated

                        await self._event_bus.emit(
                            EdictUpdated(
                                edict_id=self._edict.id,
                                old_status=EDICT_PLANNING,
                                new_status=EDICT_IN_PROGRESS,
                            )
                        )
                    _log("[PIPELINE] EdictUpdated event emitted")

                    _log("[PIPELINE] Checking pause state (pre-dispatch)...")
                    await self._check_pause()
                    _log("[PIPELINE] Pause check passed, proceeding to dispatch")
                    if self._killed:
                        result.status = EDICT_FAILED
                        result.error = "Killed by operator"
                        try:
                            await self._edict_store.update_status(
                                self._edict.id, EDICT_FAILED, error="Killed by operator",
                            )
                        except (ValueError, OSError):
                            pass
                        return result

                    if not self._config.skip_tech_lead:
                        _log(f"[PIPELINE] Dispatching Tech Lead for {ticket_id}")
                        try:
                            await self._ticket_store.update(
                                ticket_id,
                                status="in_progress",
                                note="Tech Lead picked up handoff ticket",
                                pipeline_stage="tech_lead",
                            )
                        except (ValueError, OSError, FileNotFoundError):
                            pass
                        progress.log_tech_lead_start(ticket_id)
                        try:
                            self._current_task = asyncio.create_task(run_tech_lead(
                                ticket_id, edict_dir, self._config, self._project_root,
                                event_bus=self._event_bus, server_url=self._server_url,
                            ))
                            tl_result = await self._current_task
                        finally:
                            self._current_task = None
                        self._cost_usd += tl_result.cost_usd
                        elapsed = time.monotonic() - start_time
                        progress.log_tech_lead_complete(elapsed_s=elapsed)
                        try:
                            await self._ticket_store.update(
                                ticket_id,
                                status="done",
                                note="Tech Lead completed all work",
                                pipeline_stage="done",
                            )
                        except (ValueError, OSError, FileNotFoundError):
                            pass
                        progress.log_task_complete(ticket_id)
                    else:
                        _log(f"[PIPELINE] TRIVIAL tier: skip_tech_lead=True, spawning Junior Dev directly for {ticket_id}")
                        ticket = await self._ticket_store.read(ticket_id)
                        _log(f"[PIPELINE] Junior Dev ticket loaded: {ticket.id} - {ticket.title}")
                        try:
                            await self._ticket_store.update(
                                ticket_id,
                                status="in_progress",
                                note="Junior Dev picked up handoff ticket (TRIVIAL tier)",
                                pipeline_stage="junior_dev",
                            )
                        except (ValueError, OSError, FileNotFoundError):
                            pass
                        try:
                            self._current_task = asyncio.create_task(spawn_junior_dev(
                                ticket, str(self._project_root), self._config, edict_dir,
                                event_bus=self._event_bus, server_url=self._server_url,
                            ))
                            jr_result = await self._current_task
                        finally:
                            self._current_task = None
                        self._cost_usd += jr_result.cost_usd
                        _log(f"[PIPELINE] Junior Dev complete: cost=${jr_result.cost_usd:.4f}")
                        try:
                            await self._ticket_store.update(
                                ticket_id,
                                status="done",
                                note="Junior Dev completed handoff ticket (TRIVIAL tier)",
                                pipeline_stage="done",
                            )
                        except (ValueError, OSError, FileNotFoundError):
                            pass
                        progress.log_task_complete(ticket_id)

                    # -- Completion phase --
                    tickets = await self._ticket_store.list_tickets()
                    result.tickets_passed = sum(1 for t in tickets if t.status == "done")
                    result.tickets_failed = sum(1 for t in tickets if t.status == "failed")

                    total_cost = progress.sum_agent_costs()
                    self._cost_usd = max(self._cost_usd, total_cost)
                    duration = time.monotonic() - start_time

                    await self._edict_store.update_status(self._edict.id, EDICT_DONE)
                    result.status = EDICT_DONE

                    edict = await self._edict_store.read(self._edict.id)
                    result.pr_url = edict.pr_url

                    if self._event_bus:
                        await self._event_bus.emit(SessionComplete(
                            status="done", cost_usd=self._cost_usd, duration_s=duration, error="",
                        ))

                    # Success — exit the retry loop
                    break

                except asyncio.CancelledError:
                    # Cancellation is never retried — propagate immediately
                    error_msg = "Killed by operator"
                    _log(f"[PIPELINE] CANCELLED: {error_msg}")
                    try:
                        await self._edict_store.update_status(self._edict.id, EDICT_FAILED, error=error_msg)
                    except (ValueError, OSError):
                        pass
                    result.status = EDICT_FAILED
                    result.error = error_msg

                    if self._event_bus:
                        duration = time.monotonic() - start_time
                        await self._event_bus.emit(SessionComplete(
                            status="failed", cost_usd=self._cost_usd, duration_s=duration, error=error_msg,
                        ))
                    break

                except Exception as exc:
                    import traceback
                    error_msg = str(exc)
                    _log(f"[PIPELINE] ERROR: {error_msg}")
                    traceback.print_exc(file=sys.stderr)

                    # Transition to EDICT_FAILED first (required before needs_attention)
                    try:
                        await self._edict_store.update_status(self._edict.id, EDICT_FAILED, error=error_msg)
                    except (ValueError, OSError):
                        pass

                    if retry_count < max_retries:
                        retry_count += 1
                        _log(f"[PIPELINE] Edict retry {retry_count}/{max_retries}")
                        try:
                            await self._edict_store.update_status(self._edict.id, EDICT_NEEDS_ATTENTION)
                        except (ValueError, OSError):
                            pass
                        try:
                            await self._edict_store.update_status(self._edict.id, EDICT_PLANNING)
                        except (ValueError, OSError):
                            pass
                        continue  # retry the pipeline loop
                    else:
                        # Exhausted retries — leave in needs_attention for human triage
                        _log(f"[PIPELINE] Exhausted {max_retries} retries, edict needs human attention")
                        try:
                            await self._edict_store.update_status(self._edict.id, EDICT_NEEDS_ATTENTION)
                        except (ValueError, OSError):
                            pass
                        result.status = EDICT_NEEDS_ATTENTION
                        result.error = error_msg

                        if self._event_bus:
                            duration = time.monotonic() - start_time
                            await self._event_bus.emit(SessionComplete(
                                status="needs_attention", cost_usd=self._cost_usd,
                                duration_s=duration, error=error_msg,
                            ))
                        break

        finally:
            session_log_file.close()

        result.duration_s = time.monotonic() - start_time
        result.total_cost_usd = self._cost_usd
        return result

    async def pause(self) -> None:
        """Pause the pipeline."""
        self._resume_event.clear()

    async def resume(self) -> None:
        """Resume the pipeline."""
        self._resume_event.set()

    async def kill(self) -> None:
        """Kill the pipeline."""
        self._killed = True
        self._resume_event.set()  # unblock if paused
        if self._current_task and not self._current_task.done():
            self._current_task.cancel()

    async def send_guidance(self, text: str) -> None:
        """Send operator guidance — creates a guidance ticket."""
        from golem.tickets import Ticket, TicketContext

        guidance_ticket = Ticket(
            id="",
            type="guidance",
            title="Operator guidance",
            status="pending",
            priority="high",
            created_by="operator",
            assigned_to="",
            context=TicketContext(blueprint=text),
            edict_id=self._edict.id,
        )
        await self._ticket_store.create(guidance_ticket)

    def add_cost(self, cost_usd: float) -> None:
        """Accumulate cost from agent completions."""
        self._cost_usd += cost_usd

    async def _check_pause(self) -> None:
        """Wait if paused."""
        await self._resume_event.wait()
