"""Tests for DispatchOutboxReconciler (Saga-pattern drift repair)."""

from __future__ import annotations

import logging
from dataclasses import FrozenInstanceError
from typing import Any
from unittest.mock import MagicMock

import pytest

from agent_kernel.kernel.dedupe_store import (
    DedupeStoreStateError,
    IdempotencyEnvelope,
    InMemoryDedupeStore,
)
from agent_kernel.kernel.persistence.dispatch_outbox_reconciler import (
    DispatchOutboxReconciler,
    ReconciliationAction,
    ReconciliationResult,
)

# ---------------------------------------------------------------------------
# Helpers / stubs
# ---------------------------------------------------------------------------

RUN_ID = "run-recon-test"


def _make_envelope(run_id: str, key_suffix: str = "op1") -> IdempotencyEnvelope:
    key = f"{run_id}:{key_suffix}"
    return IdempotencyEnvelope(
        dispatch_idempotency_key=key,
        operation_fingerprint=f"fp-{key_suffix}",
        attempt_seq=1,
        effect_scope="write",
        capability_snapshot_hash="sha256-abc",
        host_kind="in_process_python",
    )


class _SyncEventLogStub:
    """Minimal sync event log that exposes list_events() and _events."""

    def __init__(self) -> None:
        self._events: list[Any] = []

    def list_events(self) -> list[Any]:
        return list(self._events)

    def add_event(self, run_id: str, idempotency_key: str, event_type: str) -> None:
        ev = MagicMock()
        ev.run_id = run_id
        ev.idempotency_key = idempotency_key
        ev.event_type = event_type
        self._events.append(ev)


class _AsyncEventLogStub:
    """Minimal async event log compatible with averify_event_dedupe_consistency."""

    def __init__(self) -> None:
        self._store: list[Any] = []

    async def load(self, run_id: str, after_offset: int = 0) -> list[Any]:
        return [e for e in self._store if getattr(e, "run_id", None) == run_id]

    def add_event(self, run_id: str, idempotency_key: str, event_type: str) -> None:
        ev = MagicMock()
        ev.run_id = run_id
        ev.idempotency_key = idempotency_key
        ev.event_type = event_type
        self._store.append(ev)


# ---------------------------------------------------------------------------
# TestReconciliationAction
# ---------------------------------------------------------------------------


class TestReconciliationAction:
    def test_is_frozen_dataclass(self) -> None:
        action = ReconciliationAction(
            idempotency_key="k",
            violation_kind="orphaned_dedupe_key",
            action_taken="marked_unknown_effect",
            detail="details here",
        )
        with pytest.raises((FrozenInstanceError, AttributeError)):
            action.action_taken = "skipped"  # type: ignore[misc]

    def test_fields_stored_correctly(self) -> None:
        action = ReconciliationAction(
            idempotency_key="run1:op1",
            violation_kind="unknown_effect_no_log_evidence",
            action_taken="logged_for_review",
            detail="some detail",
        )
        assert action.idempotency_key == "run1:op1"
        assert action.violation_kind == "unknown_effect_no_log_evidence"
        assert action.action_taken == "logged_for_review"
        assert action.detail == "some detail"


# ---------------------------------------------------------------------------
# TestReconciliationResult
# ---------------------------------------------------------------------------


class TestReconciliationResult:
    def test_is_clean_when_no_violations(self) -> None:
        result = ReconciliationResult(run_id=RUN_ID)
        assert result.is_clean is True

    def test_is_clean_false_when_violations_found(self) -> None:
        result = ReconciliationResult(run_id=RUN_ID, violations_found=1)
        assert result.is_clean is False

    def test_defaults(self) -> None:
        result = ReconciliationResult(run_id="r")
        assert result.actions == []
        assert result.violations_found == 0
        assert result.violations_repaired == 0

    def test_run_id_stored(self) -> None:
        result = ReconciliationResult(run_id="my-run")
        assert result.run_id == "my-run"

    def test_actions_list_is_mutable(self) -> None:
        result = ReconciliationResult(run_id="r")
        action = ReconciliationAction(
            idempotency_key="k",
            violation_kind="orphaned_dedupe_key",
            action_taken="skipped",
            detail="",
        )
        result.actions.append(action)
        assert len(result.actions) == 1


# ---------------------------------------------------------------------------
# TestDispatchOutboxReconciler — sync path
# ---------------------------------------------------------------------------


