"""Tests for minimal in-memory kernel runtime service implementations."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from agent_kernel.kernel.contracts import (
    Action,
    ActionCommit,
    MCPActivityInput,
    RecoveryInput,
    RecoveryOutcome,
    RunProjection,
    RuntimeEvent,
    ToolActivityInput,
)
from agent_kernel.kernel.minimal_runtime import (
    ActivityBackedExecutorService,
    AsyncExecutorService,
    InMemoryDecisionDeduper,
    InMemoryDecisionProjectionService,
    InMemoryKernelRuntimeEventLog,
    InMemoryRecoveryOutcomeStore,
    StaticDispatchAdmissionService,
    StaticRecoveryGateService,
    StaticRecoveryPolicy,
)
from agent_kernel.substrate.temporal.run_actor_workflow import (
    ActorSignal,
    RunActorWorkflow,
    RunInput,
)


@dataclass(slots=True)
class _RecordingActivityGateway:
    """Records activity requests to validate executor routing."""

    tool_requests: list[ToolActivityInput] = field(
        default_factory=list,
    )
    mcp_requests: list[MCPActivityInput] = field(
        default_factory=list,
    )

    async def execute_tool(
        self,
        request: ToolActivityInput,
    ) -> dict[str, Any]:
        self.tool_requests.append(request)
        return {"route": "tool", "tool_name": request.tool_name}

    async def execute_mcp(
        self,
        request: MCPActivityInput,
    ) -> dict[str, Any]:
        self.mcp_requests.append(request)
        return {
            "route": "mcp",
            "server_name": request.server_name,
            "operation": request.operation,
        }


def _build_commit(
    run_id: str,
    commit_id: str,
    event_type: str,
    payload_json: dict[str, object] | None = None,
) -> ActionCommit:
    """Builds one commit with a single fact event for runtime tests."""
    return ActionCommit(
        run_id=run_id,
        commit_id=commit_id,
        created_at="2026-03-31T00:00:00Z",
        events=[
            RuntimeEvent(
                run_id=run_id,
                event_id=f"evt-{commit_id}",
                commit_offset=0,
                event_type=event_type,
                event_class="fact",
                event_authority="authoritative_fact",
                ordering_key=run_id,
                wake_policy="wake_actor",
                created_at="2026-03-31T00:00:00Z",
                payload_json=payload_json,
            )
        ],
    )


def _strict_snapshot_input_payload() -> dict[str, Any]:
    """Builds minimal strict snapshot payload required by workflow TurnEngine."""
    return {
        "capability_snapshot_input": {
            "tenant_policy_ref": "policy:test",
            "permission_mode": "strict",
            "declarative_bundle_digest": {
                "bundle_ref": "bundle://test",
                "semantics_version": "v1",
                "content_hash": "content-hash-1",
                "compile_hash": "compile-hash-1",
            },
        }
    }


def test_event_log_appends_and_loads_events_in_offset_order() -> None:
    """Event log should persist commits and expose stable offset ordering."""
    event_log = InMemoryKernelRuntimeEventLog()

    asyncio.run(event_log.append_action_commit(_build_commit("run-1", "c1", "run.created")))
    asyncio.run(event_log.append_action_commit(_build_commit("run-1", "c2", "run.ready")))

    events = asyncio.run(event_log.load("run-1"))
    assert [event.commit_offset for event in events] == [1, 2]
    assert [event.event_type for event in events] == [
        "run.created",
        "run.ready",
    ]

    tail_events = asyncio.run(event_log.load("run-1", after_offset=1))
    assert len(tail_events) == 1
    assert tail_events[0].event_type == "run.ready"


def test_projection_catch_up_and_readiness_follow_runtime_offsets() -> None:
    """Projection service should honor catch-up offsets and readiness."""
    event_log = InMemoryKernelRuntimeEventLog()
    projection = InMemoryDecisionProjectionService(event_log)
    asyncio.run(event_log.append_action_commit(_build_commit("run-2", "c1", "run.created")))
    asyncio.run(event_log.append_action_commit(_build_commit("run-2", "c2", "run.ready")))

    through_created = asyncio.run(projection.catch_up("run-2", through_offset=1))
    assert through_created.lifecycle_state == "created"
    assert through_created.projected_offset == 1

    through_ready = asyncio.run(projection.catch_up("run-2", through_offset=2))
    assert through_ready.lifecycle_state == "ready"
    assert through_ready.ready_for_dispatch
    assert asyncio.run(projection.readiness("run-2", required_offset=2))
    assert not asyncio.run(projection.readiness("run-2", required_offset=3))


def test_admission_enforces_dispatch_readiness() -> None:
    """Admission should deny non-ready states and grant ready dispatches."""
    event_log = InMemoryKernelRuntimeEventLog()
    projection_service = InMemoryDecisionProjectionService(event_log)
    admission = StaticDispatchAdmissionService()
    action = Action(
        action_id="action-1",
        run_id="run-3",
        action_type="web_research",
        effect_class="read_only",
    )

    non_ready_projection = asyncio.run(projection_service.get("run-3"))
    denied = asyncio.run(admission.check(action, non_ready_projection))
    assert not denied.admitted
    assert denied.reason_code == "dependency_not_ready"

    asyncio.run(event_log.append_action_commit(_build_commit("run-3", "c1", "run.ready")))
    ready_projection = asyncio.run(projection_service.get("run-3"))
    admitted = asyncio.run(admission.check(action, ready_projection))
    assert admitted.admitted
    assert admitted.reason_code == "ok"
    assert admitted.grant_ref == "grant:action-1"


def test_admission_denies_requires_human_review_policy_tag() -> None:
    """Admission should deny actions that require explicit human review."""
    event_log = InMemoryKernelRuntimeEventLog()
    projection_service = InMemoryDecisionProjectionService(event_log)
    admission = StaticDispatchAdmissionService()
    asyncio.run(event_log.append_action_commit(_build_commit("run-3b", "c1", "run.ready")))
    ready_projection = asyncio.run(projection_service.get("run-3b"))

    action = Action(
        action_id="action-1b",
        run_id="run-3b",
        action_type="web_research",
        effect_class="read_only",
        policy_tags=["requires_human_review"],
    )

    denied = asyncio.run(admission.check(action, ready_projection))
    assert not denied.admitted
    assert denied.reason_code == "policy_denied"


def test_admission_denies_timeout_over_five_minutes() -> None:
    """Admission should deny actions whose timeout exceeds 5 minutes."""
    event_log = InMemoryKernelRuntimeEventLog()
    projection_service = InMemoryDecisionProjectionService(event_log)
    admission = StaticDispatchAdmissionService()
    asyncio.run(event_log.append_action_commit(_build_commit("run-3c", "c1", "run.ready")))
    ready_projection = asyncio.run(projection_service.get("run-3c"))

    action = Action(
        action_id="action-1c",
        run_id="run-3c",
        action_type="web_research",
        effect_class="read_only",
        timeout_ms=300001,
    )

    denied = asyncio.run(admission.check(action, ready_projection))
    assert not denied.admitted
    assert denied.reason_code == "policy_denied"


def test_admission_denies_when_estimated_cost_exceeds_max_cost_tag() -> None:
    """Admission should deny action when estimated_cost exceeds max_cost."""
    event_log = InMemoryKernelRuntimeEventLog()
    projection_service = InMemoryDecisionProjectionService(event_log)
    admission = StaticDispatchAdmissionService()
    asyncio.run(event_log.append_action_commit(_build_commit("run-3d", "c1", "run.ready")))
    ready_projection = asyncio.run(projection_service.get("run-3d"))

    action = Action(
        action_id="action-1d",
        run_id="run-3d",
        action_type="web_research",
        effect_class="read_only",
        policy_tags=["max_cost:10"],
        input_json={"estimated_cost": 11},
    )

    denied = asyncio.run(admission.check(action, ready_projection))
    assert not denied.admitted
    assert denied.reason_code == "quota_exceeded"


def test_admission_allows_when_estimated_cost_within_max_cost_tag() -> None:
    """Admission should allow actions when cost is within quota."""
    event_log = InMemoryKernelRuntimeEventLog()
    projection_service = InMemoryDecisionProjectionService(event_log)
    admission = StaticDispatchAdmissionService()
    asyncio.run(event_log.append_action_commit(_build_commit("run-3e", "c1", "run.ready")))
    ready_projection = asyncio.run(projection_service.get("run-3e"))

    action = Action(
        action_id="action-1e",
        run_id="run-3e",
        action_type="web_research",
        effect_class="read_only",
        policy_tags=["max_cost:10"],
        input_json={"estimated_cost": 10},
    )

    admitted = asyncio.run(admission.check(action, ready_projection))
    assert admitted.admitted
    assert admitted.reason_code == "ok"
    assert admitted.grant_ref == "grant:action-1e"


def test_admission_ignores_invalid_quota_inputs_and_keeps_existing_decision() -> None:
    """Invalid max_cost/estimated_cost should not change readiness logic."""
    event_log = InMemoryKernelRuntimeEventLog()
    projection_service = InMemoryDecisionProjectionService(event_log)
    admission = StaticDispatchAdmissionService()
    non_ready_projection = asyncio.run(projection_service.get("run-3f"))

    action = Action(
        action_id="action-1f",
        run_id="run-3f",
        action_type="web_research",
        effect_class="read_only",
        policy_tags=["max_cost:not-a-number"],
        input_json={"estimated_cost": "unknown"},
    )

    denied = asyncio.run(admission.check(action, non_ready_projection))
    assert not denied.admitted
    assert denied.reason_code == "dependency_not_ready"


def test_admission_denies_remote_service_guaranteed_when_contract_missing() -> None:
    """Missing remote contract should deny guaranteed remote side-effect claims."""
    event_log = InMemoryKernelRuntimeEventLog()
    projection_service = InMemoryDecisionProjectionService(event_log)
    admission = StaticDispatchAdmissionService()
    asyncio.run(event_log.append_action_commit(_build_commit("run-3g", "c1", "run.ready")))
    ready_projection = asyncio.run(projection_service.get("run-3g"))

    action = Action(
        action_id="action-1g",
        run_id="run-3g",
        action_type="remote.write",
        effect_class="idempotent_write",
        external_idempotency_level="guaranteed",
        input_json={"host_kind": "remote_service"},
    )

    denied = asyncio.run(admission.check(action, ready_projection))
    assert not denied.admitted
    assert denied.reason_code == "idempotency_contract_insufficient"


def test_admission_denies_remote_service_guaranteed_when_contract_is_insufficient() -> None:
    """Insufficient remote contract should deny guaranteed remote claims."""
    event_log = InMemoryKernelRuntimeEventLog()
    projection_service = InMemoryDecisionProjectionService(event_log)
    admission = StaticDispatchAdmissionService()
    asyncio.run(event_log.append_action_commit(_build_commit("run-3h", "c1", "run.ready")))
    ready_projection = asyncio.run(projection_service.get("run-3h"))

    action = Action(
        action_id="action-1h",
        run_id="run-3h",
        action_type="remote.write",
        effect_class="idempotent_write",
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
    )

    denied = asyncio.run(admission.check(action, ready_projection))
    assert not denied.admitted
    assert denied.reason_code == "idempotency_contract_insufficient"


def test_admission_allows_remote_service_guaranteed_when_contract_is_verified() -> None:
    """Verified remote contract should preserve guaranteed admission behavior."""
    event_log = InMemoryKernelRuntimeEventLog()
    projection_service = InMemoryDecisionProjectionService(event_log)
    admission = StaticDispatchAdmissionService()
    asyncio.run(event_log.append_action_commit(_build_commit("run-3i", "c1", "run.ready")))
    ready_projection = asyncio.run(projection_service.get("run-3i"))

    action = Action(
        action_id="action-1i",
        run_id="run-3i",
        action_type="remote.write",
        effect_class="idempotent_write",
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
    )

    admitted = asyncio.run(admission.check(action, ready_projection))
    assert admitted.admitted
    assert admitted.reason_code == "ok"
    assert admitted.grant_ref == "grant:action-1i"


def test_admission_keeps_explicit_local_hosts_unaffected_by_remote_guard() -> None:
    """Explicit local hosts should bypass remote contract guard."""
    event_log = InMemoryKernelRuntimeEventLog()
    projection_service = InMemoryDecisionProjectionService(event_log)
    admission = StaticDispatchAdmissionService()
    asyncio.run(event_log.append_action_commit(_build_commit("run-3j", "c1", "run.ready")))
    ready_projection = asyncio.run(projection_service.get("run-3j"))

    local_process_action = Action(
        action_id="action-1j",
        run_id="run-3j",
        action_type="local.write",
        effect_class="idempotent_write",
        external_idempotency_level="guaranteed",
        policy_tags=["host:local_process"],
    )
    local_process_admitted = asyncio.run(admission.check(local_process_action, ready_projection))

    local_cli_action = Action(
        action_id="action-1k",
        run_id="run-3j",
        action_type="local.write",
        effect_class="idempotent_write",
        external_idempotency_level="guaranteed",
        policy_tags=["host:local_cli"],
    )
    local_cli_admitted = asyncio.run(admission.check(local_cli_action, ready_projection))

    assert local_process_admitted.admitted
    assert local_process_admitted.reason_code == "ok"
    assert local_process_admitted.grant_ref == "grant:action-1j"
    assert local_cli_admitted.admitted
    assert local_cli_admitted.reason_code == "ok"
    assert local_cli_admitted.grant_ref == "grant:action-1k"


def test_executor_recovery_and_deduper_minimal_behaviors() -> None:
    """Executor, recovery, and deduper should satisfy minimal contract."""
    executor = AsyncExecutorService()
    recovery = StaticRecoveryGateService(StaticRecoveryPolicy(mode="human_escalation"))
    deduper = InMemoryDecisionDeduper()

    action = Action(
        action_id="action-2",
        run_id="run-4",
        action_type="inspect",
        effect_class="read_only",
    )
    execute_result = asyncio.run(executor.execute(action, grant_ref="grant-2"))
    assert execute_result["action_id"] == "action-2"
    assert execute_result["grant_ref"] == "grant-2"

    fingerprint = "run-4:c1:1"
    assert not asyncio.run(deduper.seen(fingerprint))
    asyncio.run(deduper.mark(fingerprint))
    assert asyncio.run(deduper.seen(fingerprint))

    decision = asyncio.run(
        recovery.decide(
            RecoveryInput(
                run_id="run-4",
                reason_code="timeout",
                lifecycle_state="recovering",
                projection=RunProjection(
                    run_id="run-4",
                    lifecycle_state="recovering",
                    projected_offset=3,
                    waiting_external=False,
                    ready_for_dispatch=False,
                ),
            )
        )
    )
    assert decision.mode == "human_escalation"
    assert decision.reason == "recovery:timeout"


def test_recovery_outcome_store_writes_and_reads_latest_record() -> None:
    """Recovery outcome store should return the latest written outcome for a run."""
    store = InMemoryRecoveryOutcomeStore()
    first = RecoveryOutcome(
        run_id="run-recovery-1",
        action_id="action-1",
        recovery_mode="static_compensation",
        outcome_state="executed",
        written_at="2026-03-31T00:00:00Z",
    )
    second = RecoveryOutcome(
        run_id="run-recovery-1",
        action_id="action-2",
        recovery_mode="human_escalation",
        outcome_state="escalated",
        written_at="2026-03-31T00:01:00Z",
        operator_escalation_ref="ops://ticket-1",
    )

    asyncio.run(store.write_outcome(first))
    asyncio.run(store.write_outcome(second))
    latest = asyncio.run(store.latest_for_run("run-recovery-1"))

    assert latest is not None
    assert latest.action_id == "action-2"
    assert latest.recovery_mode == "human_escalation"
    assert latest.outcome_state == "escalated"


def test_activity_backed_executor_routes_default_action_to_tool_gateway() -> None:
    """Executor should route non-MCP actions through execute_tool."""
    gateway = _RecordingActivityGateway()
    executor = ActivityBackedExecutorService(gateway)

    result = asyncio.run(
        executor.execute(
            Action(
                action_id="action-tool-1",
                run_id="run-tool-1",
                action_type="web_research",
                effect_class="read_only",
                input_json={
                    "tool_name": "web.search",
                    "arguments": {"q": "kernel"},
                },
            )
        )
    )

    assert result == {"route": "tool", "tool_name": "web.search"}
    assert len(gateway.tool_requests) == 1
    assert len(gateway.mcp_requests) == 0
    assert gateway.tool_requests[0].run_id == "run-tool-1"
    assert gateway.tool_requests[0].action_id == "action-tool-1"
    assert gateway.tool_requests[0].arguments == {"q": "kernel"}


def test_activity_backed_executor_routes_input_json_mcp_payload_to_mcp_gateway() -> None:
    """Executor should route actions with input_json.mcp via execute_mcp."""
    gateway = _RecordingActivityGateway()
    executor = ActivityBackedExecutorService(gateway)

    result = asyncio.run(
        executor.execute(
            Action(
                action_id="action-mcp-1",
                run_id="run-mcp-1",
                action_type="web_research",
                effect_class="read_only",
                input_json={
                    "mcp": {
                        "server_name": "docs",
                        "operation": "fetch",
                        "arguments": {"path": "README.md"},
                    }
                },
            )
        )
    )

    assert result == {
        "route": "mcp",
        "server_name": "docs",
        "operation": "fetch",
    }
    assert len(gateway.tool_requests) == 0
    assert len(gateway.mcp_requests) == 1
    assert gateway.mcp_requests[0].run_id == "run-mcp-1"
    assert gateway.mcp_requests[0].action_id == "action-mcp-1"
    assert gateway.mcp_requests[0].arguments == {"path": "README.md"}


def test_run_actor_workflow_integrates_with_minimal_runtime_services() -> None:
    """RunActorWorkflow should run and accept signals with minimal wiring."""
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

    asyncio.run(event_log.append_action_commit(_build_commit("run-5", "c1", "run.ready")))
    run_result = asyncio.run(workflow.run(RunInput(run_id="run-5")))
    assert run_result["run_id"] == "run-5"

    asyncio.run(
        workflow.signal(
            ActorSignal(
                signal_type="external_callback",
                signal_payload={"ok": True},
                caused_by="callback-5",
            )
        )
    )
    projection_state = workflow.query()
    assert projection_state.run_id == "run-5"
    assert projection_state.projected_offset >= 2


def test_run_actor_workflow_accepts_legacy_executor_result_without_fact_events() -> None:
    """Workflow should keep legacy execution compatibility and append turn fact."""
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

    asyncio.run(event_log.append_action_commit(_build_commit("run-5b", "c1", "run.ready")))
    asyncio.run(workflow.run(RunInput(run_id="run-5b")))

    action_commit = ActionCommit(
        run_id="run-5b",
        commit_id="c2",
        created_at="2026-03-31T00:00:00Z",
        action=Action(
            action_id="action-legacy-1",
            run_id="run-5b",
            action_type="inspect",
            effect_class="read_only",
            input_json=_strict_snapshot_input_payload(),
        ),
        events=[
            RuntimeEvent(
                run_id="run-5b",
                event_id="evt-c2",
                commit_offset=2,
                event_type="action_committed",
                event_class="fact",
                event_authority="authoritative_fact",
                ordering_key="run-5b",
                wake_policy="wake_actor",
                created_at="2026-03-31T00:00:00Z",
            )
        ],
    )
    asyncio.run(event_log.append_action_commit(action_commit))

    asyncio.run(workflow.process_action_commit(action_commit))

    events = asyncio.run(event_log.load("run-5b"))
    assert len(events) == 3
    assert [event.event_type for event in events] == [
        "run.ready",
        "action_committed",
        "run.dispatching",
    ]


def test_resume_signal_moves_projection_into_recovering_state() -> None:
    """Resume signal should map to recovering projection state."""
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

    asyncio.run(
        event_log.append_action_commit(_build_commit("run-6", "c1", "run.waiting_external"))
    )
    asyncio.run(workflow.run(RunInput(run_id="run-6")))
    asyncio.run(
        workflow.signal(
            ActorSignal(
                signal_type="resume_from_snapshot",
                signal_payload={
                    "snapshot_id": "snapshot:run-6:1",
                },
                caused_by="operator-6",
            )
        )
    )
    projection_state = workflow.query()
    assert projection_state.run_id == "run-6"
    assert projection_state.lifecycle_state == "recovering"
    assert projection_state.recovery_mode == "static_compensation"
    assert projection_state.recovery_reason is None


def test_recovery_succeeded_signal_moves_projection_back_to_ready() -> None:
    """Recovery succeeded signal should move projection from recovering to ready."""
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

    asyncio.run(workflow.run(RunInput(run_id="run-7")))
    asyncio.run(
        workflow.signal(
            ActorSignal(
                signal_type="resume_from_snapshot",
                signal_payload={
                    "snapshot_id": "snapshot:run-7:0",
                },
                caused_by="operator-7",
            )
        )
    )
    asyncio.run(
        workflow.signal(
            ActorSignal(
                signal_type="recovery_succeeded",
                signal_payload={
                    "recovery_action_id": "recover-7",
                },
                caused_by="worker-7",
            )
        )
    )

    projection_state = workflow.query()
    assert projection_state.run_id == "run-7"
    assert projection_state.lifecycle_state == "ready"
    assert projection_state.ready_for_dispatch
    assert projection_state.recovery_mode is None


def test_recovery_aborted_signal_moves_projection_to_aborted() -> None:
    """Recovery aborted signal should move projection to aborted state."""
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

    asyncio.run(workflow.run(RunInput(run_id="run-8")))
    asyncio.run(
        workflow.signal(
            ActorSignal(
                signal_type="resume_from_snapshot",
                signal_payload={
                    "snapshot_id": "snapshot:run-8:0",
                },
                caused_by="operator-8",
            )
        )
    )
    asyncio.run(
        workflow.signal(
            ActorSignal(
                signal_type="recovery_aborted",
                signal_payload={"reason": "manual_abort"},
                caused_by="operator-8-abort",
            )
        )
    )

    projection_state = workflow.query()
    assert projection_state.run_id == "run-8"
    assert projection_state.lifecycle_state == "aborted"
    assert projection_state.recovery_mode == "abort"
    assert projection_state.recovery_reason == "manual_abort"


def test_cancel_requested_event_moves_projection_to_aborted_with_reason() -> None:
    """run.cancel_requested should abort projection and preserve cancel reason."""
    event_log = InMemoryKernelRuntimeEventLog()
    projection = InMemoryDecisionProjectionService(event_log)

    asyncio.run(
        event_log.append_action_commit(
            _build_commit(
                "run-8b",
                "c1",
                "run.cancel_requested",
                payload_json={"reason": "user_requested_cancel"},
            )
        )
    )
    projection_state = asyncio.run(projection.get("run-8b"))

    assert projection_state.run_id == "run-8b"
    assert projection_state.lifecycle_state == "aborted"
    assert not projection_state.waiting_external
    assert not projection_state.ready_for_dispatch
    assert projection_state.recovery_mode == "abort"
    assert projection_state.recovery_reason == "user_requested_cancel"


def test_cancel_requested_keeps_aborted_after_recovery_succeeded_event() -> None:
    """Cancel terminal state must win over later recovery success facts."""
    event_log = InMemoryKernelRuntimeEventLog()
    projection = InMemoryDecisionProjectionService(event_log)

    asyncio.run(
        event_log.append_action_commit(
            _build_commit(
                "run-8c",
                "c1",
                "run.cancel_requested",
                payload_json={"reason": "user_requested_cancel"},
            )
        )
    )
    asyncio.run(
        event_log.append_action_commit(
            _build_commit(
                "run-8c",
                "c2",
                "run.recovery_succeeded",
            )
        )
    )

    projection_state = asyncio.run(projection.get("run-8c"))
    assert projection_state.lifecycle_state == "aborted"
    assert not projection_state.ready_for_dispatch
    assert projection_state.recovery_mode == "abort"
    assert projection_state.recovery_reason == "user_requested_cancel"


def test_cancel_requested_keeps_aborted_after_signal_callback_event() -> None:
    """Cancel terminal invariant must block signal-driven ready state."""
    event_log = InMemoryKernelRuntimeEventLog()
    projection = InMemoryDecisionProjectionService(event_log)

    asyncio.run(
        event_log.append_action_commit(
            _build_commit(
                "run-8d",
                "c1",
                "run.cancel_requested",
                payload_json={"reason": "operator_cancel"},
            )
        )
    )
    asyncio.run(event_log.append_action_commit(_build_commit("run-8d", "c2", "signal.callback")))

    projection_state = asyncio.run(projection.get("run-8d"))
    assert projection_state.lifecycle_state == "aborted"
    assert not projection_state.ready_for_dispatch
    assert projection_state.recovery_mode == "abort"
    assert projection_state.recovery_reason == "operator_cancel"


def test_cancel_requested_keeps_aborted_after_recovery_aborted_and_callback_events() -> None:
    """Cancel terminal invariant must ignore later abort and callback facts."""
    event_log = InMemoryKernelRuntimeEventLog()
    projection = InMemoryDecisionProjectionService(event_log)

    asyncio.run(
        event_log.append_action_commit(
            _build_commit(
                "run-8e",
                "c1",
                "run.cancel_requested",
                payload_json={"reason": "user_requested_cancel"},
            )
        )
    )
    asyncio.run(
        event_log.append_action_commit(
            _build_commit(
                "run-8e",
                "c2",
                "run.recovery_aborted",
                payload_json={"reason": "late_recovery_abort"},
            )
        )
    )
    asyncio.run(event_log.append_action_commit(_build_commit("run-8e", "c3", "signal.callback")))

    projection_state = asyncio.run(projection.get("run-8e"))
    assert projection_state.lifecycle_state == "aborted"
    assert not projection_state.ready_for_dispatch
    assert projection_state.recovery_mode == "abort"
    assert projection_state.recovery_reason == "user_requested_cancel"


def test_operational_priority_matrix_hard_failure_overrides_prior_timeout() -> None:
    """HardFailure must dominate Timeout in projection replay order conflicts."""
    event_log = InMemoryKernelRuntimeEventLog()
    projection = InMemoryDecisionProjectionService(event_log)

    asyncio.run(
        event_log.append_action_commit(
            _build_commit(
                "run-priority-1",
                "c1",
                "run.waiting_external",
                payload_json={"reason": "timeout"},
            )
        )
    )
    asyncio.run(
        event_log.append_action_commit(
            _build_commit(
                "run-priority-1",
                "c2",
                "run.recovery_aborted",
                payload_json={"reason": "fatal_executor_failure"},
            )
        )
    )

    state = asyncio.run(projection.get("run-priority-1"))
    assert state.lifecycle_state == "aborted"
    assert not state.waiting_external
    assert not state.ready_for_dispatch
    assert state.recovery_mode == "abort"
    assert state.recovery_reason == "fatal_executor_failure"


def test_operational_priority_matrix_timeout_blocks_later_external_callback() -> None:
    """Timeout must dominate ExternalCallback when timeout already materialized."""
    event_log = InMemoryKernelRuntimeEventLog()
    projection = InMemoryDecisionProjectionService(event_log)

    asyncio.run(
        event_log.append_action_commit(
            _build_commit(
                "run-priority-2",
                "c1",
                "run.waiting_external",
                payload_json={"reason": "timeout"},
            )
        )
    )
    asyncio.run(
        event_log.append_action_commit(
            _build_commit(
                "run-priority-2",
                "c2",
                "signal.external_callback",
                payload_json={"status": "late_done"},
            )
        )
    )

    state = asyncio.run(projection.get("run-priority-2"))
    assert state.lifecycle_state == "waiting_external"
    assert state.waiting_external
    assert not state.ready_for_dispatch
    assert state.recovery_mode == "human_escalation"
    assert state.recovery_reason == "timeout"


def test_operational_priority_matrix_cancel_overrides_prior_hard_failure() -> None:
    """Cancel must override an already-materialized hard-failure abort reason."""
    event_log = InMemoryKernelRuntimeEventLog()
    projection = InMemoryDecisionProjectionService(event_log)

    asyncio.run(
        event_log.append_action_commit(
            _build_commit(
                "run-priority-3",
                "c1",
                "run.recovery_aborted",
                payload_json={"reason": "hard_failure"},
            )
        )
    )
    asyncio.run(
        event_log.append_action_commit(
            _build_commit(
                "run-priority-3",
                "c2",
                "run.cancel_requested",
                payload_json={"reason": "operator_cancel"},
            )
        )
    )

    state = asyncio.run(projection.get("run-priority-3"))
    assert state.lifecycle_state == "aborted"
    assert not state.waiting_external
    assert not state.ready_for_dispatch
    assert state.recovery_mode == "abort"
    assert state.recovery_reason == "operator_cancel"


def test_operational_priority_matrix_full_chain_resolves_to_cancel() -> None:
    """Full chain must resolve by v6.4 precedence: Cancel > HardFailure > Timeout > Callback."""
    event_log = InMemoryKernelRuntimeEventLog()
    projection = InMemoryDecisionProjectionService(event_log)

    asyncio.run(
        event_log.append_action_commit(
            _build_commit(
                "run-priority-4",
                "c1",
                "signal.external_callback",
                payload_json={"status": "done"},
            )
        )
    )
    asyncio.run(
        event_log.append_action_commit(
            _build_commit(
                "run-priority-4",
                "c2",
                "run.waiting_external",
                payload_json={"reason": "timeout"},
            )
        )
    )
    asyncio.run(
        event_log.append_action_commit(
            _build_commit(
                "run-priority-4",
                "c3",
                "run.recovery_aborted",
                payload_json={"reason": "hard_failure"},
            )
        )
    )
    asyncio.run(
        event_log.append_action_commit(
            _build_commit(
                "run-priority-4",
                "c4",
                "run.cancel_requested",
                payload_json={"reason": "user_requested_cancel"},
            )
        )
    )

    state = asyncio.run(projection.get("run-priority-4"))
    assert state.lifecycle_state == "aborted"
    assert not state.waiting_external
    assert not state.ready_for_dispatch
    assert state.recovery_mode == "abort"
    assert state.recovery_reason == "user_requested_cancel"
    assert state.projected_offset == 4


def test_recovering_event_moves_projection_into_recovering_state() -> None:
    """run.recovering event should move projection into recovering state."""
    event_log = InMemoryKernelRuntimeEventLog()
    projection = InMemoryDecisionProjectionService(event_log)

    asyncio.run(event_log.append_action_commit(_build_commit("run-9", "c1", "run.recovering")))
    state = asyncio.run(projection.get("run-9"))

    assert state.run_id == "run-9"
    assert state.lifecycle_state == "recovering"
    assert state.recovery_mode == "static_compensation"
    assert state.recovery_reason is None


def test_waiting_external_event_preserves_recovery_reason_from_payload() -> None:
    """run.waiting_external should expose reason as recovery_reason."""
    event_log = InMemoryKernelRuntimeEventLog()
    projection = InMemoryDecisionProjectionService(event_log)
    commit = ActionCommit(
        run_id="run-10",
        commit_id="c1",
        created_at="2026-03-31T00:00:00Z",
        events=[
            RuntimeEvent(
                run_id="run-10",
                event_id="evt-c1",
                commit_offset=0,
                event_type="run.waiting_external",
                event_class="fact",
                event_authority="authoritative_fact",
                ordering_key="run-10",
                wake_policy="wake_actor",
                payload_json={"reason": "requires_manual_review"},
                created_at="2026-03-31T00:00:00Z",
            )
        ],
    )

    asyncio.run(event_log.append_action_commit(commit))
    state = asyncio.run(projection.get("run-10"))
    assert state.lifecycle_state == "waiting_external"
    assert state.recovery_mode == "human_escalation"
    assert state.recovery_reason == "requires_manual_review"


def test_reconcile_failed_event_moves_projection_to_waiting_external() -> None:
    """reconcile.failed should move projection to waiting_external."""
    event_log = InMemoryKernelRuntimeEventLog()
    projection = InMemoryDecisionProjectionService(event_log)

    asyncio.run(event_log.append_action_commit(_build_commit("run-10b", "c1", "run.ready")))
    asyncio.run(
        event_log.append_action_commit(
            _build_commit(
                "run-10b",
                "c2",
                "reconcile.failed",
                payload_json={"reason": "state_mismatch"},
            )
        )
    )

    state = asyncio.run(projection.get("run-10b"))
    assert state.lifecycle_state == "waiting_external"
    assert state.waiting_external
    assert not state.ready_for_dispatch
    assert state.recovery_mode == "human_escalation"
    assert state.recovery_reason == "state_mismatch"


def test_non_failed_reconcile_event_only_advances_projection_offset() -> None:
    """Non-failed reconcile events should not change lifecycle semantics."""
    event_log = InMemoryKernelRuntimeEventLog()
    projection = InMemoryDecisionProjectionService(event_log)

    asyncio.run(event_log.append_action_commit(_build_commit("run-10c", "c1", "run.ready")))
    asyncio.run(
        event_log.append_action_commit(_build_commit("run-10c", "c2", "reconcile.succeeded"))
    )

    state = asyncio.run(projection.get("run-10c"))
    assert state.lifecycle_state == "ready"
    assert not state.waiting_external
    assert state.ready_for_dispatch
    assert state.projected_offset == 2


def test_derived_diagnostic_event_is_skipped_by_projection_authority_path() -> None:
    """derived_diagnostic events must be silently skipped during projection replay.

    The architecture invariant: derived_diagnostic events are "never replayed".
    They may appear in the same ActionCommit as authoritative facts (e.g.
    recovery.plan_selected alongside a recovery lifecycle fact) and must not
    raise errors — they are simply excluded from projection state transitions.
    """
    event_log = InMemoryKernelRuntimeEventLog()
    projection = InMemoryDecisionProjectionService(event_log)

    asyncio.run(
        event_log.append_action_commit(
            ActionCommit(
                run_id="run-10d",
                commit_id="c1",
                created_at="2026-03-31T00:00:00Z",
                events=[
                    RuntimeEvent(
                        run_id="run-10d",
                        event_id="evt-c1",
                        commit_offset=0,
                        event_type="run.ready",
                        event_class="derived",
                        event_authority="derived_diagnostic",
                        ordering_key="run-10d",
                        wake_policy="projection_only",
                        created_at="2026-03-31T00:00:00Z",
                    )
                ],
            )
        )
    )

    # derived_diagnostic event is skipped — projection stays at its default state.
    state = asyncio.run(projection.get("run-10d"))
    assert state.lifecycle_state == "created"
    assert state.projected_offset == 0


def test_projection_can_disable_derived_diagnostic_reject_guard_for_migration() -> None:
    """Projection guard should remain configurable for staged migration."""
    event_log = InMemoryKernelRuntimeEventLog()
    projection = InMemoryDecisionProjectionService(
        event_log,
        reject_derived_diagnostic_authority_input=False,
    )

    asyncio.run(
        event_log.append_action_commit(
            ActionCommit(
                run_id="run-10e",
                commit_id="c1",
                created_at="2026-03-31T00:00:00Z",
                events=[
                    RuntimeEvent(
                        run_id="run-10e",
                        event_id="evt-c1",
                        commit_offset=0,
                        event_type="run.ready",
                        event_class="derived",
                        event_authority="derived_diagnostic",
                        ordering_key="run-10e",
                        wake_policy="projection_only",
                        created_at="2026-03-31T00:00:00Z",
                    )
                ],
            )
        )
    )

    state = asyncio.run(projection.get("run-10e"))
    assert state.projected_offset == 1
    assert state.lifecycle_state == "ready"
    assert state.ready_for_dispatch


def test_projection_tracks_child_runs_for_spawn_and_completion_events() -> None:
    """Projection should add and remove child runs from lifecycle events."""
    event_log = InMemoryKernelRuntimeEventLog()
    projection = InMemoryDecisionProjectionService(event_log)

    asyncio.run(
        event_log.append_action_commit(
            _build_commit(
                "run-parent-1",
                "c1",
                "run.child_spawned",
                payload_json={"child_run_id": "child-1"},
            )
        )
    )
    asyncio.run(
        event_log.append_action_commit(
            _build_commit(
                "run-parent-1",
                "c2",
                "run.child_spawned",
                payload_json={"child_run_id": "child-1"},
            )
        )
    )
    asyncio.run(
        event_log.append_action_commit(
            _build_commit(
                "run-parent-1",
                "c3",
                "run.child_spawned",
                payload_json={"child_run_id": "child-2"},
            )
        )
    )
    asyncio.run(
        event_log.append_action_commit(
            _build_commit(
                "run-parent-1",
                "c4",
                "run.child_completed",
                payload_json={"child_run_id": "child-1"},
            )
        )
    )

    state = asyncio.run(projection.get("run-parent-1"))
    assert state.active_child_runs == ["child-2"]


def test_workflow_child_signals_update_parent_projection_child_runs() -> None:
    """Child lifecycle signals should update parent projection child run list."""
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

    asyncio.run(workflow.run(RunInput(run_id="run-parent-2")))
    asyncio.run(
        workflow.signal(
            ActorSignal(
                signal_type="child_spawned",
                signal_payload={"child_run_id": "child-a"},
                caused_by="spawn-child-a",
            )
        )
    )
    spawned_state = workflow.query()
    assert spawned_state.active_child_runs == ["child-a"]

    asyncio.run(
        workflow.signal(
            ActorSignal(
                signal_type="child_completed",
                signal_payload={"child_run_id": "child-a"},
                caused_by="complete-child-a",
            )
        )
    )
    completed_state = workflow.query()
    assert completed_state.active_child_runs == []


def test_workflow_failure_emits_recovery_event_and_updates_projection_reason() -> None:
    """Execution failure should propagate to recovery event and reason."""
    event_log = InMemoryKernelRuntimeEventLog()
    projection = InMemoryDecisionProjectionService(event_log)
    admission = StaticDispatchAdmissionService()
    recovery = StaticRecoveryGateService(
        StaticRecoveryPolicy(
            mode="human_escalation",
            reason_prefix="policy",
        )
    )
    deduper = InMemoryDecisionDeduper()

    async def _failing_handler(
        action: Action,
        grant_ref: str | None,
    ) -> dict[str, str]:
        del action, grant_ref
        raise RuntimeError("executor boom")

    executor = AsyncExecutorService(handler=_failing_handler)
    workflow = RunActorWorkflow(
        event_log=event_log,
        projection=projection,
        admission=admission,
        executor=executor,
        recovery=recovery,
        deduper=deduper,
    )

    asyncio.run(event_log.append_action_commit(_build_commit("run-11", "c1", "run.ready")))
    asyncio.run(workflow.run(RunInput(run_id="run-11")))

    failing_commit = ActionCommit(
        run_id="run-11",
        commit_id="c2",
        created_at="2026-03-31T00:00:00Z",
        action=Action(
            action_id="action-fail-1",
            run_id="run-11",
            action_type="web_research",
            effect_class="read_only",
            input_json={
                "query": "failure path",
                **_strict_snapshot_input_payload(),
            },
        ),
        events=[
            RuntimeEvent(
                run_id="run-11",
                event_id="evt-c2",
                commit_offset=2,
                event_type="action_committed",
                event_class="fact",
                event_authority="authoritative_fact",
                ordering_key="run-11",
                wake_policy="wake_actor",
                created_at="2026-03-31T00:00:00Z",
            )
        ],
    )
    asyncio.run(event_log.append_action_commit(failing_commit))

    with pytest.raises(RuntimeError, match="executor boom"):
        asyncio.run(workflow.process_action_commit(failing_commit))

    state = asyncio.run(projection.get("run-11"))
    assert state.lifecycle_state == "waiting_external"
    assert state.recovery_mode == "human_escalation"
    assert state.recovery_reason == "policy:runtimeerror"
