"""Unit tests for Temporal worker wiring and dependency lifecycle safety."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from agent_kernel.kernel.contracts import RunProjection
from agent_kernel.substrate.temporal.run_actor_workflow import (
    RunActorDependencyBundle,
    RunActorWorkflow,
    RunInput,
)
from agent_kernel.substrate.temporal.worker import TemporalKernelWorker, TemporalWorkerConfig


@dataclass(slots=True)
class _FakeWorkerInstance:
    """Simple worker instance with awaitable run method for unit tests."""

    run_fn: AsyncMock

    async def run(self) -> None:
        """Runs the test helper implementation."""
        await self.run_fn()


def test_worker_wires_task_queue_workflow_and_activities(monkeypatch: pytest.MonkeyPatch) -> None:
    """TemporalKernelWorker should pass configured task queue/workflow/activities."""
    worker_constructor_calls: list[dict] = []
    run_fn = AsyncMock()

    class _FakeWorkerClass:
        """Test suite for  FakeWorkerClass."""

        def __init__(self, client: object, **kwargs: object) -> None:
            """Initializes _FakeWorkerClass."""
            worker_constructor_calls.append({"client": client, "kwargs": kwargs})
            self._instance = _FakeWorkerInstance(run_fn=run_fn)

        async def run(self) -> None:
            """Runs the test helper implementation."""
            await self._instance.run()

    class _FakeWorkerModule:
        """Test suite for  FakeWorkerModule."""

        Worker = _FakeWorkerClass

    monkeypatch.setattr(
        "agent_kernel.substrate.temporal.worker.import_module",
        lambda module_name: _FakeWorkerModule if module_name == "temporalio.worker" else None,
    )

    client = object()
    activities = [lambda: None]
    workflows = [object]
    worker = TemporalKernelWorker(
        client=client,
        config=TemporalWorkerConfig(
            task_queue="worker-q",
            workflows=workflows,
            activities=activities,
        ),
    )
    asyncio.run(worker.run())

    assert len(worker_constructor_calls) == 1
    call = worker_constructor_calls[0]
    assert call["client"] is client
    assert call["kwargs"]["task_queue"] == "worker-q"
    assert call["kwargs"]["activities"] == activities
    assert call["kwargs"]["workflows"] == workflows
    run_fn.assert_awaited_once()


def test_worker_passes_optional_workflow_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """TemporalKernelWorker should forward workflow_runner when configured."""
    worker_constructor_calls: list[dict] = []

    class _FakeWorkerClass:
        """Test suite for  FakeWorkerClass."""

        def __init__(self, client: object, **kwargs: object) -> None:
            """Initializes _FakeWorkerClass."""
            worker_constructor_calls.append({"client": client, "kwargs": kwargs})

        async def run(self) -> None:
            """Runs the test helper implementation."""
            return None

    class _FakeWorkerModule:
        """Test suite for  FakeWorkerModule."""

        Worker = _FakeWorkerClass

    monkeypatch.setattr(
        "agent_kernel.substrate.temporal.worker.import_module",
        lambda module_name: _FakeWorkerModule if module_name == "temporalio.worker" else None,
    )
    runner = object()
    worker = TemporalKernelWorker(
        client=object(),
        config=TemporalWorkerConfig(
            task_queue="worker-runner-q",
            workflows=[object],
            workflow_runner=runner,
        ),
    )

    asyncio.run(worker.run())

    assert len(worker_constructor_calls) == 1
    assert worker_constructor_calls[0]["kwargs"]["workflow_runner"] is runner


def test_worker_clears_configured_dependencies_after_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Worker should clear configured workflow dependencies after run exits."""
    run_fn = AsyncMock()

    class _FakeWorkerClass:
        """Test suite for  FakeWorkerClass."""

        def __init__(self, client: object, **kwargs: object) -> None:
            """Initializes _FakeWorkerClass."""
            del client, kwargs

        async def run(self) -> None:
            """Runs the test helper implementation."""
            await run_fn()

    class _FakeWorkerModule:
        """Test suite for  FakeWorkerModule."""

        Worker = _FakeWorkerClass

    monkeypatch.setattr(
        "agent_kernel.substrate.temporal.worker.import_module",
        lambda module_name: _FakeWorkerModule if module_name == "temporalio.worker" else None,
    )

    projection = AsyncMock()
    projection.get.return_value = RunProjection(
        run_id="run-worker-dep-1",
        lifecycle_state="ready",
        projected_offset=1,
        waiting_external=False,
        ready_for_dispatch=True,
    )
    dependency_bundle = RunActorDependencyBundle(
        event_log=AsyncMock(),
        projection=projection,
        admission=AsyncMock(),
        executor=AsyncMock(),
        recovery=AsyncMock(),
        deduper=AsyncMock(),
    )

    worker = TemporalKernelWorker(
        client=object(),
        config=TemporalWorkerConfig(task_queue="cleanup-q"),
        dependencies=dependency_bundle,
    )
    asyncio.run(worker.run())
    run_fn.assert_awaited_once()

    # If worker cleanup failed, default workflow constructor would still use
    # the injected projection and increment this mock call count.
    workflow = RunActorWorkflow()
    asyncio.run(workflow.run(RunInput(run_id="run-worker-dep-1")))
    projection.get.assert_not_awaited()
