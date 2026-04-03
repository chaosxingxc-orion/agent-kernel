"""Unified kernel runtime: single-system lifecycle for kernel + Temporal worker.

Design rationale:
  ``KernelRuntime`` is the single entry point that assembles the kernel and
  its execution substrate into one managed system.

  Substrate architecture:
    The kernel *owns* its substrate — the substrate is a managed component,
    not a dependency the kernel passively connects to.  ``KernelRuntime.start()``
    creates a ``TemporalAdaptor`` (or a custom ``RuntimeSubstrate``), wires
    all service instances into it, and starts it.  ``stop()`` (or the async
    context manager) shuts everything down in the correct order.

  Temporal connection modes (via ``TemporalSubstrateConfig.mode``):
    ``"sdk"``  — connects to an external Temporal cluster via
                 ``Client.connect(address)``.  The default; suitable for
                 production with a managed Temporal service.
    ``"host"`` — starts an embedded Temporal dev-server via
                 ``WorkflowEnvironment.start_local()`` (requires
                 ``pip install 'temporalio[testing]'``).  No external process
                 needed; suitable for local development and CI.

  Backward compatibility:
    The flat ``temporal_address``, ``temporal_namespace``, ``task_queue``,
    ``workflow_id_prefix``, and ``strict_mode_enabled`` fields on
    ``KernelRuntimeConfig`` remain available.  When ``substrate`` is ``None``
    (the default) those fields are forwarded to a ``TemporalSubstrateConfig``
    automatically.  Pass an explicit ``substrate`` to use a different mode or
    override individual Temporal settings.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Self

if TYPE_CHECKING:
    from pathlib import Path

from agent_kernel.adapters.facade.kernel_facade import KernelFacade

if TYPE_CHECKING:
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
from agent_kernel.substrate.local.adaptor import LocalFSMAdaptor, LocalSubstrateConfig
from agent_kernel.substrate.temporal.adaptor import TemporalAdaptor, TemporalSubstrateConfig
from agent_kernel.substrate.temporal.run_actor_workflow import (
    RunActorDependencyBundle,
    RunActorStrictModeConfig,
)

_runtime_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class KernelRuntimeConfig:
    """Unified configuration for the single-system kernel runtime.

    All components — event log, substrate adaptor, facade, and gateway — are
    wired from this one config object.  No external bootstrap code required.

    Substrate selection:
        When ``substrate`` is ``None`` (default) the flat ``temporal_address``,
        ``temporal_namespace``, ``task_queue``, ``workflow_id_prefix``, and
        ``strict_mode_enabled`` fields are forwarded to a default
        ``TemporalSubstrateConfig(mode="sdk")`` automatically.

        Pass an explicit ``TemporalSubstrateConfig`` to switch modes or
        override individual Temporal settings::

            # Host mode — embedded dev-server, no external Temporal needed
            config = KernelRuntimeConfig(
                substrate=TemporalSubstrateConfig(mode="host"),
            )

    Attributes:
        task_queue: Temporal task queue.  Ignored when ``substrate`` is set.
        temporal_address: Temporal server address (host:port).
            Ignored when ``substrate`` is set.
        temporal_namespace: Temporal namespace.  Ignored when ``substrate`` is set.
        event_log_backend: Storage backend for the kernel event log.
        sqlite_database_path: SQLite file path when backend is ``"sqlite"``.
            Use ``":memory:"`` for an in-process SQLite store.
        strict_mode_enabled: Requires ``capability_snapshot_input`` and
            ``declarative_bundle_digest`` on every workflow turn when True.
            Ignored when ``substrate`` is set (use ``substrate.strict_mode_enabled``).
        workflow_id_prefix: Prefix used to build Temporal workflow ids.
            Ignored when ``substrate`` is set.
        observability_hook: Optional hook that receives FSM state transition
            events.  Defaults to no-op when None.
        event_export_port: Optional platform-facing export sink.  When set,
            every ``ActionCommit`` is exported asynchronously after being
            written to the kernel event log.
        export_timeout_ms: Per-export soft timeout in milliseconds.
            Default 5 000 ms.
        substrate: Optional explicit substrate configuration.  When provided
            takes full precedence over the flat Temporal fields above.
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
    substrate: TemporalSubstrateConfig | LocalSubstrateConfig | None = None


