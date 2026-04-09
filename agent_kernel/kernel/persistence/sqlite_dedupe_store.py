"""SQLite-backed DedupeStore for v6.4 idempotency persistence windows."""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from agent_kernel.kernel.persistence.sqlite_pool import SQLiteConnectionPool

from agent_kernel.kernel.dedupe_store import (
    DedupeRecord,
    DedupeReservation,
    DedupeStoreStateError,
    IdempotencyEnvelope,
)


class SQLiteDedupeStore:
    """Persists dedupe records in SQLite with monotonic state transitions.

    This store is designed for PoC durability and recovery windows where
    in-memory dedupe is not sufficient across process restarts.
    """

    def __init__(
        self,
        database_path: str | Path = ":memory:",
        pool: SQLiteConnectionPool | None = None,
        busy_timeout_ms: int = 5000,
    ) -> None:
        """Initialize one SQLite dedupe store.

        Args:
            database_path: SQLite file path. Use ``":memory:"`` for
                in-memory mode.
            pool: Optional shared SQLite connection pool.
            busy_timeout_ms: SQLite busy-timeout window in milliseconds for
                lock contention waits.

        """
        self._database_path = str(database_path)
        self._pool = pool
        # check_same_thread=False allows the connection to be used from multiple
        # threads.  All public methods are serialized via self._lock (RLock allows
        # re-entrant acquisition from internal helpers that call self.get()).
        if self._pool is None:
            self._conn = sqlite3.connect(
                self._database_path, isolation_level=None, check_same_thread=False
            )
        else:
            self._conn = self._pool.acquire_write()
        self._lock = threading.RLock()
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"PRAGMA busy_timeout={max(0, busy_timeout_ms)}")
        if self._pool is None:
            # WAL mode allows concurrent readers while one writer is active.
            # NORMAL sync is safe with WAL and durable on OS crash for PoC use.
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA wal_autocheckpoint=1000")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        self._ensure_schema()

    def close(self) -> None:
        """Close SQLite connection after checkpointing the WAL file.

        Checkpointing (TRUNCATE mode) ensures WAL contents are merged back
        into the main database file and the WAL file is reset to zero bytes.
        This reclaims disk space and ensures durability before process exit.
        The checkpoint is best-effort 鈥?failures are silently suppressed so
        that a crashed connection can still be closed.
        """
        with self._lock:
            if self._pool is None:
                with contextlib.suppress(Exception):
                    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self._conn.close()

    def reserve(self, envelope: IdempotencyEnvelope) -> DedupeReservation:
        """Reserves dispatch idempotency key if absent.

        Args:
            envelope: Idempotency envelope to reserve.

        Returns:
            Reservation result indicating acceptance or duplicate.

        Raises:
            DedupeStoreStateError: If the key exists in an incompatible state.

        """
        # BEGIN IMMEDIATE acquires a write lock upfront, preventing TOCTOU
        # between the existence check and the INSERT across concurrent processes.
        with self._lock:
            return self._reserve_locked(envelope)

    def _reserve_locked(self, envelope: IdempotencyEnvelope) -> DedupeReservation:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            existing_record = self.get(envelope.dispatch_idempotency_key)
            if existing_record is not None:
                self._conn.execute("ROLLBACK")
                return DedupeReservation(
                    accepted=False,
                    reason="duplicate",
                    existing_record=existing_record,
                )

            cursor = self._conn.cursor()
            cursor.execute(
                """
                INSERT INTO dedupe_store (
                  dispatch_idempotency_key,
                  operation_fingerprint,
                  attempt_seq,
                  state,
                  peer_operation_id,
                  external_ack_ref
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    envelope.dispatch_idempotency_key,
                    envelope.operation_fingerprint,
                    envelope.attempt_seq,
                    "reserved",
                    envelope.peer_operation_id,
                    None,
                ),
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return DedupeReservation(accepted=True, reason="accepted")

    def mark_dispatched(
        self,
        dispatch_idempotency_key: str,
        peer_operation_id: str | None = None,
    ) -> None:
        """Mark record as dispatched.

        Args:
            dispatch_idempotency_key: Key to mark as dispatched.
            peer_operation_id: Optional peer-side operation reference.

        Raises:
            DedupeStoreStateError: If state transition is invalid.

        """
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                record = self._get_required_record(dispatch_idempotency_key)
                if record.state not in ("reserved", "dispatched"):
                    raise DedupeStoreStateError(f"Cannot transition {record.state} -> dispatched.")
                self._update_state(
                    dispatch_idempotency_key=dispatch_idempotency_key,
                    state="dispatched",
                    peer_operation_id=peer_operation_id or record.peer_operation_id,
                    external_ack_ref=record.external_ack_ref,
                )
                self._conn.execute("COMMIT")
            except Exception:
                with contextlib.suppress(Exception):
                    self._conn.execute("ROLLBACK")
                raise

    def reserve_and_dispatch(
        self,
        envelope: IdempotencyEnvelope,
        peer_operation_id: str | None = None,
    ) -> DedupeReservation:
        """Atomically reserves and marks envelope as dispatched.

        Uses a single BEGIN IMMEDIATE transaction to eliminate the non-atomic
        window between reservation and dispatch state update.  If a duplicate
        key exists, returns a rejected reservation without modifying state.

        Args:
            envelope: Idempotency envelope to reserve and dispatch.
            peer_operation_id: Optional peer-side operation reference.

        Returns:
            Reservation result indicating acceptance or duplicate.

        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                existing_record = self.get(envelope.dispatch_idempotency_key)
                if existing_record is not None:
                    self._conn.execute("ROLLBACK")
                    return DedupeReservation(
                        accepted=False,
                        reason="duplicate",
                        existing_record=existing_record,
                    )

                cursor = self._conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO dedupe_store (
                      dispatch_idempotency_key,
                      operation_fingerprint,
                      attempt_seq,
                      state,
                      peer_operation_id,
                      external_ack_ref
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        envelope.dispatch_idempotency_key,
                        envelope.operation_fingerprint,
                        envelope.attempt_seq,
                        "dispatched",
                        peer_operation_id or envelope.peer_operation_id,
                        None,
                    ),
                )
                self._conn.execute("COMMIT")
            except Exception:
                with contextlib.suppress(Exception):
                    self._conn.execute("ROLLBACK")
                raise
        return DedupeReservation(accepted=True, reason="accepted")

    def mark_acknowledged(
        self,
        dispatch_idempotency_key: str,
        external_ack_ref: str | None = None,
    ) -> None:
        """Mark record as acknowledged.

        Args:
            dispatch_idempotency_key: Key to mark as acknowledged.
            external_ack_ref: Optional external acknowledgement reference.

        Raises:
            DedupeStoreStateError: If state transition is invalid.

        """
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                record = self._get_required_record(dispatch_idempotency_key)
                if record.state not in ("dispatched", "acknowledged"):
                    raise DedupeStoreStateError(
                        f"Cannot transition {record.state} -> acknowledged."
                    )
                self._update_state(
                    dispatch_idempotency_key=dispatch_idempotency_key,
                    state="acknowledged",
                    peer_operation_id=record.peer_operation_id,
                    external_ack_ref=external_ack_ref or record.external_ack_ref,
                )
                self._conn.execute("COMMIT")
            except Exception:
                with contextlib.suppress(Exception):
                    self._conn.execute("ROLLBACK")
                raise

    def mark_succeeded(
        self,
        dispatch_idempotency_key: str,
        external_ack_ref: str | None = None,
    ) -> None:
        """Mark record as succeeded 鈥?result confirmed and evidence collectible.

        Args:
            dispatch_idempotency_key: Key to mark as succeeded.
            external_ack_ref: Optional external acknowledgement reference.

        Raises:
            DedupeStoreStateError: If state transition is invalid.

        """
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                record = self._get_required_record(dispatch_idempotency_key)
                if record.state not in ("acknowledged", "succeeded"):
                    raise DedupeStoreStateError(f"Cannot transition {record.state} -> succeeded.")
                self._update_state(
                    dispatch_idempotency_key=dispatch_idempotency_key,
                    state="succeeded",
                    peer_operation_id=record.peer_operation_id,
                    external_ack_ref=external_ack_ref or record.external_ack_ref,
                )
                self._conn.execute("COMMIT")
            except Exception:
                with contextlib.suppress(Exception):
                    self._conn.execute("ROLLBACK")
                raise

    def mark_unknown_effect(self, dispatch_idempotency_key: str) -> None:
        """Mark record as unknown_effect.

        Args:
            dispatch_idempotency_key: Key to mark as unknown effect.

        Raises:
            DedupeStoreStateError: If state transition is invalid.

        """
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                record = self._get_required_record(dispatch_idempotency_key)
                if record.state not in ("dispatched", "unknown_effect"):
                    raise DedupeStoreStateError(
                        f"Cannot transition {record.state} -> unknown_effect."
                    )
                self._update_state(
                    dispatch_idempotency_key=dispatch_idempotency_key,
                    state="unknown_effect",
                    peer_operation_id=record.peer_operation_id,
                    external_ack_ref=record.external_ack_ref,
                )
                self._conn.execute("COMMIT")
            except Exception:
                with contextlib.suppress(Exception):
                    self._conn.execute("ROLLBACK")
                raise

    def get(self, dispatch_idempotency_key: str) -> DedupeRecord | None:
        """Get dedupe record by key.

        Args:
            dispatch_idempotency_key: Key to look up.

        Returns:
            Matching dedupe record, or ``None`` if not found.

        """
        with self._lock:
            if self._pool is not None and not self._conn.in_transaction:
                with self._pool.read_connection() as read_conn:
                    row = read_conn.execute(
                        """
                        SELECT
                          dispatch_idempotency_key,
                          operation_fingerprint,
                          attempt_seq,
                          state,
                          peer_operation_id,
                          external_ack_ref
                        FROM dedupe_store
                        WHERE dispatch_idempotency_key = ?
                        """,
                        (dispatch_idempotency_key,),
                    ).fetchone()
            else:
                row = self._conn.execute(
                    """
                    SELECT
                      dispatch_idempotency_key,
                      operation_fingerprint,
                      attempt_seq,
                      state,
                      peer_operation_id,
                      external_ack_ref
                    FROM dedupe_store
                    WHERE dispatch_idempotency_key = ?
                    """,
                    (dispatch_idempotency_key,),
                ).fetchone()
            if row is None:
                return None
            return DedupeRecord(
                dispatch_idempotency_key=row["dispatch_idempotency_key"],
                operation_fingerprint=row["operation_fingerprint"],
                attempt_seq=row["attempt_seq"],
                state=row["state"],
                peer_operation_id=row["peer_operation_id"],
                external_ack_ref=row["external_ack_ref"],
            )

    def _get_required_record(self, dispatch_idempotency_key: str) -> DedupeRecord:
        """Get record by key or raises.

        Args:
            dispatch_idempotency_key: Key to look up.

        Returns:
            Matching dedupe record.

        Raises:
            DedupeStoreStateError: If no record exists for the key.

        """
        record = self.get(dispatch_idempotency_key)
        if record is None:
            raise DedupeStoreStateError(
                f"Unknown dispatch_idempotency_key: {dispatch_idempotency_key}."
            )
        return record

    def _update_state(
        self,
        dispatch_idempotency_key: str,
        state: str,
        peer_operation_id: str | None,
        external_ack_ref: str | None,
    ) -> None:
        """Update state and optional references for one record.

        Args:
            dispatch_idempotency_key: Key of the record to update.
            state: New monotonic state value.
            peer_operation_id: Optional updated peer operation reference.
            external_ack_ref: Optional updated external acknowledgement.

        """
        cursor = self._conn.cursor()
        cursor.execute(
            """
            UPDATE dedupe_store
            SET state = ?,
                peer_operation_id = ?,
                external_ack_ref = ?
            WHERE dispatch_idempotency_key = ?
            """,
            (
                state,
                peer_operation_id,
                external_ack_ref,
                dispatch_idempotency_key,
            ),
        )
        if cursor.rowcount != 1:
            raise DedupeStoreStateError(
                f"Lost-update: key {dispatch_idempotency_key!r} not found "
                f"during state transition to {state!r}."
            )

    def _ensure_schema(self) -> None:
        """Create dedupe table if it does not exist."""
        cursor = self._conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS dedupe_store (
              dispatch_idempotency_key TEXT PRIMARY KEY,
              operation_fingerprint TEXT NOT NULL,
              attempt_seq INTEGER NOT NULL,
              state TEXT NOT NULL,
              peer_operation_id TEXT NULL,
              external_ack_ref TEXT NULL
            )
            """)
        # isolation_level=None (autocommit) 鈥?no explicit commit needed.
