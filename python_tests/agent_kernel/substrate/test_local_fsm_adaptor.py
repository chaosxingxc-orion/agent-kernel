"""Tests for LocalFSMAdaptor and LocalWorkflowGateway."""

from __future__ import annotations

import pytest

from agent_kernel.kernel.contracts import (
    QueryRunRequest,
    SignalRunRequest,
    SpawnChildRunRequest,
    StartRunRequest,
)
from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore
from agent_kernel.kernel.minimal_runtime import (
    AsyncExecutorService,
    InMemoryDecisionDeduper,
    InMemoryDecisionProjectionService,
    InMemoryKernelRuntimeEventLog,
    StaticDispatchAdmissionService,
    StaticRecoveryGateService,
)
from agent_kernel.runtime.kernel_runtime import KernelRuntime, KernelRuntimeConfig
from agent_kernel.substrate.local.adaptor import (
    LocalFSMAdaptor,
    LocalSubstrateConfig,
    LocalWorkflowGateway,
)
from agent_kernel.substrate.temporal.run_actor_workflow import (
    RunActorDependencyBundle,
    RunActorStrictModeConfig,
    configure_run_actor_dependencies,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_deps() -> RunActorDependencyBundle:
    event_log = InMemoryKernelRuntimeEventLog()
    return RunActorDependencyBundle(
        event_log=event_log,
        projection=InMemoryDecisionProjectionService(event_log),
        admission=StaticDispatchAdmissionService(),
        executor=AsyncExecutorService(),
        recovery=StaticRecoveryGateService(),
        deduper=InMemoryDecisionDeduper(),
        dedupe_store=InMemoryDedupeStore(),
        strict_mode=RunActorStrictModeConfig(enabled=False),
        workflow_id_prefix="run",
    )


# ---------------------------------------------------------------------------
# LocalSubstrateConfig
# ---------------------------------------------------------------------------


def test_local_substrate_config_defaults() -> None:
    config = LocalSubstrateConfig()
    assert config.workflow_id_prefix == "run"
    assert config.strict_mode_enabled is True


def test_local_substrate_config_custom() -> None:
    config = LocalSubstrateConfig(workflow_id_prefix="agent", strict_mode_enabled=False)
    assert config.workflow_id_prefix == "agent"
    assert config.strict_mode_enabled is False


# ---------------------------------------------------------------------------
# LocalFSMAdaptor lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_fsm_adaptor_start_exposes_gateway() -> None:
    """start() must wire a LocalWorkflowGateway accessible via .gateway."""
    deps = _make_deps()
    adaptor = LocalFSMAdaptor(LocalSubstrateConfig())
    await adaptor.start(deps)
    try:
        assert adaptor.gateway is not None
        assert isinstance(adaptor.gateway, LocalWorkflowGateway)
    finally:
        await adaptor.stop()


@pytest.mark.asyncio
async def test_local_fsm_adaptor_gateway_before_start_raises() -> None:
    """Accessing .gateway before start() must raise RuntimeError."""
    adaptor = LocalFSMAdaptor(LocalSubstrateConfig())
    with pytest.raises(RuntimeError, match=r"start\(\)"):
        _ = adaptor.gateway


@pytest.mark.asyncio
async def test_local_fsm_adaptor_stop_is_idempotent() -> None:
    """stop() must be safe to call multiple times."""
    deps = _make_deps()
    adaptor = LocalFSMAdaptor(LocalSubstrateConfig())
    await adaptor.start(deps)
    await adaptor.stop()
    await adaptor.stop()  # must not raise


@pytest.mark.asyncio
async def test_local_fsm_adaptor_worker_failed_always_false() -> None:
    """worker_failed is always False — no background worker task."""
    deps = _make_deps()
    adaptor = LocalFSMAdaptor(LocalSubstrateConfig())
    await adaptor.start(deps)
    try:
        assert adaptor.worker_failed is False
    finally:
        await adaptor.stop()


@pytest.mark.asyncio
async def test_local_fsm_adaptor_clears_deps_on_stop() -> None:
    """stop() must clear the process-local dependency registry."""
    from agent_kernel.substrate.temporal.run_actor_workflow import (
        _RUN_ACTOR_CONFIG_FALLBACK,
    )

    deps = _make_deps()
    adaptor = LocalFSMAdaptor(LocalSubstrateConfig())
    await adaptor.start(deps)
    await adaptor.stop()
    assert _RUN_ACTOR_CONFIG_FALLBACK["dependencies"] is None


# ---------------------------------------------------------------------------
# LocalWorkflowGateway: start + query
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gateway_start_workflow_returns_run_id() -> None:
    """start_workflow must return a dict with run_id and workflow_id."""
    deps = _make_deps()
    configure_run_actor_dependencies(deps)
    gw = LocalWorkflowGateway(deps)

    result = await gw.start_workflow(StartRunRequest(run_kind="task", initiator="test"))
    assert "run_id" in result
    assert "workflow_id" in result
    assert result["run_id"] == "task"
    assert result["workflow_id"] == "run:task"


@pytest.mark.asyncio
async def test_gateway_start_workflow_explicit_run_id_in_input_json() -> None:
    """run_id in input_json takes precedence over run_kind."""
    deps = _make_deps()
    configure_run_actor_dependencies(deps)
    gw = LocalWorkflowGateway(deps)

    result = await gw.start_workflow(
        StartRunRequest(
            run_kind="task",
            initiator="test",
            input_json={"run_id": "my-explicit-run"},
        )
    )
    assert result["run_id"] == "my-explicit-run"


@pytest.mark.asyncio
async def test_gateway_start_workflow_duplicate_run_id_raises() -> None:
    """Starting the same run_id twice must fail to prevent state overwrite."""
    deps = _make_deps()
    configure_run_actor_dependencies(deps)
    gw = LocalWorkflowGateway(deps)
    request = StartRunRequest(
        run_kind="task",
        initiator="test",
        input_json={"run_id": "dup-run"},
    )
    await gw.start_workflow(request)
    with pytest.raises(ValueError, match="duplicate run_id"):
        await gw.start_workflow(request)


@pytest.mark.asyncio
async def test_gateway_query_projection_returns_run_projection() -> None:
    """query_projection must return a RunProjection for a started run."""
    deps = _make_deps()
    configure_run_actor_dependencies(deps)
    gw = LocalWorkflowGateway(deps)

    await gw.start_workflow(StartRunRequest(run_kind="task", initiator="test"))
    projection = await gw.query_projection("task")
    assert projection.run_id == "task"


@pytest.mark.asyncio
async def test_gateway_query_projection_unknown_run_falls_back_to_projection_service() -> None:
    """query_projection for an unknown run falls back to DecisionProjectionService."""
    deps = _make_deps()
    configure_run_actor_dependencies(deps)
    gw = LocalWorkflowGateway(deps)

    # No start_workflow called — falls back to projection service
    projection = await gw.query_projection("unknown-run")
    assert projection.run_id == "unknown-run"


# ---------------------------------------------------------------------------
# LocalWorkflowGateway: signal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gateway_signal_unknown_run_raises_key_error() -> None:
    """signal_workflow for an unknown run_id must raise KeyError."""
    deps = _make_deps()
    configure_run_actor_dependencies(deps)
    gw = LocalWorkflowGateway(deps)

    with pytest.raises(KeyError, match="not found"):
        await gw.signal_workflow(
            "nonexistent",
            SignalRunRequest(run_id="nonexistent", signal_type="noop"),
        )


# ---------------------------------------------------------------------------
# LocalWorkflowGateway: cancel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gateway_cancel_removes_run_from_registry() -> None:
    """cancel_workflow must remove the run from the gateway's registry."""
    deps = _make_deps()
    configure_run_actor_dependencies(deps)
    gw = LocalWorkflowGateway(deps)

    await gw.start_workflow(StartRunRequest(run_kind="task", initiator="test"))
    assert "task" in gw._workflows

    await gw.cancel_workflow("task", "test cancellation")
    assert "task" not in gw._workflows


@pytest.mark.asyncio
async def test_gateway_cancel_nonexistent_run_is_safe() -> None:
    """cancel_workflow for an unknown run must not raise."""
    deps = _make_deps()
    configure_run_actor_dependencies(deps)
    gw = LocalWorkflowGateway(deps)

    await gw.cancel_workflow("nonexistent", "cleanup")  # must not raise


# ---------------------------------------------------------------------------
# LocalWorkflowGateway: child workflow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gateway_start_child_workflow_builds_child_run_id() -> None:
    """start_child_workflow must produce a child run_id scoped to parent."""
    deps = _make_deps()
    configure_run_actor_dependencies(deps)
    gw = LocalWorkflowGateway(deps)

    result = await gw.start_child_workflow(
        "parent-run",
        SpawnChildRunRequest(
            parent_run_id="parent-run",
            child_kind="subtask",
        ),
    )
    assert result["run_id"] == "parent-run:subtask"


# ---------------------------------------------------------------------------
# LocalWorkflowGateway: event stream
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gateway_stream_run_events_empty_for_new_run() -> None:
    """stream_run_events yields nothing for a run with no committed events."""
    deps = _make_deps()
    configure_run_actor_dependencies(deps)
    gw = LocalWorkflowGateway(deps)

    events = [e async for e in gw.stream_run_events("new-run")]
    assert events == []


# ---------------------------------------------------------------------------
# KernelRuntime integration with LocalSubstrateConfig
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kernel_runtime_local_substrate_start_stop() -> None:
    """KernelRuntime with LocalSubstrateConfig must start and stop cleanly."""
    config = KernelRuntimeConfig(
        substrate=LocalSubstrateConfig(strict_mode_enabled=False),
        event_log_backend="in_memory",
    )
    async with await KernelRuntime.start(config) as kernel:
        assert kernel.facade is not None
        assert kernel.gateway is not None
        assert kernel.health is not None
        assert kernel.worker_failed is False
        assert kernel.facade.get_manifest().substrate_type == "local_fsm"
        readiness = kernel.facade.get_health_readiness()
        assert readiness["status"] == "ok"
        assert "event_log" in readiness["checks"]


@pytest.mark.asyncio
async def test_kernel_runtime_local_substrate_start_run() -> None:
    """KernelRuntime.facade.start_run() must return a valid response in local mode."""
    config = KernelRuntimeConfig(
        substrate=LocalSubstrateConfig(strict_mode_enabled=False),
    )
    async with await KernelRuntime.start(config) as kernel:
        response = await kernel.facade.start_run(
            StartRunRequest(run_kind="integration-test", initiator="pytest")
        )
        assert response.run_id == "integration-test"
        assert response.lifecycle_state == "created"


@pytest.mark.asyncio
async def test_kernel_runtime_local_substrate_query_run() -> None:
    """query_run must return a projection for a started run in local mode."""
    config = KernelRuntimeConfig(
        substrate=LocalSubstrateConfig(strict_mode_enabled=False),
    )
    async with await KernelRuntime.start(config) as kernel:
        await kernel.facade.start_run(StartRunRequest(run_kind="q-test", initiator="pytest"))
        response = await kernel.facade.query_run(QueryRunRequest(run_id="q-test"))
        assert response.run_id == "q-test"


@pytest.mark.asyncio
async def test_kernel_runtime_local_substrate_no_worker_task() -> None:
    """LocalFSMAdaptor must expose _worker_task=None (no background Temporal worker)."""
    config = KernelRuntimeConfig(
        substrate=LocalSubstrateConfig(strict_mode_enabled=False),
    )
    async with await KernelRuntime.start(config) as kernel:
        assert kernel._worker_task is None
