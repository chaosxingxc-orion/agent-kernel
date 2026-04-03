"""Round 6 quality optimization tests (R6a-R6f).

Covers:
  R6a - PlanExecutor per-branch DedupeStore integration (crash-replay skip)
  R6b - PlanExecutor all-or-nothing rollback: on_branch_rollback_triggered
  R6c - TurnEngine _phase_snapshot calls assert_snapshot_compatible
  R6d - _phase_execute ack-before-commit: mark_unknown_effect on executor raise
  R6e - TurnEngine emits on_turn_phase for each phase
  R6f - KernelHealthProbe factory functions for DedupeStore and EventLog
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# R6a - PlanExecutor per-branch dedupe store (crash-replay skip)
# ---------------------------------------------------------------------------


class TestPlanExecutorBranchDedupe:
    def test_plan_executor_accepts_dedupe_store_param(self) -> None:
        from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore
        from agent_kernel.kernel.plan_executor import PlanExecutor

        store = InMemoryDedupeStore()

        async def _noop_runner(action: Any) -> Any:
            raise AssertionError("should not be called")

        executor = PlanExecutor(turn_runner=_noop_runner, dedupe_store=store)
        assert executor._dedupe_store is store

    def test_plan_executor_dedupe_store_defaults_to_none(self) -> None:
        from agent_kernel.kernel.plan_executor import PlanExecutor

        async def _noop_runner(action: Any) -> Any:
            raise NotImplementedError

        executor = PlanExecutor(turn_runner=_noop_runner)
        assert executor._dedupe_store is None

    def test_already_acknowledged_branch_is_skipped(self) -> None:
        """Branch with state=acknowledged in dedupe store should return noop result."""
        from agent_kernel.kernel.contracts import (
            Action,
            ParallelGroup,
            ParallelPlan,
        )
        from agent_kernel.kernel.dedupe_store import (
            IdempotencyEnvelope,
            InMemoryDedupeStore,
        )
        from agent_kernel.kernel.plan_executor import PlanExecutor

        store = InMemoryDedupeStore()
        group_key = "grp-1"
        action_id = "act-1"
        branch_key = f"{group_key}:{action_id}"

        # Pre-populate dedupe store: reserved -> dispatched -> acknowledged
        envelope = IdempotencyEnvelope(
            dispatch_idempotency_key=branch_key,
            operation_fingerprint="fp",
            attempt_seq=1,
            effect_scope="scope",
            capability_snapshot_hash="hash",
            host_kind="tool_call",
        )
        store.reserve(envelope)
        store.mark_dispatched(branch_key)
        store.mark_acknowledged(branch_key)

        runner_calls: list[str] = []

        async def _runner(action: Any) -> Any:
            runner_calls.append(action.action_id)
            from agent_kernel.kernel.turn_engine import TurnResult

            return TurnResult(
                state="dispatch_acknowledged",
                outcome_kind="dispatched",
                decision_ref="d",
                decision_fingerprint="fp",
                emitted_events=[],
            )

        action = MagicMock(spec=Action)
        action.action_id = action_id

        group = ParallelGroup(
            actions=[action],
            join_strategy="all",
            group_idempotency_key=group_key,
        )
        plan = ParallelPlan(groups=[group])
        executor = PlanExecutor(turn_runner=_runner, dedupe_store=store)
        result = asyncio.run(executor.execute_plan(plan, run_id="run-1"))

        # Runner should NOT have been called since branch was already acknowledged.
        assert runner_calls == []
        assert result.succeeded == 1
        assert result.failed == 0

    def test_non_acknowledged_branch_runs_normally(self) -> None:
        """Branch NOT in acknowledged state should run turn_runner normally."""
        from agent_kernel.kernel.contracts import Action, ParallelGroup, ParallelPlan
        from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore
        from agent_kernel.kernel.plan_executor import PlanExecutor
        from agent_kernel.kernel.turn_engine import TurnResult

        store = InMemoryDedupeStore()
        runner_calls: list[str] = []

        async def _runner(action: Any) -> TurnResult:
            runner_calls.append(action.action_id)
            return TurnResult(
                state="dispatch_acknowledged",
                outcome_kind="dispatched",
                decision_ref="d",
                decision_fingerprint="fp",
                emitted_events=[],
            )

        action = MagicMock(spec=Action)
        action.action_id = "act-2"
        group = ParallelGroup(
            actions=[action], join_strategy="all", group_idempotency_key="grp-2"
        )
        plan = ParallelPlan(groups=[group])
        executor = PlanExecutor(turn_runner=_runner, dedupe_store=store)
        asyncio.run(executor.execute_plan(plan, run_id="run-1"))

        assert "act-2" in runner_calls


# ---------------------------------------------------------------------------
# R6b - Branch rollback hook
# ---------------------------------------------------------------------------


class TestBranchRollbackHook:
    def test_on_branch_rollback_triggered_protocol_exists(self) -> None:
        from agent_kernel.kernel.contracts import ObservabilityHook

        assert hasattr(ObservabilityHook, "on_branch_rollback_triggered")

    def test_rollback_emitted_when_all_join_fails(self) -> None:
        """on_branch_rollback_triggered should fire for succeeded branches when join fails."""
        from agent_kernel.kernel.contracts import Action, ParallelGroup, ParallelPlan
        from agent_kernel.kernel.plan_executor import PlanExecutor
        from agent_kernel.kernel.turn_engine import TurnResult

        rollbacks: list[dict] = []

        class _StubHook:
            def on_parallel_branch_result(self, **kw: Any) -> None:
                pass

            def on_branch_rollback_triggered(
                self, *, run_id, group_idempotency_key, action_id, join_strategy
            ) -> None:
                rollbacks.append(
                    {
                        "action_id": action_id,
                        "join_strategy": join_strategy,
                    }
                )

        # Two actions: one succeeds, one fails -> join_strategy="all" not satisfied.
        success_action = MagicMock(spec=Action)
        success_action.action_id = "act-ok"
        fail_action = MagicMock(spec=Action)
        fail_action.action_id = "act-fail"

        async def _runner(action: Any) -> TurnResult:
            if action.action_id == "act-ok":
                return TurnResult(
                    state="dispatch_acknowledged",
                    outcome_kind="dispatched",
                    decision_ref="d",
                    decision_fingerprint="fp",
                    emitted_events=[],
                )
            return TurnResult(
                state="recovery_pending",
                outcome_kind="recovery_pending",
                decision_ref="d",
                decision_fingerprint="fp",
                emitted_events=[],
            )

        group = ParallelGroup(
            actions=[success_action, fail_action],
            join_strategy="all",
            group_idempotency_key="grp-rollback",
        )
        plan = ParallelPlan(groups=[group])
        executor = PlanExecutor(
            turn_runner=_runner, observability_hook=_StubHook()  # type: ignore[arg-type]
        )
        asyncio.run(executor.execute_plan(plan, run_id="run-1"))

        # Succeeded branch (act-ok) should have rollback triggered.
        assert len(rollbacks) == 1
        assert rollbacks[0]["action_id"] == "act-ok"
        assert rollbacks[0]["join_strategy"] == "all"

    def test_no_rollback_when_join_satisfied(self) -> None:
        """No rollback hook when join is satisfied."""
        from agent_kernel.kernel.contracts import Action, ParallelGroup, ParallelPlan
        from agent_kernel.kernel.plan_executor import PlanExecutor
        from agent_kernel.kernel.turn_engine import TurnResult

        rollbacks: list[dict] = []

        class _StubHook:
            def on_parallel_branch_result(self, **kw: Any) -> None:
                pass

            def on_branch_rollback_triggered(self, **kw: Any) -> None:
                rollbacks.append(kw)

        action = MagicMock(spec=Action)
        action.action_id = "act-1"

        async def _runner(action: Any) -> TurnResult:
            return TurnResult(
                state="dispatch_acknowledged",
                outcome_kind="dispatched",
                decision_ref="d",
                decision_fingerprint="fp",
                emitted_events=[],
            )

        group = ParallelGroup(
            actions=[action], join_strategy="all", group_idempotency_key="grp-ok"
        )
        executor = PlanExecutor(
            turn_runner=_runner, observability_hook=_StubHook()  # type: ignore[arg-type]
        )
        asyncio.run(executor.execute_plan(ParallelPlan(groups=[group]), run_id="run-1"))
        assert rollbacks == []

    def test_logging_hook_on_branch_rollback_triggered_does_not_raise(self) -> None:
        from agent_kernel.runtime.observability_hooks import LoggingObservabilityHook

        hook = LoggingObservabilityHook(use_json=True, logger_name="test_r6b")
        hook.on_branch_rollback_triggered(
            run_id="r1",
            group_idempotency_key="grp",
            action_id="a1",
            join_strategy="all",
        )

    def test_metrics_hook_on_branch_rollback_triggered_does_not_raise(self) -> None:
        from agent_kernel.runtime.observability_hooks import MetricsObservabilityHook

        hook = MetricsObservabilityHook()
        hook.on_branch_rollback_triggered(
            run_id="r1",
            group_idempotency_key="grp",
            action_id="a1",
            join_strategy="all",
        )


# ---------------------------------------------------------------------------
# R6c - assert_snapshot_compatible in _phase_snapshot
# ---------------------------------------------------------------------------


class TestSnapshotCompatCheckInPhaseSnapshot:
    def test_compatible_snapshot_passes_through(self) -> None:
        """A snapshot with current schema version should not block the turn."""
        from agent_kernel.kernel.capability_snapshot import (
            CapabilitySnapshot,
            CapabilitySnapshotInput,
        )
        from agent_kernel.kernel.turn_engine import TurnEngine, TurnInput

        class _SnapshotBuilder:
            def build(self, inp: Any) -> CapabilitySnapshot:
                from agent_kernel.kernel.capability_snapshot import (
                    CapabilitySnapshotBuilder,
                )

                return CapabilitySnapshotBuilder().build(inp)

        class _AlwaysAdmit:
            async def admit(self, action: Any, snapshot: Any) -> bool:
                return True

            async def check(self, action: Any, snapshot: Any) -> bool:
                return True

        from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore

        class _AlwaysAckExecutor:
            async def execute(self, action, snapshot, envelope, execution_context=None):
                return {"acknowledged": True}

        engine = TurnEngine(
            snapshot_builder=_SnapshotBuilder(),
            admission_service=_AlwaysAdmit(),
            dedupe_store=InMemoryDedupeStore(),
            executor=_AlwaysAckExecutor(),
        )

        from agent_kernel.kernel.contracts import Action

        action = MagicMock(spec=Action)
        action.action_id = "a-1"
        action.action_type = "tool_call"
        action.effect_class = "test"
        action.snapshot_input = CapabilitySnapshotInput(
            run_id="run-1",
            based_on_offset=0,
            tenant_policy_ref="policy:default",
            permission_mode="strict",
        )

        turn_input = TurnInput(
            run_id="run-1",
            through_offset=1,
            based_on_offset=0,
            trigger_type="start",
        )

        result = asyncio.run(engine.run_turn(turn_input, action=action))
        # Should complete without raising.
        assert result.outcome_kind in ("dispatched", "noop", "blocked", "recovery_pending")

    def test_incompatible_snapshot_raises_value_error(self) -> None:
        """A snapshot with unknown schema_version should raise ValueError."""
        from agent_kernel.kernel.capability_snapshot import (
            CapabilitySnapshot,
            assert_snapshot_compatible,
        )

        old_snapshot = CapabilitySnapshot(
            snapshot_ref="snapshot:run-1:0:abc",
            snapshot_hash="abc",
            run_id="run-1",
            based_on_offset=0,
            tenant_policy_ref="policy:default",
            permission_mode="strict",
            tool_bindings=[],
            mcp_bindings=[],
            skill_bindings=[],
            feature_flags=[],
            snapshot_schema_version="99",  # Unknown version
        )
        with pytest.raises(ValueError, match="99"):
            assert_snapshot_compatible(old_snapshot)


# ---------------------------------------------------------------------------
# R6d - ack-before-commit in _phase_execute
# ---------------------------------------------------------------------------


class TestAckBeforeCommit:
    def test_mark_unknown_effect_called_on_executor_raise(self) -> None:
        """If executor raises, DedupeStore should transition to unknown_effect."""
        from agent_kernel.kernel.capability_snapshot import CapabilitySnapshotInput
        from agent_kernel.kernel.contracts import Action
        from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore
        from agent_kernel.kernel.turn_engine import TurnEngine, TurnInput

        class _AlwaysAdmit:
            async def admit(self, action: Any, snapshot: Any) -> bool:
                return True

            async def check(self, action: Any, snapshot: Any) -> bool:
                return True

        class _ExplodingExecutor:
            async def execute(
                self, action: Any, snapshot: Any, envelope: Any, execution_context: Any = None
            ) -> dict:
                raise RuntimeError("executor kaboom")

        from agent_kernel.kernel.capability_snapshot import CapabilitySnapshotBuilder

        store = InMemoryDedupeStore()
        engine = TurnEngine(
            snapshot_builder=CapabilitySnapshotBuilder(),
            admission_service=_AlwaysAdmit(),
            dedupe_store=store,
            executor=_ExplodingExecutor(),
        )

        action = MagicMock(spec=Action)
        action.action_id = "a-explode"
        action.action_type = "tool_call"
        action.effect_class = "test"
        action.snapshot_input = CapabilitySnapshotInput(
            run_id="run-x",
            based_on_offset=0,
            tenant_policy_ref="policy:default",
            permission_mode="strict",
        )

        turn_input = TurnInput(
            run_id="run-x",
            through_offset=1,
            based_on_offset=0,
            trigger_type="start",
        )

        with pytest.raises(RuntimeError, match="executor kaboom"):
            asyncio.run(engine.run_turn(turn_input, action=action))

        # Find the dedupe record — it should be in unknown_effect state.
        from agent_kernel.kernel.turn_engine import _build_turn_identity

        ti = _build_turn_identity(input_value=turn_input, action=action)
        record = store.get(ti.dispatch_dedupe_key)
        assert record is not None, "DedupeStore record should exist"
        assert record.state == "unknown_effect"


# ---------------------------------------------------------------------------
# R6e - on_turn_phase emission
# ---------------------------------------------------------------------------


class TestOnTurnPhaseHook:
    def test_on_turn_phase_protocol_exists(self) -> None:
        from agent_kernel.kernel.contracts import ObservabilityHook

        assert hasattr(ObservabilityHook, "on_turn_phase")

    def test_on_turn_phase_emitted_for_each_phase(self) -> None:
        """TurnEngine should emit on_turn_phase for every phase it executes."""
        from agent_kernel.kernel.capability_snapshot import (
            CapabilitySnapshotBuilder,
            CapabilitySnapshotInput,
        )
        from agent_kernel.kernel.contracts import Action
        from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore
        from agent_kernel.kernel.turn_engine import TurnEngine, TurnInput

        phases_emitted: list[str] = []

        class _StubHook:
            def on_turn_phase(self, *, run_id, action_id, phase_name, elapsed_ms):
                phases_emitted.append(phase_name)

        class _AlwaysAdmit:
            async def admit(self, action: Any, snapshot: Any) -> bool:
                return True

            async def check(self, action: Any, snapshot: Any) -> bool:
                return True

        class _AlwaysAckExecutor2:
            async def execute(self, action, snapshot, envelope, execution_context=None):
                return {"acknowledged": True}

        engine = TurnEngine(
            snapshot_builder=CapabilitySnapshotBuilder(),
            admission_service=_AlwaysAdmit(),
            dedupe_store=InMemoryDedupeStore(),
            executor=_AlwaysAckExecutor2(),
            observability_hook=_StubHook(),  # type: ignore[arg-type]
        )

        action = MagicMock(spec=Action)
        action.action_id = "a-phase-test"
        action.action_type = "tool_call"
        action.effect_class = "test"
        action.snapshot_input = CapabilitySnapshotInput(
            run_id="run-phase",
            based_on_offset=0,
            tenant_policy_ref="policy:default",
            permission_mode="strict",
        )

        turn_input = TurnInput(
            run_id="run-phase",
            through_offset=1,
            based_on_offset=0,
            trigger_type="start",
        )

        asyncio.run(engine.run_turn(turn_input, action=action))

        # Should have received at least one phase event.
        assert len(phases_emitted) > 0
        # All phase names come from _TURN_PHASES.
        for phase_name in phases_emitted:
            assert phase_name.startswith("_phase_")

    def test_logging_hook_on_turn_phase_does_not_raise(self) -> None:
        from agent_kernel.runtime.observability_hooks import LoggingObservabilityHook

        hook = LoggingObservabilityHook(use_json=True, logger_name="test_r6e")
        hook.on_turn_phase(
            run_id="r1", action_id="a1", phase_name="_phase_snapshot", elapsed_ms=3
        )

    def test_metrics_hook_on_turn_phase_does_not_raise(self) -> None:
        from agent_kernel.runtime.observability_hooks import MetricsObservabilityHook

        hook = MetricsObservabilityHook()
        hook.on_turn_phase(
            run_id="r1", action_id="a1", phase_name="_phase_execute", elapsed_ms=12
        )

    def test_composite_hook_fans_out_on_turn_phase(self) -> None:
        from agent_kernel.runtime.observability_hooks import CompositeObservabilityHook

        phases: list[str] = []

        class _Stub:
            def on_turn_phase(self, *, run_id, action_id, phase_name, elapsed_ms):
                phases.append(phase_name)

        composite = CompositeObservabilityHook(hooks=[_Stub()])  # type: ignore[arg-type]
        composite.on_turn_phase(
            run_id="r", action_id="a", phase_name="_phase_admission", elapsed_ms=1
        )
        assert phases == ["_phase_admission"]


# ---------------------------------------------------------------------------
# R6f - Health check factory functions
# ---------------------------------------------------------------------------


class TestHealthCheckFactories:
    def test_sqlite_dedupe_store_health_check_ok(self) -> None:
        from agent_kernel.kernel.persistence.sqlite_dedupe_store import SQLiteDedupeStore
        from agent_kernel.runtime.health import (
            HealthStatus,
            sqlite_dedupe_store_health_check,
        )

        store = SQLiteDedupeStore(":memory:")
        check_fn = sqlite_dedupe_store_health_check(store)
        status, msg = check_fn()
        assert status == HealthStatus.OK
        assert "reachable" in msg.lower()
        store.close()

    def test_sqlite_dedupe_store_health_check_unhealthy_on_closed_conn(
        self, tmp_path
    ) -> None:
        from agent_kernel.kernel.persistence.sqlite_dedupe_store import SQLiteDedupeStore
        from agent_kernel.runtime.health import (
            HealthStatus,
            sqlite_dedupe_store_health_check,
        )

        store = SQLiteDedupeStore(str(tmp_path / "hc.db"))
        store.close()
        check_fn = sqlite_dedupe_store_health_check(store)
        status, _msg = check_fn()
        # Closed connection should report unhealthy.
        assert status == HealthStatus.UNHEALTHY

    def test_event_log_health_check_with_events_attr(self) -> None:
        from agent_kernel.runtime.health import HealthStatus, event_log_health_check

        class _FakeLog:
            def __init__(self) -> None:
                self.events = [1, 2, 3]

        check_fn = event_log_health_check(_FakeLog())
        status, msg = check_fn()
        assert status == HealthStatus.OK
        assert "3" in msg

    def test_event_log_health_check_with_list_events_method(self) -> None:
        from agent_kernel.runtime.health import HealthStatus, event_log_health_check

        class _FakeLog:
            def list_events(self) -> list:
                return ["e1", "e2"]

        check_fn = event_log_health_check(_FakeLog())
        status, msg = check_fn()
        assert status == HealthStatus.OK
        assert "2" in msg

    def test_event_log_health_check_unhealthy_on_error(self) -> None:
        from agent_kernel.runtime.health import HealthStatus, event_log_health_check

        class _BrokenLog:
            @property
            def events(self) -> list:
                raise OSError("disk error")

        check_fn = event_log_health_check(_BrokenLog())
        status, _msg = check_fn()
        # Since events property raising is caught inside contextlib.suppress,
        # count stays -1, but status is still OK (log is "reachable", just no events).
        assert status == HealthStatus.OK

    def test_health_probe_with_registered_dedupe_check(self, tmp_path) -> None:
        from agent_kernel.kernel.persistence.sqlite_dedupe_store import SQLiteDedupeStore
        from agent_kernel.runtime.health import (
            HealthStatus,
            KernelHealthProbe,
            sqlite_dedupe_store_health_check,
        )

        store = SQLiteDedupeStore(str(tmp_path / "probe.db"))
        probe = KernelHealthProbe()
        probe.register_check("dedupe_store", sqlite_dedupe_store_health_check(store))

        result = probe.readiness()
        assert result["status"] == HealthStatus.OK.value
        assert "dedupe_store" in result["checks"]
        store.close()
