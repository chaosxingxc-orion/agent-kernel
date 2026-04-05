"""LocalFSMAdaptor 鈥?drives RunActorWorkflow directly in asyncio.

This substrate requires no external processes.  It is suitable for:
  - Local development and debugging
  - Embedded deployments (single-process, no network dependencies)
  - Unit and integration tests that do not need Temporal's durability guarantees

What it provides:
  - Full six-authority lifecycle (same TurnEngine, same services)
  - Parallel execution via PlanExecutor / asyncio.gather
  - All observability hooks and OTel export
  - In-process signal routing via asyncio (no Temporal SDK required)

What it does NOT provide (compared to TemporalAdaptor):
  - Durable execution across process restarts
  - Cross-host single-instance guarantee (WorkflowId uniqueness)
  - Temporal cluster HA / history replay
  - Built-in timer and scheduled-workflow support

Select this adaptor via::

    config = KernelRuntimeConfig(
        substrate=LocalSubstrateConfig(),
    )
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from agent_kernel.kernel.contracts import (
    RunProjection,
    RuntimeEvent,
    SignalRunRequest,
    SpawnChildRunRequest,
    StartRunRequest,
)
from agent_kernel.substrate.temporal.run_actor_workflow import (
    ActorSignal,
    RunActorDependencyBundle,
    RunActorWorkflow,
    RunInput,
    clear_run_actor_dependencies,
    configure_run_actor_dependencies,
)

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LocalSubstrateConfig:
    """Configuration for the local in-process FSM substrate.

    Attributes:
        workflow_id_prefix: Prefix prepended to run ids when building
            workflow ids (e.g. ``"run"`` 鈫?id ``"run:<run_id>"``).
        strict_mode_enabled: When ``True``, every workflow turn must carry
            ``capability_snapshot_input`` and ``declarative_bundle_digest``.

    """

    workflow_id_prefix: str = "run"
    strict_mode_enabled: bool = True


class LocalFSMAdaptor:
    """In-process substrate that drives RunActorWorkflow directly in asyncio.

    ``KernelRuntime`` manages this adaptor's lifecycle the same way it manages
    ``TemporalAdaptor``: ``start(deps)`` then ``stop()``.  The public API
    presented to ``KernelFacade`` is identical 鈥?only the execution mechanism
    differs.
    """

    def __init__(self, config: LocalSubstrateConfig) -> None:
        """Initialise the adaptor with local substrate configuration.

        Args:
            config: Local substrate configuration.

        """
        self._config = config
        self._gateway_instance: LocalWorkflowGateway | None = None
        self._deps: RunActorDependencyBundle | None = None

    # ------------------------------------------------------------------
    # RuntimeSubstrate interface
    # ------------------------------------------------------------------

    async def start(
        self,
        deps: RunActorDependencyBundle,
        *,
        temporal_client: Any | None = None,  # accepted but unused (interface parity)
    ) -> None:
        """Wires kernel dependencies and creates the local workflow gateway.

        Args:
            deps: Kernel dependency bundle.
            temporal_client: Ignored.  Present for interface parity with
                ``TemporalAdaptor.start()``.

        """
        self._deps = deps
        configure_run_actor_dependencies(deps)
        self._gateway_instance = LocalWorkflowGateway(
            deps=deps,
            workflow_id_prefix=self._config.workflow_id_prefix,
        )
        _logger.info("LocalFSMAdaptor started 鈥?no external process required")

    async def stop(self) -> None:
        """Shuts down all in-process run tasks and clears dependency registry.

        Safe to call multiple times.
        """
        if self._gateway_instance is not None:
            await self._gateway_instance.shutdown()
        if self._deps is not None:
            clear_run_actor_dependencies(self._deps)
        _logger.info("LocalFSMAdaptor stopped")

    @property
    def gateway(self) -> LocalWorkflowGateway:
        """The local workflow gateway wired during ``start()``.

        Returns:
            The ``LocalWorkflowGateway`` instance.

        Raises:
            RuntimeError: If accessed before ``start()`` was called.

        """
        if self._gateway_instance is None:
            raise RuntimeError("LocalFSMAdaptor.gateway accessed before start() was called.")
        return self._gateway_instance

    @property
    def worker_failed(self) -> bool:
        """Always ``False`` 鈥?no background worker task in local mode.

        Returns:
            bool: Always False.

        """
        return False

    def add_worker_done_callback(self, callback: Any) -> None:
        """No-op in local mode 鈥?there is no background worker task.

        Args:
            callback: Ignored.

        """

    def check_worker(self) -> None:
        """No-op in local mode 鈥?there is no background worker task."""

    # Internal backward-compat accessor used by KernelRuntime._worker_task property
    @property
    def _worker_task(self) -> None:
        return None


class LocalWorkflowGateway:
    """Implement ``TemporalWorkflowGateway`` with in-process ``RunActorWorkflow``.

    Instances run directly in asyncio, so no Temporal SDK is required.

    Signal routing, run lifecycle, and projection queries are all handled
    in-process.  The interface is identical to ``TemporalSDKWorkflowGateway``
    so ``KernelFacade`` requires no changes.
    """

    def __init__(
        self,
        deps: RunActorDependencyBundle,
        workflow_id_prefix: str = "run",
    ) -> None:
        """Initialise the gateway with kernel deps and optional prefix.

        Args:
            deps: Kernel dependency bundle (shared with adaptor).
            workflow_id_prefix: Prefix for workflow id construction.

        """
        self._deps = deps
        self._workflow_id_prefix = workflow_id_prefix
        self._workflows: dict[str, RunActorWorkflow] = {}
        self._run_tasks: dict[str, asyncio.Task[Any]] = {}

    # ------------------------------------------------------------------
    # TemporalWorkflowGateway interface
    # ------------------------------------------------------------------

    async def start_workflow(self, request: StartRunRequest) -> dict[str, str]:
        """Start one run by initialising a ``RunActorWorkflow`` in-process.

        ``RunActorWorkflow.run()`` is awaited synchronously here to ensure the
        workflow is fully initialised (``_last_projection`` set) before this
        method returns.  Since ``run()`` only processes pending signals and
        returns, the cost is negligible.

        Args:
            request: Platform-safe run start request.

        Returns:
            Dict with ``"run_id"`` and ``"workflow_id"`` keys.

        """
        run_id = self._resolve_run_id(request)
        if run_id in self._workflows:
            raise ValueError(f"LocalWorkflowGateway: duplicate run_id {run_id!r} is not allowed")
        workflow_id = self._workflow_id_for(run_id)
        workflow = RunActorWorkflow()
        self._workflows[run_id] = workflow
        # Await run() to initialise projection state 鈥?it returns quickly.
        task = asyncio.create_task(
            workflow.run(
                RunInput(
                    run_id=run_id,
                    session_id=request.session_id,
                    parent_run_id=request.parent_run_id,
                    input_ref=request.input_ref,
                    input_json=request.input_json,
                    runtime_context_ref=request.context_ref,
                )
            ),
            name=f"local-run-init:{run_id}",
        )
        self._run_tasks[run_id] = task
        # Yield control so the task can execute its first steps (projection init).
        await asyncio.sleep(0)
        _logger.debug("LocalWorkflowGateway: run started 鈥?run_id=%s", run_id)
        return {"run_id": run_id, "workflow_id": workflow_id}

    async def signal_workflow(
        self,
        run_id: str,
        signal: SignalRunRequest,
    ) -> None:
        """Routes one signal directly to the in-process workflow instance.

        Args:
            run_id: Target run identifier.
            signal: Signal to deliver.

        Raises:
            KeyError: If no run with ``run_id`` is registered.

        """
        workflow = self._workflows.get(run_id)
        if workflow is None:
            raise KeyError(f"LocalWorkflowGateway: run not found 鈥?run_id={run_id!r}")
        await workflow.signal(
            ActorSignal(
                signal_type=signal.signal_type,
                signal_payload=signal.signal_payload,
                caused_by=signal.caused_by,
            )
        )

    async def cancel_workflow(self, run_id: str, reason: str) -> None:
        """Cancel one in-process run task and removes it from the registry.

        Args:
            run_id: Target run identifier.
            reason: Cancellation reason (logged).

        """
        task = self._run_tasks.get(run_id)
        if task is not None and not task.done():
            _logger.info(
                "LocalWorkflowGateway: cancelling run 鈥?run_id=%s reason=%s",
                run_id,
                reason,
            )
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._workflows.pop(run_id, None)
        self._run_tasks.pop(run_id, None)

    async def query_projection(self, run_id: str) -> RunProjection:
        """Return the current projection by querying the in-process workflow.

        Falls back to reading from ``DecisionProjectionService`` directly when
        the workflow instance is not yet registered (e.g. before the first
        ``start_workflow`` call has completed).

        Args:
            run_id: Target run identifier.

        Returns:
            Current authoritative ``RunProjection``.

        """
        workflow = self._workflows.get(run_id)
        if workflow is not None:
            try:
                return workflow.query()
            except RuntimeError:
                pass  # workflow not yet initialised 鈥?fall through to projection
        return await self._deps.projection.get(run_id)

    async def start_child_workflow(
        self,
        parent_run_id: str,
        request: SpawnChildRunRequest,
    ) -> dict[str, str]:
        """Start one child run linked to a parent run.

        Args:
            parent_run_id: Parent run identifier.
            request: Child run creation request.

        Returns:
            Dict with ``"run_id"`` and ``"workflow_id"`` keys.

        """
        child_run_id = request.input_json.get("child_run_id") if request.input_json else None
        if not isinstance(child_run_id, str) or not child_run_id:
            child_run_id = f"{parent_run_id}:{request.child_kind}"

        return await self.start_workflow(
            StartRunRequest(
                run_kind=request.child_kind,
                initiator="kernel",
                parent_run_id=parent_run_id,
                input_ref=request.input_ref,
                input_json={**(request.input_json or {}), "child_run_id": child_run_id},
            )
        )

    def stream_run_events(self, run_id: str) -> AsyncIterator[RuntimeEvent]:
        """Streams runtime events from the kernel event log directly.

        Args:
            run_id: Target run identifier.

        Returns:
            Async iterator of ``RuntimeEvent`` instances.

        """
        return self._stream_events(run_id)

    async def _stream_events(self, run_id: str) -> AsyncIterator[RuntimeEvent]:
        """Yield events for one run from the event log."""
        events = await self._deps.event_log.load(run_id, after_offset=0)
        for event in events:
            yield event

    async def shutdown(self) -> None:
        """Cancel all in-process run tasks."""
        for _, task in list(self._run_tasks.items()):
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._workflows.clear()
        self._run_tasks.clear()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_run_id(self, request: StartRunRequest) -> str:
        """Resolve a deterministic run id from the request fields.

        Mirrors the logic in ``TemporalSDKWorkflowGateway._build_run_id()``
        so run ids are consistent across substrates.

        Args:
            request: Start run request.

        Returns:
            Resolved run identifier string.

        """
        if request.input_json and isinstance(request.input_json.get("run_id"), str):
            return request.input_json["run_id"]
        if request.parent_run_id:
            return f"{request.parent_run_id}:{request.run_kind}"
        if request.session_id:
            return f"{request.session_id}:{request.run_kind}"
        return request.run_kind

    def _workflow_id_for(self, run_id: str) -> str:
        """Build a local workflow id from a run id.

        Args:
            run_id: Kernel run identifier.

        Returns:
            Local workflow id string.

        """
        return f"{self._workflow_id_prefix}:{run_id}"
