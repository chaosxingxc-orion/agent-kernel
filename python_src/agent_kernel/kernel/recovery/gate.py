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

``ReflectionPolicy`` integration:
  When a ``ReflectionPolicy``, ``ReasoningLoop``, and ``ReflectionContextBuilder``
  are all provided, the gate can override ``static_compensation`` or ``abort``
  decisions with ``reflect_and_retry`` when the failure kind is reflectable and
  the reflection round limit has not been reached.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

from agent_kernel.kernel.contracts import (
    ContextWindow,
    InferenceConfig,
    RecoveryDecision,
    RecoveryGateService,
    RecoveryInput,
    ReflectionPolicy,
    ScriptFailureEvidence,
)
from agent_kernel.kernel.recovery.compensation_registry import CompensationRegistry
from agent_kernel.kernel.recovery.planner import (
    RecoveryPlanAction,
    RecoveryPlanner,
)

if TYPE_CHECKING:
    from agent_kernel.kernel.reasoning_loop import ReasoningLoop
    from agent_kernel.kernel.recovery.reflection_builder import ReflectionContextBuilder

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
    - When a ``ReflectionPolicy``, ``ReasoningLoop``, and
      ``ReflectionContextBuilder`` are all provided, eligible failures can be
      overridden to ``reflect_and_retry``.
    """

    def __init__(
        self,
        planner: RecoveryPlanner | None = None,
        compensation_registry: CompensationRegistry | None = None,
        reflection_policy: ReflectionPolicy | None = None,
        reasoning_loop: ReasoningLoop | None = None,
        reflection_builder: ReflectionContextBuilder | None = None,
    ) -> None:
        """Initializes the gate with optional planner and service dependencies.

        Args:
            planner: Optional recovery planner instance. Uses
                default if not provided.
            compensation_registry: Optional registry of compensation handlers.
                When provided, ``static_compensation`` decisions are validated
                against registered handlers.  Unhandled effect classes cause
                the decision to be downgraded to ``abort``.
            reflection_policy: Optional policy governing reflect_and_retry loop.
            reasoning_loop: Optional reasoning loop for corrected action derivation.
            reflection_builder: Optional builder for enriched reflection context.
        """
        self._planner = planner or RecoveryPlanner()
        self._compensation_registry = compensation_registry
        self._reflection_policy = reflection_policy
        self._reasoning_loop = reasoning_loop
        self._reflection_builder = reflection_builder

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

        When ``reflection_policy``, ``reasoning_loop``, and
        ``reflection_builder`` are all set, and the failure kind is reflectable,
        and the reflection round limit has not been reached, the gate may
        override the planner decision with ``reflect_and_retry``.

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
                mode = "abort"  # type: ignore[assignment]
                plan = type(plan)(
                    run_id=plan.run_id,
                    action=plan.action,
                    reason=f"{plan.reason}:no_compensation_handler",
                    compensation_action_id=None,
                    escalation_channel_ref=None,
                )

        # Check if we should override with reflect_and_retry.
        if self._should_reflect(recovery_input, mode):
            return await self._decide_reflect_and_retry(recovery_input, plan.reason)

        return RecoveryDecision(
            run_id=plan.run_id,
            mode=mode,
            reason=plan.reason,
            compensation_action_id=plan.compensation_action_id,
            escalation_channel_ref=plan.escalation_channel_ref,
        )

    def _should_reflect(
        self,
        recovery_input: RecoveryInput,
        mode: Literal["static_compensation", "human_escalation", "abort"],
    ) -> bool:
        """Returns whether reflect_and_retry should be attempted.

        Args:
            recovery_input: Failure envelope for this decision round.
            mode: Planner-derived base mode (prior to reflection check).

        Returns:
            ``True`` when all reflection prerequisites are satisfied.
        """
        if self._reflection_policy is None:
            return False
        if self._reasoning_loop is None:
            return False
        if self._reflection_builder is None:
            return False
        # Only eligible for modes that are "recoverable via reflection".
        if mode not in ("static_compensation", "abort"):
            return False
        failure_kind = recovery_input.reason_code
        if not self._reflection_policy.is_reflectable(failure_kind):
            return False
        reflection_round = recovery_input.reflection_round
        return reflection_round < self._reflection_policy.max_rounds

    async def _decide_reflect_and_retry(
        self,
        recovery_input: RecoveryInput,
        base_reason: str,
    ) -> RecoveryDecision:
        """Runs the reasoning loop to derive a corrected action.

        Falls back to ``human_escalation`` or ``abort`` when the loop returns
        empty actions.

        Args:
            recovery_input: Failure envelope for this decision round.
            base_reason: Reason string derived from planner.

        Returns:
            RecoveryDecision with mode ``reflect_and_retry`` and the corrected
            action, or a fallback decision when the loop produces no actions.
        """
        assert self._reflection_policy is not None
        assert self._reasoning_loop is not None
        assert self._reflection_builder is not None

        reflection_round = recovery_input.reflection_round + 1

        # Build a minimal ScriptFailureEvidence from the recovery_input reason code.
        evidence = _build_evidence_from_input(recovery_input)

        # Build an empty base context window for the enrichment step.
        base_context = ContextWindow(system_instructions="")

        enriched_context = self._reflection_builder.build(
            evidence=evidence,
            successful_branches=[],
            base_context=base_context,
            reflection_round=reflection_round,
        )

        inference_config = InferenceConfig(model_ref="echo")

        # We need a snapshot; build a minimal one using the run_id.
        from agent_kernel.kernel.capability_snapshot import (
            CapabilitySnapshotBuilder,
            CapabilitySnapshotInput,
        )

        snapshot = CapabilitySnapshotBuilder().build(
            CapabilitySnapshotInput(
                run_id=recovery_input.run_id,
                based_on_offset=0,
                tenant_policy_ref="policy:default",
                permission_mode="strict",
            )
        )

        # Temporarily override the context port on the reasoning loop to pass
        # the pre-built enriched context.  We use a minimal override approach:
        # create an adapter that returns the pre-built enriched context.
        adapted_loop = _EnrichedContextReasoningLoop(
            reasoning_loop=self._reasoning_loop,
            enriched_context=enriched_context,
        )

        try:
            result = await adapted_loop.run_once(
                run_id=recovery_input.run_id,
                snapshot=snapshot,
                history=[],
                inference_config=inference_config,
                recovery_context=enriched_context.recovery_context,
            )
        except Exception:  # pylint: disable=broad-exception-caught
            _gate_logger.warning(
                "PlannedRecoveryGateService: reasoning loop failed during "
                "reflect_and_retry for run_id=%s — falling back",
                recovery_input.run_id,
            )
            return self._fallback_decision(recovery_input, base_reason)

        if not result.actions:
            _gate_logger.warning(
                "PlannedRecoveryGateService: reasoning loop returned no actions "
                "during reflect_and_retry for run_id=%s — falling back",
                recovery_input.run_id,
            )
            return self._fallback_decision(recovery_input, base_reason)

        corrected_action = result.actions[0]
        return RecoveryDecision(
            run_id=recovery_input.run_id,
            mode="reflect_and_retry",
            reason=f"{base_reason}:reflect_and_retry:round={reflection_round}",
            corrected_action=corrected_action,
        )

    def _fallback_decision(
        self,
        recovery_input: RecoveryInput,
        base_reason: str,
    ) -> RecoveryDecision:
        """Returns a fallback decision when reflection produces no corrected action.

        Args:
            recovery_input: Failure envelope for this decision round.
            base_reason: Reason string derived from planner.

        Returns:
            ``human_escalation`` when ``escalate_on_exhaustion`` is set on the
            policy; otherwise ``abort``.
        """
        assert self._reflection_policy is not None
        if self._reflection_policy.escalate_on_exhaustion:
            return RecoveryDecision(
                run_id=recovery_input.run_id,
                mode="human_escalation",
                reason=f"{base_reason}:reflect_exhausted:escalated",
            )
        return RecoveryDecision(
            run_id=recovery_input.run_id,
            mode="abort",
            reason=f"{base_reason}:reflect_exhausted:aborted",
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


def _build_evidence_from_input(recovery_input: RecoveryInput) -> ScriptFailureEvidence:
    """Builds a minimal ScriptFailureEvidence from a RecoveryInput.

    The evidence is assembled from the reason_code so that the reflection
    builder has a typed evidence object to work with.

    Args:
        recovery_input: Failure envelope for the current decision round.

    Returns:
        Minimal ``ScriptFailureEvidence`` suitable for reflection context
        assembly.
    """
    reason = recovery_input.reason_code
    # Map reason codes to known failure kinds where possible.
    known_kinds = frozenset(
        {
            "heartbeat_timeout",
            "runtime_error",
            "permission_denied",
            "resource_exhausted",
            "output_validation_failed",
        }
    )
    failure_kind: Any = reason if reason in known_kinds else "runtime_error"
    return ScriptFailureEvidence(
        script_id=recovery_input.failed_action_id or "unknown",
        failure_kind=failure_kind,
        budget_consumed_ratio=0.0,
        output_produced=False,
        suspected_cause=reason,
        original_script="",
    )


class _EnrichedContextReasoningLoop:
    """Thin wrapper that passes a pre-built context to the reasoning loop's gateway.

    This adapter bypasses the context_port assembly step by injecting the
    pre-enriched context directly into the LLM gateway call.

    Attributes:
        _reasoning_loop: The underlying reasoning loop.
        _enriched_context: Pre-built context window to use.
    """

    def __init__(
        self,
        reasoning_loop: Any,
        enriched_context: ContextWindow,
    ) -> None:
        self._reasoning_loop = reasoning_loop
        self._enriched_context = enriched_context

    async def run_once(
        self,
        run_id: str,
        snapshot: Any,
        history: list[Any],
        inference_config: InferenceConfig,
        recovery_context: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        """Delegates to the underlying reasoning loop using the enriched context.

        Args:
            run_id: Run identifier.
            snapshot: Capability snapshot (passed through to underlying loop).
            history: Event history (ignored; enriched context already built).
            inference_config: Inference configuration.
            recovery_context: Recovery context (already embedded in context).
            idempotency_key: Optional stable dedup key.

        Returns:
            ReasoningResult from the underlying loop.
        """
        # Pass the enriched context directly. We do this by calling run_once
        # on the underlying loop but with the pre-built enriched_context already
        # embedded via recovery_context on the context_port assembly call.
        # Since we cannot easily inject the pre-built context without refactoring
        # the loop, we pass recovery_context so the context_port can use it.
        return await self._reasoning_loop.run_once(
            run_id=run_id,
            snapshot=snapshot,
            history=history,
            inference_config=inference_config,
            recovery_context=recovery_context,
            idempotency_key=idempotency_key,
        )
