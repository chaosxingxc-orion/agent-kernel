"""Remote-service host idempotency policy evaluation for v6.4 defaults."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_kernel.kernel.contracts import (
        ExternalIdempotencyLevel,
        RemoteServiceIdempotencyContract,
    )


@dataclass(frozen=True, slots=True)
class RemoteDispatchPolicyDecision:
    """Represents effective dispatch policy after remote contract evaluation.

    Attributes:
        effective_idempotency_level: Resolved idempotency guarantee level.
        default_retry_policy: Retry policy for the remote dispatch.
        auto_retry_enabled: Whether automatic retry is enabled.
        can_claim_guaranteed: Whether the dispatch can claim guaranteed
            idempotency.
        reason: Human-readable reason for the policy evaluation outcome.
    """

    effective_idempotency_level: ExternalIdempotencyLevel
    default_retry_policy: str
    auto_retry_enabled: bool
    can_claim_guaranteed: bool
    reason: str


def evaluate_remote_service_policy(
    external_level: ExternalIdempotencyLevel | None,
    contract: RemoteServiceIdempotencyContract | None,
) -> RemoteDispatchPolicyDecision:
    """Evaluates remote-service policy with conservative v6.4 defaults.

    Policy rules:
      - Missing contract => ``no_auto_retry`` + cannot claim guaranteed.
      - Guaranteed level requires accepted dispatch key and stable ACK.
      - Bounded retry is only enabled when contract explicitly allows it.

    Args:
        external_level: Optional declared external idempotency level.
        contract: Optional remote-service idempotency contract.

    Returns:
        Conservative dispatch policy decision for remote-side execution.
    """
    normalized_level = external_level or "unknown"
    if contract is None:
        return RemoteDispatchPolicyDecision(
            effective_idempotency_level=(
                "best_effort" if normalized_level == "guaranteed" else normalized_level
            ),
            default_retry_policy="no_auto_retry",
            auto_retry_enabled=False,
            can_claim_guaranteed=False,
            reason="missing_remote_idempotency_contract",
        )

    can_claim_guaranteed = (
        normalized_level == "guaranteed"
        and contract.accepts_dispatch_idempotency_key
        and contract.returns_stable_ack
    )
    effective_level: ExternalIdempotencyLevel
    if can_claim_guaranteed:
        effective_level = "guaranteed"
    elif normalized_level == "guaranteed":
        effective_level = "best_effort"
    else:
        effective_level = normalized_level

    auto_retry_enabled = contract.default_retry_policy == "bounded_retry"
    reason = "validated_remote_contract" if can_claim_guaranteed else "remote_contract_constrained"
    return RemoteDispatchPolicyDecision(
        effective_idempotency_level=effective_level,
        default_retry_policy=contract.default_retry_policy,
        auto_retry_enabled=auto_retry_enabled,
        can_claim_guaranteed=can_claim_guaranteed,
        reason=reason,
    )
