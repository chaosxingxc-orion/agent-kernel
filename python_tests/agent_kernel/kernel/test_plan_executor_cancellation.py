"""Tests for PlanExecutor cancellation policy behavior."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_kernel.kernel.plan_executor import PlanExecutor
from agent_kernel.kernel.turn_engine import TurnResult


def _succeeded_turn_result(effect_class: str = "compensatable_write") -> TurnResult:
    return TurnResult(
        state="dispatch_acknowledged",
        outcome_kind="dispatched",
        decision_ref="decision:test",
        decision_fingerprint="fp:test",
        action_commit={
            "action_id": "action-1",
            "run_id": "run-1",
            "effect_class": effect_class,
        },
    )


class TestCancellationPolicyAbandon:
    """`abandon` should cancel pending tasks and skip compensation."""

    @pytest.mark.asyncio
    async def test_abandon_cancels_pending_task_without_compensation(self) -> None:
        runner = AsyncMock()
        comp_registry = MagicMock()
        executor = PlanExecutor(turn_runner=runner, compensation_registry=comp_registry)

        task = asyncio.create_task(asyncio.sleep(5))
        await executor._cancel_with_policy(
            tasks_to_cancel={"loser": task},
            succeeded_results=[],
            cancellation_policy="abandon",
            run_id="run-1",
        )
        await asyncio.sleep(0)
        assert task.cancelled()
        comp_registry.execute.assert_not_called()


class TestCancellationPolicyCompensateThenContinue:
    """`compensate_then_continue` should execute compensation when possible."""

    @pytest.mark.asyncio
    async def test_compensate_then_continue_calls_registry_for_succeeded_result(self) -> None:
        runner = AsyncMock()
        comp_registry = MagicMock()
        comp_registry.has_handler.return_value = True
        comp_registry.execute = AsyncMock(return_value=True)
        executor = PlanExecutor(turn_runner=runner, compensation_registry=comp_registry)

        await executor._cancel_with_policy(
            tasks_to_cancel={},
            succeeded_results=[_succeeded_turn_result()],
            cancellation_policy="compensate_then_continue",
            run_id="run-1",
        )
        comp_registry.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_compensate_then_continue_without_registry_does_not_raise(self) -> None:
        runner = AsyncMock()
        executor = PlanExecutor(turn_runner=runner, compensation_registry=None)

        await executor._cancel_with_policy(
            tasks_to_cancel={},
            succeeded_results=[_succeeded_turn_result()],
            cancellation_policy="compensate_then_continue",
            run_id="run-1",
        )
