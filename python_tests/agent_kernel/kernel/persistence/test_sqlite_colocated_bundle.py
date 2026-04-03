"""Tests for ColocatedSQLiteBundle — shared SQLite connection for atomic dispatch.

Covers schema initialisation, atomic_dispatch_record, individual store operations,
thread safety, and end-to-end round-trips.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime

import pytest

from agent_kernel.kernel.contracts import ActionCommit, RuntimeEvent
from agent_kernel.kernel.dedupe_store import (
    DedupeRecord,
    DedupeReservation,
    DedupeStoreStateError,
    IdempotencyEnvelope,
)
from agent_kernel.kernel.persistence.consistency import (
    averify_event_dedupe_consistency,
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
        operation_fingerprint="fp-xyz",
        attempt_seq=1,
        effect_scope="idempotent_write",
        capability_snapshot_hash="snap-hash",
        host_kind="in_process_python",
    )


def _bundle() -> ColocatedSQLiteBundle:
    """Returns a fresh in-memory bundle for each test."""
    return ColocatedSQLiteBundle(":memory:")


# ---------------------------------------------------------------------------
# TestColocatedSQLiteBundleSchema
# ---------------------------------------------------------------------------


class TestColocatedSQLiteBundleSchema:
    """Verifies schema initialisation and basic structural invariants."""

    def test_schema_creates_tables(self) -> None:
        """All four expected tables must exist after construction."""
        bundle = _bundle()
        tables = bundle._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        names = {row[0] for row in tables}
        assert "colocated_action_commits" in names
        assert "colocated_runtime_events" in names
        assert "colocated_dedupe_store" in names
        bundle.close()

    def test_schema_creates_index(self) -> None:
        """The stream+offset index must be created."""
        bundle = _bundle()
        indexes = bundle._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
        names = {row[0] for row in indexes}
        assert "idx_colocated_events_stream_offset" in names
        bundle.close()

    def test_event_log_attribute_exists(self) -> None:
        bundle = _bundle()
        assert bundle.event_log is not None
        bundle.close()

    def test_dedupe_store_attribute_exists(self) -> None:
        bundle = _bundle()
        assert bundle.dedupe_store is not None
        bundle.close()

    def test_close_does_not_raise(self) -> None:
        """close() must complete without raising even on empty database."""
        bundle = _bundle()
        bundle.close()  # Should not raise.

    def test_initialize_schema_idempotent(self) -> None:
        """Calling _initialize_schema() a second time must not raise."""
        bundle = _bundle()
        bundle._initialize_schema()  # second call — CREATE TABLE IF NOT EXISTS.
        bundle.close()


# ---------------------------------------------------------------------------
# TestAtomicDispatchRecord
# ---------------------------------------------------------------------------


class TestAtomicDispatchRecord:
    """Tests for the atomic_dispatch_record() method."""

    @pytest.mark.asyncio
    async def test_success_returns_commit_ref_and_accepted_reservation(self) -> None:
        """First dispatch returns a non-empty commit_ref and accepted=True."""
        bundle = _bundle()
        run_id = "run-atomic-ok"
        env = _make_envelope(run_id, "act-1")
        event = _make_event(run_id, idempotency_key=env.dispatch_idempotency_key)
        commit = _make_commit(run_id, [event])

        commit_ref, reservation = bundle.atomic_dispatch_record(commit, env)
        assert reservation.accepted is True
        assert reservation.reason == "accepted"
        assert "commit-ref-" in commit_ref
        bundle.close()

    @pytest.mark.asyncio
    async def test_duplicate_returns_accepted_false(self) -> None:
        """Second call with the same envelope key returns accepted=False."""
        bundle = _bundle()
        run_id = "run-atomic-dup"
        env = _make_envelope(run_id, "act-dup")
        event = _make_event(run_id, idempotency_key=env.dispatch_idempotency_key)
        commit = _make_commit(run_id, [event])

        bundle.atomic_dispatch_record(commit, env)
        _, reservation2 = bundle.atomic_dispatch_record(commit, env)
        assert reservation2.accepted is False
        assert reservation2.reason == "duplicate"
        assert isinstance(reservation2.existing_record, DedupeRecord)
        bundle.close()

    @pytest.mark.asyncio
    async def test_duplicate_does_not_append_second_event(self) -> None:
        """Duplicate atomic dispatch must not append a second event commit."""
        bundle = _bundle()
        run_id = "run-atomic-dup-ev"
        env = _make_envelope(run_id, "act-dup-ev")
        event = _make_event(run_id, idempotency_key=env.dispatch_idempotency_key)
        commit = _make_commit(run_id, [event])

        bundle.atomic_dispatch_record(commit, env)
        bundle.atomic_dispatch_record(commit, env)

        events = await bundle.event_log.load(run_id)
        # Only one event should have been committed.
        assert len(events) == 1
        bundle.close()

    def test_empty_events_raises_value_error(self) -> None:
        """atomic_dispatch_record must raise ValueError for empty events list."""
        bundle = _bundle()
        run_id = "run-empty-events"
        env = _make_envelope(run_id, "act-empty")
        commit = _make_commit(run_id, [])  # empty events

        with pytest.raises(ValueError, match="at least one event"):
            bundle.atomic_dispatch_record(commit, env)
        bundle.close()

    @pytest.mark.asyncio
    async def test_dedupe_record_state_is_reserved_after_atomic(self) -> None:
        """DedupeStore record is in 'reserved' state immediately after atomic dispatch."""
        bundle = _bundle()
        run_id = "run-state-check"
        env = _make_envelope(run_id, "act-state")
        event = _make_event(run_id, idempotency_key=env.dispatch_idempotency_key)
        bundle.atomic_dispatch_record(_make_commit(run_id, [event]), env)

        record = bundle.dedupe_store.get(env.dispatch_idempotency_key)
        assert record is not None
        assert record.state == "reserved"
        bundle.close()

    @pytest.mark.asyncio
    async def test_event_is_persisted_after_atomic(self) -> None:
        """Event appended in atomic_dispatch_record is retrievable via event_log.load."""
        bundle = _bundle()
        run_id = "run-event-persist"
        env = _make_envelope(run_id, "act-persist")
        event = _make_event(
            run_id, event_type="turn.dispatched", idempotency_key=env.dispatch_idempotency_key
        )
        bundle.atomic_dispatch_record(_make_commit(run_id, [event]), env)

        events = await bundle.event_log.load(run_id)
        assert len(events) == 1
        assert events[0].event_type == "turn.dispatched"
        assert events[0].idempotency_key == env.dispatch_idempotency_key
        bundle.close()

    @pytest.mark.asyncio
    async def test_multiple_different_keys_all_accepted(self) -> None:
        """Multiple distinct keys all succeed atomically."""
        bundle = _bundle()
        run_id = "run-multi-keys"
        for i in range(5):
            env = _make_envelope(run_id, f"act-{i}")
            event = _make_event(
                run_id,
                idempotency_key=env.dispatch_idempotency_key,
                commit_offset=i + 1,
            )
            _, reservation = bundle.atomic_dispatch_record(
                _make_commit(run_id, [event], commit_id=f"c-{i}"), env
            )
            assert reservation.accepted is True

        events = await bundle.event_log.load(run_id)
        assert len(events) == 5
        bundle.close()


# ---------------------------------------------------------------------------
# TestEventLogOperations
# ---------------------------------------------------------------------------


class TestEventLogOperations:
    """Tests for the SharedConnectionEventLog (event_log) sub-store."""

    @pytest.mark.asyncio
    async def test_append_action_commit_success(self) -> None:
        """append_action_commit writes events that load() returns."""
        bundle = _bundle()
        run_id = "run-append"
        event = _make_event(run_id)
        commit_ref = await bundle.event_log.append_action_commit(_make_commit(run_id, [event]))
        assert "commit-ref-" in commit_ref
        events = await bundle.event_log.load(run_id)
        assert len(events) == 1
        bundle.close()

    @pytest.mark.asyncio
    async def test_append_empty_events_raises(self) -> None:
        """append_action_commit raises ValueError for empty events."""
        bundle = _bundle()
        run_id = "run-empty"
        with pytest.raises(ValueError, match="at least one event"):
            await bundle.event_log.append_action_commit(_make_commit(run_id, []))
        bundle.close()

    @pytest.mark.asyncio
    async def test_load_returns_events_in_offset_order(self) -> None:
        """load() returns events sorted by commit_offset ascending."""
        bundle = _bundle()
        run_id = "run-order"
        events = [_make_event(run_id, commit_offset=i + 1) for i in range(4)]
        await bundle.event_log.append_action_commit(_make_commit(run_id, events))
        loaded = await bundle.event_log.load(run_id)
        offsets = [e.commit_offset for e in loaded]
        assert offsets == sorted(offsets)
        bundle.close()

    @pytest.mark.asyncio
    async def test_load_after_offset_filters_correctly(self) -> None:
        """load(after_offset=N) excludes events at or before offset N."""
        bundle = _bundle()
        run_id = "run-after"
        events = [_make_event(run_id, commit_offset=i + 1) for i in range(5)]
        await bundle.event_log.append_action_commit(_make_commit(run_id, events))
        loaded = await bundle.event_log.load(run_id, after_offset=2)
        assert all(e.commit_offset > 2 for e in loaded)
        bundle.close()

    @pytest.mark.asyncio
    async def test_max_offset_empty_returns_zero(self) -> None:
        """max_offset() returns 0 when no events exist for the run."""
        bundle = _bundle()
        assert await bundle.event_log.max_offset("nonexistent-run") == 0
        bundle.close()

    @pytest.mark.asyncio
    async def test_max_offset_after_appends(self) -> None:
        """max_offset() matches the highest commit_offset in the log."""
        bundle = _bundle()
        run_id = "run-maxoff"
        events = [_make_event(run_id, commit_offset=i + 1) for i in range(3)]
        await bundle.event_log.append_action_commit(_make_commit(run_id, events))
        all_events = await bundle.event_log.load(run_id)
        max_off = await bundle.event_log.max_offset(run_id)
        assert max_off == max(e.commit_offset for e in all_events)
        bundle.close()

    @pytest.mark.asyncio
    async def test_payload_json_roundtrip(self) -> None:
        """payload_json is persisted and retrieved correctly."""
        bundle = _bundle()
        run_id = "run-payload"
        payload = {"action": "call-tool", "tool_name": "search"}
        event = RuntimeEvent(
            run_id=run_id,
            event_id="ev-payload",
            commit_offset=1,
            event_type="turn.dispatched",
            event_class="fact",
            event_authority="authoritative_fact",
            ordering_key=run_id,
            wake_policy="wake_actor",
            created_at=_utc_now(),
            payload_json=payload,
        )
        await bundle.event_log.append_action_commit(_make_commit(run_id, [event]))
        loaded = await bundle.event_log.load(run_id)
        assert loaded[0].payload_json == payload
        bundle.close()


# ---------------------------------------------------------------------------
# TestDedupeStoreOperations
# ---------------------------------------------------------------------------


class TestDedupeStoreOperations:
    """Tests for the SharedConnectionDedupeStore (dedupe_store) sub-store."""

    def test_reserve_returns_accepted(self) -> None:
        bundle = _bundle()
        env = _make_envelope("run-r", "k1")
        res = bundle.dedupe_store.reserve(env)
        assert res.accepted is True
        assert res.reason == "accepted"
        bundle.close()

    def test_reserve_duplicate_returns_not_accepted(self) -> None:
        bundle = _bundle()
        env = _make_envelope("run-r2", "k2")
        bundle.dedupe_store.reserve(env)
        res2 = bundle.dedupe_store.reserve(env)
        assert res2.accepted is False
        assert res2.reason == "duplicate"
        bundle.close()

    def test_get_returns_none_for_unknown_key(self) -> None:
        bundle = _bundle()
        assert bundle.dedupe_store.get("nonexistent:key") is None
        bundle.close()

    def test_get_returns_record_after_reserve(self) -> None:
        bundle = _bundle()
        env = _make_envelope("run-get", "k3")
        bundle.dedupe_store.reserve(env)
        record = bundle.dedupe_store.get(env.dispatch_idempotency_key)
        assert record is not None
        assert record.state == "reserved"
        assert record.dispatch_idempotency_key == env.dispatch_idempotency_key
        bundle.close()

    def test_mark_dispatched_transitions_state(self) -> None:
        bundle = _bundle()
        env = _make_envelope("run-md", "k4")
        bundle.dedupe_store.reserve(env)
        bundle.dedupe_store.mark_dispatched(env.dispatch_idempotency_key)
        record = bundle.dedupe_store.get(env.dispatch_idempotency_key)
        assert record is not None
        assert record.state == "dispatched"
        bundle.close()

    def test_mark_acknowledged_transitions_state(self) -> None:
        bundle = _bundle()
        env = _make_envelope("run-ma", "k5")
        bundle.dedupe_store.reserve(env)
        bundle.dedupe_store.mark_dispatched(env.dispatch_idempotency_key)
        bundle.dedupe_store.mark_acknowledged(
            env.dispatch_idempotency_key, external_ack_ref="ack-1"
        )
        record = bundle.dedupe_store.get(env.dispatch_idempotency_key)
        assert record is not None
        assert record.state == "acknowledged"
        assert record.external_ack_ref == "ack-1"
        bundle.close()

    def test_mark_unknown_effect_transitions_state(self) -> None:
        bundle = _bundle()
        env = _make_envelope("run-mue", "k6")
        bundle.dedupe_store.reserve(env)
        bundle.dedupe_store.mark_dispatched(env.dispatch_idempotency_key)
        bundle.dedupe_store.mark_unknown_effect(env.dispatch_idempotency_key)
        record = bundle.dedupe_store.get(env.dispatch_idempotency_key)
        assert record is not None
        assert record.state == "unknown_effect"
        bundle.close()

    def test_invalid_transition_raises_state_error(self) -> None:
        """Transitioning from reserved directly to acknowledged raises."""
        bundle = _bundle()
        env = _make_envelope("run-inv", "k-inv")
        bundle.dedupe_store.reserve(env)
        with pytest.raises(DedupeStoreStateError):
            bundle.dedupe_store.mark_acknowledged(env.dispatch_idempotency_key)
        bundle.close()

    def test_mark_dispatched_unknown_key_raises(self) -> None:
        bundle = _bundle()
        with pytest.raises(DedupeStoreStateError):
            bundle.dedupe_store.mark_dispatched("nonexistent:key")
        bundle.close()

    def test_full_round_trip_reserve_dispatch_acknowledge(self) -> None:
        """Full state machine round-trip ends in acknowledged."""
        bundle = _bundle()
        env = _make_envelope("run-rt", "k-rt")
        res = bundle.dedupe_store.reserve(env)
        assert res.accepted is True
        bundle.dedupe_store.mark_dispatched(env.dispatch_idempotency_key)
        bundle.dedupe_store.mark_acknowledged(env.dispatch_idempotency_key)
        record = bundle.dedupe_store.get(env.dispatch_idempotency_key)
        assert record is not None
        assert record.state == "acknowledged"
        bundle.close()


# ---------------------------------------------------------------------------
# TestColocatedBundleThreadSafety
# ---------------------------------------------------------------------------


class TestColocatedBundleThreadSafety:
    """Verifies threading.Lock protection under concurrent access."""

    def test_concurrent_atomic_dispatch_from_threads(self) -> None:
        """Multiple threads can call atomic_dispatch_record concurrently without errors."""
        bundle = _bundle()
        run_id = "run-threads"
        errors: list[Exception] = []
        results: list[tuple[str, DedupeReservation]] = []
        lock = threading.Lock()

        def _worker(i: int) -> None:
            env = _make_envelope(run_id, f"act-{i}")
            event = _make_event(
                run_id,
                idempotency_key=env.dispatch_idempotency_key,
                commit_offset=i + 1,
            )
            commit = _make_commit(run_id, [event], commit_id=f"c-{i}")
            try:
                result = bundle.atomic_dispatch_record(commit, env)
                with lock:
                    results.append(result)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        n_threads = 10
        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Threads raised: {errors}"
        # All unique keys — all should be accepted.
        accepted = [r for _, r in results if r.accepted]
        assert len(accepted) == n_threads
        bundle.close()

    def test_concurrent_reserve_same_key_only_one_accepted(self) -> None:
        """Concurrent reserve() calls for same key allow exactly one acceptance."""
        bundle = _bundle()
        run_id = "run-race"
        env = _make_envelope(run_id, "race-key")
        accepted_count = 0
        lock = threading.Lock()
        errors: list[Exception] = []

        def _worker() -> None:
            nonlocal accepted_count
            try:
                res = bundle.dedupe_store.reserve(env)
                if res.accepted:
                    with lock:
                        accepted_count += 1
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=_worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert accepted_count == 1
        bundle.close()

    def test_concurrent_event_log_appends_from_threads(self) -> None:
        """Concurrent event_log.append_action_commit from threads produces correct count."""
        bundle = _bundle()
        run_id = "run-ev-threads"
        n_threads = 8
        errors: list[Exception] = []

        def _worker(i: int) -> None:
            loop = asyncio.new_event_loop()
            try:
                event = _make_event(run_id, commit_offset=i + 1)
                loop.run_until_complete(
                    bundle.event_log.append_action_commit(
                        _make_commit(run_id, [event], commit_id=f"c-{i}")
                    )
                )
            except Exception as exc:
                errors.append(exc)
            finally:
                loop.close()

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread errors: {errors}"
        loop = asyncio.new_event_loop()
        events = loop.run_until_complete(bundle.event_log.load(run_id))
        loop.close()
        assert len(events) == n_threads
        bundle.close()


# ---------------------------------------------------------------------------
# TestConsistencyCheckAfterAtomicDispatch
# ---------------------------------------------------------------------------


class TestConsistencyCheckAfterAtomicDispatch:
    """End-to-end: consistency check after various atomic dispatch scenarios.

    Uses the async variant ``averify_event_dedupe_consistency`` because the
    event_log here is ``_SharedConnectionEventLog`` whose ``load()`` is async.
    The sync variant would need to call ``asyncio.run()`` which cannot be used
    inside an already-running event loop.
    """

    @pytest.mark.asyncio
    async def test_clean_after_full_round_trip(self) -> None:
        """Consistency check is clean after reserve→dispatch→acknowledge via bundle."""
        bundle = _bundle()
        run_id = "run-e2e"
        env = _make_envelope(run_id, "e2e-act")
        key = env.dispatch_idempotency_key
        event = _make_event(run_id, event_type="turn.dispatched", idempotency_key=key)
        bundle.atomic_dispatch_record(_make_commit(run_id, [event]), env)
        bundle.dedupe_store.mark_dispatched(key)
        bundle.dedupe_store.mark_acknowledged(key)

        report = await averify_event_dedupe_consistency(
            bundle.event_log, bundle.dedupe_store, run_id
        )
        assert report.is_consistent
        bundle.close()

    @pytest.mark.asyncio
    async def test_unknown_effect_with_evidence_is_clean(self) -> None:
        """unknown_effect + turn.effect_unknown event passes consistency check."""
        bundle = _bundle()
        run_id = "run-unk-e2e"
        env = _make_envelope(run_id, "unk-act-e2e")
        key = env.dispatch_idempotency_key
        event = _make_event(run_id, event_type="turn.effect_unknown", idempotency_key=key)
        bundle.atomic_dispatch_record(_make_commit(run_id, [event]), env)
        bundle.dedupe_store.mark_dispatched(key)
        bundle.dedupe_store.mark_unknown_effect(key)

        report = await averify_event_dedupe_consistency(
            bundle.event_log, bundle.dedupe_store, run_id
        )
        assert report.is_consistent
        bundle.close()

    @pytest.mark.asyncio
    async def test_consistency_after_multiple_runs(self) -> None:
        """Consistency checks are isolated per run_id."""
        bundle = _bundle()
        for i in range(3):
            run_id = f"run-multi-{i}"
            env = _make_envelope(run_id, f"act-{i}")
            key = env.dispatch_idempotency_key
            event = _make_event(run_id, idempotency_key=key)
            bundle.atomic_dispatch_record(_make_commit(run_id, [event], commit_id=f"c-{i}"), env)

            report = await averify_event_dedupe_consistency(
                bundle.event_log, bundle.dedupe_store, run_id
            )
            assert report.is_consistent, f"run {run_id} is not consistent: {report.violations}"

        bundle.close()
