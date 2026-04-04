"""Tests for new event types added in v0.2: plan_submitted, approval_submitted,
speculation_committed."""

from __future__ import annotations

from agent_kernel.kernel.event_registry import KERNEL_EVENT_REGISTRY


class TestPlanApprovalSpeculationEvents:
    def test_plan_submitted_registered(self) -> None:
        assert KERNEL_EVENT_REGISTRY.get("run.plan_submitted") is not None

    def test_approval_submitted_registered(self) -> None:
        assert KERNEL_EVENT_REGISTRY.get("run.approval_submitted") is not None

    def test_speculation_committed_registered(self) -> None:
        assert KERNEL_EVENT_REGISTRY.get("run.speculation_committed") is not None

    def test_plan_submitted_affects_replay(self) -> None:
        d = KERNEL_EVENT_REGISTRY.get("run.plan_submitted")
        assert d is not None
        assert d.affects_replay is True

    def test_approval_submitted_affects_replay(self) -> None:
        d = KERNEL_EVENT_REGISTRY.get("run.approval_submitted")
        assert d is not None
        assert d.affects_replay is True

    def test_speculation_committed_affects_replay(self) -> None:
        d = KERNEL_EVENT_REGISTRY.get("run.speculation_committed")
        assert d is not None
        assert d.affects_replay is True

    def test_all_three_in_known_types(self) -> None:
        known = KERNEL_EVENT_REGISTRY.known_types()
        assert "run.plan_submitted" in known
        assert "run.approval_submitted" in known
        assert "run.speculation_committed" in known

    def test_manifest_includes_new_event_types(self) -> None:
        """KernelFacade.get_manifest() must surface the three new event types."""
        from unittest.mock import AsyncMock

        from agent_kernel.adapters.facade.kernel_facade import KernelFacade

        gateway = AsyncMock()
        facade = KernelFacade(gateway)
        manifest = facade.get_manifest()
        assert "run.plan_submitted" in manifest.supported_event_types
        assert "run.approval_submitted" in manifest.supported_event_types
        assert "run.speculation_committed" in manifest.supported_event_types
