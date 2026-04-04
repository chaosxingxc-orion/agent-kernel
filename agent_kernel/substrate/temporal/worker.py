"""Temporal worker bootstrap for the agent_kernel kernel.

This module wires the Temporal worker with kernel services and
starts polling for workflow and activity tasks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import import_module
from typing import Any

from agent_kernel.substrate.temporal.run_actor_workflow import (
    RunActorDependencyBundle,
    RunActorWorkflow,
    clear_run_actor_dependencies,
    configure_run_actor_dependencies,
)


@dataclass(frozen=True, slots=True)
class TemporalWorkerConfig:
    """Settings for Temporal worker runtime.

    Attributes:
        task_queue: Task queue that worker will poll.
        workflows: Workflow classes registered on this worker.
        activities: Optional list of activity callables to register.
        workflow_runner: Optional Temporal workflow runner override.
    """

    task_queue: str = "agent-kernel"
    workflows: list[Any] = field(default_factory=lambda: [RunActorWorkflow])
    activities: list[Any] = field(default_factory=list)
    workflow_runner: Any | None = None


class TemporalKernelWorker:
    """Bootstraps a Temporal worker hosting kernel workflow entrypoints."""

    def __init__(
        self,
        client: Any,
        config: TemporalWorkerConfig | None = None,
        dependencies: RunActorDependencyBundle | None = None,
    ) -> None:
        """Initializes worker with Temporal client and optional config.

        Args:
            client: Temporal SDK client instance.
            config: Optional worker configuration override.
            dependencies: Optional workflow dependency bundle.
        """
        self._client = client
        self._config = config or TemporalWorkerConfig()
        self._dependencies = dependencies

    async def run(self) -> None:
        """Runs Temporal worker loop for kernel workflows.

        Raises:
            RuntimeError: If Temporal Python SDK is not installed.
        """
        try:
            worker_module = import_module("temporalio.worker")
            worker_cls = worker_module.Worker
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("Temporal SDK is required. Install dependency: temporalio") from exc

        configure_run_actor_dependencies(self._dependencies)
        try:
            worker_kwargs = {
                "task_queue": self._config.task_queue,
                "workflows": self._config.workflows,
                "activities": self._config.activities,
            }
            if self._config.workflow_runner is not None:
                worker_kwargs["workflow_runner"] = self._config.workflow_runner
            worker = worker_cls(self._client, **worker_kwargs)
            try:
                await worker.run()
            finally:
                # Drain in-flight workflow/activity tasks before exit so that
                # Turn state is not written half-way on rolling deploys (D-M7).
                # worker.shutdown() is a no-op when worker.run() exits normally.
                shutdown = getattr(worker, "shutdown", None)
                if callable(shutdown):
                    await shutdown()
        finally:
            # Always clear process-local workflow dependency override so
            # subsequent worker/test runs do not accidentally reuse it.
            clear_run_actor_dependencies(self._dependencies)
