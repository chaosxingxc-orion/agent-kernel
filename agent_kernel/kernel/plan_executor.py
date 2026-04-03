"""PlanExecutor for sequential and parallel execution of ExecutionPlans.

Implements Phase 3 (Parallel Execution) of the agent-kernel architecture.
This module orchestrates SequentialPlan and ParallelPlan execution using
asyncio concurrency primitives.

Design boundaries:
  - PlanExecutor does NOT manage event logs or deduplication — those are
    handled by the TurnEngine for each individual Action.
  - PlanExecutor accepts a generic ``turn_runner`` callable so it never
    imports TurnEngine directly (dependency-inversion for testability).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, Literal

from agent_kernel.kernel.branch_monitor import BranchMonitor
from agent_kernel.kernel.contracts import (
    Action,
    BranchFailure,
    BranchResult,
    ExecutionPlan,
    FailureEnvelope,
    ObservabilityHook,
    ParallelGroup,
    ParallelJoinResult,
    ParallelPlan,
    SequentialPlan,
)
from agent_kernel.kernel.dedupe_store import DedupeStorePort
from agent_kernel.kernel.turn_engine import TurnResult


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

    plan_kind: Literal["sequential", "parallel"]
    total_actions: int
    succeeded: int
    failed: int
    join_results: list[ParallelJoinResult] = field(default_factory=list)

    @property
    def all_succeeded(self) -> bool:
        """Returns True when every submitted action succeeded."""
        return self.succeeded == self.total_actions


# Type alias for the async callable accepted by PlanExecutor.
TurnRunner = Callable[[Action], Coroutine[Any, Any, TurnResult]]


class PlanExecutor:
    """Executes SequentialPlan or ParallelPlan using a kernel turn runner.

    For SequentialPlan: delegates to ``turn_runner`` for each step in order.
    For ParallelPlan: uses ``asyncio.gather`` for concurrent group execution.

    This class does NOT manage event log or deduplication — those are
    handled by the TurnEngine for each individual Action.
    """

    def __init__(
        self,
        turn_runner: TurnRunner,
        branch_monitor: BranchMonitor | None = None,
        observability_hook: ObservabilityHook | None = None,
        dedupe_store: DedupeStorePort | None = None,
    ) -> None:
        """Initialises PlanExecutor with an async turn-runner callable.

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

    async def execute_plan(self, plan: ExecutionPlan, run_id: str) -> PlanResult:
        """Dispatches plan execution to the correct strategy.

        Args:
            plan: Either a SequentialPlan or ParallelPlan.
            run_id: Kernel run identifier for contextual logging.

        Returns:
            PlanResult summarising overall success/failure counts.
        """
        if isinstance(plan, SequentialPlan):
            return await self._execute_sequential(plan, run_id)
        return await self._execute_parallel(plan, run_id)

    async def _execute_sequential(
        self,
        plan: SequentialPlan,
        run_id: str,
    ) -> PlanResult:
        """Executes each step in order, accumulating success/failure counts.

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
        """Executes groups sequentially, actions within each group concurrently.

        Groups in a ParallelPlan are ordered — each group executes after the
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
            tasks = [
                asyncio.create_task(_monitored_run(action))
                for action in group.actions
            ]
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
                    raw_results.append(
                        TimeoutError(f"Branch timeout after {group.timeout_ms}ms")
                    )
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
                    # blocked or recovery_pending → BranchFailure
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


def _evaluate_join(
    group: ParallelGroup,
    successes: list[BranchResult],
    failures: list[BranchFailure],
) -> bool:
    """Evaluates the join strategy for a completed group.

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
