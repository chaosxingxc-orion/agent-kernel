"""SQLite-backed implementation of the kernel runtime event log contract."""

from __future__ import annotations

import json
import sqlite3
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from agent_kernel.kernel.persistence.sqlite_pool import SQLiteConnectionPool

from agent_kernel.kernel.contracts import (
    ActionCommit,
    KernelRuntimeEventLog,
    RuntimeEvent,
)


class SQLiteKernelRuntimeEventLog(KernelRuntimeEventLog):
    """Persists runtime events in SQLite with per-run monotonic offsets.

    This implementation keeps behavior aligned with
    ``InMemoryKernelRuntimeEventLog``:
    - appends require at least one event,
    - incoming ``commit_offset`` values are ignored,
    - new offsets are assigned sequentially per ``commit.run_id``,
    - reads return events ordered by ascending offset.
    """

    def __init__(
        self,
        database_path: str | Path,
        pool: SQLiteConnectionPool | None = None,
        busy_timeout_ms: int = 5000,
    ) -> None:
        """Initialize one SQLite event log instance.

        Args:
            database_path: SQLite file path. Use ``":memory:"`` for in-memory mode.
            pool: Optional shared SQLite connection pool.
            busy_timeout_ms: SQLite busy-timeout window in milliseconds for
                lock contention waits.
        """
        self._database_path = str(database_path)
        self._lock = threading.Lock()
        self._pool = pool
        if self._pool is None:
            self._connection = sqlite3.connect(self._database_path, check_same_thread=False)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute(f"PRAGMA busy_timeout={max(0, busy_timeout_ms)}")
        else:
            self._connection = self._pool.acquire_write()
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute(f"PRAGMA busy_timeout={max(0, busy_timeout_ms)}")
        self._initialize_schema()

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        with self._lock:
            if self._pool is None:
                self._connection.close()

    async def append_action_commit(self, commit: ActionCommit) -> str:
        """Append one action commit and normalizes offsets for the run.

        Args:
            commit: Commit envelope that contains one or more runtime events.

        Returns:
            Commit reference identifier.

        Raises:
            ValueError: If ``commit.events`` is empty.

        """
        if not commit.events:
            raise ValueError("ActionCommit.events must contain at least one event.")

        with self._lock, self._connection:
            next_offset = self._load_next_offset(commit.run_id)
            commit_sequence = self._insert_commit_row(commit=commit)
            self._insert_event_rows(
                stream_run_id=commit.run_id,
                commit_sequence=commit_sequence,
                events=commit.events,
                starting_offset=next_offset,
            )
        return f"commit-ref-{commit_sequence}"

    async def load(self, run_id: str, after_offset: int = 0) -> list[RuntimeEvent]:
        """Load run events after ``after_offset`` in ascending offset order.

        Args:
            run_id: Run identifier to load events for.
            after_offset: Exclusive lower bound offset.

        Returns:
            Ordered list of runtime events after the specified offset.

        """
        query = """
            SELECT
                event_run_id,
                event_id,
                commit_offset,
                event_type,
                event_class,
                event_authority,
                ordering_key,
                wake_policy,
                created_at,
                idempotency_key,
                payload_ref,
                payload_json
            FROM runtime_events
            WHERE stream_run_id = ? AND commit_offset > ?
            ORDER BY commit_offset ASC
        """
        with self._lock:
            if self._pool is None:
                rows = self._connection.execute(query, (run_id, after_offset)).fetchall()
            else:
                with self._pool.read_connection() as read_conn:
                    rows = read_conn.execute(query, (run_id, after_offset)).fetchall()
        return [self._row_to_runtime_event(row) for row in rows]

    async def max_offset(self, run_id: str) -> int:
        """Return the highest committed offset for a run, or 0 when empty.

        Args:
            run_id: Run identifier to query.

        Returns:
            Highest ``commit_offset`` value in the log, or 0 when no events exist.

        """
        with self._lock:
            query = """
                SELECT COALESCE(MAX(commit_offset), 0)
                FROM runtime_events
                WHERE stream_run_id = ?
            """
            if self._pool is None:
                cursor = self._connection.execute(query, (run_id,))
            else:
                with self._pool.read_connection() as read_conn:
                    cursor = read_conn.execute(query, (run_id,))
            return int(cursor.fetchone()[0])

    def _initialize_schema(self) -> None:
        """Create required tables and indexes when absent."""
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS action_commits (
                commit_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                stream_run_id TEXT NOT NULL,
                commit_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                event_count INTEGER NOT NULL CHECK (event_count > 0)
            );

            CREATE TABLE IF NOT EXISTS runtime_events (
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
                    REFERENCES action_commits(commit_sequence)
                    ON DELETE CASCADE,
                UNIQUE (stream_run_id, commit_offset),
                UNIQUE (commit_sequence, event_index)
            );

            CREATE INDEX IF NOT EXISTS idx_runtime_events_stream_offset
                ON runtime_events(stream_run_id, commit_offset);
            """
        )

    def _load_next_offset(self, stream_run_id: str) -> int:
        """Return the next offset to assign for one run stream.

        Args:
            stream_run_id: Run stream identifier.

        Returns:
            Next sequential offset value.

        """
        cursor = self._connection.execute(
            """
            SELECT COALESCE(MAX(commit_offset), 0)
            FROM runtime_events
            WHERE stream_run_id = ?
            """,
            (stream_run_id,),
        )
        max_offset = int(cursor.fetchone()[0])
        return max_offset + 1

    def _insert_commit_row(self, commit: ActionCommit) -> int:
        """Insert one commit metadata row and returns commit sequence.

        Args:
            commit: Action commit to persist.

        Returns:
            Auto-generated commit sequence identifier.

        """
        cursor = self._connection.execute(
            """
            INSERT INTO action_commits (
                stream_run_id,
                commit_id,
                created_at,
                event_count
            ) VALUES (?, ?, ?, ?)
            """,
            (commit.run_id, commit.commit_id, commit.created_at, len(commit.events)),
        )
        return int(cursor.lastrowid)

    def _insert_event_rows(
        self,
        stream_run_id: str,
        commit_sequence: int,
        events: list[RuntimeEvent],
        starting_offset: int,
    ) -> None:
        """Insert runtime event rows in their original commit input order.

        Args:
            stream_run_id: Run stream identifier for event partitioning.
            commit_sequence: Parent commit sequence foreign key.
            events: Ordered list of runtime events to persist.
            starting_offset: First offset to assign in this commit.

        """
        query = """
            INSERT INTO runtime_events (
                commit_sequence,
                event_index,
                stream_run_id,
                event_run_id,
                commit_offset,
                event_id,
                event_type,
                event_class,
                event_authority,
                ordering_key,
                wake_policy,
                created_at,
                idempotency_key,
                payload_ref,
                payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        for event_index, event in enumerate(events):
            self._connection.execute(
                query,
                (
                    commit_sequence,
                    event_index,
                    stream_run_id,
                    event.run_id,
                    starting_offset + event_index,
                    event.event_id,
                    event.event_type,
                    event.event_class,
                    event.event_authority,
                    event.ordering_key,
                    event.wake_policy,
                    event.created_at,
                    event.idempotency_key,
                    event.payload_ref,
                    _serialize_payload(event.payload_json),
                ),
            )

    def _row_to_runtime_event(self, row: sqlite3.Row) -> RuntimeEvent:
        """Convert one database row into ``RuntimeEvent``.

        Args:
            row: SQLite row to convert.

        Returns:
            Typed runtime event from row fields.

        """
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
            idempotency_key=_as_optional_str(row["idempotency_key"]),
            payload_ref=_as_optional_str(row["payload_ref"]),
            payload_json=_deserialize_payload(row["payload_json"]),
        )


def _serialize_payload(payload_json: dict[str, Any] | None) -> str | None:
    """Serialize payload JSON into SQLite text storage.

    Args:
        payload_json: Optional payload dictionary to serialize.

    Returns:
        Compact JSON string, or ``None`` when input is ``None``.

    """
    if payload_json is None:
        return None
    return json.dumps(payload_json, separators=(",", ":"), sort_keys=True)


def _deserialize_payload(raw_payload: Any) -> dict[str, Any] | None:
    """Deserializes payload JSON from SQLite text storage.

    Args:
        raw_payload: Raw SQLite text value to deserialize.

    Returns:
        Parsed dictionary, or ``None`` when input is ``None``.

    """
    if raw_payload is None:
        return None
    return json.loads(str(raw_payload))


def _as_optional_str(value: Any) -> str | None:
    """Normalize nullable SQLite values to optional strings.

    Args:
        value: Nullable value to normalize.

    Returns:
        String representation, or ``None`` when input is ``None``.

    """
    if value is None:
        return None
    return str(value)
