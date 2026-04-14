"""SQLite-backed implementation of the DecisionDeduper protocol.

Provides durable, process-restart-safe decision fingerprint tracking for
the agent-kernel workflow layer. Designed for use where the in-memory
``InMemoryDecisionDeduper`` is insufficient (e.g. long-running or resumed
Temporal workflows that must survive worker restarts).

WAL mode enables concurrent readers while the single writer is active.
All public methods are thread-safe via an internal ``threading.Lock``.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
import time
from pathlib import Path


class SQLiteDecisionDeduper:
    """Persists decision fingerprints in SQLite for idempotent decision rounds.

    Satisfies the ``DecisionDeduper`` Protocol defined in ``contracts.py``:
    - ``seen(fingerprint)`` — returns whether the fingerprint was previously marked.
    - ``mark(fingerprint)`` — records the fingerprint as processed.

    Both methods are async to match the Protocol signature, but the underlying
    SQLite I/O is synchronous (sqlite3 stdlib only). This is acceptable for
    PoC/single-process use; a production Temporal worker runs each activity
    invocation on a thread pool, so blocking I/O is not a concern on the
    workflow thread.
    """

    def __init__(
        self,
        database_path: str | Path,
        busy_timeout_ms: int = 5000,
    ) -> None:
        """Initialize the deduper and create the schema if absent.

        Args:
            database_path: Path to the SQLite database file, or ``":memory:"``
                for an in-process, non-persistent store (useful in tests).
            busy_timeout_ms: SQLite busy-timeout in milliseconds. Applied before
                WAL mode is enabled to handle lock contention on shared files.

        """
        self._database_path = str(database_path)
        self._lock = threading.Lock()
        # check_same_thread=False: lock serialization is handled by self._lock.
        self._conn = sqlite3.connect(
            self._database_path, isolation_level=None, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"PRAGMA busy_timeout={max(0, busy_timeout_ms)}")
        # WAL mode: concurrent readers do not block the single writer.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA wal_autocheckpoint=1000")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._ensure_schema()

    # ------------------------------------------------------------------
    # DecisionDeduper Protocol
    # ------------------------------------------------------------------

    async def seen(self, fingerprint: str) -> bool:
        """Return whether a decision fingerprint has already been processed.

        Args:
            fingerprint: Decision fingerprint to check.

        Returns:
            ``True`` if the fingerprint was previously marked via ``mark()``.

        """
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM decision_fingerprints WHERE fingerprint = ? LIMIT 1",
                (fingerprint,),
            ).fetchone()
        return row is not None

    async def mark(self, fingerprint: str, run_id: str = "") -> None:
        """Mark a decision fingerprint as processed.

        Idempotent: calling ``mark`` for an already-recorded fingerprint is a
        no-op (INSERT OR IGNORE).

        Args:
            fingerprint: Decision fingerprint to record.
            run_id: Optional run identifier for audit / diagnostics.

        """
        with self._lock:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO decision_fingerprints (fingerprint, run_id, created_at)
                VALUES (?, ?, ?)
                """,
                (fingerprint, run_id, time.time()),
            )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Checkpoint WAL and close the SQLite connection.

        Checkpointing (TRUNCATE mode) merges WAL contents back into the main
        database file and resets the WAL to zero bytes, reclaiming disk space.
        The checkpoint is best-effort; failures are silently suppressed so a
        crashed connection can still be closed cleanly.
        """
        with self._lock:
            with contextlib.suppress(Exception):
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._conn.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_schema(self) -> None:
        """Create the ``decision_fingerprints`` table if it does not exist."""
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS decision_fingerprints (
                fingerprint TEXT PRIMARY KEY,
                run_id      TEXT NOT NULL DEFAULT '',
                created_at  REAL NOT NULL
            )
        """)
