"""Tests for cross-store consistency verification utilities.

Covers ``verify_event_dedupe_consistency`` and ``averify_event_dedupe_consistency``
for both in-memory and SQLite store implementations.

Design note: ``verify_event_dedupe_consistency`` (sync) uses duck-typing to
extract events.  ``InMemoryKernelRuntimeEventLog`` has no ``list_events()`` or
``_events`` attribute — the sync fallback calls ``asyncio.run()`` which cannot
be used inside an already-running event loop (pytest-asyncio).  Therefore:

- Tests that use ``InMemoryKernelRuntimeEventLog`` call the *async* variant
  ``averify_event_dedupe_consistency``.
- Tests of the *sync* variant use either:
  a) A custom stub that exposes ``list_events()``, or
  b) ``ColocatedSQLiteBundle`` (which the sync checker can query via ``_conn``).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agent_kernel.kernel.contracts import ActionCommit, RuntimeEvent
from agent_kernel.kernel.dedupe_store import IdempotencyEnvelope, InMemoryDedupeStore
from agent_kernel.kernel.minimal_runtime import InMemoryKernelRuntimeEventLog
from agent_kernel.kernel.persistence.consistency import (
    ConsistencyReport,
    ConsistencyViolation,
    averify_event_dedupe_consistency,
    verify_event_dedupe_consistency,
)
from agent_kernel.kernel.persistence.sqlite_colocated_bundle import ColocatedSQLiteBundle

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def _make_event(
    run_id: str,
    event_type: str = "run.ready",
    idempotency_key: str | None = None,
    commit_offset: int = 1,
) -> RuntimeEvent:
    return RuntimeEvent(
        run_id=run_id,
        event_id=f"evt-{commit_offset}",
        commit_offset=commit_offset,
        event_type=event_type,
        event_class="fact",
        event_authority="authoritative_fact",
        ordering_key=run_id,
        wake_policy="wake_actor",
        created_at=_utc_now(),
        idempotency_key=idempotency_key,
    )


def _make_commit(
    run_id: str,
    events: list[RuntimeEvent],
    commit_id: str = "commit-1",
) -> ActionCommit:
    return ActionCommit(
        run_id=run_id,
        commit_id=commit_id,
        created_at=_utc_now(),
        events=events,
    )


def _make_envelope(run_id: str, key_suffix: str = "action-1") -> IdempotencyEnvelope:
    return IdempotencyEnvelope(
        dispatch_idempotency_key=f"{run_id}:{key_suffix}",
        operation_fingerprint="fp-abc123",
        attempt_seq=1,
        effect_scope="idempotent_write",
        capability_snapshot_hash="snap-hash",
        host_kind="in_process_python",
    )


class _ListEventsEventLog:
    """Minimal synchronous event log stub exposing ``list_events()``."""

    def __init__(self) -> None:
        self._store: list[RuntimeEvent] = []

    def add(self, event: RuntimeEvent) -> None:
        self._store.append(event)

    def list_events(self) -> list[RuntimeEvent]:
        return list(self._store)


# ---------------------------------------------------------------------------
# TestConsistencyReport
# ---------------------------------------------------------------------------


class TestConsistencyReport:
    """Tests for ConsistencyReport dataclass behaviour."""

    def test_is_consistent_when_no_violations(self) -> None:
        report = ConsistencyReport(run_id="run-1")
        assert report.is_consistent is True

    def test_is_not_consistent_when_violations_present(self) -> None:
        violation = ConsistencyViolation(
            kind="orphaned_dedupe_key",
            idempotency_key="run-1:k1",
            dedupe_state="reserved",
            event_count=0,
            detail="test violation",
        )
        report = ConsistencyReport(run_id="run-1", violations=[violation])
        assert report.is_consistent is False

    def test_default_counts_are_zero(self) -> None:
        report = ConsistencyReport(run_id="run-1")
        assert report.events_checked == 0
        assert report.dedupe_keys_checked == 0

    def test_run_id_preserved(self) -> None:
        report = ConsistencyReport(run_id="my-run")
        assert report.run_id == "my-run"

    def test_violations_list_mutable(self) -> None:
        """ConsistencyReport.violations can be appended to (not frozen)."""
        report = ConsistencyReport(run_id="r")
        v = ConsistencyViolation(
            kind="orphaned_dedupe_key",
            idempotency_key="k",
            dedupe_state="reserved",
            event_count=0,
            detail="d",
        )
        report.violations.append(v)
        assert not report.is_consistent


# ---------------------------------------------------------------------------
# TestConsistencyViolationFrozen
# ---------------------------------------------------------------------------


class TestConsistencyViolationFrozen:
    """Verifies ConsistencyViolation is a frozen dataclass."""

    def test_fields_accessible(self) -> None:
        v = ConsistencyViolation(
            kind="orphaned_dedupe_key",
            idempotency_key="run-1:k",
            dedupe_state="reserved",
            event_count=0,
            detail="crash window",
        )
        assert v.kind == "orphaned_dedupe_key"
        assert v.idempotency_key == "run-1:k"
        assert v.dedupe_state == "reserved"
        assert v.event_count == 0

    def test_mutation_raises(self) -> None:
        from dataclasses import FrozenInstanceError

        v = ConsistencyViolation(
            kind="orphaned_dedupe_key",
            idempotency_key="k",
            dedupe_state="reserved",
            event_count=0,
            detail="x",
        )
        with pytest.raises(FrozenInstanceError):
            v.kind = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# TestVerifyEventDedupeConsistencySync  (uses list_events stub + SQLite)
# ---------------------------------------------------------------------------


class TestVerifyEventDedupeConsistencySync:
    """Tests for the synchronous ``verify_event_dedupe_consistency`` function."""

    def test_clean_report_no_violations_list_events(self) -> None:
        """No violations when dedupe key has matching event via list_events() path."""
        run_id = "run-sync-clean"
        key = f"{run_id}:action-1"
        log = _ListEventsEventLog()
        dedupe = InMemoryDedupeStore()
        envelope = _make_envelope(run_id, "action-1")
        dedupe.reserve(envelope)
        dedupe.mark_dispatched(key)
        dedupe.mark_acknowledged(key)
        log.add(_make_event(run_id, idempotency_key=key))

        report = verify_event_dedupe_consistency(log, dedupe, run_id)
        assert report.is_consistent is True

    def test_events_checked_count_via_list_events(self) -> None:
        """events_checked reflects event count when list_events() is used."""
        run_id = "run-sync-cnt"
        log = _ListEventsEventLog()
        dedupe = InMemoryDedupeStore()
        for i in range(4):
            log.add(_make_event(run_id, commit_offset=i + 1))

        report = verify_event_dedupe_consistency(log, dedupe, run_id)
        assert report.events_checked == 4

    def test_orphaned_dedupe_key_detected_via_list_events(self) -> None:
        """Orphaned key detected when event_log has list_events() but no matching event."""
        run_id = "run-sync-orphan"
        log = _ListEventsEventLog()
        dedupe = InMemoryDedupeStore()
        envelope = _make_envelope(run_id, "orphan-k")
        dedupe.reserve(envelope)

        report = verify_event_dedupe_consistency(log, dedupe, run_id)
        assert not report.is_consistent
        assert report.violations[0].kind == "orphaned_dedupe_key"

    def test_unknown_effect_no_evidence_via_list_events(self) -> None:
        """unknown_effect with only non-dispatch events raises violation via list_events."""
        run_id = "run-sync-unk"
        key = f"{run_id}:unk-k"
        log = _ListEventsEventLog()
        dedupe = InMemoryDedupeStore()
        envelope = _make_envelope(run_id, "unk-k")
        dedupe.reserve(envelope)
        dedupe.mark_dispatched(key)
        dedupe.mark_unknown_effect(key)
        log.add(_make_event(run_id, event_type="run.ready", idempotency_key=key))

        report = verify_event_dedupe_consistency(log, dedupe, run_id)
        assert not report.is_consistent
        assert any(v.kind == "unknown_effect_no_log_evidence" for v in report.violations)

    def test_unknown_effect_with_dispatched_event_is_clean_via_list_events(self) -> None:
        """turn.dispatched event clears unknown_effect violation."""
        run_id = "run-sync-unk-ok"
        key = f"{run_id}:ok-k"
        log = _ListEventsEventLog()
        dedupe = InMemoryDedupeStore()
        envelope = _make_envelope(run_id, "ok-k")
        dedupe.reserve(envelope)
        dedupe.mark_dispatched(key)
        dedupe.mark_unknown_effect(key)
        log.add(_make_event(run_id, event_type="turn.dispatched", idempotency_key=key))

        report = verify_event_dedupe_consistency(log, dedupe, run_id)
        assert report.is_consistent

    def test_dedupe_keys_checked_count_via_in_memory(self) -> None:
        """dedupe_keys_checked reflects number of keys in InMemoryDedupeStore."""
        run_id = "run-keycnt-sync"
        log = _ListEventsEventLog()
        dedupe = InMemoryDedupeStore()
        for i in range(3):
            env = _make_envelope(run_id, f"act-{i}")
            dedupe.reserve(env)
            log.add(
                _make_event(
                    run_id,
                    idempotency_key=env.dispatch_idempotency_key,
                    commit_offset=i + 1,
                )
            )

        report = verify_event_dedupe_consistency(log, dedupe, run_id)
        assert report.dedupe_keys_checked == 3

    def test_empty_stores_clean(self) -> None:
        """Empty log and dedupe store produce a clean report with zero counts."""
        log = _ListEventsEventLog()
        dedupe = InMemoryDedupeStore()
        report = verify_event_dedupe_consistency(log, dedupe, "run-empty-sync")
        assert report.is_consistent
        assert report.events_checked == 0
        assert report.dedupe_keys_checked == 0

    def test_violation_detail_contains_run_id(self) -> None:
        """Violation detail string must mention the affected run_id."""
        run_id = "run-detail-sync"
        log = _ListEventsEventLog()
        dedupe = InMemoryDedupeStore()
        envelope = _make_envelope(run_id, "orphan-detail")
        dedupe.reserve(envelope)

        report = verify_event_dedupe_consistency(log, dedupe, run_id)
        assert report.violations
        assert run_id in report.violations[0].detail

    def test_keys_from_different_run_ignored(self) -> None:
        """Keys for another run are not included in the checked run's count."""
        log = _ListEventsEventLog()
        dedupe = InMemoryDedupeStore()
        run_a = "run-A-sync"
        run_b = "run-B-sync"
        env_a = _make_envelope(run_a, "act-a")
        dedupe.reserve(env_a)
        log.add(_make_event(run_a, idempotency_key=env_a.dispatch_idempotency_key))

        report = verify_event_dedupe_consistency(log, dedupe, run_b)
        assert report.is_consistent
        assert report.dedupe_keys_checked == 0


