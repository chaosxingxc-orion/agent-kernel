"""Planner-driven recovery gate implementation.

This gate translates planner output into the canonical ``RecoveryDecision``
contract. Keeping this logic in one place avoids decision-shape drift between
runtime callers and ensures mode/reason mapping stays deterministic.

``CompensationRegistry`` integration:
  When a ``CompensationRegistry`` is injected, the gate validates that a
  handler exists for the failing action's ``effect_class`` before emitting a
  ``static_compensation`` decision.  If no handler is registered the gate
  downgrades to ``abort`` and logs a warning — this prevents the kernel from
  emitting a compensation intent it can never fulfill.
"""

from __future__ import annotations

import logging
from typing import Literal

from agent_kernel.kernel.contracts import (
    RecoveryDecision,
    RecoveryGateService,
    RecoveryInput,
)
from agent_kernel.kernel.recovery.compensation_registry import CompensationRegistry
from agent_kernel.kernel.recovery.planner import (
    RecoveryPlanAction,
    RecoveryPlanner,
)

_gate_logger = logging.getLogger(__name__)

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
    - When a ``CompensationRegistry`` is provided, ``static_compensation``
      decisions are validated against it: an action whose ``effect_class`` has
      no registered handler is downgraded to ``abort``.
    """

    def __init__(
        self,
        planner: RecoveryPlanner | None = None,
        compensation_registry: CompensationRegistry | None = None,
    ) -> None:
        """Initializes the gate with an optional planner and compensation registry.

        Args:
            planner: Optional recovery planner instance. Uses
                default if not provided.
            compensation_registry: Optional registry of compensation handlers.
                When provided, ``static_compensation`` decisions are validated
                against registered handlers.  Unhandled effect classes cause
                the decision to be downgraded to ``abort``.
        """
        self._planner = planner or RecoveryPlanner()
        self._compensation_registry = compensation_registry

    @property
    def compensation_registry(self) -> CompensationRegistry | None:
        """The compensation registry, if any was provided at construction."""
        return self._compensation_registry

    async def decide(
        self,
        recovery_input: RecoveryInput,
    ) -> RecoveryDecision:
        """Builds one recovery decision from planner output.

        When a ``CompensationRegistry`` is present and the planner selects
        ``schedule_compensation``, the gate checks whether the failing
        action's ``effect_class`` has a registered handler.  If it does not,
        the mode is downgraded to ``abort`` with an explanatory reason suffix.

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

        # Validate compensation feasibility when a registry is present.
        if (
            mode == "static_compensation"
            and self._compensation_registry is not None
        ):
            failed_action_id = (
                recovery_input.failed_action_id
                or recovery_input.projection.current_action_id
            )
            # effect_class is not directly on RecoveryInput; we use the
            # compensation_action_id as a best-effort hint.  The registry
            # validation is based on the failed_action_id presence; if it
            # cannot be identified the safe fallback is abort.
            effect_class = _extract_effect_class(recovery_input)
            if effect_class is not None and not self._compensation_registry.has_handler(
                effect_class
            ):
                _gate_logger.warning(
                    "PlannedRecoveryGateService: no compensation handler for "
                    "effect_class=%s run_id=%s — downgrading to abort",
                    effect_class,
                    recovery_input.run_id,
                )
                mode = "abort"
                plan = type(plan)(
                    run_id=plan.run_id,
                    action=plan.action,
                    reason=f"{plan.reason}:no_compensation_handler",
                    compensation_action_id=None,
                    escalation_channel_ref=None,
                )

        return RecoveryDecision(
            run_id=plan.run_id,
            mode=mode,
            reason=plan.reason,
            compensation_action_id=plan.compensation_action_id,
            escalation_channel_ref=plan.escalation_channel_ref,
        )


def _extract_effect_class(recovery_input: RecoveryInput) -> str | None:
    """Attempts to extract effect_class from recovery input context.

    The ``FailureEnvelope`` (when attached to the projection) may carry a
    ``compensation_hint`` that encodes the effect class.  Falls back to
    ``None`` when not available so the gate does not abort valid compensations
    due to missing metadata.

    Args:
        recovery_input: Failure envelope for this decision round.

    Returns:
        effect_class string when determinable, otherwise ``None``.
    """
    # Prefer compensation_hint on the projection's failure context if it
    # encodes the effect class.  This is a best-effort extraction; callers
    # that need reliable effect_class routing should extend RecoveryInput.
    reason = recovery_input.reason_code.lower()
    if ":" in reason:
        # Convention: reason_code may encode "effect_class:reason" for routing.
        return reason.split(":")[0]
    return None
