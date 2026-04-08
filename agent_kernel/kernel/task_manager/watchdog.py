"""TaskWatchdog: scans for stalled tasks and notifies via callback.

Integrates with ObservabilityHook to receive run-level events and map them
to task-level heartbeats.  Runs as a non-authority background service.

Architecture invariant: TaskWatchdog never writes to the event log directly.
All state changes go through TaskRegistry (in-process).  Stall handling
decisions are delegated to the caller via an async callback.

Business logic (RestartPolicyEngine) has been migrated to hi-agent.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent_kernel.kernel.task_manager.registry import TaskRegistry

_logger = logging.getLogger(__name__)

# Type alias for the stall handler callback.
# Receives (task_id, run_id) and returns nothing.
StallHandler = Callable[[str, str], Awaitable[None]]


class TaskWatchdog:
    """Scans for stalled tasks and delegates restart decisions via callback.

    Intended to be called periodically from a background asyncio task,
    analogous to RunHeartbeatMonitor.watchdog_once().

    Args:
        registry: TaskRegistry providing stall detection and heartbeats.
        on_stall: Optional async callback invoked for each stalled task.
            Receives (task_id, current_run_id).  When None, stalls are
            logged but no action is taken.

    """

    def __init__(
        self,
        registry: TaskRegistry,
        on_stall: StallHandler | None = None,
    ) -> None:
        """Initialize the instance with configured dependencies."""
        self._registry = registry
        self._on_stall = on_stall

    async def watchdog_once(self) -> list[str]:
        """Scan for stalled tasks and notify via callback for each.

        Returns:
            List of task_ids that were found stalled and processed.

        """
        stalled = self._registry.get_stalled_tasks()
        processed: list[str] = []

        for health in stalled:
            if health.current_run_id is None:
                # No active run — might have already been handled
                continue
            _logger.warning(
                "task.stall_detected task_id=%s run_id=%s missed_beats=%d",
                health.task_id,
                health.current_run_id,
                health.consecutive_missed_beats,
            )
            if self._on_stall is not None:
                try:
                    await self._on_stall(health.task_id, health.current_run_id)
                    processed.append(health.task_id)
                except Exception as exc:
                    _logger.error(
                        "task_watchdog: error handling stall task_id=%s: %s",
                        health.task_id,
                        exc,
                    )
            else:
                processed.append(health.task_id)

        return processed

    def start(self, interval_s: float = 60.0) -> asyncio.Task[Any]:
        """Start background watchdog loop and return the task handle."""

        async def _loop() -> None:
            try:
                while True:
                    await asyncio.sleep(interval_s)
                    await self.watchdog_once()
            except asyncio.CancelledError:
                return

        return asyncio.get_running_loop().create_task(_loop(), name="task_watchdog")

    # ------------------------------------------------------------------
    # ObservabilityHook integration (partial — implement interface methods
    # needed for heartbeat forwarding)
    # ------------------------------------------------------------------

    def on_turn_state_transition(
        self,
        *,
        run_id: str,
        action_id: str,
        from_state: str,
        to_state: str,
        turn_offset: int,
        timestamp_ms: int,
    ) -> None:
        """Forward TurnEngine state transition as task heartbeat."""
        self._registry.heartbeat_for_run(run_id)

    def on_run_lifecycle_transition(
        self,
        *,
        run_id: str,
        from_state: str,
        to_state: str,
        timestamp_ms: int,
    ) -> None:
        """Forward run lifecycle transition as task heartbeat.

        When a run completes or aborts, mark the associated task attempt done.
        """
        self._registry.heartbeat_for_run(run_id)
        if to_state in ("completed", "aborted"):
            # Look up task and record attempt completion
            task_id = self._registry.get_task_id_for_run(run_id)
            if task_id:
                outcome = "completed" if to_state == "completed" else "failed"
                self._registry.complete_attempt(task_id, run_id, outcome)

    def on_action_dispatch(
        self,
        *,
        run_id: str,
        action_id: str,
        action_type: str,
        outcome_kind: str,
        latency_ms: int,
    ) -> None:
        """Forward action dispatch as task heartbeat."""
        self._registry.heartbeat_for_run(run_id)

    def on_llm_call(
        self,
        *,
        run_id: str,
        model_ref: str,
        latency_ms: int,
        token_usage: Any,
    ) -> None:
        """Forward LLM call as task heartbeat."""
        self._registry.heartbeat_for_run(run_id)

    def on_parallel_branch_result(
        self,
        *,
        run_id: str,
        action_id: str,
        branch_index: int,
        succeeded: bool,
        latency_ms: int,
    ) -> None:
        """Forward parallel branch result as task heartbeat."""
        self._registry.heartbeat_for_run(run_id)
