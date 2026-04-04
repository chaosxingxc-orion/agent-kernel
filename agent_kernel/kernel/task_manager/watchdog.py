"""TaskWatchdog: scans for stalled tasks and triggers restart policy.

Integrates with ObservabilityHook to receive run-level events and map them
to task-level heartbeats.  Runs as a non-authority background service.

Architecture invariant: TaskWatchdog never writes to the event log directly.
All state changes go through TaskRegistry (in-process) or RestartPolicyEngine
(which delegates to KernelFacade for new run launches).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent_kernel.kernel.task_manager.registry import TaskRegistry
    from agent_kernel.kernel.task_manager.restart_policy import RestartPolicyEngine

_logger = logging.getLogger(__name__)


class TaskWatchdog:
    """Scans for stalled tasks and delegates restart decisions.

    Intended to be called periodically from a background asyncio task,
    analogous to RunHeartbeatMonitor.watchdog_once().

    Args:
        registry: TaskRegistry providing stall detection and heartbeats.
        policy_engine: RestartPolicyEngine that handles failures.
    """

    def __init__(
        self,
        registry: TaskRegistry,
        policy_engine: RestartPolicyEngine,
    ) -> None:
        self._registry = registry
        self._policy_engine = policy_engine

    async def watchdog_once(self) -> list[str]:
        """Scan for stalled tasks and trigger restart for each.

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
            try:
                await self._policy_engine.handle_failure(
                    task_id=health.task_id,
                    failed_run_id=health.current_run_id,
                    failure=None,  # stall — no explicit FailureEnvelope
                )
                processed.append(health.task_id)
            except Exception as exc:
                _logger.error(
                    "task_watchdog: error handling stall task_id=%s: %s",
                    health.task_id,
                    exc,
                )

        return processed

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
            task_id = self._registry._run_index.get(run_id)
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
