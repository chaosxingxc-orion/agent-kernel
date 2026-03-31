"""Tests for SQLite-backed dedupe store persistence and monotonic behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_kernel.kernel.dedupe_store import DedupeStoreStateError, IdempotencyEnvelope
from agent_kernel.kernel.persistence.sqlite_dedupe_store import SQLiteDedupeStore


def _build_envelope(key: str) -> IdempotencyEnvelope:
    """Builds one idempotency envelope for sqlite dedupe tests."""
    return IdempotencyEnvelope(
        dispatch_idempotency_key=key,
        operation_fingerprint=f"fingerprint:{key}",
        attempt_seq=1,
        effect_scope="workspace.write",
        capability_snapshot_hash="snapshot-hash",
        host_kind="local_cli",
    )


def test_sqlite_dedupe_persists_record_across_store_reopen(tmp_path: Path) -> None:
    """Store should persist reservations and states across process-like reopen."""
    database_path = tmp_path / "dedupe.sqlite3"
    store = SQLiteDedupeStore(database_path)
    envelope = _build_envelope("key-1")
    store.reserve(envelope)
    store.mark_dispatched("key-1", peer_operation_id="peer-1")
    store.close()

    reopened = SQLiteDedupeStore(database_path)
    record = reopened.get("key-1")
    reopened.close()

    assert record is not None
    assert record.state == "dispatched"
    assert record.peer_operation_id == "peer-1"


def test_sqlite_dedupe_enforces_monotonic_transition_rules(tmp_path: Path) -> None:
    """Store should reject rollback transitions from terminal unknown_effect."""
    store = SQLiteDedupeStore(tmp_path / "dedupe-monotonic.sqlite3")
    store.reserve(_build_envelope("key-2"))
    store.mark_dispatched("key-2")
    store.mark_unknown_effect("key-2")

    with pytest.raises(DedupeStoreStateError):
        store.mark_dispatched("key-2")
    store.close()
