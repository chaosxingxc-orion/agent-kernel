"""Minimal Temporal E2E for the real RunActorWorkflow."""

from __future__ import annotations

import asyncio
import inspect
from datetime import timedelta

import pytest

from agent_kernel.kernel.contracts import SignalRunRequest, StartRunRequest
from agent_kernel.kernel.minimal_runtime import (
    AsyncExecutorService,
    InMemoryDecisionDeduper,
    InMemoryDecisionProjectionService,
    InMemoryKernelRuntimeEventLog,
    StaticDispatchAdmissionService,
    StaticRecoveryGateService,
)
from agent_kernel.substrate.temporal.gateway import (
    TemporalGatewayConfig,
    TemporalSDKWorkflowGateway,
)
from agent_kernel.substrate.temporal.run_actor_workflow import (
    RunActorDependencyBundle,
    RunActorWorkflow,
    configure_run_actor_dependencies,
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


def _in_temporal_workflow_context() -> bool:
    """Returns whether current execution is inside Temporal workflow loop."""
    if not TEMPORAL_AVAILABLE or temporal_workflow is None:
        return False
    try:
        return bool(temporal_workflow.in_workflow())
    except RuntimeError:
        return False


class _DelayedFirstGetProjectionService(InMemoryDecisionProjectionService):
    """Delays the first projection read so a callback signal can be processed."""

    def __init__(self, event_log: InMemoryKernelRuntimeEventLog) -> None:
        super().__init__(event_log)
        self._delay_consumed = False

    async def get(self, run_id: str):  # type: ignore[override]
        if (
            not self._delay_consumed
            and _in_temporal_workflow_context()
        ):
            self._delay_consumed = True
            await temporal_workflow.sleep(timedelta(seconds=1))
        return await super().get(run_id)


class _PerRunDelayedFirstGetProjectionService(InMemoryDecisionProjectionService):
    """Delays first projection read only for selected run ids.

    This helper keeps parent workflow alive long enough for the test to deliver
    both spawn and completion child lifecycle signals.
    """

    def __init__(
        self,
        event_log: InMemoryKernelRuntimeEventLog,
        delayed_run_ids: set[str],
        delay_seconds: int,
    ) -> None:
        super().__init__(event_log)
        self._remaining_delayed_run_ids = set(delayed_run_ids)
        self._delay_seconds = delay_seconds

    async def get(self, run_id: str):  # type: ignore[override]
        if (
            run_id in self._remaining_delayed_run_ids
            and _in_temporal_workflow_context()
        ):
            self._remaining_delayed_run_ids.remove(run_id)
            await temporal_workflow.sleep(timedelta(seconds=self._delay_seconds))
        return await super().get(run_id)


def _ensure_run_actor_workflow_is_temporal_workflow() -> None:
    """Adds Temporal workflow metadata to the real RunActorWorkflow class."""
    if hasattr(RunActorWorkflow, "__temporal_workflow_definition"):
        return
    # Temporal Python SDK recommends synchronous query handlers and emits
    # deprecation warnings for async query methods. Keep this assertion so the
    # integration contract does not regress back to async query shape.
    assert not inspect.iscoroutinefunction(RunActorWorkflow.query)
    temporal_workflow.run(RunActorWorkflow.run)
    temporal_workflow.signal(RunActorWorkflow.signal)
    temporal_workflow.query(RunActorWorkflow.query)
    temporal_workflow.defn(sandboxed=False)(RunActorWorkflow)


@pytest.fixture(autouse=True)
def _cleanup_run_actor_dependency_config() -> None:
    """Ensures process-local dependency override never leaks across tests."""
    configure_run_actor_dependencies(None)
    yield
    configure_run_actor_dependencies(None)


@pytest.mark.asyncio
async def test_real_run_actor_workflow_runs_signal_and_query_in_temporal_test_env() -> None:
    """RunActorWorkflow should run through start->callback signal->projection query."""
    assert WorkflowEnvironment is not None
    assert Worker is not None
    assert UnsandboxedWorkflowRunner is not None

    _ensure_run_actor_workflow_is_temporal_workflow()

    run_id = "run-actor-temporal-int-1"
    event_log = InMemoryKernelRuntimeEventLog()
    configure_run_actor_dependencies(
        RunActorDependencyBundle(
            event_log=event_log,
            projection=_DelayedFirstGetProjectionService(event_log),
            admission=StaticDispatchAdmissionService(),
            executor=AsyncExecutorService(),
            recovery=StaticRecoveryGateService(),
            deduper=InMemoryDecisionDeduper(),
        )
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        gateway = TemporalSDKWorkflowGateway(
            env.client,
            TemporalGatewayConfig(
                task_queue="run-actor-workflow-temporal-int-q",
                workflow_run_callable=RunActorWorkflow.run,
                signal_method_name="signal",
                query_method_name="query",
            ),
        )
        async with Worker(
            env.client,
            task_queue="run-actor-workflow-temporal-int-q",
            workflows=[RunActorWorkflow],
            workflow_runner=UnsandboxedWorkflowRunner(),
        ):
            started = await gateway.start_workflow(
                StartRunRequest(
                    initiator="system",
                    run_kind="integration",
                    input_json={"run_id": run_id},
                )
            )
            await gateway.signal_workflow(
                run_id,
                SignalRunRequest(
                    run_id=run_id,
                    signal_type="callback",
                    signal_payload={"ok": True},
                    caused_by="cb-run-actor-temporal-int-1",
                ),
            )

            projection = None
            for _ in range(30):
                projection = await gateway.query_projection(run_id)
                if projection.projected_offset >= 1:
                    break
                await asyncio.sleep(0.1)

            assert projection is not None
            assert started["run_id"] == run_id
            assert started["workflow_id"] == f"run:{run_id}"
            assert projection.run_id == run_id
            assert projection.lifecycle_state == "ready"
            assert projection.projected_offset >= 1


@pytest.mark.asyncio
async def test_child_completion_signal_updates_parent_active_child_runs_in_temporal_env() -> None:
    """Child completion should remove child id from parent projection list."""

    assert WorkflowEnvironment is not None
    assert Worker is not None
    assert UnsandboxedWorkflowRunner is not None

    _ensure_run_actor_workflow_is_temporal_workflow()

    parent_run_id = "run-parent-temporal-int-1"
    child_run_id = "run-child-temporal-int-1"
    event_log = InMemoryKernelRuntimeEventLog()
    projection = _PerRunDelayedFirstGetProjectionService(
        event_log=event_log,
        delayed_run_ids={parent_run_id},
        delay_seconds=5,
    )
    configure_run_actor_dependencies(
        RunActorDependencyBundle(
            event_log=event_log,
            projection=projection,
            admission=StaticDispatchAdmissionService(),
            executor=AsyncExecutorService(),
            recovery=StaticRecoveryGateService(),
            deduper=InMemoryDecisionDeduper(),
        )
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        gateway = TemporalSDKWorkflowGateway(
            env.client,
            TemporalGatewayConfig(
                task_queue="run-actor-workflow-parent-child-int-q",
                workflow_run_callable=RunActorWorkflow.run,
                signal_method_name="signal",
                query_method_name="query",
            ),
        )
        async with Worker(
            env.client,
            task_queue="run-actor-workflow-parent-child-int-q",
            workflows=[RunActorWorkflow],
            workflow_runner=UnsandboxedWorkflowRunner(),
        ):
            parent_started = await gateway.start_workflow(
                StartRunRequest(
                    initiator="system",
                    run_kind="integration-parent",
                    input_json={"run_id": parent_run_id},
                )
            )
            await gateway.signal_workflow(
                parent_run_id,
                SignalRunRequest(
                    run_id=parent_run_id,
                    signal_type="child_spawned",
                    signal_payload={"child_run_id": child_run_id},
                    caused_by="spawn-child-temporal-int-1",
                ),
            )

            spawned_projection = None
            for _ in range(40):
                spawned_projection = await projection.get(parent_run_id)
                if child_run_id in spawned_projection.active_child_runs:
                    break
                await asyncio.sleep(0.1)

            assert spawned_projection is not None
            assert child_run_id in spawned_projection.active_child_runs

            child_started = await gateway.start_workflow(
                StartRunRequest(
                    initiator="system",
                    run_kind="integration-child",
                    parent_run_id=parent_run_id,
                    input_json={"run_id": child_run_id},
                )
            )

            completed_projection = None
            for _ in range(60):
                completed_projection = await projection.get(parent_run_id)
                if child_run_id not in completed_projection.active_child_runs:
                    break
                await asyncio.sleep(0.1)

            assert completed_projection is not None
            assert parent_started["run_id"] == parent_run_id
            assert child_started["run_id"] == child_run_id
            assert child_run_id not in completed_projection.active_child_runs
