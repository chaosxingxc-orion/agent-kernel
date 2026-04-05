"""PostgreSQL-backed circuit breaker store."""

from __future__ import annotations

import time
from typing import Any

from agent_kernel.kernel.contracts import CircuitBreakerStore
from agent_kernel.kernel.persistence.pg_shared import AsyncPGBridge


class PostgresCircuitBreakerStore(CircuitBreakerStore):
    """Persist circuit-breaker counters by effect class in PostgreSQL."""

    def __init__(
        self,
        dsn: str,
        *,
        pool_min: int = 2,
        pool_max: int = 10,
        bridge: AsyncPGBridge | None = None,
    ) -> None:
        """Initialize PostgreSQL circuit breaker store."""
        self._bridge = bridge or AsyncPGBridge(dsn=dsn, pool_min=pool_min, pool_max=pool_max)
        self._own_bridge = bridge is None
        self._bridge.run_sync(self._ensure_schema())

    def close(self) -> None:
        """Close bridge when this store owns it."""
        if self._own_bridge:
            self._bridge.close()

    def get_state(self, effect_class: str) -> tuple[int, float]:
        """Return ``(failure_count, last_failure_epoch_s)`` for effect class."""
        row = self._bridge.run_sync(self._get_row(effect_class))
        if row is None:
            return (0, 0.0)
        return (int(row["failure_count"]), float(row["last_failure_ts"]))

    def record_failure(self, effect_class: str) -> int:
        """Increment failure counter and update timestamp."""
        return self._bridge.run_sync(self._record_failure(effect_class))

    def reset(self, effect_class: str) -> None:
        """Reset failure counter for one effect class."""
        self._bridge.run_sync(self._reset(effect_class))

    # ------------------------------------------------------------------
    # Compatibility aliases for persistence ports
    # ------------------------------------------------------------------

    def get_failure_count(self, effect_class: str) -> int:
        """Return failure count for one effect class."""
        return self.get_state(effect_class)[0]

    def increment_failure(self, effect_class: str) -> int:
        """Increment and return failure count."""
        return self.record_failure(effect_class)

    def get_last_failure_ts(self, effect_class: str) -> float | None:
        """Return last failure timestamp in seconds, or ``None``."""
        _count, ts = self.get_state(effect_class)
        if ts <= 0.0:
            return None
        return ts

    def list_effect_classes(self) -> list[str]:
        """Return effect classes with persisted breaker state."""
        return self._bridge.run_sync(self._list_effect_classes())

    async def _ensure_schema(self) -> None:
        pool = self._bridge.pool
        async with pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pg_circuit_breaker_state (
                    effect_class TEXT PRIMARY KEY,
                    failure_count BIGINT NOT NULL DEFAULT 0,
                    last_failure_ts DOUBLE PRECISION NOT NULL DEFAULT 0.0
                );
                """
            )

    async def _get_row(self, effect_class: str) -> Any | None:
        pool = self._bridge.pool
        async with pool.acquire() as conn:
            return await conn.fetchrow(
                """
                SELECT failure_count, last_failure_ts
                FROM pg_circuit_breaker_state
                WHERE effect_class = $1
                """,
                effect_class,
            )

    async def _record_failure(self, effect_class: str) -> int:
        now = time.time()
        pool = self._bridge.pool
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO pg_circuit_breaker_state (
                    effect_class,
                    failure_count,
                    last_failure_ts
                ) VALUES ($1, 1, $2)
                ON CONFLICT (effect_class) DO UPDATE SET
                    failure_count = pg_circuit_breaker_state.failure_count + 1,
                    last_failure_ts = EXCLUDED.last_failure_ts
                RETURNING failure_count
                """,
                effect_class,
                now,
            )
        assert row is not None
        return int(row["failure_count"])

    async def _reset(self, effect_class: str) -> None:
        pool = self._bridge.pool
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM pg_circuit_breaker_state WHERE effect_class = $1",
                effect_class,
            )

    async def _list_effect_classes(self) -> list[str]:
        pool = self._bridge.pool
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT effect_class
                FROM pg_circuit_breaker_state
                ORDER BY effect_class ASC
                """
            )
        return [str(row["effect_class"]) for row in rows]
