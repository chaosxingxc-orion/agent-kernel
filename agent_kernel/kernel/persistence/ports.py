"""Persistence-layer protocol contracts for pluggable storage backends.

These protocols sit below kernel-level contracts and describe storage
operations only. They are intentionally narrow so SQLite/PostgreSQL (or
future backends) can be swapped without changing upper-layer semantics.
"""

from __future__ import annotations

from typing import Protocol

from agent_kernel.kernel.contracts import ActionCommit, RuntimeEvent
from agent_kernel.kernel.dedupe_store import (
    DedupeRecord,
    DedupeReservation,
    IdempotencyEnvelope,
)


class EventLogStore(Protocol):
    """Persistence port for runtime event-log storage."""

    async def append_action_commit(self, commit: ActionCommit) -> str:
        """Append one action commit atomically."""

    def read_events(self, run_id: str) -> list[RuntimeEvent]:
        """Read all run events ordered by ascending commit offset."""

    def close(self) -> None:
        """Release storage resources."""


class DedupeStore(Protocol):
    """Persistence port for dispatch idempotency state machine."""

    def reserve(self, envelope: IdempotencyEnvelope) -> DedupeReservation:
        """Reserve one idempotency slot."""

    def reserve_and_dispatch(self, envelope: IdempotencyEnvelope) -> DedupeReservation:
        """Atomically reserve and mark dispatched."""

    def mark_dispatched(self, key: str) -> None:
        """Transition to dispatched state."""

    def mark_acknowledged(self, key: str) -> None:
        """Transition to acknowledged state."""

    def mark_succeeded(self, key: str) -> None:
        """Transition to succeeded state."""

    def mark_unknown_effect(self, key: str) -> None:
        """Transition to unknown_effect state."""

    def get(self, key: str) -> DedupeRecord | None:
        """Return dedupe record by key."""

    def close(self) -> None:
        """Release storage resources."""


class CircuitBreakerStore(Protocol):
    """Persistence port for circuit-breaker counters."""

    def get_failure_count(self, effect_class: str) -> int:
        """Return failure count for an effect class."""

    def increment_failure(self, effect_class: str) -> int:
        """Increment and return failure count."""

    def reset(self, effect_class: str) -> None:
        """Reset breaker state for an effect class."""

    def get_last_failure_ts(self, effect_class: str) -> float | None:
        """Return last failure epoch timestamp in seconds, if present."""

    def close(self) -> None:
        """Release storage resources."""
