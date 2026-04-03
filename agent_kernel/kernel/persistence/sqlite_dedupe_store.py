"""SQLite-backed DedupeStore for v6.4 idempotency persistence windows."""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path

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

    def __init__(self, database_path: str | Path = ":memory:") -> None:
        """Initializes one SQLite dedupe store.

        Args:
            database_path: SQLite file path. Use ``":memory:"`` for
                in-memory mode.
        """
        self._database_path = str(database_path)
        self._conn = sqlite3.connect(self._database_path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        # WAL mode allows concurrent readers while one writer is active.
        # NORMAL sync is safe with WAL — durable on OS crash for PoC use.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._ensure_schema()

    def close(self) -> None:
        """Closes SQLite connection after checkpointing the WAL file.

        Checkpointing (TRUNCATE mode) ensures WAL contents are merged back
        into the main database file and the WAL file is reset to zero bytes.
        This reclaims disk space and ensures durability before process exit.
        The checkpoint is best-effort — failures are silently suppressed so
        that a crashed connection can still be closed.
        """
        import contextlib

        with contextlib.suppress(Exception):
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self._conn.close()

    def reserve(self, envelope: IdempotencyEnvelope) -> DedupeReservation:
        """Reserves dispatch idempotency key if absent.

        Args:
            envelope: Idempotency envelope to reserve.

        Returns:
            Reservation result indicating acceptance or duplicate.
        """
        # BEGIN IMMEDIATE acquires a write lock upfront, preventing TOCTOU
        # between the existence check and the INSERT across concurrent processes.
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
        """Marks record as dispatched.

        Args:
            dispatch_idempotency_key: Key to mark as dispatched.
            peer_operation_id: Optional peer-side operation reference.

        Raises:
            DedupeStoreStateError: If state transition is invalid.
        """
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            record = self._get_required_record(dispatch_idempotency_key)
            if record.state not in ("reserved", "dispatched"):
                raise DedupeStoreStateError(
                    f"Cannot transition {record.state} -> dispatched."
                )
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

    def mark_acknowledged(
        self,
        dispatch_idempotency_key: str,
        external_ack_ref: str | None = None,
    ) -> None:
        """Marks record as acknowledged.

        Args:
            dispatch_idempotency_key: Key to mark as acknowledged.
            external_ack_ref: Optional external acknowledgement reference.

        Raises:
            DedupeStoreStateError: If state transition is invalid.
        """
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

    def mark_unknown_effect(self, dispatch_idempotency_key: str) -> None:
        """Marks record as unknown_effect.

        Args:
            dispatch_idempotency_key: Key to mark as unknown effect.

        Raises:
            DedupeStoreStateError: If state transition is invalid.
        """
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
        """Gets dedupe record by key.

        Args:
            dispatch_idempotency_key: Key to look up.

        Returns:
            Matching dedupe record, or ``None`` if not found.
        """
        cursor = self._conn.cursor()
        cursor.execute(
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
        )
        row = cursor.fetchone()
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
        """Gets record by key or raises.

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
        """Updates state and optional references for one record.

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
        """Creates dedupe table if it does not exist."""
        cursor = self._conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS dedupe_store (
              dispatch_idempotency_key TEXT PRIMARY KEY,
              operation_fingerprint TEXT NOT NULL,
              attempt_seq INTEGER NOT NULL,
              state TEXT NOT NULL,
              peer_operation_id TEXT NULL,
              external_ack_ref TEXT NULL
            )
            """
        )
        # isolation_level=None (autocommit) — no explicit commit needed.
