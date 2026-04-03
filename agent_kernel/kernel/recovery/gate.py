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

import contextlib
import logging
import math
import time
from typing import TYPE_CHECKING, Any, Literal

from agent_kernel.kernel.capability_snapshot import (
    CapabilitySnapshotBuilder,
    CapabilitySnapshotInput,
)
from agent_kernel.kernel.contracts import (
    CircuitBreakerPolicy,
    CircuitBreakerStore,
    ContextWindow,
    InferenceConfig,
    RecoveryDecision,
    RecoveryGateService,
    RecoveryInput,
    ReflectionPolicy,
    ScriptFailureEvidence,
)

if TYPE_CHECKING:
    from agent_kernel.kernel.recovery.compensation_registry import CompensationRegistry

from agent_kernel.kernel.recovery.mode_registry import KERNEL_RECOVERY_MODE_REGISTRY
from agent_kernel.kernel.recovery.planner import RecoveryPlanner

if TYPE_CHECKING:
    from agent_kernel.kernel.dedupe_store import DedupeStorePort
    from agent_kernel.kernel.reasoning_loop import ReasoningLoop
    from agent_kernel.kernel.recovery.reflection_builder import ReflectionContextBuilder

_gate_logger = logging.getLogger(__name__)


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
        default_inference_config: InferenceConfig | None = None,
        observability_hook: Any | None = None,
        circuit_breaker_policy: CircuitBreakerPolicy | None = None,
        circuit_breaker_store: CircuitBreakerStore | None = None,
        dedupe_store: DedupeStorePort | None = None,
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
            default_inference_config: Optional default ``InferenceConfig`` used
                in ``reflect_and_retry`` turns.  When ``None``, defaults to
                ``InferenceConfig(model_ref="gpt-4o-mini")``.
            observability_hook: Optional hook for emitting recovery metrics.
                Must implement ``on_recovery_triggered``.
            circuit_breaker_policy: Optional policy governing cross-run circuit
                breaking keyed by ``effect_class``.  When ``None``, circuit
                breaking is disabled.
            circuit_breaker_store: Optional persistent store for circuit breaker
                state.  When provided, failure counts survive process restarts
                and are shared across instances (e.g. multiple workers).
                When ``None``, falls back to in-memory state (backward-compatible
                default).
        """
        self._planner = planner or RecoveryPlanner()
        self._compensation_registry = compensation_registry
        self._reflection_policy = reflection_policy
        self._reasoning_loop = reasoning_loop
        self._reflection_builder = reflection_builder
        self._default_inference_config = default_inference_config
        self._observability_hook = observability_hook
        self._circuit_breaker_policy = circuit_breaker_policy
        self._circuit_breaker_store = circuit_breaker_store
        self._dedupe_store = dedupe_store
        # Monotonic failure counter keyed by run_id.  Incremented on every
        # decide() call so callers can implement exponential backoff.
        self._failure_counts: dict[str, int] = {}
        # In-memory circuit breaker fallback (used when _circuit_breaker_store is None).
        self._circuit_failures: dict[str, int] = {}
        self._circuit_last_failure_mono: dict[str, float] = {}

    @property
    def compensation_registry(self) -> CompensationRegistry | None:
        """The compensation registry, if any was provided at construction.
        Returns:
            CompensationRegistry | None: The compensation registry, or ``None`` if not configured.
        """
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
        # Increment failure count for this run before building the decision so
        # the count is already up-to-date when embedded in the result.
        failure_count = self._failure_counts.get(recovery_input.run_id, 0) + 1
        self._failure_counts[recovery_input.run_id] = failure_count

        # Circuit-breaker check: if effect_class circuit is OPEN, abort immediately.
        # Do NOT call _record_circuit_failure here — that would reset the cooldown
        # timer on every rejected request and prevent half-open recovery under
        # sustained load (DEF-019B).
        effect_class = recovery_input.failed_effect_class
        if self._is_circuit_open(effect_class):
            reason = f"{recovery_input.reason_code}:circuit_open"
            self._emit_recovery(recovery_input.run_id, recovery_input.reason_code, "abort")
            return RecoveryDecision(
                run_id=recovery_input.run_id,
                mode="abort",
                reason=reason,
                failure_count=failure_count,
            )

        plan = self._planner.build_plan_from_input(recovery_input)
        mode = KERNEL_RECOVERY_MODE_REGISTRY.get(plan.action)
        if mode is None:
            raise ValueError(f"unsupported recovery plan action: {plan.action}")

        # Track effective decision fields separately so the plan DTO is never
        # mutated or reconstructed.
        effective_reason = plan.reason
        effective_compensation_id = plan.compensation_action_id
        effective_escalation_ref = plan.escalation_channel_ref

        # Validate compensation feasibility when a registry is present.
        if mode == "static_compensation" and self._compensation_registry is not None:
            comp_effect_class = _extract_effect_class(recovery_input)
            if comp_effect_class is not None and not self._compensation_registry.has_handler(
                comp_effect_class
            ):
                _gate_logger.warning(
                    "PlannedRecoveryGateService: no compensation handler for "
                    "effect_class=%s run_id=%s — downgrading to abort",
                    comp_effect_class,
                    recovery_input.run_id,
                )
                mode = "abort"  # type: ignore[assignment]
                effective_reason = f"{plan.reason}:no_compensation_handler"
                effective_compensation_id = None
                effective_escalation_ref = None
            elif comp_effect_class is not None:
                # Auto-execute compensation with dedupe_store injection when available.
                # This ensures static_compensation is always at-most-once without
                # requiring the caller to thread the dedupe_store through manually.
                failing_action = getattr(recovery_input, "failing_action", None)
                if failing_action is not None:
                    with contextlib.suppress(Exception):
                        await self._compensation_registry.execute(
                            failing_action,
                            dedupe_store=self._dedupe_store,
                            run_id=recovery_input.run_id,
                        )

        # Check if we should override with reflect_and_retry.
        if self._should_reflect(recovery_input, mode):
            decision = await self._decide_reflect_and_retry(recovery_input, effective_reason)
            decision = _attach_backoff(decision, failure_count)
            self._emit_recovery(recovery_input.run_id, recovery_input.reason_code, decision.mode)
            return decision

        retry_after_ms = _compute_retry_after_ms(mode, failure_count)
        decision = RecoveryDecision(
            run_id=plan.run_id,
            mode=mode,
            reason=effective_reason,
            compensation_action_id=effective_compensation_id,
            escalation_channel_ref=effective_escalation_ref,
            retry_after_ms=retry_after_ms,
            failure_count=failure_count,
        )
        self._record_circuit_failure(effect_class, run_id=recovery_input.run_id)
        self._emit_recovery(recovery_input.run_id, recovery_input.reason_code, decision.mode)
        return decision

    def on_action_success(self, effect_class: str) -> None:
        """Resets circuit breaker failure count for the given effect class.

        Call this after a successful dispatch so that the circuit transitions
        back to CLOSED state.  No-op when circuit breaking is disabled or when
        the effect class has no recorded failures.

        Args:
            effect_class: The action effect class that succeeded.
        """
        if self._circuit_breaker_store is not None:
            self._circuit_breaker_store.reset(effect_class)
        else:
            self._circuit_failures.pop(effect_class, None)
            self._circuit_last_failure_mono.pop(effect_class, None)

    def _is_circuit_open(self, effect_class: str | None) -> bool:
        """Returns ``True`` when the circuit is OPEN for this effect class.

        OPEN means ``failure_count >= threshold`` AND the half-open cooldown
        has not elapsed.  Returns ``False`` when circuit breaking is disabled,
        when ``effect_class`` is unknown, or when the cooldown has elapsed
        (half-open probe is allowed).

        Args:
            effect_class: The action effect class to check.

        Returns:
            ``True`` when the request should be rejected immediately.
        """
        if self._circuit_breaker_policy is None or effect_class is None:
            return False
        if self._circuit_breaker_store is not None:
            count, last_failure_ts = self._circuit_breaker_store.get_state(effect_class)
            if count < self._circuit_breaker_policy.threshold:
                return False
            elapsed_ms = (time.time() - last_failure_ts) * 1_000
        else:
            count = self._circuit_failures.get(effect_class, 0)
            if count < self._circuit_breaker_policy.threshold:
                return False
            last_mono = self._circuit_last_failure_mono.get(effect_class, 0.0)
            elapsed_ms = (time.monotonic() - last_mono) * 1_000
        return elapsed_ms < self._circuit_breaker_policy.half_open_after_ms

    def _record_circuit_failure(self, effect_class: str | None, run_id: str = "") -> None:
        """Increments the circuit failure counter for the given effect class.

        Args:
            effect_class: The action effect class that failed.
            run_id: Run identifier for observability hook emission.
        """
        if self._circuit_breaker_policy is None or effect_class is None:
            return
        if self._circuit_breaker_store is not None:
            new_count = self._circuit_breaker_store.record_failure(effect_class)
        else:
            new_count = self._circuit_failures.get(effect_class, 0) + 1
            self._circuit_failures[effect_class] = new_count
            self._circuit_last_failure_mono[effect_class] = time.monotonic()
        self._emit_circuit_breaker_trip(
            run_id=run_id,
            effect_class=effect_class,
            failure_count=new_count,
            tripped=new_count >= self._circuit_breaker_policy.threshold,
        )

    def _emit_recovery(self, run_id: str, reason_code: str, mode: str) -> None:
        """Emits on_recovery_triggered to the observability hook, if present."""
        if self._observability_hook is None:
            return
        with contextlib.suppress(Exception):
            self._observability_hook.on_recovery_triggered(
                run_id=run_id,
                reason_code=reason_code,
                mode=mode,
            )

    def _emit_reflection_round(
        self, run_id: str, action_id: str, round_num: int, corrected: bool
    ) -> None:
        """Emits on_reflection_round to the observability hook, if present."""
        if self._observability_hook is None:
            return
        with contextlib.suppress(Exception):
            self._observability_hook.on_reflection_round(
                run_id=run_id,
                action_id=action_id,
                round_num=round_num,
                corrected=corrected,
            )

    def _emit_circuit_breaker_trip(
        self, run_id: str, effect_class: str, failure_count: int, tripped: bool
    ) -> None:
        """Emits on_circuit_breaker_trip to the observability hook, if present."""
        if self._observability_hook is None:
            return
        with contextlib.suppress(Exception):
            self._observability_hook.on_circuit_breaker_trip(
                run_id=run_id,
                effect_class=effect_class,
                failure_count=failure_count,
                tripped=tripped,
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

        evidence = _build_evidence_from_input(recovery_input)
        base_context = ContextWindow(system_instructions="")
        enriched_context = self._reflection_builder.build(
            evidence=evidence,
            successful_branches=[],
            base_context=base_context,
            reflection_round=reflection_round,
        )

        inference_config = (
            self._default_inference_config
            if self._default_inference_config is not None
            else InferenceConfig(model_ref="gpt-4o-mini")
        )

        # Prefer the snapshot injected by the TurnEngine (keeping gate within its
        # authority boundary).  Fall back to building a minimal one only when
        # the caller did not provide one (e.g. tests that construct RecoveryInput
        # directly).
        if recovery_input.capability_snapshot is not None:
            snapshot = recovery_input.capability_snapshot
        else:
            snapshot = CapabilitySnapshotBuilder().build(
                CapabilitySnapshotInput(
                    run_id=recovery_input.run_id,
                    based_on_offset=recovery_input.projection.projected_offset,
                    tenant_policy_ref="policy:default",
                    permission_mode="strict",
                )
            )

        # Deterministic idempotency key: same run + offset + round → same LLM call.
        # Prevents duplicate inference charges when the gate retries reflection.
        _based_on_offset = recovery_input.projection.projected_offset
        _reflection_key = (
            f"{recovery_input.run_id}:{_based_on_offset}:reflection:{reflection_round}"
        )
        try:
            result = await self._reasoning_loop.run_once(
                run_id=recovery_input.run_id,
                snapshot=snapshot,
                history=[],
                inference_config=inference_config,
                prebuilt_context=enriched_context,
                idempotency_key=_reflection_key,
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
            _action_id_fb = getattr(
                getattr(recovery_input, "failing_action", None), "action_id", "unknown"
            )
            self._emit_reflection_round(
                run_id=recovery_input.run_id,
                action_id=_action_id_fb,
                round_num=reflection_round,
                corrected=False,
            )
            return self._fallback_decision(recovery_input, base_reason)

        corrected_action = result.actions[0]
        _action_id = getattr(
            getattr(recovery_input, "failing_action", None), "action_id", "unknown"
        )
        self._emit_reflection_round(
            run_id=recovery_input.run_id,
            action_id=_action_id,
            round_num=reflection_round,
            corrected=True,
        )
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


_RETRYABLE_MODES: frozenset[str] = frozenset({"static_compensation", "reflect_and_retry"})
_BACKOFF_BASE_MS: int = 500
_BACKOFF_MAX_MS: int = 30_000


def _compute_retry_after_ms(mode: str, failure_count: int) -> int | None:
    """Returns exponential backoff delay for retryable modes.

    Formula: ``base_ms * 2^(failure_count - 1)``, capped at ``max_ms``.
    Returns ``None`` for terminal modes (``abort``, ``human_escalation``).

    Args:
        mode: Recovery mode string.
        failure_count: Monotonic failure count for the current run.

    Returns:
        Milliseconds to wait before retry, or ``None`` for terminal modes.
    """
    if mode not in _RETRYABLE_MODES:
        return None
    exponent = min(failure_count - 1, 16)  # cap exponent to avoid overflow
    delay = int(_BACKOFF_BASE_MS * math.pow(2, exponent))
    return min(delay, _BACKOFF_MAX_MS)


def _attach_backoff(decision: RecoveryDecision, failure_count: int) -> RecoveryDecision:
    """Returns a copy of *decision* with ``retry_after_ms`` and ``failure_count`` set.

    Frozen dataclasses cannot be mutated; we use ``object.__setattr__`` via a
    ``replace``-style reconstruction instead.

    Args:
        decision: Original decision from ``_decide_reflect_and_retry``.
        failure_count: Current monotonic failure count.

    Returns:
        New ``RecoveryDecision`` with backoff fields populated.
    """
    retry_after_ms = _compute_retry_after_ms(decision.mode, failure_count)
    return RecoveryDecision(
        run_id=decision.run_id,
        mode=decision.mode,
        reason=decision.reason,
        compensation_action_id=decision.compensation_action_id,
        escalation_channel_ref=decision.escalation_channel_ref,
        corrected_action=decision.corrected_action,
        retry_after_ms=retry_after_ms,
        failure_count=failure_count,
    )
