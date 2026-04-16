"""SQLite-backed TurnIntentLog for durable turn intent persistence."""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

from agent_kernel.kernel.contracts import TurnIntentLog, TurnIntentRecord


class SQLiteTurnIntentLog(TurnIntentLog):
    """Persists turn intent metadata for replay-safe recovery."""

    def __init__(self, database_path: str | Path = ":memory:") -> None:
        """Initialize the instance with configured dependencies."""
        self._database_path = str(database_path)
        # check_same_thread=False + RLock: same pattern as SQLiteDedupeStore.
        self._conn = sqlite3.connect(self._database_path, check_same_thread=False)
        self._lock = threading.RLock()
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA wal_autocheckpoint=1000")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._ensure_schema()

    def close(self) -> None:
        """Close SQLite connection."""
        with self._lock:
            self._conn.close()

    async def write_intent(self, intent: TurnIntentRecord) -> None:
        """Write one turn intent with idempotent semantics by intent ref.

        Args:
            intent: Turn intent record to persist.

        Raises:
            sqlite3.Error: On database write failure.

        """
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO turn_intent_log (
                      run_id,
                      intent_commit_ref,
                      decision_ref,
                      decision_fingerprint,
                      dispatch_dedupe_key,
                      host_kind,
                      outcome_kind,
                      written_at,
                      reflection_round
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(intent_commit_ref) DO UPDATE SET
                      run_id = excluded.run_id,
                      decision_ref = excluded.decision_ref,
                      decision_fingerprint = excluded.decision_fingerprint,
                      dispatch_dedupe_key = excluded.dispatch_dedupe_key,
                      host_kind = excluded.host_kind,
                      outcome_kind = excluded.outcome_kind,
                      written_at = excluded.written_at,
                      reflection_round = excluded.reflection_round
                    """,
                    (
                        intent.run_id,
                        intent.intent_commit_ref,
                        intent.decision_ref,
                        intent.decision_fingerprint,
                        intent.dispatch_dedupe_key,
                        intent.host_kind,
                        intent.outcome_kind,
                        intent.written_at,
                        intent.reflection_round,
                    ),
                )
                self._conn.commit()
            except Exception:
                with contextlib.suppress(Exception):
                    self._conn.rollback()
                raise

    async def latest_for_run(self, run_id: str) -> TurnIntentRecord | None:
        """Return latest persisted turn intent record for one run.

        Args:
            run_id: Identifier of the target run.

        Returns:
            TurnIntentRecord | None: Latest turn intent record, or ``None`` if absent.

        """
        with self._lock:
            row = self._conn.execute(
                """
            SELECT
              run_id,
              intent_commit_ref,
              decision_ref,
              decision_fingerprint,
              dispatch_dedupe_key,
              host_kind,
              outcome_kind,
              written_at,
              reflection_round
            FROM turn_intent_log
            WHERE run_id = ?
            ORDER BY written_at DESC, id DESC
            LIMIT 1
            """,
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return TurnIntentRecord(
            run_id=row["run_id"],
            intent_commit_ref=row["intent_commit_ref"],
            decision_ref=row["decision_ref"],
            decision_fingerprint=row["decision_fingerprint"],
            dispatch_dedupe_key=row["dispatch_dedupe_key"],
            host_kind=row["host_kind"],
            outcome_kind=row["outcome_kind"],
            written_at=row["written_at"],
            reflection_round=row["reflection_round"] if row["reflection_round"] is not None else 0,
        )

    def _ensure_schema(self) -> None:
        """Ensures required database schema objects exist."""
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS turn_intent_log (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              run_id TEXT NOT NULL,
              intent_commit_ref TEXT NOT NULL UNIQUE,
              decision_ref TEXT NOT NULL,
              decision_fingerprint TEXT NOT NULL,
              dispatch_dedupe_key TEXT,
              host_kind TEXT,
              outcome_kind TEXT NOT NULL,
              written_at TEXT NOT NULL,
              reflection_round INTEGER DEFAULT 0
            )
            """)
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_turn_intent_log_run_written
            ON turn_intent_log(run_id, written_at DESC, id DESC)
            """)
        self._conn.commit()
