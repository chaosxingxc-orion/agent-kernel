"""Unified kernel runtime: single-system lifecycle for kernel + Temporal worker.

Design rationale:
  The prior dual-system approach required the caller to:
    1. Start a Temporal worker separately.
    2. Call ``configure_run_actor_dependencies()`` at the right time.
    3. Build ``AgentKernelRuntimeBundle`` with a matching config.
  Any ordering mistake produced silent state divergence between the
  facade's event log and the workflow's event log.

  ``KernelRuntime`` eliminates this by owning the full lifecycle:
    - One call to ``KernelRuntime.start()`` connects to Temporal, wires
      all service instances, starts the worker as a background asyncio task,
      and returns a fully operational kernel handle.
    - ``KernelRuntime.stop()`` (or the async context manager) gracefully
      shuts down the worker and clears process-local dependency state.

  The kernel and its Temporal worker are now one system, not two.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from agent_kernel.adapters.facade.kernel_facade import KernelFacade
from agent_kernel.kernel.contracts import (
    EventExportPort,
    ObservabilityHook,
    TemporalWorkflowGateway,
)
from agent_kernel.kernel.minimal_runtime import (
    AsyncExecutorService,
    InMemoryDecisionDeduper,
    InMemoryDecisionProjectionService,
    InMemoryKernelRuntimeEventLog,
    StaticDispatchAdmissionService,
    StaticRecoveryGateService,
)
from agent_kernel.runtime.health import KernelHealthProbe
from agent_kernel.substrate.temporal.gateway import (
    TemporalGatewayConfig,
    TemporalSDKWorkflowGateway,
)
from agent_kernel.substrate.temporal.run_actor_workflow import (
    RunActorDependencyBundle,
    RunActorStrictModeConfig,
    RunActorWorkflow,
    clear_run_actor_dependencies,
    configure_run_actor_dependencies,
)
from agent_kernel.substrate.temporal.worker import (
    TemporalKernelWorker,
    TemporalWorkerConfig,
)

_runtime_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class KernelRuntimeConfig:
    """Unified configuration for the single-system kernel runtime.

    All components — event log, worker, facade, and gateway — are wired
    from this one config object.  No external bootstrap code required.

    Attributes:
        task_queue: Temporal task queue for kernel workflow workers.
        temporal_address: Temporal server address (host:port).
        temporal_namespace: Temporal namespace for workflow isolation.
        event_log_backend: Storage backend for the kernel event log.
        sqlite_database_path: SQLite file path when backend is ``"sqlite"``.
            Use ``":memory:"`` for an in-process SQLite store.
        strict_mode_enabled: Requires ``capability_snapshot_input`` and
            ``declarative_bundle_digest`` on every workflow turn when True.
        workflow_id_prefix: Prefix used to build Temporal workflow ids from
            run ids (e.g. ``"run"`` yields workflow id ``"run:<run_id>"``).
        observability_hook: Optional hook that receives FSM state transition
            events.  Defaults to no-op when None.
        event_export_port: Optional platform-facing export sink.  When set,
            every ``ActionCommit`` is exported asynchronously after being
            written to the kernel event log.  Use ``InMemoryRunTraceStore``
            for development; use a streaming backend in production.
        export_timeout_ms: Per-export soft timeout in milliseconds.  Exports
            that exceed this are cancelled and logged at WARNING level.
            Default 5 000 ms.
    """

    task_queue: str = "agent-kernel"
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    event_log_backend: Literal["in_memory", "sqlite"] = "in_memory"
    sqlite_database_path: str | Path = ":memory:"
    strict_mode_enabled: bool = True
    workflow_id_prefix: str = "run"
    observability_hook: ObservabilityHook | None = None
    event_export_port: EventExportPort | None = None
    export_timeout_ms: int = 5000


class KernelRuntime:
    """Single-system kernel runtime.

    The kernel and its Temporal worker share one lifecycle.  Starting the
    kernel starts the worker; stopping the kernel stops the worker.

    Typical usage::

        config = KernelRuntimeConfig(task_queue="my-queue")
        async with await KernelRuntime.start(config) as kernel:
            await kernel.facade.start_run(request)
            projection = await kernel.gateway.query_projection(run_id)

    For testing with a pre-wired Temporal client::

        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with await KernelRuntime.start(config,
                temporal_client=env.client) as kernel:
                ...
    """

    def __init__(
        self,
        *,
        facade: KernelFacade,
        gateway: TemporalSDKWorkflowGateway,
        health: KernelHealthProbe,
        worker_task: asyncio.Task[Any],
        deps: RunActorDependencyBundle,
    ) -> None:
        self._facade = facade
        self._gateway = gateway
        self._health = health
        self._worker_task = worker_task
        self._deps = deps
        # Register a done callback so worker failures are never silently
        # swallowed.
        # The callback runs in the event loop thread and only logs; callers may
        # also register their own callbacks via add_worker_done_callback().
        self._worker_task.add_done_callback(self._on_worker_done)
        self._worker_done_callbacks: list[Any] = []

    def _on_worker_done(self, task: asyncio.Task[Any]) -> None:
        """Internal done callback — logs worker exit and dispatches user callbacks."""
        if task.cancelled():
            _runtime_logger.info(
                "KernelRuntime worker task cancelled — task=%s", task.get_name()
            )
        elif task.exception() is not None:
            _runtime_logger.critical(
                "KernelRuntime worker task FAILED — task=%s error=%r",
                task.get_name(),
                task.exception(),
            )
        else:
            _runtime_logger.info(
                "KernelRuntime worker task completed cleanly — task=%s",
                task.get_name(),
            )
        for cb in self._worker_done_callbacks:
            with contextlib.suppress(Exception):
                cb(task)

    def add_worker_done_callback(self, callback: Any) -> None:
        """Register a callback invoked when the worker background task exits.

        Useful for triggering alerts or automatic restarts in long-running
        services.  The callback receives the completed ``asyncio.Task`` as its
        only argument.  Exceptions raised in the callback are swallowed.

        Args:
            callback: Callable that accepts one ``asyncio.Task`` argument.
        """
        self._worker_done_callbacks.append(callback)

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    async def start(
        cls,
        config: KernelRuntimeConfig | None = None,
        *,
        temporal_client: Any | None = None,
    ) -> KernelRuntime:
        """Start the kernel and its Temporal worker in a single call.

        Args:
            config: Runtime configuration.  Defaults to
                ``KernelRuntimeConfig()`` when omitted.
            temporal_client: Pre-existing Temporal client.  When provided
                the ``temporal_address`` and ``temporal_namespace`` fields
                in ``config`` are ignored.  Useful for integration tests
                that use ``WorkflowEnvironment``.

        Returns:
            A fully operational ``KernelRuntime`` instance.

        Raises:
            RuntimeError: If the Temporal Python SDK is not installed.
        """
        if config is None:
            config = KernelRuntimeConfig()

        client = temporal_client or await _connect_temporal(config)

        # Build the shared service layer.  All components receive the
        # *same* instances so there is no risk of projection divergence.
        event_log, projection, admission, executor, recovery, deduper, dedupe_store = (
            _build_services(config)
        )

        deps = RunActorDependencyBundle(
            event_log=event_log,
            projection=projection,
            admission=admission,
            executor=executor,
            recovery=recovery,
            deduper=deduper,
            dedupe_store=dedupe_store,
            strict_mode=RunActorStrictModeConfig(
                enabled=config.strict_mode_enabled
            ),
            workflow_id_prefix=config.workflow_id_prefix,
            observability_hook=config.observability_hook,
        )

        # Wire dependencies into the process-local registry immediately so
        # they are available before the worker background task runs its
        # first turn.  Store the returned token so stop() can clear *only
        # this runtime's* registration without disturbing other runtimes.
        configure_run_actor_dependencies(deps)

        gateway_config = TemporalGatewayConfig(
            task_queue=config.task_queue,
            workflow_id_prefix=config.workflow_id_prefix,
            workflow_run_callable=RunActorWorkflow.run,
            signal_method_name="signal",
            query_method_name="query",
        )
        gateway = TemporalSDKWorkflowGateway(client, gateway_config)
        facade = KernelFacade(gateway)
        health = KernelHealthProbe()

        # Start the Temporal worker as a background asyncio task.
        # The kernel owns this task — it is NOT the caller's responsibility.
        worker = TemporalKernelWorker(
            client=client,
            config=TemporalWorkerConfig(task_queue=config.task_queue),
            dependencies=deps,
        )
        worker_task = asyncio.create_task(
            worker.run(),
            name=f"kernel-worker:{config.task_queue}",
        )

        _runtime_logger.info(
            "KernelRuntime started — task_queue=%s worker_task=%s",
            config.task_queue,
            worker_task.get_name(),
        )
        return cls(
            facade=facade,
            gateway=gateway,
            health=health,
            worker_task=worker_task,
            deps=deps,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def stop(self) -> None:
        """Gracefully stop the Temporal worker and clear kernel state.

        Safe to call multiple times.  Cancels the worker background task
        and waits for it to finish cleanup.
        """
        if self._worker_task.done():
            return
        _runtime_logger.info(
            "KernelRuntime stopping — cancelling worker task %s",
            self._worker_task.get_name(),
        )
        self._worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._worker_task
        # Clear only this runtime's registration.  If another KernelRuntime
        # has already registered its own bundle, this is a no-op and the new
        # runtime's state is left intact.
        clear_run_actor_dependencies(self._deps)

    async def __aenter__(self) -> KernelRuntime:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.stop()

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def facade(self) -> KernelFacade:
        """The kernel facade — the only allowed platform entrypoint."""
        return self._facade

    @property
    def gateway(self) -> TemporalWorkflowGateway:
        """The Temporal workflow gateway for direct substrate access."""
        return self._gateway

    @property
    def health(self) -> KernelHealthProbe:
        """K8s-style liveness/readiness probes for this runtime."""
        return self._health

    @property
    def worker_failed(self) -> bool:
        """True when the background worker task has exited with an error."""
        return (
            self._worker_task.done()
            and not self._worker_task.cancelled()
            and (self._worker_task.exception() is not None)
        )

    def check_worker(self) -> None:
        """Raise the worker exception if the background task has failed.

        Call this periodically in long-running applications to surface worker
        failures that would otherwise be silently swallowed.

        Raises:
            Exception: The exception that caused the worker to exit.
        """
        if self.worker_failed:
            raise self._worker_task.exception()  # type: ignore[misc]


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------


async def _connect_temporal(config: KernelRuntimeConfig) -> Any:
    """Connect to Temporal server using SDK client.

    Args:
        config: Runtime configuration with address and namespace.

    Returns:
        Connected Temporal client instance.

    Raises:
        RuntimeError: If the Temporal Python SDK is not installed.
    """
    try:
        from temporalio.client import Client  # type: ignore[import]
    except ImportError as exc:
        raise RuntimeError(
            "Temporal SDK is required. Install with: pip install temporalio"
        ) from exc
    return await Client.connect(
        config.temporal_address,
        namespace=config.temporal_namespace,
    )


def _build_services(
    config: KernelRuntimeConfig,
) -> tuple[Any, Any, Any, Any, Any, Any, Any]:
    """Build shared service instances from config.

    All returned instances share state (e.g. event_log ↔ projection).
    They must be passed together to ``RunActorDependencyBundle``.

    Consistency guarantee: when ``event_log_backend="sqlite"``, both the
    event_log and the dedupe_store are SQLite-backed so idempotency survives
    process restarts.  The projection service receives the same underlying
    event_log (before export wrapping) to prevent stale-snapshot races on the
    ``await`` yield boundary.

    Export wrapping: when ``event_export_port`` is configured, the event_log
    returned to callers is wrapped in ``EventExportingEventLog`` so every
    ``append_action_commit`` fires an async export after the durable write.
    The projection service always reads from the unwrapped inner log so it
    sees the same data regardless of export availability.

    Args:
        config: Runtime configuration.

    Returns:
        Tuple of (event_log, projection, admission, executor, recovery,
            deduper, dedupe_store).
    """
    if config.event_log_backend == "sqlite":
        from agent_kernel.kernel.persistence.sqlite_event_log import (
            SQLiteKernelRuntimeEventLog,
        )
        from agent_kernel.kernel.persistence.sqlite_dedupe_store import (
            SQLiteDedupeStore,
        )

        base_event_log: Any = SQLiteKernelRuntimeEventLog(
            config.sqlite_database_path
        )
        dedupe_store: Any = SQLiteDedupeStore(config.sqlite_database_path)
    else:
        from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore

        base_event_log = InMemoryKernelRuntimeEventLog()
        dedupe_store = InMemoryDedupeStore()

    # Projection always reads from the raw storage layer so replay is never
    # gated on export availability.
    projection = InMemoryDecisionProjectionService(base_event_log)

    # Wrap with export if a platform sink is configured.  The wrapper is what
    # gets wired into RunActorDependencyBundle so all appends fire the export.
    if config.event_export_port is not None:
        from agent_kernel.kernel.event_export import EventExportingEventLog

        event_log: Any = EventExportingEventLog(
            base_event_log,
            config.event_export_port,
            export_timeout_ms=config.export_timeout_ms,
        )
    else:
        event_log = base_event_log

    admission = StaticDispatchAdmissionService()
    executor = AsyncExecutorService()
    recovery = StaticRecoveryGateService()
    deduper = InMemoryDecisionDeduper()
    return event_log, projection, admission, executor, recovery, deduper, dedupe_store
