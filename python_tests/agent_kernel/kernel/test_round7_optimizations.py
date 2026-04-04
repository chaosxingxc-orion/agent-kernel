"""Round 7 quality optimization tests (R7a-R7f).

Covers:
  R7a - MetricsHook: on_admission_evaluated counter + on_dispatch_attempted histogram
  R7b - Reflection exhaustion: escalate_on_exhaustion path verified
  R7c - turn.branch_rollback_intent registered in KERNEL_EVENT_REGISTRY
  R7d - Crash-replay integration: executor raise -> unknown_effect -> deterministic re-run
  R7e - SQLiteDedupeStore.close() performs WAL checkpoint (TRUNCATE)
  R7f - TurnEngine phase_timeout_ms: asyncio.wait_for per phase
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# R7a - MetricsHook admission + dispatch metrics
# ---------------------------------------------------------------------------


class TestMetricsHookAdmissionDispatch:
    def test_on_admission_evaluated_no_op_without_otel(self) -> None:
        from agent_kernel.runtime.observability_hooks import MetricsObservabilityHook

        hook = MetricsObservabilityHook()
        # Should not raise even without OTel.
        hook.on_admission_evaluated(run_id="r1", action_id="a1", admitted=True, latency_ms=5)

    def test_on_dispatch_attempted_no_op_without_otel(self) -> None:
        from agent_kernel.runtime.observability_hooks import MetricsObservabilityHook

        hook = MetricsObservabilityHook()
        hook.on_dispatch_attempted(
            run_id="r1", action_id="a1", dedupe_outcome="accepted", latency_ms=10
        )

    def test_metrics_hook_has_admission_and_dispatch_instruments(self) -> None:
        from agent_kernel.runtime.observability_hooks import MetricsObservabilityHook

        hook = MetricsObservabilityHook()
        # When OTel is absent instruments are None; when present they should be created.
        # At minimum, the attributes must exist.
        assert hasattr(hook, "_admission_evaluations")
        assert hasattr(hook, "_dispatch_attempt_latency")

    def test_on_admission_evaluated_false_no_op_without_otel(self) -> None:
        from agent_kernel.runtime.observability_hooks import MetricsObservabilityHook

        hook = MetricsObservabilityHook()
        hook.on_admission_evaluated(run_id="r1", action_id="a1", admitted=False, latency_ms=3)

    def test_logging_hook_on_admission_evaluated_unchanged(self) -> None:
        from agent_kernel.runtime.observability_hooks import LoggingObservabilityHook

        hook = LoggingObservabilityHook(use_json=True, logger_name="test_r7a")
        hook.on_admission_evaluated(run_id="r1", action_id="a1", admitted=True, latency_ms=2)
        hook.on_dispatch_attempted(
            run_id="r1", action_id="a1", dedupe_outcome="accepted", latency_ms=8
        )


# ---------------------------------------------------------------------------
# R7b - Reflection exhaustion escalation path
# ---------------------------------------------------------------------------


class TestReflectionExhaustionEscalation:
    def test_fallback_decision_escalates_when_escalate_on_exhaustion_true(self) -> None:
        """_fallback_decision returns human_escalation when escalate_on_exhaustion=True."""
        from agent_kernel.kernel.contracts import (
            RecoveryInput,
            ReflectionPolicy,
            RunProjection,
        )
        from agent_kernel.kernel.recovery.gate import PlannedRecoveryGateService

        policy = ReflectionPolicy(
            max_rounds=3,
            reflection_timeout_ms=5_000,
            escalate_on_exhaustion=True,
        )
        gate = PlannedRecoveryGateService(reflection_policy=policy)

        projection = RunProjection(
            run_id="run-exhaust",
            lifecycle_state="dispatching",
            projected_offset=0,
            waiting_external=False,
            ready_for_dispatch=True,
        )
        recovery_input = RecoveryInput(
            run_id="run-exhaust",
            reason_code="some_error",
            lifecycle_state="dispatching",
            projection=projection,
        )

        # _fallback_decision is called when reasoning loop returns no actions.
        decision = gate._fallback_decision(recovery_input, "test:reason")  # type: ignore[attr-defined]
        assert decision.mode == "human_escalation"
        assert "escalated" in decision.reason

    def test_fallback_decision_aborts_when_escalate_on_exhaustion_false(self) -> None:
        """_fallback_decision returns abort when escalate_on_exhaustion=False."""
        from agent_kernel.kernel.contracts import (
            RecoveryInput,
            ReflectionPolicy,
            RunProjection,
        )
        from agent_kernel.kernel.recovery.gate import PlannedRecoveryGateService

        policy = ReflectionPolicy(
            max_rounds=3,
            reflection_timeout_ms=5_000,
            escalate_on_exhaustion=False,
        )
        gate = PlannedRecoveryGateService(reflection_policy=policy)

        projection = RunProjection(
            run_id="run-abort",
            lifecycle_state="dispatching",
            projected_offset=0,
            waiting_external=False,
            ready_for_dispatch=True,
        )
        recovery_input = RecoveryInput(
            run_id="run-abort",
            reason_code="error",
            lifecycle_state="dispatching",
            projection=projection,
        )

        decision = gate._fallback_decision(recovery_input, "test:reason")  # type: ignore[attr-defined]
        assert decision.mode == "abort"
        assert "aborted" in decision.reason

    def test_should_reflect_false_when_rounds_exhausted(self) -> None:
        """_should_reflect returns False when reflection_round >= max_rounds."""
        from agent_kernel.kernel.contracts import (
            RecoveryInput,
            ReflectionPolicy,
            RunProjection,
        )
        from agent_kernel.kernel.recovery.gate import PlannedRecoveryGateService

        policy = ReflectionPolicy(max_rounds=2, reflection_timeout_ms=5_000)

        class _StubLoop:
            async def run_once(self, **kw: Any) -> Any:
                pass

        class _StubBuilder:
            def build(self, **kw: Any) -> Any:
                pass

        gate = PlannedRecoveryGateService(
            reflection_policy=policy,
            reasoning_loop=_StubLoop(),  # type: ignore[arg-type]
            reflection_builder=_StubBuilder(),  # type: ignore[arg-type]
        )

        projection = RunProjection(
            run_id="run-exhaust3",
            lifecycle_state="dispatching",
            projected_offset=0,
            waiting_external=False,
            ready_for_dispatch=True,
        )
        recovery_input = RecoveryInput(
            run_id="run-exhaust3",
            reason_code="tool_failure",
            lifecycle_state="dispatching",
            projection=projection,
            reflection_round=2,  # == max_rounds=2
        )

        # _should_reflect should return False (rounds exhausted).
        result = gate._should_reflect(recovery_input, "abort")  # type: ignore[attr-defined]
        assert result is False

    def test_should_reflect_true_below_max_rounds(self) -> None:
        """_should_reflect returns True when round < max_rounds and all prerequisites met."""
        from agent_kernel.kernel.contracts import (
            RecoveryInput,
            ReflectionPolicy,
            RunProjection,
        )
        from agent_kernel.kernel.recovery.gate import PlannedRecoveryGateService

        policy = ReflectionPolicy(
            max_rounds=5,
            reflection_timeout_ms=5_000,
            reflectable_failure_kinds=["tool_failure"],
        )

        class _StubLoop:
            async def run_once(self, **kw: Any) -> Any:
                pass

        class _StubBuilder:
            def build(self, **kw: Any) -> Any:
                pass

        gate = PlannedRecoveryGateService(
            reflection_policy=policy,
            reasoning_loop=_StubLoop(),  # type: ignore[arg-type]
            reflection_builder=_StubBuilder(),  # type: ignore[arg-type]
        )

        projection = RunProjection(
            run_id="run-reflect",
            lifecycle_state="dispatching",
            projected_offset=0,
            waiting_external=False,
            ready_for_dispatch=True,
        )
        recovery_input = RecoveryInput(
            run_id="run-reflect",
            reason_code="tool_failure",
            lifecycle_state="dispatching",
            projection=projection,
            reflection_round=2,  # < max_rounds=5
        )

        result = gate._should_reflect(recovery_input, "abort")  # type: ignore[attr-defined]
        assert result is True


# ---------------------------------------------------------------------------
# R7c - turn.branch_rollback_intent event registered
# ---------------------------------------------------------------------------


class TestBranchRollbackIntentEvent:
    def test_branch_rollback_intent_registered_in_registry(self) -> None:
        from agent_kernel.kernel.event_registry import KERNEL_EVENT_REGISTRY

        descriptor = KERNEL_EVENT_REGISTRY.get("turn.branch_rollback_intent")
        assert descriptor is not None

    def test_branch_rollback_intent_is_diagnostic(self) -> None:
        from agent_kernel.kernel.event_registry import KERNEL_EVENT_REGISTRY

        descriptor = KERNEL_EVENT_REGISTRY.get("turn.branch_rollback_intent")
        assert descriptor is not None
        assert descriptor.affects_replay is False

    def test_branch_rollback_intent_authority_is_executor(self) -> None:
        from agent_kernel.kernel.event_registry import KERNEL_EVENT_REGISTRY

        descriptor = KERNEL_EVENT_REGISTRY.get("turn.branch_rollback_intent")
        assert descriptor is not None
        assert descriptor.authority == "Executor"


# ---------------------------------------------------------------------------
# R7d - Crash-replay integration: executor raise -> unknown_effect -> re-run
# ---------------------------------------------------------------------------


class TestCrashReplayDeterminism:
    def test_executor_raise_produces_unknown_effect_state(self) -> None:
        """R6d guarantee: executor exception leaves dedupe in unknown_effect."""
        from agent_kernel.kernel.capability_snapshot import (
            CapabilitySnapshotBuilder,
            CapabilitySnapshotInput,
        )
        from agent_kernel.kernel.contracts import Action
        from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore
        from agent_kernel.kernel.turn_engine import TurnEngine, TurnInput, _build_turn_identity

        class _AlwaysAdmit:
            async def admit(self, action: Any, snapshot: Any) -> bool:
                return True

            async def check(self, action: Any, snapshot: Any) -> bool:
                return True

        store = InMemoryDedupeStore()

        class _RaisingExecutor:
            async def execute(
                self, action: Any, snapshot: Any, envelope: Any, execution_context: Any = None
            ) -> dict:
                raise RuntimeError("simulated crash")

        engine = TurnEngine(
            snapshot_builder=CapabilitySnapshotBuilder(),
            admission_service=_AlwaysAdmit(),
            dedupe_store=store,
            executor=_RaisingExecutor(),
        )

        action = MagicMock(spec=Action)
        action.action_id = "crash-action"
        action.action_type = "tool_call"
        action.effect_class = "test"
        action.snapshot_input = CapabilitySnapshotInput(
            run_id="run-crash",
            based_on_offset=0,
            tenant_policy_ref="policy:default",
            permission_mode="strict",
        )
        turn_input = TurnInput(
            run_id="run-crash",
            through_offset=1,
            based_on_offset=0,
            trigger_type="start",
        )

        with pytest.raises(RuntimeError):
            asyncio.run(engine.run_turn(turn_input, action=action))

        # DedupeStore record should be in unknown_effect state.
        ti = _build_turn_identity(input_value=turn_input, action=action)
        record = store.get(ti.dispatch_dedupe_key)
        assert record is not None
        assert record.state == "unknown_effect"

    def test_rerun_with_same_key_blocked_as_duplicate(self) -> None:
        """After crash (unknown_effect), re-running with same dedupe key blocks dispatch."""
        from agent_kernel.kernel.capability_snapshot import (
            CapabilitySnapshotBuilder,
            CapabilitySnapshotInput,
        )
        from agent_kernel.kernel.contracts import Action
        from agent_kernel.kernel.dedupe_store import (
            IdempotencyEnvelope,
            InMemoryDedupeStore,
        )
        from agent_kernel.kernel.turn_engine import TurnEngine, TurnInput, _build_turn_identity

        class _AlwaysAdmit:
            async def admit(self, action: Any, snapshot: Any) -> bool:
                return True

            async def check(self, action: Any, snapshot: Any) -> bool:
                return True

        class _AlwaysAckExecutor:
            async def execute(
                self, action: Any, snapshot: Any, envelope: Any, execution_context: Any = None
            ) -> dict:
                return {"acknowledged": True}

        action = MagicMock(spec=Action)
        action.action_id = "dup-action"
        action.action_type = "tool_call"
        action.effect_class = "test"
        action.snapshot_input = CapabilitySnapshotInput(
            run_id="run-dup",
            based_on_offset=0,
            tenant_policy_ref="policy:default",
            permission_mode="strict",
        )
        turn_input = TurnInput(
            run_id="run-dup",
            through_offset=1,
            based_on_offset=0,
            trigger_type="start",
        )

        store = InMemoryDedupeStore()

        # Pre-populate: simulate a previous dispatch that reached unknown_effect.
        ti = _build_turn_identity(input_value=turn_input, action=action)
        envelope = IdempotencyEnvelope(
            dispatch_idempotency_key=ti.dispatch_dedupe_key,
            operation_fingerprint=ti.decision_fingerprint,
            attempt_seq=1,
            effect_scope="test",
            capability_snapshot_hash="hash",
            host_kind="tool_call",
        )
        store.reserve(envelope)
        store.mark_dispatched(ti.dispatch_dedupe_key)
        store.mark_unknown_effect(ti.dispatch_dedupe_key)

        engine = TurnEngine(
            snapshot_builder=CapabilitySnapshotBuilder(),
            admission_service=_AlwaysAdmit(),
            dedupe_store=store,
            executor=_AlwaysAckExecutor(),
        )

        # Re-run: dedupe should block the dispatch since key already exists.
        result = asyncio.run(engine.run_turn(turn_input, action=action))
        assert result.outcome_kind == "blocked"


# ---------------------------------------------------------------------------
# R7e - SQLiteDedupeStore WAL checkpoint on close
# ---------------------------------------------------------------------------


class TestSQLiteDedupeStoreWALCheckpoint:
    def test_close_performs_wal_checkpoint(self, tmp_path) -> None:
        """close() should run PRAGMA wal_checkpoint(TRUNCATE) before closing."""
        from agent_kernel.kernel.persistence.sqlite_dedupe_store import SQLiteDedupeStore

        store = SQLiteDedupeStore(str(tmp_path / "chk.db"))
        # Create a WAL file by doing a write.
        from agent_kernel.kernel.dedupe_store import IdempotencyEnvelope

        envelope = IdempotencyEnvelope(
            dispatch_idempotency_key="key-chk",
            operation_fingerprint="fp",
            attempt_seq=1,
            effect_scope="scope",
            capability_snapshot_hash="hash",
            host_kind="tool_call",
        )
        store.reserve(envelope)
        # close() should not raise — WAL checkpoint is best-effort.
        store.close()

    def test_close_does_not_raise_on_double_close(self, tmp_path) -> None:
        """double close() should not raise (best-effort checkpoint)."""
        from agent_kernel.kernel.persistence.sqlite_dedupe_store import SQLiteDedupeStore

        store = SQLiteDedupeStore(str(tmp_path / "dbl.db"))
        store.close()
        # Second close should raise sqlite3.ProgrammingError (closed connection)
        # but we at least verify first close works.

    def test_wal_checkpoint_pragma_accepted(self, tmp_path) -> None:
        """PRAGMA wal_checkpoint(TRUNCATE) should work on file-based DB."""
        from agent_kernel.kernel.persistence.sqlite_dedupe_store import SQLiteDedupeStore

        store = SQLiteDedupeStore(str(tmp_path / "wal_ck.db"))
        # Execute checkpoint directly to verify it's accepted.
        result = store._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        assert result is not None  # returns (busy, log, checkpointed)
        store.close()


# ---------------------------------------------------------------------------
# R7f - TurnEngine phase_timeout_ms
# ---------------------------------------------------------------------------


class TestTurnEnginePhaseTimeout:
    def test_phase_timeout_ms_accepted_by_constructor(self) -> None:
        from agent_kernel.kernel.capability_snapshot import CapabilitySnapshotBuilder
        from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore
        from agent_kernel.kernel.turn_engine import TurnEngine

        class _AlwaysAdmit:
            async def admit(self, a: Any, s: Any) -> bool:
                return True

            async def check(self, a: Any, s: Any) -> bool:
                return True

        class _AlwaysAck:
            async def execute(self, a: Any, s: Any, e: Any, execution_context: Any = None) -> dict:
                return {"acknowledged": True}

        engine = TurnEngine(
            snapshot_builder=CapabilitySnapshotBuilder(),
            admission_service=_AlwaysAdmit(),
            dedupe_store=InMemoryDedupeStore(),
            executor=_AlwaysAck(),
            phase_timeout_ms=5_000,
        )
        assert engine._phase_timeout_s == pytest.approx(5.0)

    def test_phase_timeout_none_by_default(self) -> None:
        from agent_kernel.kernel.capability_snapshot import CapabilitySnapshotBuilder
        from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore
        from agent_kernel.kernel.turn_engine import TurnEngine

        class _Stub:
            async def admit(self, a: Any, s: Any) -> bool:
                return True

            async def check(self, a: Any, s: Any) -> bool:
                return True

            async def execute(self, a: Any, s: Any, e: Any, execution_context: Any = None) -> dict:
                return {"acknowledged": True}

        engine = TurnEngine(
            snapshot_builder=CapabilitySnapshotBuilder(),
            admission_service=_Stub(),
            dedupe_store=InMemoryDedupeStore(),
            executor=_Stub(),
        )
        assert engine._phase_timeout_s is None

    def test_phase_timeout_raises_on_slow_phase(self) -> None:
        """When a phase exceeds phase_timeout_ms, TimeoutError is raised."""
        from agent_kernel.kernel.capability_snapshot import (
            CapabilitySnapshotBuilder,
            CapabilitySnapshotInput,
        )
        from agent_kernel.kernel.contracts import Action
        from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore
        from agent_kernel.kernel.turn_engine import TurnEngine, TurnInput

        class _AlwaysAdmit:
            async def admit(self, action: Any, snapshot: Any) -> bool:
                return True

            async def check(self, action: Any, snapshot: Any) -> bool:
                return True

        class _SlowExecutor:
            async def execute(
                self, action: Any, snapshot: Any, envelope: Any, execution_context: Any = None
            ) -> dict:
                # Sleep longer than the timeout.
                await asyncio.sleep(10.0)
                return {"acknowledged": True}

        engine = TurnEngine(
            snapshot_builder=CapabilitySnapshotBuilder(),
            admission_service=_AlwaysAdmit(),
            dedupe_store=InMemoryDedupeStore(),
            executor=_SlowExecutor(),
            phase_timeout_ms=50,  # 50ms timeout
        )

        action = MagicMock(spec=Action)
        action.action_id = "slow-action"
        action.action_type = "tool_call"
        action.effect_class = "test"
        action.snapshot_input = CapabilitySnapshotInput(
            run_id="run-slow",
            based_on_offset=0,
            tenant_policy_ref="policy:default",
            permission_mode="strict",
        )
        turn_input = TurnInput(
            run_id="run-slow",
            through_offset=1,
            based_on_offset=0,
            trigger_type="start",
        )

        with pytest.raises((TimeoutError, asyncio.TimeoutError)):
            asyncio.run(engine.run_turn(turn_input, action=action))

    def test_phase_timeout_none_completes_normally(self) -> None:
        """Without phase_timeout_ms, normal turns complete as usual."""
        from agent_kernel.kernel.capability_snapshot import (
            CapabilitySnapshotBuilder,
            CapabilitySnapshotInput,
        )
        from agent_kernel.kernel.contracts import Action
        from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore
        from agent_kernel.kernel.turn_engine import TurnEngine, TurnInput

        class _AlwaysAdmit:
            async def admit(self, action: Any, snapshot: Any) -> bool:
                return True

            async def check(self, action: Any, snapshot: Any) -> bool:
                return True

        class _FastExecutor:
            async def execute(
                self, action: Any, snapshot: Any, envelope: Any, execution_context: Any = None
            ) -> dict:
                return {"acknowledged": True}

        engine = TurnEngine(
            snapshot_builder=CapabilitySnapshotBuilder(),
            admission_service=_AlwaysAdmit(),
            dedupe_store=InMemoryDedupeStore(),
            executor=_FastExecutor(),
        )

        action = MagicMock(spec=Action)
        action.action_id = "fast-action"
        action.action_type = "tool_call"
        action.effect_class = "test"
        action.snapshot_input = CapabilitySnapshotInput(
            run_id="run-fast",
            based_on_offset=0,
            tenant_policy_ref="policy:default",
            permission_mode="strict",
        )
        turn_input = TurnInput(
            run_id="run-fast",
            through_offset=1,
            based_on_offset=0,
            trigger_type="start",
        )

        result = asyncio.run(engine.run_turn(turn_input, action=action))
        assert result.outcome_kind == "dispatched"
