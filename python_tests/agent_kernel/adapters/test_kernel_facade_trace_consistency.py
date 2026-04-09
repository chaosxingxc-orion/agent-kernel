"""Tests for cross-instance trace state consistency via event log replay.

Verifies that branch/stage/human-gate state written by one KernelFacade
instance can be reconstructed by a second instance backed by the same
event log.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from agent_kernel.adapters.facade.kernel_facade import KernelFacade
from agent_kernel.kernel.contracts import (
    ApprovalRequest,
    HumanGateRequest,
    OpenBranchRequest,
    RunProjection,
)
from agent_kernel.kernel.minimal_runtime import InMemoryKernelRuntimeEventLog

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_gateway(lifecycle_state: str = "running") -> MagicMock:
    gw = MagicMock()
    gw.query_projection = AsyncMock(
        return_value=RunProjection(
            run_id="run-1",
            lifecycle_state=lifecycle_state,  # type: ignore[arg-type]
            projected_offset=0,
            waiting_external=False,
            ready_for_dispatch=True,
        ),
    )
    gw.signal_run = AsyncMock()
    gw.signal_workflow = AsyncMock()
    return gw


def _make_facade(
    event_log: InMemoryKernelRuntimeEventLog,
    gateway: MagicMock | None = None,
) -> KernelFacade:
    return KernelFacade(
        workflow_gateway=gateway or _make_gateway(),
        event_log=event_log,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBranchStateSurvivesFacadeRestart:
    """open_branch on facade1 should be visible via facade2."""

    def test_branch_state_survives_facade_restart(self) -> None:
        event_log = InMemoryKernelRuntimeEventLog()
        gw = _make_gateway()

        facade1 = _make_facade(event_log, gateway=gw)
        asyncio.run(
            facade1.open_branch(
                OpenBranchRequest(
                    run_id="run-1",
                    branch_id="b-1",
                    stage_id="route",
                    parent_branch_id=None,
                    proposed_by="test",
                )
            )
        )

        # New facade instance, same event log, fresh in-memory registries.
        facade2 = _make_facade(event_log, gateway=gw)
        view = asyncio.run(facade2.query_trace_runtime("run-1"))

        assert len(view.branches) == 1
        assert view.branches[0].branch_id == "b-1"
        assert view.branches[0].stage_id == "route"
        assert view.branches[0].state == "active"


class TestStageStateSurvivesFacadeRestart:
    """open_stage on facade1 should be visible via facade2."""

    def test_stage_state_survives_facade_restart(self) -> None:
        event_log = InMemoryKernelRuntimeEventLog()
        gw = _make_gateway()

        facade1 = _make_facade(event_log, gateway=gw)
        asyncio.run(facade1.open_stage("route", "run-1", branch_id=None))

        facade2 = _make_facade(event_log, gateway=gw)
        view = asyncio.run(facade2.query_trace_runtime("run-1"))

        assert len(view.stages) == 1
        assert view.stages[0].stage_id == "route"
        assert view.stages[0].state == "active"


class TestHumanGateStateSurvivesFacadeRestart:
    """open_human_gate + submit_approval on facade1 should be visible via facade2."""

    def test_human_gate_state_survives_facade_restart(self) -> None:
        event_log = InMemoryKernelRuntimeEventLog()
        gw = _make_gateway()

        facade1 = _make_facade(event_log, gateway=gw)
        asyncio.run(
            facade1.open_human_gate(
                HumanGateRequest(
                    gate_ref="gate-1",
                    gate_type="gate_a",  # type: ignore[arg-type]
                    run_id="run-1",
                    trigger_reason="test",
                    trigger_source="system",
                )
            )
        )

        # Facade2 should see the open gate as pending.
        facade2 = _make_facade(event_log, gateway=gw)
        view = asyncio.run(facade2.query_trace_runtime("run-1"))
        assert view.review_state == "pending"

    def test_resolved_gate_survives_facade_restart(self) -> None:
        event_log = InMemoryKernelRuntimeEventLog()
        gw = _make_gateway()

        facade1 = _make_facade(event_log, gateway=gw)
        asyncio.run(
            facade1.open_human_gate(
                HumanGateRequest(
                    gate_ref="gate-1",
                    gate_type="gate_a",  # type: ignore[arg-type]
                    run_id="run-1",
                    trigger_reason="test",
                    trigger_source="system",
                )
            )
        )
        asyncio.run(
            facade1.submit_approval(
                ApprovalRequest(
                    run_id="run-1",
                    approval_ref="gate-1",
                    approved=True,
                    reviewer_id="human-1",
                )
            )
        )

        facade2 = _make_facade(event_log, gateway=gw)
        view = asyncio.run(facade2.query_trace_runtime("run-1"))
        assert view.review_state == "completed"


class TestCrossFadeTraceMerge:
    """Two facades sharing an event log must see each other's trace entries."""

    def test_cross_facade_trace_merge(self) -> None:
        """Facade A opens branch, Facade B opens stage (same run).

        When Facade B queries trace for the run it must see both its own
        stage AND Facade A's branch — even though Facade B already has
        local state for the run.
        """
        event_log = InMemoryKernelRuntimeEventLog()
        gw = _make_gateway()

        facade_a = _make_facade(event_log, gateway=gw)
        facade_b = _make_facade(event_log, gateway=gw)

        # Facade A creates a branch.
        asyncio.run(
            facade_a.open_branch(
                OpenBranchRequest(
                    run_id="run-1",
                    branch_id="branch-from-a",
                    stage_id="route",
                    parent_branch_id=None,
                    proposed_by="facade-a",
                )
            )
        )

        # Facade B creates a stage (gives it local state for run-1).
        asyncio.run(facade_b.open_stage("plan", "run-1", branch_id=None))

        # Facade B queries — must see BOTH its own stage AND A's branch.
        view = asyncio.run(facade_b.query_trace_runtime("run-1"))

        branch_ids = {b.branch_id for b in view.branches}
        stage_ids = {s.stage_id for s in view.stages}
        assert "branch-from-a" in branch_ids, (
            "Branch created by facade A is missing from facade B's trace view"
        )
        assert "plan" in stage_ids, (
            "Stage created by facade B itself is missing from its trace view"
        )
