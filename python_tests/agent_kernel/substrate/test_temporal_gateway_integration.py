"""Integration tests for Temporal gateway using local Temporal test env."""

from __future__ import annotations

from typing import Any

import pytest

from agent_kernel.kernel.contracts import SignalRunRequest, StartRunRequest
from agent_kernel.substrate.temporal.gateway import (
    TemporalGatewayConfig,
    TemporalSDKWorkflowGateway,
)

try:
    from temporalio import workflow as temporal_workflow
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import UnsandboxedWorkflowRunner, Worker

    TEMPORAL_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency in CI
    temporal_workflow = None
    WorkflowEnvironment = None
    Worker = None
    UnsandboxedWorkflowRunner = None
    TEMPORAL_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not TEMPORAL_AVAILABLE,
    reason="temporalio SDK is not installed in this environment.",
)

if TEMPORAL_AVAILABLE:

    @temporal_workflow.defn
    class GatewaySmokeWorkflow:
        """Small workflow used to validate gateway integration paths."""

        def __init__(self) -> None:
            """Initializes GatewaySmokeWorkflow."""
            self._projection = {
                "run_id": "unknown",
                "lifecycle_state": "created",
                "projected_offset": 0,
                "waiting_external": False,
                "ready_for_dispatch": False,
                "active_child_runs": [],
            }

        @temporal_workflow.run
        async def run(self, run_input: Any) -> str:
            """Runs the test helper implementation."""
            self._projection["lifecycle_state"] = "ready"
            if isinstance(run_input, dict):
                self._projection["run_id"] = str(run_input.get("run_id", "run-x"))
            else:
                self._projection["run_id"] = str(getattr(run_input, "run_id", "run-x"))
            return "started"

        @temporal_workflow.signal
        async def signal(self, payload: dict) -> None:
            """Signals the fake workflow handle."""
            del payload
            self._projection["projected_offset"] += 1
            self._projection["waiting_external"] = False

        @temporal_workflow.query
        def query(self) -> dict:
            """Returns query data from the fake workflow handle."""
            return self._projection

else:

    class GatewaySmokeWorkflow:  # pragma: no cover - fallback for type checkers
        """Fallback class used when temporalio is unavailable."""


@pytest.mark.asyncio
async def test_gateway_runs_against_temporal_test_environment() -> None:
    """Gateway should start workflow and query projection in Temporal test env."""
    assert WorkflowEnvironment is not None
    assert Worker is not None
    assert UnsandboxedWorkflowRunner is not None

    async with await WorkflowEnvironment.start_time_skipping() as env:
        client = env.client
        config = TemporalGatewayConfig(
            task_queue="gateway-int-q",
            workflow_run_callable=GatewaySmokeWorkflow.run,
            signal_method_name="signal",
            query_method_name="query",
        )
        gateway = TemporalSDKWorkflowGateway(client, config)

        async with Worker(
            client,
            task_queue="gateway-int-q",
            workflows=[GatewaySmokeWorkflow],
            workflow_runner=UnsandboxedWorkflowRunner(),
        ):
            await gateway.start_workflow(
                StartRunRequest(
                    initiator="system",
                    run_kind="smoke",
                    input_json={"run_id": "run-int-1"},
                )
            )
            await gateway.signal_workflow(
                "run-int-1",
                SignalRunRequest(run_id="run-int-1", signal_type="callback"),
            )
            projection = await gateway.query_projection("run-int-1")

            assert projection.run_id == "run-int-1"
            assert projection.lifecycle_state == "ready"
            assert projection.projected_offset >= 1
