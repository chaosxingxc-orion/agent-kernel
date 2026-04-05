"""Saga-pattern reconciler for EventLog 鈫?DedupeStore consistency drift.

When a process crash leaves a dedupe key in ``"reserved"`` or ``"dispatched"``
state with no corresponding event in the event log, the :class:`DispatchOutboxReconciler`
repairs the inconsistency by transitioning the key to ``"unknown_effect"``.
This surfaces the ambiguous dispatch for recovery without silent loss.

For ``"unknown_effect"`` entries that already lack log evidence the reconciler
logs a WARNING for human review 鈥?auto-repair is not possible because the
outcome is genuinely unknown.

Typical usage inside an async startup or watchdog path::

    reconciler = DispatchOutboxReconciler()
    result = await reconciler.reconcile(event_log, dedupe_store, run_id)
    if not result.is_clean:
        logger.warning("Reconciliation repaired %d violations", result.violations_repaired)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from agent_kernel.kernel.dedupe_store import DedupeStoreStateError
from agent_kernel.kernel.persistence.consistency import (
    averify_event_dedupe_consistency,
    verify_event_dedupe_consistency,
)

_DEFAULT_LOGGER = logging.getLogger(__name__)

# States where no forward transition is possible 鈥?skip silently.
_TERMINAL_STATES = frozenset({"acknowledged", "unknown_effect"})

# States that can be transitioned to "unknown_effect" during reconciliation.
_REPAIRABLE_STATES = frozenset({"reserved", "dispatched"})


@dataclass(frozen=True, slots=True)
class ReconciliationAction:
    """One repair action taken during reconciliation.

    Attributes:
        idempotency_key: The dispatch idempotency key that was examined.
        violation_kind: Machine-readable violation category, one of
            ``"orphaned_dedupe_key"`` or ``"unknown_effect_no_log_evidence"``.
        action_taken: Outcome of the repair attempt: ``"marked_unknown_effect"``,
            ``"logged_for_review"``, or ``"skipped"``.
        detail: Human-readable description of what was done and why.

    """

    idempotency_key: str
    violation_kind: str
    action_taken: str
    detail: str


@dataclass(slots=True)
class ReconciliationResult:
    """Aggregate outcome of one reconciliation pass for a single run.

    Attributes:
        run_id: Run identifier that was reconciled.
        actions: Ordered list of :class:`ReconciliationAction` records, one per
            violation examined.
        violations_found: Total number of violations detected by the consistency
            checker.
        violations_repaired: Number of violations where ``action_taken`` is not
            ``"skipped"`` (i.e. a concrete action was taken).

    """

    run_id: str
    actions: list[ReconciliationAction] = field(default_factory=list)
    violations_found: int = 0
    violations_repaired: int = 0

    @property
    def is_clean(self) -> bool:
        """``True`` when no violations were detected.

        Returns:
            bool:

        """
        return self.violations_found == 0


class DispatchOutboxReconciler:
    """Saga-pattern reconciler for EventLog 鈫?DedupeStore drift.

    For each violation found by :func:`~consistency.verify_event_dedupe_consistency`:

    - **orphaned_dedupe_key in state "reserved" or "dispatched"**: transition
      the key to ``"unknown_effect"`` via :meth:`~DedupeStore.mark_unknown_effect`.
      This surfaces the ambiguous side-effect for the recovery layer without
      silent loss.
    - **orphaned_dedupe_key already in "unknown_effect" or "acknowledged"**:
      the key is already in a terminal state; log at DEBUG and record
      ``action_taken="skipped"``.
    - **unknown_effect_no_log_evidence**: the dedupe entry is ``"unknown_effect"``
      but the event log has no dispatch evidence.  Auto-repair is not possible;
      log at WARNING for human review and record ``action_taken="logged_for_review"``.

    :class:`DedupeStoreStateError` raised during repair (e.g. due to a concurrent
    writer) is caught gracefully; the action is recorded as ``"skipped"`` and
    reconciliation continues.
    """

    def __init__(
        self,
        logger: logging.Logger | None = None,
        *,
        event_schema_migrator: Any | None = None,
        target_event_schema_version: str | None = None,
    ) -> None:
        """Initialise the reconciler with an optional logger.

        Args:
            logger: Logger to use.  Defaults to the module-level logger when
                ``None``.

        """
        self._log = logger if logger is not None else _DEFAULT_LOGGER
        self._event_schema_migrator = event_schema_migrator
        self._target_event_schema_version = target_event_schema_version

    async def reconcile(
        self,
        event_log: Any,
        dedupe_store: Any,
        run_id: str,
    ) -> ReconciliationResult:
        """Detect and repairs EventLog 鈫?DedupeStore inconsistencies (async).

        Runs :func:`~consistency.averify_event_dedupe_consistency` then repairs
        each violation in turn.

        Args:
            event_log: Any ``KernelRuntimeEventLog``-compatible object with an
                async ``load(run_id)`` method.
            dedupe_store: Any ``DedupeStore``-compatible object.
            run_id: Run identifier to scope the check and repair to.

        Returns:
            :class:`ReconciliationResult` describing all actions taken.

        """
        report = await averify_event_dedupe_consistency(
            self._event_log_with_schema_migration(event_log),
            dedupe_store,
            run_id,
        )
        return self._apply_repairs(dedupe_store, run_id, report)

    def reconcile_sync(
        self,
        event_log: Any,
        dedupe_store: Any,
        run_id: str,
    ) -> ReconciliationResult:
        """Detect and repairs EventLog 鈫?DedupeStore inconsistencies (sync).

        Thin wrapper around :func:`~consistency.verify_event_dedupe_consistency`
        for callers that cannot use ``await``.

        Args:
            event_log: Any ``KernelRuntimeEventLog``-compatible object.  The
                sync checker will use ``list_events()``, ``_events``, or fall
                back to ``asyncio.run(load(run_id))``.
            dedupe_store: Any ``DedupeStore``-compatible object.
            run_id: Run identifier to scope the check and repair to.

        Returns:
            :class:`ReconciliationResult` describing all actions taken.

        """
        report = verify_event_dedupe_consistency(
            self._event_log_with_schema_migration(event_log),
            dedupe_store,
            run_id,
        )
        return self._apply_repairs(dedupe_store, run_id, report)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _apply_repairs(
        self,
        dedupe_store: Any,
        run_id: str,
        report: Any,
    ) -> ReconciliationResult:
        """Apply repair actions for all violations in *report*.

        Args:
            dedupe_store: DedupeStore to mutate during repair.
            run_id: Run identifier (used for logging only).
            report: :class:`~consistency.ConsistencyReport` produced by the
                checker.

        Returns:
            Populated :class:`ReconciliationResult`.

        """
        result = ReconciliationResult(run_id=run_id)
        result.violations_found = len(report.violations)

        for violation in report.violations:
            key = violation.idempotency_key or ""
            action = self._repair_violation(dedupe_store, run_id, key, violation)
            result.actions.append(action)
            if action.action_taken != "skipped":
                result.violations_repaired += 1

        return result

    def _repair_violation(
        self,
        dedupe_store: Any,
        run_id: str,
        key: str,
        violation: Any,
    ) -> ReconciliationAction:
        """Produce a single :class:`ReconciliationAction` for one violation.

        Args:
            dedupe_store: DedupeStore to mutate.
            run_id: Run identifier for log messages.
            key: Idempotency key from the violation.
            violation: :class:`~consistency.ConsistencyViolation` to repair.

        Returns:
            A completed :class:`ReconciliationAction`.

        """
        kind = violation.kind

        if kind == "orphaned_dedupe_key":
            return self._repair_orphaned_key(dedupe_store, run_id, key, violation)

        if kind == "unknown_effect_no_log_evidence":
            return self._repair_unknown_effect_no_evidence(run_id, key, violation)

        # Unknown violation kind 鈥?skip conservatively.
        self._log.debug(
            "Reconciler skipping unknown violation kind %r for key %r in run %r.",
            kind,
            key,
            run_id,
        )
        return ReconciliationAction(
            idempotency_key=key,
            violation_kind=kind,
            action_taken="skipped",
            detail=f"Unknown violation kind {kind!r}; skipped conservatively.",
        )

    def _repair_orphaned_key(
        self,
        dedupe_store: Any,
        run_id: str,
        key: str,
        violation: Any,
    ) -> ReconciliationAction:
        """Repairs an orphaned dedupe key by transitioning it to unknown_effect.

        If the key is already in a terminal state (``"unknown_effect"`` or
        ``"acknowledged"``) the action is recorded as ``"skipped"``.  If the
        :meth:`~DedupeStore.mark_unknown_effect` call raises
        :exc:`~dedupe_store.DedupeStoreStateError` (race condition with another
        writer) the action is also recorded as ``"skipped"``.

        Args:
            dedupe_store: DedupeStore to mutate.
            run_id: Run identifier for log messages.
            key: Idempotency key to repair.
            violation: Source violation (used for ``dedupe_state`` fallback).

        Returns:
            :class:`ReconciliationAction` with the outcome.

        """
        record = dedupe_store.get(key)
        current_state = record.state if record is not None else violation.dedupe_state

        if current_state in _TERMINAL_STATES:
            self._log.debug(
                "Reconciler skipping orphaned key %r (state=%r) in run %r 鈥?already terminal.",
                key,
                current_state,
                run_id,
            )
            return ReconciliationAction(
                idempotency_key=key,
                violation_kind="orphaned_dedupe_key",
                action_taken="skipped",
                detail=(
                    f"Key {key!r} is already in terminal state {current_state!r}; "
                    "no transition required."
                ),
            )

        if current_state not in _REPAIRABLE_STATES:
            # Defensive: unknown state 鈥?skip.
            self._log.warning(
                "Reconciler cannot repair orphaned key %r (state=%r) in run %r 鈥?"
                "state not in repairable set.",
                key,
                current_state,
                run_id,
            )
            return ReconciliationAction(
                idempotency_key=key,
                violation_kind="orphaned_dedupe_key",
                action_taken="skipped",
                detail=(
                    f"Key {key!r} has unrecognised state {current_state!r}; "
                    "cannot determine safe repair action."
                ),
            )

        # Attempt the transition.
        try:
            # mark_unknown_effect only accepts "dispatched" 鈫?"unknown_effect".
            # For "reserved" keys we must first advance to "dispatched".
            if current_state == "reserved":
                dedupe_store.mark_dispatched(key)
            dedupe_store.mark_unknown_effect(key)
        except DedupeStoreStateError as exc:
            self._log.warning(
                "Reconciler could not repair orphaned key %r in run %r (DedupeStoreStateError: %s)",
                key,
                run_id,
                exc,
            )
            return ReconciliationAction(
                idempotency_key=key,
                violation_kind="orphaned_dedupe_key",
                action_taken="skipped",
                detail=f"DedupeStoreStateError during repair: {exc}",
            )

        self._log.info(
            "Reconciler marked orphaned key %r as unknown_effect in run %r (was %r).",
            key,
            run_id,
            current_state,
        )
        return ReconciliationAction(
            idempotency_key=key,
            violation_kind="orphaned_dedupe_key",
            action_taken="marked_unknown_effect",
            detail=(
                f"Key {key!r} was in state {current_state!r} with no event log evidence; "
                "transitioned to 'unknown_effect' for recovery surface."
            ),
        )

    def _repair_unknown_effect_no_evidence(
        self,
        run_id: str,
        key: str,
        violation: Any,
    ) -> ReconciliationAction:
        """Log a WARNING for unknown_effect entries lacking log evidence.

        Auto-repair is not possible because the dispatch outcome is genuinely
        ambiguous.  The action is counted as repaired (``"logged_for_review"``)
        since a concrete action was taken.

        Args:
            run_id: Run identifier for log messages.
            key: Idempotency key affected.
            violation: Source violation (used for the log message).

        Returns:
            :class:`ReconciliationAction` with ``action_taken="logged_for_review"``.

        """
        self._log.warning(
            "Reconciler: key %r in run %r is in state 'unknown_effect' with no "
            "dispatch evidence in the event log. Human review required. Detail: %s",
            key,
            run_id,
            violation.detail,
        )
        return ReconciliationAction(
            idempotency_key=key,
            violation_kind="unknown_effect_no_log_evidence",
            action_taken="logged_for_review",
            detail=(
                f"Key {key!r} is 'unknown_effect' with no event log evidence for run "
                f"{run_id!r}. Logged at WARNING for human review."
            ),
        )

    def _event_log_with_schema_migration(self, event_log: Any) -> Any:
        """Return event-log view that migrates events to target schema on read."""
        if self._event_schema_migrator is None:
            return event_log
        if self._target_event_schema_version is None:
            return event_log

        migrator = self._event_schema_migrator
        target = self._target_event_schema_version

        class _MigratingEventLogView:
            def __init__(self, inner: Any) -> None:
                self._inner = inner

            async def load(self, run_id: str, after_offset: int = 0) -> list[Any]:
                events = await self._inner.load(run_id, after_offset=after_offset)
                return migrator.migrate_batch(list(events), target_version=target)

            def list_events(self) -> list[Any]:
                if hasattr(self._inner, "list_events"):
                    events = self._inner.list_events()
                    return migrator.migrate_batch(list(events), target_version=target)
                if hasattr(self._inner, "_events"):
                    events = list(self._inner._events)
                    return migrator.migrate_batch(events, target_version=target)
                return []

            @property
            def _events(self) -> list[Any]:
                return self.list_events()

        return _MigratingEventLogView(event_log)


@dataclass(frozen=True, slots=True)
class ScheduledReconciliationResult:
    """Summary of one scheduled outbox reconciliation sweep."""

    scanned_run_ids: list[str]
    violations_found: int
    violations_repaired: int
    finished_at_ms: int


class ScheduledOutboxReconciler:
    """Periodic scheduler for outbox consistency reconciliation."""

    def __init__(
        self,
        reconciler: DispatchOutboxReconciler,
        *,
        event_log: Any,
        dedupe_store: Any,
        interval_s: float = 300.0,
        observability_hook: Any = None,
        run_ids_provider: Any | None = None,
    ) -> None:
        """Initialize scheduler.

        Args:
            reconciler: Reconciler instance to execute.
            event_log: Runtime event log.
            dedupe_store: Dedupe store.
            interval_s: Sweep interval in seconds.
            observability_hook: Optional observability hook.
            run_ids_provider: Optional callable returning iterable run ids.
        """
        self._reconciler = reconciler
        self._event_log = event_log
        self._dedupe_store = dedupe_store
        self._interval_s = interval_s
        self._observability_hook = observability_hook
        self._run_ids_provider = run_ids_provider
        self._task: asyncio.Task[Any] | None = None
        self._last_result: ScheduledReconciliationResult | None = None

    @property
    def last_reconciliation_result(self) -> ScheduledReconciliationResult | None:
        """Return most recent reconciliation summary, if any."""
        return self._last_result

    async def reconcile_once(self) -> ScheduledReconciliationResult:
        """Run one reconciliation sweep across discovered run ids."""
        run_ids = list(self._discover_run_ids())
        total_found = 0
        total_repaired = 0
        for run_id in run_ids:
            result = await self._reconciler.reconcile(self._event_log, self._dedupe_store, run_id)
            total_found += result.violations_found
            total_repaired += result.violations_repaired
        summary = ScheduledReconciliationResult(
            scanned_run_ids=run_ids,
            violations_found=total_found,
            violations_repaired=total_repaired,
            finished_at_ms=int(time.time() * 1000),
        )
        self._last_result = summary
        if total_found > 0 and self._observability_hook is not None:
            with contextlib.suppress(Exception):
                self._observability_hook.on_recovery_triggered(
                    run_id="kernel",
                    reason_code="outbox_inconsistency_detected",
                    mode="human_escalation",
                )
        return summary

    def start(self) -> asyncio.Task[Any]:
        """Start background periodic reconciliation task."""
        if self._task is not None and not self._task.done():
            return self._task

        async def _loop() -> None:
            try:
                while True:
                    await asyncio.sleep(self._interval_s)
                    await self.reconcile_once()
            except asyncio.CancelledError:
                return

        self._task = asyncio.get_running_loop().create_task(_loop(), name="outbox_reconciler")
        return self._task

    def _discover_run_ids(self) -> list[str]:
        """Discover run ids from provider or known event-log internals."""
        if callable(self._run_ids_provider):
            provided = self._run_ids_provider()
            if provided is None:
                return []
            return [str(run_id) for run_id in provided]

        if hasattr(self._event_log, "_events_by_run"):
            return [str(run_id) for run_id in self._event_log._events_by_run]

        conn = getattr(self._event_log, "_connection", None)
        if conn is not None:
            try:
                rows = conn.execute("SELECT DISTINCT stream_run_id FROM runtime_events").fetchall()
                return [str(row[0]) for row in rows]
            except Exception:
                return []
        return []
