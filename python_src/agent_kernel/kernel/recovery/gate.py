"""Planner-driven recovery gate implementation.

This gate translates planner output into the canonical ``RecoveryDecision``
contract. Keeping this logic in one place avoids decision-shape drift between
runtime callers and ensures mode/reason mapping stays deterministic.
"""

from __future__ import annotations

from typing import Literal

from agent_kernel.kernel.contracts import (
    RecoveryDecision,
    RecoveryGateService,
    RecoveryInput,
)
from agent_kernel.kernel.recovery.planner import (
    RecoveryPlanAction,
    RecoveryPlanner,
)

_PLAN_ACTION_TO_MODE: dict[
    RecoveryPlanAction,
    Literal[
        "static_compensation",
        "human_escalation",
        "abort",
    ],
] = {
    "schedule_compensation": "static_compensation",
    "notify_human_operator": "human_escalation",
    "abort_run": "abort",
}


class PlannedRecoveryGateService(RecoveryGateService):
    """Selects recovery decisions from planner-generated recovery plans.

    Design intent:
    - The planner picks the deterministic "what to do next" action.
    - The gate performs strict contract translation to ``RecoveryDecision``.
    - No additional heuristics are introduced here, so planner
      behavior remains the single source of truth for mode
      selection.
    """

    def __init__(
        self,
        planner: RecoveryPlanner | None = None,
    ) -> None:
        """Initializes the gate with an optional planner.

        Args:
            planner: Optional recovery planner instance. Uses
                default if not provided.
        """
        self._planner = planner or RecoveryPlanner()

    async def decide(
        self,
        recovery_input: RecoveryInput,
    ) -> RecoveryDecision:
        """Builds one recovery decision from planner output.

        Args:
            recovery_input: Failure envelope for this decision round.

        Returns:
            RecoveryDecision translated from planner action/motivation.

        Raises:
            ValueError: If planner returns an unsupported action.
        """
        plan = self._planner.build_plan_from_input(recovery_input)
        mode = _PLAN_ACTION_TO_MODE.get(plan.action)
        if mode is None:
            raise ValueError(f"unsupported recovery plan action: {plan.action}")
        return RecoveryDecision(
            run_id=plan.run_id,
            mode=mode,
            reason=plan.reason,
            compensation_action_id=plan.compensation_action_id,
            escalation_channel_ref=plan.escalation_channel_ref,
        )