# ---------------------------------------------------------------------------
# TestVerifyEventDedupeConsistencyColocated (SQLite via _conn strategy)
# ---------------------------------------------------------------------------


class TestVerifyEventDedupeConsistencyColocated:
    """Consistency checks using the SQLite ColocatedSQLiteBundle."""

    def _make_bundle(self) -> ColocatedSQLiteBundle:
        return ColocatedSQLiteBundle(":memory:")

    @pytest.mark.asyncio
    async def test_clean_after_atomic_dispatch(self) -> None:
        """Consistency check is clean after a successful atomic_dispatch_record."""
        bundle = self._make_bundle()
        run_id = "run-colocated-clean"
        key = f"{run_id}:act-1"
        envelope = _make_envelope(run_id, "act-1")
        event = _make_event(run_id, event_type="turn.dispatched", idempotency_key=key)
        commit = _make_commit(run_id, [event])
        bundle.atomic_dispatch_record(commit, envelope)

        report = await averify_event_dedupe_consistency(
            bundle.event_log, bundle.dedupe_store, run_id
        )
        assert report.is_consistent

        bundle.close()

    @pytest.mark.asyncio
    async def test_orphaned_key_detected_in_sqlite(self) -> None:
        """Consistency check detects orphaned key in SQLite dedupe store."""
        bundle = self._make_bundle()
        run_id = "run-sqlite-orphan"
        envelope = _make_envelope(run_id, "orphan-sqlite")
        bundle.dedupe_store.reserve(envelope)

        report = await averify_event_dedupe_consistency(
            bundle.event_log, bundle.dedupe_store, run_id
        )
        assert not report.is_consistent
        assert report.violations[0].kind == "orphaned_dedupe_key"

        bundle.close()

    @pytest.mark.asyncio
    async def test_dedupe_keys_checked_count_sqlite(self) -> None:
        """dedupe_keys_checked is accurate for SQLite strategy."""
        bundle = self._make_bundle()
        run_id = "run-sqlite-cnt"
        for i in range(4):
            env = _make_envelope(run_id, f"act-{i}")
            event = _make_event(
                run_id,
                idempotency_key=env.dispatch_idempotency_key,
                commit_offset=i + 1,
            )
            commit = _make_commit(run_id, [event], commit_id=f"c-{i}")
            bundle.atomic_dispatch_record(commit, env)

        report = await averify_event_dedupe_consistency(
            bundle.event_log, bundle.dedupe_store, run_id
        )
        assert report.dedupe_keys_checked == 4
        assert report.events_checked == 4

        bundle.close()

    def test_sync_checker_with_sqlite_conn_strategy(self) -> None:
        """Sync verify uses _conn attribute to query colocated_dedupe_store table."""
        bundle = self._make_bundle()
        run_id = "run-sync-sqlite"
        key = f"{run_id}:s-act"
        envelope = _make_envelope(run_id, "s-act")
        event = _make_event(run_id, event_type="turn.dispatched", idempotency_key=key)
        bundle.atomic_dispatch_record(_make_commit(run_id, [event]), envelope)

        # Sync checker uses _conn to enumerate dedupe keys (strategy 1),
        # then asyncio.run() to load events — but asyncio.run() works fine outside
        # an async test, so we call this from a regular (non-async) test method.
        report = verify_event_dedupe_consistency(bundle.event_log, bundle.dedupe_store, run_id)
        # Key count should be 1 (strategy 1 worked).
        assert report.dedupe_keys_checked == 1

        bundle.close()


