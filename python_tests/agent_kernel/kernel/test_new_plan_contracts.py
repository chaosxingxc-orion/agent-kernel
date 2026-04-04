"""Tests for new ExecutionPlan contract types: ConditionalPlan, DependencyGraph,
SpeculativePlan, and KernelManifest."""

from __future__ import annotations

import dataclasses

import pytest

from agent_kernel.kernel.contracts import (
    Action,
    ConditionalBranch,
    ConditionalPlan,
    DependencyGraph,
    DependencyNode,
    KernelManifest,
    ParallelGroup,
    ParallelPlan,
    PlanSubmissionResponse,
    SequentialPlan,
    SpeculativeCandidate,
    SpeculativePlan,
)
from agent_kernel.kernel.plan_type_registry import (
    KERNEL_PLAN_TYPE_REGISTRY,
    PlanTypeDescriptor,
    PlanTypeRegistry,
)


def _make_action(action_id: str = "a1") -> Action:
    return Action(
        action_id=action_id,
        run_id="run-1",
        action_type="tool_call",
        effect_class="read_only",
    )


# ---------------------------------------------------------------------------
# ConditionalPlan
# ---------------------------------------------------------------------------


class TestConditionalBranch:
    def test_frozen(self) -> None:
        branch = ConditionalBranch(
            trigger_outcomes=("dispatched",),
            plan=SequentialPlan(steps=()),
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            branch.trigger_outcomes = ("blocked",)  # type: ignore[misc]

    def test_multiple_trigger_outcomes(self) -> None:
        branch = ConditionalBranch(
            trigger_outcomes=("blocked", "recovery_pending"),
            plan=SequentialPlan(steps=()),
        )
        assert "blocked" in branch.trigger_outcomes
        assert "recovery_pending" in branch.trigger_outcomes


class TestConditionalPlan:
    def test_basic_construction(self) -> None:
        gating = _make_action("gate")
        branch_a = ConditionalBranch(
            trigger_outcomes=("dispatched",),
            plan=SequentialPlan(steps=(_make_action("next"),)),
        )
        branch_b = ConditionalBranch(
            trigger_outcomes=("blocked",),
            plan=SequentialPlan(steps=()),
        )
        plan = ConditionalPlan(
            gating_action=gating,
            branches=(branch_a, branch_b),
        )
        assert plan.gating_action.action_id == "gate"
        assert len(plan.branches) == 2
        assert plan.default_plan is None

    def test_default_plan_set(self) -> None:
        fallback = SequentialPlan(steps=(_make_action("fallback"),))
        plan = ConditionalPlan(
            gating_action=_make_action(),
            branches=(),
            default_plan=fallback,
        )
        assert plan.default_plan is fallback

    def test_frozen(self) -> None:
        plan = ConditionalPlan(
            gating_action=_make_action(),
            branches=(),
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            plan.branches = ()  # type: ignore[misc]

    def test_nested_parallel_plan_as_branch(self) -> None:
        inner = ParallelPlan(
            groups=(
                ParallelGroup(
                    actions=(_make_action("p1"), _make_action("p2")),
                    join_strategy="all",
                ),
            )
        )
        branch = ConditionalBranch(trigger_outcomes=("dispatched",), plan=inner)
        plan = ConditionalPlan(
            gating_action=_make_action("gate"),
            branches=(branch,),
        )
        assert isinstance(plan.branches[0].plan, ParallelPlan)


# ---------------------------------------------------------------------------
# DependencyGraph
# ---------------------------------------------------------------------------


class TestDependencyNode:
    def test_no_deps_by_default(self) -> None:
        node = DependencyNode(node_id="n1", action=_make_action("n1"))
        assert node.depends_on == ()

    def test_explicit_deps(self) -> None:
        node = DependencyNode(
            node_id="n3",
            action=_make_action("n3"),
            depends_on=("n1", "n2"),
        )
        assert "n1" in node.depends_on
        assert "n2" in node.depends_on

    def test_frozen(self) -> None:
        node = DependencyNode(node_id="n1", action=_make_action())
        with pytest.raises(dataclasses.FrozenInstanceError):
            node.node_id = "other"  # type: ignore[misc]


class TestDependencyGraph:
    def test_empty_graph(self) -> None:
        g = DependencyGraph(nodes=())
        assert g.nodes == ()

    def test_linear_chain(self) -> None:
        nodes = (
            DependencyNode("a", _make_action("a"), depends_on=()),
            DependencyNode("b", _make_action("b"), depends_on=("a",)),
            DependencyNode("c", _make_action("c"), depends_on=("b",)),
        )
        g = DependencyGraph(nodes=nodes)
        assert len(g.nodes) == 3
        assert g.nodes[2].depends_on == ("b",)

    def test_fan_out_fan_in(self) -> None:
        nodes = (
            DependencyNode("root", _make_action("root"), depends_on=()),
            DependencyNode("left", _make_action("left"), depends_on=("root",)),
            DependencyNode("right", _make_action("right"), depends_on=("root",)),
            DependencyNode("merge", _make_action("merge"), depends_on=("left", "right")),
        )
        g = DependencyGraph(nodes=nodes)
        assert len(g.nodes) == 4

    def test_frozen(self) -> None:
        g = DependencyGraph(nodes=())
        with pytest.raises(dataclasses.FrozenInstanceError):
            g.nodes = ()  # type: ignore[misc]


# ---------------------------------------------------------------------------
# SpeculativePlan
# ---------------------------------------------------------------------------


class TestSpeculativeCandidate:
    def test_basic(self) -> None:
        c = SpeculativeCandidate(
            candidate_id="cand-1",
            plan=SequentialPlan(steps=(_make_action(),)),
        )
        assert c.candidate_id == "cand-1"

    def test_frozen(self) -> None:
        c = SpeculativeCandidate(candidate_id="x", plan=SequentialPlan(steps=()))
        with pytest.raises(dataclasses.FrozenInstanceError):
            c.candidate_id = "y"  # type: ignore[misc]


class TestSpeculativePlan:
    def test_multiple_candidates(self) -> None:
        c1 = SpeculativeCandidate("c1", SequentialPlan(steps=(_make_action("s1"),)))
        c2 = SpeculativeCandidate("c2", SequentialPlan(steps=(_make_action("s2"),)))
        plan = SpeculativePlan(candidates=(c1, c2))
        assert len(plan.candidates) == 2
        assert plan.speculation_timeout_ms is None

    def test_with_timeout(self) -> None:
        plan = SpeculativePlan(
            candidates=(SpeculativeCandidate("c1", SequentialPlan(steps=())),),
            speculation_timeout_ms=30_000,
        )
        assert plan.speculation_timeout_ms == 30_000

    def test_frozen(self) -> None:
        plan = SpeculativePlan(candidates=())
        with pytest.raises(dataclasses.FrozenInstanceError):
            plan.candidates = ()  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ExecutionPlan union includes new types
# ---------------------------------------------------------------------------


class TestExecutionPlanUnion:
    def test_sequential_is_execution_plan(self) -> None:
        from agent_kernel.kernel.contracts import ExecutionPlan

        plan: ExecutionPlan = SequentialPlan(steps=())
        assert isinstance(plan, SequentialPlan)

    def test_conditional_is_execution_plan(self) -> None:
        from agent_kernel.kernel.contracts import ExecutionPlan

        plan: ExecutionPlan = ConditionalPlan(gating_action=_make_action(), branches=())
        assert isinstance(plan, ConditionalPlan)

    def test_dependency_graph_is_execution_plan(self) -> None:
        from agent_kernel.kernel.contracts import ExecutionPlan

        plan: ExecutionPlan = DependencyGraph(nodes=())
        assert isinstance(plan, DependencyGraph)

    def test_speculative_is_execution_plan(self) -> None:
        from agent_kernel.kernel.contracts import ExecutionPlan

        plan: ExecutionPlan = SpeculativePlan(candidates=())
        assert isinstance(plan, SpeculativePlan)


# ---------------------------------------------------------------------------
# PlanTypeRegistry
# ---------------------------------------------------------------------------


class TestPlanTypeRegistry:
    def test_kernel_registry_has_all_five_types(self) -> None:
        known = KERNEL_PLAN_TYPE_REGISTRY.known_types()
        assert "sequential" in known
        assert "parallel" in known
        assert "conditional" in known
        assert "dependency_graph" in known
        assert "speculative" in known

    def test_speculative_is_marked_speculative(self) -> None:
        d = KERNEL_PLAN_TYPE_REGISTRY.get("speculative")
        assert d is not None
        assert d.is_speculative is True
        assert d.requires_child_workflow is True

    def test_sequential_is_not_speculative(self) -> None:
        d = KERNEL_PLAN_TYPE_REGISTRY.get("sequential")
        assert d is not None
        assert d.is_speculative is False
        assert d.requires_child_workflow is False

    def test_duplicate_registration_raises(self) -> None:
        registry = PlanTypeRegistry()
        registry.register(PlanTypeDescriptor("custom", "A custom plan"))
        with pytest.raises(ValueError, match="already registered"):
            registry.register(PlanTypeDescriptor("custom", "Duplicate"))

    def test_unknown_type_returns_none(self) -> None:
        assert KERNEL_PLAN_TYPE_REGISTRY.get("nonexistent") is None

    def test_all_returns_sorted_list(self) -> None:
        items = KERNEL_PLAN_TYPE_REGISTRY.all()
        types = [d.plan_type for d in items]
        assert types == sorted(types)


# ---------------------------------------------------------------------------
# KernelManifest
# ---------------------------------------------------------------------------


class TestKernelManifest:
    def test_construction_and_frozen(self) -> None:
        manifest = KernelManifest(
            kernel_version="0.2.0",
            protocol_version="1.0.0",
            supported_plan_types=frozenset({"sequential", "parallel"}),
            supported_action_types=frozenset({"tool_call"}),
            supported_interaction_targets=frozenset({"tool_executor"}),
            supported_recovery_modes=frozenset({"abort"}),
            supported_governance_features=frozenset({"dedupe"}),
            supported_event_types=frozenset({"run.started"}),
            substrate_type="temporal",
        )
        assert manifest.kernel_version == "0.2.0"
        assert manifest.capability_snapshot_schema_version == "2"
        with pytest.raises(dataclasses.FrozenInstanceError):
            manifest.kernel_version = "9.9.9"  # type: ignore[misc]

    def test_default_schema_version_is_two(self) -> None:
        manifest = KernelManifest(
            kernel_version="0.1",
            protocol_version="1.0",
            supported_plan_types=frozenset(),
            supported_action_types=frozenset(),
            supported_interaction_targets=frozenset(),
            supported_recovery_modes=frozenset(),
            supported_governance_features=frozenset(),
            supported_event_types=frozenset(),
            substrate_type="local_fsm",
        )
        assert manifest.capability_snapshot_schema_version == "2"


# ---------------------------------------------------------------------------
# PlanSubmissionResponse and ApprovalRequest
# ---------------------------------------------------------------------------


class TestPlanSubmissionResponse:
    def test_accepted(self) -> None:
        r = PlanSubmissionResponse(run_id="r1", plan_type="sequential", accepted=True)
        assert r.accepted is True
        assert r.rejection_reason is None

    def test_rejected_with_reason(self) -> None:
        r = PlanSubmissionResponse(
            run_id="r1",
            plan_type="unknown",
            accepted=False,
            rejection_reason="not registered",
        )
        assert r.accepted is False
        assert r.rejection_reason == "not registered"
