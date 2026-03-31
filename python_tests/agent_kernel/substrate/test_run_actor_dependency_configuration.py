"""Tests for RunActorWorkflow dependency configuration behavior."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from agent_kernel.kernel.contracts import RunProjection
from agent_kernel.substrate.temporal.run_actor_workflow import (
    RunActorDependencyBundle,
    RunActorWorkflow,
    RunInput,
    configure_run_actor_dependencies,
)


def test_workflow_can_run_with_default_dependencies_when_not_configured() -> None:
    """RunActorWorkflow should be constructible without explicit dependencies."""
    configure_run_actor_dependencies(None)
    workflow = RunActorWorkflow()

    result = asyncio.run(workflow.run(RunInput(run_id="run-default-1")))

    assert result["run_id"] == "run-default-1"
    assert result["final_state"] == "completed"


def test_workflow_uses_configured_dependencies_when_no_explicit_args() -> None:
    """Configured dependency bundle should back default workflow constructor."""
    event_log = AsyncMock()
    projection = AsyncMock()
    projection.get.return_value = RunProjection(
        run_id="run-configured-1",
        lifecycle_state="ready",
        projected_offset=5,
        waiting_external=False,
        ready_for_dispatch=True,
    )
    admission = AsyncMock()
    executor = AsyncMock()
    recovery = AsyncMock()
    deduper = AsyncMock()
    configure_run_actor_dependencies(
        RunActorDependencyBundle(
            event_log=event_log,
            projection=projection,
            admission=admission,
            executor=executor,
            recovery=recovery,
            deduper=deduper,
        )
    )
    workflow = RunActorWorkflow()

    result = asyncio.run(workflow.run(RunInput(run_id="run-configured-1")))

    projection.get.assert_awaited_once_with("run-configured-1")
    assert result["final_state"] == "completed"
    configure_run_actor_dependencies(None)
