"""Tests for RunActorWorkflow continue_as_new History safety mechanism."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from agent_kernel.kernel.contracts import RunProjection
from agent_kernel.substrate.temporal.run_actor_workflow import (
    RunActorStrictModeConfig,
    RunActorWorkflow,
    RunInput,
)


def _make_projection(lifecycle_state: str = "ready") -> RunProjection:
    return RunProjection(
        run_id="run-1",
        lifecycle_state=lifecycle_state,
        projected_offset=1,
        waiting_external=False,
        ready_for_dispatch=True,
        current_action_id=None,
        recovery_mode=None,
        recovery_reason=None,
        active_child_runs=[],
    )


def _make_workflow(threshold: int = 10_000) -> RunActorWorkflow:
    from agent_kernel.kernel.minimal_runtime import (
        AsyncExecutorService,
        InMemoryDecisionDeduper,
        InMemoryDecisionProjectionService,
        InMemoryKernelRuntimeEventLog,
        StaticDispatchAdmissionService,
        StaticRecoveryGateService,
    )

    event_log = InMemoryKernelRuntimeEventLog()
    return RunActorWorkflow(
        event_log=event_log,
        projection=InMemoryDecisionProjectionService(event_log),
        admission=StaticDispatchAdmissionService(),
        executor=AsyncExecutorService(),
        recovery=StaticRecoveryGateService(),
        deduper=InMemoryDecisionDeduper(),
        strict_mode=RunActorStrictModeConfig(
            enabled=False,
            history_event_threshold=threshold,
        ),
    )


class TestRunActorStrictModeConfigThreshold:
    def test_default_threshold_is_ten_thousand(self) -> None:
        config = RunActorStrictModeConfig()
        assert config.history_event_threshold == 10_000

    def test_custom_threshold_is_stored(self) -> None:
        config = RunActorStrictModeConfig(history_event_threshold=500)
        assert config.history_event_threshold == 500


class TestShouldContinueAsNew:
    def test_returns_false_outside_temporal_context(self) -> None:
        wf = _make_workflow(threshold=1)
        wf._history_event_count = 99
        wf._last_projection = _make_projection("ready")
        # _is_temporal_workflow_context() returns False in test env
        assert wf._should_continue_as_new() is False

    def test_returns_false_when_count_below_threshold(self) -> None:
        wf = _make_workflow(threshold=100)
        wf._history_event_count = 50
        wf._last_projection = _make_projection("ready")
        with (
            patch(
                "agent_kernel.substrate.temporal.run_actor_workflow._is_temporal_workflow_context",
                return_value=True,
            ),
            patch(
                "agent_kernel.substrate.temporal.run_actor_workflow.temporal_workflow",
                new=MagicMock(),
            ),
        ):
            assert wf._should_continue_as_new() is False

    def test_returns_true_when_count_meets_threshold(self) -> None:
        wf = _make_workflow(threshold=5)
        wf._history_event_count = 5
        wf._last_projection = _make_projection("ready")
        with (
            patch(
                "agent_kernel.substrate.temporal.run_actor_workflow._is_temporal_workflow_context",
                return_value=True,
            ),
            patch(
                "agent_kernel.substrate.temporal.run_actor_workflow.temporal_workflow",
                new=MagicMock(),
            ),
        ):
            assert wf._should_continue_as_new() is True

    def test_returns_false_for_completed_lifecycle(self) -> None:
        wf = _make_workflow(threshold=1)
        wf._history_event_count = 999
        wf._last_projection = _make_projection("completed")
        with (
            patch(
                "agent_kernel.substrate.temporal.run_actor_workflow._is_temporal_workflow_context",
                return_value=True,
            ),
            patch(
                "agent_kernel.substrate.temporal.run_actor_workflow.temporal_workflow",
                new=MagicMock(),
            ),
        ):
            assert wf._should_continue_as_new() is False

    def test_returns_false_for_aborted_lifecycle(self) -> None:
        wf = _make_workflow(threshold=1)
        wf._history_event_count = 999
        wf._last_projection = _make_projection("aborted")
        with (
            patch(
                "agent_kernel.substrate.temporal.run_actor_workflow._is_temporal_workflow_context",
                return_value=True,
            ),
            patch(
                "agent_kernel.substrate.temporal.run_actor_workflow.temporal_workflow",
                new=MagicMock(),
            ),
        ):
            assert wf._should_continue_as_new() is False

    def test_returns_false_when_projection_is_none(self) -> None:
        wf = _make_workflow(threshold=1)
        wf._history_event_count = 999
        wf._last_projection = None
        with (
            patch(
                "agent_kernel.substrate.temporal.run_actor_workflow._is_temporal_workflow_context",
                return_value=True,
            ),
            patch(
                "agent_kernel.substrate.temporal.run_actor_workflow.temporal_workflow",
                new=MagicMock(),
            ),
        ):
            assert wf._should_continue_as_new() is False


class TestTriggerContinueAsNew:
    def test_calls_temporal_continue_as_new_with_run_input(self) -> None:
        wf = _make_workflow()
        wf._run_id = "run-42"
        wf._session_id = "sess-1"
        wf._parent_run_id = "parent-run-1"

        mock_tw = MagicMock()
        with patch(
            "agent_kernel.substrate.temporal.run_actor_workflow.temporal_workflow",
            new=mock_tw,
        ):
            wf._trigger_continue_as_new()

        mock_tw.continue_as_new.assert_called_once()
        call_arg = mock_tw.continue_as_new.call_args[0][0]
        assert isinstance(call_arg, RunInput)
        assert call_arg.run_id == "run-42"
        assert call_arg.session_id == "sess-1"
        assert call_arg.parent_run_id == "parent-run-1"

    def test_does_nothing_when_run_id_is_none(self) -> None:
        wf = _make_workflow()
        wf._run_id = None

        mock_tw = MagicMock()
        with patch(
            "agent_kernel.substrate.temporal.run_actor_workflow.temporal_workflow",
            new=mock_tw,
        ):
            wf._trigger_continue_as_new()

        mock_tw.continue_as_new.assert_not_called()

    def test_preserves_none_parent_run_id(self) -> None:
        wf = _make_workflow()
        wf._run_id = "run-99"
        wf._session_id = None
        wf._parent_run_id = None

        mock_tw = MagicMock()
        with patch(
            "agent_kernel.substrate.temporal.run_actor_workflow.temporal_workflow",
            new=mock_tw,
        ):
            wf._trigger_continue_as_new()

        call_arg = mock_tw.continue_as_new.call_args[0][0]
        assert call_arg.parent_run_id is None


class TestHistoryCounterIncrement:
    def test_history_event_count_starts_at_zero(self) -> None:
        wf = _make_workflow()
        assert wf._history_event_count == 0

    def test_parent_run_id_stored_during_run(self) -> None:
        import asyncio

        from agent_kernel.kernel.minimal_runtime import (
            AsyncExecutorService,
            InMemoryDecisionDeduper,
            InMemoryDecisionProjectionService,
            InMemoryKernelRuntimeEventLog,
            StaticDispatchAdmissionService,
            StaticRecoveryGateService,
        )

        event_log = InMemoryKernelRuntimeEventLog()
        wf = RunActorWorkflow(
            event_log=event_log,
            projection=InMemoryDecisionProjectionService(event_log),
            admission=StaticDispatchAdmissionService(),
            executor=AsyncExecutorService(),
            recovery=StaticRecoveryGateService(),
            deduper=InMemoryDecisionDeduper(),
            strict_mode=RunActorStrictModeConfig(enabled=False),
        )

        async def _run() -> None:
            await wf.run(
                RunInput(
                    run_id="run-x",
                    session_id="sess-x",
                    parent_run_id="parent-x",
                )
            )

        asyncio.run(_run())
        assert wf._parent_run_id == "parent-x"
