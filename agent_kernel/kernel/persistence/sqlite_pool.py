"""Lightweight SQLite connection pool with read/write separation."""

from __future__ import annotations

import contextlib
import queue
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager


class SQLiteConnectionPool:
    """Provide pooled read connections and one dedicated write connection."""

    def __init__(
        self,
        database_path: str,
        read_pool_size: int = 4,
        busy_timeout_ms: int = 5000,
    ) -> None:
        """Initialize connection pool."""
        self._database_path = database_path
        self._read_pool_size = max(read_pool_size, 1)
        self._busy_timeout_ms = max(0, busy_timeout_ms)
        self._read_pool: queue.SimpleQueue[sqlite3.Connection] = queue.SimpleQueue()
        self._read_lock = threading.Lock()
        self._write_conn = self._open_connection(query_only=False)
        self._closed = False

        for _ in range(self._read_pool_size):
            self._read_pool.put(self._open_connection(query_only=True))

    @property
    def database_path(self) -> str:
        """Return configured SQLite database path."""
        return self._database_path

    def acquire_read(self) -> sqlite3.Connection:
        """Acquire one read connection from pool."""
        conn = self._read_pool.get()
        conn.execute("SELECT 1")
        return conn

    def release_read(self, conn: sqlite3.Connection) -> None:
        """Return one read connection to pool."""
        if self._closed:
            with contextlib.suppress(Exception):
                conn.close()
            return
        self._read_pool.put(conn)

    def acquire_write(self) -> sqlite3.Connection:
        """Return dedicated write connection."""
        self._write_conn.execute("SELECT 1")
        return self._write_conn

    def release_write(self, conn: sqlite3.Connection) -> None:
        """Release write connection (no-op for dedicated writer)."""
        del conn

    @contextmanager
    def read_connection(self) -> Iterator[sqlite3.Connection]:
        """Context manager for one pooled read connection."""
        conn = self.acquire_read()
        try:
            yield conn
        finally:
            self.release_read(conn)

    @contextmanager
    def write_connection(self) -> Iterator[sqlite3.Connection]:
        """Context manager for dedicated write connection."""
        conn = self.acquire_write()
        try:
            yield conn
        finally:
            self.release_write(conn)

    def close_all(self) -> None:
        """Close all pooled connections."""
        with self._read_lock:
            if self._closed:
                return
            self._closed = True
        with contextlib.suppress(Exception):
            self._write_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        with contextlib.suppress(Exception):
            self._write_conn.close()

        while True:
            try:
                conn = self._read_pool.get_nowait()
            except Exception:
                break
            with contextlib.suppress(Exception):
                conn.close()

    def _open_connection(self, *, query_only: bool) -> sqlite3.Connection:
        """Opens a SQLite connection with configured pragmas."""
        conn = sqlite3.connect(self._database_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=1000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        if query_only:
            conn.execute("PRAGMA query_only=ON")
        return conn
