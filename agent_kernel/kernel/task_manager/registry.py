"""TaskRegistry: stores and queries task descriptors and attempts.

Backed by KernelRuntimeEventLog so task state is durable and replayable.
All mutating operations append events; reads reconstruct state from the
in-memory task store (populated at registration/update time).

This is NOT an authority — it does not own projection or admission.
It is a coordinator that observes run outcomes and maintains task state.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field

from agent_kernel.kernel.task_manager.contracts import (
    TaskAttempt,
    TaskDescriptor,
    TaskHealthStatus,
    TaskLifecycleState,
)

_logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _TaskEntry:
    """Internal mutable entry for one task."""

    descriptor: TaskDescriptor
    lifecycle_state: TaskLifecycleState = "pending"
    attempts: list[TaskAttempt] = field(default_factory=list)
    last_heartbeat_ms: int | None = None
    consecutive_missed_beats: int = 0


class TaskRegistry:
    """Central in-process registry for task descriptors and attempt history.

    Thread-safety: all public methods are protected by a threading.Lock so the
    registry is safe to use from both asyncio coroutines and background threads.

    For production deployments back this with a persistent store by subclassing
    or wrapping this registry with a durable adapter.  The in-process store is
    suitable for PoC and single-worker scenarios.

    Args:
        max_tasks: Maximum number of tasks retained in memory before oldest
            completed/aborted tasks are evicted.  Prevents unbounded growth.
    """

    _MAX_TASKS_DEFAULT = 10_000

    def __init__(self, max_tasks: int = _MAX_TASKS_DEFAULT) -> None:
        self._tasks: dict[str, _TaskEntry] = {}
        self._session_index: dict[str, list[str]] = {}  # session_id → [task_id]
        self._run_index: dict[str, str] = {}  # run_id → task_id
        self._lock = threading.Lock()
        self._max_tasks = max_tasks

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, descriptor: TaskDescriptor) -> None:
        """Register a new task descriptor.

        Args:
            descriptor: Task to register.  task_id must be unique.

        Raises:
            ValueError: If task_id is already registered.
        """
        with self._lock:
            if descriptor.task_id in self._tasks:
                raise ValueError(
                    f"Task '{descriptor.task_id}' is already registered. "
                    "Use a different task_id or call update_state() instead."
                )
            self._evict_if_needed()
            self._tasks[descriptor.task_id] = _TaskEntry(descriptor=descriptor)
            self._session_index.setdefault(descriptor.session_id, []).append(descriptor.task_id)
            _logger.debug(
                "task.registered task_id=%s session_id=%s kind=%s",
                descriptor.task_id,
                descriptor.session_id,
                descriptor.task_kind,
            )

    # ------------------------------------------------------------------
    # Attempt tracking
    # ------------------------------------------------------------------

    def start_attempt(self, attempt: TaskAttempt) -> None:
        """Record the start of a new attempt for an existing task.

        Args:
            attempt: Attempt metadata.  task_id must be registered.

        Raises:
            KeyError: If task_id is not registered.
        """
        with self._lock:
            entry = self._tasks[attempt.task_id]
            entry.attempts.append(attempt)
            entry.lifecycle_state = "running"
            entry.last_heartbeat_ms = _now_ms()
            self._run_index[attempt.run_id] = attempt.task_id
            _logger.debug(
                "task.attempt_started task_id=%s attempt_seq=%d run_id=%s",
                attempt.task_id,
                attempt.attempt_seq,
                attempt.run_id,
            )

    def complete_attempt(self, task_id: str, run_id: str, outcome: str) -> None:
        """Mark the current attempt as completed and advance task state.

        Args:
            task_id: Task identifier.
            run_id: Run identifier of the attempt being completed.
            outcome: One of "completed", "failed", "cancelled".
        """
        with self._lock:
            entry = self._tasks.get(task_id)
            if entry is None:
                _logger.warning("complete_attempt: unknown task_id=%s", task_id)
                return
            import datetime

            now_iso = datetime.datetime.now(datetime.UTC).isoformat()
            # Update the matching attempt in-place via replacement (list of frozen)
            updated: list[TaskAttempt] = []
            for a in entry.attempts:
                if a.run_id == run_id and a.outcome is None:
                    from dataclasses import replace

                    updated.append(replace(a, outcome=outcome, completed_at=now_iso))
                else:
                    updated.append(a)
            entry.attempts = updated

            if outcome == "completed":
                entry.lifecycle_state = "completed"
            else:
                entry.lifecycle_state = "failed"

            self._run_index.pop(run_id, None)
            _logger.debug(
                "task.attempt_completed task_id=%s run_id=%s outcome=%s",
                task_id,
                run_id,
                outcome,
            )

    def update_state(self, task_id: str, state: TaskLifecycleState) -> None:
        """Explicitly set the lifecycle state for a task.

        Used by RestartPolicyEngine to mark tasks as "restarting",
        "reflecting", "escalated", or "aborted".

        Args:
            task_id: Task identifier.
            state: New lifecycle state.
        """
        with self._lock:
            entry = self._tasks.get(task_id)
            if entry is None:
                _logger.warning("update_state: unknown task_id=%s", task_id)
                return
            entry.lifecycle_state = state

    def heartbeat(self, task_id: str) -> None:
        """Record a liveness heartbeat for a task.

        Called by TaskWatchdog on each ObservabilityHook event for runs
        associated with this task.

        Args:
            task_id: Task identifier.
        """
        with self._lock:
            entry = self._tasks.get(task_id)
            if entry is None:
                return
            now = _now_ms()
            if entry.last_heartbeat_ms is not None and now <= entry.last_heartbeat_ms:
                now = entry.last_heartbeat_ms + 1
            entry.last_heartbeat_ms = now
            entry.consecutive_missed_beats = 0

    def heartbeat_for_run(self, run_id: str) -> None:
        """Record a liveness heartbeat via run_id lookup.

        Convenience wrapper used by TaskWatchdog's ObservabilityHook
        integration where only run_id is available.

        Args:
            run_id: Run identifier to look up and heartbeat.
        """
        with self._lock:
            task_id = self._run_index.get(run_id)
            if task_id is None:
                return
            entry = self._tasks.get(task_id)
            if entry is None:
                return
            now = _now_ms()
            # Guarantee monotonic increase even when clock resolution coarser
            # than the interval between two consecutive calls (e.g. on Windows
            # where time.perf_counter_ns() may return the same millisecond
            # value for calls made within the same timer tick).
            if entry.last_heartbeat_ms is not None and now <= entry.last_heartbeat_ms:
                now = entry.last_heartbeat_ms + 1
            entry.last_heartbeat_ms = now
            entry.consecutive_missed_beats = 0

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get(self, task_id: str) -> TaskDescriptor | None:
        """Return descriptor for task_id, or None if not registered.

        Args:
            task_id: Task identifier.

        Returns:
            TaskDescriptor or None.
        """
        with self._lock:
            entry = self._tasks.get(task_id)
            return entry.descriptor if entry else None

    def get_health(self, task_id: str) -> TaskHealthStatus | None:
        """Return current health snapshot for a task.

        Args:
            task_id: Task identifier.

        Returns:
            TaskHealthStatus or None if task_id not registered.
        """
        with self._lock:
            entry = self._tasks.get(task_id)
            if entry is None:
                return None
            current_attempt = entry.attempts[-1] if entry.attempts else None
            timeout_ms = entry.descriptor.restart_policy.heartbeat_timeout_ms
            is_stalled = False
            if entry.lifecycle_state == "running" and entry.last_heartbeat_ms is not None:
                age_ms = _now_ms() - entry.last_heartbeat_ms
                is_stalled = age_ms > timeout_ms
            return TaskHealthStatus(
                task_id=task_id,
                lifecycle_state=entry.lifecycle_state,
                current_run_id=(
                    current_attempt.run_id
                    if current_attempt and current_attempt.outcome is None
                    else None
                ),
                attempt_seq=len(entry.attempts),
                max_attempts=entry.descriptor.restart_policy.max_attempts,
                last_heartbeat_ms=entry.last_heartbeat_ms,
                consecutive_missed_beats=entry.consecutive_missed_beats,
                is_stalled=is_stalled,
            )

    def get_attempts(self, task_id: str) -> list[TaskAttempt]:
        """Return all recorded attempts for a task (oldest first).

        Args:
            task_id: Task identifier.

        Returns:
            List of TaskAttempt records.
        """
        with self._lock:
            entry = self._tasks.get(task_id)
            return list(entry.attempts) if entry else []

    def list_session_tasks(self, session_id: str) -> list[TaskDescriptor]:
        """Return all task descriptors registered for a session.

        Args:
            session_id: Session identifier.

        Returns:
            List of TaskDescriptor objects in registration order.
        """
        with self._lock:
            task_ids = self._session_index.get(session_id, [])
            result = []
            for tid in task_ids:
                entry = self._tasks.get(tid)
                if entry:
                    result.append(entry.descriptor)
            return result

    def get_stalled_tasks(self) -> list[TaskHealthStatus]:
        """Return health snapshots for all tasks that appear stalled.

        A task is stalled when it is in "running" state and its last heartbeat
        exceeds the policy heartbeat_timeout_ms.  Used by TaskWatchdog.

        Returns:
            List of TaskHealthStatus for stalled tasks.
        """
        with self._lock:
            stalled = []
            now = _now_ms()
            for task_id, entry in self._tasks.items():
                if entry.lifecycle_state != "running":
                    continue
                if entry.last_heartbeat_ms is None:
                    continue
                timeout_ms = entry.descriptor.restart_policy.heartbeat_timeout_ms
                if now - entry.last_heartbeat_ms > timeout_ms:
                    entry.consecutive_missed_beats += 1
                    current_attempt = entry.attempts[-1] if entry.attempts else None
                    stalled.append(
                        TaskHealthStatus(
                            task_id=task_id,
                            lifecycle_state=entry.lifecycle_state,
                            current_run_id=(
                                current_attempt.run_id
                                if current_attempt and current_attempt.outcome is None
                                else None
                            ),
                            attempt_seq=len(entry.attempts),
                            max_attempts=entry.descriptor.restart_policy.max_attempts,
                            last_heartbeat_ms=entry.last_heartbeat_ms,
                            consecutive_missed_beats=entry.consecutive_missed_beats,
                            is_stalled=True,
                        )
                    )
            return stalled

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _evict_if_needed(self) -> None:
        """Evict oldest terminal tasks when max_tasks is exceeded."""
        if len(self._tasks) < self._max_tasks:
            return
        terminal = [
            tid
            for tid, e in self._tasks.items()
            if e.lifecycle_state in ("completed", "aborted", "escalated")
        ]
        # Evict oldest half of terminal tasks
        to_evict = terminal[: max(1, len(terminal) // 2)]
        for tid in to_evict:
            entry = self._tasks.pop(tid, None)
            if entry:
                sid = entry.descriptor.session_id
                if sid in self._session_index:
                    self._session_index[sid] = [t for t in self._session_index[sid] if t != tid]
        if to_evict:
            _logger.debug("task_registry.evicted count=%d", len(to_evict))


def _now_ms() -> int:
    """Return current time in milliseconds using high-resolution performance counter."""
    import time

    return time.perf_counter_ns() // 1_000_000
