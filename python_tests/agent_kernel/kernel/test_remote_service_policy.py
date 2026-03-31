"""Tests for v6.4 remote-service idempotency default policy."""

from __future__ import annotations

from agent_kernel.kernel.contracts import RemoteServiceIdempotencyContract
from agent_kernel.kernel.remote_service_policy import (
    evaluate_remote_service_policy,
)


def test_missing_remote_contract_forces_no_auto_retry_and_no_guaranteed_claim() -> None:
    """Missing contract should enforce conservative defaults."""
    decision = evaluate_remote_service_policy(
        external_level="guaranteed",
        contract=None,
    )

    assert decision.default_retry_policy == "no_auto_retry"
    assert not decision.auto_retry_enabled
    assert not decision.can_claim_guaranteed
    assert decision.effective_idempotency_level == "best_effort"


def test_remote_contract_without_ack_or_key_downgrades_guaranteed_claim() -> None:
    """Guaranteed claim should be downgraded when key/ACK guarantees are missing."""
    contract = RemoteServiceIdempotencyContract(
        accepts_dispatch_idempotency_key=False,
        returns_stable_ack=False,
        peer_retry_model="unknown",
        default_retry_policy="no_auto_retry",
    )
    decision = evaluate_remote_service_policy(
        external_level="guaranteed",
        contract=contract,
    )

    assert decision.effective_idempotency_level == "best_effort"
    assert not decision.can_claim_guaranteed
    assert decision.default_retry_policy == "no_auto_retry"
    assert not decision.auto_retry_enabled


def test_remote_contract_with_verified_requirements_allows_guaranteed_claim() -> None:
    """Verified contract should preserve guaranteed level and bounded retry policy."""
    contract = RemoteServiceIdempotencyContract(
        accepts_dispatch_idempotency_key=True,
        returns_stable_ack=True,
        peer_retry_model="at_least_once",
        default_retry_policy="bounded_retry",
    )
    decision = evaluate_remote_service_policy(
        external_level="guaranteed",
        contract=contract,
    )

    assert decision.effective_idempotency_level == "guaranteed"
    assert decision.can_claim_guaranteed
    assert decision.default_retry_policy == "bounded_retry"
    assert decision.auto_retry_enabled
