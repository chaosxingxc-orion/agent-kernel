"""Large stability matrix for DedupeStore monotonic behavior."""

from __future__ import annotations

import pytest

from agent_kernel.kernel.dedupe_store import IdempotencyEnvelope, InMemoryDedupeStore

_CASE_COUNT = 1000


def _envelope_for(seed: int) -> IdempotencyEnvelope:
    """Envelope for."""
    host_kind = "remote_service" if seed % 3 == 0 else "local_cli"
    return IdempotencyEnvelope(
        dispatch_idempotency_key=f"dispatch:{seed}",
        operation_fingerprint=f"fp:{seed}",
        attempt_seq=1,
        effect_scope="idempotent_write",
        capability_snapshot_hash=f"hash:{seed % 41}",
        host_kind=host_kind,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("seed", list(range(_CASE_COUNT)))
def test_dedupe_matrix_reserve_and_terminal_state(seed: int) -> None:
    """Each dedupe key should remain monotonic and idempotent across transitions."""
    store = InMemoryDedupeStore()
    envelope = _envelope_for(seed)

    first = store.reserve(envelope)
    second = store.reserve(envelope)
    assert first.accepted
    assert second.accepted is False
    assert second.reason == "duplicate"

    store.mark_dispatched(envelope.dispatch_idempotency_key)
    if seed % 2 == 0:
        store.mark_acknowledged(envelope.dispatch_idempotency_key)
        acknowledged_record = store.get(envelope.dispatch_idempotency_key)
        assert acknowledged_record is not None
        assert acknowledged_record.state == "acknowledged"
    else:
        store.mark_unknown_effect(envelope.dispatch_idempotency_key)
        unknown_effect_record = store.get(envelope.dispatch_idempotency_key)
        assert unknown_effect_record is not None
        assert unknown_effect_record.state == "unknown_effect"
