"""Verifies for temporal sdk gateway request/response mapping behavior."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from agent_kernel.kernel.contracts import (
    QueryRunResponse,
    RunProjection,
    RuntimeEvent,
    SignalRunRequest,
    SpawnChildRunRequest,
    StartRunRequest,
)
from agent_kernel.substrate.temporal.gateway import (
    TemporalGatewayConfig,
    TemporalSDKWorkflowGateway,
)


@dataclass(slots=True)
class _FakeHandle:
    """Test suite for  FakeHandle."""

    query_result: Any = None
    signal_calls: list[tuple[Any, Any]] = field(default_factory=list)
    query_calls: list[Any] = field(default_factory=list)
    cancel_calls: list[str] = field(default_factory=list)

    async def signal(self, signal_fn: Any, payload: Any) -> None:
        """Signals the fake workflow handle."""
        self.signal_calls.append((signal_fn, payload))

    async def query(self, query_fn: Any) -> Any:
        """Returns query data from the fake workflow handle."""
        self.query_calls.append(query_fn)
        return self.query_result

    async def cancel(self, reason: str) -> None:
        """Cancels the fake workflow handle."""
        self.cancel_calls.append(reason)


@dataclass(slots=True)
class _FakeClient:
    """Test suite for  FakeClient."""

    start_calls: list[dict[str, Any]] = field(default_factory=list)
    handles: dict[str, _FakeHandle] = field(default_factory=dict)

    async def start_workflow(self, workflow_fn: Any, run_input: Any, **kwargs: Any) -> None:
        """Start workflow."""
        self.start_calls.append(
            {
                "workflow_fn": workflow_fn,
                "run_input": run_input,
                "kwargs": kwargs,
            }
        )

    def get_workflow_handle(self, workflow_id: str) -> _FakeHandle:
        """Get workflow handle."""
        if workflow_id not in self.handles:
            self.handles[workflow_id] = _FakeHandle()
        return self.handles[workflow_id]


@dataclass(slots=True)
class _NoReasonCancelHandle:
    """Test suite for  NoReasonCancelHandle."""

    cancel_called: bool = False

    async def signal(self, signal_fn: Any, payload: Any) -> None:
        """Signals the fake workflow handle."""
        del signal_fn, payload

    async def query(self, query_fn: Any) -> Any:
        """Returns query data from the fake workflow handle."""
        del query_fn
        return None

    async def cancel(self) -> None:
        """Cancels the fake workflow handle."""
        self.cancel_called = True


def test_start_workflow_builds_workflow_id_and_task_queue() -> None:
    """Verifies start workflow builds workflow id and task queue."""
    client = _FakeClient()
    gateway = TemporalSDKWorkflowGateway(
        client,
        TemporalGatewayConfig(task_queue="kernel-q", workflow_id_prefix="run"),
    )

    response = asyncio.run(
        gateway.start_workflow(
            StartRunRequest(
                initiator="agent_core_runner",
                run_kind="research",
                session_id="session-1",
                input_json={"run_id": "run-1"},
            )
        )
    )

    assert response["workflow_id"] == "run:run-1"
    assert response["run_id"] == "run-1"
    assert len(client.start_calls) == 1
    assert client.start_calls[0]["kwargs"]["task_queue"] == "kernel-q"
    assert client.start_calls[0]["kwargs"]["id"] == "run:run-1"


def test_signal_and_cancel_route_to_workflow_handle() -> None:
    """Verifies signal and cancel route to workflow handle."""
    client = _FakeClient()
    gateway = TemporalSDKWorkflowGateway(client)

    asyncio.run(
        gateway.signal_workflow(
            "run-1",
            SignalRunRequest(
                run_id="run-1",
                signal_type="callback",
                signal_payload={"ok": True},
                caused_by="cb-1",
            ),
        )
    )
    asyncio.run(gateway.cancel_workflow("run-1", "manual abort"))

    handle = client.get_workflow_handle("run:run-1")
    assert len(handle.signal_calls) == 1
    assert handle.signal_calls[0][1]["signal_type"] == "callback"
    assert handle.cancel_calls == ["manual abort"]


def test_cancel_workflow_falls_back_when_sdk_handle_does_not_accept_reason() -> None:
    """Gateway should support SDK variants where handle.cancel() has no reason arg."""
    client = _FakeClient()
    no_reason_handle = _NoReasonCancelHandle()
    client.handles["run:run-no-reason"] = no_reason_handle
    gateway = TemporalSDKWorkflowGateway(client)

    asyncio.run(gateway.cancel_workflow("run-no-reason", "manual abort"))

    assert no_reason_handle.cancel_called is True


def test_query_projection_accepts_kernel_projection_payload() -> None:
    """Verifies query projection accepts kernel projection payload."""
    client = _FakeClient()
    handle = client.get_workflow_handle("run:run-2")
    handle.query_result = RunProjection(
        run_id="run-2",
        lifecycle_state="ready",
        projected_offset=5,
        waiting_external=False,
        ready_for_dispatch=True,
    )
    gateway = TemporalSDKWorkflowGateway(client)

    projection = asyncio.run(gateway.query_projection("run-2"))
    assert projection.run_id == "run-2"
    assert projection.lifecycle_state == "ready"
    assert projection.projected_offset == 5


def test_query_projection_accepts_query_run_response_payload() -> None:
    """Verifies query projection accepts query run response payload."""
    client = _FakeClient()
    handle = client.get_workflow_handle("run:run-3")
    handle.query_result = QueryRunResponse(
        run_id="run-3",
        lifecycle_state="waiting_external",
        projected_offset=7,
        waiting_external=True,
        recovery_mode="human_escalation",
        recovery_reason="manual review",
    )
    gateway = TemporalSDKWorkflowGateway(client)

    projection = asyncio.run(gateway.query_projection("run-3"))
    assert projection.run_id == "run-3"
    assert projection.lifecycle_state == "waiting_external"
    assert projection.projected_offset == 7
    assert projection.recovery_mode == "human_escalation"
    assert projection.recovery_reason == "manual review"


def test_start_child_workflow_uses_child_workflow_id_namespace() -> None:
    """Verifies start child workflow uses child workflow id namespace."""
    client = _FakeClient()
    gateway = TemporalSDKWorkflowGateway(client)

    response = asyncio.run(
        gateway.start_child_workflow(
            "parent-1",
            SpawnChildRunRequest(
                parent_run_id="parent-1",
                child_kind="verification",
                input_json={"child_run_id": "child-1"},
            ),
        )
    )

    assert response["workflow_id"] == "run:child:parent-1:child-1"
    assert response["run_id"] == "child-1"
    assert client.start_calls[0]["kwargs"]["id"] == "run:child:parent-1:child-1"


def test_stream_run_events_raises_when_query_hook_not_configured() -> None:
    """Gateway must raise RuntimeError when event_stream_query_method_name is not set.

    Silently yielding an empty iterator would cause silent data loss for all
    downstream consumers (SSE endpoint, reconcile loop, branch progress
    tracking).  The correct behaviour is a loud, actionable RuntimeError so
    operators know to configure the field rather than silently receiving no
    events.
    """
    client = _FakeClient()
    gateway = TemporalSDKWorkflowGateway(client)

    async def _collect() -> list[Any]:
        """Collects values for assertion."""
        collected_events: list[Any] = []
        async for event in gateway.stream_run_events("run-stream-2"):
            collected_events.append(event)
        return collected_events

    with pytest.raises(RuntimeError, match="event_stream_query_method_name is not set"):
        asyncio.run(_collect())


def test_stream_run_events_uses_query_hook_and_normalizes_payload() -> None:
    """Gateway should stream events from configured query hook payload."""
    client = _FakeClient()
    handle = client.get_workflow_handle("run:run-stream-3")
    handle.query_result = {
        "events": [
            RuntimeEvent(
                run_id="run-stream-3",
                event_id="evt-1",
                commit_offset=1,
                event_type="run.ready",
                event_class="fact",
                event_authority="authoritative_fact",
                ordering_key="run-stream-3",
                wake_policy="wake_actor",
                created_at="2026-04-01T00:00:00Z",
            ),
            {
                "run_id": "run-stream-3",
                "event_id": "evt-2",
                "commit_offset": 2,
                "event_type": "diagnostic.trace",
                "event_class": "derived",
                "event_authority": "derived_diagnostic",
                "ordering_key": "run-stream-3",
                "wake_policy": "projection_only",
                "created_at": "2026-04-01T00:00:00Z",
                "payload_json": {"trace_id": "trace-2"},
            },
        ]
    }
    gateway = TemporalSDKWorkflowGateway(
        client,
        TemporalGatewayConfig(
            event_stream_query_method_name="query_runtime_events",
        ),
    )

    async def _collect() -> list[RuntimeEvent]:
        """Collects values for assertion."""
        collected_events: list[RuntimeEvent] = []
        async for event in gateway.stream_run_events("run-stream-3"):
            collected_events.append(event)
        return collected_events

    collected_events = asyncio.run(_collect())

    assert handle.query_calls == ["query_runtime_events"]
    assert [event.event_id for event in collected_events] == ["evt-1", "evt-2"]
    assert collected_events[1].payload_json == {"trace_id": "trace-2"}
