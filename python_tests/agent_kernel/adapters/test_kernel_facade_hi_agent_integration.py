"""Tests for hi-agent integration proposal modifications.

Covers:
  - PolicyVersionSet alignment (acceptance_policy_version, memory_policy_version)
  - RunPostmortemView and query_run_postmortem
  - ChildRunSummary and query_child_runs
  - SpawnChildRunRequest extended fields
  - KernelManifest trace_protocol_version bump to 2.8
  - supported_trace_features includes evolve_postmortem and child_run_orchestration
  - child_run_completed signal mapping
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_kernel.adapters.facade.kernel_facade import KernelFacade
from agent_kernel.kernel.contracts import (
    ChildRunSummary,
    HumanGateResolution,
    RunPolicyVersions,
    RunPostmortemView,
    RunProjection,
    SpawnChildRunRequest,
    TraceStageView,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_gateway(
    lifecycle_state: str = "running",
    waiting_external: bool = False,
    active_child_runs: list[str] | None = None,
    policy_versions: RunPolicyVersions | None = None,
) -> MagicMock:
    """Make gateway."""
    gw = MagicMock()
    gw.query_projection = AsyncMock(
        return_value=RunProjection(
            run_id="run-1",
            lifecycle_state=lifecycle_state,  # type: ignore[arg-type]
            projected_offset=5,
            waiting_external=waiting_external,
            ready_for_dispatch=True,
            active_child_runs=active_child_runs or [],
            policy_versions=policy_versions,
        ),
    )
    gw.signal_run = AsyncMock()
    gw.signal_workflow = AsyncMock()
    gw.start_child_workflow = AsyncMock(
        return_value={"run_id": "child-1", "workflow_id": "child-1"},
    )
    return gw


def _make_facade(**kwargs) -> KernelFacade:
    """Make facade."""
    gw = kwargs.pop("gateway", _make_gateway())
    return KernelFacade(workflow_gateway=gw, **kwargs)


# ---------------------------------------------------------------------------
# PolicyVersionSet alignment
# ---------------------------------------------------------------------------


class TestPolicyVersionSetAlignment:
    """Verify RunPolicyVersions has acceptance and memory fields."""

    def test_acceptance_policy_version_field_exists(self) -> None:
        """Verifies acceptance policy version field exists."""
        pvs = RunPolicyVersions(acceptance_policy_version="acceptance_v1")
        assert pvs.acceptance_policy_version == "acceptance_v1"

    def test_memory_policy_version_field_exists(self) -> None:
        """Verifies memory policy version field exists."""
        pvs = RunPolicyVersions(memory_policy_version="memory_v1")
        assert pvs.memory_policy_version == "memory_v1"

    def test_all_six_policy_fields(self) -> None:
        """Verifies all six policy fields."""
        pvs = RunPolicyVersions(
            route_policy_version="route_v1",
            acceptance_policy_version="acceptance_v1",
            memory_policy_version="memory_v1",
            skill_policy_version="skill_v1",
            evaluation_policy_version="evaluation_v1",
            task_view_policy_version="task_view_v1",
            pinned_at="2026-04-07T00:00:00Z",
        )
        assert pvs.route_policy_version == "route_v1"
        assert pvs.acceptance_policy_version == "acceptance_v1"
        assert pvs.memory_policy_version == "memory_v1"
        assert pvs.skill_policy_version == "skill_v1"
        assert pvs.evaluation_policy_version == "evaluation_v1"
        assert pvs.task_view_policy_version == "task_view_v1"

    def test_new_fields_default_to_none(self) -> None:
        """Verifies new fields default to none."""
        pvs = RunPolicyVersions()
        assert pvs.acceptance_policy_version is None
        assert pvs.memory_policy_version is None


# ---------------------------------------------------------------------------
# SpawnChildRunRequest extended fields
# ---------------------------------------------------------------------------


class TestSpawnChildRunRequestExtensions:
    """Verify SpawnChildRunRequest has new hi-agent integration fields."""

    def test_task_id_field(self) -> None:
        """Verifies task id field."""
        req = SpawnChildRunRequest(
            parent_run_id="p-1",
            child_kind="plan_step",
            task_id="task-42",
        )
        assert req.task_id == "task-42"

    def test_inherit_policy_versions_default_true(self) -> None:
        """Verifies inherit policy versions default true."""
        req = SpawnChildRunRequest(parent_run_id="p-1", child_kind="branch")
        assert req.inherit_policy_versions is True

    def test_policy_version_overrides(self) -> None:
        """Verifies policy version overrides."""
        req = SpawnChildRunRequest(
            parent_run_id="p-1",
            child_kind="branch",
            policy_version_overrides={"route_policy_version": "route_v2"},
        )
        assert req.policy_version_overrides == {"route_policy_version": "route_v2"}

    def test_notify_parent_on_complete_default_true(self) -> None:
        """Verifies notify parent on complete default true."""
        req = SpawnChildRunRequest(parent_run_id="p-1", child_kind="branch")
        assert req.notify_parent_on_complete is True


# ---------------------------------------------------------------------------
# RunPostmortemView DTO
# ---------------------------------------------------------------------------


class TestRunPostmortemViewDTO:
    """Verify RunPostmortemView dataclass structure."""

    def test_create_postmortem_view(self) -> None:
        """Verifies create postmortem view."""
        pm = RunPostmortemView(
            run_id="run-1",
            task_id="task-1",
            run_kind="default",
            outcome="completed",
            stages=[],
            branches=[],
            total_action_count=10,
            failure_codes=["missing_evidence"],
            duration_ms=5000,
            human_gate_resolutions=[],
            policy_versions=None,
            event_count=42,
            created_at="2026-04-07T00:00:00Z",
            completed_at="2026-04-07T00:05:00Z",
        )
        assert pm.run_id == "run-1"
        assert pm.total_action_count == 10
        assert pm.failure_codes == ["missing_evidence"]
        assert pm.outcome == "completed"

    def test_postmortem_is_frozen(self) -> None:
        """Verifies postmortem is frozen."""
        pm = RunPostmortemView(
            run_id="run-1",
            task_id=None,
            run_kind="default",
            outcome="completed",
            stages=[],
            branches=[],
            total_action_count=0,
            failure_codes=[],
            duration_ms=0,
            human_gate_resolutions=[],
            policy_versions=None,
            event_count=0,
            created_at="2026-04-07T00:00:00Z",
            completed_at=None,
        )
        with pytest.raises(AttributeError):
            pm.run_id = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ChildRunSummary DTO
# ---------------------------------------------------------------------------


class TestChildRunSummaryDTO:
    """Verify ChildRunSummary dataclass structure."""

    def test_create_child_summary(self) -> None:
        """Verifies create child summary."""
        cs = ChildRunSummary(
            child_run_id="child-1",
            child_kind="plan_step",
            task_id="task-1",
            lifecycle_state="completed",
            outcome="completed",
            created_at="2026-04-07T00:00:00Z",
            completed_at="2026-04-07T00:01:00Z",
        )
        assert cs.child_run_id == "child-1"
        assert cs.outcome == "completed"


# ---------------------------------------------------------------------------
# HumanGateResolution DTO
# ---------------------------------------------------------------------------


class TestHumanGateResolutionDTO:
    """Verify HumanGateResolution dataclass structure."""

    def test_create_resolution(self) -> None:
        """Verifies create resolution."""
        r = HumanGateResolution(
            gate_ref="gate-1",
            gate_type="final_approval",
            resolution="approved",
            resolved_by="user@example.com",
        )
        assert r.gate_ref == "gate-1"
        assert r.resolution == "approved"


# ---------------------------------------------------------------------------
# KernelManifest trace features
# ---------------------------------------------------------------------------


class TestKernelManifestTraceFeatures:
    """Verify manifest declares evolve_postmortem and child_run_orchestration."""

    def test_manifest_trace_version_is_2_8(self) -> None:
        """Verifies manifest trace version is 2 8."""
        facade = _make_facade()
        manifest = facade.get_manifest()
        assert manifest.trace_protocol_version == "2.8"

    def test_manifest_has_evolve_postmortem(self) -> None:
        """Verifies manifest has evolve postmortem."""
        facade = _make_facade()
        manifest = facade.get_manifest()
        assert "evolve_postmortem" in manifest.supported_trace_features

    def test_manifest_has_child_run_orchestration(self) -> None:
        """Verifies manifest has child run orchestration."""
        facade = _make_facade()
        manifest = facade.get_manifest()
        assert "child_run_orchestration" in manifest.supported_trace_features

    def test_manifest_has_12_trace_features(self) -> None:
        """Verifies manifest has 12 trace features."""
        facade = _make_facade()
        manifest = facade.get_manifest()
        assert len(manifest.supported_trace_features) == 12


# ---------------------------------------------------------------------------
# query_run_postmortem facade method
# ---------------------------------------------------------------------------


class TestQueryRunPostmortem:
    """Verify query_run_postmortem returns RunPostmortemView."""

    def test_returns_postmortem_view(self) -> None:
        """Verifies returns postmortem view."""
        facade = _make_facade()
        result = asyncio.run(
            facade.query_run_postmortem("run-1"),
        )
        assert isinstance(result, RunPostmortemView)
        assert result.run_id == "run-1"

    def test_aggregates_failure_codes_from_stages(self) -> None:
        """Verifies aggregates failure codes from stages."""
        facade = _make_facade()
        # Pre-populate stages with failures.
        facade._stage_registry["run-1"] = {
            "s1": TraceStageView(
                stage_id="s1",
                state="failed",
                entered_at="2026-04-07T00:00:00Z",
                failure_code="missing_evidence",
            ),
        }
        result = asyncio.run(
            facade.query_run_postmortem("run-1"),
        )
        assert "missing_evidence" in result.failure_codes

    def test_aggregates_human_gate_resolutions(self) -> None:
        """Verifies aggregates human gate resolutions."""
        facade = _make_facade()
        facade._resolved_human_gates["run-1"] = {"gate-1", "gate-2"}
        result = asyncio.run(
            facade.query_run_postmortem("run-1"),
        )
        assert len(result.human_gate_resolutions) == 2

    def test_includes_policy_versions(self) -> None:
        """Verifies includes policy versions."""
        pvs = RunPolicyVersions(
            route_policy_version="route_v1",
            acceptance_policy_version="acceptance_v1",
            memory_policy_version="memory_v1",
        )
        gw = _make_gateway(policy_versions=pvs)
        facade = _make_facade(gateway=gw)
        result = asyncio.run(
            facade.query_run_postmortem("run-1"),
        )
        assert result.policy_versions is not None
        assert result.policy_versions.acceptance_policy_version == "acceptance_v1"


# ---------------------------------------------------------------------------
# query_child_runs facade method
# ---------------------------------------------------------------------------


class TestQueryChildRuns:
    """Verify query_child_runs returns ChildRunSummary list."""

    def test_returns_empty_when_no_children(self) -> None:
        """Verifies returns empty when no children."""
        facade = _make_facade()
        result = asyncio.run(
            facade.query_child_runs("run-1"),
        )
        assert result == []

    def test_returns_child_summaries(self) -> None:
        """Verifies returns child summaries."""
        gw = _make_gateway(active_child_runs=["child-1", "child-2"])
        facade = _make_facade(gateway=gw)
        result = asyncio.run(
            facade.query_child_runs("run-1"),
        )
        assert len(result) == 2
        assert all(isinstance(s, ChildRunSummary) for s in result)
        child_ids = {s.child_run_id for s in result}
        assert child_ids == {"child-1", "child-2"}


# ---------------------------------------------------------------------------
# child_run_completed signal mapping
# ---------------------------------------------------------------------------


class TestChildRunCompletedSignal:
    """Verify child_run_completed is in the workflow signal event type map."""

    def test_signal_mapping_exists(self) -> None:
        """Verifies signal mapping exists."""
        from agent_kernel.substrate.temporal.run_actor_workflow import (
            _SIGNAL_EVENT_TYPE_MAP,
        )

        assert "child_run_completed" in _SIGNAL_EVENT_TYPE_MAP
        assert _SIGNAL_EVENT_TYPE_MAP["child_run_completed"] == "run.child_run_completed"
