"""Tests for KernelFacade capability discovery and new typed interfaces."""

from __future__ import annotations

import asyncio
import dataclasses
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_kernel.adapters.facade.kernel_facade import KernelFacade
from agent_kernel.kernel.contracts import (
    ApprovalRequest,
    DependencyGraph,
    DependencyNode,
    KernelManifest,
    SequentialPlan,
    SpeculativePlan,
)


def _make_action(action_id: str = "a1"):
    from agent_kernel.kernel.contracts import Action

    return Action(
        action_id=action_id,
        run_id="run-1",
        action_type="tool_call",
        effect_class="read_only",
    )


def _make_facade(**kwargs) -> KernelFacade:
    gateway = AsyncMock()
    gateway.start_workflow.return_value = {"workflow_id": "wf-1", "run_id": "run-1"}
    return KernelFacade(gateway, **kwargs)


# ---------------------------------------------------------------------------
# get_manifest
# ---------------------------------------------------------------------------


class TestGetManifest:
    def test_returns_kernel_manifest(self) -> None:
        facade = _make_facade()
        manifest = facade.get_manifest()
        assert isinstance(manifest, KernelManifest)

    def test_manifest_includes_all_five_plan_types(self) -> None:
        facade = _make_facade()
        manifest = facade.get_manifest()
        assert "sequential" in manifest.supported_plan_types
        assert "parallel" in manifest.supported_plan_types
        assert "conditional" in manifest.supported_plan_types
        assert "dependency_graph" in manifest.supported_plan_types
        assert "speculative" in manifest.supported_plan_types

    def test_manifest_includes_kernel_action_types(self) -> None:
        facade = _make_facade()
        manifest = facade.get_manifest()
        assert "tool_call" in manifest.supported_action_types
        assert "mcp_call" in manifest.supported_action_types
        assert "noop" in manifest.supported_action_types

    def test_manifest_includes_governance_features(self) -> None:
        facade = _make_facade()
        manifest = facade.get_manifest()
        assert "approval_gate" in manifest.supported_governance_features
        assert "speculation_mode" in manifest.supported_governance_features
        assert "at_most_once_dedupe" in manifest.supported_governance_features

    def test_manifest_substrate_type_default(self) -> None:
        facade = _make_facade()
        manifest = facade.get_manifest()
        assert manifest.substrate_type == "temporal"

    def test_manifest_substrate_type_custom(self) -> None:
        facade = _make_facade(substrate_type="local_fsm")
        manifest = facade.get_manifest()
        assert manifest.substrate_type == "local_fsm"

    def test_manifest_is_frozen(self) -> None:
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
        facade = _make_facade()
        manifest = facade.get_manifest()
        assert "tool_executor" in manifest.supported_interaction_targets
        assert "human_actor" in manifest.supported_interaction_targets
        assert "agent_peer" in manifest.supported_interaction_targets


# ---------------------------------------------------------------------------
# submit_plan
# ---------------------------------------------------------------------------


class TestSubmitPlan:
    def test_accepts_sequential_plan(self) -> None:
        facade = _make_facade()
        plan = SequentialPlan(steps=(_make_action(),))
        response = asyncio.run(facade.submit_plan("run-1", plan))
        assert response.accepted is True
        assert response.plan_type == "sequential"
        assert response.run_id == "run-1"

    def test_accepts_dependency_graph(self) -> None:
        facade = _make_facade()
        plan = DependencyGraph(nodes=(DependencyNode("n1", _make_action("n1")),))
        response = asyncio.run(facade.submit_plan("run-2", plan))
        assert response.accepted is True
        assert response.plan_type == "dependency_graph"

    def test_accepts_speculative_plan(self) -> None:
        from agent_kernel.kernel.contracts import SpeculativeCandidate

        facade = _make_facade()
        plan = SpeculativePlan(candidates=(SpeculativeCandidate("c1", SequentialPlan(steps=())),))
        response = asyncio.run(facade.submit_plan("run-3", plan))
        assert response.accepted is True
        assert response.plan_type == "speculative"

    def test_signals_workflow_on_acceptance(self) -> None:
        facade = _make_facade()
        plan = SequentialPlan(steps=())
        asyncio.run(facade.submit_plan("run-x", plan))
        facade._workflow_gateway.signal_workflow.assert_awaited_once()
        call_args = facade._workflow_gateway.signal_workflow.call_args
        signal_request = call_args[0][1]
        assert signal_request.signal_type == "plan_submitted"

    def test_rejects_unregistered_plan_type(self) -> None:
        """A custom object that is not a registered plan type is rejected."""

        class WeirdPlan:
            pass

        facade = _make_facade()
        response = asyncio.run(facade.submit_plan("run-y", WeirdPlan()))  # type: ignore[arg-type]
        assert response.accepted is False
        assert response.rejection_reason is not None
        facade._workflow_gateway.signal_workflow.assert_not_awaited()


# ---------------------------------------------------------------------------
# submit_approval
# ---------------------------------------------------------------------------


class TestSubmitApproval:
    def test_signals_approval_submitted(self) -> None:
        facade = _make_facade()
        request = ApprovalRequest(
            run_id="run-1",
            approval_ref="appr-001",
            approved=True,
            reviewer_id="user-alice",
        )
        asyncio.run(facade.submit_approval(request))
        facade._workflow_gateway.signal_workflow.assert_awaited_once()
        call_args = facade._workflow_gateway.signal_workflow.call_args
        signal_request = call_args[0][1]
        assert signal_request.signal_type == "approval_submitted"
        assert signal_request.signal_payload["approved"] is True
        assert signal_request.signal_payload["reviewer_id"] == "user-alice"
        assert signal_request.signal_payload["approval_ref"] == "appr-001"

    def test_signals_denial(self) -> None:
        facade = _make_facade()
        request = ApprovalRequest(
            run_id="run-2",
            approval_ref="appr-002",
            approved=False,
            reviewer_id="user-bob",
            reason="policy violation",
        )
        asyncio.run(facade.submit_approval(request))
        call_args = facade._workflow_gateway.signal_workflow.call_args
        signal_request = call_args[0][1]
        assert signal_request.signal_payload["approved"] is False
        assert signal_request.signal_payload["reason"] == "policy violation"


# ---------------------------------------------------------------------------
# commit_speculation
# ---------------------------------------------------------------------------


class TestCommitSpeculation:
    def test_signals_speculation_committed(self) -> None:
        facade = _make_facade()
        asyncio.run(facade.commit_speculation("run-1", "cand-42"))
        facade._workflow_gateway.signal_workflow.assert_awaited_once()
        call_args = facade._workflow_gateway.signal_workflow.call_args
        signal_request = call_args[0][1]
        assert signal_request.signal_type == "speculation_committed"
        assert signal_request.signal_payload["winner_candidate_id"] == "cand-42"
        assert signal_request.run_id == "run-1"


# ---------------------------------------------------------------------------
# get_health
# ---------------------------------------------------------------------------


class TestGetHealth:
    def test_returns_ok_without_probe(self) -> None:
        facade = _make_facade()
        health = facade.get_health()
        assert health["status"] == "ok"

    def test_delegates_to_health_probe_when_injected(self) -> None:
        probe = MagicMock()
        probe.liveness.return_value = {"status": "ok", "checks": {"db": "ok"}}
        facade = _make_facade(health_probe=probe)
        health = facade.get_health()
        probe.liveness.assert_called_once()
        assert health["checks"]["db"] == "ok"

    def test_substrate_type_in_default_response(self) -> None:
        facade = _make_facade(substrate_type="local_fsm")
        health = facade.get_health()
        assert health["substrate"] == "local_fsm"
