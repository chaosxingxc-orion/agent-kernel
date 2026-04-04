"""Tests for RestartPolicyEngine: retry, reflect, escalate, abort decisions."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_kernel.kernel.task_manager.contracts import (
    TaskAttempt,
    TaskDescriptor,
    TaskRestartPolicy,
)
from agent_kernel.kernel.task_manager.registry import TaskRegistry
from agent_kernel.kernel.task_manager.restart_policy import RestartDecision, RestartPolicyEngine

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_registry_with_task(
    task_id: str = "t1",
    session_id: str = "s1",
    max_attempts: int = 3,
    on_exhausted: str = "reflect",
) -> TaskRegistry:
    reg = TaskRegistry()
    reg.register(
        TaskDescriptor(
            task_id=task_id,
            session_id=session_id,
            task_kind="root",
            goal_description="test goal",
            restart_policy=TaskRestartPolicy(
                max_attempts=max_attempts,
                on_exhausted=on_exhausted,  # type: ignore[arg-type]
            ),
        )
    )
    attempt = TaskAttempt(
        attempt_id="a1",
        task_id=task_id,
        run_id="r1",
        attempt_seq=1,
        started_at="2026-01-01T00:00:00+00:00",
    )
    reg.start_attempt(attempt)
    return reg


def _make_engine(
    registry: TaskRegistry,
    facade: object | None = None,
) -> RestartPolicyEngine:
    if facade is None:
        facade = AsyncMock()
        facade.start_run = AsyncMock(return_value=MagicMock(run_id="r-new"))
    return RestartPolicyEngine(registry=registry, facade=facade)


# ---------------------------------------------------------------------------
# Pure _decide() logic (no side effects)
# ---------------------------------------------------------------------------


class TestDecideLogic:
    def test_retry_when_attempts_remaining(self) -> None:
        engine = _make_engine(_make_registry_with_task())
        policy = TaskRestartPolicy(max_attempts=3, on_exhausted="reflect")
        decision = engine._decide(policy, attempt_seq=1, failure=None)
        assert decision.action == "retry"
        assert decision.next_attempt_seq == 2

    def test_reflect_when_exhausted_on_reflect(self) -> None:
        engine = _make_engine(_make_registry_with_task())
        policy = TaskRestartPolicy(max_attempts=2, on_exhausted="reflect")
        decision = engine._decide(policy, attempt_seq=2, failure=None)
        assert decision.action == "reflect"
        assert decision.next_attempt_seq is None

    def test_escalate_when_exhausted_on_escalate(self) -> None:
        engine = _make_engine(_make_registry_with_task())
        policy = TaskRestartPolicy(max_attempts=1, on_exhausted="escalate")
        decision = engine._decide(policy, attempt_seq=1, failure=None)
        assert decision.action == "escalate"

    def test_abort_when_exhausted_on_abort(self) -> None:
        engine = _make_engine(_make_registry_with_task())
        policy = TaskRestartPolicy(max_attempts=1, on_exhausted="abort")
        decision = engine._decide(policy, attempt_seq=1, failure=None)
        assert decision.action == "abort"

    def test_non_retryable_failure_skips_retry(self) -> None:
        engine = _make_engine(_make_registry_with_task())
        policy = TaskRestartPolicy(max_attempts=5, on_exhausted="reflect")
        failure = MagicMock()
        failure.retryability = "non_retryable"
        failure.failure_code = "PERMISSION_DENIED"
        decision = engine._decide(policy, attempt_seq=1, failure=failure)
        assert decision.action == "reflect"
        assert "non_retryable" in decision.reason

    def test_unknown_retryability_treated_as_retryable(self) -> None:
        engine = _make_engine(_make_registry_with_task())
        policy = TaskRestartPolicy(max_attempts=3, on_exhausted="abort")
        failure = MagicMock()
        failure.retryability = "unknown"
        decision = engine._decide(policy, attempt_seq=1, failure=failure)
        assert decision.action == "retry"


# ---------------------------------------------------------------------------
# handle_failure() integration (with registry side-effects)
# ---------------------------------------------------------------------------


class TestHandleFailure:
    @pytest.mark.asyncio
    async def test_retry_updates_state_to_restarting(self) -> None:
        reg = _make_registry_with_task(max_attempts=3)
        engine = _make_engine(reg)
        decision = await engine.handle_failure("t1", "r1")
        assert decision.action == "retry"
        health = reg.get_health("t1")
        assert health is not None
        assert health.lifecycle_state == "restarting"

    @pytest.mark.asyncio
    async def test_reflect_updates_state(self) -> None:
        reg = _make_registry_with_task(max_attempts=1, on_exhausted="reflect")
        engine = _make_engine(reg)
        decision = await engine.handle_failure("t1", "r1")
        assert decision.action == "reflect"
        health = reg.get_health("t1")
        assert health is not None
        assert health.lifecycle_state == "reflecting"

    @pytest.mark.asyncio
    async def test_escalate_updates_state(self) -> None:
        reg = _make_registry_with_task(max_attempts=1, on_exhausted="escalate")
        engine = _make_engine(reg)
        decision = await engine.handle_failure("t1", "r1")
        assert decision.action == "escalate"
        health = reg.get_health("t1")
        assert health is not None
        assert health.lifecycle_state == "escalated"

    @pytest.mark.asyncio
    async def test_abort_updates_state(self) -> None:
        reg = _make_registry_with_task(max_attempts=1, on_exhausted="abort")
        engine = _make_engine(reg)
        decision = await engine.handle_failure("t1", "r1")
        assert decision.action == "abort"
        health = reg.get_health("t1")
        assert health is not None
        assert health.lifecycle_state == "aborted"

    @pytest.mark.asyncio
    async def test_unknown_task_returns_abort_decision(self) -> None:
        reg = TaskRegistry()
        engine = _make_engine(reg)
        decision = await engine.handle_failure("ghost", "r0")
        assert decision.action == "abort"
        assert decision.task_id == "ghost"

    @pytest.mark.asyncio
    async def test_retry_launch_failure_aborts_task(self) -> None:
        reg = _make_registry_with_task(max_attempts=3)
        facade = AsyncMock()
        facade.start_run = AsyncMock(side_effect=RuntimeError("gateway down"))
        engine = RestartPolicyEngine(registry=reg, facade=facade)
        decision = await engine.handle_failure("t1", "r1")
        assert decision.action == "retry"
        # After launch failure the state should be aborted
        health = reg.get_health("t1")
        assert health is not None
        assert health.lifecycle_state == "aborted"

    @pytest.mark.asyncio
    async def test_restart_decision_is_frozen_dataclass(self) -> None:
        reg = _make_registry_with_task()
        engine = _make_engine(reg)
        decision = await engine.handle_failure("t1", "r1")
        assert isinstance(decision, RestartDecision)
        with pytest.raises((AttributeError, TypeError)):
            decision.action = "abort"  # type: ignore[misc]
