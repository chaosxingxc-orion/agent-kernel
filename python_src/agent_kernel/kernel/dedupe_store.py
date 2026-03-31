"""v6.4 DedupeStore implementation for idempotent dispatch protection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol


class DedupeStoreStateError(ValueError):
    """Raised when dedupe state transitions violate monotonic constraints."""


DedupeState = Literal["reserved", "dispatched", "acknowledged", "unknown_effect"]
HostKind = Literal["local_process", "local_cli", "remote_service"]


@dataclass(frozen=True, slots=True)
class IdempotencyEnvelope:
    """Represents one dispatch idempotency declaration for executor handoff.

    Attributes:
        dispatch_idempotency_key: Unique key for deduplication.
        operation_fingerprint: Deterministic fingerprint of the operation.
        attempt_seq: Monotonic attempt sequence number.
        effect_scope: Declared effect class scope.
        capability_snapshot_hash: Hash of the governing snapshot.
        host_kind: Target host kind for the dispatch.
        peer_operation_id: Optional peer-side operation identifier.
        policy_snapshot_ref: Optional policy snapshot reference.
        rule_bundle_hash: Optional rule bundle hash.
    """

    dispatch_idempotency_key: str
    operation_fingerprint: str
    attempt_seq: int
    effect_scope: str
    capability_snapshot_hash: str
    host_kind: HostKind
    peer_operation_id: str | None = None
    policy_snapshot_ref: str | None = None
    rule_bundle_hash: str | None = None


@dataclass(frozen=True, slots=True)
class DedupeRecord:
    """Represents one dedupe ledger record for a dispatch key.

    Attributes:
        dispatch_idempotency_key: Unique deduplication key.
        operation_fingerprint: Fingerprint of the dispatched operation.
        attempt_seq: Attempt sequence number at dispatch time.
        state: Current monotonic state of the record.
        peer_operation_id: Optional peer-side operation reference.
        external_ack_ref: Optional external acknowledgement reference.
    """

    dispatch_idempotency_key: str
    operation_fingerprint: str
    attempt_seq: int
    state: DedupeState
    peer_operation_id: str | None = None
    external_ack_ref: str | None = None


@dataclass(frozen=True, slots=True)
class DedupeReservation:
    """Represents reserve() response for one idempotency envelope.

    Attributes:
        accepted: Whether the reservation was accepted.
        reason: Discriminator for the reservation outcome.
        existing_record: Existing record when ``reason`` is ``"duplicate"``.
    """

    accepted: bool
    reason: Literal["duplicate", "reserved", "accepted"]
    existing_record: DedupeRecord | None = None


class DedupeStorePort(Protocol):
    """Protocol for dispatch idempotency state machine at executor boundary.

    Boundary note:
      - This store tracks dispatch-level lifecycle
        (reserved/dispatched/acknowledged/unknown_effect).
      - It is different from ``DecisionDeduper`` which de-duplicates
        decision fingerprints at workflow orchestration level.
    """

    def reserve(self, envelope: IdempotencyEnvelope) -> DedupeReservation:
        """Reserves envelope idempotency key before external dispatch.

        Args:
            envelope: Idempotency envelope to reserve.

        Returns:
            Reservation result indicating acceptance or duplicate.
        """

    def mark_dispatched(
        self,
        dispatch_idempotency_key: str,
        peer_operation_id: str | None = None,
    ) -> None:
        """Marks envelope as dispatched.

        Args:
            dispatch_idempotency_key: Key to mark as dispatched.
            peer_operation_id: Optional peer-side operation reference.
        """

    def mark_acknowledged(
        self,
        dispatch_idempotency_key: str,
        external_ack_ref: str | None = None,
    ) -> None:
        """Marks envelope as acknowledged.

        Args:
            dispatch_idempotency_key: Key to mark as acknowledged.
            external_ack_ref: Optional external acknowledgement reference.
        """

    def mark_unknown_effect(self, dispatch_idempotency_key: str) -> None:
        """Marks envelope as unknown effect.

        Args:
            dispatch_idempotency_key: Key to mark as unknown effect.
        """

    def get(self, dispatch_idempotency_key: str) -> DedupeRecord | None:
        """Gets record by idempotency key.

        Args:
            dispatch_idempotency_key: Key to look up.

        Returns:
            Matching dedupe record, or ``None`` if not found.
        """


class InMemoryDedupeStore:
    """In-memory dedupe store with monotonic state transitions.

    Transition graph:
      reserved -> dispatched -> acknowledged
      reserved -> dispatched -> unknown_effect

    Disallowed examples:
      acknowledged -> dispatched
      unknown_effect -> reserved
      unknown_effect -> dispatched
    """

    def __init__(self) -> None:
        self._records_by_key: dict[str, DedupeRecord] = {}

    def reserve(self, envelope: IdempotencyEnvelope) -> DedupeReservation:
        """Reserves one dispatch idempotency key if absent.

        Args:
            envelope: Idempotency metadata for the outgoing dispatch.

        Returns:
            Reservation result with acceptance status.
        """
        existing_record = self._records_by_key.get(envelope.dispatch_idempotency_key)
        if existing_record is not None:
            return DedupeReservation(
                accepted=False,
                reason="duplicate",
                existing_record=existing_record,
            )

        self._records_by_key[envelope.dispatch_idempotency_key] = DedupeRecord(
            dispatch_idempotency_key=envelope.dispatch_idempotency_key,
            operation_fingerprint=envelope.operation_fingerprint,
            attempt_seq=envelope.attempt_seq,
            state="reserved",
            peer_operation_id=envelope.peer_operation_id,
        )
        return DedupeReservation(accepted=True, reason="accepted")

    def mark_dispatched(
        self,
        dispatch_idempotency_key: str,
        peer_operation_id: str | None = None,
    ) -> None:
        """Marks record as dispatched.

        Raises:
            DedupeStoreStateError: If key is missing or transition is invalid.
        """
        record = self._get_required_record(dispatch_idempotency_key)
        if record.state not in ("reserved", "dispatched"):
            raise DedupeStoreStateError(
                f"Cannot transition {record.state} -> dispatched."
            )
        self._records_by_key[dispatch_idempotency_key] = DedupeRecord(
            dispatch_idempotency_key=record.dispatch_idempotency_key,
            operation_fingerprint=record.operation_fingerprint,
            attempt_seq=record.attempt_seq,
            state="dispatched",
            peer_operation_id=peer_operation_id or record.peer_operation_id,
            external_ack_ref=record.external_ack_ref,
        )

    def mark_acknowledged(
        self,
        dispatch_idempotency_key: str,
        external_ack_ref: str | None = None,
    ) -> None:
        """Marks record as acknowledged.

        Raises:
            DedupeStoreStateError: If key is missing or transition is invalid.
        """
        record = self._get_required_record(dispatch_idempotency_key)
        if record.state not in ("dispatched", "acknowledged"):
            raise DedupeStoreStateError(
                f"Cannot transition {record.state} -> acknowledged."
            )
        self._records_by_key[dispatch_idempotency_key] = DedupeRecord(
            dispatch_idempotency_key=record.dispatch_idempotency_key,
            operation_fingerprint=record.operation_fingerprint,
            attempt_seq=record.attempt_seq,
            state="acknowledged",
            peer_operation_id=record.peer_operation_id,
            external_ack_ref=external_ack_ref or record.external_ack_ref,
        )

    def mark_unknown_effect(self, dispatch_idempotency_key: str) -> None:
        """Marks record as unknown_effect for ambiguous side-effect outcomes.

        Raises:
            DedupeStoreStateError: If key is missing or transition is invalid.
        """
        record = self._get_required_record(dispatch_idempotency_key)
        if record.state not in ("dispatched", "unknown_effect"):
            raise DedupeStoreStateError(
                f"Cannot transition {record.state} -> unknown_effect."
            )
        self._records_by_key[dispatch_idempotency_key] = DedupeRecord(
            dispatch_idempotency_key=record.dispatch_idempotency_key,
            operation_fingerprint=record.operation_fingerprint,
            attempt_seq=record.attempt_seq,
            state="unknown_effect",
            peer_operation_id=record.peer_operation_id,
            external_ack_ref=record.external_ack_ref,
        )

    def get(self, dispatch_idempotency_key: str) -> DedupeRecord | None:
        """Returns dedupe record by dispatch key.

        Args:
            dispatch_idempotency_key: Key to look up.

        Returns:
            Matching record, or ``None`` if not found.
        """
        return self._records_by_key.get(dispatch_idempotency_key)

    def _get_required_record(self, dispatch_idempotency_key: str) -> DedupeRecord:
        """Gets record or raises state error when key is unknown.

        Args:
            dispatch_idempotency_key: Key to look up.

        Returns:
            Matching dedupe record.

        Raises:
            DedupeStoreStateError: If no record exists for the key.
        """
        record = self._records_by_key.get(dispatch_idempotency_key)
        if record is None:
            raise DedupeStoreStateError(
                f"Unknown dispatch_idempotency_key: {dispatch_idempotency_key}."
            )
        return record
