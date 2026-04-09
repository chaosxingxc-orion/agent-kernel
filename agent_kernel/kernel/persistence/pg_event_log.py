"""PostgreSQL-backed kernel runtime event log implementation."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent_kernel.kernel.contracts import ActionCommit, RuntimeEvent

from agent_kernel.kernel.contracts import KernelRuntimeEventLog, RuntimeEvent
from agent_kernel.kernel.persistence.pg_shared import AsyncPGBridge


class PostgresKernelRuntimeEventLog(KernelRuntimeEventLog):
    """Persist runtime events in PostgreSQL with per-run monotonic offsets."""

    def __init__(
        self,
        dsn: str,
        *,
        pool_min: int = 2,
        pool_max: int = 10,
        bridge: AsyncPGBridge | None = None,
    ) -> None:
        """Initialize PostgreSQL event log.

        Args:
            dsn: PostgreSQL DSN.
            pool_min: Connection-pool minimum size.
            pool_max: Connection-pool maximum size.
            bridge: Optional shared bridge for colocated bundle usage.

        """
        self._bridge = bridge or AsyncPGBridge(dsn=dsn, pool_min=pool_min, pool_max=pool_max)
        self._own_bridge = bridge is None
        self._bridge.run_sync(self._ensure_schema())

    def close(self) -> None:
        """Close bridge resources when owned by this instance."""
        if self._own_bridge:
            self._bridge.close()

    async def append_action_commit(self, commit: ActionCommit) -> str:
        """Append one action commit atomically."""
        if not commit.events:
            raise ValueError("ActionCommit.events must contain at least one event.")
        return await self._bridge.run_async(self._append_action_commit_impl(commit))

    async def load(self, run_id: str, after_offset: int = 0) -> list[RuntimeEvent]:
        """Load events for one run ordered by commit offset."""
        rows = await self._bridge.run_async(self._load_rows(run_id, after_offset))
        return [self._row_to_runtime_event(row) for row in rows]

    async def max_offset(self, run_id: str) -> int:
        """Return highest committed offset for one run."""
        return await self._bridge.run_async(self._max_offset_impl(run_id))

    def read_events(self, run_id: str) -> list[RuntimeEvent]:
        """Read full run stream synchronously for persistence-port compatibility."""
        rows = self._bridge.run_sync(self._load_rows(run_id, 0))
        return [self._row_to_runtime_event(row) for row in rows]

    async def _ensure_schema(self) -> None:
        pool = self._bridge.pool
        async with pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pg_action_commits (
                    commit_sequence BIGSERIAL PRIMARY KEY,
                    stream_run_id TEXT NOT NULL,
                    commit_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    event_count INTEGER NOT NULL CHECK (event_count > 0)
                );
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pg_runtime_events (
                    id BIGSERIAL PRIMARY KEY,
                    commit_sequence BIGINT NOT NULL
                        REFERENCES pg_action_commits(commit_sequence)
                        ON DELETE CASCADE,
                    event_index INTEGER NOT NULL,
                    stream_run_id TEXT NOT NULL,
                    event_run_id TEXT NOT NULL,
                    commit_offset BIGINT NOT NULL,
                    event_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    event_class TEXT NOT NULL,
                    event_authority TEXT NOT NULL,
                    ordering_key TEXT NOT NULL,
                    wake_policy TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    idempotency_key TEXT NULL,
                    payload_ref TEXT NULL,
                    payload_json JSONB NULL,
                    schema_version TEXT NOT NULL DEFAULT '1',
                    UNIQUE (stream_run_id, commit_offset),
                    UNIQUE (commit_sequence, event_index)
                );
                """
            )
            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_pg_runtime_events_stream_offset
                    ON pg_runtime_events(stream_run_id, commit_offset);
                """
            )

    async def _append_action_commit_impl(self, commit: ActionCommit) -> str:
        pool = self._bridge.pool
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                commit.run_id,
            )
            next_offset = await conn.fetchval(
                """
                    SELECT COALESCE(MAX(commit_offset), 0) + 1
                    FROM pg_runtime_events
                    WHERE stream_run_id = $1
                    """,
                commit.run_id,
            )
            commit_sequence = await conn.fetchval(
                """
                    INSERT INTO pg_action_commits (
                        stream_run_id,
                        commit_id,
                        created_at,
                        event_count
                    ) VALUES ($1, $2, $3, $4)
                    RETURNING commit_sequence
                    """,
                commit.run_id,
                commit.commit_id,
                commit.created_at,
                len(commit.events),
            )
            assert isinstance(commit_sequence, int)
            for index, event in enumerate(commit.events):
                payload = None
                if event.payload_json is not None:
                    payload = json.loads(
                        json.dumps(event.payload_json, sort_keys=True, separators=(",", ":"))
                    )
                await conn.execute(
                    """
                        INSERT INTO pg_runtime_events (
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
                            payload_json,
                            schema_version
                        ) VALUES (
                            $1, $2, $3, $4, $5, $6, $7, $8,
                            $9, $10, $11, $12, $13, $14, $15, $16
                        )
                        """,
                    commit_sequence,
                    index,
                    commit.run_id,
                    event.run_id,
                    int(next_offset) + index,
                    event.event_id,
                    event.event_type,
                    event.event_class,
                    event.event_authority,
                    event.ordering_key,
                    event.wake_policy,
                    event.created_at,
                    event.idempotency_key,
                    event.payload_ref,
                    payload,
                    event.schema_version,
                )
        return f"commit-ref-{commit_sequence}"

    async def _load_rows(self, run_id: str, after_offset: int) -> list[Any]:
        pool = self._bridge.pool
        async with pool.acquire() as conn:
            return await conn.fetch(
                """
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
                    payload_json,
                    schema_version
                FROM pg_runtime_events
                WHERE stream_run_id = $1 AND commit_offset > $2
                ORDER BY commit_offset ASC
                """,
                run_id,
                after_offset,
            )

    async def _max_offset_impl(self, run_id: str) -> int:
        pool = self._bridge.pool
        async with pool.acquire() as conn:
            value = await conn.fetchval(
                """
                SELECT COALESCE(MAX(commit_offset), 0)
                FROM pg_runtime_events
                WHERE stream_run_id = $1
                """,
                run_id,
            )
        return int(value or 0)

    @staticmethod
    def _row_to_runtime_event(row: Any) -> RuntimeEvent:
        payload = row["payload_json"]
        if payload is not None and not isinstance(payload, dict):
            payload = dict(payload)
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
            idempotency_key=row["idempotency_key"],
            payload_ref=row["payload_ref"],
            payload_json=payload,
            schema_version=str(row["schema_version"]),
        )
