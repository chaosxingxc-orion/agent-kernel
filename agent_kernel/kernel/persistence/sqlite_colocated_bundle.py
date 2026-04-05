"""ColocatedSQLiteBundle — shared SQLite connection for EventLog + DedupeStore.

Enables atomic dispatch-record writes (dedupe reservation + event append) in a
single SQLite transaction, eliminating the window where a crash between the two
separate-connection writes leaves them inconsistent.

Usage::

    bundle = ColocatedSQLiteBundle("agent.db")
    bundle.initialize_schema()

    # Atomic dispatch: reserve dedupe key AND append event in one transaction.
    bundle.atomic_dispatch_record(commit, envelope)

    # Individual stores remain available for non-atomic operations.
    _ = bundle.event_log.load(run_id="r1")
    _ = bundle.dedupe_store.get("key")

    bundle.close()
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

from agent_kernel.kernel.contracts import ActionCommit, RuntimeEvent
from agent_kernel.kernel.dedupe_store import (
    DedupeRecord,
    DedupeReservation,
    DedupeStoreStateError,
    IdempotencyEnvelope,
)

# ---------------------------------------------------------------------------
# Shared-connection EventLog view
# ---------------------------------------------------------------------------


class _SharedConnectionEventLog:
    """EventLog that operates on a caller-supplied SQLite connection.

    This is an internal helper for ``ColocatedSQLiteBundle``; not intended for
    standalone use.  All methods acquire ``_lock`` and operate within the
    provided connection — the bundle owner manages lifecycle (open/close).
    """

    def __init__(self, conn: sqlite3.Connection, lock: threading.Lock) -> None:
        self._conn = conn
        self._lock = lock

    async def append_action_commit(self, commit: ActionCommit) -> str:
        """Appends one action commit inside the shared connection.

        Args:
            commit: The action commit containing events to persist.

        Returns:
            A commit reference string encoding the sequence number.

        Raises:
            ValueError: If ``commit.events`` is empty.
        """
        if not commit.events:
            raise ValueError("ActionCommit.events must contain at least one event.")
        with self._lock:
            try:
                self._conn.execute("BEGIN")
                next_offset = self._next_offset(commit.run_id)
                seq = self._insert_commit_row(commit)
                self._insert_event_rows(commit.run_id, seq, commit.events, next_offset)
                self._conn.execute("COMMIT")
            except Exception:
                with contextlib.suppress(Exception):
                    self._conn.execute("ROLLBACK")
                raise
        return f"commit-ref-{seq}"

    async def load(self, run_id: str, after_offset: int = 0) -> list[RuntimeEvent]:
        """Loads events for a run after ``after_offset``.
        Args:
            run_id:
            after_offset:
        Returns:
            list[RuntimeEvent]:
        """
        query = """
            SELECT
                event_run_id, event_id, commit_offset, event_type, event_class,
                event_authority, ordering_key, wake_policy, created_at,
                idempotency_key, payload_ref, payload_json
            FROM colocated_runtime_events
            WHERE stream_run_id = ? AND commit_offset > ?
            ORDER BY commit_offset ASC
        """
        with self._lock:
            rows = self._conn.execute(query, (run_id, after_offset)).fetchall()
        return [self._row_to_event(row) for row in rows]

    async def max_offset(self, run_id: str) -> int:
        """Returns the highest committed offset for a run, or 0.

        Args:
            run_id: Target run identifier.

        Returns:
            Maximum commit offset, or 0 when no events exist.
        """
        with self._lock:
            cursor = self._conn.execute(
                "SELECT COALESCE(MAX(commit_offset), 0) FROM colocated_runtime_events "
                "WHERE stream_run_id = ?",
                (run_id,),
            )
            return int(cursor.fetchone()[0])

    # --- private helpers ---

    def _next_offset(self, run_id: str) -> int:
        cursor = self._conn.execute(
            "SELECT COALESCE(MAX(commit_offset), 0) FROM colocated_runtime_events "
            "WHERE stream_run_id = ?",
            (run_id,),
        )
        return int(cursor.fetchone()[0]) + 1

    def _insert_commit_row(self, commit: ActionCommit) -> int:
        cursor = self._conn.execute(
            """
            INSERT INTO colocated_action_commits (
                stream_run_id, commit_id, created_at, event_count
            ) VALUES (?, ?, ?, ?)
            """,
            (commit.run_id, commit.commit_id, commit.created_at, len(commit.events)),
        )
        return int(cursor.lastrowid)

    def _insert_event_rows(
        self,
        run_id: str,
        seq: int,
        events: list[RuntimeEvent],
        start_offset: int,
    ) -> None:
        query = """
            INSERT INTO colocated_runtime_events (
                commit_sequence, event_index, stream_run_id, event_run_id,
                commit_offset, event_id, event_type, event_class, event_authority,
                ordering_key, wake_policy, created_at, idempotency_key,
                payload_ref, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        for idx, event in enumerate(events):
            self._conn.execute(
                query,
                (
                    seq,
                    idx,
                    run_id,
                    event.run_id,
                    start_offset + idx,
                    event.event_id,
                    event.event_type,
                    event.event_class,
                    event.event_authority,
                    event.ordering_key,
                    event.wake_policy,
                    event.created_at,
                    event.idempotency_key,
                    event.payload_ref,
                    json.dumps(event.payload_json) if event.payload_json is not None else None,
                ),
            )

    def _row_to_event(self, row: sqlite3.Row) -> RuntimeEvent:
        payload_raw = row["payload_json"]
        payload = json.loads(payload_raw) if payload_raw is not None else None
        return RuntimeEvent(
            run_id=str(row["event_run_id"]),
            event_id=str(row["event_id"]),
            commit_offset=int(row["commit_offset"]),
            event_type=str(row["event_type"]),
            event_class=str(row["event_class"]),
            event_authority=str(row["event_authority"]),
            ordering_key=str(row["ordering_key"]),
            wake_policy=str(row["wake_policy"]),
            created_at=str(row["created_at"]),
            idempotency_key=row["idempotency_key"] or None,
            payload_ref=row["payload_ref"] or None,
            payload_json=payload,
        )


# ---------------------------------------------------------------------------
# Shared-connection DedupeStore view
# ---------------------------------------------------------------------------


class _SharedConnectionDedupeStore:
    """DedupeStore that operates on a caller-supplied SQLite connection.

    Like ``_SharedConnectionEventLog``, this is an internal helper only.
    """

    def __init__(self, conn: sqlite3.Connection, lock: threading.Lock) -> None:
        self._conn = conn
        self._lock = lock

    def reserve(self, envelope: IdempotencyEnvelope) -> DedupeReservation:
        """Reserves dispatch idempotency key if absent.

        Args:
            envelope: Idempotency envelope carrying the deduplication key.

        Returns:
            ``DedupeReservation`` indicating acceptance or rejection.

        Raises:
            DedupeStoreStateError: If the key already exists in an incompatible state.
        """
        # (thread-safe)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._get(envelope.dispatch_idempotency_key)
                if existing is not None:
                    self._conn.execute("ROLLBACK")
                    return DedupeReservation(
                        accepted=False,
                        reason="duplicate",
                        existing_record=existing,
                    )
                self._conn.execute(
                    """
                    INSERT INTO colocated_dedupe_store (
                        dispatch_idempotency_key, operation_fingerprint,
                        attempt_seq, state, peer_operation_id, external_ack_ref
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

    def reserve_and_dispatch(
        self,
        envelope: IdempotencyEnvelope,
        peer_operation_id: str | None = None,
    ) -> DedupeReservation:
        """Atomically reserves and marks envelope as dispatched.

        Uses a single BEGIN IMMEDIATE transaction to eliminate the non-atomic
        window between reservation and dispatch state update.

        Args:
            envelope: Idempotency envelope to reserve and dispatch.
            peer_operation_id: Optional peer-side operation reference.

        Returns:
            Reservation result indicating acceptance or duplicate.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._get(envelope.dispatch_idempotency_key)
                if existing is not None:
                    self._conn.execute("ROLLBACK")
                    return DedupeReservation(
                        accepted=False,
                        reason="duplicate",
                        existing_record=existing,
                    )
                self._conn.execute(
                    """
                    INSERT INTO colocated_dedupe_store (
                        dispatch_idempotency_key, operation_fingerprint,
                        attempt_seq, state, peer_operation_id, external_ack_ref
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

    def mark_dispatched(
        self, dispatch_idempotency_key: str, peer_operation_id: str | None = None
    ) -> None:
        """Transitions record to ``dispatched`` state.
        Args:
            dispatch_idempotency_key:
            peer_operation_id:
        Raises:
            Exception:
        """
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                record = self._get(dispatch_idempotency_key)
                if record is None:
                    raise DedupeStoreStateError(
                        f"Unknown dispatch_idempotency_key: {dispatch_idempotency_key}."
                    )
                if record.state not in ("reserved", "dispatched"):
                    raise DedupeStoreStateError(f"Cannot transition {record.state} -> dispatched.")
                cursor = self._conn.execute(
                    """
                    UPDATE colocated_dedupe_store
                    SET state = ?, peer_operation_id = ?, external_ack_ref = ?
                    WHERE dispatch_idempotency_key = ?
                    """,
                    (
                        "dispatched",
                        peer_operation_id or record.peer_operation_id,
                        record.external_ack_ref,
                        dispatch_idempotency_key,
                    ),
                )
                if cursor.rowcount != 1:
                    raise DedupeStoreStateError(
                        f"Lost-update: key {dispatch_idempotency_key!r} disappeared."
                    )
                self._conn.execute("COMMIT")
            except Exception:
                with contextlib.suppress(Exception):
                    self._conn.execute("ROLLBACK")
                raise

    def mark_acknowledged(
        self, dispatch_idempotency_key: str, external_ack_ref: str | None = None
    ) -> None:
        """Transitions record to ``acknowledged`` state.
        Args:
            dispatch_idempotency_key:
            external_ack_ref:
        Raises:
            Exception:
        """
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                record = self._get(dispatch_idempotency_key)
                if record is None:
                    raise DedupeStoreStateError(
                        f"Unknown dispatch_idempotency_key: {dispatch_idempotency_key}."
                    )
                if record.state not in ("dispatched", "acknowledged"):
                    raise DedupeStoreStateError(
                        f"Cannot transition {record.state} -> acknowledged."
                    )
                cursor = self._conn.execute(
                    """
                    UPDATE colocated_dedupe_store
                    SET state = ?, peer_operation_id = ?, external_ack_ref = ?
                    WHERE dispatch_idempotency_key = ?
                    """,
                    (
                        "acknowledged",
                        record.peer_operation_id,
                        external_ack_ref or record.external_ack_ref,
                        dispatch_idempotency_key,
                    ),
                )
                if cursor.rowcount != 1:
                    raise DedupeStoreStateError(
                        f"Lost-update: key {dispatch_idempotency_key!r} disappeared."
                    )
                self._conn.execute("COMMIT")
            except Exception:
                with contextlib.suppress(Exception):
                    self._conn.execute("ROLLBACK")
                raise

    def mark_unknown_effect(self, dispatch_idempotency_key: str) -> None:
        """Transitions record to ``unknown_effect`` state.
        Args:
            dispatch_idempotency_key:
        Raises:
            Exception:
        """
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                record = self._get(dispatch_idempotency_key)
                if record is None:
                    raise DedupeStoreStateError(
                        f"Unknown dispatch_idempotency_key: {dispatch_idempotency_key}."
                    )
                if record.state not in ("dispatched", "unknown_effect"):
                    raise DedupeStoreStateError(
                        f"Cannot transition {record.state} -> unknown_effect."
                    )
                cursor = self._conn.execute(
                    """
                    UPDATE colocated_dedupe_store
                    SET state = ?, peer_operation_id = ?, external_ack_ref = ?
                    WHERE dispatch_idempotency_key = ?
                    """,
                    (
                        "unknown_effect",
                        record.peer_operation_id,
                        record.external_ack_ref,
                        dispatch_idempotency_key,
                    ),
                )
                if cursor.rowcount != 1:
                    raise DedupeStoreStateError(
                        f"Lost-update: key {dispatch_idempotency_key!r} disappeared."
                    )
                self._conn.execute("COMMIT")
            except Exception:
                with contextlib.suppress(Exception):
                    self._conn.execute("ROLLBACK")
                raise

    def get(self, dispatch_idempotency_key: str) -> DedupeRecord | None:
        """Returns dedupe record by key, or ``None``.
        Args:
            dispatch_idempotency_key:
        Returns:
            DedupeRecord | None:
        """
        with self._lock:
            return self._get(dispatch_idempotency_key)

    # --- private helpers (caller must hold lock) ---

    def _get(self, key: str) -> DedupeRecord | None:
        cursor = self._conn.execute(
            """
            SELECT dispatch_idempotency_key, operation_fingerprint,
                   attempt_seq, state, peer_operation_id, external_ack_ref
            FROM colocated_dedupe_store
            WHERE dispatch_idempotency_key = ?
            """,
            (key,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return DedupeRecord(
            dispatch_idempotency_key=row[0],
            operation_fingerprint=row[1],
            attempt_seq=row[2],
            state=row[3],
            peer_operation_id=row[4],
            external_ack_ref=row[5],
        )

    def _require(self, key: str) -> DedupeRecord:
        with self._lock:
            record = self._get(key)
        if record is None:
            raise DedupeStoreStateError(f"Unknown dispatch_idempotency_key: {key}.")
        return record

    def _update(
        self,
        key: str,
        state: str,
        peer_operation_id: str | None,
        external_ack_ref: str | None,
    ) -> None:
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE colocated_dedupe_store
                SET state = ?, peer_operation_id = ?, external_ack_ref = ?
                WHERE dispatch_idempotency_key = ?
                """,
                (state, peer_operation_id, external_ack_ref, key),
            )
            if cursor.rowcount != 1:
                raise DedupeStoreStateError(
                    f"Lost-update: key {key!r} not found during state transition to {state!r}."
                )


# ---------------------------------------------------------------------------
# ColocatedSQLiteBundle
# ---------------------------------------------------------------------------


class ColocatedSQLiteBundle:
    """Shared SQLite connection bundle for EventLog + DedupeStore.

    Both stores operate on the same SQLite connection, enabling
    ``atomic_dispatch_record()`` to reserve a dedupe key AND append an event
    commit in a single transaction — eliminating the crash window between
    two independent writes.

    Attributes:
        event_log: EventLog view backed by the shared connection.
        dedupe_store: DedupeStore view backed by the shared connection.
    """

    def __init__(self, database_path: str | Path = ":memory:") -> None:
        """Opens the shared SQLite database and initializes schema.

        Args:
            database_path: SQLite file path. Use ``":memory:"`` for in-memory mode.
        """
        self._database_path = str(database_path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            self._database_path,
            isolation_level=None,  # autocommit; transactions managed explicitly
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._initialize_schema()

        self.event_log = _SharedConnectionEventLog(self._conn, self._lock)
        self.dedupe_store = _SharedConnectionDedupeStore(self._conn, self._lock)

    def close(self) -> None:
        """Closes the shared connection after a best-effort WAL checkpoint."""
        with contextlib.suppress(Exception):
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self._conn.close()

    def atomic_dispatch_record(
        self,
        commit: ActionCommit,
        envelope: IdempotencyEnvelope,
    ) -> tuple[str, DedupeReservation]:
        """Reserves dedupe key and appends event commit in one transaction.

        This is the primary value of colocation: both the DedupeStore
        reservation and the EventLog append succeed or fail together, removing
        the inconsistency window present when using two separate connections.

        Args:
            commit: Action commit to append to the event log.
            envelope: Idempotency envelope to reserve in the dedupe store.

        Returns:
            Tuple of (commit_ref, reservation) where ``reservation.accepted``
            is ``False`` when the key was already reserved (duplicate).

        Raises:
            ValueError: If ``commit.events`` is empty.
        """
        if not commit.events:
            raise ValueError("ActionCommit.events must contain at least one event.")

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                # Dedupe reservation check first — cheapest gate.
                existing = self.dedupe_store._get(envelope.dispatch_idempotency_key)
                if existing is not None:
                    self._conn.execute("ROLLBACK")
                    return (
                        "",
                        DedupeReservation(
                            accepted=False,
                            reason="duplicate",
                            existing_record=existing,
                        ),
                    )

                # Reserve dedupe key.
                self._conn.execute(
                    """
                    INSERT INTO colocated_dedupe_store (
                        dispatch_idempotency_key, operation_fingerprint,
                        attempt_seq, state, peer_operation_id, external_ack_ref
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

                # Append event commit.
                next_offset = self.event_log._next_offset(commit.run_id)
                seq = self.event_log._insert_commit_row(commit)
                self.event_log._insert_event_rows(commit.run_id, seq, commit.events, next_offset)

                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

        return (
            f"commit-ref-{seq}",
            DedupeReservation(accepted=True, reason="accepted"),
        )

    def _initialize_schema(self) -> None:
        """Creates colocated tables and indexes when absent."""
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS colocated_action_commits (
                commit_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                stream_run_id TEXT NOT NULL,
                commit_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                event_count INTEGER NOT NULL CHECK (event_count > 0)
            );

            CREATE TABLE IF NOT EXISTS colocated_runtime_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                commit_sequence INTEGER NOT NULL,
                event_index INTEGER NOT NULL,
                stream_run_id TEXT NOT NULL,
                event_run_id TEXT NOT NULL,
                commit_offset INTEGER NOT NULL,
                event_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                event_class TEXT NOT NULL,
                event_authority TEXT NOT NULL,
                ordering_key TEXT NOT NULL,
                wake_policy TEXT NOT NULL,
                created_at TEXT NOT NULL,
                idempotency_key TEXT,
                payload_ref TEXT,
                payload_json TEXT,
                FOREIGN KEY (commit_sequence)
                    REFERENCES colocated_action_commits(commit_sequence)
                    ON DELETE CASCADE,
                UNIQUE (stream_run_id, commit_offset),
                UNIQUE (commit_sequence, event_index)
            );

            CREATE INDEX IF NOT EXISTS idx_colocated_events_stream_offset
                ON colocated_runtime_events(stream_run_id, commit_offset);

            CREATE TABLE IF NOT EXISTS colocated_dedupe_store (
                dispatch_idempotency_key TEXT PRIMARY KEY,
                operation_fingerprint TEXT NOT NULL,
                attempt_seq INTEGER NOT NULL,
                state TEXT NOT NULL,
                peer_operation_id TEXT,
                external_ack_ref TEXT
            );
            """)
