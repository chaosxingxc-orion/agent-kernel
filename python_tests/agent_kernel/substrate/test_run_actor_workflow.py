"""Red tests for the agent_kernel RunActorWorkflow contract."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

import agent_kernel.substrate.temporal.run_actor_workflow as run_actor_workflow_module
from agent_kernel.kernel.contracts import (
    Action,
    ActionCommit,
    FailureEnvelope,
    RecoveryDecision,
    RunProjection,
    RuntimeEvent,
    TurnIntentRecord,
)
from agent_kernel.kernel.minimal_runtime import (
    AsyncExecutorService,
    InMemoryDecisionDeduper,
    InMemoryDecisionProjectionService,
    InMemoryKernelRuntimeEventLog,
    InMemoryRecoveryOutcomeStore,
    StaticDispatchAdmissionService,
    StaticRecoveryGateService,
)
from agent_kernel.kernel.turn_engine import TurnOutcomeKind, TurnResult, TurnState, TurnStateEvent
from agent_kernel.substrate.temporal.run_actor_workflow import (
    ActorSignal,
    RunActorStrictModeConfig,
    RunActorWorkflow,
    RunInput,
)


def _build_capability_snapshot_input(
    *,
    include_declarative_bundle_digest: bool = True,
) -> dict[str, Any]:
    """Builds snapshot input payload used by strict workflow tests."""
    payload: dict[str, Any] = {
        "tenant_policy_ref": "policy:v1",
        "permission_mode": "strict",
    }
    if include_declarative_bundle_digest:
        payload["declarative_bundle_digest"] = {
            "bundle_ref": "bundle:v1",
            "semantics_version": "v1",
            "content_hash": "content-hash-1",
            "compile_hash": "compile-hash-1",
        }
    return payload


def make_commit(
    offset: int,
    *,
    include_capability_snapshot_input: bool = True,
    include_declarative_bundle_digest: bool = True,
) -> ActionCommit:
    """Builds one action commit that should drive one authoritative round.

    Args:
        offset: Commit offset carried by the runtime event.

    Returns:
        One commit object containing exactly one fact event.
    """

    input_json: dict[str, Any] = {"query": "kernel"}
    if include_capability_snapshot_input:
        input_json["capability_snapshot_input"] = _build_capability_snapshot_input(
            include_declarative_bundle_digest=include_declarative_bundle_digest,
        )

    return ActionCommit(
        run_id="run-1",
        commit_id=f"commit-{offset}",
        created_at="2026-03-31T00:00:00Z",
        action=Action(
            action_id="action-1",
            run_id="run-1",
            action_type="web_research",
            effect_class="read_only",
            input_json=input_json,
        ),
        events=[
            RuntimeEvent(
                run_id="run-1",
                event_id=f"evt-{offset}",
                commit_offset=offset,
                event_type="action_committed",
                event_class="fact",
                event_authority="authoritative_fact",
                ordering_key="run-1",
                wake_policy="wake_actor",
                created_at="2026-03-31T00:00:00Z",
            )
        ],
    )


def make_remote_service_commit(
    offset: int,
    *,
    include_declarative_bundle_digest: bool,
    include_verified_remote_contract: bool,
) -> ActionCommit:
    """Builds remote-service guaranteed commit for canonical path tests."""
    input_json: dict[str, Any] = {
        "host_kind": "remote_service",
        "capability_snapshot_input": _build_capability_snapshot_input(
            include_declarative_bundle_digest=include_declarative_bundle_digest,
        ),
    }
    if include_verified_remote_contract:
        input_json["remote_service_idempotency_contract"] = {
            "accepts_dispatch_idempotency_key": True,
            "returns_stable_ack": True,
            "peer_retry_model": "at_least_once",
            "default_retry_policy": "bounded_retry",
        }
    return ActionCommit(
        run_id="run-1",
        commit_id=f"commit-{offset}",
        created_at="2026-03-31T00:00:00Z",
        action=Action(
            action_id=f"action-remote-{offset}",
            run_id="run-1",
            action_type="web_research",
            effect_class="idempotent_write",
            external_idempotency_level="guaranteed",
            input_json=input_json,
        ),
        events=[
            RuntimeEvent(
                run_id="run-1",
                event_id=f"evt-{offset}",
                commit_offset=offset,
                event_type="action_committed",
                event_class="fact",
                event_authority="authoritative_fact",
                ordering_key="run-1",
                wake_policy="wake_actor",
                created_at="2026-03-31T00:00:00Z",
            )
        ],
    )


def make_projection(offset: int) -> RunProjection:
    """Builds a ready projection used by workflow tests.

    Args:
        offset: Projection offset used for readiness checks.

    Returns:
        A projection in ``ready`` state.
    """

    return RunProjection(
        run_id="run-1",
        lifecycle_state="ready",
        projected_offset=offset,
        waiting_external=False,
        ready_for_dispatch=True,
    )


@dataclass(frozen=True, slots=True)
class _AdmissionResultStub:
    """Simple admission result object used by workflow tests."""

    admitted: bool
    reason_code: str
    grant_ref: str | None = None


def make_turn_result(
    state: TurnState,
    outcome_kind: TurnOutcomeKind,
    emitted_states: list[str],
    action_commit: dict[str, Any] | None = None,
    recovery_input: FailureEnvelope | None = None,
) -> TurnResult:
    """Builds one deterministic TurnResult used by workflow tests."""
    return TurnResult(
        state=state,
        outcome_kind=outcome_kind,
        decision_ref="decision:run-1:1",
        decision_fingerprint="run-1:signal:action-1:1",
        dispatch_dedupe_key="run-1:action-1:1",
        intent_commit_ref="intent:action-1:1",
        action_commit=action_commit,
        recovery_input=recovery_input,
        emitted_events=[TurnStateEvent(state=emitted_state) for emitted_state in emitted_states],
    )


def build_workflow(
    turn_engine: Any | None = None,
) -> tuple[
    RunActorWorkflow,
    AsyncMock,
    AsyncMock,
    AsyncMock,
    AsyncMock,
    AsyncMock,
    AsyncMock,
]:
    """Builds a workflow and all mocked dependencies.

    Returns:
        A tuple containing the workflow plus event log, projection,
        admission, executor, recovery, and deduper mocks.
    """

    event_log = AsyncMock()
    projection = AsyncMock()
    admission = AsyncMock()
    # Snapshot-only admission path prefers ``admit``. Keep test expectations
    # stable by aliasing admit to the same mock used by legacy check assertions.
    admission.admit = admission.check
    executor = AsyncMock()
    recovery = AsyncMock()
    deduper = AsyncMock()
    workflow = RunActorWorkflow(
        event_log,
        projection,
        admission,
        executor,
        recovery,
        deduper,
        turn_engine=turn_engine,
    )
    return (
        workflow,
        event_log,
        projection,
        admission,
        executor,
        recovery,
        deduper,
    )


def test_workflow_uses_explicit_offsets_for_catch_up_and_readiness() -> None:
    """Workflow must pass the same commit offset through projection gating."""

    (
        workflow,
        _event_log,
        projection,
        _admission,
        _executor,
        _recovery,
        deduper,
    ) = build_workflow()
    projection.catch_up.return_value = make_projection(7)
    projection.readiness.return_value = True
    deduper.seen.return_value = False

    asyncio.run(workflow.process_action_commit(make_commit(7)))

    projection.catch_up.assert_awaited_once_with("run-1", 7)
    projection.readiness.assert_awaited_once_with("run-1", 7)


def test_workflow_marks_decision_fingerprint_before_execute() -> None:
    """Workflow must dedupe one authoritative round before execution."""

    (
        workflow,
        _event_log,
        projection,
        admission,
        executor,
        _recovery,
        deduper,
    ) = build_workflow()
    projection.catch_up.return_value = make_projection(9)
    projection.readiness.return_value = True
    admission.check.return_value = _AdmissionResultStub(
        admitted=True,
        reason_code="ok",
        grant_ref="grant-1",
    )
    deduper.seen.return_value = False

    asyncio.run(workflow.process_action_commit(make_commit(9)))

    deduper.mark.assert_awaited_once()
    executor.execute.assert_awaited_once()


def test_workflow_persists_dispatched_turn_outcome_without_derived_executor_events() -> None:
    """Workflow should persist dispatched outcome without deriving executor events."""

    (
        workflow,
        event_log,
        projection,
        admission,
        executor,
        _recovery,
        deduper,
    ) = build_workflow()
    projection.catch_up.return_value = make_projection(22)
    projection.readiness.return_value = True
    admission.check.return_value = _AdmissionResultStub(
        admitted=True,
        reason_code="ok",
        grant_ref="grant-1",
    )
    deduper.seen.return_value = False
    derived_event = RuntimeEvent(
        run_id="run-1",
        event_id="evt-derived-1",
        commit_offset=0,
        event_type="diagnostic.trace",
        event_class="derived",
        event_authority="derived_diagnostic",
        ordering_key="run-1",
        wake_policy="projection_only",
        payload_json={"trace": "executor metadata"},
        created_at="2026-03-31T00:00:00Z",
    )
    executor.execute.return_value = {
        "derived_events": [derived_event],
        "acknowledged": True,
    }

    asyncio.run(workflow.process_action_commit(make_commit(22)))

    event_log.append_action_commit.assert_awaited_once()
    appended_turn_commit = event_log.append_action_commit.await_args.args[0]
    assert appended_turn_commit.caused_by == "action-1"
    assert appended_turn_commit.events[0].event_type == "run.dispatching"
    assert appended_turn_commit.events[0].payload_json is not None
    assert appended_turn_commit.events[0].payload_json["outcome_kind"] == "dispatched"
    assert appended_turn_commit.events[0].payload_json["host_kind"] == "local_cli"
    assert (
        appended_turn_commit.events[0].payload_json["emitted_states"][-1] == "dispatch_acknowledged"
    )
    assert "remote_policy_decision" not in appended_turn_commit.events[0].payload_json
    assert "derived_events" not in appended_turn_commit.events[0].payload_json


def test_workflow_persists_blocked_turn_outcome_as_ready_event() -> None:
    """Workflow should persist blocked turn outcomes as ready facts."""
    turn_engine = Mock()
    turn_engine.run_turn = AsyncMock(
        return_value=make_turn_result(
            state="dispatch_blocked",
            outcome_kind="blocked",
            emitted_states=[
                "collecting",
                "intent_committed",
                "snapshot_built",
                "admission_checked",
                "dispatch_blocked",
            ],
        )
    )
    (
        workflow,
        event_log,
        projection,
        _admission,
        _executor,
        recovery,
        deduper,
    ) = build_workflow(turn_engine=turn_engine)
    projection.catch_up.return_value = make_projection(23)
    projection.readiness.return_value = True
    deduper.seen.return_value = False

    asyncio.run(workflow.process_action_commit(make_commit(23)))

    event_log.append_action_commit.assert_awaited_once()
    blocked_commit = event_log.append_action_commit.await_args.args[0]
    assert blocked_commit.events[0].event_type == "run.ready"
    assert blocked_commit.events[0].payload_json is not None
    assert blocked_commit.events[0].payload_json["outcome_kind"] == "blocked"
    recovery.decide.assert_not_awaited()


def test_workflow_persists_noop_turn_outcome_as_ready_event() -> None:
    """Workflow should persist noop turn outcomes as ready facts."""
    turn_engine = Mock()
    turn_engine.run_turn = AsyncMock(
        return_value=make_turn_result(
            state="completed_noop",
            outcome_kind="noop",
            emitted_states=[
                "collecting",
                "intent_committed",
                "completed_noop",
            ],
        )
    )
    (
        workflow,
        event_log,
        projection,
        _admission,
        _executor,
        recovery,
        deduper,
    ) = build_workflow(turn_engine=turn_engine)
    projection.catch_up.return_value = make_projection(24)
    projection.readiness.return_value = True
    deduper.seen.return_value = False

    asyncio.run(workflow.process_action_commit(make_commit(24)))

    event_log.append_action_commit.assert_awaited_once()
    noop_commit = event_log.append_action_commit.await_args.args[0]
    assert noop_commit.events[0].event_type == "run.ready"
    assert noop_commit.events[0].payload_json is not None
    assert noop_commit.events[0].payload_json["outcome_kind"] == "noop"
    recovery.decide.assert_not_awaited()


def test_workflow_strict_mode_noops_when_declarative_digest_missing() -> None:
    """Strict workflow should noop when declarative digest input is missing."""
    (
        workflow,
        event_log,
        projection,
        admission,
        executor,
        recovery,
        deduper,
    ) = build_workflow()
    projection.catch_up.return_value = make_projection(25)
    projection.readiness.return_value = True
    deduper.seen.return_value = False

    asyncio.run(
        workflow.process_action_commit(make_commit(25, include_declarative_bundle_digest=False))
    )

    event_log.append_action_commit.assert_awaited_once()
    noop_commit = event_log.append_action_commit.await_args.args[0]
    assert noop_commit.events[0].event_type == "run.ready"
    assert noop_commit.events[0].payload_json is not None
    assert noop_commit.events[0].payload_json["outcome_kind"] == "noop"
    admission.check.assert_not_awaited()
    executor.execute.assert_not_awaited()
    recovery.decide.assert_not_awaited()


def test_workflow_allows_missing_declarative_digest_when_strict_mode_disabled() -> None:
    """Workflow should allow missing declarative digest when strict mode is disabled."""
    (
        workflow,
        event_log,
        projection,
        admission,
        executor,
        recovery,
        deduper,
    ) = build_workflow()
    workflow = RunActorWorkflow(
        event_log=event_log,
        projection=projection,
        admission=admission,
        executor=executor,
        recovery=recovery,
        deduper=deduper,
        strict_mode=RunActorStrictModeConfig(enabled=False),
    )
    projection.catch_up.return_value = make_projection(25)
    projection.readiness.return_value = True
    deduper.seen.return_value = False
    admission.check.return_value = _AdmissionResultStub(
        admitted=True,
        reason_code="ok",
        grant_ref="grant-1",
    )
    executor.execute.return_value = {"acknowledged": True}

    asyncio.run(
        workflow.process_action_commit(make_commit(25, include_declarative_bundle_digest=False))
    )

    event_log.append_action_commit.assert_awaited_once()
    turn_commit = event_log.append_action_commit.await_args.args[0]
    assert turn_commit.events[0].event_type == "run.dispatching"
    assert turn_commit.events[0].payload_json is not None
    assert turn_commit.events[0].payload_json["outcome_kind"] == "dispatched"
    assert (admission.check.await_count + admission.admit.await_count) >= 1
    executor.execute.assert_awaited_once()
    recovery.decide.assert_not_awaited()


@pytest.mark.parametrize(
    (
        "include_declarative_bundle_digest",
        "include_verified_remote_contract",
        "expected_outcome_kind",
        "expected_terminal_state",
        "should_execute",
    ),
    [
        (False, True, "noop", "completed_noop", False),
        (True, False, "blocked", "dispatch_blocked", False),
        (True, True, "dispatched", "dispatch_acknowledged", True),
    ],
)
def test_workflow_canonical_path_combines_remote_policy_and_strict_digest(
    include_declarative_bundle_digest: bool,
    include_verified_remote_contract: bool,
    expected_outcome_kind: str,
    expected_terminal_state: str,
    should_execute: bool,
) -> None:
    """Canonical path should gate remote dispatch and strict digest together."""
    (
        workflow,
        event_log,
        projection,
        admission,
        executor,
        _recovery,
        deduper,
    ) = build_workflow()
    projection.catch_up.return_value = make_projection(26)
    projection.readiness.return_value = True
    deduper.seen.return_value = False
    admission.check.return_value = _AdmissionResultStub(
        admitted=True,
        reason_code="ok",
        grant_ref="grant-1",
    )

    asyncio.run(
        workflow.process_action_commit(
            make_remote_service_commit(
                26,
                include_declarative_bundle_digest=include_declarative_bundle_digest,
                include_verified_remote_contract=include_verified_remote_contract,
            )
        )
    )

    event_log.append_action_commit.assert_awaited_once()
    appended_turn_commit = event_log.append_action_commit.await_args.args[0]
    payload = appended_turn_commit.events[0].payload_json
    assert payload is not None
    assert payload["outcome_kind"] == expected_outcome_kind
    assert payload["emitted_states"][-1] == expected_terminal_state
    if include_declarative_bundle_digest:
        assert payload["host_kind"] == "remote_service"
        assert payload["remote_policy_decision"] is not None
        assert payload["remote_policy_decision"]["effective_idempotency_level"] == (
            "guaranteed" if include_verified_remote_contract else "best_effort"
        )
        assert payload["remote_policy_decision"]["default_retry_policy"] == (
            "bounded_retry" if include_verified_remote_contract else "no_auto_retry"
        )
        assert payload["remote_policy_decision"]["auto_retry_enabled"] == (
            include_verified_remote_contract
        )
        assert payload["remote_policy_decision"]["can_claim_guaranteed"] == (
            include_verified_remote_contract
        )
        assert payload["remote_policy_decision"]["reason"] == (
            "validated_remote_contract"
            if include_verified_remote_contract
            else "missing_remote_idempotency_contract"
        )
    else:
        assert "host_kind" not in payload
        assert "remote_policy_decision" not in payload
    if should_execute:
        executor.execute.assert_awaited_once()
    else:
        executor.execute.assert_not_awaited()


def test_workflow_persists_recovery_pending_before_recovery_gate_event() -> None:
    """Workflow should persist recovering outcome before recovery decision event."""
    turn_engine = Mock()
    turn_engine.run_turn = AsyncMock(
        return_value=make_turn_result(
            state="recovery_pending",
            outcome_kind="recovery_pending",
            emitted_states=[
                "collecting",
                "intent_committed",
                "snapshot_built",
                "admission_checked",
                "dispatched",
                "effect_unknown",
                "recovery_pending",
            ],
            recovery_input=FailureEnvelope(
                run_id="run-1",
                action_id="action-1",
                failed_stage="execution",
                failed_component="executor",
                failure_code="effect_unknown",
                failure_class="unknown",
            ),
        )
    )
    (
        workflow,
        event_log,
        projection,
        _admission,
        _executor,
        recovery,
        deduper,
    ) = build_workflow(turn_engine=turn_engine)
    projection.catch_up.return_value = make_projection(25)
    projection.readiness.return_value = True
    deduper.seen.return_value = False
    recovery.decide.return_value = RecoveryDecision(
        run_id="run-1",
        mode="human_escalation",
        reason="needs_operator",
    )

    asyncio.run(workflow.process_action_commit(make_commit(25)))

    assert event_log.append_action_commit.await_count == 2
    recovering_commit = event_log.append_action_commit.await_args_list[0].args[0]
    recovery_commit = event_log.append_action_commit.await_args_list[1].args[0]
    assert recovering_commit.events[0].event_type == "run.recovering"
    assert recovery_commit.events[0].event_type == "recovery.plan_selected"
    assert recovery_commit.events[0].event_authority == "derived_diagnostic"
    assert recovery_commit.events[1].event_type == "run.waiting_external"
    assert recovery_commit.events[1].commit_offset == 28


def test_workflow_skips_duplicate_decision_fingerprint() -> None:
    """Workflow must not re-dispatch when fingerprint is already seen."""

    (
        workflow,
        _event_log,
        projection,
        _admission,
        executor,
        _recovery,
        deduper,
    ) = build_workflow()
    projection.catch_up.return_value = make_projection(9)
    projection.readiness.return_value = True
    deduper.seen.return_value = True

    asyncio.run(workflow.process_action_commit(make_commit(9)))

    executor.execute.assert_not_awaited()


def test_workflow_routes_execution_failures_to_recovery_gate() -> None:
    """Workflow must hand off execution failures to Recovery."""

    (
        workflow,
        _event_log,
        projection,
        admission,
        executor,
        recovery,
        deduper,
    ) = build_workflow()
    projection.catch_up.return_value = make_projection(12)
    projection.readiness.return_value = True
    admission.check.return_value = _AdmissionResultStub(
        admitted=True,
        reason_code="ok",
        grant_ref="grant-1",
    )
    executor.execute.side_effect = RuntimeError("tool timeout")
    deduper.seen.return_value = False

    with pytest.raises(RuntimeError, match="tool timeout"):
        asyncio.run(workflow.process_action_commit(make_commit(12)))

    recovery.decide.assert_awaited_once()


def test_workflow_appends_abort_recovery_event_after_failure() -> None:
    """Workflow should emit recovery-aborted event when recovery aborts."""

    (
        workflow,
        event_log,
        projection,
        admission,
        executor,
        recovery,
        deduper,
    ) = build_workflow()
    projection.catch_up.return_value = make_projection(12)
    projection.readiness.return_value = True
    admission.check.return_value = _AdmissionResultStub(
        admitted=True,
        reason_code="ok",
        grant_ref="grant-1",
    )
    executor.execute.side_effect = RuntimeError("tool timeout")
    deduper.seen.return_value = False
    recovery.decide.return_value = RecoveryDecision(
        run_id="run-1",
        mode="abort",
        reason="fatal",
    )

    with pytest.raises(RuntimeError, match="tool timeout"):
        asyncio.run(workflow.process_action_commit(make_commit(12)))

    appended_recovery_commit = event_log.append_action_commit.await_args.args[0]
    assert appended_recovery_commit.run_id == "run-1"
    assert appended_recovery_commit.events[-1].event_type == "run.recovery_aborted"
    assert appended_recovery_commit.events[-1].payload_json["mode"] == "abort"
    assert appended_recovery_commit.events[-1].payload_json["reason"] == "fatal"


def test_workflow_appends_human_escalation_event_after_failure() -> None:
    """Workflow should emit waiting-external event for escalation."""

    (
        workflow,
        event_log,
        projection,
        admission,
        executor,
        recovery,
        deduper,
    ) = build_workflow()
    projection.catch_up.return_value = make_projection(14)
    projection.readiness.return_value = True
    admission.check.return_value = _AdmissionResultStub(
        admitted=True,
        reason_code="ok",
        grant_ref="grant-1",
    )
    executor.execute.side_effect = RuntimeError("tool timeout")
    deduper.seen.return_value = False
    recovery.decide.return_value = RecoveryDecision(
        run_id="run-1",
        mode="human_escalation",
        reason="needs_operator",
    )

    with pytest.raises(RuntimeError, match="tool timeout"):
        asyncio.run(workflow.process_action_commit(make_commit(14)))

    appended_recovery_commit = event_log.append_action_commit.await_args.args[0]
    assert appended_recovery_commit.events[-1].event_type == "run.waiting_external"
    assert appended_recovery_commit.events[-1].payload_json["mode"] == "human_escalation"
    assert appended_recovery_commit.events[-1].payload_json["reason"] == "needs_operator"


def test_workflow_writes_recovery_outcome_after_failure_recovery_decision() -> None:
    """Workflow should persist recovery outcome after recovery event append."""

    (
        workflow,
        event_log,
        projection,
        admission,
        executor,
        recovery,
        deduper,
    ) = build_workflow()
    recovery_outcomes = InMemoryRecoveryOutcomeStore()
    workflow = RunActorWorkflow(
        event_log=event_log,
        projection=projection,
        admission=admission,
        executor=executor,
        recovery=recovery,
        recovery_outcomes=recovery_outcomes,
        deduper=deduper,
    )
    projection.catch_up.return_value = make_projection(31)
    projection.readiness.return_value = True
    admission.check.return_value = _AdmissionResultStub(
        admitted=True,
        reason_code="ok",
        grant_ref="grant-1",
    )
    executor.execute.side_effect = RuntimeError("tool timeout")
    deduper.seen.return_value = False
    recovery.decide.return_value = RecoveryDecision(
        run_id="run-1",
        mode="human_escalation",
        reason="needs_operator",
        escalation_channel_ref="ops://pager",
    )

    with pytest.raises(RuntimeError, match="tool timeout"):
        asyncio.run(workflow.process_action_commit(make_commit(31)))

    latest_outcome = asyncio.run(recovery_outcomes.latest_for_run("run-1"))
    assert latest_outcome is not None
    assert latest_outcome.recovery_mode == "human_escalation"
    assert latest_outcome.outcome_state == "escalated"
    assert latest_outcome.operator_escalation_ref == "ops://pager"


def test_workflow_writes_turn_intent_record_after_turn_outcome_commit() -> None:
    """Workflow should persist turn intent metadata when turn intent log is provided."""

    @dataclass(slots=True)
    class _InMemoryTurnIntentLog:
        records: list[TurnIntentRecord]

        async def write_intent(self, intent: TurnIntentRecord) -> None:
            self.records.append(intent)

        async def latest_for_run(self, run_id: str) -> TurnIntentRecord | None:
            for record in reversed(self.records):
                if record.run_id == run_id:
                    return record
            return None

    (
        _workflow,
        event_log,
        projection,
        admission,
        executor,
        recovery,
        deduper,
    ) = build_workflow()
    turn_intent_log = _InMemoryTurnIntentLog(records=[])
    workflow = RunActorWorkflow(
        event_log=event_log,
        projection=projection,
        admission=admission,
        executor=executor,
        recovery=recovery,
        turn_intent_log=turn_intent_log,
        deduper=deduper,
    )
    projection.catch_up.return_value = make_projection(41)
    projection.readiness.return_value = True
    admission.check.return_value = _AdmissionResultStub(
        admitted=True,
        reason_code="ok",
        grant_ref="grant-1",
    )
    executor.execute.return_value = {"acknowledged": True}
    deduper.seen.return_value = False

    asyncio.run(workflow.process_action_commit(make_commit(41)))

    latest_intent = asyncio.run(turn_intent_log.latest_for_run("run-1"))
    assert latest_intent is not None
    assert latest_intent.intent_commit_ref.startswith("intent:action-1:")
    assert latest_intent.outcome_kind == "dispatched"
    assert latest_intent.dispatch_dedupe_key is not None


def test_run_returns_completed_when_projection_is_not_aborted() -> None:
    """Workflow run should complete unless projection already aborts."""

    (
        workflow,
        _event_log,
        projection,
        _admission,
        _executor,
        _recovery,
        _deduper,
    ) = build_workflow()
    projection.get.return_value = make_projection(1)

    result = asyncio.run(
        workflow.run(
            RunInput(
                run_id="run-1",
                session_id="session-1",
                input_json={"goal": "collect sources"},
            )
        )
    )

    projection.get.assert_awaited_once_with("run-1")
    assert result == {
        "run_id": "run-1",
        "final_state": "completed",
    }


def test_run_returns_aborted_when_projection_state_is_aborted() -> None:
    """Workflow run should reflect abort when projection is aborted."""

    (
        workflow,
        _event_log,
        projection,
        _admission,
        _executor,
        _recovery,
        _deduper,
    ) = build_workflow()
    projection.get.return_value = RunProjection(
        run_id="run-1",
        lifecycle_state="aborted",
        projected_offset=3,
        waiting_external=False,
        ready_for_dispatch=False,
    )

    result = asyncio.run(workflow.run(RunInput(run_id="run-1")))

    assert result == {
        "run_id": "run-1",
        "final_state": "aborted",
    }


def test_run_with_parent_run_id_signals_parent_when_temporal_context_is_active() -> None:
    """Child run completion should signal parent when context exists."""

    (
        workflow,
        _event_log,
        projection,
        _admission,
        _executor,
        _recovery,
        _deduper,
    ) = build_workflow()
    projection.get.return_value = make_projection(2)
    parent_handle = AsyncMock()
    temporal_workflow_stub = Mock()
    temporal_workflow_stub.get_external_workflow_handle.return_value = parent_handle

    with (
        patch.object(
            run_actor_workflow_module,
            "_is_temporal_workflow_context",
            return_value=True,
        ),
        patch.object(
            run_actor_workflow_module,
            "temporal_workflow",
            temporal_workflow_stub,
        ),
    ):
        result = asyncio.run(
            workflow.run(
                RunInput(
                    run_id="child-run-1",
                    parent_run_id="parent-run-1",
                )
            )
        )

    assert result == {
        "run_id": "child-run-1",
        "final_state": "completed",
    }
    temporal_workflow_stub.get_external_workflow_handle.assert_called_once_with("run:parent-run-1")
    parent_handle.signal.assert_awaited_once_with(
        "signal",
        {
            "signal_type": "child_completed",
            "signal_payload": {
                "child_run_id": "child-run-1",
            },
            "caused_by": "child:child-run-1",
        },
    )


def test_run_without_parent_run_id_does_not_signal_parent_workflow() -> None:
    """Run should not attempt parent callback when no parent_run_id."""

    (
        workflow,
        _event_log,
        projection,
        _admission,
        _executor,
        _recovery,
        _deduper,
    ) = build_workflow()
    projection.get.return_value = make_projection(3)
    temporal_workflow_stub = Mock()

    with (
        patch.object(
            run_actor_workflow_module,
            "_is_temporal_workflow_context",
            return_value=True,
        ),
        patch.object(
            run_actor_workflow_module,
            "temporal_workflow",
            temporal_workflow_stub,
        ),
    ):
        result = asyncio.run(workflow.run(RunInput(run_id="run-no-parent")))

    assert result == {
        "run_id": "run-no-parent",
        "final_state": "completed",
    }
    temporal_workflow_stub.get_external_workflow_handle.assert_not_called()


def test_run_with_parent_run_id_is_safe_outside_temporal_workflow_context() -> None:
    """Run should skip parent callback outside Temporal context."""

    (
        workflow,
        _event_log,
        projection,
        _admission,
        _executor,
        _recovery,
        _deduper,
    ) = build_workflow()
    projection.get.return_value = make_projection(4)
    temporal_workflow_stub = Mock()

    with (
        patch.object(
            run_actor_workflow_module,
            "_is_temporal_workflow_context",
            return_value=False,
        ),
        patch.object(
            run_actor_workflow_module,
            "temporal_workflow",
            temporal_workflow_stub,
        ),
    ):
        result = asyncio.run(
            workflow.run(
                RunInput(
                    run_id="child-run-2",
                    parent_run_id="parent-run-2",
                )
            )
        )

    assert result == {
        "run_id": "child-run-2",
        "final_state": "completed",
    }
    temporal_workflow_stub.get_external_workflow_handle.assert_not_called()


def test_signal_appends_commit_and_triggers_one_authoritative_round() -> None:
    """Signal should append a wake event and drive one decision-gated round."""

    (
        workflow,
        event_log,
        projection,
        admission,
        _executor,
        _recovery,
        deduper,
    ) = build_workflow()
    projection.get.return_value = make_projection(6)
    projection.catch_up.return_value = make_projection(7)
    projection.readiness.return_value = True
    deduper.seen.return_value = False

    asyncio.run(workflow.run(RunInput(run_id="run-1")))
    asyncio.run(
        workflow.signal(
            ActorSignal(
                signal_type="external_callback",
                signal_payload={"status": "done"},
                caused_by="callback-1",
            )
        )
    )

    event_log.append_action_commit.assert_awaited_once()
    appended_commit = event_log.append_action_commit.await_args.args[0]
    assert appended_commit.run_id == "run-1"
    assert appended_commit.action is None
    assert appended_commit.caused_by == "callback-1"
    assert appended_commit.events[0].event_type == "signal.external_callback"
    assert appended_commit.events[0].wake_policy == "wake_actor"
    projection.catch_up.assert_awaited_once_with("run-1", 7)
    projection.readiness.assert_awaited_once_with("run-1", 7)
    deduper.mark.assert_awaited_once()
    admission.check.assert_not_awaited()


def test_signal_before_run_is_buffered_and_replayed_after_run() -> None:
    """Workflow should buffer early signals and replay after run() initializes."""

    (
        workflow,
        event_log,
        projection,
        admission,
        _executor,
        _recovery,
        deduper,
    ) = build_workflow()
    projection.get.side_effect = [make_projection(8), make_projection(8)]
    projection.catch_up.return_value = make_projection(9)
    projection.readiness.return_value = True
    deduper.seen.return_value = False

    asyncio.run(
        workflow.signal(
            ActorSignal(
                signal_type="external_callback",
                signal_payload={"status": "done"},
                caused_by="buffered-callback-1",
            )
        )
    )
    event_log.append_action_commit.assert_not_awaited()

    asyncio.run(workflow.run(RunInput(run_id="run-1")))

    event_log.append_action_commit.assert_awaited_once()
    buffered_commit = event_log.append_action_commit.await_args.args[0]
    assert buffered_commit.caused_by == "buffered-callback-1"
    assert buffered_commit.events[0].event_type == "signal.external_callback"
    deduper.mark.assert_awaited_once()
    admission.check.assert_not_awaited()


def test_query_reads_latest_projection_for_active_run() -> None:
    """Workflow query should synchronously return cached projection."""

    (
        workflow,
        _event_log,
        projection,
        _admission,
        _executor,
        _recovery,
        _deduper,
    ) = build_workflow()
    projection.get.return_value = make_projection(8)

    asyncio.run(workflow.run(RunInput(run_id="run-1")))
    queried_projection = workflow.query()

    projection.get.assert_awaited_once_with("run-1")
    assert queried_projection.run_id == "run-1"
    assert queried_projection.projected_offset == 8


def test_workflow_rejects_derived_diagnostic_commit_on_authority_path() -> None:
    """derived_diagnostic event must not be accepted as workflow authority input."""
    (
        workflow,
        _event_log,
        _projection,
        _admission,
        _executor,
        _recovery,
        _deduper,
    ) = build_workflow()

    commit = ActionCommit(
        run_id="run-1",
        commit_id="commit-derived-1",
        created_at="2026-03-31T00:00:00Z",
        action=make_commit(10).action,
        events=[
            RuntimeEvent(
                run_id="run-1",
                event_id="evt-derived-authority",
                commit_offset=10,
                event_type="diagnostic.trace",
                event_class="derived",
                event_authority="derived_diagnostic",
                ordering_key="run-1",
                wake_policy="projection_only",
                created_at="2026-03-31T00:00:00Z",
            )
        ],
    )
    with pytest.raises(ValueError, match="derived_diagnostic"):
        asyncio.run(workflow.process_action_commit(commit))


def test_workflow_rejects_multiple_dispatch_states_in_single_turn() -> None:
    """Workflow boundary should reject turn result with multiple dispatched states."""
    turn_engine = Mock()
    turn_engine.run_turn = AsyncMock(
        return_value=make_turn_result(
            state="dispatch_acknowledged",
            outcome_kind="dispatched",
            emitted_states=[
                "collecting",
                "intent_committed",
                "dispatched",
                "dispatched",
                "dispatch_acknowledged",
            ],
        )
    )
    (
        workflow,
        _event_log,
        projection,
        _admission,
        _executor,
        _recovery,
        deduper,
    ) = build_workflow(turn_engine=turn_engine)
    projection.catch_up.return_value = make_projection(30)
    projection.readiness.return_value = True
    deduper.seen.return_value = False

    with pytest.raises(RuntimeError, match="multiple authoritative dispatch"):
        asyncio.run(workflow.process_action_commit(make_commit(30)))


def test_workflow_blocks_recovery_gate_direct_event_log_mutation() -> None:
    """Recovery gate must be read-only and cannot append runtime events directly."""
    event_log = InMemoryKernelRuntimeEventLog()
    projection = InMemoryDecisionProjectionService(event_log)
    admission = StaticDispatchAdmissionService()

    async def _failing_handler(_action: Action, _grant_ref: str | None) -> dict[str, Any]:
        raise RuntimeError("executor failed")

    executor = AsyncExecutorService(handler=_failing_handler)

    class _MutatingRecoveryGate:
        async def decide(self, recovery_input: Any) -> RecoveryDecision:
            await event_log.append_action_commit(
                ActionCommit(
                    run_id=recovery_input.run_id,
                    commit_id="illegal:recovery:mutation",
                    created_at="2026-03-31T00:00:00Z",
                    action=None,
                    events=[
                        RuntimeEvent(
                            run_id=recovery_input.run_id,
                            event_id="evt-illegal-recovery-mutation",
                            commit_offset=999,
                            event_type="run.ready",
                            event_class="fact",
                            event_authority="authoritative_fact",
                            ordering_key=recovery_input.run_id,
                            wake_policy="wake_actor",
                            created_at="2026-03-31T00:00:00Z",
                        )
                    ],
                )
            )
            return RecoveryDecision(
                run_id=recovery_input.run_id,
                mode="human_escalation",
                reason="illegal-mutation",
            )

    workflow = RunActorWorkflow(
        event_log=event_log,
        projection=projection,
        admission=admission,
        executor=executor,
        recovery=_MutatingRecoveryGate(),
        deduper=InMemoryDecisionDeduper(),
    )

    asyncio.run(
        event_log.append_action_commit(
            ActionCommit(
                run_id="run-1",
                commit_id="commit-ready",
                created_at="2026-03-31T00:00:00Z",
                action=None,
                events=[
                    RuntimeEvent(
                        run_id="run-1",
                        event_id="evt-ready",
                        commit_offset=1,
                        event_type="run.ready",
                        event_class="fact",
                        event_authority="authoritative_fact",
                        ordering_key="run-1",
                        wake_policy="wake_actor",
                        created_at="2026-03-31T00:00:00Z",
                    )
                ],
            )
        )
    )
    asyncio.run(workflow.run(RunInput(run_id="run-1")))

    action_commit = make_commit(2)
    asyncio.run(event_log.append_action_commit(action_commit))

    with pytest.raises(RuntimeError, match="Recovery gate must be read-only"):
        asyncio.run(workflow.process_action_commit(action_commit))


def test_signal_dedupes_replayed_callback_by_caused_by_token() -> None:
    """Workflow should ignore duplicated callback signals."""

    (
        workflow,
        event_log,
        projection,
        _admission,
        _executor,
        _recovery,
        deduper,
    ) = build_workflow()
    projection.get.return_value = make_projection(10)
    projection.catch_up.return_value = make_projection(11)
    projection.readiness.return_value = True
    deduper.seen.return_value = False

    asyncio.run(workflow.run(RunInput(run_id="run-1")))
    signal = ActorSignal(
        signal_type="external_callback",
        signal_payload={"status": "done"},
        caused_by="callback-dup-1",
    )
    asyncio.run(workflow.signal(signal))
    asyncio.run(workflow.signal(signal))

    event_log.append_action_commit.assert_awaited_once()
    deduper.mark.assert_awaited_once()


def test_resume_signal_is_encoded_as_resume_event_type() -> None:
    """Workflow should emit a dedicated resume event type."""

    (
        workflow,
        event_log,
        projection,
        _admission,
        _executor,
        _recovery,
        deduper,
    ) = build_workflow()
    projection.get.return_value = make_projection(4)
    projection.catch_up.return_value = make_projection(5)
    projection.readiness.return_value = True
    deduper.seen.return_value = False

    asyncio.run(workflow.run(RunInput(run_id="run-1")))
    asyncio.run(
        workflow.signal(
            ActorSignal(
                signal_type="resume_from_snapshot",
                signal_payload={
                    "snapshot_id": "snapshot:run-1:4",
                },
                caused_by="operator-1",
            )
        )
    )

    appended_commit = event_log.append_action_commit.await_args.args[0]
    assert appended_commit.events[0].event_type == "run.resume_requested"


def test_cancel_signal_is_encoded_as_cancel_requested_event_type() -> None:
    """Workflow should encode cancel signal as runtime cancel event."""

    (
        workflow,
        event_log,
        projection,
        _admission,
        _executor,
        _recovery,
        deduper,
    ) = build_workflow()
    projection.get.return_value = make_projection(11)
    projection.catch_up.return_value = make_projection(12)
    projection.readiness.return_value = True
    deduper.seen.return_value = False

    asyncio.run(workflow.run(RunInput(run_id="run-1")))
    asyncio.run(
        workflow.signal(
            ActorSignal(
                signal_type="cancel_requested",
                signal_payload={
                    "reason": "user_requested_cancel",
                },
                caused_by="operator-cancel-1",
            )
        )
    )

    appended_commit = event_log.append_action_commit.await_args.args[0]
    assert appended_commit.events[0].event_type == "run.cancel_requested"


def test_timeout_signal_is_encoded_as_waiting_external_with_timeout_reason() -> None:
    """Workflow should encode timeout signal as waiting-external runtime event."""
    (
        workflow,
        event_log,
        projection,
        _admission,
        _executor,
        _recovery,
        deduper,
    ) = build_workflow()
    projection.get.return_value = make_projection(12)
    projection.catch_up.return_value = make_projection(13)
    projection.readiness.return_value = True
    deduper.seen.return_value = False

    asyncio.run(workflow.run(RunInput(run_id="run-1")))
    asyncio.run(
        workflow.signal(
            ActorSignal(
                signal_type="timeout",
                caused_by="timer-1",
            )
        )
    )

    appended_commit = event_log.append_action_commit.await_args.args[0]
    assert appended_commit.events[0].event_type == "run.waiting_external"
    assert appended_commit.events[0].payload_json == {
        "mode": "human_escalation",
        "reason": "timeout",
    }


def test_hard_failure_signal_is_encoded_as_recovery_aborted_with_reason() -> None:
    """Workflow should encode hard-failure signal as recovery-aborted runtime event."""
    (
        workflow,
        event_log,
        projection,
        _admission,
        _executor,
        _recovery,
        deduper,
    ) = build_workflow()
    projection.get.return_value = make_projection(13)
    projection.catch_up.return_value = make_projection(14)
    projection.readiness.return_value = True
    deduper.seen.return_value = False

    asyncio.run(workflow.run(RunInput(run_id="run-1")))
    asyncio.run(
        workflow.signal(
            ActorSignal(
                signal_type="hard_failure",
                signal_payload={"reason": "executor_fatal"},
                caused_by="worker-1",
            )
        )
    )

    appended_commit = event_log.append_action_commit.await_args.args[0]
    assert appended_commit.events[0].event_type == "run.recovery_aborted"
    assert appended_commit.events[0].payload_json == {
        "mode": "abort",
        "reason": "executor_fatal",
    }


def test_recovery_succeeded_signal_is_encoded_as_ready_transition_event() -> None:
    """Workflow should encode recovery success as runtime event."""

    (
        workflow,
        event_log,
        projection,
        _admission,
        _executor,
        _recovery,
        deduper,
    ) = build_workflow()
    projection.get.return_value = make_projection(14)
    projection.catch_up.return_value = make_projection(15)
    projection.readiness.return_value = True
    deduper.seen.return_value = False

    asyncio.run(workflow.run(RunInput(run_id="run-1")))
    asyncio.run(
        workflow.signal(
            ActorSignal(
                signal_type="recovery_succeeded",
                signal_payload={
                    "recovery_action_id": "recover-1",
                },
                caused_by="recovery-worker-1",
            )
        )
    )

    appended_commit = event_log.append_action_commit.await_args.args[0]
    assert appended_commit.events[0].event_type == "run.recovery_succeeded"


def test_recovery_aborted_signal_is_encoded_as_abort_transition_event() -> None:
    """Workflow should encode recovery abort as terminal runtime event."""

    (
        workflow,
        event_log,
        projection,
        _admission,
        _executor,
        _recovery,
        deduper,
    ) = build_workflow()
    projection.get.return_value = make_projection(19)
    projection.catch_up.return_value = make_projection(20)
    projection.readiness.return_value = True
    deduper.seen.return_value = False

    asyncio.run(workflow.run(RunInput(run_id="run-1")))
    asyncio.run(
        workflow.signal(
            ActorSignal(
                signal_type="recovery_aborted",
                signal_payload={"reason": "operator_abort"},
                caused_by="operator-9",
            )
        )
    )

    appended_commit = event_log.append_action_commit.await_args.args[0]
    assert appended_commit.events[0].event_type == "run.recovery_aborted"


def test_cancel_terminal_invariant_blocks_recovery_succeeded_and_callback_signals() -> None:
    """Cancel must keep projection aborted after recovery and callback."""
    event_log = InMemoryKernelRuntimeEventLog()
    projection = InMemoryDecisionProjectionService(event_log)
    admission = StaticDispatchAdmissionService()
    executor = AsyncExecutorService()
    recovery = StaticRecoveryGateService()
    deduper = InMemoryDecisionDeduper()
    workflow = RunActorWorkflow(
        event_log=event_log,
        projection=projection,
        admission=admission,
        executor=executor,
        recovery=recovery,
        deduper=deduper,
    )

    asyncio.run(workflow.run(RunInput(run_id="run-cancel-terminal-1")))
    asyncio.run(
        workflow.signal(
            ActorSignal(
                signal_type="cancel_requested",
                signal_payload={"reason": "operator_cancel"},
                caused_by="cancel-1",
            )
        )
    )

    asyncio.run(
        workflow.signal(
            ActorSignal(
                signal_type="recovery_succeeded",
                signal_payload={
                    "recovery_action_id": "recover-1",
                },
                caused_by="recovery-success-1",
            )
        )
    )
    asyncio.run(
        workflow.signal(
            ActorSignal(
                signal_type="external_callback",
                signal_payload={"status": "done"},
                caused_by="callback-1",
            )
        )
    )

    state = workflow.query()
    assert state.lifecycle_state == "aborted"
    assert not state.ready_for_dispatch
    assert not state.waiting_external
    assert state.recovery_mode == "abort"
    assert state.recovery_reason == "operator_cancel"
    assert state.projected_offset == 3


def test_cancel_terminal_invariant_blocks_recovery_aborted_and_callback_signals() -> None:
    """Cancel must keep projection aborted after recovery-abort and callback."""
    event_log = InMemoryKernelRuntimeEventLog()
    projection = InMemoryDecisionProjectionService(event_log)
    admission = StaticDispatchAdmissionService()
    executor = AsyncExecutorService()
    recovery = StaticRecoveryGateService()
    deduper = InMemoryDecisionDeduper()
    workflow = RunActorWorkflow(
        event_log=event_log,
        projection=projection,
        admission=admission,
        executor=executor,
        recovery=recovery,
        deduper=deduper,
    )

    asyncio.run(workflow.run(RunInput(run_id="run-cancel-terminal-2")))
    asyncio.run(
        workflow.signal(
            ActorSignal(
                signal_type="cancel_requested",
                signal_payload={
                    "reason": "user_requested_cancel",
                },
                caused_by="cancel-2",
            )
        )
    )

    asyncio.run(
        workflow.signal(
            ActorSignal(
                signal_type="recovery_aborted",
                signal_payload={
                    "reason": "late_recovery_abort",
                },
                caused_by="recovery-abort-2",
            )
        )
    )
    asyncio.run(
        workflow.signal(
            ActorSignal(
                signal_type="external_callback",
                signal_payload={"status": "late_callback"},
                caused_by="callback-2",
            )
        )
    )

    state = workflow.query()
    assert state.lifecycle_state == "aborted"
    assert not state.ready_for_dispatch
    assert not state.waiting_external
    assert state.recovery_mode == "abort"
    assert state.recovery_reason == "user_requested_cancel"
    assert state.projected_offset == 3


def test_operational_priority_matrix_timeout_blocks_late_external_callback_signal() -> None:
    """Timeout must dominate a later external callback in workflow signal path."""
    event_log = InMemoryKernelRuntimeEventLog()
    projection = InMemoryDecisionProjectionService(event_log)
    admission = StaticDispatchAdmissionService()
    executor = AsyncExecutorService()
    recovery = StaticRecoveryGateService()
    deduper = InMemoryDecisionDeduper()
    workflow = RunActorWorkflow(
        event_log=event_log,
        projection=projection,
        admission=admission,
        executor=executor,
        recovery=recovery,
        deduper=deduper,
    )

    asyncio.run(workflow.run(RunInput(run_id="run-priority-signal-1")))
    asyncio.run(
        workflow.signal(
            ActorSignal(
                signal_type="timeout",
                signal_payload={"reason": "tool_timeout"},
                caused_by="timeout-1",
            )
        )
    )
    asyncio.run(
        workflow.signal(
            ActorSignal(
                signal_type="external_callback",
                signal_payload={"status": "done"},
                caused_by="callback-1",
            )
        )
    )

    state = workflow.query()
    assert state.lifecycle_state == "waiting_external"
    assert state.waiting_external
    assert not state.ready_for_dispatch
    assert state.recovery_mode == "human_escalation"
    assert state.recovery_reason == "tool_timeout"
    assert state.projected_offset == 2


def test_operational_priority_matrix_full_chain_resolves_to_cancel_in_workflow() -> None:
    """Full signal chain must resolve to cancel by v6.4 precedence matrix."""
    event_log = InMemoryKernelRuntimeEventLog()
    projection = InMemoryDecisionProjectionService(event_log)
    admission = StaticDispatchAdmissionService()
    executor = AsyncExecutorService()
    recovery = StaticRecoveryGateService()
    deduper = InMemoryDecisionDeduper()
    workflow = RunActorWorkflow(
        event_log=event_log,
        projection=projection,
        admission=admission,
        executor=executor,
        recovery=recovery,
        deduper=deduper,
    )

    asyncio.run(workflow.run(RunInput(run_id="run-priority-signal-2")))
    asyncio.run(
        workflow.signal(
            ActorSignal(
                signal_type="external_callback",
                signal_payload={"status": "done"},
                caused_by="callback-2",
            )
        )
    )
    asyncio.run(
        workflow.signal(
            ActorSignal(
                signal_type="timeout",
                signal_payload={"reason": "tool_timeout"},
                caused_by="timeout-2",
            )
        )
    )
    asyncio.run(
        workflow.signal(
            ActorSignal(
                signal_type="hard_failure",
                signal_payload={"reason": "executor_fatal"},
                caused_by="hard-failure-2",
            )
        )
    )
    asyncio.run(
        workflow.signal(
            ActorSignal(
                signal_type="cancel_requested",
                signal_payload={"reason": "operator_cancel"},
                caused_by="cancel-2",
            )
        )
    )

    state = workflow.query()
    assert state.lifecycle_state == "aborted"
    assert not state.waiting_external
    assert not state.ready_for_dispatch
    assert state.recovery_mode == "abort"
    assert state.recovery_reason == "operator_cancel"
    assert state.projected_offset == 4
