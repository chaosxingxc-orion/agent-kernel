"""Tests for recovery planner deterministic plan mapping behavior."""

from __future__ import annotations

import asyncio
from typing import Literal

import pytest

from agent_kernel.kernel.contracts import (
    RecoveryDecision,
    RecoveryInput,
    RunLifecycleState,
    RunProjection,
)
from agent_kernel.kernel.recovery.gate import PlannedRecoveryGateService
from agent_kernel.kernel.recovery.planner import PlannerHeuristicPolicy, RecoveryPlanner


def _projection(run_id: str = "run-1") -> RunProjection:
    return RunProjection(
        run_id=run_id,
        lifecycle_state="recovering",
        projected_offset=10,
        waiting_external=False,
        ready_for_dispatch=False,
    )


def _recovery_input(
    *,
    reason_code: str,
    lifecycle_state: RunLifecycleState = "recovering",
    failed_action_id: str | None = None,
    recovery_mode: Literal["static_compensation", "human_escalation", "abort"] | None = None,
) -> RecoveryInput:
    projection = RunProjection(
        run_id="run-1",
        lifecycle_state=lifecycle_state,
        projected_offset=8,
        waiting_external=lifecycle_state == "waiting_external",
        ready_for_dispatch=False,
        current_action_id="action-current",
        recovery_mode=recovery_mode,
    )
    return RecoveryInput(
        run_id="run-1",
        reason_code=reason_code,
        lifecycle_state=lifecycle_state,
        projection=projection,
        failed_action_id=failed_action_id,
    )


def test_planner_maps_static_compensation_to_schedule_action() -> None:
    planner = RecoveryPlanner()
    plan = planner.build_plan(
        RecoveryDecision(
            run_id="run-1",
            mode="static_compensation",
            reason="retry with compensation",
            compensation_action_id="comp-1",
        ),
        _projection(),
    )
    assert plan.action == "schedule_compensation"
    assert plan.compensation_action_id == "comp-1"


def test_planner_maps_human_escalation_to_notification_action() -> None:
    planner = RecoveryPlanner()
    plan = planner.build_plan(
        RecoveryDecision(
            run_id="run-1",
            mode="human_escalation",
            reason="requires operator",
            escalation_channel_ref="pagerduty://team-a",
        ),
        _projection(),
    )
    assert plan.action == "notify_human_operator"
    assert plan.escalation_channel_ref == "pagerduty://team-a"


def test_planner_maps_abort_to_abort_action() -> None:
    planner = RecoveryPlanner()
    plan = planner.build_plan(
        RecoveryDecision(
            run_id="run-1",
            mode="abort",
            reason="fatal non-recoverable",
        ),
        _projection(),
    )
    assert plan.action == "abort_run"


def test_planner_rejects_mismatched_run_identity() -> None:
    planner = RecoveryPlanner()
    with pytest.raises(ValueError, match="run_id"):
        planner.build_plan(
            RecoveryDecision(
                run_id="run-x",
                mode="abort",
                reason="fatal",
            ),
            _projection(run_id="run-y"),
        )


@pytest.mark.parametrize(
    ("reason_code", "lifecycle_state", "failed_action_id", "expected_action"),
    [
        ("executor_transient_error", "recovering", "action-1", "schedule_compensation"),
        ("fatal_dependency_missing", "recovering", "action-1", "abort_run"),
        ("requires_human_review_timeout", "recovering", None, "notify_human_operator"),
        # waiting_external is authoritative and should win over reason classification.
        ("fatal_dependency_missing", "waiting_external", "action-1", "notify_human_operator"),
    ],
)
def test_planner_build_plan_from_input_classification_matrix(
    reason_code: str,
    lifecycle_state: RunLifecycleState,
    failed_action_id: str | None,
    expected_action: str,
) -> None:
    planner = RecoveryPlanner()
    recovery_input = _recovery_input(
        reason_code=reason_code,
        lifecycle_state=lifecycle_state,
        failed_action_id=failed_action_id,
    )

    plan = planner.build_plan_from_input(recovery_input)

    assert plan.action == expected_action
    if expected_action == "schedule_compensation":
        assert plan.compensation_action_id == "action-1"
    if expected_action == "notify_human_operator":
        assert plan.escalation_channel_ref == "human://operator"


@pytest.mark.parametrize(
    ("reason_code", "expected_action"),
    [
        ("ops_review_needed", "notify_human_operator"),
        ("hard_fail_dependency", "abort_run"),
        ("retry_me_network", "schedule_compensation"),
    ],
)
def test_planner_policy_prefix_configuration_controls_classification(
    reason_code: str,
    expected_action: str,
) -> None:
    planner = RecoveryPlanner(
        policy=PlannerHeuristicPolicy(
            human_reason_prefixes=("ops_",),
            fatal_reason_prefixes=("hard_fail_",),
            transient_reason_prefixes=("retry_me_",),
        )
    )
    recovery_input = _recovery_input(
        reason_code=reason_code,
        lifecycle_state="recovering",
        failed_action_id="action-42",
    )

    plan = planner.build_plan_from_input(recovery_input)
    assert plan.action == expected_action


@pytest.mark.parametrize(
    ("recovery_input", "expected_mode", "expected_reason"),
    [
        (
            _recovery_input(
                reason_code="executor_transient_error",
                lifecycle_state="recovering",
                failed_action_id="action-1",
            ),
            "static_compensation",
            "recovery:executor_transient_error",
        ),
        (
            _recovery_input(
                reason_code="requires_human_review_timeout",
                lifecycle_state="recovering",
            ),
            "human_escalation",
            "recovery:requires_human_review_timeout",
        ),
        (
            _recovery_input(
                reason_code="fatal_dependency_missing",
                lifecycle_state="recovering",
            ),
            "abort",
            "recovery:fatal_dependency_missing",
        ),
        (
            _recovery_input(
                reason_code="fatal_dependency_missing",
                lifecycle_state="waiting_external",
            ),
            "human_escalation",
            "recovery:fatal_dependency_missing",
        ),
    ],
)
def test_planner_driven_gate_maps_planner_output_to_recovery_decision(
    recovery_input: RecoveryInput,
    expected_mode: str,
    expected_reason: str,
) -> None:
    """Gate should translate planner actions into stable decision mode/reason."""
    planner = RecoveryPlanner()
    gate = PlannedRecoveryGateService(planner=planner)

    plan = planner.build_plan_from_input(recovery_input)
    decision = asyncio.run(gate.decide(recovery_input))

    assert decision.mode == expected_mode
    assert decision.reason == expected_reason
    assert decision.reason == plan.reason
    if expected_mode == "static_compensation":
        assert decision.compensation_action_id == "action-1"
    if expected_mode == "human_escalation":
        assert decision.escalation_channel_ref == "human://operator"
