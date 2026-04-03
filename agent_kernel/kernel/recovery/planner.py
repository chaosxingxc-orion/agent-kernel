"""Recovery planner utilities for deterministic recovery routing.

The planner intentionally owns only deterministic mapping logic.
It does not perform side effects, does not query external systems,
and does not mutate projection state. This boundary makes the
planner safe to use in unit tests and in runtime gate decisions
where reproducibility matters.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from agent_kernel.kernel.contracts import (
        RecoveryDecision,
        RecoveryInput,
        RunProjection,
    )

RecoveryPlanAction = Literal[
    "schedule_compensation",
    "notify_human_operator",
    "abort_run",
]
ReasonClassification = Literal["transient", "fatal", "human", "unknown"]


@dataclass(frozen=True, slots=True)
class RecoveryPlan:
    """Describes one deterministic next action after recovery decision.

    Attributes:
        run_id: Run identifier for the recovery plan.
        action: Planned recovery action to execute.
        reason: Human-readable reason for the recovery decision.
        compensation_action_id: Optional action id for compensation scheduling.
        escalation_channel_ref: Optional escalation channel for human
            notification.
    """

    run_id: str
    action: RecoveryPlanAction
    reason: str
    compensation_action_id: str | None = None
    escalation_channel_ref: str | None = None


@dataclass(frozen=True, slots=True)
class PlannerHeuristicPolicy:
    """Defines planner-only heuristics for selecting recovery actions.

    Classification strategy:
    - ``human``: requires operator intervention and therefore escalates.
    - ``fatal``: indicates unrecoverable failure and therefore aborts.
    - ``transient``: indicates retryable failure and prefers compensation.
    - ``unknown``: falls back to configurable action (default: compensation).

    Compatibility note:
    Existing behavior is preserved by default. Unknown reasons
    still prefer compensation when an action id exists, and fall
    back to abort otherwise.
    """

    reason_prefix: str = "recovery"
    default_escalation_channel_ref: str = "human://operator"
    human_reason_prefixes: tuple[str, ...] = (
        "human_",
        "manual_",
        "requires_human",
        "waiting_external",
        "external_",
    )
    transient_reason_prefixes: tuple[str, ...] = (
        "transient_",
        "executor_transient",
        "retryable_",
    )
    fatal_reason_prefixes: tuple[str, ...] = (
        "fatal",
        "non_recoverable",
        "permission_denied",
        "policy_denied",
    )
    # Backward-compatible alias: callers that used ``abort_reason_prefixes``
    # continue to work and are merged into fatal classification.
    abort_reason_prefixes: tuple[str, ...] = ()
    transient_action: RecoveryPlanAction = "schedule_compensation"
    fatal_action: RecoveryPlanAction = "abort_run"
    human_action: RecoveryPlanAction = "notify_human_operator"
    unknown_action: RecoveryPlanAction = "schedule_compensation"

    def classify_reason(self, reason_code: str) -> ReasonClassification:
        """Classifies reason_code into one heuristic category.

        Args:
            reason_code: Raw reason code from recovery input.

        Returns:
            One classification used by planner action strategy.
        """
        normalized_reason = reason_code.strip().lower()
        if _has_prefix(normalized_reason, self.human_reason_prefixes):
            return "human"
        fatal_prefixes = self.fatal_reason_prefixes + self.abort_reason_prefixes
        if _has_prefix(normalized_reason, fatal_prefixes):
            return "fatal"
        if _has_prefix(normalized_reason, self.transient_reason_prefixes):
            return "transient"
        return "unknown"

    def action_for_classification(
        self,
        classification: ReasonClassification,
    ) -> RecoveryPlanAction:
        """Maps one classification to its configured planner action.

        Args:
            classification: Heuristic reason classification.

        Returns:
            Configured recovery plan action for the classification.
        """
        if classification == "human":
            return self.human_action
        if classification == "fatal":
            return self.fatal_action
        if classification == "transient":
            return self.transient_action
        return self.unknown_action


class RecoveryPlanner:
    """Builds deterministic recovery plans from decisions or failure envelopes.

    The planner supports two usage modes:
    1) Legacy mapping from an already-authoritative ``RecoveryDecision``.
    2) Planner-driven mapping from ``RecoveryInput`` for the recovery gate.
    """

    def __init__(
        self,
        policy: PlannerHeuristicPolicy | None = None,
    ) -> None:
        """Initializes the planner with an optional policy.

        Args:
            policy: Optional heuristic policy for classification.
                Uses default if not provided.
        """
        self._policy = policy or PlannerHeuristicPolicy()

    def build_plan(
        self,
        decision: RecoveryDecision,
        projection: RunProjection,
    ) -> RecoveryPlan:
        """Builds one plan for the given decision and current projection.

        Args:
            decision: Authoritative recovery decision.
            projection: Current projection snapshot for contextual validation.

        Returns:
            Deterministic recovery plan for downstream execution routing.

        Raises:
            ValueError: If decision run_id mismatches projection run_id.
        """
        if decision.run_id != projection.run_id:
            raise ValueError("recovery decision run_id must match projection run_id.")

        if decision.mode == "static_compensation":
            return RecoveryPlan(
                run_id=decision.run_id,
                action="schedule_compensation",
                reason=decision.reason,
                compensation_action_id=decision.compensation_action_id,
            )
        if decision.mode == "human_escalation":
            return RecoveryPlan(
                run_id=decision.run_id,
                action="notify_human_operator",
                reason=decision.reason,
                escalation_channel_ref=decision.escalation_channel_ref,
            )
        return RecoveryPlan(
            run_id=decision.run_id,
            action="abort_run",
            reason=decision.reason,
        )

    def build_plan_from_input(self, recovery_input: RecoveryInput) -> RecoveryPlan:
        """Builds a deterministic plan from the failure envelope.

        Design boundary:
        The planner never infers additional domain facts. It only uses the
        provided input/projection snapshot and string heuristics, so callers can
        safely replay the same input and receive the same output.

        Args:
            recovery_input: Failure envelope with authoritative projection.

        Returns:
            Recovery plan that the gate can translate into a decision.

        Raises:
            ValueError: If recovery_input run_id mismatches projection run_id.
        """
        projection = recovery_input.projection
        if recovery_input.run_id != projection.run_id:
            raise ValueError("recovery input run_id must match projection run_id.")

        reason = f"{self._policy.reason_prefix}:{recovery_input.reason_code}"
        compensation_action_id = (
            recovery_input.failed_action_id or recovery_input.projection.current_action_id
        )

        action = "abort_run"
        escalation_channel_ref: str | None = None
        plan_compensation_action_id: str | None = None

        # Explicit projection mode always wins because it is the authoritative
        # runtime hint from prior state transitions.
        if projection.recovery_mode == "human_escalation":
            action = "notify_human_operator"
            escalation_channel_ref = self._policy.default_escalation_channel_ref
        elif projection.recovery_mode == "static_compensation":
            action = "schedule_compensation"
            plan_compensation_action_id = compensation_action_id
        elif projection.recovery_mode == "abort":
            action = "abort_run"
        else:
            normalized_reason = recovery_input.reason_code.strip().lower()
            if recovery_input.lifecycle_state == "waiting_external":
                action = "notify_human_operator"
                escalation_channel_ref = self._policy.default_escalation_channel_ref
            else:
                classification = self._policy.classify_reason(normalized_reason)
                action = self._policy.action_for_classification(classification)
                if action == "notify_human_operator":
                    escalation_channel_ref = self._policy.default_escalation_channel_ref
                elif action == "schedule_compensation":
                    if compensation_action_id is not None:
                        plan_compensation_action_id = compensation_action_id
                    else:
                        action = "abort_run"

        return RecoveryPlan(
            run_id=recovery_input.run_id,
            action=action,
            reason=reason,
            compensation_action_id=plan_compensation_action_id,
            escalation_channel_ref=escalation_channel_ref,
        )


def _has_prefix(value: str, prefixes: tuple[str, ...]) -> bool:
    """Returns whether ``value`` starts with any configured prefix.

    Args:
        value: Normalized string to check.
        prefixes: Tuple of candidate prefix strings.

    Returns:
        ``True`` when ``value`` starts with at least one prefix.
    """
    return any(value.startswith(prefix) for prefix in prefixes)
