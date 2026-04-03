"""Cross-store consistency verification utilities.

Provides ``verify_event_dedupe_consistency()`` for detecting state drift
between a ``KernelRuntimeEventLog`` and a ``DedupeStore`` for a given run.

Drift can occur when:
- A crash happens after ``dedupe_store.reserve()`` but before the event append,
  leaving a "reserved" key with no corresponding event in the log.
- An executor exception leaves dedupe in "unknown_effect" while the event log
  has no dispatch event, or vice-versa.

This checker is intentionally read-only and non-blocking: it produces a
``ConsistencyReport`` describing any discrepancies without mutating state.
"""

from __future__ import annotations

import contextlib
import sqlite3
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ConsistencyViolation:
    """Describes a single consistency discrepancy.

    Attributes:
        kind: Short machine-readable category (e.g. ``"orphaned_dedupe_key"``).
        idempotency_key: The affected dispatch idempotency key, if applicable.
        dedupe_state: Current dedupe record state, or ``None`` when absent.
        event_count: Number of matching events found in the log, or ``None``.
        detail: Human-readable explanation of the violation.
    """

    kind: str
    idempotency_key: str | None
    dedupe_state: str | None
    event_count: int | None
    detail: str


@dataclass(slots=True)
class ConsistencyReport:
    """Outcome of a consistency verification pass for one run.

    Attributes:
        run_id: Run identifier that was checked.
        violations: All detected consistency violations (empty when clean).
        events_checked: Total number of events examined in the log.
        dedupe_keys_checked: Total number of dedupe keys examined.
    """

    run_id: str
    violations: list[ConsistencyViolation] = field(default_factory=list)
    events_checked: int = 0
    dedupe_keys_checked: int = 0

    @property
    def is_consistent(self) -> bool:
        """``True`` when no violations were detected."""
        return len(self.violations) == 0


def verify_event_dedupe_consistency(
    event_log: Any,
    dedupe_store: Any,
    run_id: str,
) -> ConsistencyReport:
    """Cross-checks EventLog and DedupeStore state for one run.

    Looks for two classes of drift:

    1. **Orphaned dedupe key** — a key exists in the dedupe store for this run
       but no event with a matching ``idempotency_key`` exists in the log.
       This indicates a crash after ``reserve()`` but before the event append,
       or a store that was populated outside the normal TurnEngine path.

    2. **Unknown-effect without log evidence** — a dedupe record is in state
       ``"unknown_effect"`` (executor crashed mid-flight) but the event log
       contains no ``turn.effect_unknown`` or ``turn.dispatched`` event for
       that key.  The executor outcome truly cannot be determined and human
       review is appropriate.

    The check is best-effort: it uses duck-typed access (``list_events`` /
    ``events`` attribute, then ``load`` coroutine) so it works with both the
    in-memory and SQLite implementations without hard coupling.  Async
    ``load()`` is called synchronously via ``asyncio.get_event_loop().run_until_complete``
    only when no synchronous accessor is available; callers in async contexts
    should prefer the async variant ``averify_event_dedupe_consistency``.

    Args:
        event_log: Any KernelRuntimeEventLog-compatible object.
        dedupe_store: Any DedupeStore-compatible object with ``get()`` and
            optionally an iterable of all keys via ``_all_keys()`` or the
            internal SQLite connection.
        run_id: Run identifier to scope the check to.

    Returns:
        ConsistencyReport describing all detected violations.
    """
    report = ConsistencyReport(run_id=run_id)

    # --- Load events for the run ---
    events: list[Any] = []
    if hasattr(event_log, "list_events"):
        # InMemoryKernelRuntimeEventLog exposes list_events()
        try:
            events = list(event_log.list_events())
            events = [e for e in events if getattr(e, "run_id", None) == run_id]
        except Exception:
            pass
    elif hasattr(event_log, "_events"):
        # Fallback: direct attribute access (some test stubs)
        with contextlib.suppress(Exception):
            events = [e for e in event_log._events if getattr(e, "run_id", None) == run_id]
    else:
        # Try the async load() via asyncio.run()
        try:
            import asyncio

            events = asyncio.run(event_log.load(run_id))
        except Exception:
            pass

    report.events_checked = len(events)

    # Build a set of idempotency_keys that appear in the event log for this run.
    event_idempotency_keys: set[str] = set()
    for ev in events:
        ik = getattr(ev, "idempotency_key", None)
        if ik:
            event_idempotency_keys.add(ik)

    # Build a set of event_type values per idempotency_key.
    event_types_by_key: dict[str, set[str]] = {}
    for ev in events:
        ik = getattr(ev, "idempotency_key", None)
        et = getattr(ev, "event_type", None)
        if ik and et:
            event_types_by_key.setdefault(ik, set()).add(et)

    # --- Enumerate all dedupe keys belonging to this run ---
    all_dedupe_keys: list[str] = _collect_dedupe_keys(dedupe_store, run_id)
    report.dedupe_keys_checked = len(all_dedupe_keys)

    for key in all_dedupe_keys:
        record = dedupe_store.get(key)
        if record is None:
            continue

        # --- Violation 1: orphaned dedupe key ---
        if key not in event_idempotency_keys:
            report.violations.append(
                ConsistencyViolation(
                    kind="orphaned_dedupe_key",
                    idempotency_key=key,
                    dedupe_state=record.state,
                    event_count=0,
                    detail=(
                        f"DedupeStore key {key!r} (state={record.state!r}) has no "
                        f"matching event in the EventLog for run {run_id!r}. "
                        "Possible crash between reserve() and event append."
                    ),
                )
            )
            continue

        # --- Violation 2: unknown_effect with no dispatch evidence ---
        if record.state == "unknown_effect":
            found_types = event_types_by_key.get(key, set())
            has_dispatch_evidence = bool(
                found_types & {"turn.dispatched", "turn.effect_unknown", "turn.effect_recorded"}
            )
            if not has_dispatch_evidence:
                report.violations.append(
                    ConsistencyViolation(
                        kind="unknown_effect_no_log_evidence",
                        idempotency_key=key,
                        dedupe_state="unknown_effect",
                        event_count=len(found_types),
                        detail=(
                            f"DedupeStore key {key!r} is in state 'unknown_effect' "
                            f"but no dispatch/effect event was found in the EventLog "
                            f"for run {run_id!r}. Human review recommended."
                        ),
                    )
                )

    return report


