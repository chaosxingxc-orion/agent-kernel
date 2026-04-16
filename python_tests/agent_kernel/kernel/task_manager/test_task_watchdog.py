"""Verifies for taskwatchdog: stall detection and observabilityhook forwarding."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_kernel.kernel.task_manager.contracts import (
    TaskAttempt,
    TaskDescriptor,
    TaskRestartPolicy,
)
from agent_kernel.kernel.task_manager.registry import TaskRegistry
from agent_kernel.kernel.task_manager.watchdog import TaskWatchdog

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_registry_with_running_task(
    task_id: str = "t1",
    heartbeat_timeout_ms: int = 300_000,
) -> TaskRegistry:
    """Make registry with running task."""
    reg = TaskRegistry()
    reg.register(
        TaskDescriptor(
            task_id=task_id,
            session_id="s1",
            task_kind="root",
            goal_description="test",
            restart_policy=TaskRestartPolicy(
                max_attempts=3,
                heartbeat_timeout_ms=heartbeat_timeout_ms,
            ),
        )
    )
    reg.start_attempt(
        TaskAttempt(
            attempt_id="a1",
            task_id=task_id,
            run_id="r1",
            attempt_seq=1,
            started_at="2026-01-01T00:00:00+00:00",
        )
    )
    return reg


def _make_watchdog(
    registry: TaskRegistry,
    on_stall: object | None = None,
) -> TaskWatchdog:
    """Make watchdog."""
    if on_stall is None:
        on_stall = AsyncMock()
    return TaskWatchdog(registry=registry, on_stall=on_stall)


# ---------------------------------------------------------------------------
# watchdog_once() — stall handling
# ---------------------------------------------------------------------------


class TestWatchdogOnce:
    """Test suite for WatchdogOnce."""

    @pytest.mark.asyncio
    async def test_no_stalled_tasks_returns_empty(self) -> None:
        """Verifies no stalled tasks returns empty."""
        reg = _make_registry_with_running_task()
        watchdog = _make_watchdog(reg)
        result = await watchdog.watchdog_once()
        assert result == []

    @pytest.mark.asyncio
    async def test_stalled_task_is_processed(self) -> None:
        """Verifies stalled task is processed."""
        reg = _make_registry_with_running_task(heartbeat_timeout_ms=1)
        # Force last heartbeat to old value
        entry = reg._tasks["t1"]
        entry.last_heartbeat_ms = int(time.monotonic() * 1000) - 10_000
        watchdog = _make_watchdog(reg)
        result = await watchdog.watchdog_once()
        assert "t1" in result

    @pytest.mark.asyncio
    async def test_completed_task_not_processed(self) -> None:
        """Verifies completed task not processed."""
        reg = _make_registry_with_running_task(heartbeat_timeout_ms=1)
        reg.complete_attempt("t1", "r1", "completed")
        watchdog = _make_watchdog(reg)
        result = await watchdog.watchdog_once()
        assert result == []

    @pytest.mark.asyncio
    async def test_stall_without_active_run_skipped(self) -> None:
        """Stalled task with no current_run_id is skipped (already handled)."""
        reg = TaskRegistry()
        reg.register(
            TaskDescriptor(
                task_id="t1",
                session_id="s1",
                task_kind="root",
                goal_description="test",
                restart_policy=TaskRestartPolicy(max_attempts=3, heartbeat_timeout_ms=1),
            )
        )
        # Never start an attempt — no run_id
        watchdog = _make_watchdog(reg)
        result = await watchdog.watchdog_once()
        assert result == []

    @pytest.mark.asyncio
    async def test_callback_error_does_not_propagate(self) -> None:
        """Verifies callback error does not propagate."""
        reg = _make_registry_with_running_task(heartbeat_timeout_ms=1)
        entry = reg._tasks["t1"]
        entry.last_heartbeat_ms = int(time.monotonic() * 1000) - 10_000
        on_stall = AsyncMock(side_effect=RuntimeError("boom"))
        watchdog = _make_watchdog(reg, on_stall=on_stall)
        # Should not raise; errors are swallowed and logged
        result = await watchdog.watchdog_once()
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_no_callback_still_reports_stalled(self) -> None:
        """When on_stall is None, stalled tasks are still detected and listed."""
        reg = _make_registry_with_running_task(heartbeat_timeout_ms=1)
        entry = reg._tasks["t1"]
        entry.last_heartbeat_ms = int(time.monotonic() * 1000) - 10_000
        watchdog = TaskWatchdog(registry=reg, on_stall=None)
        result = await watchdog.watchdog_once()
        assert "t1" in result


# ---------------------------------------------------------------------------
# ObservabilityHook methods
# ---------------------------------------------------------------------------


class TestObservabilityHook:
    """Test suite for ObservabilityHook."""

    def test_on_turn_state_transition_heartbeats_task(self) -> None:
        """Verifies on turn state transition heartbeats task."""
        reg = _make_registry_with_running_task()
        watchdog = _make_watchdog(reg)
        initial_hb = reg._tasks["t1"].last_heartbeat_ms
        watchdog.on_turn_state_transition(
            run_id="r1",
            action_id="a1",
            from_state="collecting",
            to_state="dispatched",
            turn_offset=1,
            timestamp_ms=12345,
        )
        assert reg._tasks["t1"].last_heartbeat_ms != initial_hb

    def test_on_action_dispatch_heartbeats_task(self) -> None:
        """Verifies on action dispatch heartbeats task."""
        reg = _make_registry_with_running_task()
        watchdog = _make_watchdog(reg)
        watchdog.on_action_dispatch(
            run_id="r1",
            action_id="a1",
            action_type="tool_call",
            outcome_kind="success",
            latency_ms=100,
        )
        assert reg._tasks["t1"].last_heartbeat_ms is not None

    def test_on_llm_call_heartbeats_task(self) -> None:
        """Verifies on llm call heartbeats task."""
        reg = _make_registry_with_running_task()
        watchdog = _make_watchdog(reg)
        watchdog.on_llm_call(
            run_id="r1",
            model_ref="gpt-4o",
            latency_ms=500,
            token_usage=MagicMock(),
        )
        assert reg._tasks["t1"].last_heartbeat_ms is not None

    def test_on_parallel_branch_result_heartbeats_task(self) -> None:
        """Verifies on parallel branch result heartbeats task."""
        reg = _make_registry_with_running_task()
        watchdog = _make_watchdog(reg)
        watchdog.on_parallel_branch_result(
            run_id="r1",
            action_id="a1",
            branch_index=0,
            succeeded=True,
            latency_ms=200,
        )
        assert reg._tasks["t1"].last_heartbeat_ms is not None

    def test_on_run_lifecycle_transition_completed_marks_attempt(self) -> None:
        """Verifies on run lifecycle transition completed marks attempt."""
        reg = _make_registry_with_running_task()
        watchdog = _make_watchdog(reg)
        watchdog.on_run_lifecycle_transition(
            run_id="r1",
            from_state="running",
            to_state="completed",
            timestamp_ms=99999,
        )
        health = reg.get_health("t1")
        assert health is not None
        assert health.lifecycle_state == "completed"

    def test_on_run_lifecycle_transition_aborted_marks_failed(self) -> None:
        """Verifies on run lifecycle transition aborted marks failed."""
        reg = _make_registry_with_running_task()
        watchdog = _make_watchdog(reg)
        watchdog.on_run_lifecycle_transition(
            run_id="r1",
            from_state="running",
            to_state="aborted",
            timestamp_ms=99999,
        )
        health = reg.get_health("t1")
        assert health is not None
        assert health.lifecycle_state == "failed"

    def test_on_run_lifecycle_transition_unknown_run_is_noop(self) -> None:
        """Verifies on run lifecycle transition unknown run is noop."""
        reg = _make_registry_with_running_task()
        watchdog = _make_watchdog(reg)
        # Should not raise
        watchdog.on_run_lifecycle_transition(
            run_id="no-such-run",
            from_state="running",
            to_state="completed",
            timestamp_ms=0,
        )
