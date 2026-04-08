"""Tests for event types added in v0.2: approval_submitted."""

from __future__ import annotations

from agent_kernel.kernel.event_registry import KERNEL_EVENT_REGISTRY


class TestApprovalEvents:
    def test_approval_submitted_registered(self) -> None:
        assert KERNEL_EVENT_REGISTRY.get("run.approval_submitted") is not None

    def test_approval_submitted_affects_replay(self) -> None:
        d = KERNEL_EVENT_REGISTRY.get("run.approval_submitted")
        assert d is not None
        assert d.affects_replay is True

    def test_approval_in_known_types(self) -> None:
        known = KERNEL_EVENT_REGISTRY.known_types()
        assert "run.approval_submitted" in known

    def test_manifest_includes_approval_event_type(self) -> None:
        """KernelFacade.get_manifest() must surface the approval event type."""
        from unittest.mock import AsyncMock

        from agent_kernel.adapters.facade.kernel_facade import KernelFacade

        gateway = AsyncMock()
        facade = KernelFacade(gateway)
        manifest = facade.get_manifest()
        assert "run.approval_submitted" in manifest.supported_event_types