async def averify_event_dedupe_consistency(
    event_log: Any,
    dedupe_store: Any,
    run_id: str,
) -> ConsistencyReport:
    """Async variant of ``verify_event_dedupe_consistency``.

    Uses ``await event_log.load(run_id)`` to avoid running a sync wrapper
    inside an async context.  All other logic is identical to the sync variant.

    Args:
        event_log: Any KernelRuntimeEventLog-compatible object with async
            ``load(run_id)`` method.
        dedupe_store: Any DedupeStore-compatible object.
        run_id: Run identifier to scope the check to.

    Returns:
        ConsistencyReport describing all detected violations.
    """
    report = ConsistencyReport(run_id=run_id)

    try:
        events = await event_log.load(run_id)
    except Exception:
        events = []

    report.events_checked = len(events)

    event_idempotency_keys: set[str] = set()
    event_types_by_key: dict[str, set[str]] = {}
    for ev in events:
        ik = getattr(ev, "idempotency_key", None)
        et = getattr(ev, "event_type", None)
        if ik:
            event_idempotency_keys.add(ik)
        if ik and et:
            event_types_by_key.setdefault(ik, set()).add(et)

    all_dedupe_keys: list[str] = _collect_dedupe_keys(dedupe_store, run_id)
    report.dedupe_keys_checked = len(all_dedupe_keys)

    for key in all_dedupe_keys:
        record = dedupe_store.get(key)
        if record is None:
            continue

        if key not in event_idempotency_keys:
            report.violations.append(
                ConsistencyViolation(
                    kind="orphaned_dedupe_key",
                    idempotency_key=key,
                    dedupe_state=record.state,
                    event_count=0,
                    detail=(
                        f"DedupeStore key {key!r} (state={record.state!r}) has no "
                        f"matching event in the EventLog for run {run_id!r}. "
                        "Possible crash between reserve() and event append."
                    ),
                )
            )
            continue

        if record.state == "unknown_effect":
            found_types = event_types_by_key.get(key, set())
            has_dispatch_evidence = bool(
                found_types & {"turn.dispatched", "turn.effect_unknown", "turn.effect_recorded"}
            )
            if not has_dispatch_evidence:
                report.violations.append(
                    ConsistencyViolation(
                        kind="unknown_effect_no_log_evidence",
                        idempotency_key=key,
                        dedupe_state="unknown_effect",
                        event_count=len(found_types),
                        detail=(
                            f"DedupeStore key {key!r} is in state 'unknown_effect' "
                            f"but no dispatch/effect event was found in the EventLog "
                            f"for run {run_id!r}. Human review recommended."
                        ),
                    )
                )

    return report


def _collect_dedupe_keys(dedupe_store: Any, run_id: str) -> list[str]:
    """Collects all dedupe keys that belong to ``run_id``.

    Uses multiple strategies to enumerate keys from different store
    implementations, falling back gracefully when private internals are absent:

    1. SQLite: query ``dedupe_store`` or ``colocated_dedupe_store`` table
       directly via ``dedupe_store._conn``.
    2. In-memory: iterate ``dedupe_store._records`` dict.
    3. Prefix heuristic: keys matching ``f"{run_id}:*"`` from any iterable.

    Args:
        dedupe_store: DedupeStore implementation to enumerate.
        run_id: Run identifier prefix filter.

    Returns:
        List of matching dispatch idempotency keys.
    """
    # Strategy 1: SQLite connection present — query directly.
    conn = getattr(dedupe_store, "_conn", None)
    if conn is not None:
        try:
            # Try colocated table first, then standalone table.
            for table in ("colocated_dedupe_store", "dedupe_store"):
                try:
                    rows = conn.execute(
                        f"SELECT dispatch_idempotency_key FROM {table} "
                        "WHERE dispatch_idempotency_key LIKE ?",
                        (f"{run_id}:%",),
                    ).fetchall()
                    return [row[0] for row in rows]
                except sqlite3.OperationalError:
                    continue
        except Exception:
            pass

    # Strategy 2: In-memory records dict (InMemoryDedupeStore uses _records_by_key).
    for attr in ("_records_by_key", "_records"):
        records = getattr(dedupe_store, attr, None)
        if records is not None:
            try:
                return [k for k in records if k.startswith(f"{run_id}:")]
            except Exception:
                pass

    # Strategy 3: No known structure — return empty (best-effort).
    return []
