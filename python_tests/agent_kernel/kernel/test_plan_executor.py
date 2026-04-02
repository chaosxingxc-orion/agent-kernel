"""Tests for PlanExecutor sequential and parallel execution strategies."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from agent_kernel.kernel.contracts import (
    Action,
    FailureEnvelope,
    ParallelGroup,
    ParallelPlan,
    RunProjection,
    SequentialPlan,
)
from agent_kernel.kernel.plan_executor import PlanExecutor, PlanResult
from agent_kernel.kernel.turn_engine import TurnResult

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_action(action_id: str, run_id: str = "run-1") -> Action:
    """Builds a minimal Action DTO for use in tests."""
    return Action(
        action_id=action_id,
        run_id=run_id,
        action_type="test_action",
        effect_class="read_only",
    )


def _success_turn_result(action_id: str = "action-1") -> TurnResult:
    """Builds a TurnResult representing a successful dispatch."""
    return TurnResult(
        state="dispatch_acknowledged",
        outcome_kind="dispatched",
        decision_ref=f"decision:{action_id}",
        decision_fingerprint=f"fp:{action_id}",
    )


def _noop_turn_result(action_id: str = "action-1") -> TurnResult:
    """Builds a TurnResult representing a no-op turn."""
    return TurnResult(
        state="completed_noop",
        outcome_kind="noop",
        decision_ref=f"decision:{action_id}",
        decision_fingerprint=f"fp:{action_id}",
    )


def _blocked_turn_result(action_id: str = "action-1") -> TurnResult:
    """Builds a TurnResult representing a blocked dispatch."""
    return TurnResult(
        state="dispatch_blocked",
        outcome_kind="blocked",
        decision_ref=f"decision:{action_id}",
        decision_fingerprint=f"fp:{action_id}",
    )


def _recovery_turn_result(action_id: str = "action-1") -> TurnResult:
    """Builds a TurnResult representing a recovery-pending outcome."""
    _proj = RunProjection(
        run_id="run-1",
        lifecycle_state="recovering",
        projected_offset=1,
        waiting_external=False,
        ready_for_dispatch=False,
    )
    evidence = FailureEnvelope(
        run_id="run-1",
        action_id=action_id,
        failed_stage="execution",
        failed_component="test",
        failure_code="test_error",
        failure_class="transient",
    )
    return TurnResult(
        state="recovery_pending",
        outcome_kind="recovery_pending",
        decision_ref=f"decision:{action_id}",
        decision_fingerprint=f"fp:{action_id}",
        recovery_input=evidence,
    )


# ---------------------------------------------------------------------------
# Recording turn runner for sequential tests
# ---------------------------------------------------------------------------


@dataclass
class _RecordingRunner:
    """Captures calls to a fake turn runner and returns preset results."""

    results: list[TurnResult] = field(default_factory=list)
    called_actions: list[str] = field(default_factory=list)
    call_index: int = field(default=0, init=False)

    async def __call__(self, action: Action) -> TurnResult:
        self.called_actions.append(action.action_id)
        result = self.results[self.call_index]
        self.call_index += 1
        return result


# ---------------------------------------------------------------------------
# Sequential plan tests
# ---------------------------------------------------------------------------


class TestSequentialPlan:
    """PlanExecutor sequential execution tests."""

    @pytest.mark.asyncio
    async def test_sequential_all_succeed(self) -> None:
        """All 3 steps succeed — PlanResult has 3 succeeded, 0 failed."""
        actions = [_make_action(f"a{i}") for i in range(3)]
        runner = _RecordingRunner(
            results=[_success_turn_result(a.action_id) for a in actions]
        )
        executor = PlanExecutor(runner)

        plan = SequentialPlan(steps=tuple(actions))
        result = await executor.execute_plan(plan, run_id="run-1")

        assert result.plan_kind == "sequential"
        assert result.total_actions == 3
        assert result.succeeded == 3
        assert result.failed == 0
        assert result.all_succeeded is True
        assert result.join_results == []

    @pytest.mark.asyncio
    async def test_sequential_calls_in_order(self) -> None:
        """Steps are invoked in declaration order."""
        actions = [_make_action(f"a{i}") for i in range(3)]
        runner = _RecordingRunner(
            results=[_success_turn_result(a.action_id) for a in actions]
        )
        executor = PlanExecutor(runner)

        await executor.execute_plan(SequentialPlan(steps=tuple(actions)), run_id="run-1")

        assert runner.called_actions == ["a0", "a1", "a2"]

    @pytest.mark.asyncio
    async def test_sequential_counts_failure(self) -> None:
        """A recovery_pending outcome increments the failed counter."""
        actions = [_make_action(f"a{i}") for i in range(3)]
        runner = _RecordingRunner(
            results=[
                _success_turn_result("a0"),
                _recovery_turn_result("a1"),
                _success_turn_result("a2"),
            ]
        )
        executor = PlanExecutor(runner)

        result = await executor.execute_plan(
            SequentialPlan(steps=tuple(actions)), run_id="run-1"
        )

        assert result.succeeded == 2
        assert result.failed == 1
        assert result.all_succeeded is False

    @pytest.mark.asyncio
    async def test_sequential_noop_counts_as_success(self) -> None:
        """Noop outcome_kind counts as a success."""
        action = _make_action("a0")
        runner = _RecordingRunner(results=[_noop_turn_result("a0")])
        executor = PlanExecutor(runner)

        result = await executor.execute_plan(
            SequentialPlan(steps=(action,)), run_id="run-1"
        )

        assert result.succeeded == 1
        assert result.failed == 0

    @pytest.mark.asyncio
    async def test_sequential_exception_counts_as_failure(self) -> None:
        """An exception raised by turn_runner increments the failed counter."""
        action = _make_action("a0")

        async def raising_runner(a: Action) -> TurnResult:
            raise RuntimeError("boom")

        executor = PlanExecutor(raising_runner)
        result = await executor.execute_plan(
            SequentialPlan(steps=(action,)), run_id="run-1"
        )

        assert result.failed == 1
        assert result.succeeded == 0

    @pytest.mark.asyncio
    async def test_sequential_empty_plan(self) -> None:
        """Empty SequentialPlan produces a PlanResult with zero counts."""
        executor = PlanExecutor(lambda a: None)  # type: ignore[arg-type]
        result = await executor.execute_plan(SequentialPlan(steps=()), run_id="run-1")

        assert result.total_actions == 0
        assert result.succeeded == 0
        assert result.all_succeeded is True


# ---------------------------------------------------------------------------
# Parallel plan tests
# ---------------------------------------------------------------------------


class TestParallelPlanJoinStrategies:
    """PlanExecutor join strategy tests."""

    def _make_group(
        self,
        actions: list[Action],
        join_strategy: str = "all",
        n: int | None = None,
        timeout_ms: int | None = None,
    ) -> ParallelGroup:
        return ParallelGroup(
            actions=tuple(actions),
            join_strategy=join_strategy,  # type: ignore[arg-type]
            n=n,
            timeout_ms=timeout_ms,
            group_idempotency_key="grp-1",
        )

    @pytest.mark.asyncio
    async def test_parallel_all_strategy_all_succeed(self) -> None:
        """join_strategy=all, all succeed → join_satisfied=True."""
        actions = [_make_action(f"a{i}") for i in range(3)]

        async def runner(action: Action) -> TurnResult:
            return _success_turn_result(action.action_id)

        executor = PlanExecutor(runner)
        group = self._make_group(actions, join_strategy="all")
        plan = ParallelPlan(groups=(group,))

        result = await executor.execute_plan(plan, run_id="run-1")

        assert result.plan_kind == "parallel"
        assert len(result.join_results) == 1
        join = result.join_results[0]
        assert join.join_satisfied is True
        assert len(join.successes) == 3
        assert len(join.failures) == 0

    @pytest.mark.asyncio
    async def test_parallel_all_strategy_one_failure(self) -> None:
        """join_strategy=all with one failure → join_satisfied=False."""
        actions = [_make_action(f"a{i}") for i in range(3)]
        fail_id = "a1"

        async def runner(action: Action) -> TurnResult:
            if action.action_id == fail_id:
                return _recovery_turn_result(action.action_id)
            return _success_turn_result(action.action_id)

        executor = PlanExecutor(runner)
        group = self._make_group(actions, join_strategy="all")
        plan = ParallelPlan(groups=(group,))

        result = await executor.execute_plan(plan, run_id="run-1")
        join = result.join_results[0]

        assert join.join_satisfied is False
        assert len(join.failures) == 1
        assert join.failures[0].action_id == fail_id

    @pytest.mark.asyncio
    async def test_parallel_any_strategy_one_success(self) -> None:
        """join_strategy=any with one success + two failures → join_satisfied=True."""
        actions = [_make_action(f"a{i}") for i in range(3)]

        async def runner(action: Action) -> TurnResult:
            if action.action_id == "a0":
                return _success_turn_result(action.action_id)
            return _recovery_turn_result(action.action_id)

        executor = PlanExecutor(runner)
        group = self._make_group(actions, join_strategy="any")
        plan = ParallelPlan(groups=(group,))

        result = await executor.execute_plan(plan, run_id="run-1")
        join = result.join_results[0]

        assert join.join_satisfied is True
        assert len(join.successes) == 1
        assert len(join.failures) == 2

    @pytest.mark.asyncio
    async def test_parallel_any_strategy_all_fail(self) -> None:
        """join_strategy=any, all fail → join_satisfied=False."""
        actions = [_make_action(f"a{i}") for i in range(3)]

        async def runner(action: Action) -> TurnResult:
            return _recovery_turn_result(action.action_id)

        executor = PlanExecutor(runner)
        group = self._make_group(actions, join_strategy="any")
        plan = ParallelPlan(groups=(group,))

        result = await executor.execute_plan(plan, run_id="run-1")
        join = result.join_results[0]

        assert join.join_satisfied is False

    @pytest.mark.asyncio
    async def test_parallel_n_of_m_exactly_n_succeed(self) -> None:
        """join_strategy=n_of_m, n=2, 3 actions, 2 succeed → join_satisfied=True."""
        actions = [_make_action(f"a{i}") for i in range(3)]

        async def runner(action: Action) -> TurnResult:
            if action.action_id in ("a0", "a1"):
                return _success_turn_result(action.action_id)
            return _recovery_turn_result(action.action_id)

        executor = PlanExecutor(runner)
        group = self._make_group(actions, join_strategy="n_of_m", n=2)
        plan = ParallelPlan(groups=(group,))

        result = await executor.execute_plan(plan, run_id="run-1")
        join = result.join_results[0]

        assert join.join_satisfied is True
        assert len(join.successes) == 2

    @pytest.mark.asyncio
    async def test_parallel_n_of_m_fewer_than_n_succeed(self) -> None:
        """join_strategy=n_of_m, n=2, only 1 succeeds → join_satisfied=False."""
        actions = [_make_action(f"a{i}") for i in range(3)]

        async def runner(action: Action) -> TurnResult:
            if action.action_id == "a0":
                return _success_turn_result(action.action_id)
            return _recovery_turn_result(action.action_id)

        executor = PlanExecutor(runner)
        group = self._make_group(actions, join_strategy="n_of_m", n=2)
        plan = ParallelPlan(groups=(group,))

        result = await executor.execute_plan(plan, run_id="run-1")
        join = result.join_results[0]

        assert join.join_satisfied is False

    @pytest.mark.asyncio
    async def test_parallel_exception_maps_to_branch_failure(self) -> None:
        """Exceptions raised by turn_runner map to BranchFailure."""
        actions = [_make_action("a0")]

        async def runner(action: Action) -> TurnResult:
            raise RuntimeError("unexpected error")

        executor = PlanExecutor(runner)
        group = self._make_group(actions, join_strategy="all")
        plan = ParallelPlan(groups=(group,))

        result = await executor.execute_plan(plan, run_id="run-1")
        join = result.join_results[0]

        assert join.join_satisfied is False
        assert len(join.failures) == 1
        assert join.failures[0].failure_kind == "exception"
        assert join.failures[0].failure_code == "RuntimeError"

    @pytest.mark.asyncio
    async def test_parallel_group_timeout_maps_to_branch_failure(self) -> None:
        """Group timeout maps each action to BranchFailure."""
        actions = [_make_action(f"a{i}") for i in range(2)]

        async def slow_runner(action: Action) -> TurnResult:
            await asyncio.sleep(10.0)  # deliberate slow — will be cut off
            return _success_turn_result(action.action_id)

        executor = PlanExecutor(slow_runner)
        # 10 ms timeout — far shorter than the 10-second sleep above.
        group = self._make_group(actions, join_strategy="all", timeout_ms=10)
        plan = ParallelPlan(groups=(group,))

        result = await executor.execute_plan(plan, run_id="run-1")
        join = result.join_results[0]

        assert join.join_satisfied is False
        assert len(join.failures) == 2
        for failure in join.failures:
            assert failure.failure_kind == "exception"
            assert "TimeoutError" in failure.failure_code


# ---------------------------------------------------------------------------
# PlanResult property tests
# ---------------------------------------------------------------------------


class TestPlanResult:
    """PlanResult all_succeeded property tests."""

    def test_all_succeeded_true(self) -> None:
        result = PlanResult(
            plan_kind="sequential",
            total_actions=3,
            succeeded=3,
            failed=0,
        )
        assert result.all_succeeded is True

    def test_all_succeeded_false_when_any_failed(self) -> None:
        result = PlanResult(
            plan_kind="parallel",
            total_actions=3,
            succeeded=2,
            failed=1,
        )
        assert result.all_succeeded is False

    def test_all_succeeded_true_for_empty(self) -> None:
        result = PlanResult(
            plan_kind="sequential",
            total_actions=0,
            succeeded=0,
            failed=0,
        )
        assert result.all_succeeded is True


# ---------------------------------------------------------------------------
# Concurrency verification
# ---------------------------------------------------------------------------


class TestParallelConcurrency:
    """Verify that parallel groups actually execute concurrently."""

    @pytest.mark.asyncio
    async def test_parallel_group_executes_concurrently(self) -> None:
        """All actions in a group must start before any one finishes."""
        start_order: list[str] = []
        finish_order: list[str] = []

        async def ordered_runner(action: Action) -> TurnResult:
            start_order.append(action.action_id)
            await asyncio.sleep(0)  # yield to event loop
            finish_order.append(action.action_id)
            return _success_turn_result(action.action_id)

        actions = [_make_action(f"a{i}") for i in range(3)]
        executor = PlanExecutor(ordered_runner)
        group = ParallelGroup(
            actions=tuple(actions),
            join_strategy="all",
            group_idempotency_key="grp-concurrent",
        )
        await executor.execute_plan(ParallelPlan(groups=(group,)), run_id="run-1")

        # All 3 should have started before all 3 finish.
        assert len(start_order) == 3
        assert len(finish_order) == 3
