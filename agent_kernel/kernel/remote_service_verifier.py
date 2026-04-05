"""End-to-end verifier for remote-service idempotency contract behavior."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from agent_kernel.kernel.contracts import (
    ExternalIdempotencyLevel,
    RemoteServiceIdempotencyContract,
)
from agent_kernel.kernel.dedupe_store import DedupeStorePort, IdempotencyEnvelope
from agent_kernel.kernel.remote_service_policy import (
    RemoteDispatchPolicyDecision,
    evaluate_remote_service_policy,
)


@dataclass(frozen=True, slots=True)
class RemoteServiceSimProfile:
    """Configuration for one simulated remote-service behavior."""

    accepts_key: bool = True
    stable_ack: bool = True
    conflict_on_duplicate: bool = True
    fail_mode: Literal["none", "timeout", "reject_key", "transient_500"] = "none"


@dataclass(frozen=True, slots=True)
class VerificationScenario:
    """One end-to-end verification scenario."""

    name: str
    profile: RemoteServiceSimProfile
    declared_level: ExternalIdempotencyLevel
    contract: RemoteServiceIdempotencyContract | None
    expected_effective_level: ExternalIdempotencyLevel
    expected_can_claim_guaranteed: bool
    expected_dedupe_terminal_state: str


@dataclass(slots=True)
class VerificationResult:
    """Result for one scenario run."""

    scenario_name: str
    passed: bool
    policy_decision: RemoteDispatchPolicyDecision
    actual_dedupe_state: str | None = None
    failure_reason: str | None = None


STANDARD_SCENARIOS: list[VerificationScenario] = [
    VerificationScenario(
        name="happy_path_guaranteed",
        profile=RemoteServiceSimProfile(),
        declared_level="guaranteed",
        contract=RemoteServiceIdempotencyContract(
            accepts_dispatch_idempotency_key=True,
            returns_stable_ack=True,
            peer_retry_model="exactly_once_claimed",
            default_retry_policy="bounded_retry",
        ),
        expected_effective_level="guaranteed",
        expected_can_claim_guaranteed=True,
        expected_dedupe_terminal_state="acknowledged",
    ),
    VerificationScenario(
        name="missing_contract_downgrade",
        profile=RemoteServiceSimProfile(accepts_key=False, stable_ack=False),
        declared_level="guaranteed",
        contract=None,
        expected_effective_level="best_effort",
        expected_can_claim_guaranteed=False,
        expected_dedupe_terminal_state="acknowledged",
    ),
    VerificationScenario(
        name="timeout_to_unknown_effect",
        profile=RemoteServiceSimProfile(fail_mode="timeout"),
        declared_level="best_effort",
        contract=RemoteServiceIdempotencyContract(
            accepts_dispatch_idempotency_key=True,
            returns_stable_ack=False,
            peer_retry_model="at_least_once",
            default_retry_policy="no_auto_retry",
        ),
        expected_effective_level="best_effort",
        expected_can_claim_guaranteed=False,
        expected_dedupe_terminal_state="unknown_effect",
    ),
    VerificationScenario(
        name="conflict_409_dedupe_hit",
        profile=RemoteServiceSimProfile(conflict_on_duplicate=True),
        declared_level="guaranteed",
        contract=RemoteServiceIdempotencyContract(
            accepts_dispatch_idempotency_key=True,
            returns_stable_ack=True,
            peer_retry_model="exactly_once_claimed",
            default_retry_policy="bounded_retry",
        ),
        expected_effective_level="guaranteed",
        expected_can_claim_guaranteed=True,
        expected_dedupe_terminal_state="acknowledged",
    ),
    VerificationScenario(
        name="reject_key_fallback",
        profile=RemoteServiceSimProfile(fail_mode="reject_key", accepts_key=False),
        declared_level="guaranteed",
        contract=RemoteServiceIdempotencyContract(
            accepts_dispatch_idempotency_key=False,
            returns_stable_ack=True,
            peer_retry_model="at_most_once",
            default_retry_policy="no_auto_retry",
        ),
        expected_effective_level="best_effort",
        expected_can_claim_guaranteed=False,
        expected_dedupe_terminal_state="acknowledged",
    ),
]


class RemoteServiceVerifier:
    """Execute policy + dedupe lifecycle checks for remote-service scenarios."""

    def __init__(self, dedupe_store: DedupeStorePort) -> None:
        self._dedupe_store = dedupe_store

    def run_all(
        self,
        scenarios: list[VerificationScenario] | None = None,
    ) -> list[VerificationResult]:
        """Run all scenarios and return per-scenario result objects."""
        return [self._run_one(s) for s in (scenarios or STANDARD_SCENARIOS)]

    def _run_one(self, scenario: VerificationScenario) -> VerificationResult:
        """Run one scenario end-to-end."""
        decision = evaluate_remote_service_policy(
            scenario.declared_level,
            scenario.contract,
        )
        if decision.effective_idempotency_level != scenario.expected_effective_level:
            return VerificationResult(
                scenario_name=scenario.name,
                passed=False,
                policy_decision=decision,
                failure_reason=(
                    "effective_idempotency_level mismatch: expected "
                    f"{scenario.expected_effective_level!r}, got "
                    f"{decision.effective_idempotency_level!r}"
                ),
            )
        if decision.can_claim_guaranteed != scenario.expected_can_claim_guaranteed:
            return VerificationResult(
                scenario_name=scenario.name,
                passed=False,
                policy_decision=decision,
                failure_reason=(
                    "can_claim_guaranteed mismatch: expected "
                    f"{scenario.expected_can_claim_guaranteed!r}, got "
                    f"{decision.can_claim_guaranteed!r}"
                ),
            )

        key = f"verify:{scenario.name}"
        envelope = IdempotencyEnvelope(
            dispatch_idempotency_key=key,
            operation_fingerprint=f"fp:{scenario.name}",
            attempt_seq=1,
            effect_scope="remote_service",
            capability_snapshot_hash="verify_hash",
            host_kind="remote_service",
        )
        first_reservation = self._dedupe_store.reserve_and_dispatch(envelope)
        if not first_reservation.accepted:
            return VerificationResult(
                scenario_name=scenario.name,
                passed=False,
                policy_decision=decision,
                failure_reason="initial reservation rejected unexpectedly",
            )

        if scenario.profile.fail_mode == "timeout":
            self._dedupe_store.mark_unknown_effect(key)
        else:
            self._dedupe_store.mark_acknowledged(key, external_ack_ref=f"ack:{scenario.name}")

        # Simulate duplicate replay for a service that returns 409 Conflict on
        # the same idempotency key: the second dispatch should be dedupe-hit.
        if scenario.name == "conflict_409_dedupe_hit" and scenario.profile.conflict_on_duplicate:
            replay_reservation = self._dedupe_store.reserve_and_dispatch(envelope)
            if replay_reservation.accepted:
                return VerificationResult(
                    scenario_name=scenario.name,
                    passed=False,
                    policy_decision=decision,
                    failure_reason="expected duplicate dedupe hit, but replay was accepted",
                )

        record = self._dedupe_store.get(key)
        actual_state = record.state if record is not None else "missing"
        if actual_state != scenario.expected_dedupe_terminal_state:
            return VerificationResult(
                scenario_name=scenario.name,
                passed=False,
                policy_decision=decision,
                actual_dedupe_state=actual_state,
                failure_reason=(
                    "dedupe terminal state mismatch: expected "
                    f"{scenario.expected_dedupe_terminal_state!r}, got {actual_state!r}"
                ),
            )

        return VerificationResult(
            scenario_name=scenario.name,
            passed=True,
            policy_decision=decision,
            actual_dedupe_state=actual_state,
        )
