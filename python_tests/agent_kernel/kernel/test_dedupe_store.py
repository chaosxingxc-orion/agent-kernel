"""Verifies for v6.4 dedupestore monotonic idempotency semantics."""

from __future__ import annotations

import pytest

from agent_kernel.kernel.dedupe_store import (
    DedupeStoreStateError,
    IdempotencyEnvelope,
    InMemoryDedupeStore,
)


def _build_envelope(
    key: str = "dispatch-key-1",
    fingerprint: str = "fingerprint-1",
) -> IdempotencyEnvelope:
    """Builds a minimal idempotency envelope for dedupe store tests."""
    return IdempotencyEnvelope(
        dispatch_idempotency_key=key,
        operation_fingerprint=fingerprint,
        attempt_seq=1,
        effect_scope="workspace.write",
        capability_snapshot_hash="snapshot-hash-1",
        host_kind="local_cli",
    )


def test_reserve_accepts_first_request_and_blocks_duplicate_key() -> None:
    """Store should accept first reservation and short-circuit duplicate key."""
    store = InMemoryDedupeStore()

    first = store.reserve(_build_envelope())
    second = store.reserve(_build_envelope())

    assert first.accepted
    assert first.reason == "accepted"
    assert not second.accepted
    assert second.reason == "duplicate"
    assert second.existing_record is not None
    assert second.existing_record.state == "reserved"


def test_state_transitions_are_monotonic_reserved_to_dispatched_to_acknowledged() -> None:
    """Store should allow only forward state transitions for one key."""
    store = InMemoryDedupeStore()
    envelope = _build_envelope()
    store.reserve(envelope)

    store.mark_dispatched(envelope.dispatch_idempotency_key, peer_operation_id="peer-op-1")
    store.mark_acknowledged(envelope.dispatch_idempotency_key, external_ack_ref="ack-1")
    record = store.get(envelope.dispatch_idempotency_key)

    assert record is not None
    assert record.state == "acknowledged"
    assert record.peer_operation_id == "peer-op-1"
    assert record.external_ack_ref == "ack-1"


def test_unknown_effect_is_terminal_for_dispatch_transition() -> None:
    """Store should reject dispatch rollback after unknown_effect has been marked."""
    store = InMemoryDedupeStore()
    envelope = _build_envelope()
    store.reserve(envelope)
    store.mark_dispatched(envelope.dispatch_idempotency_key)
    store.mark_unknown_effect(envelope.dispatch_idempotency_key)

    with pytest.raises(DedupeStoreStateError):
        store.mark_dispatched(envelope.dispatch_idempotency_key)

    record = store.get(envelope.dispatch_idempotency_key)
    assert record is not None
    assert record.state == "unknown_effect"


def test_duplicate_key_with_different_fingerprint_is_rejected() -> None:
    """Store should reject key collisions even if fingerprint changes."""
    store = InMemoryDedupeStore()
    first = _build_envelope(key="dispatch-key-2", fingerprint="fp-1")
    second = _build_envelope(key="dispatch-key-2", fingerprint="fp-2")

    reservation_a = store.reserve(first)
    reservation_b = store.reserve(second)

    assert reservation_a.accepted
    assert not reservation_b.accepted
    assert reservation_b.reason == "duplicate"


def test_unknown_key_update_raises_state_error() -> None:
    """Store should fail fast when marking state for an unknown dispatch key."""
    store = InMemoryDedupeStore()

    with pytest.raises(DedupeStoreStateError):
        store.mark_acknowledged("missing-key")
