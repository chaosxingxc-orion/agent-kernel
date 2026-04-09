"""Tests for LocalWorkflowGateway.execute_turn()."""

from __future__ import annotations

import pytest

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


def _make_action(run_id: str = "run-001", action_id: str = "act-001") -> Action:
    return Action(
        action_id=action_id,
        run_id=run_id,
        action_type="trace_stage",
        effect_class=EffectClass.READ_ONLY,
        input_json={"node_id": "S1"},
    )


@pytest.mark.asyncio
async def test_execute_turn_returns_dispatched() -> None:
    deps = _make_deps()
    configure_run_actor_dependencies(deps)
    gw = LocalWorkflowGateway(deps)

    async def handler(action: Action, grant: str | None) -> dict:
        return {"result": "done"}

    result = await gw.execute_turn(
        "run-001",
        _make_action(),
        handler,
        idempotency_key="S1:0",
    )
    assert result.outcome_kind == "dispatched"
    assert result.state == "effect_recorded"
    assert result.action_commit is not None


@pytest.mark.asyncio
async def test_execute_turn_is_idempotent() -> None:
    deps = _make_deps()
    configure_run_actor_dependencies(deps)
    gw = LocalWorkflowGateway(deps)
    call_count = 0

    async def handler(action: Action, grant: str | None) -> dict:
        nonlocal call_count
        call_count += 1
        return {"call": call_count}

    action = _make_action()
    r1 = await gw.execute_turn("run-001", action, handler, idempotency_key="k1")
    r2 = await gw.execute_turn("run-001", action, handler, idempotency_key="k1")

    assert r1.decision_ref == r2.decision_ref
    assert call_count == 1  # handler called exactly once


@pytest.mark.asyncio
async def test_execute_turn_different_keys_call_handler_each_time() -> None:
    deps = _make_deps()
    configure_run_actor_dependencies(deps)
    gw = LocalWorkflowGateway(deps)
    call_count = 0

    async def handler(action: Action, grant: str | None) -> dict:
        nonlocal call_count
        call_count += 1
        return {}

    action = _make_action()
    await gw.execute_turn("run-001", action, handler, idempotency_key="k1")
    await gw.execute_turn("run-001", action, handler, idempotency_key="k2")

    assert call_count == 2
