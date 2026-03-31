"""SQLite-backed TurnIntentLog for durable turn intent persistence."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from agent_kernel.kernel.contracts import TurnIntentLog, TurnIntentRecord


class SQLiteTurnIntentLog(TurnIntentLog):
    """Persists turn intent metadata for replay-safe recovery."""

    def __init__(self, database_path: str | Path = ":memory:") -> None:
        self._database_path = str(database_path)
        self._conn = sqlite3.connect(self._database_path)
        self._conn.row_factory = sqlite3.Row
        self._ensure_schema()

    def close(self) -> None:
        """Closes SQLite connection."""
        self._conn.close()

    async def write_intent(self, intent: TurnIntentRecord) -> None:
        """Writes one turn intent with idempotent semantics by intent ref."""
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
              written_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(intent_commit_ref) DO UPDATE SET
              run_id = excluded.run_id,
              decision_ref = excluded.decision_ref,
              decision_fingerprint = excluded.decision_fingerprint,
              dispatch_dedupe_key = excluded.dispatch_dedupe_key,
              host_kind = excluded.host_kind,
              outcome_kind = excluded.outcome_kind,
              written_at = excluded.written_at
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
            ),
        )
        self._conn.commit()

    async def latest_for_run(self, run_id: str) -> TurnIntentRecord | None:
        """Returns latest persisted turn intent record for one run."""
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
              written_at
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
        )

    def _ensure_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS turn_intent_log (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              run_id TEXT NOT NULL,
              intent_commit_ref TEXT NOT NULL UNIQUE,
              decision_ref TEXT NOT NULL,
              decision_fingerprint TEXT NOT NULL,
              dispatch_dedupe_key TEXT,
              host_kind TEXT,
              outcome_kind TEXT NOT NULL,
              written_at TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_turn_intent_log_run_written
            ON turn_intent_log(run_id, written_at DESC, id DESC)
            """
        )
        self._conn.commit()

