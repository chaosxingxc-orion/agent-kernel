"""Tests for v6.4 TurnEngine canonical path and FSM guardrails."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from agent_kernel.kernel.capability_snapshot import (
    CapabilitySnapshot,
    CapabilitySnapshotBuildError,
)
from agent_kernel.kernel.contracts import Action, ExecutionContext
from agent_kernel.kernel.dedupe_store import (
    DedupeReservation,
    DedupeStorePort,
    IdempotencyEnvelope,
    InMemoryDedupeStore,
)
from agent_kernel.kernel.turn_engine import (
    TurnEngine,
    TurnInput,
)


@dataclass(slots=True)
class _StubSnapshotBuilder:
    """Builds fixed snapshots for tests and allows fault injection."""

    should_fail: bool = False

    def build(self, *_args: Any, **_kwargs: Any) -> CapabilitySnapshot:
        if self.should_fail:
            raise CapabilitySnapshotBuildError("missing critical input")
        return CapabilitySnapshot(
            snapshot_ref="snapshot:run-1:1:abc",
            snapshot_hash="hash-1",
            run_id="run-1",
            based_on_offset=1,
            tenant_policy_ref="policy:v1",
            permission_mode="strict",
            tool_bindings=["tool.search"],
            mcp_bindings=[],
            skill_bindings=[],
            feature_flags=[],
            created_at="2026-03-31T00:00:00Z",
        )


@dataclass(slots=True)
class _StubAdmissionService:
    """Returns static admission and records call count for FSM assertions."""

    admitted: bool
    calls: int = 0

    async def admit(self, *_args: Any, **_kwargs: Any) -> bool:
        self.calls += 1
        return self.admitted

    async def check(self, *_args: Any, **_kwargs: Any) -> bool:
        self.calls += 1
        return self.admitted


@dataclass(slots=True)
class _LegacyCheckOnlyAdmissionService:
    """Legacy admission stub that only supports ``check`` path."""

    admitted: bool
    calls: int = 0

    async def check(self, *_args: Any, **_kwargs: Any) -> bool:
        self.calls += 1
        return self.admitted


@dataclass(slots=True)
class _DualPathAdmissionService:
    """Admission stub exposing both admit/check with independent counters."""

    admitted: bool
    admit_calls: int = 0
    check_calls: int = 0

    async def admit(self, *_args: Any, **_kwargs: Any) -> bool:
        self.admit_calls += 1
        return self.admitted

    async def check(self, *_args: Any, **_kwargs: Any) -> bool:
        self.check_calls += 1
        return self.admitted


@dataclass(slots=True)
class _SubjectCapturingLegacyAdmissionService:
    """Legacy check-only admission that captures the provided subject object."""

    admitted: bool
    calls: int = 0
    subjects: list[Any] = field(default_factory=list)

    async def check(self, _action: Any, subject: Any) -> bool:
        self.calls += 1
        self.subjects.append(subject)
        return self.admitted


@dataclass(slots=True)
class _StubExecutor:
    """Returns static execution result package used by TurnEngine tests."""

    result: dict[str, Any]
    envelopes: list[IdempotencyEnvelope] = field(default_factory=list)

    async def execute(
        self,
        _action: Action,
        _snapshot: CapabilitySnapshot,
        envelope: IdempotencyEnvelope,
    ) -> dict[str, Any]:
        self.envelopes.append(envelope)
        return self.result


@dataclass(slots=True)
class _ContextAwareStubExecutor:
    """Captures execution context when TurnEngine passes context envelope."""

    result: dict[str, Any]
    contexts: list[ExecutionContext] = field(default_factory=list)

    async def execute(
        self,
        _action: Action,
        _snapshot: CapabilitySnapshot,
        _envelope: IdempotencyEnvelope,
        execution_context: ExecutionContext | None = None,
    ) -> dict[str, Any]:
        if execution_context is not None:
            self.contexts.append(execution_context)
        return self.result


@dataclass(slots=True)
class _FailingReserveDedupeStore(DedupeStorePort):
    """Raises on reserve to simulate dedupe backend outage."""

    records: dict[str, str] = field(default_factory=dict)

    def reserve(self, envelope: IdempotencyEnvelope) -> DedupeReservation:
        del envelope
        raise RuntimeError("dedupe unavailable")

    def mark_dispatched(
        self,
        dispatch_idempotency_key: str,
        peer_operation_id: str | None = None,
    ) -> None:
        del peer_operation_id
        self.records[dispatch_idempotency_key] = "dispatched"

    def mark_acknowledged(
        self,
        dispatch_idempotency_key: str,
        external_ack_ref: str | None = None,
    ) -> None:
        del external_ack_ref
        self.records[dispatch_idempotency_key] = "acknowledged"

    def mark_unknown_effect(self, dispatch_idempotency_key: str) -> None:
        self.records[dispatch_idempotency_key] = "unknown_effect"

    def get(self, dispatch_idempotency_key: str) -> Any:
        return self.records.get(dispatch_idempotency_key)


def _build_action(
    effect_class: str = "idempotent_write",
    *,
    external_idempotency_level: str | None = None,
    input_json: dict[str, Any] | None = None,
    policy_tags: list[str] | None = None,
) -> Action:
    """Builds a deterministic action payload for turn tests."""
    payload = {"query": "agent kernel"}
    if input_json is not None:
        payload.update(input_json)
    return Action(
        action_id="action-1",
        run_id="run-1",
        action_type="tool.search",
        effect_class=effect_class,
        external_idempotency_level=external_idempotency_level,
        input_json=payload,
        policy_tags=policy_tags or [],
    )


def _build_turn_input() -> TurnInput:
    """Builds minimal turn input used by all test cases."""
    return TurnInput(
        run_id="run-1",
        through_offset=10,
        based_on_offset=10,
        trigger_type="start",
    )


def test_turn_engine_returns_completed_noop_when_snapshot_build_fails() -> None:
    """Snapshot failures should end turn without entering admission."""
    turn_engine = TurnEngine(
        snapshot_builder=_StubSnapshotBuilder(should_fail=True),
        admission_service=_StubAdmissionService(admitted=True),
        dedupe_store=InMemoryDedupeStore(),
        executor=_StubExecutor(result={"acknowledged": True}),
    )

    result = asyncio.run(
        turn_engine.run_turn(_build_turn_input(), _build_action())
    )

    assert result.state == "completed_noop"
    assert result.outcome_kind == "noop"
    assert result.recovery_input is None


def test_turn_engine_blocks_when_admission_denies() -> None:
    """Admission deny should produce blocked outcome without external dispatch."""
    admission = _StubAdmissionService(admitted=False)
    turn_engine = TurnEngine(
        snapshot_builder=_StubSnapshotBuilder(),
        admission_service=admission,
        dedupe_store=InMemoryDedupeStore(),
        executor=_StubExecutor(result={"acknowledged": True}),
    )

    result = asyncio.run(
        turn_engine.run_turn(_build_turn_input(), _build_action())
    )

    assert result.state == "dispatch_blocked"
    assert result.outcome_kind == "blocked"
    assert admission.calls == 1


def test_turn_engine_blocks_duplicate_dedupe_key_before_dispatch() -> None:
    """Dedupe duplicate should short-circuit dispatch path."""
    dedupe_store = InMemoryDedupeStore()
    envelope = IdempotencyEnvelope(
        dispatch_idempotency_key="run-1:action-1:10",
        operation_fingerprint="fp-1",
        attempt_seq=1,
        effect_scope="workspace.write",
        capability_snapshot_hash="hash-1",
        host_kind="local_cli",
    )
    dedupe_store.reserve(envelope)

    turn_engine = TurnEngine(
        snapshot_builder=_StubSnapshotBuilder(),
        admission_service=_StubAdmissionService(admitted=True),
        dedupe_store=dedupe_store,
        executor=_StubExecutor(result={"acknowledged": True}),
    )
    result = asyncio.run(
        turn_engine.run_turn(_build_turn_input(), _build_action())
    )

    assert result.state == "dispatch_blocked"
    assert result.outcome_kind == "blocked"


def test_turn_engine_returns_dispatched_when_executor_acknowledged() -> None:
    """Acknowledged dispatch should produce dispatched outcome."""
    admission = _StubAdmissionService(admitted=True)
    turn_engine = TurnEngine(
        snapshot_builder=_StubSnapshotBuilder(),
        admission_service=admission,
        dedupe_store=InMemoryDedupeStore(),
        executor=_StubExecutor(result={"acknowledged": True}),
    )

    result = asyncio.run(
        turn_engine.run_turn(_build_turn_input(), _build_action())
    )

    assert result.state == "dispatch_acknowledged"
    assert result.outcome_kind == "dispatched"
    assert admission.calls == 1
    assert result.dispatch_dedupe_key == "run-1:action-1:10"
    assert result.intent_commit_ref != ""
    assert [event["state"] for event in result.emitted_events] == [
        "collecting",
        "intent_committed",
        "snapshot_built",
        "admission_checked",
        "dispatched",
        "dispatch_acknowledged",
    ]


def test_turn_engine_does_not_emit_legacy_fallback_state_on_admit_path() -> None:
    """Primary admit path should not emit legacy check fallback state."""
    turn_engine = TurnEngine(
        snapshot_builder=_StubSnapshotBuilder(),
        admission_service=_StubAdmissionService(admitted=True),
        dedupe_store=InMemoryDedupeStore(),
        executor=_StubExecutor(result={"acknowledged": True}),
    )

    result = asyncio.run(turn_engine.run_turn(_build_turn_input(), _build_action()))

    assert all(
        event.get("state") != "admission_legacy_check_fallback"
        for event in result.emitted_events
    )


def test_turn_engine_emits_legacy_fallback_state_when_only_check_is_available() -> None:
    """Legacy check-only admission should emit fallback state for observability."""
    turn_engine = TurnEngine(
        snapshot_builder=_StubSnapshotBuilder(),
        admission_service=_LegacyCheckOnlyAdmissionService(admitted=True),
        dedupe_store=InMemoryDedupeStore(),
        executor=_StubExecutor(result={"acknowledged": True}),
    )

    result = asyncio.run(turn_engine.run_turn(_build_turn_input(), _build_action()))

    emitted_states = [event["state"] for event in result.emitted_events]
    assert "admission_legacy_check_fallback" in emitted_states
    assert emitted_states.index("admission_legacy_check_fallback") < emitted_states.index(
        "admission_checked"
    )
    fallback_event = next(
        event for event in result.emitted_events
        if event.get("state") == "admission_legacy_check_fallback"
    )
    assert fallback_event["severity"] == "warning"
    assert fallback_event["deprecation_phase"] == "soft"
    assert fallback_event["target_removal_version"] == "0.2"


def test_turn_engine_prefers_admit_when_admit_and_check_both_exist() -> None:
    """When both methods exist, admit path should be used and check should not run."""
    admission = _DualPathAdmissionService(admitted=True)
    turn_engine = TurnEngine(
        snapshot_builder=_StubSnapshotBuilder(),
        admission_service=admission,
        dedupe_store=InMemoryDedupeStore(),
        executor=_StubExecutor(result={"acknowledged": True}),
    )

    result = asyncio.run(turn_engine.run_turn(_build_turn_input(), _build_action()))

    assert result.state == "dispatch_acknowledged"
    assert admission.admit_calls == 1
    assert admission.check_calls == 0
    assert all(
        event.get("state") != "admission_legacy_check_fallback"
        for event in result.emitted_events
    )


def test_turn_engine_legacy_check_fallback_uses_explicit_admission_subject() -> None:
    """Legacy check fallback should receive explicit admission_subject when provided."""
    admission = _SubjectCapturingLegacyAdmissionService(admitted=True)
    turn_engine = TurnEngine(
        snapshot_builder=_StubSnapshotBuilder(),
        admission_service=admission,
        dedupe_store=InMemoryDedupeStore(),
        executor=_StubExecutor(result={"acknowledged": True}),
    )
    admission_subject = {"projection_like": True, "run_id": "run-1"}

    result = asyncio.run(
        turn_engine.run_turn(
            _build_turn_input(),
            _build_action(),
            admission_subject=admission_subject,
        )
    )

    assert result.state == "dispatch_acknowledged"
    assert admission.calls == 1
    assert admission.subjects[0] is admission_subject


def test_turn_engine_effect_unknown_maps_to_recovery_pending() -> None:
    """Ambiguous execution evidence should move turn into recovery_pending."""
    turn_engine = TurnEngine(
        snapshot_builder=_StubSnapshotBuilder(),
        admission_service=_StubAdmissionService(admitted=True),
        dedupe_store=InMemoryDedupeStore(),
        executor=_StubExecutor(result={"acknowledged": False}),
    )

    result = asyncio.run(
        turn_engine.run_turn(_build_turn_input(), _build_action())
    )

    assert result.state == "recovery_pending"
    assert result.outcome_kind == "recovery_pending"
    assert result.recovery_input is not None
    assert result.recovery_input.failure_class == "unknown"
    assert result.recovery_input.evidence_priority_source == "local_inference"
    assert (
        result.recovery_input.evidence_priority_ref
        == "turn_engine:effect_unknown_without_ack"
    )


def test_turn_engine_passes_execution_context_to_context_aware_executor() -> None:
    """TurnEngine should construct and pass ExecutionContext when supported."""
    executor = _ContextAwareStubExecutor(result={"acknowledged": True})
    turn_engine = TurnEngine(
        snapshot_builder=_StubSnapshotBuilder(),
        admission_service=_StubAdmissionService(admitted=True),
        dedupe_store=InMemoryDedupeStore(),
        executor=executor,
    )

    result = asyncio.run(turn_engine.run_turn(_build_turn_input(), _build_action()))

    assert result.state == "dispatch_acknowledged"
    assert len(executor.contexts) == 1
    execution_context = executor.contexts[0]
    assert execution_context.run_id == "run-1"
    assert execution_context.action_id == "action-1"
    assert execution_context.capability_snapshot_hash == "hash-1"


def test_turn_engine_degrades_when_dedupe_store_unavailable_for_idempotent_write() -> None:
    """Dedupe outage should degrade idempotent_write and still dispatch once."""
    executor = _StubExecutor(result={"acknowledged": True})
    failing_store = _FailingReserveDedupeStore()
    turn_engine = TurnEngine(
        snapshot_builder=_StubSnapshotBuilder(),
        admission_service=_StubAdmissionService(admitted=True),
        dedupe_store=failing_store,
        executor=executor,
    )

    result = asyncio.run(turn_engine.run_turn(_build_turn_input(), _build_action()))

    assert result.state == "dispatch_acknowledged"
    assert result.outcome_kind == "dispatched"
    assert any(event.get("state") == "dedupe_degraded" for event in result.emitted_events)
    assert executor.envelopes[0].effect_scope == "compensatable_write"


def test_turn_engine_degrade_to_irreversible_for_remote_service_host() -> None:
    """Remote service dispatch should degrade to irreversible_write on dedupe outage."""
    executor = _StubExecutor(result={"acknowledged": True})
    failing_store = _FailingReserveDedupeStore()
    turn_engine = TurnEngine(
        snapshot_builder=_StubSnapshotBuilder(),
        admission_service=_StubAdmissionService(admitted=True),
        dedupe_store=failing_store,
        executor=executor,
    )

    action = _build_action(
        external_idempotency_level="best_effort",
        input_json={"host_kind": "remote_service"},
    )
    result = asyncio.run(turn_engine.run_turn(_build_turn_input(), action))

    assert result.state == "dispatch_acknowledged"
    assert executor.envelopes[0].effect_scope == "irreversible_write"


def test_turn_engine_strict_mode_requires_declared_snapshot_inputs() -> None:
    """Strict mode should fail turn when declared snapshot input payload is absent."""
    turn_engine = TurnEngine(
        snapshot_builder=_StubSnapshotBuilder(),
        admission_service=_StubAdmissionService(admitted=True),
        dedupe_store=InMemoryDedupeStore(),
        executor=_StubExecutor(result={"acknowledged": True}),
        require_declared_snapshot_inputs=True,
    )

    result = asyncio.run(
        turn_engine.run_turn(_build_turn_input(), _build_action())
    )

    assert result.state == "completed_noop"
    assert result.outcome_kind == "noop"


def test_turn_engine_blocks_remote_service_guaranteed_when_contract_missing() -> None:
    """Remote guaranteed dispatch must block when no remote contract is supplied."""
    executor = _StubExecutor(result={"acknowledged": True})
    turn_engine = TurnEngine(
        snapshot_builder=_StubSnapshotBuilder(),
        admission_service=_StubAdmissionService(admitted=True),
        dedupe_store=InMemoryDedupeStore(),
        executor=executor,
    )

    result = asyncio.run(
        turn_engine.run_turn(
            _build_turn_input(),
            _build_action(
                external_idempotency_level="guaranteed",
                input_json={"host_kind": "remote_service"},
            ),
        )
    )

    assert result.state == "dispatch_blocked"
    assert result.outcome_kind == "blocked"
    assert result.host_kind == "remote_service"
    assert result.remote_policy_decision is not None
    assert result.remote_policy_decision.effective_idempotency_level == "best_effort"
    assert result.remote_policy_decision.default_retry_policy == "no_auto_retry"
    assert not result.remote_policy_decision.auto_retry_enabled
    assert not result.remote_policy_decision.can_claim_guaranteed
    assert result.remote_policy_decision.reason == "missing_remote_idempotency_contract"
    assert not executor.envelopes


def test_turn_engine_blocks_remote_service_guaranteed_when_contract_is_insufficient() -> None:
    """Remote guaranteed dispatch must block when contract cannot prove guarantees."""
    executor = _StubExecutor(result={"acknowledged": True})
    turn_engine = TurnEngine(
        snapshot_builder=_StubSnapshotBuilder(),
        admission_service=_StubAdmissionService(admitted=True),
        dedupe_store=InMemoryDedupeStore(),
        executor=executor,
    )

    result = asyncio.run(
        turn_engine.run_turn(
            _build_turn_input(),
            _build_action(
                external_idempotency_level="guaranteed",
                input_json={
                    "host_kind": "remote_service",
                    "remote_service_idempotency_contract": {
                        "accepts_dispatch_idempotency_key": False,
                        "returns_stable_ack": False,
                        "peer_retry_model": "unknown",
                        "default_retry_policy": "no_auto_retry",
                    },
                },
            ),
        )
    )

    assert result.state == "dispatch_blocked"
    assert result.outcome_kind == "blocked"
    assert result.host_kind == "remote_service"
    assert result.remote_policy_decision is not None
    assert result.remote_policy_decision.effective_idempotency_level == "best_effort"
    assert result.remote_policy_decision.default_retry_policy == "no_auto_retry"
    assert not result.remote_policy_decision.auto_retry_enabled
    assert not result.remote_policy_decision.can_claim_guaranteed
    assert result.remote_policy_decision.reason == "remote_contract_constrained"
    assert not executor.envelopes


def test_turn_engine_allows_remote_service_when_contract_is_verified() -> None:
    """Verified remote contract should allow dispatch with remote host kind."""
    executor = _StubExecutor(result={"acknowledged": True})
    turn_engine = TurnEngine(
        snapshot_builder=_StubSnapshotBuilder(),
        admission_service=_StubAdmissionService(admitted=True),
        dedupe_store=InMemoryDedupeStore(),
        executor=executor,
    )

    result = asyncio.run(
        turn_engine.run_turn(
            _build_turn_input(),
            _build_action(
                external_idempotency_level="guaranteed",
                input_json={
                    "host_kind": "remote_service",
                    "remote_service_idempotency_contract": {
                        "accepts_dispatch_idempotency_key": True,
                        "returns_stable_ack": True,
                        "peer_retry_model": "at_least_once",
                        "default_retry_policy": "bounded_retry",
                    },
                },
            ),
        )
    )

    assert result.state == "dispatch_acknowledged"
    assert result.outcome_kind == "dispatched"
    assert result.host_kind == "remote_service"
    assert result.remote_policy_decision is not None
    assert result.remote_policy_decision.effective_idempotency_level == "guaranteed"
    assert result.remote_policy_decision.default_retry_policy == "bounded_retry"
    assert result.remote_policy_decision.auto_retry_enabled
    assert result.remote_policy_decision.can_claim_guaranteed
    assert result.remote_policy_decision.reason == "validated_remote_contract"
    assert executor.envelopes[0].host_kind == "remote_service"


def test_turn_engine_keeps_local_hosts_unaffected_by_remote_policy() -> None:
    """Explicit local hosts should keep existing behavior and host selection."""
    local_process_executor = _StubExecutor(result={"acknowledged": True})
    local_process_engine = TurnEngine(
        snapshot_builder=_StubSnapshotBuilder(),
        admission_service=_StubAdmissionService(admitted=True),
        dedupe_store=InMemoryDedupeStore(),
        executor=local_process_executor,
    )
    local_process_result = asyncio.run(
        local_process_engine.run_turn(
            _build_turn_input(),
            _build_action(
                external_idempotency_level="guaranteed",
                policy_tags=["host:local_process"],
            ),
        )
    )

    local_cli_executor = _StubExecutor(result={"acknowledged": True})
    local_cli_engine = TurnEngine(
        snapshot_builder=_StubSnapshotBuilder(),
        admission_service=_StubAdmissionService(admitted=True),
        dedupe_store=InMemoryDedupeStore(),
        executor=local_cli_executor,
    )
    local_cli_result = asyncio.run(
        local_cli_engine.run_turn(
            _build_turn_input(),
            _build_action(
                external_idempotency_level="guaranteed",
                policy_tags=["host:local_cli"],
            ),
        )
    )

    assert local_process_result.state == "dispatch_acknowledged"
    assert local_process_result.outcome_kind == "dispatched"
    assert local_process_executor.envelopes[0].host_kind == "local_process"
    assert local_process_result.host_kind == "local_process"
    assert local_process_result.remote_policy_decision is None
    assert local_cli_result.state == "dispatch_acknowledged"
    assert local_cli_result.outcome_kind == "dispatched"
    assert local_cli_executor.envelopes[0].host_kind == "local_cli"
    assert local_cli_result.host_kind == "local_cli"
    assert local_cli_result.remote_policy_decision is None
