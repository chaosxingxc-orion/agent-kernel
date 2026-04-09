"""Tests for KernelFacade run cleanup and bounded eviction."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from agent_kernel.adapters.facade.kernel_facade import KernelFacade
from agent_kernel.kernel.contracts import (
    ApprovalRequest,
    HumanGateRequest,
    OpenBranchRequest,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_gateway() -> MagicMock:
    gw = MagicMock()
    gw.signal_run = AsyncMock()
    gw.signal_workflow = AsyncMock()
    return gw


def _make_facade(
    *,
    gateway: MagicMock | None = None,
    max_tracked_runs: int = 10_000,
) -> KernelFacade:
    gw = gateway or _make_gateway()
    return KernelFacade(workflow_gateway=gw, max_tracked_runs=max_tracked_runs)


def _populate_all_registries(facade: KernelFacade, run_id: str) -> None:
    """Populate all 5 in-memory registries for *run_id* via public APIs."""
    # open_branch -> _branch_registry
    asyncio.run(
        facade.open_branch(
            OpenBranchRequest(
                run_id=run_id,
                branch_id=f"br-{run_id}",
                stage_id="route",
            )
        )
    )
    # open_stage -> _stage_registry
    asyncio.run(facade.open_stage(stage_id="capture", run_id=run_id))
    # submit_approval -> _submitted_approvals + _resolved_human_gates
    asyncio.run(
        facade.submit_approval(
            ApprovalRequest(
                run_id=run_id,
                approval_ref=f"apr-{run_id}",
                approved=True,
                reviewer_id="reviewer-1",
            )
        )
    )
    # open_human_gate -> _open_human_gates
    asyncio.run(
        facade.open_human_gate(
            HumanGateRequest(
                gate_ref=f"gate-{run_id}",
                gate_type="final_approval",
                run_id=run_id,
                trigger_reason="test",
                trigger_source="system",
            )
        )
    )


def _has_any_state(facade: KernelFacade, run_id: str) -> bool:
    """Return True if *run_id* has any residual state in any registry."""
    if run_id in facade._branch_registry:
        return True
    if run_id in facade._stage_registry:
        return True
    if any(k[0] == run_id for k in facade._submitted_approvals):
        return True
    if run_id in facade._open_human_gates:
        return True
    return run_id in facade._resolved_human_gates


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCleanupCompletedRun:
    """cleanup_completed_run removes ALL state for a given run_id."""

    def test_cleanup_completed_run_removes_all_state(self) -> None:
        facade = _make_facade()
        _populate_all_registries(facade, "run-1")

        # Precondition: state exists.
        assert _has_any_state(facade, "run-1")

        facade.cleanup_completed_run("run-1")

        # All five registries must be empty for this run_id.
        assert not _has_any_state(facade, "run-1")
        # Tracking structures must also be clean.
        assert "run-1" not in facade._tracked_run_set
        assert "run-1" not in facade._tracked_run_order

    def test_cleanup_nonexistent_run_is_noop(self) -> None:
        facade = _make_facade()

        # Should not raise.
        facade.cleanup_completed_run("nonexistent-run")

        # Tracking structures remain empty.
        assert len(facade._tracked_run_set) == 0
        assert len(facade._tracked_run_order) == 0

    def test_cleanup_preserves_other_runs(self) -> None:
        facade = _make_facade()
        _populate_all_registries(facade, "run-keep")
        _populate_all_registries(facade, "run-remove")

        facade.cleanup_completed_run("run-remove")

        assert not _has_any_state(facade, "run-remove")
        assert _has_any_state(facade, "run-keep")


class TestBoundedEviction:
    """Eviction removes the oldest run_id when over max_tracked_runs."""

    def test_eviction_removes_oldest_when_over_limit(self) -> None:
        facade = _make_facade(max_tracked_runs=3)

        # Populate 3 runs (at limit).
        for i in range(3):
            _populate_all_registries(facade, f"run-{i}")

        assert len(facade._tracked_run_set) == 3

        # Adding a 4th run should evict run-0.
        _populate_all_registries(facade, "run-3")

        assert len(facade._tracked_run_set) == 3
        assert "run-0" not in facade._tracked_run_set
        assert not _has_any_state(facade, "run-0")
        # run-1, run-2, run-3 should remain.
        for rid in ("run-1", "run-2", "run-3"):
            assert rid in facade._tracked_run_set

    def test_touch_same_run_does_not_duplicate(self) -> None:
        facade = _make_facade(max_tracked_runs=2)

        # Touch same run multiple times via different operations.
        _populate_all_registries(facade, "run-a")
        _populate_all_registries(facade, "run-a")

        assert len(facade._tracked_run_set) == 1
        assert facade._tracked_run_order.count("run-a") == 1

    def test_eviction_cascades_multiple(self) -> None:
        """Adding N runs beyond the limit evicts N oldest runs."""
        facade = _make_facade(max_tracked_runs=2)

        for i in range(5):
            _populate_all_registries(facade, f"run-{i}")

        # Only the last 2 should survive.
        assert len(facade._tracked_run_set) == 2
        assert facade._tracked_run_set == {"run-3", "run-4"}
        for evicted in ("run-0", "run-1", "run-2"):
            assert not _has_any_state(facade, evicted)
