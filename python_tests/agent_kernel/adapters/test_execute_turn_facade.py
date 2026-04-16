"""Verifies for kernelfacade.execute turn() with localworkflowgateway substrate."""

from __future__ import annotations

import pytest

from agent_kernel.adapters.facade.kernel_facade import KernelFacade
from agent_kernel.kernel.contracts import Action, EffectClass
from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore
from agent_kernel.kernel.minimal_runtime import (
    AsyncExecutorService,
    InMemoryDecisionDeduper,
    InMemoryDecisionProjectionService,
    InMemoryKernelRuntimeEventLog,
    StaticDispatchAdmissionService,
    StaticRecoveryGateService,
)
from agent_kernel.substrate.local.adaptor import LocalWorkflowGateway
from agent_kernel.substrate.temporal.run_actor_workflow import (
    RunActorDependencyBundle,
    RunActorStrictModeConfig,
    configure_run_actor_dependencies,
)


def _make_deps() -> RunActorDependencyBundle:
    """Make deps."""
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


def _make_facade() -> KernelFacade:
    """Make facade."""
    deps = _make_deps()
    configure_run_actor_dependencies(deps)
    gateway = LocalWorkflowGateway(deps)
    return KernelFacade(workflow_gateway=gateway, substrate_type="local_fsm")


@pytest.mark.asyncio
async def test_facade_execute_turn_returns_dispatched() -> None:
    """Verifies facade execute turn returns dispatched."""
    facade = _make_facade()

    action = Action(
        action_id="act-001",
        run_id="run-001",
        action_type="trace_stage",
        effect_class=EffectClass.READ_ONLY,
        input_json={"node_id": "S1"},
    )

    async def handler(action: Action, grant: str | None) -> dict:
        """Handles the test callback invocation."""
        return {"output": "done"}

    result = await facade.execute_turn(
        run_id="run-001",
        action=action,
        handler=handler,
        idempotency_key="S1:0",
    )
    assert result.outcome_kind == "dispatched"
    assert result.state == "effect_recorded"
    assert result.action_commit is not None


@pytest.mark.asyncio
async def test_facade_execute_turn_idempotent() -> None:
    """Verifies facade execute turn idempotent."""
    facade = _make_facade()
    call_count = 0

    action = Action(
        action_id="act-002",
        run_id="run-002",
        action_type="trace_stage",
        effect_class=EffectClass.READ_ONLY,
        input_json={},
    )

    async def handler(action: Action, grant: str | None) -> dict:
        """Handles the test callback invocation."""
        nonlocal call_count
        call_count += 1
        return {}

    r1 = await facade.execute_turn("run-002", action, handler, idempotency_key="k1")
    r2 = await facade.execute_turn("run-002", action, handler, idempotency_key="k1")

    assert r1.decision_ref == r2.decision_ref
    assert call_count == 1
