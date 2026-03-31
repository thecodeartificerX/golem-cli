"""Dependency-aware parallel ticket orchestrator.

Reads the ticket dependency graph, groups tickets into execution waves using
longest-path analysis, runs each wave in parallel (bounded by
max_parallel_per_wave), merges passing worktrees between waves, and handles
partial failures gracefully — all without burning a Claude session on
scheduling logic.

Public API:
    build_dag(tickets) -> dict[str, TicketNode]
    assign_waves(nodes) -> dict[int, list[str]]
    class CycleError(ValueError)
    class WaveExecutor
      .run(ticket_ids, base_branch) -> OrchestratorResult
      .abort(reason) -> None
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from golem.config import GolemConfig
from golem.parallel import _create_batches
from golem.progress import ProgressLogger
from golem.recovery import is_rate_limit_error
from golem.tickets import Ticket, TicketStore
from golem.worktree import commit_task, create_worktree, delete_worktree, merge_group_branches
from golem.writer import JuniorDevResult, spawn_junior_dev

if TYPE_CHECKING:
    from golem.events import EventBus

# -- Constants --

_STAGGER_DELAY_S: float = 1.0
_RATE_LIMIT_BASE_S: float = 30.0
_RATE_LIMIT_MAX_S: float = 300.0


# ---------------------------------------------------------------------------
# DAG data types
# ---------------------------------------------------------------------------


@dataclass
class TicketNode:
    """Node in the ticket dependency DAG."""

    ticket_id: str
    depends_on: list[str]      # ticket IDs that must complete before this one
    dependents: list[str]      # ticket IDs that depend on this one (reverse edges)
    wave: int = -1             # set during wave assignment; -1 = unassigned


# ---------------------------------------------------------------------------
# Cycle detection
# ---------------------------------------------------------------------------


class CycleError(ValueError):
    """Raised when the ticket dependency graph contains a cycle."""


def _detect_cycle(nodes: dict[str, TicketNode]) -> None:
    """Kahn's algorithm for cycle detection.  Raises CycleError on cycle."""
    in_degree: dict[str, int] = {nid: len(n.depends_on) for nid, n in nodes.items()}
    queue: deque[str] = deque(nid for nid, deg in in_degree.items() if deg == 0)
    processed = 0

    while queue:
        nid = queue.popleft()
        processed += 1
        for dep_id in nodes[nid].dependents:
            in_degree[dep_id] -= 1
            if in_degree[dep_id] == 0:
                queue.append(dep_id)

    if processed != len(nodes):
        cycle_members = [nid for nid, deg in in_degree.items() if deg > 0]
        raise CycleError(
            f"Ticket dependency cycle detected involving: {', '.join(sorted(cycle_members))}. "
            "Check the planner output — tickets cannot have circular dependencies."
        )


# ---------------------------------------------------------------------------
# DAG builder
# ---------------------------------------------------------------------------


def build_dag(tickets: list[Ticket]) -> dict[str, TicketNode]:
    """Build a dependency DAG from a list of tickets.

    Returns a dict of ticket_id -> TicketNode.
    Raises CycleError if the graph contains a cycle.
    """
    ticket_ids = {t.id for t in tickets}
    nodes: dict[str, TicketNode] = {}

    for ticket in tickets:
        # Filter out depends_on IDs that don't exist in this run
        valid_deps = [dep for dep in ticket.depends_on if dep in ticket_ids]
        nodes[ticket.id] = TicketNode(
            ticket_id=ticket.id,
            depends_on=valid_deps,
            dependents=[],
        )

    # Build reverse edges
    for node in nodes.values():
        for dep_id in node.depends_on:
            nodes[dep_id].dependents.append(node.ticket_id)

    # Detect cycles (Kahn's: if topological sort doesn't consume all nodes, there's a cycle)
    _detect_cycle(nodes)

    return nodes


# ---------------------------------------------------------------------------
# Wave assignment
# ---------------------------------------------------------------------------


