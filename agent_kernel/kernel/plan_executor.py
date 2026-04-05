"""PlanExecutor for sequential, parallel, conditional, dag, and speculative execution.

Implements Phase 3 (Parallel Execution) and extended plan type support for the
agent-kernel architecture. This module orchestrates SequentialPlan, ParallelPlan,
ConditionalPlan, DependencyGraph, and SpeculativePlan execution using asyncio
concurrency primitives.

Design boundaries:
  - PlanExecutor does NOT manage event logs or deduplication 鈥?those are
    handled by the TurnEngine for each individual Action.
  - PlanExecutor accepts a generic ``turn_runner`` callable so it never
    imports TurnEngine directly (dependency-inversion for testability).
"""

from __future__ import annotations

import asyncio
import contextlib
import graphlib
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from agent_kernel.kernel.branch_monitor import BranchMonitor
    from agent_kernel.kernel.dedupe_store import DedupeStorePort

from agent_kernel.kernel.contracts import (
    Action,
    BranchFailure,
    BranchResult,
    ConditionalPlan,
    DependencyGraph,
    ExecutionPlan,
    FailureEnvelope,
    ObservabilityHook,
    ParallelGroup,
    ParallelJoinResult,
    ParallelPlan,
    SequentialPlan,
    SpeculativePlan,
)
from agent_kernel.kernel.turn_engine import TurnResult


class UnsupportedPlanTypeError(Exception):
    """Raised when execute_plan receives an unrecognised plan type.

    Attributes:
        plan: The unsupported plan object that triggered the error.

    """

    def __init__(self, plan: Any) -> None:
        """Initialise the error with a human-readable message.

        Args:
            plan: The unsupported plan object.

        """
        super().__init__(
            f"Unsupported plan type: {type(plan).__name__!r}. "
            "Register a handler in PlanExecutor.execute_plan()."
        )
        self.plan = plan


@dataclass(frozen=True, slots=True)
class PlanResult:
    """Aggregate result produced by PlanExecutor after plan completion.

    Attributes:
        plan_kind: Discriminator for the executed plan type.
        total_actions: Total number of actions submitted for execution.
        succeeded: Number of actions that completed without failure.
        failed: Number of actions that failed or raised an exception.
        join_results: Ordered list of group join results for parallel plans.
            Empty list for sequential plans.

    """

    plan_kind: Literal["sequential", "parallel", "conditional", "dependency_graph", "speculative"]
    total_actions: int
    succeeded: int
    failed: int
    join_results: list[ParallelJoinResult] = field(default_factory=list)
    committed_winner_id: str | None = None  # SpeculativePlan only: candidate_id of committed winner

    @property
    def all_succeeded(self) -> bool:
        """Returns True when every action succeeded.

        Returns:
            ``True`` if ``failed`` is zero.

        """
        return self.succeeded == self.total_actions


# Type alias for the async callable accepted by PlanExecutor.
TurnRunner = Callable[[Action], Coroutine[Any, Any, TurnResult]]


