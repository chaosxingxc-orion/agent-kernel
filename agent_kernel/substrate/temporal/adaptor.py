"""TemporalAdaptor 鈥?Temporal integrated as a managed kernel substrate.

Design principles:
  - The kernel *owns* this adaptor's lifecycle.  ``KernelRuntime.start()``
    calls ``TemporalAdaptor.start()``; ``KernelRuntime.stop()`` calls
    ``TemporalAdaptor.stop()``.  Temporal is a managed component, not a
    dependency the kernel passively connects to.
  - Two connection modes are supported via ``TemporalSubstrateConfig.mode``:
      ``"sdk"``  鈥?connects to an external Temporal cluster via
                   ``temporalio.client.Client.connect()``.  Suitable for
                   production deployments with a managed Temporal service.
      ``"host"`` 鈥?starts an embedded Temporal dev-server via
                   ``WorkflowEnvironment.start_local()`` (requires the
                   ``temporalio[testing]`` extra).  Suitable for local
                   development and self-contained single-machine deployments.
  - No Temporal SDK types escape this module.  The ``gateway`` property
    returns a ``WorkflowGateway``-compatible object using only kernel DTOs.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from importlib import import_module
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from agent_kernel.substrate.temporal.gateway import TemporalSDKWorkflowGateway
    from agent_kernel.substrate.temporal.run_actor_workflow import RunActorDependencyBundle

from agent_kernel.substrate.temporal.gateway import (
    TemporalGatewayConfig,
    TemporalSDKWorkflowGateway,
)
from agent_kernel.substrate.temporal.run_actor_workflow import (
    RunActorWorkflow,
    clear_run_actor_dependencies,
    configure_run_actor_dependencies,
)
from agent_kernel.substrate.temporal.worker import (
    TemporalKernelWorker,
    TemporalWorkerConfig,
)

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TemporalSubstrateConfig:
    """Configuration for Temporal as a managed kernel runtime substrate.

    Attributes:
        mode: Connection mode.
            ``"sdk"`` connects to an external Temporal cluster via
            ``Client.connect()``.
            ``"host"`` starts an embedded Temporal dev-server via
            ``WorkflowEnvironment.start_local()`` (requires
            ``pip install 'temporalio[testing]'``).
        address: Temporal server address (``host:port``).
            Used in ``"sdk"`` mode only.
        namespace: Temporal namespace for workflow isolation.
            Used in ``"sdk"`` mode only.
        task_queue: Temporal task queue that kernel workflow workers poll.
        workflow_id_prefix: Prefix prepended to run ids when building
            Temporal workflow ids (e.g. ``"run"`` 鈫?workflow id
            ``"run:<run_id>"``).
        strict_mode_enabled: When ``True``, every workflow turn must carry a
            ``capability_snapshot_input`` and ``declarative_bundle_digest``.
        host_port: TCP port for the embedded dev-server.
            ``None`` auto-assigns a free port.  Used in ``"host"`` mode only.
        host_db_filename: Persistence file for the embedded dev-server.
            ``None`` uses an ephemeral in-memory store (data lost on stop).
            Used in ``"host"`` mode only.
        host_ui_port: Port for the Temporal Web UI bundled with the
            dev-server.  ``None`` disables the UI.
            Used in ``"host"`` mode only.

    """

    mode: Literal["sdk", "host"] = "sdk"
    address: str = "localhost:7233"
    namespace: str = "default"
    task_queue: str = "agent-kernel"
    workflow_id_prefix: str = "run"
    strict_mode_enabled: bool = True
    host_port: int | None = None
    host_db_filename: str | None = None
    host_ui_port: int | None = None


class TemporalAdaptor:
    """Integrates Temporal as a managed component within the kernel runtime.

    The adaptor encapsulates all Temporal SDK concerns 鈥?client connection,
    worker registration, workflow gateway wiring, and (in host mode) the
    embedded dev-server lifecycle.  ``KernelRuntime`` delegates substrate
    operations to this class and never imports ``temporalio`` directly.

    Typical usage (managed by ``KernelRuntime``, not called directly)::

        config = TemporalSubstrateConfig(mode="sdk", address="temporal.prod:7233")
        adaptor = TemporalAdaptor(config)
        await adaptor.start(deps)
        # ... kernel runs ...
        await adaptor.stop()

    For host mode (embedded dev-server, no external Temporal needed)::

        config = TemporalSubstrateConfig(mode="host")
        adaptor = TemporalAdaptor(config)
        await adaptor.start(deps)
    """

    def __init__(self, config: TemporalSubstrateConfig) -> None:
        """Initialise the adaptor with substrate configuration.

        Args:
            config: Temporal substrate configuration controlling connection
                mode, addressing, and worker settings.

        """
        self._config = config
        self._client: Any = None
        self._env: Any = None  # WorkflowEnvironment in host mode, None in sdk mode
        self._gateway_instance: TemporalSDKWorkflowGateway | None = None
        self._worker_task: asyncio.Task[Any] | None = None
        self._worker_done_callbacks: list[Any] = []
        self._deps: Any = None  # stored for clear_run_actor_dependencies on stop()

    # ------------------------------------------------------------------
    # RuntimeSubstrate interface
    # ------------------------------------------------------------------

    async def start(
        self,
        deps: RunActorDependencyBundle,
        *,
        temporal_client: Any | None = None,
    ) -> None:
        """Start the Temporal connection and kernel worker.

        Acquires a Temporal client (injected, sdk-connected, or host-embedded),
        wires the workflow gateway, and launches the Temporal worker as a
        background asyncio task.

        Args:
            deps: Kernel dependency bundle wired into the workflow worker.
            temporal_client: Pre-built Temporal client.  When provided the
                ``mode``, ``address``, and ``namespace`` fields are ignored.
                Intended for test harnesses that supply a
                ``WorkflowEnvironment`` client.

        """
        if temporal_client is not None:
            self._client = temporal_client
            self._env = None
        elif self._config.mode == "host":
            self._client, self._env = await self._connect_host()
        else:
            self._client = await self._connect_sdk()
            self._env = None

        gateway_config = TemporalGatewayConfig(
            task_queue=self._config.task_queue,
            workflow_id_prefix=self._config.workflow_id_prefix,
            workflow_run_callable=RunActorWorkflow.run,
            signal_method_name="signal",
            query_method_name="query",
        )
        self._gateway_instance = TemporalSDKWorkflowGateway(self._client, gateway_config)

        self._deps = deps
        configure_run_actor_dependencies(deps)

        worker = TemporalKernelWorker(
            client=self._client,
            config=TemporalWorkerConfig(task_queue=self._config.task_queue),
            dependencies=deps,
        )
        self._worker_task = asyncio.create_task(
            worker.run(),
            name=f"kernel-worker:{self._config.task_queue}",
        )
        self._worker_task.add_done_callback(self._on_worker_done)
        _logger.info(
            "TemporalAdaptor started 鈥?mode=%s task_queue=%s worker_task=%s",
            self._config.mode,
            self._config.task_queue,
            self._worker_task.get_name(),
        )

    async def stop(self) -> None:
        """Gracefully stops the worker and shuts down the Temporal connection.

        Cancels the background worker task and, in host mode, shuts down the
        embedded dev-server.  Safe to call multiple times.
        """
        if self._worker_task is not None and not self._worker_task.done():
            _logger.info(
                "TemporalAdaptor stopping 鈥?cancelling worker task %s",
                self._worker_task.get_name(),
            )
            self._worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._worker_task
        # Clear the process-local dependency registry so a subsequent runtime
        # start does not accidentally reuse a stale bundle.
        if self._deps is not None:
            clear_run_actor_dependencies(self._deps)
        if self._env is not None:
            _logger.info("TemporalAdaptor shutting down embedded dev-server")
            with contextlib.suppress(Exception):
                await self._env.shutdown()

    @property
    def gateway(self) -> TemporalSDKWorkflowGateway:
        """The workflow gateway wired by this substrate.

        Returns:
            The ``TemporalSDKWorkflowGateway`` instance wired during
            ``start()``.

        Raises:
            RuntimeError: If accessed before ``start()`` has been called.

        """
        if self._gateway_instance is None:
            raise RuntimeError("TemporalAdaptor.gateway accessed before start() was called.")
        return self._gateway_instance

    @property
    def worker_failed(self) -> bool:
        """``True`` when the background worker task has exited with an error.

        Returns:
            bool: Worker failure state.

        """
        return (
            self._worker_task is not None
            and self._worker_task.done()
            and not self._worker_task.cancelled()
            and self._worker_task.exception() is not None
        )

    def add_worker_done_callback(self, callback: Any) -> None:
        """Register a callback invoked when the worker background task exits.

        Callbacks receive the completed ``asyncio.Task`` as their only
        argument.  Exceptions raised in callbacks are swallowed.

        Args:
            callback: Callable that accepts one ``asyncio.Task`` argument.

        """
        # Callbacks are stored and dispatched via _on_worker_done, which is the
        # single task done callback.  Do NOT add the user callback directly to
        # the task 鈥?that would fire it twice (once from _on_worker_done and
        # once from the direct task callback).
        self._worker_done_callbacks.append(callback)

    def check_worker(self) -> None:
        """Raise the worker exception if the background task has failed.

        Raises:
            Exception: The exception that caused the worker to exit.

        """
        if self.worker_failed:
            raise self._worker_task.exception()  # type: ignore[misc]

    # ------------------------------------------------------------------
    # Internal connection helpers
    # ------------------------------------------------------------------

    async def _connect_sdk(self) -> Any:
        """Connect to an external Temporal cluster via the Python SDK.

        Returns:
            Connected ``temporalio.client.Client`` instance.

        Raises:
            RuntimeError: If ``temporalio`` is not installed.

        """
        try:
            client_module = import_module("temporalio.client")
        except ImportError as exc:
            raise RuntimeError(
                "Temporal SDK is required. Install with: pip install temporalio"
            ) from exc
        return await client_module.Client.connect(
            self._config.address,
            namespace=self._config.namespace,
        )

    async def _connect_host(self) -> tuple[Any, Any]:
        """Start an embedded Temporal dev-server and returns ``(client, env)``.

        Uses ``WorkflowEnvironment.start_local()`` from the testing extras.
        The dev-server binary is downloaded automatically on first run.

        Returns:
            Tuple of ``(temporalio.client.Client, WorkflowEnvironment)``.

        Raises:
            RuntimeError: If ``temporalio[testing]`` is not installed.

        """
        try:
            testing_module = import_module("temporalio.testing")
        except ImportError as exc:
            raise RuntimeError(
                "Temporal testing extras are required for host mode. "
                "Install with: pip install 'temporalio[testing]'"
            ) from exc
        start_kwargs: dict[str, Any] = {}
        if self._config.host_port is not None:
            start_kwargs["port"] = self._config.host_port
        if self._config.host_db_filename is not None:
            start_kwargs["db_filename"] = self._config.host_db_filename
        if self._config.host_ui_port is not None:
            start_kwargs["ui_port"] = self._config.host_ui_port
        env = await testing_module.WorkflowEnvironment.start_local(**start_kwargs)
        _logger.info(
            "TemporalAdaptor host mode: embedded dev-server started 鈥?db=%s",
            self._config.host_db_filename or "<ephemeral>",
        )
        return env.client, env

    def _on_worker_done(self, task: asyncio.Task[Any]) -> None:
        """Handle worker task completion and log exit details."""
        if task.cancelled():
            _logger.info("TemporalAdaptor worker task cancelled 鈥?task=%s", task.get_name())
        elif task.exception() is not None:
            _logger.critical(
                "TemporalAdaptor worker task FAILED 鈥?task=%s error=%r",
                task.get_name(),
                task.exception(),
            )
        else:
            _logger.info(
                "TemporalAdaptor worker task completed cleanly 鈥?task=%s",
                task.get_name(),
            )
        for cb in self._worker_done_callbacks:
            with contextlib.suppress(Exception):
                cb(task)
