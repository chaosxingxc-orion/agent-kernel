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


class TestSQLiteDedupeStoreCountByRun:
    """Unit tests for SQLiteDedupeStore.count_by_run using an in-memory database."""

    def setup_method(self) -> None:
        """Create a fresh in-memory store for each test."""
        self.store = SQLiteDedupeStore(":memory:")

    def teardown_method(self) -> None:
        """Close the store after each test."""
        self.store.close()

    def test_count_by_run_returns_zero_when_no_records(self) -> None:
        """count_by_run returns 0 when no records exist for the given run."""
        assert self.store.count_by_run("run-empty") == 0

    def test_count_by_run_counts_only_matching_run(self) -> None:
        """count_by_run counts records for the target run and ignores other runs."""
        self.store.reserve(_build_envelope("run-a:action:1"))
        self.store.reserve(_build_envelope("run-a:action:2"))
        self.store.reserve(_build_envelope("run-b:action:1"))

        assert self.store.count_by_run("run-a") == 2
        assert self.store.count_by_run("run-b") == 1

    def test_count_by_run_across_multiple_runs(self) -> None:
        """count_by_run returns correct totals after records are added for several runs."""
        for i in range(3):
            self.store.reserve(_build_envelope(f"run-x:op:{i}"))
        for i in range(5):
            self.store.reserve(_build_envelope(f"run-y:op:{i}"))

        assert self.store.count_by_run("run-x") == 3
        assert self.store.count_by_run("run-y") == 5
        assert self.store.count_by_run("run-z") == 0