class KernelRuntime:
    """Single-system kernel runtime.

    The kernel and its substrate adaptor share one lifecycle.  Starting the
    kernel starts the substrate; stopping the kernel stops the substrate.

    Typical usage::

        config = KernelRuntimeConfig(task_queue="my-queue")
        async with await KernelRuntime.start(config) as kernel:
            await kernel.facade.start_run(request)
            projection = await kernel.gateway.query_projection(run_id)

    Host mode (embedded Temporal dev-server, no external process needed)::

        config = KernelRuntimeConfig(
            substrate=TemporalSubstrateConfig(mode="host"),
        )
        async with await KernelRuntime.start(config) as kernel:
            ...

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
        adaptor: TemporalAdaptor,
        health: KernelHealthProbe,
    ) -> None:
        self._facade = facade
        self._adaptor = adaptor
        self._health = health

    # ------------------------------------------------------------------
    # Backward-compat internal accessors (used by tests and heartbeat)
    # ------------------------------------------------------------------

    @property
    def _worker_task(self) -> Any:
        """Delegates to the adaptor's worker task (backward compat)."""
        return self._adaptor._worker_task

    @property
    def _deps(self) -> Any:
        """Delegates to the adaptor's dependency bundle (backward compat)."""
        return self._adaptor._deps

    def add_worker_done_callback(self, callback: Any) -> None:
        """Register a callback invoked when the substrate worker task exits.

        Useful for triggering alerts or automatic restarts in long-running
        services.  The callback receives the completed ``asyncio.Task`` as its
        only argument.  Exceptions raised in the callback are swallowed.

        Args:
            callback: Callable that accepts one ``asyncio.Task`` argument.
        """
        self._adaptor.add_worker_done_callback(callback)

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
        """Start the kernel and its substrate in a single call.

        Assembles the service layer, builds the substrate adaptor, starts the
        Temporal worker (in background), and returns a fully operational
        ``KernelRuntime`` handle.

        Args:
            config: Runtime configuration.  Defaults to
                ``KernelRuntimeConfig()`` when omitted.
            temporal_client: Pre-existing Temporal client.  When provided
                the ``temporal_address``, ``temporal_namespace``, and
                ``substrate.mode`` fields are ignored.  Intended for
                integration tests that supply a ``WorkflowEnvironment`` client.

        Returns:
            A fully operational ``KernelRuntime`` instance.

        Raises:
            RuntimeError: If the Temporal Python SDK is not installed.
        """
        if config is None:
            config = KernelRuntimeConfig()

        # Build the substrate config — explicit substrate takes precedence
        # over the flat backward-compat fields.
        substrate_config: TemporalSubstrateConfig | LocalSubstrateConfig
        if config.substrate is not None:
            substrate_config = config.substrate
        else:
            substrate_config = TemporalSubstrateConfig(
                mode="sdk",
                address=config.temporal_address,
                namespace=config.temporal_namespace,
                task_queue=config.task_queue,
                workflow_id_prefix=config.workflow_id_prefix,
                strict_mode_enabled=config.strict_mode_enabled,
            )

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
            strict_mode=RunActorStrictModeConfig(enabled=substrate_config.strict_mode_enabled),
            workflow_id_prefix=substrate_config.workflow_id_prefix,
            observability_hook=config.observability_hook,
        )

        # Dispatch to the appropriate substrate adaptor.
        adaptor: TemporalAdaptor | LocalFSMAdaptor
        if isinstance(substrate_config, LocalSubstrateConfig):
            adaptor = LocalFSMAdaptor(substrate_config)
            await adaptor.start(deps)
        else:
            adaptor = TemporalAdaptor(substrate_config)
            await adaptor.start(deps, temporal_client=temporal_client)

        facade = KernelFacade(adaptor.gateway)
        health = KernelHealthProbe()

        _runtime_logger.info(
            "KernelRuntime started — substrate=%s",
            type(substrate_config).__name__,
        )
        return cls(facade=facade, adaptor=adaptor, health=health)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def stop(self) -> None:
        """Gracefully stop the substrate and clear kernel state.

        Delegates to the substrate adaptor which cancels the worker task,
        waits for cleanup, and (in host mode) shuts down the embedded
        dev-server.  Safe to call multiple times.
        """
        await self._adaptor.stop()

    async def __aenter__(self) -> Self:
        """Enters the async context manager and returns this runtime.

        Returns:
            Self: This runtime instance.
        """
        return self

    async def __aexit__(self, *_: object) -> None:
        """Exits the async context manager, stopping the runtime."""
        await self.stop()

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def facade(self) -> KernelFacade:
        """The kernel facade — the only allowed platform entrypoint.

        Returns:
            KernelFacade: The wired facade instance.
        """
        return self._facade

    @property
    def gateway(self) -> TemporalWorkflowGateway:
        """The workflow gateway for direct substrate access.

        Returns:
            TemporalWorkflowGateway: The gateway wired by the active substrate.
        """
        return self._adaptor.gateway

    @property
    def health(self) -> KernelHealthProbe:
        """K8s-style liveness/readiness probes for this runtime.

        Returns:
            KernelHealthProbe: The health probe instance.
        """
        return self._health

    @property
    def worker_failed(self) -> bool:
        """True when the substrate's background worker has exited with an error.

        Returns:
            bool: Worker failure state.
        """
        return self._adaptor.worker_failed

    def check_worker(self) -> None:
        """Raise the worker exception if the background task has failed.

        Call this periodically in long-running applications to surface worker
        failures that would otherwise be silently swallowed.

        Raises:
            Exception: The exception that caused the worker to exit.
        """
        self._adaptor.check_worker()


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------


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
        from agent_kernel.kernel.persistence.sqlite_dedupe_store import (
            SQLiteDedupeStore,
        )
        from agent_kernel.kernel.persistence.sqlite_event_log import (
            SQLiteKernelRuntimeEventLog,
        )

        base_event_log: Any = SQLiteKernelRuntimeEventLog(config.sqlite_database_path)
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