# ---------------------------------------------------------------------------
# TestAverifyEventDedupeConsistency
# ---------------------------------------------------------------------------


class TestAverifyEventDedupeConsistency:
    """Tests for the async variant of verify_event_dedupe_consistency."""

    @pytest.mark.asyncio
    async def test_async_clean_report(self) -> None:
        """averify_event_dedupe_consistency returns clean report for consistent state."""
        log = InMemoryKernelRuntimeEventLog()
        dedupe = InMemoryDedupeStore()
        run_id = "run-async-clean"
        key = f"{run_id}:async-act"
        envelope = _make_envelope(run_id, "async-act")
        dedupe.reserve(envelope)
        dedupe.mark_dispatched(key)

        event = _make_event(run_id, event_type="turn.dispatched", idempotency_key=key)
        await log.append_action_commit(_make_commit(run_id, [event]))

        report = await averify_event_dedupe_consistency(log, dedupe, run_id)
        assert report.is_consistent

    @pytest.mark.asyncio
    async def test_async_orphaned_key_detected(self) -> None:
        """averify detects orphaned dedupe key."""
        log = InMemoryKernelRuntimeEventLog()
        dedupe = InMemoryDedupeStore()
        run_id = "run-async-orphan"
        envelope = _make_envelope(run_id, "orphaned")
        dedupe.reserve(envelope)

        report = await averify_event_dedupe_consistency(log, dedupe, run_id)
        assert not report.is_consistent
        assert report.violations[0].kind == "orphaned_dedupe_key"

    @pytest.mark.asyncio
    async def test_async_events_checked_count(self) -> None:
        """averify counts events correctly via async load()."""
        log = InMemoryKernelRuntimeEventLog()
        dedupe = InMemoryDedupeStore()
        run_id = "run-async-cnt"
        events = [_make_event(run_id, commit_offset=i + 1) for i in range(3)]
        await log.append_action_commit(_make_commit(run_id, events))

        report = await averify_event_dedupe_consistency(log, dedupe, run_id)
        assert report.events_checked == 3

    @pytest.mark.asyncio
    async def test_async_unknown_effect_no_evidence_detected(self) -> None:
        """averify detects unknown_effect without dispatch evidence."""
        log = InMemoryKernelRuntimeEventLog()
        dedupe = InMemoryDedupeStore()
        run_id = "run-async-unk"
        key = f"{run_id}:unk-act"
        envelope = _make_envelope(run_id, "unk-act")
        dedupe.reserve(envelope)
        dedupe.mark_dispatched(key)
        dedupe.mark_unknown_effect(key)

        event = _make_event(run_id, event_type="run.ready", idempotency_key=key)
        await log.append_action_commit(_make_commit(run_id, [event]))

        report = await averify_event_dedupe_consistency(log, dedupe, run_id)
        assert not report.is_consistent
        assert any(v.kind == "unknown_effect_no_log_evidence" for v in report.violations)

    @pytest.mark.asyncio
    async def test_async_unknown_effect_with_dispatched_event_is_clean(self) -> None:
        """unknown_effect + turn.dispatched event is clean via async variant."""
        log = InMemoryKernelRuntimeEventLog()
        dedupe = InMemoryDedupeStore()
        run_id = "run-async-unk-ok"
        key = f"{run_id}:ok-act"
        envelope = _make_envelope(run_id, "ok-act")
        dedupe.reserve(envelope)
        dedupe.mark_dispatched(key)
        dedupe.mark_unknown_effect(key)

        event = _make_event(run_id, event_type="turn.dispatched", idempotency_key=key)
        await log.append_action_commit(_make_commit(run_id, [event]))

        report = await averify_event_dedupe_consistency(log, dedupe, run_id)
        assert report.is_consistent

    @pytest.mark.asyncio
    async def test_async_unknown_effect_with_effect_unknown_is_clean(self) -> None:
        """unknown_effect + turn.effect_unknown event is clean via async variant."""
        log = InMemoryKernelRuntimeEventLog()
        dedupe = InMemoryDedupeStore()
        run_id = "run-async-eu"
        key = f"{run_id}:eu-act"
        envelope = _make_envelope(run_id, "eu-act")
        dedupe.reserve(envelope)
        dedupe.mark_dispatched(key)
        dedupe.mark_unknown_effect(key)

        event = _make_event(run_id, event_type="turn.effect_unknown", idempotency_key=key)
        await log.append_action_commit(_make_commit(run_id, [event]))

        report = await averify_event_dedupe_consistency(log, dedupe, run_id)
        assert report.is_consistent

    @pytest.mark.asyncio
    async def test_async_returns_consistency_report_type(self) -> None:
        """averify always returns a ConsistencyReport instance."""
        log = InMemoryKernelRuntimeEventLog()
        dedupe = InMemoryDedupeStore()
        report = await averify_event_dedupe_consistency(log, dedupe, "any-run")
        assert isinstance(report, ConsistencyReport)

    @pytest.mark.asyncio
    async def test_async_run_id_preserved(self) -> None:
        """averify sets report.run_id to the passed run_id."""
        log = InMemoryKernelRuntimeEventLog()
        dedupe = InMemoryDedupeStore()
        run_id = "run-id-check"
        report = await averify_event_dedupe_consistency(log, dedupe, run_id)
        assert report.run_id == run_id

    @pytest.mark.asyncio
    async def test_async_multiple_keys_mixed_violations(self) -> None:
        """Reports violations only for the inconsistent keys."""
        log = InMemoryKernelRuntimeEventLog()
        dedupe = InMemoryDedupeStore()
        run_id = "run-async-mixed"
        # Key 1: clean.
        env1 = _make_envelope(run_id, "k1")
        dedupe.reserve(env1)
        ev1 = _make_event(
            run_id, idempotency_key=env1.dispatch_idempotency_key, commit_offset=1
        )
        await log.append_action_commit(_make_commit(run_id, [ev1], commit_id="c1"))
        # Key 2: orphaned.
        env2 = _make_envelope(run_id, "k2")
        dedupe.reserve(env2)

        report = await averify_event_dedupe_consistency(log, dedupe, run_id)
        assert len(report.violations) == 1
        assert report.violations[0].idempotency_key == env2.dispatch_idempotency_key

    @pytest.mark.asyncio
    async def test_async_empty_log_and_dedupe_clean(self) -> None:
        """Empty stores produce clean report with zero counts via async variant."""
        log = InMemoryKernelRuntimeEventLog()
        dedupe = InMemoryDedupeStore()
        report = await averify_event_dedupe_consistency(log, dedupe, "run-async-empty")
        assert report.is_consistent
        assert report.events_checked == 0
        assert report.dedupe_keys_checked == 0

    @pytest.mark.asyncio
    async def test_async_keys_from_different_run_ignored(self) -> None:
        """Keys from another run are not counted in the checked run."""
        log = InMemoryKernelRuntimeEventLog()
        dedupe = InMemoryDedupeStore()
        run_a = "run-A-async"
        run_b = "run-B-async"
        env_a = _make_envelope(run_a, "act-a")
        dedupe.reserve(env_a)
        ev_a = _make_event(run_a, idempotency_key=env_a.dispatch_idempotency_key)
        await log.append_action_commit(_make_commit(run_a, [ev_a]))

        report = await averify_event_dedupe_consistency(log, dedupe, run_b)
        assert report.is_consistent
        assert report.dedupe_keys_checked == 0
