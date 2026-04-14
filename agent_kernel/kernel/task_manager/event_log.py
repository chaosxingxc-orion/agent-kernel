"""TaskEventLog: persistence adapter for task lifecycle events.

TaskRegistry calls TaskEventAppender on every state-mutating operation so that
task state can be reconstructed after a Worker restart by replaying the event
stream.  InMemoryTaskEventLog is the PoC implementation; production deployments
substitute a persistent backend (PostgreSQL, Redis Stream, Temporal
SearchAttributes, etc.).

Design constraints:
- ``append_task_event`` is **synchronous** so it can be called under the
  threading.Lock inside TaskRegistry without introducing an asyncio boundary.
- Implementations must be thread-safe.
- Payloads must be JSON-serializable (dicts of primitives only).
"""

from __future__ import annotations

import contextlib
import logging
import threading
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from agent_kernel.kernel.task_manager.registry import TaskRegistry

_logger = logging.getLogger(__name__)

__all__ = [
    "InMemoryTaskEventLog",
    "TaskEventAppender",
]


@runtime_checkable
class TaskEventAppender(Protocol):
    """Write-side of task event persistence.

    Implementations must be **thread-safe** 鈥?TaskRegistry holds its internal
    lock while calling these methods.
    """

    def append_task_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """Record one task lifecycle event.

        Args:
            event_type: Registered event_type string (e.g. ``"task.registered"``).
            payload: JSON-serializable event payload.

        """
        ...

    def all_events(self) -> list[dict[str, Any]]:
        """Return all recorded events in append order.

        Returns:
            List of ``{"event_type": str, "payload": dict}`` records.

        """
        ...


class InMemoryTaskEventLog:
    """Thread-safe in-memory task event log.

    Suitable for PoC and unit tests.  Production: replace with a durable
    adapter that persists events to a storage backend.

    Args:
        max_events: Maximum number of events retained before oldest events are
            evicted (sliding window).  Prevents unbounded memory growth.

    """

    _MAX_EVENTS_DEFAULT = 100_000

    def __init__(self, max_events: int = _MAX_EVENTS_DEFAULT) -> None:
        """Initialize the instance with configured dependencies."""
        self._events: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._max_events = max_events

    # ------------------------------------------------------------------
    # TaskEventAppender implementation
    # ------------------------------------------------------------------

    def append_task_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """Append one task event.  Evicts oldest events when cap is exceeded."""
        with self._lock:
            if len(self._events) >= self._max_events:
                # Evict oldest quarter to amortize the cost of truncation.
                drop = max(1, self._max_events // 4)
                self._events = self._events[drop:]
            self._events.append({"event_type": event_type, "payload": payload})

    def all_events(self) -> list[dict[str, Any]]:
        """Return a snapshot of all stored events in append order."""
        with self._lock:
            return list(self._events)

    # ------------------------------------------------------------------
    # Recovery helper
    # ------------------------------------------------------------------

    def replay_into_registry(self, registry: TaskRegistry) -> int:
        """Restore TaskRegistry state by replaying stored events.

        Iterates events in append order and drives the registry through the
        same state transitions that were originally recorded.  Idempotent
        with respect to already-registered tasks: ``register()`` is skipped if
        ``task_id`` is already present.

        Args:
            registry: Fresh (or partially-populated) TaskRegistry to restore.

        Returns:
            Number of events successfully replayed.

        """
        from agent_kernel.kernel.task_manager.contracts import (
            TaskAttempt,
            TaskDescriptor,
            TaskRestartPolicy,
        )

        events = self.all_events()
        replayed = 0
        for record in events:
            event_type: str = record.get("event_type", "")
            payload: dict[str, Any] = dict(record.get("payload", {}))
            try:
                if event_type == "task.registered":
                    # Reconstruct nested TaskRestartPolicy from payload.
                    policy_data = payload.pop("restart_policy", {})
                    policy = TaskRestartPolicy(**policy_data)
                    # dependency_task_ids stored as list; convert back to tuple.
                    dep_ids = payload.pop("dependency_task_ids", [])
                    descriptor = TaskDescriptor(
                        **payload,
                        dependency_task_ids=tuple(dep_ids),
                        restart_policy=policy,
                    )
                    with contextlib.suppress(ValueError):
                        registry.register(descriptor)
                elif event_type == "task.attempt_started":
                    attempt = TaskAttempt(**payload)
                    existing_attempts = registry.get_attempts(attempt.task_id)
                    if any(
                        a.attempt_id == attempt.attempt_id
                        or (a.run_id == attempt.run_id and a.attempt_seq == attempt.attempt_seq)
                        for a in existing_attempts
                    ):
                        replayed += 1
                        continue
                    # start_attempt raises KeyError if task not registered;
                    # skip if that happens (event ordering issue).
                    with contextlib.suppress(KeyError):
                        registry.start_attempt(attempt)
                elif event_type in ("task.attempt_completed", "task.attempt_failed"):
                    registry.complete_attempt(
                        payload["task_id"],
                        payload["run_id"],
                        payload["outcome"],
                    )
                elif event_type in (
                    "task.restarting",
                    "task.reflecting",
                    "task.completed",
                    "task.escalated",
                    "task.aborted",
                ):
                    state_map = {
                        "task.restarting": "restarting",
                        "task.reflecting": "reflecting",
                        "task.completed": "completed",
                        "task.escalated": "escalated",
                        "task.aborted": "aborted",
                    }
                    registry.update_state(payload["task_id"], state_map[event_type])
                replayed += 1
            except Exception:  # pylint: disable=broad-exception-caught
                _logger.warning(
                    "TaskEventLog.replay_into_registry: skipping malformed event event_type=%r",
                    event_type,
                    exc_info=True,
                )
        return replayed
