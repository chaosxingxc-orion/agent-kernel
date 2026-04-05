"""SQLite-backed RecoveryOutcomeStore for durable recovery closure records."""

from __future__ import annotations

import contextlib
import json
import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

from agent_kernel.kernel.contracts import RecoveryOutcome, RecoveryOutcomeStore


class SQLiteRecoveryOutcomeStore(RecoveryOutcomeStore):
    """Persists recovery outcomes in SQLite with per-run latest lookup."""

    def __init__(self, database_path: str | Path = ":memory:") -> None:
        """Initialize the instance with configured dependencies."""
        self._database_path = str(database_path)
        self._conn = sqlite3.connect(self._database_path)
        self._conn.row_factory = sqlite3.Row
        self._ensure_schema()

    def close(self) -> None:
        """Close SQLite connection."""
        self._conn.close()

    async def write_outcome(self, outcome: RecoveryOutcome) -> None:
        """Persist one recovery outcome row.

        Args:
            outcome: Recovery outcome record to persist.

        Raises:
            sqlite3.Error: On database write failure.

        """
        try:
            self._conn.execute(
                """
                INSERT INTO recovery_outcome (
                  run_id,
                  action_id,
                  recovery_mode,
                  outcome_state,
                  written_at,
                  operator_escalation_ref,
                  emitted_event_ids_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    outcome.run_id,
                    outcome.action_id,
                    outcome.recovery_mode,
                    outcome.outcome_state,
                    outcome.written_at,
                    outcome.operator_escalation_ref,
                    json.dumps(outcome.emitted_event_ids, ensure_ascii=True),
                ),
            )
            self._conn.commit()
        except Exception:
            with contextlib.suppress(Exception):
                self._conn.rollback()
            raise

    async def latest_for_run(self, run_id: str) -> RecoveryOutcome | None:
        """Return latest recovery outcome for one run, if present.

        Args:
            run_id: Identifier of the target run.

        Returns:
            RecoveryOutcome | None: Latest recovery outcome, or ``None`` if absent.

        """
        row = self._conn.execute(
            """
            SELECT
              run_id,
              action_id,
              recovery_mode,
              outcome_state,
              written_at,
              operator_escalation_ref,
              emitted_event_ids_json
            FROM recovery_outcome
            WHERE run_id = ?
            ORDER BY written_at DESC, id DESC
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return RecoveryOutcome(
            run_id=row["run_id"],
            action_id=row["action_id"],
            recovery_mode=row["recovery_mode"],
            outcome_state=row["outcome_state"],
            written_at=row["written_at"],
            operator_escalation_ref=row["operator_escalation_ref"],
            emitted_event_ids=_deserialize_ids(row["emitted_event_ids_json"]),
        )

    def _ensure_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS recovery_outcome (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              run_id TEXT NOT NULL,
              action_id TEXT,
              recovery_mode TEXT NOT NULL,
              outcome_state TEXT NOT NULL,
              written_at TEXT NOT NULL,
              operator_escalation_ref TEXT,
              emitted_event_ids_json TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_recovery_outcome_run_written
            ON recovery_outcome(run_id, written_at DESC, id DESC)
            """
        )
        self._conn.commit()


def _deserialize_ids(payload: str) -> list[str]:
    """Deserializes event id list payload with conservative fallback."""
    try:
        value = json.loads(payload)
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, str)]