class PlanExecutor:
    """Executes SequentialPlan or ParallelPlan using a kernel turn runner.

    For SequentialPlan: delegates to ``turn_runner`` for each step in order.
    For ParallelPlan: uses ``asyncio.gather`` for concurrent group execution.

    This class does NOT manage event log or deduplication 鈥?those are
    handled by the TurnEngine for each individual Action.
    """

    def __init__(
        self,
        turn_runner: TurnRunner,
        branch_monitor: BranchMonitor | None = None,
        observability_hook: ObservabilityHook | None = None,
        dedupe_store: DedupeStorePort | None = None,
    ) -> None:
        """Initialise PlanExecutor with an async turn-runner callable.

        Args:
            turn_runner: Async callable that accepts an Action and returns a
                TurnResult. Typically wraps TurnEngine.execute().
            branch_monitor: Optional ``BranchMonitor`` instance for per-branch
                heartbeat tracking in parallel groups.  When ``None``, branch
                monitoring is disabled (backward-compatible default).
            observability_hook: Optional hook for emitting per-branch telemetry
                events (``on_parallel_branch_result``).  When ``None``, branch
                telemetry is disabled.
            dedupe_store: Optional ``DedupeStorePort`` for per-branch idempotency
                checks.  When present, already-acknowledged branches are skipped
                on crash-replay.  When ``None``, per-branch dedupe is disabled.

        """
        self._turn_runner = turn_runner
        self._branch_monitor = branch_monitor
        self._observability_hook = observability_hook
        self._dedupe_store = dedupe_store
        # Per-execution speculative task registry.  Populated during
        # _execute_speculative and cleared after candidates settle.
        # A stack is used so that nested SpeculativePlan executions do not
        # overwrite the outer layer's task dict (D-H4).
        self._active_speculative_tasks: dict[str, asyncio.Task[PlanResult]] = {}
        self._speculative_task_stack: list[dict[str, asyncio.Task[PlanResult]]] = []
        # Tracks the last committed winner so _execute_speculative() can
        # surface it in the returned PlanResult for event-log recording.
        self._committed_winner_id: str | None = None

    async def commit_speculation(self, winner_candidate_id: str) -> None:
        """Cancel all speculative tasks except the winner and records the decision.

        Called when a ``speculation_committed`` signal identifies the winning
        candidate.  Non-winner asyncio Tasks are cancelled so their side-effects
        are not applied.  Already-done tasks are left untouched.  The winner
        ``candidate_id`` is stored so ``_execute_speculative()`` can include it
        in the returned ``PlanResult`` for downstream event-log recording.

        Args:
            winner_candidate_id: ``candidate_id`` of the candidate to keep.

        """
        self._committed_winner_id = winner_candidate_id
        for cid, task in list(self._active_speculative_tasks.items()):
            if cid != winner_candidate_id and not task.done():
                task.cancel()

    async def execute_plan(self, plan: ExecutionPlan, run_id: str) -> PlanResult:
        """Dispatches plan execution to the correct strategy.

        Args:
            plan: A SequentialPlan, ParallelPlan, ConditionalPlan,
                DependencyGraph, or SpeculativePlan.
            run_id: Kernel run identifier for contextual logging.

        Returns:
            PlanResult summarising overall success/failure counts.

        Raises:
            UnsupportedPlanTypeError: When ``plan`` is not a recognised plan type.

        """
        if isinstance(plan, SequentialPlan):
            return await self._execute_sequential(plan, run_id)
        if isinstance(plan, ParallelPlan):
            return await self._execute_parallel(plan, run_id)
        if isinstance(plan, ConditionalPlan):
            return await self._execute_conditional(plan, run_id)
        if isinstance(plan, DependencyGraph):
            return await self._execute_dag(plan, run_id)
        if isinstance(plan, SpeculativePlan):
            return await self._execute_speculative(plan, run_id)
        raise UnsupportedPlanTypeError(plan)

    async def _execute_sequential(
        self,
        plan: SequentialPlan,
        run_id: str,
    ) -> PlanResult:
        """Execute each step in order, accumulating success/failure counts.

        All steps are attempted regardless of prior failures so that the caller
        receives a complete accounting of the plan.  Failures increment the
        ``failed`` counter but do not halt subsequent steps.

        Args:
            plan: Sequential plan with ordered steps.
            run_id: Kernel run identifier.

        Returns:
            PlanResult with sequential plan_kind.

        """
        succeeded = 0
        failed = 0
        for action in plan.steps:
            try:
                result = await self._turn_runner(action)
                if result.outcome_kind in ("dispatched", "noop"):
                    succeeded += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
        return PlanResult(
            plan_kind="sequential",
            total_actions=len(plan.steps),
            succeeded=succeeded,
            failed=failed,
            join_results=[],
        )

    async def _execute_parallel(
        self,
        plan: ParallelPlan,
        run_id: str,
    ) -> PlanResult:
        """Execute groups sequentially, actions within each group concurrently.

        Groups in a ParallelPlan are ordered 鈥?each group executes after the
        previous group's join barrier is satisfied (or not).  Actions within a
        single group execute concurrently via ``asyncio.gather``.

        Args:
            plan: Parallel plan with ordered groups.
            run_id: Kernel run identifier.

        Returns:
            PlanResult with parallel plan_kind and per-group join results.

        """
        join_results: list[ParallelJoinResult] = []
        total_actions = 0
        total_succeeded = 0
        total_failed = 0

        for group in plan.groups:
            total_actions += len(group.actions)
            join_result = await self._execute_group(group, run_id)
            join_results.append(join_result)
            total_succeeded += len(join_result.successes)
            total_failed += len(join_result.failures)

        return PlanResult(
            plan_kind="parallel",
            total_actions=total_actions,
            succeeded=total_succeeded,
            failed=total_failed,
            join_results=join_results,
        )

    async def _execute_group(
        self,
        group: ParallelGroup,
        run_id: str,
    ) -> ParallelJoinResult:
        """Concurrently executes all actions in a group and evaluates the join.

        Args:
            group: Parallel group with actions, join_strategy, and optional timeout.
            run_id: Kernel run identifier.

        Returns:
            ParallelJoinResult with success/failure breakdown and join verdict.

        """
        # Register branches before gather so the monitor knows about them.
        if self._branch_monitor is not None:
            for action in group.actions:
                self._branch_monitor.register_branch(
                    action.action_id,
                    expected_interval_ms=group.timeout_ms or 30_000,
                )

        async def _monitored_run(action: Action) -> TurnResult:
            try:
                result = await self._turn_runner(action)
                if self._branch_monitor is not None:
                    self._branch_monitor.complete_branch(action.action_id)
                return result
            except Exception:
                if self._branch_monitor is not None:
                    self._branch_monitor.complete_branch(action.action_id)
                raise

        if group.timeout_ms is not None:
            timeout_s = group.timeout_ms / 1000.0
            tasks = [asyncio.create_task(_monitored_run(action)) for action in group.actions]
            done, pending = await asyncio.wait(tasks, timeout=timeout_s)
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            raw_results = []
            for task in tasks:
                if task in done:
                    exc = task.exception()
                    raw_results.append(exc if exc is not None else task.result())
                else:
                    raw_results.append(TimeoutError(f"Branch timeout after {group.timeout_ms}ms"))
        else:
            coros = [_monitored_run(action) for action in group.actions]
            raw_results = await asyncio.gather(*coros, return_exceptions=True)

        successes: list[BranchResult] = []
        failures: list[BranchFailure] = []

        for action, raw in zip(group.actions, raw_results, strict=True):
            if isinstance(raw, BaseException):
                failure_code = type(raw).__name__
                outcome_label = "timeout" if isinstance(raw, TimeoutError) else "failed"
                failures.append(
                    BranchFailure(
                        action_id=action.action_id,
                        failure_kind="exception",
                        failure_code=failure_code,
                        evidence=None,
                    )
                )
                if self._observability_hook is not None:
                    with contextlib.suppress(Exception):
                        self._observability_hook.on_parallel_branch_result(
                            run_id=run_id,
                            group_idempotency_key=group.group_idempotency_key,
                            action_id=action.action_id,
                            outcome=outcome_label,
                            failure_code=failure_code,
                        )
            else:
                turn_result: TurnResult = raw
                if turn_result.outcome_kind in ("dispatched", "noop"):
                    output_json = (
                        turn_result.action_commit
                        if isinstance(turn_result.action_commit, dict)
                        else None
                    )
                    successes.append(
                        BranchResult(
                            action_id=action.action_id,
                            output_json=output_json,
                            acknowledged=True,
                        )
                    )
                    if self._observability_hook is not None:
                        with contextlib.suppress(Exception):
                            self._observability_hook.on_parallel_branch_result(
                                run_id=run_id,
                                group_idempotency_key=group.group_idempotency_key,
                                action_id=action.action_id,
                                outcome="acknowledged",
                            )
                else:
                    # blocked or recovery_pending 鈫?BranchFailure
                    evidence: FailureEnvelope | None = turn_result.recovery_input
                    failures.append(
                        BranchFailure(
                            action_id=action.action_id,
                            failure_kind="turn_failure",
                            failure_code=turn_result.outcome_kind,
                            evidence=evidence,
                        )
                    )
                    if self._observability_hook is not None:
                        with contextlib.suppress(Exception):
                            self._observability_hook.on_parallel_branch_result(
                                run_id=run_id,
                                group_idempotency_key=group.group_idempotency_key,
                                action_id=action.action_id,
                                outcome="failed",
                                failure_code=turn_result.outcome_kind,
                            )

        join_satisfied = _evaluate_join(group, successes, failures)

        # R6b: when join fails, signal rollback intent for each succeeded branch.
        if not join_satisfied and successes and self._observability_hook is not None:
            for branch in successes:
                with contextlib.suppress(Exception):
                    self._observability_hook.on_branch_rollback_triggered(
                        run_id=run_id,
                        group_idempotency_key=group.group_idempotency_key,
                        action_id=branch.action_id,
                        join_strategy=group.join_strategy,
                    )

        return ParallelJoinResult(
            group_idempotency_key=group.group_idempotency_key,
            successes=successes,
            failures=failures,
            join_satisfied=join_satisfied,
        )

    async def _execute_dag(self, plan: DependencyGraph, run_id: str) -> PlanResult:
        """Execute a DependencyGraph using topological ordering.

        Nodes with no unmet dependencies execute concurrently within each
        topological layer via ``asyncio.TaskGroup``. Layers are processed
        sequentially so that dependency ordering is respected.

        Args:
            plan: DAG plan with nodes and dependency edges.
            run_id: Kernel run identifier.

        Returns:
            PlanResult with ``plan_kind="dependency_graph"``.

        Raises:
            UnsupportedPlanTypeError: When the graph contains a cycle.

        """
        if not plan.nodes:
            return PlanResult(
                plan_kind="dependency_graph",
                total_actions=0,
                succeeded=0,
                failed=0,
            )

        dep_map: dict[str, set[str]] = {node.node_id: set(node.depends_on) for node in plan.nodes}
        node_by_id = {node.node_id: node for node in plan.nodes}

        ts: graphlib.TopologicalSorter[str] = graphlib.TopologicalSorter(dep_map)
        try:
            ts.prepare()
        except graphlib.CycleError as exc:
            raise UnsupportedPlanTypeError(plan) from exc

        succeeded = 0
        failed = 0

        while ts.is_active():
            ready = list(ts.get_ready())
            layer_results: list[TurnResult | BaseException] = []

            async def _run_node(
                node_id: str,
                _results: list[TurnResult | BaseException] = layer_results,
            ) -> None:
                try:
                    result = await self._turn_runner(node_by_id[node_id].action)
                    _results.append(result)
                except Exception as exc:
                    _results.append(exc)

            async with asyncio.TaskGroup() as tg:
                for nid in ready:
                    tg.create_task(_run_node(nid))

            for raw in layer_results:
                if isinstance(raw, BaseException):
                    failed += 1
                elif raw.outcome_kind in ("dispatched", "noop"):
                    succeeded += 1
                else:
                    failed += 1

            for nid in ready:
                ts.done(nid)

        return PlanResult(
            plan_kind="dependency_graph",
            total_actions=len(plan.nodes),
            succeeded=succeeded,
            failed=failed,
        )

    async def _execute_conditional(self, plan: ConditionalPlan, run_id: str) -> PlanResult:
        """Execute a ConditionalPlan by evaluating the gating action first.

        The gating action is run via ``turn_runner``. Its ``outcome_kind`` is
        matched against each branch's ``trigger_outcomes`` (left to right). The
        first matching branch's sub-plan is executed recursively. When no branch
        matches and ``default_plan`` is set, the default is executed. When no
        branch matches and there is no default, only the gating action is counted.

        Args:
            plan: Conditional plan with gating action, branches, and optional default.
            run_id: Kernel run identifier.

        Returns:
            PlanResult with ``plan_kind="conditional"``.

        """
        try:
            gate_result = await self._turn_runner(plan.gating_action)
            gate_succeeded = gate_result.outcome_kind in ("dispatched", "noop")
            outcome_kind = gate_result.outcome_kind
        except Exception:
            gate_succeeded = False
            outcome_kind = "exception"

        gate_success_count = 1 if gate_succeeded else 0
        gate_fail_count = 0 if gate_succeeded else 1

        matched_plan: Any = None
        for branch in plan.branches:
            if outcome_kind in branch.trigger_outcomes:
                matched_plan = branch.plan
                break

        if matched_plan is None and plan.default_plan is not None:
            matched_plan = plan.default_plan

        if matched_plan is None:
            return PlanResult(
                plan_kind="conditional",
                total_actions=1,
                succeeded=gate_success_count,
                failed=gate_fail_count,
            )

        sub_result = await self.execute_plan(matched_plan, run_id)
        return PlanResult(
            plan_kind="conditional",
            total_actions=1 + sub_result.total_actions,
            succeeded=gate_success_count + sub_result.succeeded,
            failed=gate_fail_count + sub_result.failed,
        )

    async def _execute_speculative(self, plan: SpeculativePlan, run_id: str) -> PlanResult:
        """Execute all speculative candidates concurrently via asyncio.

        All candidate plans launch simultaneously. An optional
        ``speculation_timeout_ms`` caps the total wait time. Partial results
        from timed-out candidates are counted as failures. The "commit winner"
        step is handled externally via the ``commit_speculation`` facade signal;
        this method provides the local asyncio-based execution path.

        Known limitation: candidates run in-process without cross-process
        isolation. Full Child Workflow isolation requires Temporal integration
        beyond the current PoC scope.

        Args:
            plan: Speculative plan with candidate sub-plans and optional timeout.
            run_id: Kernel run identifier.

        Returns:
            PlanResult with ``plan_kind="speculative"`` aggregating all
            candidates' success and failure counts.

        """
        if not plan.candidates:
            return PlanResult(
                plan_kind="speculative",
                total_actions=0,
                succeeded=0,
                failed=0,
            )

        # Create per-candidate Tasks so commit_speculation() can cancel
        # non-winners by candidate_id before their side-effects are applied.
        tasks: dict[str, asyncio.Task[PlanResult]] = {
            c.candidate_id: asyncio.create_task(
                self.execute_plan(c.plan, run_id), name=f"speculative:{c.candidate_id}"
            )
            for c in plan.candidates
        }
        self._speculative_task_stack.append(tasks)
        self._active_speculative_tasks = tasks

        try:
            task_list = list(tasks.values())
            if plan.speculation_timeout_ms is not None:
                timeout_s = plan.speculation_timeout_ms / 1000.0
                raw_results: list[PlanResult | BaseException] = await asyncio.gather(
                    *[asyncio.wait_for(t, timeout=timeout_s) for t in task_list],
                    return_exceptions=True,
                )
            else:
                raw_results = await asyncio.gather(*task_list, return_exceptions=True)
        finally:
            self._speculative_task_stack.pop()
            self._active_speculative_tasks = (
                self._speculative_task_stack[-1] if self._speculative_task_stack else {}
            )

        total_actions = 0
        succeeded = 0
        failed = 0

        for raw in raw_results:
            if isinstance(raw, BaseException):
                failed += 1
                total_actions += 1
            else:
                result: PlanResult = raw
                total_actions += result.total_actions
                succeeded += result.succeeded
                failed += result.failed

        # Capture the committed winner before clearing so callers can record
        # the decision in the event log via PlanResult.committed_winner_id.
        winner = self._committed_winner_id
        self._committed_winner_id = None
        return PlanResult(
            plan_kind="speculative",
            total_actions=total_actions,
            succeeded=succeeded,
            failed=failed,
            committed_winner_id=winner,
        )


def _evaluate_join(
    group: ParallelGroup,
    successes: list[BranchResult],
    failures: list[BranchFailure],
) -> bool:
    """Evaluate the join strategy for a completed group.

    Args:
        group: The parallel group whose strategy is evaluated.
        successes: Successfully completed branch results.
        failures: Failed branch results.

    Returns:
        True when the join condition is satisfied.

    """
    strategy = group.join_strategy
    if strategy == "all":
        return len(failures) == 0
    if strategy == "any":
        return len(successes) >= 1
    if strategy == "n_of_m":
        required = group.n if group.n is not None else len(group.actions)
        return len(successes) >= required
    return False
