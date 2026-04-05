"""Colocated PostgreSQL persistence bundle with shared asyncpg pool."""

from __future__ import annotations

from agent_kernel.kernel.persistence.pg_circuit_breaker_store import (
    PostgresCircuitBreakerStore,
)
from agent_kernel.kernel.persistence.pg_dedupe_store import PostgresDedupeStore
from agent_kernel.kernel.persistence.pg_event_log import PostgresKernelRuntimeEventLog
from agent_kernel.kernel.persistence.pg_recovery_outcome_store import (
    PostgresRecoveryOutcomeStore,
)
from agent_kernel.kernel.persistence.pg_shared import AsyncPGBridge


class PostgresColocatedBundle:
    """Provide PostgreSQL stores over one shared connection pool."""

    def __init__(
        self,
        dsn: str,
        *,
        pool_min: int = 2,
        pool_max: int = 10,
    ) -> None:
        """Initialize shared bridge and concrete store instances."""
        self._bridge = AsyncPGBridge(dsn=dsn, pool_min=pool_min, pool_max=pool_max)
        self.event_log = PostgresKernelRuntimeEventLog(
            dsn,
            pool_min=pool_min,
            pool_max=pool_max,
            bridge=self._bridge,
        )
        self.dedupe_store = PostgresDedupeStore(
            dsn,
            pool_min=pool_min,
            pool_max=pool_max,
            bridge=self._bridge,
        )
        self.circuit_breaker_store = PostgresCircuitBreakerStore(
            dsn,
            pool_min=pool_min,
            pool_max=pool_max,
            bridge=self._bridge,
        )
        self.recovery_outcome_store = PostgresRecoveryOutcomeStore(
            dsn,
            pool_min=pool_min,
            pool_max=pool_max,
            bridge=self._bridge,
        )

    def close(self) -> None:
        """Close shared bridge resources."""
        self._bridge.close()
