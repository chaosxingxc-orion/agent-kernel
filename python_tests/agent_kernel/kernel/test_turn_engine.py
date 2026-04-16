"""Verifies for v6.4 turnengine canonical path and fsm guardrails."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from agent_kernel.kernel.capability_snapshot import (
    CapabilitySnapshot,
    CapabilitySnapshotBuildError,
)
from agent_kernel.kernel.contracts import Action, EffectClass, ExecutionContext
from agent_kernel.kernel.dedupe_store import (
    DedupeReservation,
    DedupeStorePort,
    IdempotencyEnvelope,
    InMemoryDedupeStore,
)
from agent_kernel.kernel.idempotency_key_policy import IdempotencyKeyPolicy
from agent_kernel.kernel.turn_engine import (
    TurnEngine,
    TurnEngineDefaults,
    TurnInput,
)


@dataclass(slots=True)
class _StubSnapshotBuilder:
    """Builds fixed snapshots for tests and allows fault injection."""

    should_fail: bool = False

    def build(self, *_args: Any, **_kwargs: Any) -> CapabilitySnapshot:
        """Builds a test fixture value."""
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
        """Admit."""
        self.calls += 1
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
        """Executes the test operation."""
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
        """Executes the test operation."""
        if execution_context is not None:
            self.contexts.append(execution_context)
        return self.result


@dataclass(slots=True)
class _FailingReserveDedupeStore(DedupeStorePort):
    """Raises on reserve to simulate dedupe backend outage."""

    records: dict[str, str] = field(default_factory=dict)

    def reserve(self, envelope: IdempotencyEnvelope) -> DedupeReservation:
        """Reserves a test key."""
        del envelope
        raise RuntimeError("dedupe unavailable")

    def reserve_and_dispatch(
        self,
        envelope: IdempotencyEnvelope,
        peer_operation_id: str | None = None,
    ) -> DedupeReservation:
        """Reserve and dispatch."""
        del envelope, peer_operation_id
        raise RuntimeError("dedupe unavailable")

    def mark_dispatched(
        self,
        dispatch_idempotency_key: str,
        peer_operation_id: str | None = None,
    ) -> None:
        """Mark dispatched."""
        del peer_operation_id
        self.records[dispatch_idempotency_key] = "dispatched"

    def mark_acknowledged(
        self,
        dispatch_idempotency_key: str,
        external_ack_ref: str | None = None,
    ) -> None:
        """Mark acknowledged."""
        del external_ack_ref
        self.records[dispatch_idempotency_key] = "acknowledged"

    def mark_unknown_effect(self, dispatch_idempotency_key: str) -> None:
        """Mark unknown effect."""
        self.records[dispatch_idempotency_key] = "unknown_effect"

    def get(self, dispatch_idempotency_key: str) -> Any:
        """Gets test data."""
        return self.records.get(dispatch_idempotency_key)


def _build_action(
    effect_class: str = EffectClass.IDEMPOTENT_WRITE,
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

    result = asyncio.run(turn_engine.run_turn(_build_turn_input(), _build_action()))

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

    result = asyncio.run(turn_engine.run_turn(_build_turn_input(), _build_action()))

    assert result.state == "dispatch_blocked"
    assert result.outcome_kind == "blocked"
    assert admission.calls == 1


def test_turn_engine_blocks_duplicate_dedupe_key_before_dispatch() -> None:
    """Dedupe duplicate should short-circuit dispatch path."""
    action = _build_action()
    dedupe_store = InMemoryDedupeStore()
    dedupe_key = IdempotencyKeyPolicy.generate(
        run_id="run-1",
        action=action,
        snapshot_hash="hash-1",
    )
    envelope = IdempotencyEnvelope(
        dispatch_idempotency_key=dedupe_key,
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
    result = asyncio.run(turn_engine.run_turn(_build_turn_input(), action))

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

    result = asyncio.run(turn_engine.run_turn(_build_turn_input(), _build_action()))

    assert result.state == "dispatch_acknowledged"
    assert result.outcome_kind == "dispatched"
    assert admission.calls == 1
    assert result.dispatch_dedupe_key is not None
    assert result.dispatch_dedupe_key.startswith("dispatch:run-1:action-1:")
    assert result.intent_commit_ref != ""
    assert [event.state for event in result.emitted_events] == [
        "collecting",
        "intent_committed",
        "snapshot_built",
        "admission_checked",
        "dispatched",
        "dispatch_acknowledged",
    ]


def test_turn_engine_effect_unknown_maps_to_recovery_pending() -> None:
    """Ambiguous execution evidence should move turn into recovery_pending."""
    turn_engine = TurnEngine(
        snapshot_builder=_StubSnapshotBuilder(),
        admission_service=_StubAdmissionService(admitted=True),
        dedupe_store=InMemoryDedupeStore(),
        executor=_StubExecutor(result={"acknowledged": False}),
    )

    result = asyncio.run(turn_engine.run_turn(_build_turn_input(), _build_action()))

    assert result.state == "recovery_pending"
    assert result.outcome_kind == "recovery_pending"
    assert result.recovery_input is not None
    assert result.recovery_input.failure_class == "unknown"
    assert result.recovery_input.evidence_priority_source == "local_inference"
    assert result.recovery_input.evidence_priority_ref == "turn_engine:effect_unknown_without_ack"


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
    assert any(event.state == "dedupe_degraded" for event in result.emitted_events)
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

    result = asyncio.run(turn_engine.run_turn(_build_turn_input(), _build_action()))

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


# ---------------------------------------------------------------------------
# P4c — on_admission_evaluated + on_dispatch_attempted hook callbacks
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _CapturingHook:
    """Minimal ObservabilityHook stub that records P4c hook invocations."""

    admission_calls: list[dict[str, Any]] = field(default_factory=list)
    dispatch_calls: list[dict[str, Any]] = field(default_factory=list)

    # required by Protocol — unused in these tests
    def on_turn_state_transition(self, *_a: Any, **_kw: Any) -> None:
        """On turn state transition."""
        pass

    def on_recovery_triggered(self, *_a: Any, **_kw: Any) -> None:
        """On recovery triggered."""
        pass

    def on_parallel_branch_result(self, *_a: Any, **_kw: Any) -> None:
        """On parallel branch result."""
        pass

    def on_admission_evaluated(
        self,
        *,
        run_id: str,
        action_id: str,
        admitted: bool,
        latency_ms: int,
    ) -> None:
        """On admission evaluated."""
        self.admission_calls.append(
            {
                "run_id": run_id,
                "action_id": action_id,
                "admitted": admitted,
                "latency_ms": latency_ms,
            }
        )

    def on_dispatch_attempted(
        self,
        *,
        run_id: str,
        action_id: str,
        dedupe_outcome: str,
        latency_ms: int,
    ) -> None:
        """On dispatch attempted."""
        self.dispatch_calls.append(
            {
                "run_id": run_id,
                "action_id": action_id,
                "dedupe_outcome": dedupe_outcome,
                "latency_ms": latency_ms,
            }
        )


def _build_engine_with_hook(hook: _CapturingHook, admitted: bool = True) -> TurnEngine:
    """Build engine with hook."""
    return TurnEngine(
        snapshot_builder=_StubSnapshotBuilder(),
        admission_service=_StubAdmissionService(admitted=admitted),
        dedupe_store=InMemoryDedupeStore(),
        executor=_StubExecutor(result={"acknowledged": True}),
        observability_hook=hook,
    )


class TestObservabilityHookCallbacks:
    """Verifies that TurnEngine invokes P4c hooks with correct arguments."""

    def test_on_admission_evaluated_called_when_admitted(self) -> None:
        """Verifies on admission evaluated called when admitted."""
        hook = _CapturingHook()
        engine = _build_engine_with_hook(hook, admitted=True)
        asyncio.run(engine.run_turn(_build_turn_input(), _build_action()))

        assert len(hook.admission_calls) == 1
        call = hook.admission_calls[0]
        assert call["admitted"] is True
        assert call["run_id"] == "run-1"
        assert isinstance(call["latency_ms"], int)
        assert call["latency_ms"] >= 0

    def test_on_admission_evaluated_called_when_blocked(self) -> None:
        """Verifies on admission evaluated called when blocked."""
        hook = _CapturingHook()
        engine = _build_engine_with_hook(hook, admitted=False)
        asyncio.run(engine.run_turn(_build_turn_input(), _build_action()))

        assert len(hook.admission_calls) == 1
        assert hook.admission_calls[0]["admitted"] is False
        # dispatch hook should not be called when admission is blocked
        assert len(hook.dispatch_calls) == 0

    def test_on_dispatch_attempted_called_on_successful_dispatch(self) -> None:
        """Verifies on dispatch attempted called on successful dispatch."""
        hook = _CapturingHook()
        engine = _build_engine_with_hook(hook, admitted=True)
        asyncio.run(engine.run_turn(_build_turn_input(), _build_action()))

        assert len(hook.dispatch_calls) == 1
        call = hook.dispatch_calls[0]
        assert call["run_id"] == "run-1"
        assert isinstance(call["dedupe_outcome"], str)
        assert isinstance(call["latency_ms"], int)
        assert call["latency_ms"] >= 0

    def test_no_hook_does_not_raise(self) -> None:
        """TurnEngine without observability_hook must complete without error."""
        engine = TurnEngine(
            snapshot_builder=_StubSnapshotBuilder(),
            admission_service=_StubAdmissionService(admitted=True),
            dedupe_store=InMemoryDedupeStore(),
            executor=_StubExecutor(result={"acknowledged": True}),
        )
        result = asyncio.run(engine.run_turn(_build_turn_input(), _build_action()))
        assert result.outcome_kind == "dispatched"


# ---------------------------------------------------------------------------
# R3c — TurnEngine FSM dispatch table extensibility
# ---------------------------------------------------------------------------


class TestTurnEngineDispatchTableExtensibility:
    """Verifies that _TURN_PHASES can be extended in subclasses."""

    def test_turn_phases_is_class_variable_with_six_phases(self) -> None:
        """Verifies turn phases is class variable with six phases."""
        from agent_kernel.kernel.turn_engine import TurnEngine

        assert hasattr(TurnEngine, "_TURN_PHASES")
        assert len(TurnEngine._TURN_PHASES) == 6

    def test_subclass_can_inject_custom_phase(self) -> None:
        """A subclass that prepends a no-op phase must still complete normally."""
        from agent_kernel.kernel.turn_engine import TurnEngine, TurnPhaseContext

        intercepted: list[str] = []

        class _CustomEngine(TurnEngine):
            """Test suite for  CustomEngine."""

            _TURN_PHASES = ("_phase_audit", *TurnEngine._TURN_PHASES)

            async def _phase_audit(self, ctx: TurnPhaseContext) -> None:
                """Phase audit."""
                intercepted.append("audit_called")

        engine = _CustomEngine(
            snapshot_builder=_StubSnapshotBuilder(),
            admission_service=_StubAdmissionService(admitted=True),
            dedupe_store=InMemoryDedupeStore(),
            executor=_StubExecutor(result={"acknowledged": True}),
        )
        result = asyncio.run(engine.run_turn(_build_turn_input(), _build_action()))
        assert result.outcome_kind == "dispatched"
        assert intercepted == ["audit_called"]

    def test_subclass_can_override_admission_phase(self) -> None:
        """A subclass that overrides _phase_admission can short-circuit to blocked."""
        from agent_kernel.kernel.turn_engine import TurnEngine, TurnPhaseContext, TurnResult

        class _AlwaysBlockEngine(TurnEngine):
            """Test suite for  AlwaysBlockEngine."""

            async def _phase_admission(self, ctx: TurnPhaseContext) -> None:
                """Phase admission."""
                ctx.emitted_events.append(
                    __import__(
                        "agent_kernel.kernel.turn_engine",
                        fromlist=["TurnStateEvent"],
                    ).TurnStateEvent(state="dispatch_blocked")
                )
                assert ctx.turn_identity is not None
                ti = ctx.turn_identity
                ctx.result = TurnResult(
                    state="dispatch_blocked",
                    outcome_kind="blocked",
                    decision_ref=ti.decision_ref,
                    decision_fingerprint=ti.decision_fingerprint,
                    emitted_events=ctx.emitted_events,
                )

        engine = _AlwaysBlockEngine(
            snapshot_builder=_StubSnapshotBuilder(),
            admission_service=_StubAdmissionService(admitted=True),
            dedupe_store=InMemoryDedupeStore(),
            executor=_StubExecutor(result={"acknowledged": True}),
        )
        result = asyncio.run(engine.run_turn(_build_turn_input(), _build_action()))
        assert result.outcome_kind == "blocked"


# ---------------------------------------------------------------------------
# TurnEngineDefaults injection
# ---------------------------------------------------------------------------


def test_turn_engine_uses_default_defaults() -> None:
    """TurnEngine without explicit defaults uses PoC fallback values."""
    engine = TurnEngine(
        snapshot_builder=_StubSnapshotBuilder(),
        admission_service=_StubAdmissionService(admitted=True),
        dedupe_store=InMemoryDedupeStore(),
        executor=_StubExecutor(result={"acknowledged": True}),
    )
    expected = TurnEngineDefaults(
        model_ref="echo",
        tenant_policy_ref="policy:default",
        permission_mode="strict",
    )
    assert engine._defaults == expected
    assert engine._defaults.model_ref == "echo"
    assert engine._defaults.tenant_policy_ref == "policy:default"
    assert engine._defaults.permission_mode == "strict"


def test_turn_engine_uses_custom_defaults() -> None:
    """TurnEngine with custom TurnEngineDefaults stores them correctly."""
    custom = TurnEngineDefaults(
        model_ref="gpt-4o",
        tenant_policy_ref="policy:enterprise",
        permission_mode="permissive",
    )
    engine = TurnEngine(
        snapshot_builder=_StubSnapshotBuilder(),
        admission_service=_StubAdmissionService(admitted=True),
        dedupe_store=InMemoryDedupeStore(),
        executor=_StubExecutor(result={"acknowledged": True}),
        defaults=custom,
    )
    assert engine._defaults is custom
    assert engine._defaults.model_ref == "gpt-4o"
    assert engine._defaults.tenant_policy_ref == "policy:enterprise"
    assert engine._defaults.permission_mode == "permissive"
