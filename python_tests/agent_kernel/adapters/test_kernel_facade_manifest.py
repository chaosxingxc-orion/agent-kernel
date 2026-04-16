"""Verifies for kernelfacade capability discovery and new typed interfaces."""

from __future__ import annotations

import asyncio
import dataclasses
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_kernel.adapters.facade.kernel_facade import KernelFacade
from agent_kernel.kernel.contracts import (
    ApprovalRequest,
    KernelManifest,
)


def _make_facade(**kwargs) -> KernelFacade:
    """Make facade."""
    gateway = AsyncMock()
    gateway.start_workflow.return_value = {"workflow_id": "wf-1", "run_id": "run-1"}
    return KernelFacade(gateway, **kwargs)


def _inner_gw(facade: KernelFacade) -> AsyncMock:
    """Unwrap WorkflowGatewaySignalAdapter to reach the underlying AsyncMock."""
    return getattr(facade._workflow_gateway, "_gateway", facade._workflow_gateway)


# ---------------------------------------------------------------------------
# get_manifest
# ---------------------------------------------------------------------------


class TestGetManifest:
    """Test suite for GetManifest."""

    def test_returns_kernel_manifest(self) -> None:
        """Verifies returns kernel manifest."""
        facade = _make_facade()
        manifest = facade.get_manifest()
        assert isinstance(manifest, KernelManifest)

    def test_manifest_includes_kernel_action_types(self) -> None:
        """Verifies manifest includes kernel action types."""
        facade = _make_facade()
        manifest = facade.get_manifest()
        assert "tool_call" in manifest.supported_action_types
        assert "mcp_call" in manifest.supported_action_types
        assert "noop" in manifest.supported_action_types

    def test_manifest_includes_governance_features(self) -> None:
        """Verifies manifest includes governance features."""
        facade = _make_facade()
        manifest = facade.get_manifest()
        assert "approval_gate" in manifest.supported_governance_features
        assert "speculation_mode" in manifest.supported_governance_features
        assert "at_most_once_dedupe" in manifest.supported_governance_features

    def test_manifest_substrate_type_default(self) -> None:
        """Verifies manifest substrate type default."""
        facade = _make_facade()
        manifest = facade.get_manifest()
        assert manifest.substrate_type == "temporal"

    def test_manifest_substrate_type_custom(self) -> None:
        """Verifies manifest substrate type custom."""
        facade = _make_facade(substrate_type="local_fsm")
        manifest = facade.get_manifest()
        assert manifest.substrate_type == "local_fsm"

    def test_manifest_is_frozen(self) -> None:
        """Verifies manifest is frozen."""
        facade = _make_facade()
        manifest = facade.get_manifest()
        with pytest.raises(dataclasses.FrozenInstanceError):
            manifest.kernel_version = "0.0.0"  # type: ignore[misc]

    def test_manifest_is_synchronous(self) -> None:
        """get_manifest must not be a coroutine — platforms call it synchronously."""
        import inspect

        facade = _make_facade()
        result = facade.get_manifest()
        assert not inspect.iscoroutine(result)

    def test_manifest_includes_interaction_targets(self) -> None:
        """Verifies manifest includes interaction targets."""
        facade = _make_facade()
        manifest = facade.get_manifest()
        assert "tool_executor" in manifest.supported_interaction_targets
        assert "human_actor" in manifest.supported_interaction_targets
        assert "agent_peer" in manifest.supported_interaction_targets


# ---------------------------------------------------------------------------
# submit_approval
# ---------------------------------------------------------------------------


class TestSubmitApproval:
    """Test suite for SubmitApproval."""

    def test_signals_approval_submitted(self) -> None:
        """Verifies signals approval submitted."""
        facade = _make_facade()
        request = ApprovalRequest(
            run_id="run-1",
            approval_ref="appr-001",
            approved=True,
            reviewer_id="user-alice",
        )
        asyncio.run(facade.submit_approval(request))
        _inner_gw(facade).signal_workflow.assert_awaited_once()
        call_args = _inner_gw(facade).signal_workflow.call_args
        signal_request = call_args[0][1]
        assert signal_request.signal_type == "approval_submitted"
        assert signal_request.signal_payload["approved"] is True
        assert signal_request.signal_payload["reviewer_id"] == "user-alice"
        assert signal_request.signal_payload["approval_ref"] == "appr-001"

    def test_signals_denial(self) -> None:
        """Verifies signals denial."""
        facade = _make_facade()
        request = ApprovalRequest(
            run_id="run-2",
            approval_ref="appr-002",
            approved=False,
            reviewer_id="user-bob",
            reason="policy violation",
        )
        asyncio.run(facade.submit_approval(request))
        call_args = _inner_gw(facade).signal_workflow.call_args
        signal_request = call_args[0][1]
        assert signal_request.signal_payload["approved"] is False
        assert signal_request.signal_payload["reason"] == "policy violation"

    def test_submit_approval_rejected_while_draining(self) -> None:
        """Verifies submit approval rejected while draining."""
        facade = _make_facade()
        facade.set_draining(True)
        request = ApprovalRequest(
            run_id="run-2",
            approval_ref="appr-003",
            approved=True,
            reviewer_id="user-drain",
        )
        with pytest.raises(RuntimeError, match="draining"):
            asyncio.run(facade.submit_approval(request))

    def test_submit_approval_tracks_inflight_with_drain_coordinator(self) -> None:
        """Verifies submit approval tracks inflight with drain coordinator."""

        class _DrainStub:
            """Test suite for  DrainStub."""

            def __init__(self) -> None:
                """Initializes _DrainStub."""
                self.enter_calls = 0
                self.exit_calls = 0

            async def enter(self) -> None:
                """Enters the test context manager."""
                self.enter_calls += 1

            async def exit(self) -> None:
                """Exits the test context manager."""
                self.exit_calls += 1

        drain = _DrainStub()
        facade = _make_facade(drain_coordinator=drain)
        request = ApprovalRequest(
            run_id="run-2",
            approval_ref="appr-004",
            approved=True,
            reviewer_id="user-drain",
        )
        asyncio.run(facade.submit_approval(request))
        assert drain.enter_calls == 1
        assert drain.exit_calls == 1


# ---------------------------------------------------------------------------
# get_health
# ---------------------------------------------------------------------------


class TestGetHealth:
    """Test suite for GetHealth."""

    def test_returns_ok_without_probe(self) -> None:
        """Verifies returns ok without probe."""
        facade = _make_facade()
        health = facade.get_health()
        assert health["status"] == "ok"

    def test_delegates_to_health_probe_when_injected(self) -> None:
        """Verifies delegates to health probe when injected."""
        probe = MagicMock()
        probe.liveness.return_value = {"status": "ok", "checks": {"db": "ok"}}
        facade = _make_facade(health_probe=probe)
        health = facade.get_health()
        probe.liveness.assert_called_once()
        assert health["checks"]["db"] == "ok"

    def test_substrate_type_in_default_response(self) -> None:
        """Verifies substrate type in default response."""
        facade = _make_facade(substrate_type="local_fsm")
        health = facade.get_health()
        assert health["substrate"] == "local_fsm"