def assign_waves(nodes: dict[str, TicketNode]) -> dict[int, list[str]]:
    """Assign each ticket to an execution wave using longest-path analysis.

    Wave 0 = tickets with no dependencies.
    Wave N = tickets whose deepest dependency is in wave N-1.
    Returns dict of wave_number -> list[ticket_id], ordered by wave.
    """
    in_degree: dict[str, int] = {nid: len(n.depends_on) for nid, n in nodes.items()}
    queue: deque[str] = deque(nid for nid, deg in in_degree.items() if deg == 0)
    wave_map: dict[str, int] = {}

    for nid in list(queue):
        wave_map[nid] = 0

    while queue:
        nid = queue.popleft()
        for dep_id in nodes[nid].dependents:
            # dep_id's wave is at least (nid's wave + 1)
            wave_map[dep_id] = max(wave_map.get(dep_id, 0), wave_map[nid] + 1)
            in_degree[dep_id] -= 1
            if in_degree[dep_id] == 0:
                queue.append(dep_id)

    # Set wave on nodes
    for nid, wave_num in wave_map.items():
        nodes[nid].wave = wave_num

    # Group by wave
    waves: dict[int, list[str]] = {}
    for nid, wave_num in wave_map.items():
        waves.setdefault(wave_num, []).append(nid)

    # Sort ticket IDs within each wave for deterministic ordering
    for wave_num in waves:
        waves[wave_num].sort()

    return waves


# ---------------------------------------------------------------------------
# ETA tracker
# ---------------------------------------------------------------------------


class _ETATracker:
    """Estimates remaining time based on completed waves."""

    def __init__(self, total_waves: int) -> None:
        self._total = total_waves
        self._wave_durations: list[float] = []

    def record_wave(self, duration_s: float) -> None:
        self._wave_durations.append(duration_s)

    def eta_seconds(self) -> float | None:
        if not self._wave_durations:
            return None
        avg = sum(self._wave_durations) / len(self._wave_durations)
        remaining = self._total - len(self._wave_durations)
        return avg * remaining


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


class TicketOutcome(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    RATE_LIMITED = "rate_limited"


@dataclass
class TicketExecutionResult:
    ticket_id: str
    outcome: TicketOutcome
    worktree_branch: str = ""
    writer_result: JuniorDevResult | None = None
    error: str = ""
    duration_s: float = 0.0


@dataclass
class WaveResult:
    wave_number: int
    ticket_results: list[TicketExecutionResult]
    merge_success: bool = False
    merge_error: str = ""
    integration_branch: str = ""   # branch that passing tickets were merged into

    @property
    def passed_count(self) -> int:
        return sum(1 for r in self.ticket_results if r.outcome == TicketOutcome.PASSED)

    @property
    def failed_count(self) -> int:
        return sum(1 for r in self.ticket_results if r.outcome == TicketOutcome.FAILED)

    @property
    def all_failed(self) -> bool:
        return self.passed_count == 0 and len(self.ticket_results) > 0


@dataclass
class OrchestratorResult:
    waves_completed: int
    waves_total: int
    tickets_passed: int
    tickets_failed: int
    tickets_skipped: int
    total_cost_usd: float
    total_duration_s: float
    integration_branch: str = ""
    pr_url: str = ""
    aborted: bool = False
    abort_reason: str = ""


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _get_branch_changed_files(branch: str, base_branch: str, repo_root: Path) -> list[str]:
    """Return list of files changed in branch relative to base_branch."""
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base_branch}...{branch}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# WaveExecutor
# ---------------------------------------------------------------------------


