"""SQLite-backed CircuitBreakerStore for cross-run circuit breaker persistence."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path


class SQLiteCircuitBreakerStore:
    """Persists circuit breaker state in SQLite for cross-run fault isolation.

    Each effect_class has a single row tracking failure count and the Unix
    epoch timestamp of the most recent failure.  Timestamps are stored as
    Unix epoch floats so they are meaningful across process restarts (unlike
    ``time.monotonic()`` which resets per process).

    A single shared connection is kept open for the lifetime of the store.
    This is necessary for ``:memory:`` databases (each new connection would
    create a distinct empty database) and also avoids repeated connection
    overhead for file-backed stores.
    """

    _CREATE_TABLE = """
        CREATE TABLE IF NOT EXISTS circuit_breaker_state (
            effect_class    TEXT    PRIMARY KEY,
            failure_count   INTEGER NOT NULL DEFAULT 0,
            last_failure_ts REAL    NOT NULL DEFAULT 0.0
        )
    """

    def __init__(self, database_path: str | Path = ":memory:") -> None:
        """Initialises the store and creates the schema if absent.

        Args:
            database_path: SQLite file path.  Use ``":memory:"`` for
                in-process ephemeral storage (useful in tests).
        """
        self._conn: sqlite3.Connection = sqlite3.connect(
            str(database_path), check_same_thread=False
        )
        self._lock = threading.Lock()
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(self._CREATE_TABLE)
        self._conn.commit()

    # ------------------------------------------------------------------
    # CircuitBreakerStore protocol
    # ------------------------------------------------------------------

    def get_state(self, effect_class: str) -> tuple[int, float]:
        """Returns ``(failure_count, last_failure_epoch_s)`` for *effect_class*.

        Args:
            effect_class: The action effect class to query.

        Returns:
            ``(failure_count, last_failure_epoch_s)``.  Returns ``(0, 0.0)``
            when the effect class has no recorded failures.
        """
        _sql = (
            "SELECT failure_count, last_failure_ts FROM circuit_breaker_state"
            " WHERE effect_class = ?"
        )
        with self._lock:
            row = self._conn.execute(_sql, (effect_class,)).fetchone()
        if row is None:
            return (0, 0.0)
        return (int(row[0]), float(row[1]))

    def record_failure(self, effect_class: str) -> int:
        """Increments failure count and records the current wall-clock time.

        Uses upsert so the first failure for a new effect class is handled
        identically to subsequent ones.

        Args:
            effect_class: The action effect class that just failed.

        Returns:
            The new failure count after incrementing.
        """
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO circuit_breaker_state (effect_class, failure_count, last_failure_ts)
                VALUES (?, 1, ?)
                ON CONFLICT(effect_class) DO UPDATE SET
                    failure_count   = failure_count + 1,
                    last_failure_ts = excluded.last_failure_ts
                """,
                (effect_class, now),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT failure_count FROM circuit_breaker_state WHERE effect_class = ?",
                (effect_class,),
            ).fetchone()
            if row is None:
                raise RuntimeError(
                    f"circuit_breaker_store: row for effect_class={effect_class!r} "
                    "disappeared after UPSERT"
                )
            return int(row[0])

    def reset(self, effect_class: str) -> None:
        """Deletes the failure row for *effect_class*, returning it to CLOSED state.

        Args:
            effect_class: The action effect class that just succeeded.
        """
        with self._lock:
            self._conn.execute(
                "DELETE FROM circuit_breaker_state WHERE effect_class = ?",
                (effect_class,),
            )
            self._conn.commit()
