"""PostgreSQL-backed DedupeStore implementation."""

from __future__ import annotations

from typing import Any

from agent_kernel.kernel.dedupe_store import (
    DedupeRecord,
    DedupeReservation,
    DedupeStorePort,
    DedupeStoreStateError,
    IdempotencyEnvelope,
)
from agent_kernel.kernel.persistence.pg_shared import AsyncPGBridge


class PostgresDedupeStore(DedupeStorePort):
    """Persist dispatch idempotency records in PostgreSQL."""

    def __init__(
        self,
        dsn: str,
        *,
        pool_min: int = 2,
        pool_max: int = 10,
        bridge: AsyncPGBridge | None = None,
    ) -> None:
        """Initialize PostgreSQL dedupe store."""
        self._bridge = bridge or AsyncPGBridge(dsn=dsn, pool_min=pool_min, pool_max=pool_max)
        self._own_bridge = bridge is None
        self._bridge.run_sync(self._ensure_schema())

    def close(self) -> None:
        """Close bridge resources when owned by this store."""
        if self._own_bridge:
            self._bridge.close()

    def reserve(self, envelope: IdempotencyEnvelope) -> DedupeReservation:
        """Reserve dedupe key if absent."""
        return self._bridge.run_sync(self._reserve_impl(envelope))

    def reserve_and_dispatch(
        self,
        envelope: IdempotencyEnvelope,
        peer_operation_id: str | None = None,
    ) -> DedupeReservation:
        """Atomically reserve and mark as dispatched."""
        return self._bridge.run_sync(
            self._reserve_and_dispatch_impl(envelope, peer_operation_id=peer_operation_id)
        )

    def mark_dispatched(
        self,
        dispatch_idempotency_key: str,
        peer_operation_id: str | None = None,
    ) -> None:
        """Transition record to dispatched."""
        self._bridge.run_sync(
            self._transition_impl(
                dispatch_idempotency_key,
                allowed=frozenset({"reserved", "dispatched"}),
                target_state="dispatched",
                peer_operation_id=peer_operation_id,
                external_ack_ref=None,
            )
        )

    def mark_acknowledged(
        self,
        dispatch_idempotency_key: str,
        external_ack_ref: str | None = None,
    ) -> None:
        """Transition record to acknowledged."""
        self._bridge.run_sync(
            self._transition_impl(
                dispatch_idempotency_key,
                allowed=frozenset({"dispatched", "acknowledged"}),
                target_state="acknowledged",
                peer_operation_id=None,
                external_ack_ref=external_ack_ref,
            )
        )

    def mark_succeeded(
        self,
        dispatch_idempotency_key: str,
        external_ack_ref: str | None = None,
    ) -> None:
        """Transition record to succeeded."""
        self._bridge.run_sync(
            self._transition_impl(
                dispatch_idempotency_key,
                allowed=frozenset({"acknowledged", "succeeded"}),
                target_state="succeeded",
                peer_operation_id=None,
                external_ack_ref=external_ack_ref,
            )
        )

    def mark_unknown_effect(self, dispatch_idempotency_key: str) -> None:
        """Transition record to unknown_effect."""
        self._bridge.run_sync(
            self._transition_impl(
                dispatch_idempotency_key,
                allowed=frozenset({"dispatched", "unknown_effect"}),
                target_state="unknown_effect",
                peer_operation_id=None,
                external_ack_ref=None,
            )
        )

    def get(self, dispatch_idempotency_key: str) -> DedupeRecord | None:
        """Return dedupe record by key."""
        row = self._bridge.run_sync(self._get_row(dispatch_idempotency_key))
        if row is None:
            return None
        return self._row_to_record(row)

    async def _ensure_schema(self) -> None:
        pool = self._bridge.pool
        async with pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pg_dedupe_store (
                    dispatch_idempotency_key TEXT PRIMARY KEY,
                    operation_fingerprint TEXT NOT NULL,
                    attempt_seq BIGINT NOT NULL,
                    state TEXT NOT NULL,
                    peer_operation_id TEXT NULL,
                    external_ack_ref TEXT NULL
                );
                """
            )

    async def _reserve_impl(self, envelope: IdempotencyEnvelope) -> DedupeReservation:
        pool = self._bridge.pool
        async with pool.acquire() as conn, conn.transaction():
            inserted = await conn.fetchrow(
                """
                    INSERT INTO pg_dedupe_store (
                        dispatch_idempotency_key,
                        operation_fingerprint,
                        attempt_seq,
                        state,
                        peer_operation_id,
                        external_ack_ref
                    ) VALUES ($1, $2, $3, 'reserved', $4, NULL)
                    ON CONFLICT (dispatch_idempotency_key) DO NOTHING
                    RETURNING dispatch_idempotency_key
                    """,
                envelope.dispatch_idempotency_key,
                envelope.operation_fingerprint,
                envelope.attempt_seq,
                envelope.peer_operation_id,
            )
            if inserted is not None:
                return DedupeReservation(accepted=True, reason="accepted")
            existing = await self._get_row(envelope.dispatch_idempotency_key, conn=conn)
            return DedupeReservation(
                accepted=False,
                reason="duplicate",
                existing_record=self._row_to_record(existing),
            )

    async def _reserve_and_dispatch_impl(
        self,
        envelope: IdempotencyEnvelope,
        peer_operation_id: str | None,
    ) -> DedupeReservation:
        pool = self._bridge.pool
        async with pool.acquire() as conn, conn.transaction():
            inserted = await conn.fetchrow(
                """
                    INSERT INTO pg_dedupe_store (
                        dispatch_idempotency_key,
                        operation_fingerprint,
                        attempt_seq,
                        state,
                        peer_operation_id,
                        external_ack_ref
                    ) VALUES ($1, $2, $3, 'dispatched', $4, NULL)
                    ON CONFLICT (dispatch_idempotency_key) DO NOTHING
                    RETURNING dispatch_idempotency_key
                    """,
                envelope.dispatch_idempotency_key,
                envelope.operation_fingerprint,
                envelope.attempt_seq,
                peer_operation_id or envelope.peer_operation_id,
            )
            if inserted is not None:
                return DedupeReservation(accepted=True, reason="accepted")
            existing = await self._get_row(envelope.dispatch_idempotency_key, conn=conn)
            return DedupeReservation(
                accepted=False,
                reason="duplicate",
                existing_record=self._row_to_record(existing),
            )

    async def _transition_impl(
        self,
        key: str,
        *,
        allowed: frozenset[str],
        target_state: str,
        peer_operation_id: str | None,
        external_ack_ref: str | None,
    ) -> None:
        pool = self._bridge.pool
        async with pool.acquire() as conn, conn.transaction():
            row = await self._get_row(key, conn=conn, for_update=True)
            if row is None:
                raise DedupeStoreStateError(f"Unknown dispatch_idempotency_key: {key}.")
            current = str(row["state"])
            if current not in allowed:
                raise DedupeStoreStateError(f"Cannot transition {current} -> {target_state}.")
            next_peer = peer_operation_id or row["peer_operation_id"]
            next_ack = external_ack_ref or row["external_ack_ref"]
            await conn.execute(
                """
                    UPDATE pg_dedupe_store
                    SET state = $1,
                        peer_operation_id = $2,
                        external_ack_ref = $3
                    WHERE dispatch_idempotency_key = $4
                    """,
                target_state,
                next_peer,
                next_ack,
                key,
            )

    async def _get_row(
        self,
        key: str,
        *,
        conn: Any | None = None,
        for_update: bool = False,
    ) -> Any | None:
        query = """
            SELECT
                dispatch_idempotency_key,
                operation_fingerprint,
                attempt_seq,
                state,
                peer_operation_id,
                external_ack_ref
            FROM pg_dedupe_store
            WHERE dispatch_idempotency_key = $1
            """ + (" FOR UPDATE" if for_update else "")
        if conn is not None:
            return await conn.fetchrow(query, key)
        pool = self._bridge.pool
        async with pool.acquire() as local_conn:
            return await local_conn.fetchrow(query, key)

    @staticmethod
    def _row_to_record(row: Any) -> DedupeRecord:
        return DedupeRecord(
            dispatch_idempotency_key=str(row["dispatch_idempotency_key"]),
            operation_fingerprint=str(row["operation_fingerprint"]),
            attempt_seq=int(row["attempt_seq"]),
            state=str(row["state"]),
            peer_operation_id=row["peer_operation_id"],
            external_ack_ref=row["external_ack_ref"],
        )
