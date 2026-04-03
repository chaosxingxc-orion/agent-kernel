"""Round 8 quality optimization tests (R8a-R8c).

Covers:
  R8a - DedupeAwareScriptRuntime: mark_unknown_effect on inner execute exception
  R8b - ColocatedSQLiteBundle: shared connection, atomic_dispatch_record
  R8c - verify_event_dedupe_consistency: orphaned key + unknown_effect drift detection
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_envelope(run_id: str, key_suffix: str = "a1") -> Any:
    from agent_kernel.kernel.dedupe_store import IdempotencyEnvelope

    return IdempotencyEnvelope(
        dispatch_idempotency_key=f"{run_id}:{key_suffix}",
        operation_fingerprint="fp-1",
        attempt_seq=1,
        effect_scope="write",
        capability_snapshot_hash="snap-hash",
        host_kind="in_process_python",
    )


def _make_commit(run_id: str, key: str) -> Any:
    from agent_kernel.kernel.contracts import ActionCommit, RuntimeEvent

    event = RuntimeEvent(
        run_id=run_id,
        event_id=str(uuid.uuid4()),
        commit_offset=0,
        event_type="turn.dispatched",
        event_class="authoritative_fact",
        event_authority="Executor",
        ordering_key="default",
        wake_policy="none",
        created_at="2026-01-01T00:00:00Z",
        idempotency_key=key,
    )
    return ActionCommit(
        run_id=run_id,
        commit_id=str(uuid.uuid4()),
        created_at="2026-01-01T00:00:00Z",
        events=[event],
    )


# ---------------------------------------------------------------------------
# R8a - DedupeAwareScriptRuntime: unknown_effect on exception
# ---------------------------------------------------------------------------


class TestDedupeAwareScriptRuntimeUnknownEffect:
    def _make_input(self) -> Any:
        from agent_kernel.kernel.contracts import ScriptActivityInput

        return ScriptActivityInput(
            run_id="r1",
            action_id="a1",
            script_id="s1",
            script_content="",
            host_kind="in_process_python",
        )

    def test_mark_unknown_effect_called_on_inner_exception(self) -> None:
        """When inner execute_script raises, dedupe is marked unknown_effect."""
        from agent_kernel.kernel.cognitive.script_runtime import DedupeAwareScriptRuntime
        from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore

        dedupe = InMemoryDedupeStore()

        class _BoomRuntime:
            async def execute_script(self, input_value):
                raise RuntimeError("boom")

        runtime = DedupeAwareScriptRuntime(
            inner=_BoomRuntime(),
            dedupe_store=dedupe,
        )

        with pytest.raises(RuntimeError, match="boom"):
            asyncio.run(runtime.execute_script(self._make_input()))

        record = dedupe.get("script:r1:a1:s1")
        assert record is not None
        assert record.state == "unknown_effect"

    def test_mark_acknowledged_on_success(self) -> None:
        """Normal execution leaves dedupe in acknowledged state."""
        from agent_kernel.kernel.cognitive.script_runtime import (
            DedupeAwareScriptRuntime,
            EchoScriptRuntime,
        )
        from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore

        dedupe = InMemoryDedupeStore()
        runtime = DedupeAwareScriptRuntime(
            inner=EchoScriptRuntime(),
            dedupe_store=dedupe,
        )

        asyncio.run(
            runtime.execute_script(self._make_input())
        )

        record = dedupe.get("script:r1:a1:s1")
        assert record is not None
        assert record.state == "acknowledged"

    def test_duplicate_key_returns_noop(self) -> None:
        """Duplicate idempotency key returns a noop result without re-executing."""
        from agent_kernel.kernel.cognitive.script_runtime import (
            DedupeAwareScriptRuntime,
            EchoScriptRuntime,
        )
        from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore

        dedupe = InMemoryDedupeStore()
        runtime = DedupeAwareScriptRuntime(
            inner=EchoScriptRuntime(),
            dedupe_store=dedupe,
        )

        # First call.
        asyncio.run(runtime.execute_script(self._make_input()))
        # Second call with same idempotency key → noop.
        result2 = asyncio.run(
            runtime.execute_script(self._make_input())
        )
        # Noop result has exit_code=0, execution_ms=0.
        assert result2.exit_code == 0
        assert result2.execution_ms == 0

    def test_unknown_effect_suppress_dedupe_store_error(self) -> None:
        """If mark_unknown_effect itself raises, the original exception still propagates."""
        from agent_kernel.kernel.cognitive.script_runtime import DedupeAwareScriptRuntime
        from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore

        dedupe = InMemoryDedupeStore()

        class _BadDedupeStore:
            def reserve(self, envelope):
                return dedupe.reserve(envelope)

            def mark_dispatched(self, key, peer_operation_id=None):
                return dedupe.mark_dispatched(key, peer_operation_id)

            def mark_unknown_effect(self, key):
                raise OSError("disk full")

            def get(self, key):
                return dedupe.get(key)

        class _BoomRuntime:
            async def execute_script(self, input_value):
                raise ValueError("inner error")

        runtime = DedupeAwareScriptRuntime(
            inner=_BoomRuntime(),
            dedupe_store=_BadDedupeStore(),
        )

        # Original exception propagates even when mark_unknown_effect raises.
        with pytest.raises(ValueError, match="inner error"):
            asyncio.run(
                runtime.execute_script(self._make_input())
            )


# ---------------------------------------------------------------------------
# R8b - ColocatedSQLiteBundle
# ---------------------------------------------------------------------------


class TestColocatedSQLiteBundle:
    def test_initialize_creates_tables(self) -> None:
        from agent_kernel.kernel.persistence.sqlite_colocated_bundle import (
            ColocatedSQLiteBundle,
        )

        bundle = ColocatedSQLiteBundle(":memory:")
        tables = bundle._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {row[0] for row in tables}
        assert "colocated_action_commits" in table_names
        assert "colocated_runtime_events" in table_names
        assert "colocated_dedupe_store" in table_names
        bundle.close()

    def test_event_log_append_and_load(self) -> None:
        from agent_kernel.kernel.persistence.sqlite_colocated_bundle import (
            ColocatedSQLiteBundle,
        )

        run_id = "run-el-1"
        bundle = ColocatedSQLiteBundle(":memory:")
        commit = _make_commit(run_id, f"{run_id}:a1")
        asyncio.run(
            bundle.event_log.append_action_commit(commit)
        )
        events = asyncio.run(
            bundle.event_log.load(run_id)
        )
        assert len(events) == 1
        assert events[0].run_id == run_id
        bundle.close()

    def test_dedupe_store_reserve_and_get(self) -> None:
        from agent_kernel.kernel.persistence.sqlite_colocated_bundle import (
            ColocatedSQLiteBundle,
        )

        run_id = "run-ds-1"
        bundle = ColocatedSQLiteBundle(":memory:")
        envelope = _make_envelope(run_id)
        reservation = bundle.dedupe_store.reserve(envelope)
        assert reservation.accepted is True

        duplicate = bundle.dedupe_store.reserve(envelope)
        assert duplicate.accepted is False
        assert duplicate.reason == "duplicate"

        record = bundle.dedupe_store.get(envelope.dispatch_idempotency_key)
        assert record is not None
        assert record.state == "reserved"
        bundle.close()

    def test_atomic_dispatch_record_success(self) -> None:
        """atomic_dispatch_record reserves dedupe key and appends event atomically."""
        from agent_kernel.kernel.persistence.sqlite_colocated_bundle import (
            ColocatedSQLiteBundle,
        )

        run_id = "run-atomic-1"
        bundle = ColocatedSQLiteBundle(":memory:")
        envelope = _make_envelope(run_id)
        commit = _make_commit(run_id, envelope.dispatch_idempotency_key)

        commit_ref, reservation = bundle.atomic_dispatch_record(commit, envelope)
        assert reservation.accepted is True
        assert commit_ref.startswith("commit-ref-")

        # Both stores should reflect the write.
        events = asyncio.run(
            bundle.event_log.load(run_id)
        )
        assert len(events) == 1

        record = bundle.dedupe_store.get(envelope.dispatch_idempotency_key)
        assert record is not None
        assert record.state == "reserved"
        bundle.close()

    def test_atomic_dispatch_record_duplicate_returns_false(self) -> None:
        """Duplicate key in atomic_dispatch_record returns accepted=False without appending."""
        from agent_kernel.kernel.persistence.sqlite_colocated_bundle import (
            ColocatedSQLiteBundle,
        )

        run_id = "run-atomic-dup"
        bundle = ColocatedSQLiteBundle(":memory:")
        envelope = _make_envelope(run_id)
        commit = _make_commit(run_id, envelope.dispatch_idempotency_key)

        # First call succeeds.
        bundle.atomic_dispatch_record(commit, envelope)
        # Second call with same envelope → duplicate.
        commit_ref2, reservation2 = bundle.atomic_dispatch_record(commit, envelope)
        assert reservation2.accepted is False
        assert commit_ref2 == ""

        # Event log should have only one event (first call).
        events = asyncio.run(
            bundle.event_log.load(run_id)
        )
        assert len(events) == 1
        bundle.close()

    def test_atomic_dispatch_record_rollback_on_error(self) -> None:
        """If event insert fails, dedupe reservation is rolled back."""
        from agent_kernel.kernel.persistence.sqlite_colocated_bundle import (
            ColocatedSQLiteBundle,
        )

        run_id = "run-atomic-err"
        bundle = ColocatedSQLiteBundle(":memory:")
        envelope = _make_envelope(run_id)

        # Corrupt commit to trigger insert error (empty events).
        from agent_kernel.kernel.contracts import ActionCommit

        bad_commit = ActionCommit(
            run_id=run_id,
            commit_id="bad",
            created_at="2026-01-01T00:00:00Z",
            events=[],
        )

        with pytest.raises(ValueError, match="events must contain"):
            bundle.atomic_dispatch_record(bad_commit, envelope)

        # Dedupe key should NOT be reserved after rollback.
        record = bundle.dedupe_store.get(envelope.dispatch_idempotency_key)
        assert record is None
        bundle.close()

    def test_close_performs_wal_checkpoint(self) -> None:
        """close() issues PRAGMA wal_checkpoint without raising."""
        import os
        import tempfile

        from agent_kernel.kernel.persistence.sqlite_colocated_bundle import (
            ColocatedSQLiteBundle,
        )

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            bundle = ColocatedSQLiteBundle(db_path)
            bundle.close()  # Should not raise.
        finally:
            for ext in ("", "-wal", "-shm"):
                path = db_path + ext
                if os.path.exists(path):
                    os.unlink(path)

    def test_dedupe_state_transitions_via_shared_connection(self) -> None:
        """Full dedupe lifecycle works through the shared connection store."""
        from agent_kernel.kernel.persistence.sqlite_colocated_bundle import (
            ColocatedSQLiteBundle,
        )

        run_id = "run-lifecycle"
        bundle = ColocatedSQLiteBundle(":memory:")
        envelope = _make_envelope(run_id)

        bundle.dedupe_store.reserve(envelope)
        key = envelope.dispatch_idempotency_key
        bundle.dedupe_store.mark_dispatched(key)
        bundle.dedupe_store.mark_acknowledged(key)

        record = bundle.dedupe_store.get(key)
        assert record is not None
        assert record.state == "acknowledged"
        bundle.close()

    def test_unknown_effect_transition(self) -> None:
        from agent_kernel.kernel.persistence.sqlite_colocated_bundle import (
            ColocatedSQLiteBundle,
        )

        run_id = "run-unk"
        bundle = ColocatedSQLiteBundle(":memory:")
        envelope = _make_envelope(run_id)

        bundle.dedupe_store.reserve(envelope)
        key = envelope.dispatch_idempotency_key
        bundle.dedupe_store.mark_dispatched(key)
        bundle.dedupe_store.mark_unknown_effect(key)

        record = bundle.dedupe_store.get(key)
        assert record is not None
        assert record.state == "unknown_effect"
        bundle.close()


# ---------------------------------------------------------------------------
# R8c - verify_event_dedupe_consistency
# ---------------------------------------------------------------------------


class TestVerifyEventDedupeConsistency:
    def _make_event_log_with_events(self, run_id: str, key: str | None) -> Any:
        """In-memory event log stub with list_events()."""

        @dataclass
        class _Event:
            run_id: str
            idempotency_key: str | None
            event_type: str

        class _EventLog:
            def __init__(self) -> None:
                self._data: list[Any] = []

            def list_events(self) -> list[Any]:
                return list(self._data)

        log = _EventLog()
        if key is not None:
            log._data.append(
                _Event(
                    run_id=run_id,
                    idempotency_key=key,
                    event_type="turn.dispatched",
                )
            )
        return log

    def test_consistent_run_has_no_violations(self) -> None:
        from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore
        from agent_kernel.kernel.persistence.consistency import (
            verify_event_dedupe_consistency,
        )

        run_id = "run-ok"
        dedupe = InMemoryDedupeStore()
        key = f"{run_id}:a1"
        envelope = _make_envelope(run_id)
        dedupe.reserve(envelope)
        dedupe.mark_dispatched(key)
        dedupe.mark_acknowledged(key)

        log = self._make_event_log_with_events(run_id, key)
        report = verify_event_dedupe_consistency(log, dedupe, run_id)
        assert report.is_consistent
        assert report.dedupe_keys_checked == 1
        assert report.events_checked == 1

    def test_orphaned_dedupe_key_detected(self) -> None:
        """Key in dedupe store but no matching event → orphaned_dedupe_key violation."""
        from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore
        from agent_kernel.kernel.persistence.consistency import (
            verify_event_dedupe_consistency,
        )

        run_id = "run-orphan"
        dedupe = InMemoryDedupeStore()
        envelope = _make_envelope(run_id)
        dedupe.reserve(envelope)
        # No event appended to log.
        log = self._make_event_log_with_events(run_id, None)

        report = verify_event_dedupe_consistency(log, dedupe, run_id)
        assert not report.is_consistent
        assert len(report.violations) == 1
        assert report.violations[0].kind == "orphaned_dedupe_key"
        assert report.violations[0].dedupe_state == "reserved"

    def test_unknown_effect_without_log_evidence(self) -> None:
        """unknown_effect record without dispatch event → unknown_effect_no_log_evidence."""
        from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore
        from agent_kernel.kernel.persistence.consistency import (
            verify_event_dedupe_consistency,
        )

        run_id = "run-unk"
        dedupe = InMemoryDedupeStore()
        envelope = _make_envelope(run_id)
        key = envelope.dispatch_idempotency_key
        dedupe.reserve(envelope)
        dedupe.mark_dispatched(key)
        dedupe.mark_unknown_effect(key)

        # Event log has the key but only with a non-dispatch event type.
        @dataclass
        class _Event:
            run_id: str
            idempotency_key: str
            event_type: str

        class _EventLog:
            def list_events(self) -> list:
                return [_Event(run_id=run_id, idempotency_key=key, event_type="signal.received")]

        report = verify_event_dedupe_consistency(_EventLog(), dedupe, run_id)
        assert not report.is_consistent
        assert report.violations[0].kind == "unknown_effect_no_log_evidence"

    def test_unknown_effect_with_dispatch_evidence_is_clean(self) -> None:
        """unknown_effect with turn.dispatched event → no violation."""
        from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore
        from agent_kernel.kernel.persistence.consistency import (
            verify_event_dedupe_consistency,
        )

        run_id = "run-unk-ok"
        dedupe = InMemoryDedupeStore()
        envelope = _make_envelope(run_id)
        key = envelope.dispatch_idempotency_key
        dedupe.reserve(envelope)
        dedupe.mark_dispatched(key)
        dedupe.mark_unknown_effect(key)

        log = self._make_event_log_with_events(run_id, key)  # event_type="turn.dispatched"
        report = verify_event_dedupe_consistency(log, dedupe, run_id)
        assert report.is_consistent

    def test_async_variant_consistent(self) -> None:
        """averify_event_dedupe_consistency finds no violations for clean state."""
        from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore
        from agent_kernel.kernel.persistence.consistency import (
            averify_event_dedupe_consistency,
        )
        from agent_kernel.kernel.persistence.sqlite_event_log import (
            SQLiteKernelRuntimeEventLog,
        )

        run_id = "run-async-ok"
        event_log = SQLiteKernelRuntimeEventLog(":memory:")
        dedupe = InMemoryDedupeStore()

        # Append a dispatched event.
        key = f"{run_id}:a1"
        commit = _make_commit(run_id, key)
        asyncio.run(
            event_log.append_action_commit(commit)
        )

        # Reserve + acknowledge dedupe.
        envelope = _make_envelope(run_id)
        dedupe.reserve(envelope)
        dedupe.mark_dispatched(key)
        dedupe.mark_acknowledged(key)

        report = asyncio.run(
            averify_event_dedupe_consistency(event_log, dedupe, run_id)
        )
        assert report.is_consistent
        event_log.close()

    def test_colocated_bundle_consistency_after_atomic_write(self) -> None:
        """After atomic_dispatch_record the consistency check is clean."""
        from agent_kernel.kernel.persistence.consistency import (
            verify_event_dedupe_consistency,
        )
        from agent_kernel.kernel.persistence.sqlite_colocated_bundle import (
            ColocatedSQLiteBundle,
        )

        run_id = "run-colocated-ok"
        bundle = ColocatedSQLiteBundle(":memory:")
        envelope = _make_envelope(run_id)
        commit = _make_commit(run_id, envelope.dispatch_idempotency_key)
        bundle.atomic_dispatch_record(commit, envelope)

        report = verify_event_dedupe_consistency(
            bundle.event_log, bundle.dedupe_store, run_id
        )
        assert report.is_consistent
        bundle.close()

    def test_colocated_bundle_orphaned_key_detected(self) -> None:
        """Dedupe key reserved without event → orphaned key detected."""
        from agent_kernel.kernel.persistence.consistency import (
            verify_event_dedupe_consistency,
        )
        from agent_kernel.kernel.persistence.sqlite_colocated_bundle import (
            ColocatedSQLiteBundle,
        )

        run_id = "run-colocated-orphan"
        bundle = ColocatedSQLiteBundle(":memory:")
        envelope = _make_envelope(run_id)
        # Reserve dedupe but do NOT append an event.
        bundle.dedupe_store.reserve(envelope)

        report = verify_event_dedupe_consistency(
            bundle.event_log, bundle.dedupe_store, run_id
        )
        assert not report.is_consistent
        assert report.violations[0].kind == "orphaned_dedupe_key"
        bundle.close()

    def test_empty_run_is_consistent(self) -> None:
        """Run with no events and no dedupe keys is trivially consistent."""
        from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore
        from agent_kernel.kernel.persistence.consistency import (
            verify_event_dedupe_consistency,
        )

        class _EmptyLog:
            def list_events(self) -> list:
                return []

        report = verify_event_dedupe_consistency(_EmptyLog(), InMemoryDedupeStore(), "run-empty")
        assert report.is_consistent
        assert report.events_checked == 0
        assert report.dedupe_keys_checked == 0