class WaveExecutor:
    """Dependency-aware parallel ticket executor.

    Reads the ticket graph, groups into waves, runs each wave in parallel
    (bounded by max_parallel_per_wave), merges passing worktrees between waves,
    and emits EventBus events throughout.

    This is pure Python orchestration — no Claude SDK sessions are spawned
    by this class itself. It delegates to spawn_junior_dev() for writer pairs
    and to merge_group_branches() for git operations.
    """

    def __init__(
        self,
        golem_dir: Path,
        project_root: Path,
        config: GolemConfig,
        event_bus: EventBus | None = None,
    ) -> None:
        self._golem_dir = golem_dir
        self._project_root = project_root
        self._config = config
        self._event_bus = event_bus
        self._store = TicketStore(golem_dir / "tickets")
        self._progress = ProgressLogger(golem_dir)
        self._abort = asyncio.Event()

    async def run(
        self,
        ticket_ids: list[str] | None = None,
        base_branch: str = "main",
    ) -> OrchestratorResult:
        """Execute all pending tickets with dependency-aware wave scheduling.

        Args:
            ticket_ids: Subset of ticket IDs to run.  None = all pending tickets.
            base_branch: Git branch to branch worktrees from for wave 0.

        Returns OrchestratorResult with summary statistics.
        """
        start_time = time.monotonic()

        # 1. Load tickets
        all_tickets = await self._store.list_tickets()
        if ticket_ids is not None:
            all_tickets = [t for t in all_tickets if t.id in ticket_ids]

        pending = [t for t in all_tickets if t.status in ("pending", "in_progress")]
        if not pending:
            return OrchestratorResult(
                waves_completed=0, waves_total=0,
                tickets_passed=0, tickets_failed=0, tickets_skipped=0,
                total_cost_usd=0.0, total_duration_s=0.0,
            )

        # 2. Build DAG and assign waves
        try:
            nodes = build_dag(pending)
        except CycleError as e:
            self._progress.log_error("orchestrator", str(e))
            raise

        waves = assign_waves(nodes)

        if self._event_bus:
            from golem.events import OrchestratorStarted
            await self._event_bus.emit(OrchestratorStarted(
                wave_count=len(waves),
                ticket_count=len(pending),
                wave_sizes={str(k): len(v) for k, v in waves.items()},
            ))

        # 3. Execute wave by wave
        total_passed = 0
        total_failed = 0
        total_skipped = 0
        total_cost = 0.0
        current_base = base_branch     # each wave branches from the previous wave's integration branch
        failed_ticket_ids: set[str] = set()
        wave_results: list[WaveResult] = []
        eta_tracker = _ETATracker(len(waves))

        for wave_num in sorted(waves.keys()):
            if self._abort.is_set():
                break

            wave_ticket_ids = waves[wave_num]

            # Tickets whose dependency failed in a prior wave are skipped
            runnable = [tid for tid in wave_ticket_ids if not (set(nodes[tid].depends_on) & failed_ticket_ids)]
            skipped_this_wave = [tid for tid in wave_ticket_ids if tid not in runnable]

            for tid in skipped_this_wave:
                total_skipped += 1
                failed_ticket_ids.add(tid)  # propagate: their dependents also skip
                await self._store.update(
                    tid, status="skipped",
                    note="Skipped: dependency failed", agent="orchestrator",
                )

            if not runnable:
                if self._event_bus:
                    from golem.events import WaveSkipped
                    await self._event_bus.emit(WaveSkipped(wave_number=wave_num,
                                                           reason="all_dependencies_failed"))
                self._progress.log_wave_skipped(wave_num, "all_dependencies_failed")
                continue

            if self._event_bus:
                from golem.events import WaveStarted
                await self._event_bus.emit(WaveStarted(
                    wave_number=wave_num,
                    ticket_ids=runnable,
                    base_branch=current_base,
                ))
            self._progress.log_wave_start(wave_num, len(waves), runnable)

            wave_start = time.monotonic()
            wave_result = await self._execute_wave(
                wave_number=wave_num,
                ticket_ids=runnable,
                base_branch=current_base,
            )
            wave_results.append(wave_result)
            eta_tracker.record_wave(time.monotonic() - wave_start)

            # Accumulate counts
            total_passed += wave_result.passed_count
            total_failed += wave_result.failed_count
            for tr in wave_result.ticket_results:
                total_cost += (tr.writer_result.cost_usd if tr.writer_result else 0.0)
                if tr.outcome == TicketOutcome.FAILED:
                    failed_ticket_ids.add(tr.ticket_id)

            if self._event_bus:
                from golem.events import WaveCompleted
                await self._event_bus.emit(WaveCompleted(
                    wave_number=wave_num,
                    passed=wave_result.passed_count,
                    failed=wave_result.failed_count,
                    merge_success=wave_result.merge_success,
                    integration_branch=wave_result.integration_branch,
                ))
            self._progress.log_wave_complete(wave_num, wave_result.passed_count, wave_result.failed_count)

            # Wave failure policy
            if wave_result.all_failed:
                policy = self._config.wave_failure_policy
                if policy == "abort":
                    self._abort.set()
                    if self._event_bus:
                        from golem.events import OrchestratorAborted
                        await self._event_bus.emit(OrchestratorAborted(
                            reason=f"Wave {wave_num} had 0 passing tickets (wave_failure_policy=abort)"
                        ))
                    break
                # policy == "continue": keep going, dependents will be skipped

            # Update base branch for next wave (use integration branch if merge succeeded)
            if wave_result.merge_success and wave_result.integration_branch:
                current_base = wave_result.integration_branch

        duration_s = time.monotonic() - start_time
        integration_branch = wave_results[-1].integration_branch if wave_results else ""

        if self._event_bus:
            from golem.events import OrchestratorComplete
            await self._event_bus.emit(OrchestratorComplete(
                waves_completed=len(wave_results),
                tickets_passed=total_passed,
                tickets_failed=total_failed,
                tickets_skipped=total_skipped,
                total_cost_usd=total_cost,
                duration_s=duration_s,
                integration_branch=integration_branch,
            ))

        return OrchestratorResult(
            waves_completed=len(wave_results),
            waves_total=len(waves),
            tickets_passed=total_passed,
            tickets_failed=total_failed,
            tickets_skipped=total_skipped,
            total_cost_usd=total_cost,
            total_duration_s=duration_s,
            integration_branch=integration_branch,
            aborted=self._abort.is_set(),
        )

    async def abort(self, reason: str = "") -> None:
        """Signal the orchestrator to stop after the current wave completes."""
        self._abort.set()
        if self._event_bus:
            from golem.events import OrchestratorAborted
            await self._event_bus.emit(OrchestratorAborted(reason=reason))

    # -- Wave execution --

    async def _execute_wave(
        self,
        wave_number: int,
        ticket_ids: list[str],
        base_branch: str,
    ) -> WaveResult:
        """Execute a single wave: create worktrees, run writers in parallel, merge."""
        tickets = [await self._store.read(tid) for tid in ticket_ids]

        # 1. Create worktrees for all tickets in this wave
        worktree_paths: dict[str, Path] = {}
        worktree_branches: dict[str, str] = {}

        for ticket in tickets:
            branch, wt_path = self._worktree_info(ticket.id, wave_number)
            try:
                create_worktree(
                    group_id=ticket.id,
                    branch=branch,
                    base_branch=base_branch,
                    path=wt_path,
                    repo_root=self._project_root,
                    branch_prefix=self._config.branch_prefix,
                )
                worktree_paths[ticket.id] = wt_path
                worktree_branches[ticket.id] = branch
                if self._event_bus:
                    from golem.events import WorktreeCreated
                    await self._event_bus.emit(WorktreeCreated(branch=branch, path=str(wt_path)))
            except Exception as e:
                self._progress.log_error(
                    "orchestrator", f"Worktree creation failed for {ticket.id}: {e}"
                )
                await self._store.update(
                    ticket.id, status="failed",
                    note=f"Worktree creation failed: {e}", agent="orchestrator",
                )

        runnable_tickets = [t for t in tickets if t.id in worktree_paths]

        # 2. Dispatch writers in parallel, bounded by max_parallel_per_wave
        ticket_results = await self._dispatch_writers(
            tickets=runnable_tickets,
            worktree_paths=worktree_paths,
        )

        # Add failed worktree-creation entries
        for ticket in tickets:
            if ticket.id not in worktree_paths:
                ticket_results.append(TicketExecutionResult(
                    ticket_id=ticket.id,
                    outcome=TicketOutcome.FAILED,
                    error="Worktree creation failed",
                ))

        # 3. Determine which branches to merge
        passing_results = [r for r in ticket_results if r.outcome == TicketOutcome.PASSED]
        passing_branches = [
            worktree_branches[r.ticket_id]
            for r in passing_results
            if r.ticket_id in worktree_branches
        ]

        # 4. Merge passing branches into integration branch
        merge_success = True
        merge_error = ""
        integration_branch = ""

        if passing_branches:
            integration_branch = self._integration_branch_name(wave_number)
            if self._event_bus:
                from golem.events import MergeStarted
                await self._event_bus.emit(MergeStarted(
                    wave_number=wave_number,
                    source_branches=passing_branches,
                    target_branch=integration_branch,
                ))

            merge_success, merge_error = await self._merge_wave_results(
                passing_branches=passing_branches,
                integration_branch=integration_branch,
                base_branch=base_branch,
            )

            if self._event_bus:
                from golem.events import MergeCompleted
                await self._event_bus.emit(MergeCompleted(
                    wave_number=wave_number,
                    source_branches=passing_branches,
                    target_branch=integration_branch,
                    success=merge_success,
                    error=merge_error,
                ))

        # 5. Clean up worktrees (best-effort — don't abort if cleanup fails)
        for ticket_id, wt_path in worktree_paths.items():
            try:
                delete_worktree(wt_path, self._project_root)
            except Exception as e:
                self._progress.log_warning(
                    "orchestrator", f"Worktree cleanup failed for {ticket_id}: {e}"
                )

        return WaveResult(
            wave_number=wave_number,
            ticket_results=ticket_results,
            merge_success=merge_success,
            merge_error=merge_error,
            integration_branch=integration_branch if merge_success else "",
        )

    async def _dispatch_writers(
        self,
        tickets: list[Ticket],
        worktree_paths: dict[str, Path],
    ) -> list[TicketExecutionResult]:
        """Dispatch writers in parallel, bounded by max_parallel_per_wave.

        Uses gather() with return_exceptions=True (equivalent to Promise.allSettled).
        Stagger launches by _STAGGER_DELAY_S * index to avoid thundering herd.
        """
        max_parallel = self._config.max_parallel_per_wave
        results: list[TicketExecutionResult] = []
        rate_limited_count = 0

        batches = _create_batches(tickets, max_parallel)

        for batch in batches:
            if self._abort.is_set():
                for ticket in batch:
                    results.append(TicketExecutionResult(
                        ticket_id=ticket.id,
                        outcome=TicketOutcome.SKIPPED,
                        error="Aborted",
                    ))
                continue

            # Rate limit backoff between batches
            if rate_limited_count > 0:
                backoff_s = min(
                    _RATE_LIMIT_BASE_S * (2 ** rate_limited_count),
                    _RATE_LIMIT_MAX_S,
                )
                if self._event_bus:
                    from golem.events import RateLimitBackoff
                    await self._event_bus.emit(RateLimitBackoff(
                        delay_s=backoff_s,
                        rate_limited_count=rate_limited_count,
                    ))
                await self._interruptible_sleep(backoff_s)
                rate_limited_count = 0

            # Launch batch with staggered starts
            coros = [
                self._run_single_ticket(
                    ticket=ticket,
                    worktree_path=worktree_paths[ticket.id],
                    stagger_delay_s=idx * _STAGGER_DELAY_S,
                )
                for idx, ticket in enumerate(batch)
            ]
            settled = await asyncio.gather(*coros, return_exceptions=True)

            for outcome in settled:
                if isinstance(outcome, TicketExecutionResult):
                    results.append(outcome)
                    if outcome.outcome == TicketOutcome.RATE_LIMITED:
                        rate_limited_count += 1
                else:
                    # asyncio.gather with return_exceptions=True — unexpected exception object
                    err_msg = str(outcome)
                    results.append(TicketExecutionResult(
                        ticket_id="unknown",
                        outcome=TicketOutcome.FAILED,
                        error=err_msg,
                    ))
                    if is_rate_limit_error(err_msg):
                        rate_limited_count += 1

        return results

    async def _run_single_ticket(
        self,
        ticket: Ticket,
        worktree_path: Path,
        stagger_delay_s: float = 0.0,
    ) -> TicketExecutionResult:
        """Run a writer pair for one ticket.  Never raises — all errors become TicketExecutionResult."""
        import time as _time
        start = _time.monotonic()

        if stagger_delay_s > 0:
            await asyncio.sleep(stagger_delay_s)

        if self._abort.is_set():
            return TicketExecutionResult(
                ticket_id=ticket.id,
                outcome=TicketOutcome.SKIPPED,
                error="Aborted before start",
            )

        if self._event_bus:
            from golem.events import TicketQueued
            await self._event_bus.emit(TicketQueued(
                ticket_id=ticket.id,
                worktree_path=str(worktree_path),
            ))

        await self._store.update(
            ticket.id, status="in_progress",
            note="Dispatched to writer", agent="orchestrator",
        )

        writer_result: JuniorDevResult | None = None
        try:
            writer_result = await spawn_junior_dev(
                ticket=ticket,
                worktree_path=str(worktree_path),
                config=self._config,
                golem_dir=self._golem_dir,
                event_bus=self._event_bus,
            )
        except RuntimeError as e:
            err = str(e)
            if is_rate_limit_error(err):
                await self._store.update(
                    ticket.id, status="pending",
                    note="Rate limited, will retry", agent="orchestrator",
                )
                return TicketExecutionResult(
                    ticket_id=ticket.id,
                    outcome=TicketOutcome.RATE_LIMITED,
                    error=err,
                    duration_s=_time.monotonic() - start,
                )
            await self._store.update(
                ticket.id, status="failed",
                note=f"Writer failed: {err}", agent="orchestrator",
            )
            return TicketExecutionResult(
                ticket_id=ticket.id,
                outcome=TicketOutcome.FAILED,
                error=err,
                duration_s=_time.monotonic() - start,
            )
        except Exception as e:
            err = str(e)
            await self._store.update(
                ticket.id, status="failed",
                note=f"Writer failed: {err}", agent="orchestrator",
            )
            return TicketExecutionResult(
                ticket_id=ticket.id,
                outcome=TicketOutcome.FAILED,
                error=err,
                duration_s=_time.monotonic() - start,
            )

        # QA validation
        qa_passed = await self._run_qa(ticket, worktree_path)

        if not qa_passed and self._config.max_rework_attempts > 0:
            await self._store.update(
                ticket.id, status="needs_work",
                note="QA failed on first attempt — retrying with rework prompt",
                agent="orchestrator",
            )
            # Reload ticket so rework prompt sees the needs_work history event
            ticket = await self._store.read(ticket.id)
            try:
                writer_result = await spawn_junior_dev(
                    ticket=ticket,
                    worktree_path=str(worktree_path),
                    config=self._config,
                    golem_dir=self._golem_dir,
                    event_bus=self._event_bus,
                )
                qa_passed = await self._run_qa(ticket, worktree_path)
            except RuntimeError:
                qa_passed = False

        if qa_passed:
            commit_task(worktree_path, ticket.id, ticket.title)
            await self._store.update(
                ticket.id, status="done",
                note="QA passed, committed", agent="orchestrator",
            )
            branch, _ = self._worktree_info(ticket.id, -1)
            return TicketExecutionResult(
                ticket_id=ticket.id,
                outcome=TicketOutcome.PASSED,
                worktree_branch=branch,
                writer_result=writer_result,
                duration_s=_time.monotonic() - start,
            )
        else:
            await self._store.update(
                ticket.id, status="failed",
                note="QA failed after rework attempt", agent="orchestrator",
            )
            return TicketExecutionResult(
                ticket_id=ticket.id,
                outcome=TicketOutcome.FAILED,
                writer_result=writer_result,
                error="QA failed after rework",
                duration_s=_time.monotonic() - start,
            )

    async def _run_qa(self, ticket: Ticket, worktree_path: Path) -> bool:
        """Run QA checks for a ticket in its worktree.  Returns True if QA passed."""
        from golem.qa import run_qa
        checks = list(ticket.context.qa_checks) + list(self._config.infrastructure_checks)
        if not checks:
            return True
        result = run_qa(str(worktree_path), checks)
        self._progress.log_qa_result(ticket.id, result.passed, result.summary)
        return result.passed

    async def _merge_wave_results(
        self,
        passing_branches: list[str],
        integration_branch: str,
        base_branch: str,
    ) -> tuple[bool, str]:
        """Merge passing branches into integration_branch.

        Merge order: deterministic (ticket ID order, which is already sorted).
        Pre-conflict detection: branches touching the same file are merged
        sequentially with conflict resolution from spec 08.

        Returns (success, error_detail).
        """
        if not passing_branches:
            return True, ""

        # Detect file overlaps between branches
        file_map: dict[str, list[str]] = {}
        for branch in passing_branches:
            changed_files = _get_branch_changed_files(branch, base_branch, self._project_root)
            for f in changed_files:
                file_map.setdefault(f, []).append(branch)

        overlapping_files = {f: branches for f, branches in file_map.items() if len(branches) > 1}

        if overlapping_files:
            if self._event_bus:
                from golem.events import MergeConflictPredicted
                for filename, branches in overlapping_files.items():
                    await self._event_bus.emit(MergeConflictPredicted(
                        filename=filename,
                        branch_a=branches[0],
                        branch_b=branches[1] if len(branches) > 1 else "",
                        wave_number=-1,
                    ))
            self._progress.log_warning(
                "orchestrator",
                f"File overlap detected in {len(overlapping_files)} files across "
                f"{len(passing_branches)} branches — merging sequentially with conflict resolution",
            )

        success, conflict_info = merge_group_branches(
            group_branches=passing_branches,
            target_branch=integration_branch,
            repo_root=self._project_root,
        )

        return success, conflict_info

    async def _interruptible_sleep(self, duration: float) -> bool:
        """Sleep for duration seconds, waking early if the abort event fires.

        Returns True if interrupted by abort, False if the full duration elapsed.
        Mirrors the pattern from golem.parallel._interruptible_sleep but gates on
        the WaveExecutor's own _abort event so kill signals cancel backoff waits.
        """
        try:
            await asyncio.wait_for(self._abort.wait(), timeout=duration)
            return True  # interrupted
        except asyncio.TimeoutError:
            return False  # completed normally

    # -- Helpers --

    def _worktree_info(self, ticket_id: str, wave_number: int) -> tuple[str, Path]:
        """Return (branch_name, worktree_path) for a ticket."""
        if self._config.session_id:
            session_prefix = f"{self._config.branch_prefix}/{self._config.session_id}"
        else:
            session_prefix = self._config.branch_prefix
        branch = f"{session_prefix}/{ticket_id.lower()}"
        wt_path = self._golem_dir / "worktrees" / ticket_id.lower()
        return branch, wt_path

    def _integration_branch_name(self, wave_number: int) -> str:
        """Return the integration branch name for a wave."""
        if self._config.session_id:
            session_prefix = f"{self._config.branch_prefix}/{self._config.session_id}"
        else:
            session_prefix = self._config.branch_prefix
        return f"{session_prefix}/wave-{wave_number}-integration"
