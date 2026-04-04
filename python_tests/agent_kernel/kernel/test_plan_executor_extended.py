"""Tests for PlanExecutor extended plan types: ConditionalPlan, DependencyGraph, SpeculativePlan."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from agent_kernel.kernel.contracts import (
    Action,
    ConditionalBranch,
    ConditionalPlan,
    DependencyGraph,
    DependencyNode,
    SequentialPlan,
    SpeculativeCandidate,
    SpeculativePlan,
)
from agent_kernel.kernel.plan_executor import PlanExecutor, UnsupportedPlanTypeError
from agent_kernel.kernel.turn_engine import TurnResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_action(action_id: str, run_id: str = "run-1") -> Action:
    """Builds a minimal Action DTO for use in tests."""
    return Action(
        action_id=action_id,
        run_id=run_id,
        action_type="test_action",
        effect_class="read_only",
    )


def _dispatched_result() -> TurnResult:
    """Returns a TurnResult representing a successful dispatch."""
    return TurnResult(
        state="dispatch_acknowledged",
        outcome_kind="dispatched",
        decision_ref="decision:test",
        decision_fingerprint="fp:test",
    )


def _blocked_result() -> TurnResult:
    """Returns a TurnResult representing a blocked outcome."""
    return TurnResult(
        state="dispatch_blocked",
        outcome_kind="blocked",
        decision_ref="decision:test",
        decision_fingerprint="fp:test",
    )


def _make_executor(turn_runner: AsyncMock) -> PlanExecutor:
    """Wraps an AsyncMock turn_runner in a PlanExecutor."""
    return PlanExecutor(turn_runner=turn_runner)


# ---------------------------------------------------------------------------
# P1: UnsupportedPlanTypeError
# ---------------------------------------------------------------------------


class TestUnsupportedPlanType:
    """P1 — explicit isinstance guard raises UnsupportedPlanTypeError."""

    @pytest.mark.asyncio
    async def test_unsupported_plan_type_raises(self) -> None:
        """A plain object that is not a known plan type raises UnsupportedPlanTypeError."""
        runner = AsyncMock(return_value=_dispatched_result())
        executor = _make_executor(runner)

        class _FakePlan:
            pass

        with pytest.raises(UnsupportedPlanTypeError):
            await executor.execute_plan(_FakePlan(), run_id="run-1")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# P2: DependencyGraph
# ---------------------------------------------------------------------------


class TestDependencyGraph:
    """P2 — DAG execution via graphlib.TopologicalSorter."""

    @pytest.mark.asyncio
    async def test_dag_linear_chain_executes_in_order(self) -> None:
        """a→b→c: all three nodes execute and total_actions=3."""
        executed: list[str] = []

        async def runner(action: Action) -> TurnResult:
            executed.append(action.action_id)
            return _dispatched_result()

        executor = PlanExecutor(turn_runner=runner)
        plan = DependencyGraph(
            nodes=(
                DependencyNode(node_id="a", action=_make_action("a"), depends_on=()),
                DependencyNode(node_id="b", action=_make_action("b"), depends_on=("a",)),
                DependencyNode(node_id="c", action=_make_action("c"), depends_on=("b",)),
            )
        )
        result = await executor.execute_plan(plan, run_id="run-1")

        assert set(executed) == {"a", "b", "c"}
        assert result.total_actions == 3
        assert result.succeeded == 3
        assert result.failed == 0

    @pytest.mark.asyncio
    async def test_dag_fan_out_fan_in(self) -> None:
        """root→{left,right}→merge: all four nodes run."""
        runner = AsyncMock(return_value=_dispatched_result())
        executor = _make_executor(runner)
        plan = DependencyGraph(
            nodes=(
                DependencyNode(node_id="root", action=_make_action("root"), depends_on=()),
                DependencyNode(node_id="left", action=_make_action("left"), depends_on=("root",)),
                DependencyNode(node_id="right", action=_make_action("right"), depends_on=("root",)),
                DependencyNode(
                    node_id="merge",
                    action=_make_action("merge"),
                    depends_on=("left", "right"),
                ),
            )
        )
        result = await executor.execute_plan(plan, run_id="run-1")

        assert result.total_actions == 4
        assert result.succeeded == 4
        assert result.failed == 0

    @pytest.mark.asyncio
    async def test_dag_empty_graph(self) -> None:
        """Empty DependencyGraph produces total_actions=0."""
        runner = AsyncMock(return_value=_dispatched_result())
        executor = _make_executor(runner)
        plan = DependencyGraph(nodes=())

        result = await executor.execute_plan(plan, run_id="run-1")

        assert result.total_actions == 0
        assert result.succeeded == 0
        assert result.failed == 0
        runner.assert_not_called()

    @pytest.mark.asyncio
    async def test_dag_cycle_raises(self) -> None:
        """A graph with a cycle raises UnsupportedPlanTypeError."""
        runner = AsyncMock(return_value=_dispatched_result())
        executor = _make_executor(runner)
        # a→b, b→a forms a cycle
        plan = DependencyGraph(
            nodes=(
                DependencyNode(node_id="a", action=_make_action("a"), depends_on=("b",)),
                DependencyNode(node_id="b", action=_make_action("b"), depends_on=("a",)),
            )
        )
        with pytest.raises(UnsupportedPlanTypeError):
            await executor.execute_plan(plan, run_id="run-1")

    @pytest.mark.asyncio
    async def test_dag_plan_kind_is_dependency_graph(self) -> None:
        """plan_kind discriminator is 'dependency_graph'."""
        runner = AsyncMock(return_value=_dispatched_result())
        executor = _make_executor(runner)
        plan = DependencyGraph(
            nodes=(DependencyNode(node_id="x", action=_make_action("x"), depends_on=()),)
        )
        result = await executor.execute_plan(plan, run_id="run-1")
        assert result.plan_kind == "dependency_graph"


# ---------------------------------------------------------------------------
# P3: ConditionalPlan
# ---------------------------------------------------------------------------


class TestConditionalPlan:
    """P3 — ConditionalPlan gating-action branching."""

    @pytest.mark.asyncio
    async def test_conditional_routes_to_matching_branch(self) -> None:
        """Gating action returns 'dispatched'; matching branch executes its sub-plan."""
        call_log: list[str] = []

        async def runner(action: Action) -> TurnResult:
            call_log.append(action.action_id)
            return _dispatched_result()

        executor = PlanExecutor(turn_runner=runner)
        branch_plan = SequentialPlan(steps=(_make_action("branch-step"),))
        plan = ConditionalPlan(
            gating_action=_make_action("gate"),
            branches=(ConditionalBranch(trigger_outcomes=("dispatched",), plan=branch_plan),),
        )
        result = await executor.execute_plan(plan, run_id="run-1")

        assert "gate" in call_log
        assert "branch-step" in call_log
        assert result.plan_kind == "conditional"
        assert result.total_actions == 2
        assert result.succeeded == 2
        assert result.failed == 0

    @pytest.mark.asyncio
    async def test_conditional_uses_default_plan_when_no_match(self) -> None:
        """When no branch trigger matches, the default_plan executes."""
        # Gate returns blocked (no branch matches); default-step returns dispatched.
        call_count = 0

        async def runner(action: Action) -> TurnResult:
            nonlocal call_count
            call_count += 1
            if action.action_id == "gate":
                return _blocked_result()
            return _dispatched_result()

        executor = PlanExecutor(turn_runner=runner)
        default_plan = SequentialPlan(steps=(_make_action("default-step"),))
        plan = ConditionalPlan(
            gating_action=_make_action("gate"),
            branches=(
                ConditionalBranch(trigger_outcomes=("dispatched",), plan=SequentialPlan(steps=())),
            ),
            default_plan=default_plan,
        )
        result = await executor.execute_plan(plan, run_id="run-1")

        assert call_count == 2  # gate + default-step
        assert result.plan_kind == "conditional"
        assert result.total_actions == 2
        assert result.failed == 1  # gate was blocked
        assert result.succeeded == 1  # default step dispatched

    @pytest.mark.asyncio
    async def test_conditional_no_match_no_default_returns_gating_only(self) -> None:
        """No matching branch and no default: only the gating action is counted."""
        runner = AsyncMock(return_value=_dispatched_result())
        executor = _make_executor(runner)
        plan = ConditionalPlan(
            gating_action=_make_action("gate"),
            branches=(
                ConditionalBranch(trigger_outcomes=("blocked",), plan=SequentialPlan(steps=())),
            ),
            default_plan=None,
        )
        result = await executor.execute_plan(plan, run_id="run-1")

        assert runner.call_count == 1  # only the gate
        assert result.total_actions == 1
        assert result.succeeded == 1
        assert result.failed == 0

    @pytest.mark.asyncio
    async def test_conditional_plan_kind(self) -> None:
        """plan_kind discriminator is 'conditional'."""
        runner = AsyncMock(return_value=_dispatched_result())
        executor = _make_executor(runner)
        plan = ConditionalPlan(
            gating_action=_make_action("gate"),
            branches=(),
        )
        result = await executor.execute_plan(plan, run_id="run-1")
        assert result.plan_kind == "conditional"


# ---------------------------------------------------------------------------
# P4: SpeculativePlan
# ---------------------------------------------------------------------------


class TestSpeculativePlan:
    """P4 — SpeculativePlan runs all candidates concurrently."""

    @pytest.mark.asyncio
    async def test_speculative_runs_all_candidates(self) -> None:
        """All candidate sub-plans execute and results are aggregated."""
        runner = AsyncMock(return_value=_dispatched_result())
        executor = _make_executor(runner)
        plan = SpeculativePlan(
            candidates=(
                SpeculativeCandidate(
                    candidate_id="c1",
                    plan=SequentialPlan(steps=(_make_action("c1-a1"), _make_action("c1-a2"))),
                ),
                SpeculativeCandidate(
                    candidate_id="c2",
                    plan=SequentialPlan(steps=(_make_action("c2-a1"),)),
                ),
            )
        )
        result = await executor.execute_plan(plan, run_id="run-1")

        assert result.plan_kind == "speculative"
        assert result.total_actions == 3  # 2 + 1
        assert result.succeeded == 3
        assert result.failed == 0

    @pytest.mark.asyncio
    async def test_speculative_with_timeout(self) -> None:
        """When speculation_timeout_ms is set, slow candidates count as failures."""

        async def slow_runner(action: Action) -> TurnResult:
            await asyncio.sleep(10)  # will be cancelled by timeout
            return _dispatched_result()

        executor = PlanExecutor(turn_runner=slow_runner)
        plan = SpeculativePlan(
            candidates=(
                SpeculativeCandidate(
                    candidate_id="slow",
                    plan=SequentialPlan(steps=(_make_action("slow-a"),)),
                ),
            ),
            speculation_timeout_ms=10,  # 10 ms — very short
        )
        result = await executor.execute_plan(plan, run_id="run-1")

        assert result.plan_kind == "speculative"
        # The timed-out candidate counts as 1 failure
        assert result.failed >= 1

    @pytest.mark.asyncio
    async def test_speculative_plan_kind(self) -> None:
        """plan_kind discriminator is 'speculative'."""
        runner = AsyncMock(return_value=_dispatched_result())
        executor = _make_executor(runner)
        plan = SpeculativePlan(
            candidates=(
                SpeculativeCandidate(
                    candidate_id="c1",
                    plan=SequentialPlan(steps=(_make_action("a1"),)),
                ),
            )
        )
        result = await executor.execute_plan(plan, run_id="run-1")
        assert result.plan_kind == "speculative"
