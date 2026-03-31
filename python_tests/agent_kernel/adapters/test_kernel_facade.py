"""Red tests for the agent_kernel KernelFacade contract."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock, call

import pytest

from agent_kernel.adapters.facade.kernel_facade import KernelFacade
from agent_kernel.kernel.contracts import (
    CancelRunRequest,
    QueryRunRequest,
    ResumeRunRequest,
    RunProjection,
    RuntimeEvent,
    SignalRunRequest,
    SpawnChildRunRequest,
    StartRunRequest,
)


async def _stream_stub(events: list[RuntimeEvent]):
    """Yields events from an in-memory list for facade stream tests."""
    for event in events:
        yield event


def test_start_run_uses_gateway_and_returns_facade_safe_shape() -> None:
    """KernelFacade must remain the only platform entrypoint."""

    gateway = AsyncMock()
    gateway.start_workflow.return_value = {
        "workflow_id": "temporal-run-1",
        "run_id": "run-1",
    }
    facade = KernelFacade(gateway)

    response = asyncio.run(
        facade.start_run(
            StartRunRequest(
                initiator="agent_core_runner",
                run_kind="research",
                session_id="session-1",
                input_json={"goal": "collect sources"},
            )
        )
    )

    gateway.start_workflow.assert_awaited_once()
    assert response.run_id == "run-1"
    assert response.temporal_workflow_id == "temporal-run-1"
    assert response.lifecycle_state == "created"


def test_start_run_binds_context_when_context_adapter_is_configured() -> None:
    """KernelFacade should bind context through adapter before start."""

    gateway = AsyncMock()
    gateway.start_workflow.return_value = {
        "workflow_id": "temporal-run-2",
    }
    context_adapter = Mock()
    context_adapter.bind_context.return_value = Mock(
        binding_ref="ctx-binding-1",
    )
    facade = KernelFacade(
        gateway,
        context_adapter=context_adapter,
    )

    response = asyncio.run(
        facade.start_run(
            StartRunRequest(
                initiator="agent_core_runner",
                run_kind="research",
                session_id="session-2",
                context_ref="ctx-2",
            )
        )
    )

    start_request = (
        gateway.start_workflow.await_args.args[0]
    )
    assert start_request.context_ref == "ctx-binding-1"
    context_adapter.bind_context.assert_called_once()
    context_adapter.bind_run_context.assert_called_once_with(
        response.run_id, "ctx-binding-1",
    )


def test_query_run_exports_projection_without_temporal_leakage() -> None:
    """KernelFacade must expose kernel truth, not raw Temporal."""

    gateway = AsyncMock()
    gateway.query_projection.return_value = RunProjection(
        run_id="run-1",
        lifecycle_state="waiting_external",
        projected_offset=8,
        waiting_external=True,
        ready_for_dispatch=False,
        current_action_id="action-1",
        recovery_mode="human_escalation",
        recovery_reason="needs operator",
    )
    facade = KernelFacade(gateway)

    response = asyncio.run(
        facade.query_run(QueryRunRequest(run_id="run-1"))
    )

    assert response.run_id == "run-1"
    assert response.lifecycle_state == "waiting_external"
    assert response.projected_offset == 8
    assert response.recovery_mode == "human_escalation"
    assert response.recovery_reason == "needs operator"
    assert not hasattr(response, "workflow_execution_handle")


def test_query_run_updates_checkpoint_adapter_when_configured() -> None:
    """KernelFacade should forward queried projection into adapter."""

    gateway = AsyncMock()
    projection = RunProjection(
        run_id="run-9",
        lifecycle_state="ready",
        projected_offset=9,
        waiting_external=False,
        ready_for_dispatch=True,
    )
    gateway.query_projection.return_value = projection
    checkpoint_adapter = Mock()
    facade = KernelFacade(
        gateway,
        checkpoint_adapter=checkpoint_adapter,
    )

    response = asyncio.run(
        facade.query_run(QueryRunRequest(run_id="run-9"))
    )

    checkpoint_adapter.bind_projection.assert_called_once_with(
        projection,
    )
    assert response.run_id == "run-9"


def test_query_run_dashboard_returns_dashboard_friendly_projection_view() -> None:
    """KernelFacade should export minimal dashboard read-model."""

    gateway = AsyncMock()
    gateway.query_projection.return_value = RunProjection(
        run_id="run-11",
        lifecycle_state="recovering",
        projected_offset=14,
        waiting_external=True,
        ready_for_dispatch=False,
        current_action_id="action-11",
        recovery_mode="static_compensation",
        recovery_reason="retry window",
        active_child_runs=["child-a", "child-b", "child-c"],
    )
    facade = KernelFacade(gateway)

    response = asyncio.run(
        facade.query_run_dashboard("run-11")
    )

    assert response.run_id == "run-11"
    assert response.lifecycle_state == "recovering"
    assert response.projected_offset == 14
    assert response.waiting_external is True
    assert response.recovery_mode == "static_compensation"
    assert response.recovery_reason == "retry window"
    assert response.active_child_runs_count == 3
    assert response.correlation_hint == "action:action-11"


def test_query_run_dashboard_uses_state_fallback_correlation_hint() -> None:
    """KernelFacade should provide deterministic fallback hints."""

    gateway = AsyncMock()
    gateway.query_projection.return_value = RunProjection(
        run_id="run-12",
        lifecycle_state="ready",
        projected_offset=15,
        waiting_external=False,
        ready_for_dispatch=True,
        active_child_runs=[],
    )
    facade = KernelFacade(gateway)

    response = asyncio.run(
        facade.query_run_dashboard("run-12")
    )

    assert response.correlation_hint == "state:ready:run-12"
    assert response.active_child_runs_count == 0


def test_spawn_child_run_routes_through_gateway_and_returns_child_view() -> None:
    """KernelFacade should start child workflow and signal parent."""

    gateway = AsyncMock()
    gateway.start_child_workflow.return_value = {
        "workflow_id": "temporal-child-1",
        "run_id": "child-run-1",
    }
    facade = KernelFacade(gateway)

    response = asyncio.run(
        facade.spawn_child_run(
            SpawnChildRunRequest(
                parent_run_id="run-parent",
                child_kind="verification",
                input_json={"source_count": 2},
            )
        )
    )

    gateway.start_child_workflow.assert_awaited_once()
    gateway.signal_workflow.assert_awaited_once()
    signal_call = gateway.signal_workflow.await_args.args
    assert signal_call[0] == "run-parent"
    assert signal_call[1].signal_type == "child_spawned"
    assert signal_call[1].signal_payload == {
        "child_run_id": "child-run-1",
        "correlation_id": "spawn_child:run-parent",
        "event_ref": "facade:spawn_child:run-parent",
    }
    assert (
        signal_call[1].caused_by
        == "kernel_facade.spawn_child_run"
    )
    assert response.child_run_id == "child-run-1"
    assert response.lifecycle_state == "created"


def test_spawn_child_run_prefers_child_projection_lifecycle_when_query_succeeds() -> None:
    """KernelFacade should return child projection lifecycle instead of hardcoded created."""
    gateway = AsyncMock()
    gateway.start_child_workflow.return_value = {
        "workflow_id": "temporal-child-2",
        "run_id": "child-run-2",
    }
    gateway.query_projection.return_value = RunProjection(
        run_id="child-run-2",
        lifecycle_state="ready",
        projected_offset=1,
        waiting_external=False,
        ready_for_dispatch=True,
    )
    facade = KernelFacade(gateway)

    response = asyncio.run(
        facade.spawn_child_run(
            SpawnChildRunRequest(
                parent_run_id="run-parent-2",
                child_kind="verification",
                input_json={"source_count": 1},
            )
        )
    )

    assert response.child_run_id == "child-run-2"
    assert response.lifecycle_state == "ready"


def test_signal_run_routes_callbacks_back_into_workflow() -> None:
    """KernelFacade must route signals into gateway unchanged."""

    gateway = AsyncMock()
    facade = KernelFacade(gateway)

    asyncio.run(
        facade.signal_run(
            SignalRunRequest(
                run_id="run-1",
                signal_type="external_callback",
                signal_payload={"status": "done"},
                caused_by="callback-1",
            )
        )
    )

    gateway.signal_workflow.assert_awaited_once()


def test_stream_run_events_defaults_to_authoritative_facts_only() -> None:
    """KernelFacade should stream authoritative facts by default."""
    gateway = AsyncMock()
    streamed_events = [
        RuntimeEvent(
            run_id="run-stream-1",
            event_id="evt-1",
            commit_offset=1,
            event_type="run.ready",
            event_class="fact",
            event_authority="authoritative_fact",
            ordering_key="run-stream-1",
            wake_policy="wake_actor",
            created_at="2026-04-01T00:00:00Z",
        ),
        RuntimeEvent(
            run_id="run-stream-1",
            event_id="evt-2",
            commit_offset=2,
            event_type="diagnostic.trace",
            event_class="derived",
            event_authority="derived_diagnostic",
            ordering_key="run-stream-1",
            wake_policy="projection_only",
            created_at="2026-04-01T00:00:00Z",
        ),
        RuntimeEvent(
            run_id="run-stream-1",
            event_id="evt-3",
            commit_offset=3,
            event_type="derived.replay",
            event_class="derived",
            event_authority="derived_replayable",
            ordering_key="run-stream-1",
            wake_policy="projection_only",
            created_at="2026-04-01T00:00:00Z",
        ),
    ]
    gateway.stream_run_events = Mock(
        return_value=_stream_stub(streamed_events),
    )
    facade = KernelFacade(gateway)

    async def _collect() -> list[RuntimeEvent]:
        collected_events: list[RuntimeEvent] = []
        async for event in facade.stream_run_events("run-stream-1"):
            collected_events.append(event)
        return collected_events

    collected_events = asyncio.run(_collect())

    gateway.stream_run_events.assert_called_once_with("run-stream-1")
    assert collected_events == [streamed_events[0]]


def test_stream_run_events_optionally_passes_derived_diagnostic_events() -> None:
    """KernelFacade should pass diagnostics when explicit opt-in is enabled."""
    gateway = AsyncMock()
    streamed_events = [
        RuntimeEvent(
            run_id="run-stream-2",
            event_id="evt-1",
            commit_offset=1,
            event_type="run.ready",
            event_class="fact",
            event_authority="authoritative_fact",
            ordering_key="run-stream-2",
            wake_policy="wake_actor",
            created_at="2026-04-01T00:00:00Z",
        ),
        RuntimeEvent(
            run_id="run-stream-2",
            event_id="evt-2",
            commit_offset=2,
            event_type="diagnostic.trace",
            event_class="derived",
            event_authority="derived_diagnostic",
            ordering_key="run-stream-2",
            wake_policy="projection_only",
            created_at="2026-04-01T00:00:00Z",
        ),
        RuntimeEvent(
            run_id="run-stream-2",
            event_id="evt-3",
            commit_offset=3,
            event_type="derived.replay",
            event_class="derived",
            event_authority="derived_replayable",
            ordering_key="run-stream-2",
            wake_policy="projection_only",
            created_at="2026-04-01T00:00:00Z",
        ),
    ]
    gateway.stream_run_events = Mock(
        return_value=_stream_stub(streamed_events),
    )
    facade = KernelFacade(gateway)

    async def _collect() -> list[RuntimeEvent]:
        collected_events: list[RuntimeEvent] = []
        async for event in facade.stream_run_events(
            "run-stream-2",
            include_derived_diagnostic=True,
        ):
            collected_events.append(event)
        return collected_events

    collected_events = asyncio.run(_collect())

    gateway.stream_run_events.assert_called_once_with("run-stream-2")
    assert collected_events == streamed_events[:2]


def test_cancel_run_signals_cancel_intent_before_workflow_cancel() -> None:
    """KernelFacade should signal cancel intent before hard cancel."""

    gateway = AsyncMock()
    facade = KernelFacade(gateway)

    asyncio.run(
        facade.cancel_run(
            CancelRunRequest(
                run_id="run-7",
                reason="operator_requested",
                caused_by="ops-console",
            )
        )
    )

    expected_signal = SignalRunRequest(
        run_id="run-7",
        signal_type="cancel_requested",
        signal_payload={
            "reason": "operator_requested",
            "correlation_id": "ops-console",
            "event_ref": "facade:cancel:run-7",
        },
        caused_by="ops-console",
    )
    gateway.signal_workflow.assert_awaited_once_with(
        "run-7", expected_signal,
    )
    gateway.cancel_workflow.assert_awaited_once_with(
        "run-7", "operator_requested",
    )
    assert gateway.mock_calls == [
        call.signal_workflow("run-7", expected_signal),
        call.cancel_workflow(
            "run-7", "operator_requested",
        ),
    ]


def test_resume_run_routes_checkpoint_resume_into_workflow_signal() -> None:
    """KernelFacade should map resume input via adapter and signal."""

    gateway = AsyncMock()
    checkpoint_adapter = AsyncMock()
    checkpoint_adapter.import_resume_request.return_value = Mock(
        run_id="run-1",
        snapshot_id="snapshot:run-1:8",
        snapshot_offset=8,
    )
    facade = KernelFacade(
        gateway,
        checkpoint_adapter=checkpoint_adapter,
    )

    asyncio.run(
        facade.resume_run(
            ResumeRunRequest(
                run_id="run-1",
                snapshot_id="snapshot:run-1:8",
                caused_by="operator",
            )
        )
    )

    checkpoint_adapter.import_resume_request.assert_awaited_once()
    gateway.signal_workflow.assert_awaited_once()
    signal_request = (
        gateway.signal_workflow.await_args.args[1]
    )
    assert signal_request.run_id == "run-1"
    assert (
        signal_request.signal_type
        == "resume_from_snapshot"
    )
    assert signal_request.signal_payload == {
        "snapshot_id": "snapshot:run-1:8",
        "snapshot_offset": 8,
        "correlation_id": "operator",
        "event_ref": "facade:resume:run-1",
    }
    assert signal_request.caused_by == "operator"


def test_cancel_run_uses_deterministic_correlation_when_caused_by_missing() -> None:
    """KernelFacade should fallback observability ids."""

    gateway = AsyncMock()
    facade = KernelFacade(gateway)

    asyncio.run(
        facade.cancel_run(
            CancelRunRequest(
                run_id="run-8",
                reason="operator_requested",
            )
        )
    )

    signal_request = (
        gateway.signal_workflow.await_args.args[1]
    )
    assert signal_request.signal_payload is not None
    assert (
        signal_request.signal_payload["correlation_id"]
        == "cancel:run-8"
    )
    assert (
        signal_request.signal_payload["event_ref"]
        == "facade:cancel:run-8"
    )


def test_cancel_run_tolerates_expected_completion_race() -> None:
    """Cancel should tolerate expected race where workflow already completed."""
    gateway = AsyncMock()
    gateway.cancel_workflow.side_effect = RuntimeError("workflow already completed")
    facade = KernelFacade(gateway)

    asyncio.run(
        facade.cancel_run(
            CancelRunRequest(
                run_id="run-race",
                reason="operator_requested",
            )
        )
    )

    gateway.signal_workflow.assert_awaited_once()
    gateway.cancel_workflow.assert_awaited_once()


def test_resume_run_uses_deterministic_correlation_when_caused_by_missing() -> None:
    """KernelFacade should fallback resume ids without cause."""

    gateway = AsyncMock()
    checkpoint_adapter = AsyncMock()
    checkpoint_adapter.import_resume_request.return_value = Mock(
        run_id="run-2",
        snapshot_id="snapshot:run-2:3",
        snapshot_offset=3,
    )
    facade = KernelFacade(
        gateway,
        checkpoint_adapter=checkpoint_adapter,
    )

    asyncio.run(
        facade.resume_run(
            ResumeRunRequest(
                run_id="run-2",
                snapshot_id="snapshot:run-2:3",
            )
        )
    )

    signal_request = (
        gateway.signal_workflow.await_args.args[1]
    )
    assert signal_request.signal_payload is not None
    assert (
        signal_request.signal_payload["correlation_id"]
        == "resume:run-2"
    )
    assert (
        signal_request.signal_payload["event_ref"]
        == "facade:resume:run-2"
    )


def test_resume_run_requires_checkpoint_adapter() -> None:
    """KernelFacade should fail fast when resume lacks adapter."""

    gateway = AsyncMock()
    facade = KernelFacade(gateway)

    with pytest.raises(
        RuntimeError,
        match=r"checkpoint_adapter is required for resume_run",
    ):
        asyncio.run(
            facade.resume_run(
                ResumeRunRequest(run_id="run-1")
            )
        )


def test_escalate_recovery_signals_workflow_with_observability_payload() -> None:
    """KernelFacade should route recovery escalation with stable ids."""

    gateway = AsyncMock()
    facade = KernelFacade(gateway)

    asyncio.run(
        facade.escalate_recovery(
            run_id="run-10",
            reason="manual operator triage required",
            caused_by="recovery-gate",
        )
    )

    expected_signal = SignalRunRequest(
        run_id="run-10",
        signal_type="recovery_escalation",
        signal_payload={
            "reason": "manual operator triage required",
            "correlation_id": "recovery-gate",
            "event_ref": (
                "facade:recovery_escalation:run-10"
            ),
        },
        caused_by="recovery-gate",
    )
    gateway.signal_workflow.assert_awaited_once_with(
        "run-10", expected_signal,
    )
