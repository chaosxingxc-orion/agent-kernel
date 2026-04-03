"""Tests for KernelRuntime single-system lifecycle."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_kernel.kernel.minimal_runtime import (
    AsyncExecutorService,
    InMemoryDecisionDeduper,
    InMemoryDecisionProjectionService,
    InMemoryKernelRuntimeEventLog,
    StaticDispatchAdmissionService,
    StaticRecoveryGateService,
)
from agent_kernel.runtime.kernel_runtime import (
    KernelRuntime,
    KernelRuntimeConfig,
    _build_services,
)
from agent_kernel.substrate.temporal.run_actor_workflow import (
    RunActorDependencyBundle,
    configure_run_actor_dependencies,
)

# ---------------------------------------------------------------------------
# _build_services unit tests
# ---------------------------------------------------------------------------


def test_build_services_in_memory_returns_shared_event_log() -> None:
    """Event log and projection service share the same instance."""
    config = KernelRuntimeConfig()
    event_log, projection, admission, executor, recovery, deduper, _dedupe_store = (
        _build_services(config)
    )

    assert isinstance(event_log, InMemoryKernelRuntimeEventLog)
    assert isinstance(projection, InMemoryDecisionProjectionService)
    assert isinstance(admission, StaticDispatchAdmissionService)
    assert isinstance(executor, AsyncExecutorService)
    assert isinstance(recovery, StaticRecoveryGateService)
    assert isinstance(deduper, InMemoryDecisionDeduper)
    # The projection service must hold the same event_log instance.
    assert projection._event_log is event_log


def test_build_services_sqlite_returns_sqlite_event_log() -> None:
    """SQLite backend creates a SQLiteKernelRuntimeEventLog."""
    from agent_kernel.kernel.persistence.sqlite_event_log import SQLiteKernelRuntimeEventLog

    config = KernelRuntimeConfig(event_log_backend="sqlite", sqlite_database_path=":memory:")
    event_log, _projection, *_ = _build_services(config)

    assert isinstance(event_log, SQLiteKernelRuntimeEventLog)


# ---------------------------------------------------------------------------
# KernelRuntimeConfig defaults
# ---------------------------------------------------------------------------


def test_kernel_runtime_config_defaults() -> None:
    config = KernelRuntimeConfig()
    assert config.task_queue == "agent-kernel"
    assert config.temporal_address == "localhost:7233"
    assert config.temporal_namespace == "default"
    assert config.event_log_backend == "in_memory"
    assert config.strict_mode_enabled is True
    assert config.workflow_id_prefix == "run"
    assert config.observability_hook is None


def test_kernel_runtime_config_custom_values() -> None:
    config = KernelRuntimeConfig(
        task_queue="my-queue",
        temporal_address="temporal.prod:7233",
        event_log_backend="sqlite",
        sqlite_database_path="/data/kernel.db",
        strict_mode_enabled=False,
        workflow_id_prefix="agent",
    )
    assert config.task_queue == "my-queue"
    assert config.temporal_address == "temporal.prod:7233"
    assert config.event_log_backend == "sqlite"
    assert config.strict_mode_enabled is False
    assert config.workflow_id_prefix == "agent"


# ---------------------------------------------------------------------------
# KernelRuntime lifecycle via mock Temporal client
# ---------------------------------------------------------------------------


def _make_mock_temporal_client() -> MagicMock:
    """Returns a minimal mock that satisfies TemporalSDKWorkflowGateway."""
    client = MagicMock()
    client.start_workflow = AsyncMock(
        return_value=MagicMock(id="run:run-1", first_execution_run_id="wf-1")
    )
    return client


async def _fake_worker_run() -> None:
    """Simulates a long-running worker that yields and then blocks."""
    await asyncio.sleep(9999)


@pytest.mark.asyncio
async def test_kernel_runtime_start_creates_worker_task() -> None:
    """start() must launch a background worker task."""
    mock_client = _make_mock_temporal_client()
    config = KernelRuntimeConfig(task_queue="test-q")

    with patch(
        "agent_kernel.runtime.kernel_runtime.TemporalKernelWorker.run",
        side_effect=_fake_worker_run,
    ):
        kernel = await KernelRuntime.start(config, temporal_client=mock_client)
        try:
            assert not kernel._worker_task.done()
            assert kernel._worker_task.get_name() == "kernel-worker:test-q"
        finally:
            await kernel.stop()


@pytest.mark.asyncio
async def test_kernel_runtime_stop_cancels_worker_task() -> None:
    """stop() must cancel the worker task cleanly."""
    mock_client = _make_mock_temporal_client()
    config = KernelRuntimeConfig(task_queue="test-q")

    with patch(
        "agent_kernel.runtime.kernel_runtime.TemporalKernelWorker.run",
        side_effect=_fake_worker_run,
    ):
        kernel = await KernelRuntime.start(config, temporal_client=mock_client)
        await kernel.stop()
        assert kernel._worker_task.done()


@pytest.mark.asyncio
async def test_kernel_runtime_stop_is_idempotent() -> None:
    """Calling stop() multiple times must not raise."""
    mock_client = _make_mock_temporal_client()
    config = KernelRuntimeConfig(task_queue="test-q")

    with patch(
        "agent_kernel.runtime.kernel_runtime.TemporalKernelWorker.run",
        side_effect=_fake_worker_run,
    ):
        kernel = await KernelRuntime.start(config, temporal_client=mock_client)
        await kernel.stop()
        await kernel.stop()  # second call must not raise


@pytest.mark.asyncio
async def test_kernel_runtime_context_manager_stops_on_exit() -> None:
    """Async context manager must stop the worker on __aexit__."""
    mock_client = _make_mock_temporal_client()
    config = KernelRuntimeConfig(task_queue="test-q")

    with patch(
        "agent_kernel.runtime.kernel_runtime.TemporalKernelWorker.run",
        side_effect=_fake_worker_run,
    ):
        async with await KernelRuntime.start(config, temporal_client=mock_client) as kernel:
            task = kernel._worker_task
            assert not task.done()
        assert task.done()


@pytest.mark.asyncio
async def test_kernel_runtime_exposes_facade_and_gateway() -> None:
    """KernelRuntime.facade and .gateway must be non-None after start."""
    mock_client = _make_mock_temporal_client()
    config = KernelRuntimeConfig(task_queue="test-q")

    with patch(
        "agent_kernel.runtime.kernel_runtime.TemporalKernelWorker.run",
        side_effect=_fake_worker_run,
    ):
        async with await KernelRuntime.start(config, temporal_client=mock_client) as kernel:
            assert kernel.facade is not None
            assert kernel.gateway is not None
            assert kernel.health is not None


@pytest.mark.asyncio
async def test_kernel_runtime_wires_dependencies_before_worker_task() -> None:
    """configure_run_actor_dependencies must be called before worker task starts."""
    mock_client = _make_mock_temporal_client()
    config = KernelRuntimeConfig(task_queue="test-q")

    wired: list[RunActorDependencyBundle | None] = []

    original_configure = configure_run_actor_dependencies

    def _capture_configure(deps: RunActorDependencyBundle | None) -> None:
        wired.append(deps)
        original_configure(deps)

    with (
        patch(
            "agent_kernel.runtime.kernel_runtime.TemporalKernelWorker.run",
            side_effect=_fake_worker_run,
        ),
        patch(
            "agent_kernel.runtime.kernel_runtime.configure_run_actor_dependencies",
            side_effect=_capture_configure,
        ),
    ):
        async with await KernelRuntime.start(config, temporal_client=mock_client):
            # First call in start() must be with a non-None bundle.
            assert len(wired) >= 1
            assert wired[0] is not None


@pytest.mark.asyncio
async def test_kernel_runtime_clears_dependencies_on_stop() -> None:
    """stop() must ensure dependencies are cleared from process-local registry."""
    from agent_kernel.substrate.temporal.run_actor_workflow import (
        _RUN_ACTOR_CONFIG_FALLBACK,
    )
    mock_client = _make_mock_temporal_client()
    config = KernelRuntimeConfig(task_queue="test-q")

    with patch(
        "agent_kernel.runtime.kernel_runtime.TemporalKernelWorker.run",
        side_effect=_fake_worker_run,
    ):
        kernel = await KernelRuntime.start(config, temporal_client=mock_client)
        await kernel.stop()

    assert _RUN_ACTOR_CONFIG_FALLBACK["dependencies"] is None


@pytest.mark.asyncio
async def test_kernel_runtime_worker_failed_false_while_running() -> None:
    """worker_failed must be False while the task is still running."""
    mock_client = _make_mock_temporal_client()
    config = KernelRuntimeConfig(task_queue="test-q")

    with patch(
        "agent_kernel.runtime.kernel_runtime.TemporalKernelWorker.run",
        side_effect=_fake_worker_run,
    ):
        async with await KernelRuntime.start(config, temporal_client=mock_client) as kernel:
            assert kernel.worker_failed is False


@pytest.mark.asyncio
async def test_kernel_runtime_check_worker_raises_on_failure() -> None:
    """check_worker() must raise if the background task exited with an error."""
    mock_client = _make_mock_temporal_client()
    config = KernelRuntimeConfig(task_queue="test-q")

    async def _fail_immediately() -> None:
        raise RuntimeError("worker boom")

    with patch(
        "agent_kernel.runtime.kernel_runtime.TemporalKernelWorker.run",
        side_effect=_fail_immediately,
    ):
        kernel = await KernelRuntime.start(config, temporal_client=mock_client)
        # Let the task run and fail.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert kernel.worker_failed
        with pytest.raises(RuntimeError, match="worker boom"):
            kernel.check_worker()
        # Cleanup without re-raising
        kernel._worker_task.cancel()


@pytest.mark.asyncio
async def test_kernel_runtime_no_config_uses_defaults() -> None:
    """Omitting config must use KernelRuntimeConfig() defaults."""
    mock_client = _make_mock_temporal_client()

    with patch(
        "agent_kernel.runtime.kernel_runtime.TemporalKernelWorker.run",
        side_effect=_fake_worker_run,
    ):
        async with await KernelRuntime.start(temporal_client=mock_client) as kernel:
            assert kernel._worker_task.get_name() == "kernel-worker:agent-kernel"


@pytest.mark.asyncio
async def test_kernel_runtime_worker_done_callback_fires_on_cancel() -> None:
    """Worker done callback must be invoked when the task is cancelled via stop()."""
    mock_client = _make_mock_temporal_client()
    config = KernelRuntimeConfig(task_queue="test-q")
    fired: list[Any] = []

    with patch(
        "agent_kernel.runtime.kernel_runtime.TemporalKernelWorker.run",
        side_effect=_fake_worker_run,
    ):
        kernel = await KernelRuntime.start(config, temporal_client=mock_client)
        kernel.add_worker_done_callback(lambda task: fired.append(task))
        await kernel.stop()

    assert len(fired) == 1
    assert fired[0].done()


@pytest.mark.asyncio
async def test_kernel_runtime_worker_done_callback_fires_on_failure() -> None:
    """Worker done callback must be invoked when the task exits with an error."""
    mock_client = _make_mock_temporal_client()
    config = KernelRuntimeConfig(task_queue="test-q")
    fired: list[Any] = []

    async def _fail_immediately() -> None:
        raise RuntimeError("boom")

    with patch(
        "agent_kernel.runtime.kernel_runtime.TemporalKernelWorker.run",
        side_effect=_fail_immediately,
    ):
        kernel = await KernelRuntime.start(config, temporal_client=mock_client)
        kernel.add_worker_done_callback(lambda task: fired.append(task))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert len(fired) == 1
    assert fired[0].exception() is not None
    kernel._worker_task.cancel()


@pytest.mark.asyncio
async def test_kernel_runtime_services_share_event_log_instance() -> None:
    """The event log used by the bundle must be the same Python object
    that the projection service holds — no state divergence possible."""
    mock_client = _make_mock_temporal_client()
    config = KernelRuntimeConfig()

    with patch(
        "agent_kernel.runtime.kernel_runtime.TemporalKernelWorker.run",
        side_effect=_fake_worker_run,
    ):
        async with await KernelRuntime.start(config, temporal_client=mock_client) as kernel:
            deps = kernel._deps
            projection = deps.projection
            assert isinstance(projection, InMemoryDecisionProjectionService)
            assert projection._event_log is deps.event_log
