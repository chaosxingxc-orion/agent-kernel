"""Event export infrastructure for agent-kernel evolution layer.

This module provides the bridge between the kernel's operational event log
(correctness, short-lived, run-scoped) and the platform's evolution store
(analytics, long-lived, cross-run).

Architecture boundary
---------------------
The kernel writes to ``KernelRuntimeEventLog`` for correctness guarantees.
The platform subscribes via ``EventExportPort`` for evolution data.  The
export is deliberately fire-and-forget: a slow or failed platform store
never blocks kernel execution.

Components
----------
- ``EventExportingEventLog`` 鈥?decorator that wraps any ``KernelRuntimeEventLog``
  and fires async export after each successful ``append_action_commit``.
- ``TurnTrace`` / ``RunTrace`` 鈥?structured evolution view of one turn / run,
  assembled from exported ``ActionCommit`` objects.
- ``InMemoryRunTraceStore`` 鈥?reference ``EventExportPort`` implementation for
  development and integration tests.  Not for production at scale.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent_kernel.kernel.contracts import ActionCommit, EventExportPort, RuntimeEvent
    from agent_kernel.kernel.persistence.event_schema_migration import EventSchemaMigrator

_export_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Export-aware event log wrapper
# ---------------------------------------------------------------------------

_TERMINAL_EVENT_TYPES: frozenset[str] = frozenset({"run.completed", "run.aborted"})
_RECOVERY_LIFECYCLE_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "run.recovering",
        "run.waiting_external",
        "run.waiting_human_input",
        "run.recovery_aborted",
        "run.recovery_succeeded",
    }
)


class EventExportingEventLog:
    """Wraps a ``KernelRuntimeEventLog`` and fires async export after each commit.

    The export path is transparent to kernel execution:
    - ``append_action_commit`` awaits only the inner log write.
    - Export is launched as a background ``asyncio.Task`` (fire-and-forget).
    - Timeout or exception in export is logged at WARNING and discarded.

    Use in production by injecting via ``KernelRuntimeConfig.event_export_port``.

    Args:
        inner: The underlying kernel event log that provides durability.
        export_port: Platform-owned export sink.
        export_timeout_ms: Per-export soft timeout in milliseconds.
            Exports that exceed this are cancelled and logged.  Default 5 s.

    """

    def __init__(
        self,
        inner: Any,
        export_port: EventExportPort,
        *,
        export_timeout_ms: int = 5000,
        event_schema_migrator: EventSchemaMigrator | None = None,
        target_event_schema_version: str | None = None,
    ) -> None:
        """Initialize the instance with configured dependencies."""
        self._inner = inner
        self._export_port = export_port
        self._export_timeout_s = export_timeout_ms / 1000.0
        self._background_tasks: set[asyncio.Task] = set()  # prevents GC of fire-and-forget tasks
        self._event_schema_migrator = event_schema_migrator
        self._target_event_schema_version = target_event_schema_version

    async def append_action_commit(self, commit: ActionCommit) -> str:
        """Append commit to inner log then fires async export.

        Args:
            commit: Commit to append.

        Returns:
            Commit reference from the inner log.

        """
        commit_ref: str = await self._inner.append_action_commit(commit)
        export_commit = self._maybe_migrate_commit(commit)
        task = asyncio.create_task(
            self._safe_export(export_commit),
            name=f"kernel-export:{commit.run_id}:{commit.commit_id}",
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return commit_ref

    async def close(self) -> None:
        """Cancel export tasks and close inner event log.

        The runtime invokes ``close()`` during shutdown through an async-aware
        resource closer. This method therefore awaits cancelled export tasks
        and also awaits ``inner.close()`` when the wrapped log exposes an async
        close method.
        """
        pending = [task for task in self._background_tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._background_tasks.clear()

        close_fn = getattr(self._inner, "close", None)
        if callable(close_fn):
            maybe_result = close_fn()
            if asyncio.iscoroutine(maybe_result):
                await maybe_result

    async def load(
        self,
        run_id: str,
        after_offset: int = 0,
    ) -> list[RuntimeEvent]:
        """Delegate load to the inner log unchanged.

        Args:
            run_id: Run identifier.
            after_offset: Exclusive lower-bound offset.

        Returns:
            Ordered list of runtime events.

        """
        return await self._inner.load(run_id, after_offset=after_offset)

    async def _safe_export(self, commit: ActionCommit) -> None:
        """Export one commit with timeout and exception isolation.

        Args:
            commit: Commit to export to the platform store.

        """
        try:
            await asyncio.wait_for(
                self._export_port.export_commit(commit),
                timeout=self._export_timeout_s,
            )
        except TimeoutError:
            _export_logger.warning(
                "export_commit timed out run_id=%s commit_id=%s timeout_s=%.1f",
                commit.run_id,
                commit.commit_id,
                self._export_timeout_s,
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            _export_logger.warning(
                "export_commit failed run_id=%s commit_id=%s error=%r",
                commit.run_id,
                commit.commit_id,
                exc,
            )

    def _maybe_migrate_commit(self, commit: ActionCommit) -> ActionCommit:
        """Migrate commit events for export when migrator is configured."""
        if self._event_schema_migrator is None:
            return commit
        if self._target_event_schema_version is None:
            return commit
        migrated_events = self._event_schema_migrator.migrate_batch(
            list(commit.events),
            target_version=self._target_event_schema_version,
        )
        return replace(commit, events=migrated_events)


# ---------------------------------------------------------------------------
# Evolution data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TurnTrace:
    """Structured summary of one dispatched turn.

    A ``TurnTrace`` is derived from an ``ActionCommit`` that carries an action.
    It captures the minimum fields needed to understand what the agent tried,
    under what capability configuration, and what happened.

    Attributes:
        action_id: Unique action identifier.
        action_type: Logical action type (e.g. ``"tool_call"``).
        effect_class: Declared side-effect class of the action.
        host_kind: Resolved dispatch host kind, when known.
        outcome_kind: Macro outcome: ``noop`` / ``blocked`` /
            ``dispatched`` / ``recovery_pending``.
        recovery_mode: Recovery mode when ``outcome_kind`` is
            ``recovery_pending``, otherwise ``None``.
        turn_event_types: Ordered list of ``event_type`` strings emitted
            for this turn 鈥?the full FSM trace.
        committed_at: RFC3339 UTC timestamp of the commit.

    """

    action_id: str
    action_type: str
    effect_class: str
    host_kind: str | None
    outcome_kind: str | None
    recovery_mode: str | None
    turn_event_types: list[str]
    committed_at: str
    dedupe_outcome: str | None = None


@dataclass(slots=True)
class RunTrace:
    """Accumulated evolution view of one run built from exported commits.

    A ``RunTrace`` is built incrementally as ``ActionCommit`` objects arrive
    from the kernel.  Each exported commit is merged via ``absorb()``.

    Attributes:
        run_id: Kernel run identifier.
        turns: Ordered list of dispatched turn summaries.
        lifecycle_event_types: Ordered list of ``run.*`` lifecycle event types
            observed across all commits, in arrival order (deduplicated).
        failure_count: Number of turns that ended in ``recovery_pending``.
        is_terminal: ``True`` when a terminal event (``run.completed`` or
            ``run.aborted``) has been observed.
        terminal_state: ``"completed"`` or ``"aborted"`` when terminal,
            otherwise ``None``.
        first_commit_at: RFC3339 timestamp of the first commit received.
        last_commit_at: RFC3339 timestamp of the most recent commit received.

    """

    run_id: str
    turns: list[TurnTrace] = field(default_factory=list)
    lifecycle_event_types: list[str] = field(default_factory=list)
    failure_count: int = 0
    is_terminal: bool = False
    terminal_state: str | None = None
    first_commit_at: str | None = None
    last_commit_at: str | None = None

    def absorb(self, commit: ActionCommit) -> None:
        """Merge one exported commit into this trace.

        Args:
            commit: The ``ActionCommit`` to absorb.

        """
        if self.first_commit_at is None:
            self.first_commit_at = commit.created_at
        self.last_commit_at = commit.created_at

        event_types = [e.event_type for e in commit.events]

        # Track lifecycle transitions (run.* events, deduplicated, in order)
        for et in event_types:
            if et.startswith("run.") and et not in self.lifecycle_event_types:
                self.lifecycle_event_types.append(et)

        # Detect terminal
        for et in event_types:
            if et in _TERMINAL_EVENT_TYPES and not self.is_terminal:
                self.is_terminal = True
                self.terminal_state = et.split(".")[1]  # "completed" or "aborted"

        # Build TurnTrace when the commit carries a dispatched action
        if commit.action is not None:
            turn = _build_turn_trace(commit, event_types)
            self.turns.append(turn)
            if turn.outcome_kind == "recovery_pending":
                self.failure_count += 1


# ---------------------------------------------------------------------------
# Reference EventExportPort implementation
# ---------------------------------------------------------------------------


class InMemoryRunTraceStore:
    """Accumulates ``ActionCommit`` exports into per-run ``RunTrace`` objects.

    Implements ``EventExportPort``.  Suitable for development, integration
    tests, and single-process observability dashboards.  Not intended for
    production at scale 鈥?use a durable streaming backend instead
    (e.g. Kafka, Redis Streams, S3).

    Usage::

        store = InMemoryRunTraceStore()
        config = KernelRuntimeConfig(event_export_port=store)
        async with await KernelRuntime.start(config) as kernel:
            await kernel.facade.start_run(request)

        trace = store.get("my-run-id")
        print(trace.failure_count, trace.terminal_state)
    """

    def __init__(self) -> None:
        """Initialize the instance with configured dependencies."""
        self._traces: dict[str, RunTrace] = {}

    async def export_commit(self, commit: ActionCommit) -> None:
        """Absorbs one commit into the run trace for ``commit.run_id``.

        Args:
            commit: The kernel ``ActionCommit`` to process.

        """
        if commit.run_id not in self._traces:
            self._traces[commit.run_id] = RunTrace(run_id=commit.run_id)
        self._traces[commit.run_id].absorb(commit)

    def get(self, run_id: str) -> RunTrace | None:
        """Return the accumulated trace for one run, or ``None``.

        Args:
            run_id: Run identifier to look up.

        Returns:
            Accumulated ``RunTrace``, or ``None`` when no commits received.

        """
        return self._traces.get(run_id)

    def all(self) -> list[RunTrace]:
        """Return all accumulated traces sorted by run_id.

        Returns:
            Sorted list of all ``RunTrace`` objects.

        """
        return sorted(self._traces.values(), key=lambda t: t.run_id)

    def terminal_runs(self) -> list[RunTrace]:
        """Return only traces where a terminal event was observed.

        Returns:
            List of ``RunTrace`` objects with ``is_terminal=True``.

        """
        return [t for t in self._traces.values() if t.is_terminal]

    def failed_runs(self) -> list[RunTrace]:
        """Return traces that had at least one recovery_pending turn.

        Returns:
            List of ``RunTrace`` objects with ``failure_count > 0``.

        """
        return [t for t in self._traces.values() if t.failure_count > 0]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_turn_trace(commit: ActionCommit, event_types: list[str]) -> TurnTrace:
    """Build one ``TurnTrace`` from a commit that carries an action.

    Args:
        commit: Action commit with a non-None ``action``.
        event_types: Flattened list of event_type strings from commit events.

    Returns:
        Structured turn trace for the dispatched action.

    """
    action = commit.action
    assert action is not None  # guaranteed by caller

    outcome_kind = _infer_outcome_kind(event_types)
    recovery_mode = _infer_recovery_mode(event_types, commit.events)
    host_kind = _infer_host_kind(commit.events)
    dedupe_outcome = _infer_dedupe_outcome(event_types)

    return TurnTrace(
        action_id=action.action_id,
        action_type=action.action_type,
        effect_class=action.effect_class,
        host_kind=host_kind,
        outcome_kind=outcome_kind,
        recovery_mode=recovery_mode,
        turn_event_types=event_types,
        committed_at=commit.created_at,
        dedupe_outcome=dedupe_outcome,
    )


def _infer_outcome_kind(event_types: list[str]) -> str | None:
    """Derive macro outcome from ordered event type list.

    Priority: recovery_pending > dispatched > blocked > noop.

    Args:
        event_types: Ordered event_type strings for the turn.

    Returns:
        Outcome kind string or ``None`` when undetermined.

    """
    if "turn.recovery_pending" in event_types or "turn.effect_unknown" in event_types:
        return "recovery_pending"
    if (
        "turn.dispatch_acknowledged" in event_types
        or "turn.effect_recorded" in event_types
        or "turn.dispatched" in event_types
    ):
        return "dispatched"
    if "turn.dispatch_blocked" in event_types:
        return "blocked"
    if "turn.completed_noop" in event_types:
        return "noop"
    return None


def _infer_dedupe_outcome(event_types: list[str]) -> str | None:
    """Infers DedupeStore reservation outcome from turn event types.

    ``turn.dispatched`` present 鈫?``"accepted"`` (or ``"degraded"`` when
    ``turn.dedupe_degraded`` is also present).  ``turn.dispatch_blocked``
    without a prior ``turn.dispatched`` 鈫?``"duplicate"``.  Otherwise ``None``.

    Args:
        event_types: Ordered event_type strings for the turn.

    Returns:
        Dedupe outcome string or ``None`` when not determinable.

    """
    if "turn.dispatched" in event_types:
        if "turn.dedupe_degraded" in event_types:
            return "degraded"
        return "accepted"
    if "turn.dispatch_blocked" in event_types:
        # Blocked before dispatch 鈫?treated as duplicate reservation.
        return "duplicate"
    return None


def _infer_recovery_mode(
    event_types: list[str],
    events: list[RuntimeEvent],
) -> str | None:
    """Extract recovery mode from ``recovery.plan_selected`` payload if present.

    Falls back to inferring from lifecycle event type when the diagnostic event
    is absent (e.g. in legacy commits written before the plan_selected event
    was introduced).

    Args:
        event_types: Flat list of event_type strings.
        events: Full list of ``RuntimeEvent`` objects from the commit.

    Returns:
        Recovery mode string or ``None`` when not a recovery turn.

    """
    # Prefer the structured diagnostic event payload
    for event in events:
        if event.event_type == "recovery.plan_selected" and isinstance(event.payload_json, dict):
            planned_mode = event.payload_json.get("planned_mode")
            if isinstance(planned_mode, str):
                return planned_mode

    # Legacy fallback: infer from lifecycle event type
    if "run.waiting_external" in event_types or "run.waiting_human_input" in event_types:
        return "human_escalation"
    if "run.recovery_aborted" in event_types:
        return "abort"
    if "run.recovery_succeeded" in event_types:
        return "static_compensation"
    return None


def _infer_host_kind(events: list[RuntimeEvent]) -> str | None:
    """Extract host_kind from turn outcome payload when present.

    Args:
        events: Full list of ``RuntimeEvent`` objects from the commit.

    Returns:
        ``host_kind`` string or ``None`` when not recorded.

    """
    for event in events:
        if event.event_type in (
            "turn.dispatched",
            "turn.dispatch_acknowledged",
            "turn.effect_unknown",
        ) and isinstance(event.payload_json, dict):
            host_kind = event.payload_json.get("host_kind")
            if isinstance(host_kind, str):
                return host_kind
    return None
