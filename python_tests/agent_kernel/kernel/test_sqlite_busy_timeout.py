"""Tests for SQLite busy-timeout configuration across persistence layers."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from agent_kernel.kernel.dedupe_store import IdempotencyEnvelope
from agent_kernel.kernel.persistence.sqlite_dedupe_store import SQLiteDedupeStore
from agent_kernel.kernel.persistence.sqlite_event_log import SQLiteKernelRuntimeEventLog
from agent_kernel.kernel.persistence.sqlite_pool import SQLiteConnectionPool


def _envelope(key: str) -> IdempotencyEnvelope:
    return IdempotencyEnvelope(
        dispatch_idempotency_key=key,
        operation_fingerprint=f"fp:{key}",
        attempt_seq=1,
        effect_scope="test",
        capability_snapshot_hash="hash",
        host_kind="local_process",
    )


def test_dedupe_store_sets_busy_timeout_pragma(tmp_path: Path) -> None:
    store = SQLiteDedupeStore(tmp_path / "dedupe.db", busy_timeout_ms=3000)
    row = store._conn.execute("PRAGMA busy_timeout").fetchone()
    assert row is not None
    assert row[0] == 3000
    store.close()


def test_event_log_sets_busy_timeout_pragma(tmp_path: Path) -> None:
    event_log = SQLiteKernelRuntimeEventLog(tmp_path / "events.db", busy_timeout_ms=3500)
    row = event_log._connection.execute("PRAGMA busy_timeout").fetchone()
    assert row is not None
    assert row[0] == 3500
    event_log.close()


def test_pool_sets_busy_timeout_for_read_and_write_connections(tmp_path: Path) -> None:
    pool = SQLiteConnectionPool(str(tmp_path / "pool.db"), busy_timeout_ms=4000)
    with pool.read_connection() as read_conn:
        read_row = read_conn.execute("PRAGMA busy_timeout").fetchone()
    with pool.write_connection() as write_conn:
        write_row = write_conn.execute("PRAGMA busy_timeout").fetchone()
    assert read_row is not None and read_row[0] == 4000
    assert write_row is not None and write_row[0] == 4000
    pool.close_all()


def test_busy_timeout_allows_waiting_for_lock_release(tmp_path: Path) -> None:
    db_path = tmp_path / "contention.db"
    store1 = SQLiteDedupeStore(db_path, busy_timeout_ms=2000)
    store2 = SQLiteDedupeStore(db_path, busy_timeout_ms=2000)

    # Hold an immediate write lock in store1.
    store1._conn.execute("BEGIN IMMEDIATE")
    store1._conn.execute("SELECT 1")

    observed: dict[str, float | Exception] = {}

    def _writer() -> None:
        start = time.monotonic()
        try:
            store2.reserve(_envelope("key-2"))
            observed["elapsed"] = time.monotonic() - start
        except Exception as exc:  # pragma: no cover - diagnostic path
            observed["error"] = exc

    worker = threading.Thread(target=_writer)
    worker.start()
    time.sleep(0.2)
    store1._conn.execute("ROLLBACK")
    worker.join(timeout=2.5)

    assert "error" not in observed, f"unexpected write error: {observed.get('error')!r}"
    assert "elapsed" in observed
    assert float(observed["elapsed"]) >= 0.15

    store1.close()
    store2.close()