class TestDispatchOutboxReconcilerSync:
    def _reconciler(self, logger: logging.Logger | None = None) -> DispatchOutboxReconciler:
        return DispatchOutboxReconciler(logger=logger)

    def _store_with_reserved(self, key_suffix: str = "op1") -> InMemoryDedupeStore:
        store = InMemoryDedupeStore()
        env = _make_envelope(RUN_ID, key_suffix)
        store.reserve(env)
        return store

    def test_clean_run_is_clean(self) -> None:
        log = _SyncEventLogStub()
        store = InMemoryDedupeStore()
        result = self._reconciler().reconcile_sync(log, store, RUN_ID)
        assert result.is_clean is True
        assert result.violations_found == 0
        assert result.violations_repaired == 0
        assert result.actions == []

    def test_orphaned_reserved_key_marked_unknown_effect(self) -> None:
        log = _SyncEventLogStub()  # no events
        store = self._store_with_reserved()
        key = f"{RUN_ID}:op1"

        result = self._reconciler().reconcile_sync(log, store, RUN_ID)

        assert result.violations_found == 1
        assert result.violations_repaired == 1
        assert len(result.actions) == 1
        action = result.actions[0]
        assert action.action_taken == "marked_unknown_effect"
        assert action.violation_kind == "orphaned_dedupe_key"
        assert action.idempotency_key == key
        # Verify the dedupe store was actually mutated.
        record = store.get(key)
        assert record is not None
        assert record.state == "unknown_effect"

    def test_orphaned_dispatched_key_marked_unknown_effect(self) -> None:
        log = _SyncEventLogStub()
        store = InMemoryDedupeStore()
        env = _make_envelope(RUN_ID, "op2")
        store.reserve(env)
        store.mark_dispatched(env.dispatch_idempotency_key)

        result = self._reconciler().reconcile_sync(log, store, RUN_ID)

        assert result.violations_found == 1
        assert result.violations_repaired == 1
        action = result.actions[0]
        assert action.action_taken == "marked_unknown_effect"
        record = store.get(env.dispatch_idempotency_key)
        assert record is not None
        assert record.state == "unknown_effect"

    def test_orphaned_unknown_effect_key_skipped(self) -> None:
        log = _SyncEventLogStub()
        store = InMemoryDedupeStore()
        env = _make_envelope(RUN_ID, "op3")
        store.reserve(env)
        store.mark_dispatched(env.dispatch_idempotency_key)
        store.mark_unknown_effect(env.dispatch_idempotency_key)
        # Still no event in the log → orphaned_dedupe_key violation.

        result = self._reconciler().reconcile_sync(log, store, RUN_ID)

        assert result.violations_found == 1
        assert result.violations_repaired == 0
        action = result.actions[0]
        assert action.action_taken == "skipped"
        # State must not change.
        assert store.get(env.dispatch_idempotency_key).state == "unknown_effect"  # type: ignore[union-attr]

    def test_orphaned_acknowledged_key_skipped(self) -> None:
        log = _SyncEventLogStub()
        store = InMemoryDedupeStore()
        env = _make_envelope(RUN_ID, "op4")
        store.reserve(env)
        store.mark_dispatched(env.dispatch_idempotency_key)
        store.mark_acknowledged(env.dispatch_idempotency_key)
        # No event log entry → still an orphaned_dedupe_key.

        result = self._reconciler().reconcile_sync(log, store, RUN_ID)

        assert result.violations_found == 1
        assert result.violations_repaired == 0
        action = result.actions[0]
        assert action.action_taken == "skipped"
        assert store.get(env.dispatch_idempotency_key).state == "acknowledged"  # type: ignore[union-attr]

    def test_unknown_effect_no_log_evidence_logged_for_review(self) -> None:
        """DedupeStore has unknown_effect but event log has no dispatch events."""
        log = _SyncEventLogStub()
        store = InMemoryDedupeStore()
        env = _make_envelope(RUN_ID, "op5")
        key = env.dispatch_idempotency_key
        store.reserve(env)
        store.mark_dispatched(key)
        store.mark_unknown_effect(key)
        # Add a non-dispatch event (e.g. intent committed) so the key is in
        # event_idempotency_keys, but no dispatch-class event exists.
        log.add_event(RUN_ID, key, "turn.intent_committed")

        result = self._reconciler().reconcile_sync(log, store, RUN_ID)

        assert result.violations_found == 1
        assert result.violations_repaired == 1
        action = result.actions[0]
        assert action.action_taken == "logged_for_review"
        assert action.violation_kind == "unknown_effect_no_log_evidence"

    def test_multiple_violations_all_repaired(self) -> None:
        log = _SyncEventLogStub()
        store = InMemoryDedupeStore()

        # Orphaned reserved key (op-a).
        env_a = _make_envelope(RUN_ID, "op-a")
        store.reserve(env_a)

        # Orphaned dispatched key (op-b).
        env_b = _make_envelope(RUN_ID, "op-b")
        store.reserve(env_b)
        store.mark_dispatched(env_b.dispatch_idempotency_key)

        result = self._reconciler().reconcile_sync(log, store, RUN_ID)

        assert result.violations_found == 2
        assert result.violations_repaired == 2
        assert all(a.action_taken == "marked_unknown_effect" for a in result.actions)
        assert store.get(env_a.dispatch_idempotency_key).state == "unknown_effect"  # type: ignore[union-attr]
        assert store.get(env_b.dispatch_idempotency_key).state == "unknown_effect"  # type: ignore[union-attr]

    def test_dedupe_state_error_during_repair_skipped(self) -> None:
        """If mark_unknown_effect raises, action is skipped and no exception propagates."""
        log = _SyncEventLogStub()
        store = InMemoryDedupeStore()
        env = _make_envelope(RUN_ID, "op-err")
        store.reserve(env)

        # Monkey-patch mark_dispatched to raise to simulate race condition on
        # the intermediate step that advances "reserved" → "dispatched".
        original_mark_dispatched = store.mark_dispatched

        def _failing_mark_dispatched(key: str, *args: Any, **kwargs: Any) -> None:
            raise DedupeStoreStateError("simulated race condition")

        store.mark_dispatched = _failing_mark_dispatched  # type: ignore[method-assign]

        result = self._reconciler().reconcile_sync(log, store, RUN_ID)

        assert result.violations_found == 1
        assert result.violations_repaired == 0
        action = result.actions[0]
        assert action.action_taken == "skipped"
        assert "DedupeStoreStateError" in action.detail

        # Restore so we don't leak into other tests.
        store.mark_dispatched = original_mark_dispatched  # type: ignore[method-assign]

    def test_violations_repaired_counts_non_skipped(self) -> None:
        log = _SyncEventLogStub()
        store = InMemoryDedupeStore()

        # One repairable (reserved), one terminal (acknowledged — skipped).
        env_r = _make_envelope(RUN_ID, "r1")
        store.reserve(env_r)

        env_a = _make_envelope(RUN_ID, "a1")
        store.reserve(env_a)
        store.mark_dispatched(env_a.dispatch_idempotency_key)
        store.mark_acknowledged(env_a.dispatch_idempotency_key)

        result = self._reconciler().reconcile_sync(log, store, RUN_ID)

        assert result.violations_found == 2
        # Only the reserved→unknown_effect action is repaired.
        assert result.violations_repaired == 1

    def test_custom_logger_receives_warning_for_unknown_effect_no_evidence(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        log = _SyncEventLogStub()
        store = InMemoryDedupeStore()
        env = _make_envelope(RUN_ID, "op-warn")
        key = env.dispatch_idempotency_key
        store.reserve(env)
        store.mark_dispatched(key)
        store.mark_unknown_effect(key)
        log.add_event(RUN_ID, key, "turn.intent_committed")

        named_logger = logging.getLogger("test_custom_logger")
        reconciler = DispatchOutboxReconciler(logger=named_logger)

        with caplog.at_level(logging.WARNING, logger="test_custom_logger"):
            reconciler.reconcile_sync(log, store, RUN_ID)

        assert any("unknown_effect" in record.message for record in caplog.records)

    def test_reconcile_sync_returns_correct_run_id(self) -> None:
        log = _SyncEventLogStub()
        store = InMemoryDedupeStore()
        result = self._reconciler().reconcile_sync(log, store, "my-special-run")
        assert result.run_id == "my-special-run"


# ---------------------------------------------------------------------------
# TestDispatchOutboxReconciler — async path
# ---------------------------------------------------------------------------


class TestDispatchOutboxReconcilerAsync:
    def _reconciler(self) -> DispatchOutboxReconciler:
        return DispatchOutboxReconciler()

    @pytest.mark.asyncio
    async def test_clean_run_is_clean_async(self) -> None:
        log = _AsyncEventLogStub()
        store = InMemoryDedupeStore()
        result = await self._reconciler().reconcile(log, store, RUN_ID)
        assert result.is_clean is True
        assert result.violations_found == 0

    @pytest.mark.asyncio
    async def test_orphaned_reserved_key_async(self) -> None:
        log = _AsyncEventLogStub()
        store = InMemoryDedupeStore()
        env = _make_envelope(RUN_ID, "async-op1")
        key = env.dispatch_idempotency_key
        store.reserve(env)

        result = await self._reconciler().reconcile(log, store, RUN_ID)

        assert result.violations_found == 1
        assert result.violations_repaired == 1
        action = result.actions[0]
        assert action.action_taken == "marked_unknown_effect"
        assert store.get(key).state == "unknown_effect"  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_orphaned_dispatched_key_async(self) -> None:
        log = _AsyncEventLogStub()
        store = InMemoryDedupeStore()
        env = _make_envelope(RUN_ID, "async-op2")
        key = env.dispatch_idempotency_key
        store.reserve(env)
        store.mark_dispatched(key)

        result = await self._reconciler().reconcile(log, store, RUN_ID)

        assert result.violations_found == 1
        assert result.violations_repaired == 1
        assert store.get(key).state == "unknown_effect"  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_unknown_effect_no_log_evidence_async(self) -> None:
        log = _AsyncEventLogStub()
        store = InMemoryDedupeStore()
        env = _make_envelope(RUN_ID, "async-op3")
        key = env.dispatch_idempotency_key
        store.reserve(env)
        store.mark_dispatched(key)
        store.mark_unknown_effect(key)
        log.add_event(RUN_ID, key, "turn.intent_committed")

        result = await self._reconciler().reconcile(log, store, RUN_ID)

        assert result.violations_found == 1
        assert result.violations_repaired == 1
        action = result.actions[0]
        assert action.action_taken == "logged_for_review"

    @pytest.mark.asyncio
    async def test_already_unknown_effect_orphaned_key_skipped_async(self) -> None:
        log = _AsyncEventLogStub()
        store = InMemoryDedupeStore()
        env = _make_envelope(RUN_ID, "async-op4")
        key = env.dispatch_idempotency_key
        store.reserve(env)
        store.mark_dispatched(key)
        store.mark_unknown_effect(key)
        # No log entry at all → orphaned_dedupe_key with unknown_effect state.

        result = await self._reconciler().reconcile(log, store, RUN_ID)

        assert result.violations_found == 1
        assert result.violations_repaired == 0
        action = result.actions[0]
        assert action.action_taken == "skipped"

    @pytest.mark.asyncio
    async def test_dedupe_state_error_skipped_async(self) -> None:
        log = _AsyncEventLogStub()
        store = InMemoryDedupeStore()
        env = _make_envelope(RUN_ID, "async-err")
        store.reserve(env)

        def _fail(*args: Any, **kwargs: Any) -> None:
            raise DedupeStoreStateError("simulated")

        store.mark_dispatched = _fail  # type: ignore[method-assign]

        result = await self._reconciler().reconcile(log, store, RUN_ID)

        assert result.violations_repaired == 0
        assert result.actions[0].action_taken == "skipped"

    @pytest.mark.asyncio
    async def test_reconcile_returns_correct_run_id_async(self) -> None:
        log = _AsyncEventLogStub()
        store = InMemoryDedupeStore()
        result = await self._reconciler().reconcile(log, store, "async-run-42")
        assert result.run_id == "async-run-42"

    @pytest.mark.asyncio
    async def test_multiple_violations_mixed_async(self) -> None:
        log = _AsyncEventLogStub()
        store = InMemoryDedupeStore()

        # Orphaned reserved.
        env_r = _make_envelope(RUN_ID, "m1")
        store.reserve(env_r)

        # unknown_effect_no_log_evidence.
        env_u = _make_envelope(RUN_ID, "m2")
        store.reserve(env_u)
        store.mark_dispatched(env_u.dispatch_idempotency_key)
        store.mark_unknown_effect(env_u.dispatch_idempotency_key)
        log.add_event(RUN_ID, env_u.dispatch_idempotency_key, "turn.intent_committed")

        result = await self._reconciler().reconcile(log, store, RUN_ID)

        assert result.violations_found == 2
        assert result.violations_repaired == 2
        action_kinds = {a.action_taken for a in result.actions}
        assert "marked_unknown_effect" in action_kinds
        assert "logged_for_review" in action_kinds
