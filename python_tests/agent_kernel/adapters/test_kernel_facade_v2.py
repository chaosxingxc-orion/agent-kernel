"""Tests for KernelFacade v0.2 improvements.

- injectable kernel_version
- substrate_limitations in KernelManifest
- approval_ref dedup gate
- get_health_readiness()
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from agent_kernel.adapters.facade.kernel_facade import KernelFacade
from agent_kernel.kernel.contracts import ApprovalRequest


def _make_facade(**kwargs) -> KernelFacade:
    """Make facade."""
    gateway = AsyncMock()
    gateway.start_workflow.return_value = {"workflow_id": "wf-1", "run_id": "run-1"}
    return KernelFacade(gateway, **kwargs)


def _inner_gw(facade: KernelFacade) -> AsyncMock:
    """Unwrap WorkflowGatewaySignalAdapter to reach the underlying AsyncMock."""
    return getattr(facade._workflow_gateway, "_gateway", facade._workflow_gateway)


# ---------------------------------------------------------------------------
# injectable kernel_version
# ---------------------------------------------------------------------------


class TestInjectableKernelVersion:
    """Test suite for InjectableKernelVersion."""

    def test_default_version_is_0_2_0(self) -> None:
        """Verifies default version is 0 2 0."""
        facade = _make_facade()
        assert facade.get_manifest().kernel_version == "0.2.0"

    def test_custom_version_reflected_in_manifest(self) -> None:
        """Verifies custom version reflected in manifest."""
        facade = _make_facade(kernel_version="1.0.0-beta")
        assert facade.get_manifest().kernel_version == "1.0.0-beta"

    def test_two_facades_can_have_different_versions(self) -> None:
        """Verifies two facades can have different versions."""
        f1 = _make_facade(kernel_version="0.1.0")
        f2 = _make_facade(kernel_version="0.3.0")
        assert f1.get_manifest().kernel_version == "0.1.0"
        assert f2.get_manifest().kernel_version == "0.3.0"


# ---------------------------------------------------------------------------
# substrate_limitations in KernelManifest
# ---------------------------------------------------------------------------


class TestSubstrateLimitations:
    """Test suite for SubstrateLimitations."""

    def test_temporal_has_no_limitations(self) -> None:
        """Verifies temporal has no limitations."""
        facade = _make_facade(substrate_type="temporal")
        manifest = facade.get_manifest()
        assert manifest.substrate_limitations == frozenset()

    def test_local_fsm_declares_no_child_workflow(self) -> None:
        """Verifies local fsm declares no child workflow."""
        facade = _make_facade(substrate_type="local_fsm")
        manifest = facade.get_manifest()
        assert "no_child_workflow_isolation" in manifest.substrate_limitations

    def test_local_fsm_declares_no_temporal_history(self) -> None:
        """Verifies local fsm declares no temporal history."""
        facade = _make_facade(substrate_type="local_fsm")
        manifest = facade.get_manifest()
        assert "no_temporal_history" in manifest.substrate_limitations

    def test_local_fsm_declares_no_cross_process_speculation(self) -> None:
        """Verifies local fsm declares no cross process speculation."""
        facade = _make_facade(substrate_type="local_fsm")
        manifest = facade.get_manifest()
        assert "no_cross_process_speculation" in manifest.substrate_limitations

    def test_unknown_substrate_returns_empty_limitations(self) -> None:
        """Verifies unknown substrate returns empty limitations."""
        facade = _make_facade(substrate_type="custom_substrate")
        manifest = facade.get_manifest()
        assert manifest.substrate_limitations == frozenset()

    def test_limitations_is_frozenset(self) -> None:
        """Verifies limitations is frozenset."""
        facade = _make_facade(substrate_type="local_fsm")
        assert isinstance(facade.get_manifest().substrate_limitations, frozenset)


# ---------------------------------------------------------------------------
# approval_ref dedup gate
# ---------------------------------------------------------------------------


class TestApprovalRefDedup:
    """Test suite for ApprovalRefDedup."""

    def test_first_submission_signals_workflow(self) -> None:
        """Verifies first submission signals workflow."""
        facade = _make_facade()
        request = ApprovalRequest(
            run_id="run-1",
            approval_ref="appr-001",
            approved=True,
            reviewer_id="alice",
        )
        asyncio.run(facade.submit_approval(request))
        _inner_gw(facade).signal_workflow.assert_awaited_once()

    def test_duplicate_approval_ref_is_dropped(self) -> None:
        """Verifies duplicate approval ref is dropped."""
        facade = _make_facade()
        request = ApprovalRequest(
            run_id="run-1",
            approval_ref="appr-001",
            approved=True,
            reviewer_id="alice",
        )
        asyncio.run(facade.submit_approval(request))
        asyncio.run(facade.submit_approval(request))
        # Signal should only fire once despite two calls
        assert _inner_gw(facade).signal_workflow.await_count == 1

    def test_same_ref_different_run_both_signal(self) -> None:
        """Verifies same ref different run both signal."""
        facade = _make_facade()
        r1 = ApprovalRequest(
            run_id="run-1", approval_ref="appr-001", approved=True, reviewer_id="alice"
        )
        r2 = ApprovalRequest(
            run_id="run-2", approval_ref="appr-001", approved=True, reviewer_id="alice"
        )
        asyncio.run(facade.submit_approval(r1))
        asyncio.run(facade.submit_approval(r2))
        assert _inner_gw(facade).signal_workflow.await_count == 2

    def test_different_refs_same_run_both_signal(self) -> None:
        """Verifies different refs same run both signal."""
        facade = _make_facade()
        r1 = ApprovalRequest(
            run_id="run-1", approval_ref="appr-001", approved=True, reviewer_id="alice"
        )
        r2 = ApprovalRequest(
            run_id="run-1", approval_ref="appr-002", approved=False, reviewer_id="bob"
        )
        asyncio.run(facade.submit_approval(r1))
        asyncio.run(facade.submit_approval(r2))
        assert _inner_gw(facade).signal_workflow.await_count == 2

    def test_dedup_is_per_facade_instance(self) -> None:
        """Two facade instances do not share the dedup set."""
        gateway = AsyncMock()
        gateway.start_workflow.return_value = {"workflow_id": "wf-1", "run_id": "run-1"}
        f1 = KernelFacade(gateway)
        f2 = KernelFacade(gateway)
        request = ApprovalRequest(
            run_id="run-1", approval_ref="appr-001", approved=True, reviewer_id="alice"
        )
        asyncio.run(f1.submit_approval(request))
        asyncio.run(f2.submit_approval(request))
        assert gateway.signal_workflow.await_count == 2


# ---------------------------------------------------------------------------
# get_health_readiness
# ---------------------------------------------------------------------------


class TestGetHealthReadiness:
    """Test suite for GetHealthReadiness."""

    def test_returns_ok_without_probe(self) -> None:
        """Verifies returns ok without probe."""
        facade = _make_facade()
        result = facade.get_health_readiness()
        assert result["status"] == "ok"

    def test_delegates_to_readiness_probe(self) -> None:
        """Verifies delegates to readiness probe."""
        probe = MagicMock()
        probe.readiness.return_value = {"status": "ok", "checks": {"db": "ok"}}
        facade = _make_facade(health_probe=probe)
        result = facade.get_health_readiness()
        probe.readiness.assert_called_once()
        assert result["checks"]["db"] == "ok"

    def test_substrate_in_default_response(self) -> None:
        """Verifies substrate in default response."""
        facade = _make_facade(substrate_type="local_fsm")
        result = facade.get_health_readiness()
        assert result["substrate"] == "local_fsm"

    def test_liveness_and_readiness_are_independent(self) -> None:
        """Verifies liveness and readiness are independent."""
        probe = MagicMock()
        probe.liveness.return_value = {"status": "ok", "source": "liveness"}
        probe.readiness.return_value = {"status": "degraded", "source": "readiness"}
        facade = _make_facade(health_probe=probe)
        assert facade.get_health()["source"] == "liveness"
        assert facade.get_health_readiness()["source"] == "readiness"
