"""PostgreSQL-backed RecoveryOutcomeStore implementation."""

from __future__ import annotations

import json
from typing import Any

from agent_kernel.kernel.contracts import RecoveryOutcome, RecoveryOutcomeStore
from agent_kernel.kernel.persistence.pg_shared import AsyncPGBridge


class PostgresRecoveryOutcomeStore(RecoveryOutcomeStore):
    """Persist recovery outcomes in PostgreSQL."""

    def __init__(
        self,
        dsn: str,
        *,
        pool_min: int = 2,
        pool_max: int = 10,
        bridge: AsyncPGBridge | None = None,
    ) -> None:
        """Initialize PostgreSQL recovery-outcome store."""
        self._bridge = bridge or AsyncPGBridge(dsn=dsn, pool_min=pool_min, pool_max=pool_max)
        self._own_bridge = bridge is None
        self._bridge.run_sync(self._ensure_schema())

    def close(self) -> None:
        """Close bridge when this store owns it."""
        if self._own_bridge:
            self._bridge.close()

    async def write_outcome(self, outcome: RecoveryOutcome) -> None:
        """Persist one recovery outcome row."""
        await self._bridge.run_async(self._write_outcome(outcome))

    async def latest_for_run(self, run_id: str) -> RecoveryOutcome | None:
        """Return latest recovery outcome for one run, if present."""
        row = await self._bridge.run_async(self._latest_for_run(run_id))
        if row is None:
            return None
        payload = row["emitted_event_ids_json"]
        emitted_ids = []
        if isinstance(payload, str):
            try:
                parsed = json.loads(payload)
                if isinstance(parsed, list):
                    emitted_ids = [value for value in parsed if isinstance(value, str)]
            except json.JSONDecodeError:
                emitted_ids = []
        return RecoveryOutcome(
            run_id=str(row["run_id"]),
            action_id=row["action_id"],
            recovery_mode=str(row["recovery_mode"]),
            outcome_state=str(row["outcome_state"]),
            written_at=str(row["written_at"]),
            operator_escalation_ref=row["operator_escalation_ref"],
            emitted_event_ids=emitted_ids,
        )

    async def _ensure_schema(self) -> None:
        """Ensures required database schema objects exist."""
        pool = self._bridge.pool
        async with pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pg_recovery_outcome (
                    id BIGSERIAL PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    action_id TEXT NULL,
                    recovery_mode TEXT NOT NULL,
                    outcome_state TEXT NOT NULL,
                    written_at TEXT NOT NULL,
                    operator_escalation_ref TEXT NULL,
                    emitted_event_ids_json TEXT NOT NULL
                );
                """
            )
            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_pg_recovery_outcome_run_written
                ON pg_recovery_outcome (run_id, written_at DESC, id DESC);
                """
            )

    async def _write_outcome(self, outcome: RecoveryOutcome) -> None:
        """Writes one recovery outcome record."""
        pool = self._bridge.pool
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO pg_recovery_outcome (
                    run_id,
                    action_id,
                    recovery_mode,
                    outcome_state,
                    written_at,
                    operator_escalation_ref,
                    emitted_event_ids_json
                ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                outcome.run_id,
                outcome.action_id,
                outcome.recovery_mode,
                outcome.outcome_state,
                outcome.written_at,
                outcome.operator_escalation_ref,
                json.dumps(outcome.emitted_event_ids, ensure_ascii=True),
            )

    async def _latest_for_run(self, run_id: str) -> Any | None:
        """Latest for run."""
        pool = self._bridge.pool
        async with pool.acquire() as conn:
            return await conn.fetchrow(
                """
                SELECT
                    run_id,
                    action_id,
                    recovery_mode,
                    outcome_state,
                    written_at,
                    operator_escalation_ref,
                    emitted_event_ids_json
                FROM pg_recovery_outcome
                WHERE run_id = $1
                ORDER BY written_at DESC, id DESC
                LIMIT 1
                """,
                run_id,
            )
