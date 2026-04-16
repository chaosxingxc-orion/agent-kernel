"""v6.4 TurnEngine minimal canonical path implementation.

This implementation encodes the first authoritative execution path:
  RunActor -> TurnEngine -> Snapshot -> Admission -> Dedupe -> Executor.

The engine is intentionally narrow for PoC:
  - One turn performs at most one authoritative dispatch attempt.
  - Admission is evaluated at most once per turn.
  - Ambiguous side-effect evidence is surfaced as effect_unknown and translated
    into recovery_pending via FailureEnvelope.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol

_logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from agent_kernel.kernel.reasoning_loop import ReasoningLoop

from agent_kernel.kernel.action_type_registry import validate_action_type
from agent_kernel.kernel.capability_snapshot import (
    CapabilitySnapshot,
    CapabilitySnapshotBuildError,
    CapabilitySnapshotInput,
    assert_snapshot_compatible,
)
from agent_kernel.kernel.contracts import (
    Action,
    ExecutionContext,
    FailureEnvelope,
    InferenceConfig,
    RemoteServiceIdempotencyContract,
)
from agent_kernel.kernel.dedupe_store import (
    DedupeReservation,
    DedupeStorePort,
    HostKind,
    IdempotencyEnvelope,
)
from agent_kernel.kernel.failure_evidence import apply_failure_evidence_priority
from agent_kernel.kernel.idempotency_key_policy import IdempotencyKeyPolicy
from agent_kernel.kernel.remote_service_policy import (
    RemoteDispatchPolicyDecision,
    evaluate_remote_service_policy,
)

TurnTriggerType = Literal["start", "signal", "child_join", "recovery_resume"]
TurnState = Literal[
    "collecting",
    "intent_committed",
    "reasoning",
    "snapshot_built",
    "admission_checked",
    "dispatch_blocked",
    "dispatched",
    "dispatch_acknowledged",
    "effect_unknown",
    "effect_recorded",
    "recovery_pending",
    "completed_noop",
]
TurnOutcomeKind = Literal["noop", "blocked", "dispatched", "recovery_pending"]


@dataclass(frozen=True, slots=True)
class TurnStateEvent:
    """Represents one FSM state-transition event emitted by TurnEngine.

    Attributes:
        state: FSM state name or diagnostic event name reached.
        reason: Optional human-readable reason for the transition.
        metadata: Optional extra key-value pairs for diagnostic events
            (e.g. effect scope downgrade fields, deprecation markers).

    """

    state: str
    reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TurnInput:
    """Represents one turn execution trigger for a run.

    Attributes:
        run_id: Kernel run identifier.
        through_offset: Upper-bound offset for this turn.
        based_on_offset: Baseline offset the turn replays from.
        trigger_type: Discriminator for the turn trigger origin.
        history: Optional ordered event history for passing to reasoning loop.

    """

    run_id: str
    through_offset: int
    based_on_offset: int
    trigger_type: TurnTriggerType
    history: list[Any] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class TurnEngineDefaults:
    """Configurable defaults for TurnEngine phases.

    Platform layer must provide these values — the kernel does not
    assume any specific model, policy, or permission mode.
    """

    model_ref: str
    tenant_policy_ref: str
    permission_mode: str


@dataclass(frozen=True, slots=True)
class TurnResult:
    """Represents deterministic result produced by one turn execution.

    Attributes:
        state: Final turn state after execution.
        outcome_kind: Class-level outcome discriminator.
        decision_ref: Stable reference for the dispatch decision.
        decision_fingerprint: Deterministic fingerprint for the decision.
        dispatch_dedupe_key: Optional dedup key for the dispatch attempt.
        intent_commit_ref: Optional reference linking to the intent commit.
        host_kind: Optional resolved host kind for the dispatch target.
        remote_policy_decision: Optional remote policy evaluation result.
        action_commit: Optional commit metadata for a successful dispatch.
        recovery_input: Optional failure envelope when recovery is pending.
        emitted_events: Ordered list of state-transition event dicts.

    """

    state: TurnState
    outcome_kind: TurnOutcomeKind
    decision_ref: str
    decision_fingerprint: str
    dispatch_dedupe_key: str | None = None
    intent_commit_ref: str | None = None
    host_kind: HostKind | None = None
    remote_policy_decision: RemoteDispatchPolicyDecision | None = None
    action_commit: dict[str, Any] | None = None
    recovery_input: FailureEnvelope | None = None
    emitted_events: list[TurnStateEvent] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _TurnIdentity:
    """Carries deterministic identity fields for one turn execution.

    Attributes:
        decision_ref: Stable reference for the dispatch decision.
        decision_fingerprint: Deterministic fingerprint derived from
            run identity, trigger type, action, and offset.
        intent_commit_ref: Reference linking turn to its intent commit.
        dispatch_dedupe_key: Deduplication key for at-most-once dispatch.

    """

    decision_ref: str
    decision_fingerprint: str
    intent_commit_ref: str
    dispatch_dedupe_key: str


@dataclass(frozen=True, slots=True)
class _DispatchPolicy:
    """Carries resolved host metadata and remote-service policy outcome.

    Attributes:
        host_kind: Resolved dispatch host destination kind.
        should_block: Whether the dispatch must be blocked.
        block_reason: Human-readable reason when ``should_block`` is True.
        remote_policy_decision: Remote idempotency policy evaluation
            result when the action targets a remote service.

    """

    host_kind: HostKind
    should_block: bool
    block_reason: str | None = None
    remote_policy_decision: RemoteDispatchPolicyDecision | None = None


class SnapshotBuilderPort(Protocol):
    """Protocol for capability snapshot construction."""

    def build(self, input_value: CapabilitySnapshotInput) -> CapabilitySnapshot:
        """Build one capability snapshot.

        Args:
            input_value: Normalized snapshot input payload.

        Returns:
            Immutable capability snapshot with deterministic hash.

        """


class AdmissionPort(Protocol):
    """Protocol for admission checks in turn execution."""

    async def admit(self, action: Action, snapshot: CapabilitySnapshot) -> Any:
        """Return whether action is admitted for dispatch under snapshot.

        Args:
            action: Candidate action to evaluate.
            snapshot: Capability snapshot for policy evaluation.

        Returns:
            Admission decision object consumed by turn execution.

        """

    async def check(self, action: Action, snapshot: CapabilitySnapshot) -> Any:
        """Return whether action is admitted for dispatch.

        Args:
            action: Candidate action to evaluate.
            snapshot: Capability snapshot for policy evaluation.

        Returns:
            Admission result or boolean indicating admission status.

        """


class ExecutorPort(Protocol):
    """Protocol for action execution in turn execution."""

    async def execute(
        self,
        action: Action,
        snapshot: CapabilitySnapshot,
        envelope: IdempotencyEnvelope,
        execution_context: ExecutionContext | None = None,
    ) -> dict[str, Any]:
        """Execute one action and returns execution evidence payload.

        Args:
            action: Admitted action to execute.
            snapshot: Capability snapshot governing execution.
            envelope: Idempotency envelope for dispatch deduplication.

            execution_context: Parameter from function signature.

        Returns:
            Execution evidence dictionary with acknowledgement status.

        """


class SnapshotInputResolverPort(Protocol):
    """Resolves snapshot input payload from turn input and action context."""

    def resolve(
        self,
        input_value: TurnInput,
        action: Action,
    ) -> CapabilitySnapshotInput:
        """Resolve one snapshot input object.

        Args:
            input_value: Turn input providing run identity and offsets.
            action: Action carrying declared snapshot payload.

        Returns:
            Normalized snapshot input for capability snapshot builder.

        """


@dataclass(slots=True)
class TurnPhaseContext:
    """Mutable carrier passed between TurnEngine phase handlers.

    Each phase handler reads context fields set by previous phases and
    writes its own outputs.  When a phase produces a terminal result it
    sets ``result``; subsequent phases are skipped.

    This dataclass is the extension point for the dispatch-table design:
    subclasses that add custom phases simply add fields here and override
    the relevant phase methods.

    Attributes:
        input_value: Turn trigger provided by the caller.
        action: Resolved action, or ``None`` if still deriving.
        emitted_events: Ordered list of state-transition events.
        result: When set, the turn has reached a terminal state.
        turn_identity: Deterministic identity fields (set in identity phase).
        snapshot: Built capability snapshot (set in snapshot phase).
        admission_result: Raw admission service result (set in admission phase).
        dispatch_policy: Resolved dispatch policy (set in policy phase).
        envelope: Idempotency envelope for deduplication (set in dedupe phase).
        execute_result: Raw executor response dict (set in execute phase).

    """

    input_value: TurnInput
    action: Action | None
    emitted_events: list[TurnStateEvent]
    result: TurnResult | None = None
    turn_identity: _TurnIdentity | None = None
    snapshot: CapabilitySnapshot | None = None
    admission_result: Any = None
    dispatch_policy: _DispatchPolicy | None = None
    envelope: IdempotencyEnvelope | None = None
    execute_result: dict[str, Any] | None = None
    # Internal flags set by _phase_dedupe and consumed by _phase_execute.
    _dedupe_available: bool = True
    _dedupe_outcome: str = "accepted"
    _legacy_alias_key: str | None = None


class TurnEngine:
    """Runs one canonical turn with strict v6.4 guardrails.

    Extension points
    ----------------
    ``_TURN_PHASES`` is a class-level ordered tuple of phase method names.
    Each phase is an ``async`` method that accepts a ``TurnPhaseContext`` and
    returns ``None``.  When a phase wants to terminate the turn early it sets
    ``ctx.result`` 鈥?the dispatcher stops and returns that result.

    Subclasses may override individual phase methods or extend
    ``_TURN_PHASES`` by re-declaring it in the subclass body::

        class CustomTurnEngine(TurnEngine):
            _TURN_PHASES = TurnEngine._TURN_PHASES + ("_phase_custom_audit",)

            async def _phase_custom_audit(self, ctx: TurnPhaseContext) -> None:
                ...  # inject custom logic before dispatching
    """

    #: Ordered sequence of phase method names executed by ``run_turn``.
    #: Each phase is an async method on this class accepting a TurnPhaseContext.
    _TURN_PHASES: tuple[str, ...] = (
        "_phase_noop_or_reasoning",
        "_phase_snapshot",
        "_phase_admission",
        "_phase_dispatch_policy",
        "_phase_dedupe",
        "_phase_execute",
    )

    @classmethod
    def register_phase(
        cls,
        name: str,
        *,
        after: str | None = None,
        before: str | None = None,
    ) -> None:
        """Insert a new phase method name into ``_TURN_PHASES`` for this class.

        Call this at module import time (not per-request) to inject custom
        auditing or side-effect hooks without subclassing.

        Args:
            name: Method name to insert.  The method must exist on ``cls``
                (either defined directly or inherited).
            after: Existing phase name after which *name* is inserted.
                When ``None`` and *before* is also ``None``, *name* is
                appended at the end.
            before: Existing phase name before which *name* is inserted.
                Mutually exclusive with *after*.

        Raises:
            ValueError: When *name* is already registered, or when the
                anchor phase (*after* / *before*) is not found.
            TypeError: When both *after* and *before* are provided.

        """
        if after is not None and before is not None:
            raise TypeError("Provide at most one of 'after' or 'before', not both.")
        if name in cls._TURN_PHASES:
            raise ValueError(
                f"Phase {name!r} is already registered in {cls.__name__}._TURN_PHASES."
            )
        phases = list(cls._TURN_PHASES)
        if after is not None:
            if after not in phases:
                raise ValueError(
                    f"Anchor phase {after!r} not found in {cls.__name__}._TURN_PHASES."
                )
            idx = phases.index(after)
            phases.insert(idx + 1, name)
        elif before is not None:
            if before not in phases:
                raise ValueError(
                    f"Anchor phase {before!r} not found in {cls.__name__}._TURN_PHASES."
                )
            idx = phases.index(before)
            phases.insert(idx, name)
        else:
            phases.append(name)
        cls._TURN_PHASES = tuple(phases)

    def __init__(
        self,
        snapshot_builder: SnapshotBuilderPort,
        admission_service: AdmissionPort,
        dedupe_store: DedupeStorePort,
        executor: ExecutorPort,
        snapshot_input_resolver: SnapshotInputResolverPort | None = None,
        require_declared_snapshot_inputs: bool = False,
        reasoning_loop: ReasoningLoop | None = None,
        observability_hook: Any | None = None,
        phase_timeout_ms: int | None = None,
        defaults: TurnEngineDefaults | None = None,
    ) -> None:
        """Initialize TurnEngine with required service dependencies.

        Args:
            snapshot_builder: Service that builds capability snapshots.
            admission_service: Service that evaluates action admission.
            dedupe_store: Store for idempotent dispatch deduplication.
            executor: Service that executes admitted actions.
            snapshot_input_resolver: Optional resolver for snapshot input
                payloads. When ``None``, the engine resolves from action
                payload directly.
            require_declared_snapshot_inputs: When ``True``, enforces
                v6.4 strict mode requiring declared snapshot inputs.
            reasoning_loop: Optional reasoning loop for deriving actions from
                an LLM when no explicit action is provided.
            observability_hook: Optional hook for emitting dispatch and
                recovery metrics.  Must implement ``on_action_dispatch``
                and ``on_recovery_triggered``.
            phase_timeout_ms: Optional per-phase wall-clock timeout in
                milliseconds.  When set, each phase is wrapped in
                ``asyncio.wait_for``; a ``TimeoutError`` propagates to the
                caller so RunActorWorkflow can treat it as a recoverable
                failure.  When ``None`` (default), no per-phase timeout is
                applied.
            defaults: Configurable defaults for business-policy values
                used in turn phases.  When ``None``, falls back to
                PoC/testing defaults (echo model, policy:default, strict).

        """
        self._snapshot_builder = snapshot_builder
        self._admission_service = admission_service
        self._dedupe_store = dedupe_store
        self._executor = executor
        self._snapshot_input_resolver = snapshot_input_resolver
        self._require_declared_snapshot_inputs = require_declared_snapshot_inputs
        self._reasoning_loop = reasoning_loop
        self._observability_hook = observability_hook
        self._phase_timeout_s: float | None = (
            phase_timeout_ms / 1000.0 if phase_timeout_ms is not None else None
        )
        self._defaults = defaults or TurnEngineDefaults(
            model_ref="echo",
            tenant_policy_ref="policy:default",
            permission_mode="strict",
        )

    async def run_turn(
        self,
        input_value: TurnInput,
        action: Action | None = None,
        admission_subject: Any | None = None,
    ) -> TurnResult:
        """Run one turn by dispatching through ``_TURN_PHASES`` in order.

        Each phase is an async method accepting a ``TurnPhaseContext``.  When a
        phase sets ``ctx.result`` the dispatcher stops and returns that result,
        allowing early exit at any phase boundary.

        Args:
            input_value: Turn trigger and offset context.
            action: Candidate action selected by upstream decision path.
                When ``None`` and a reasoning loop is configured, the engine
                runs the loop to derive an action. When ``None`` and no
                reasoning loop is configured, returns ``completed_noop``.
            admission_subject: Unused; kept for backward-compatible signature.

        Returns:
            Deterministic turn result with explicit outcome class.

        Raises:
            TypeError: If the admission result is an unawaited awaitable.

        """
        ctx = TurnPhaseContext(
            input_value=input_value,
            action=action,
            emitted_events=[
                TurnStateEvent(state="collecting"),
                TurnStateEvent(state="intent_committed"),
            ],
        )
        for phase_name in self._TURN_PHASES:
            _phase_start_ns = time.monotonic_ns()
            phase_fn = getattr(self, phase_name)
            if self._phase_timeout_s is not None:
                await asyncio.wait_for(phase_fn(ctx), timeout=self._phase_timeout_s)
            else:
                await phase_fn(ctx)
            if self._observability_hook is not None:
                _phase_elapsed_ms = (time.monotonic_ns() - _phase_start_ns) // 1_000_000
                with contextlib.suppress(Exception):
                    self._observability_hook.on_turn_phase(
                        run_id=ctx.input_value.run_id,
                        action_id=ctx.action.action_id if ctx.action else "",
                        phase_name=phase_name,
                        elapsed_ms=_phase_elapsed_ms,
                    )
            if ctx.result is not None:
                return ctx.result
        # Safety net 鈥?all paths through the phases must set ctx.result.
        raise RuntimeError(  # pragma: no cover
            "TurnEngine phases completed without setting ctx.result."
        )

    # ------------------------------------------------------------------
    # Phase handlers 鈥?each sets ctx.result to terminate, or leaves it
    # None to allow the next phase to run.
    # ------------------------------------------------------------------

    async def _phase_noop_or_reasoning(self, ctx: TurnPhaseContext) -> None:
        """Phase 1: resolve action via reasoning loop or return noop."""
        if ctx.action is not None:
            return
        if self._reasoning_loop is None:
            ctx.emitted_events.append(TurnStateEvent(state="completed_noop"))
            ctx.result = TurnResult(
                state="completed_noop",
                outcome_kind="noop",
                decision_ref=(
                    f"decision:{ctx.input_value.run_id}:{ctx.input_value.based_on_offset}"
                ),
                decision_fingerprint=(
                    f"{ctx.input_value.run_id}:{ctx.input_value.trigger_type}"
                    f":noop:{ctx.input_value.based_on_offset}"
                ),
                emitted_events=ctx.emitted_events,
            )
            return
        ctx.emitted_events.append(TurnStateEvent(state="reasoning"))
        inference_config = InferenceConfig(model_ref=self._defaults.model_ref)
        reasoning_idempotency_key = (
            f"{ctx.input_value.run_id}:{ctx.input_value.based_on_offset}:reasoning"
        )
        try:
            reasoning_result = await self._reasoning_loop.run_once(
                run_id=ctx.input_value.run_id,
                snapshot=self._snapshot_builder.build(
                    CapabilitySnapshotInput(
                        run_id=ctx.input_value.run_id,
                        based_on_offset=ctx.input_value.based_on_offset,
                        tenant_policy_ref=self._defaults.tenant_policy_ref,
                        permission_mode=self._defaults.permission_mode,
                    )
                ),
                history=list(ctx.input_value.history),
                inference_config=inference_config,
                idempotency_key=reasoning_idempotency_key,
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            _logger.warning("reasoning phase failed, degrading to noop: %s", exc, exc_info=True)
            ctx.emitted_events.append(TurnStateEvent(state="reasoning_failed"))
            ctx.emitted_events.append(TurnStateEvent(state="completed_noop"))
            ctx.result = TurnResult(
                state="completed_noop",
                outcome_kind="noop",
                decision_ref=(
                    f"decision:{ctx.input_value.run_id}:{ctx.input_value.based_on_offset}"
                ),
                decision_fingerprint=(
                    f"{ctx.input_value.run_id}:{ctx.input_value.trigger_type}"
                    f":noop:{ctx.input_value.based_on_offset}"
                ),
                emitted_events=ctx.emitted_events,
            )
            return
        if not reasoning_result.actions:
            ctx.emitted_events.append(TurnStateEvent(state="completed_noop"))
            ctx.result = TurnResult(
                state="completed_noop",
                outcome_kind="noop",
                decision_ref=(
                    f"decision:{ctx.input_value.run_id}:{ctx.input_value.based_on_offset}"
                ),
                decision_fingerprint=(
                    f"{ctx.input_value.run_id}:{ctx.input_value.trigger_type}"
                    f":noop:{ctx.input_value.based_on_offset}"
                ),
                emitted_events=ctx.emitted_events,
            )
            return
        ctx.action = reasoning_result.actions[0]

    async def _phase_snapshot(self, ctx: TurnPhaseContext) -> None:
        """Phase 2: build capability snapshot; noop on build errors."""
        assert ctx.action is not None
        ctx.turn_identity = _build_turn_identity(input_value=ctx.input_value, action=ctx.action)
        try:
            snapshot_input = _resolve_snapshot_input(
                input_value=ctx.input_value,
                action=ctx.action,
                resolver=self._snapshot_input_resolver,
                require_declared_snapshot_inputs=self._require_declared_snapshot_inputs,
            )
            ctx.snapshot = self._snapshot_builder.build(snapshot_input)
            assert_snapshot_compatible(ctx.snapshot)
            # Recompute identity with snapshot-aware idempotency key policy.
            ctx.turn_identity = _build_turn_identity(
                input_value=ctx.input_value,
                action=ctx.action,
                snapshot_hash=ctx.snapshot.snapshot_hash,
            )
            ctx.emitted_events.append(TurnStateEvent(state="snapshot_built"))
        except CapabilitySnapshotBuildError, ValueError:
            ctx.emitted_events.append(TurnStateEvent(state="completed_noop"))
            ti = ctx.turn_identity
            ctx.result = TurnResult(
                state="completed_noop",
                outcome_kind="noop",
                decision_ref=ti.decision_ref,
                decision_fingerprint=ti.decision_fingerprint,
                dispatch_dedupe_key=ti.dispatch_dedupe_key,
                intent_commit_ref=ti.intent_commit_ref,
                emitted_events=ctx.emitted_events,
            )

    async def _phase_admission(self, ctx: TurnPhaseContext) -> None:
        """Phase 3: evaluate admission; block if not admitted."""
        assert ctx.action is not None
        assert ctx.snapshot is not None
        assert ctx.turn_identity is not None
        ti = ctx.turn_identity
        _start_ns = time.monotonic_ns()
        ctx.admission_result = await _evaluate_admission(
            admission_service=self._admission_service,
            action=ctx.action,
            snapshot=ctx.snapshot,
        )
        _latency_ms = (time.monotonic_ns() - _start_ns) // 1_000_000
        admitted = _is_admitted(ctx.admission_result)
        ctx.emitted_events.append(TurnStateEvent(state="admission_checked"))
        if self._observability_hook is not None:
            with contextlib.suppress(Exception):
                self._observability_hook.on_admission_evaluated(
                    run_id=ctx.input_value.run_id,
                    action_id=ctx.action.action_id,
                    admitted=admitted,
                    latency_ms=_latency_ms,
                )
        if not admitted:
            ctx.emitted_events.append(TurnStateEvent(state="dispatch_blocked"))
            ctx.result = TurnResult(
                state="dispatch_blocked",
                outcome_kind="blocked",
                decision_ref=ti.decision_ref,
                decision_fingerprint=ti.decision_fingerprint,
                dispatch_dedupe_key=ti.dispatch_dedupe_key,
                intent_commit_ref=ti.intent_commit_ref,
                emitted_events=ctx.emitted_events,
            )

    async def _phase_dispatch_policy(self, ctx: TurnPhaseContext) -> None:
        """Phase 4: evaluate dispatch policy; block if policy rejects."""
        assert ctx.action is not None
        assert ctx.turn_identity is not None
        ti = ctx.turn_identity
        # Validate action_type against KERNEL_ACTION_TYPE_REGISTRY (warning only).
        # Unknown types are not blocked so custom registrations loaded after
        # startup still dispatch correctly.
        validate_action_type(ctx.action.action_type)
        ctx.dispatch_policy = _resolve_dispatch_policy(action=ctx.action)
        if ctx.dispatch_policy.should_block:
            ctx.emitted_events.append(
                TurnStateEvent(
                    state="dispatch_blocked",
                    reason=ctx.dispatch_policy.block_reason,
                )
            )
            ctx.result = TurnResult(
                state="dispatch_blocked",
                outcome_kind="blocked",
                decision_ref=ti.decision_ref,
                decision_fingerprint=ti.decision_fingerprint,
                dispatch_dedupe_key=ti.dispatch_dedupe_key,
                intent_commit_ref=ti.intent_commit_ref,
                host_kind=ctx.dispatch_policy.host_kind,
                remote_policy_decision=ctx.dispatch_policy.remote_policy_decision,
                emitted_events=ctx.emitted_events,
            )

    async def _phase_dedupe(self, ctx: TurnPhaseContext) -> None:
        """Phase 5: reserve idempotency slot; block if already taken."""
        assert ctx.action is not None
        assert ctx.snapshot is not None
        assert ctx.turn_identity is not None
        assert ctx.dispatch_policy is not None
        ti = ctx.turn_identity
        dp = ctx.dispatch_policy
        legacy_key = _legacy_dispatch_dedupe_key(
            run_id=ctx.input_value.run_id,
            action_id=ctx.action.action_id,
            based_on_offset=ctx.input_value.based_on_offset,
        )
        if legacy_key != ti.dispatch_dedupe_key:
            legacy_record = None
            with contextlib.suppress(Exception):
                legacy_record = self._dedupe_store.get(legacy_key)
            if legacy_record is not None:
                ctx.emitted_events.append(TurnStateEvent(state="dispatch_blocked"))
                ctx.result = TurnResult(
                    state="dispatch_blocked",
                    outcome_kind="blocked",
                    decision_ref=ti.decision_ref,
                    decision_fingerprint=ti.decision_fingerprint,
                    dispatch_dedupe_key=ti.dispatch_dedupe_key,
                    intent_commit_ref=ti.intent_commit_ref,
                    host_kind=dp.host_kind,
                    remote_policy_decision=dp.remote_policy_decision,
                    emitted_events=ctx.emitted_events,
                )
                if self._observability_hook is not None:
                    with contextlib.suppress(Exception):
                        self._observability_hook.on_dedupe_hit(
                            run_id=ctx.input_value.run_id,
                            action_id=legacy_key,
                            outcome="duplicate",
                        )
                return
        ctx.envelope = _resolve_idempotency_envelope(
            admission_result=ctx.admission_result,
            turn_identity=ti,
            action=ctx.action,
            snapshot=ctx.snapshot,
            host_kind=dp.host_kind,
        )
        reservation, ctx.envelope, dedupe_available = _reserve_with_degradation(
            dedupe_store=self._dedupe_store,
            envelope=ctx.envelope,
            action=ctx.action,
            host_kind=dp.host_kind,
            emitted_events=ctx.emitted_events,
        )
        ctx._dedupe_available = dedupe_available  # type: ignore[attr-defined]
        if not reservation.accepted:
            ctx.emitted_events.append(TurnStateEvent(state="dispatch_blocked"))
            ctx.result = TurnResult(
                state="dispatch_blocked",
                outcome_kind="blocked",
                decision_ref=ti.decision_ref,
                decision_fingerprint=ti.decision_fingerprint,
                dispatch_dedupe_key=ti.dispatch_dedupe_key,
                intent_commit_ref=ti.intent_commit_ref,
                host_kind=dp.host_kind,
                remote_policy_decision=dp.remote_policy_decision,
                emitted_events=ctx.emitted_events,
            )
            if self._observability_hook is not None:
                with contextlib.suppress(Exception):
                    self._observability_hook.on_dedupe_hit(
                        run_id=ctx.input_value.run_id,
                        action_id=ti.dispatch_dedupe_key,
                        outcome="duplicate",
                    )
            return
        dedupe_available = ctx._dedupe_available  # type: ignore[attr-defined]
        _dedupe_outcome = "degraded" if not dedupe_available else "accepted"
        ctx._dedupe_outcome = _dedupe_outcome  # type: ignore[attr-defined]
        if dedupe_available and legacy_key != ti.dispatch_dedupe_key:
            # Backward compatibility: mirror the new dedupe slot into the
            # historical key shape so pre-policy readers can still resolve the
            # same dispatch lifecycle.
            with contextlib.suppress(Exception):
                legacy_envelope = IdempotencyEnvelope(
                    dispatch_idempotency_key=legacy_key,
                    operation_fingerprint=ctx.envelope.operation_fingerprint,
                    attempt_seq=ctx.envelope.attempt_seq,
                    effect_scope=ctx.envelope.effect_scope,
                    capability_snapshot_hash=ctx.envelope.capability_snapshot_hash,
                    host_kind=ctx.envelope.host_kind,
                    peer_operation_id=ctx.envelope.peer_operation_id,
                    policy_snapshot_ref=ctx.envelope.policy_snapshot_ref,
                    rule_bundle_hash=ctx.envelope.rule_bundle_hash,
                )
                legacy_reservation = self._dedupe_store.reserve_and_dispatch(
                    legacy_envelope,
                    peer_operation_id=ctx.envelope.peer_operation_id,
                )
                if legacy_reservation.accepted:
                    ctx._legacy_alias_key = legacy_key  # type: ignore[attr-defined]
        # mark_dispatched() is no longer called here: _reserve_with_degradation
        # uses reserve_and_dispatch() which atomically combines reservation and
        # dispatch state update, eliminating the non-atomic window (D-M3).
        if self._observability_hook is not None:
            with contextlib.suppress(Exception):
                self._observability_hook.on_dedupe_hit(
                    run_id=ctx.input_value.run_id,
                    action_id=ti.dispatch_dedupe_key,
                    outcome=_dedupe_outcome,
                )
        ctx.emitted_events.append(TurnStateEvent(state="dispatched"))

    async def _phase_execute(self, ctx: TurnPhaseContext) -> None:
        """Phase 6: call executor and build terminal result."""
        assert ctx.action is not None
        assert ctx.snapshot is not None
        assert ctx.turn_identity is not None
        assert ctx.dispatch_policy is not None
        assert ctx.envelope is not None
        ti = ctx.turn_identity
        dp = ctx.dispatch_policy
        dedupe_available: bool = ctx._dedupe_available  # type: ignore[attr-defined]
        _dedupe_outcome: str = ctx._dedupe_outcome  # type: ignore[attr-defined]
        legacy_alias_key: str | None = ctx._legacy_alias_key  # type: ignore[attr-defined]

        execution_context = _build_execution_context(
            input_value=ctx.input_value,
            action=ctx.action,
            snapshot=ctx.snapshot,
            turn_identity=ti,
            admission_result=ctx.admission_result,
            envelope=ctx.envelope,
        )
        _dispatch_start_ns = time.monotonic_ns()
        try:
            ctx.execute_result = await _execute_with_context(
                executor=self._executor,
                action=ctx.action,
                snapshot=ctx.snapshot,
                envelope=ctx.envelope,
                execution_context=execution_context,
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            # Ack-before-commit: executor raised before acknowledging -- mark as
            # unknown_effect so the DedupeStore does not stay in "dispatched".
            _logger.warning(
                "executor raised before acknowledging, marking unknown_effect: %s",
                exc,
                exc_info=True,
            )
            if dedupe_available:
                with contextlib.suppress(Exception):
                    self._dedupe_store.mark_unknown_effect(ti.dispatch_dedupe_key)
                if legacy_alias_key is not None:
                    with contextlib.suppress(Exception):
                        self._dedupe_store.mark_unknown_effect(legacy_alias_key)
            raise
        _dispatch_latency_ms = (time.monotonic_ns() - _dispatch_start_ns) // 1_000_000
        if self._observability_hook is not None:
            with contextlib.suppress(Exception):
                self._observability_hook.on_dispatch_attempted(
                    run_id=ctx.input_value.run_id,
                    action_id=ctx.action.action_id,
                    dedupe_outcome=_dedupe_outcome,
                    latency_ms=_dispatch_latency_ms,
                )
        acknowledged = bool(ctx.execute_result.get("acknowledged", False))
        if acknowledged:
            if dedupe_available:
                self._dedupe_store.mark_acknowledged(ti.dispatch_dedupe_key)
                if legacy_alias_key is not None:
                    with contextlib.suppress(Exception):
                        self._dedupe_store.mark_acknowledged(legacy_alias_key)
            ctx.emitted_events.append(TurnStateEvent(state="dispatch_acknowledged"))
            if self._observability_hook is not None:
                with contextlib.suppress(Exception):
                    self._observability_hook.on_action_dispatch(
                        run_id=ctx.input_value.run_id,
                        action_id=ctx.action.action_id,
                        action_type=type(ctx.action).__name__,
                        outcome_kind="dispatched",
                        latency_ms=_dispatch_latency_ms,
                    )
            ctx.result = TurnResult(
                state="dispatch_acknowledged",
                outcome_kind="dispatched",
                decision_ref=ti.decision_ref,
                decision_fingerprint=ti.decision_fingerprint,
                dispatch_dedupe_key=ti.dispatch_dedupe_key,
                intent_commit_ref=ti.intent_commit_ref,
                host_kind=dp.host_kind,
                remote_policy_decision=dp.remote_policy_decision,
                action_commit={
                    "action_id": ctx.action.action_id,
                    "run_id": ctx.input_value.run_id,
                    "committed_at": _utc_now_iso(),
                    "execution_context": _execution_context_payload(execution_context),
                },
                emitted_events=ctx.emitted_events,
            )
            return

        if dedupe_available:
            self._dedupe_store.mark_unknown_effect(ti.dispatch_dedupe_key)
            if legacy_alias_key is not None:
                with contextlib.suppress(Exception):
                    self._dedupe_store.mark_unknown_effect(legacy_alias_key)
        ctx.emitted_events.extend(
            [TurnStateEvent(state="effect_unknown"), TurnStateEvent(state="recovery_pending")]
        )
        if self._observability_hook is not None:
            with contextlib.suppress(Exception):
                self._observability_hook.on_action_dispatch(
                    run_id=ctx.input_value.run_id,
                    action_id=ctx.action.action_id,
                    action_type=type(ctx.action).__name__,
                    outcome_kind="effect_unknown",
                    latency_ms=_dispatch_latency_ms,
                )
        ctx.result = _build_recovery_pending_turn_result(
            input_value=ctx.input_value,
            action=ctx.action,
            turn_identity=ti,
            execute_result=ctx.execute_result,
            host_kind=dp.host_kind,
            remote_policy_decision=dp.remote_policy_decision,
            emitted_events=ctx.emitted_events,
        )


def _utc_now_iso() -> str:
    """Return an RFC3339 UTC timestamp.

    Returns:
        UTC timestamp string in ``YYYY-MM-DDTHH:MM:SSZ`` format.

    """
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def _is_admitted(admission_result: Any) -> bool:
    """Resolve admitted bool from varied admission return shapes.

    Compatibility:
      - ``bool`` return is accepted for test doubles.
      - ``AdmissionResult``-like objects with ``admitted`` attribute are accepted.
      - awaitable values are rejected here because caller must await before use.

    Args:
        admission_result: Admission return value, either bool or
            ``AdmissionResult``-like object.

    Returns:
        Boolean admission status.

    Raises:
        TypeError: If ``admission_result`` is an awaitable that was not
            awaited before the check.

    """
    if inspect.isawaitable(admission_result):
        raise TypeError("Admission result must be awaited before admission check.")
    if isinstance(admission_result, bool):
        return admission_result
    admitted = getattr(admission_result, "admitted", None)
    if isinstance(admitted, bool):
        return admitted
    return bool(admission_result)


async def _evaluate_admission(
    admission_service: AdmissionPort,
    action: Action,
    snapshot: CapabilitySnapshot,
) -> Any:
    """Evaluate admission via the canonical ``admit(action, snapshot)`` interface.

    Args:
        admission_service: Admission service implementing ``admit()``.
        action: Candidate action to evaluate.
        snapshot: Current capability snapshot.

    Returns:
        Admission result (bool or ``AdmissionResult``).

    Raises:
        TypeError: When the service does not implement ``admit()``.

    """
    admit_method = getattr(admission_service, "admit", None)
    if not callable(admit_method):
        raise TypeError(
            f"Admission service {type(admission_service).__name__!r} must implement admit()."
        )
    return await admit_method(action, snapshot)


def _resolve_idempotency_envelope(
    admission_result: Any,
    turn_identity: _TurnIdentity,
    action: Action,
    snapshot: CapabilitySnapshot,
    host_kind: HostKind,
) -> IdempotencyEnvelope:
    """Resolve idempotency envelope from admission result or deterministic fallback."""
    envelope_payload = getattr(admission_result, "idempotency_envelope", None)
    parsed = _parse_idempotency_envelope(
        envelope_payload=envelope_payload,
        capability_snapshot_hash=snapshot.snapshot_hash,
    )
    if parsed is not None:
        return IdempotencyEnvelope(
            dispatch_idempotency_key=turn_identity.dispatch_dedupe_key,
            operation_fingerprint=turn_identity.decision_fingerprint,
            attempt_seq=parsed.attempt_seq,
            effect_scope=parsed.effect_scope,
            capability_snapshot_hash=parsed.capability_snapshot_hash,
            host_kind=parsed.host_kind,
            peer_operation_id=parsed.peer_operation_id,
            policy_snapshot_ref=parsed.policy_snapshot_ref,
            rule_bundle_hash=parsed.rule_bundle_hash,
        )
    return IdempotencyEnvelope(
        dispatch_idempotency_key=turn_identity.dispatch_dedupe_key,
        operation_fingerprint=turn_identity.decision_fingerprint,
        attempt_seq=1,
        effect_scope=action.effect_class,
        capability_snapshot_hash=snapshot.snapshot_hash,
        host_kind=host_kind,
    )


def _parse_idempotency_envelope(
    envelope_payload: Any,
    capability_snapshot_hash: str,
) -> IdempotencyEnvelope | None:  # pylint: disable=too-many-return-statements
    """Parse envelope from admission-provided payload when valid."""
    if isinstance(envelope_payload, IdempotencyEnvelope):
        return envelope_payload
    if not isinstance(envelope_payload, dict):
        return None

    key = envelope_payload.get("dispatch_idempotency_key")
    operation_fingerprint = envelope_payload.get("operation_fingerprint")
    attempt_seq = envelope_payload.get("attempt_seq")
    effect_scope = envelope_payload.get("effect_scope")
    host_kind_value = envelope_payload.get("host_kind")
    if not isinstance(key, str) or key == "":
        return None
    if not isinstance(operation_fingerprint, str) or operation_fingerprint == "":
        return None
    if not isinstance(attempt_seq, int) or attempt_seq <= 0:
        return None
    if not isinstance(effect_scope, str) or effect_scope == "":
        return None
    normalized_host_kind = _normalize_host_kind(host_kind_value)
    if normalized_host_kind is None:
        return None

    return IdempotencyEnvelope(
        dispatch_idempotency_key=key,
        operation_fingerprint=operation_fingerprint,
        attempt_seq=attempt_seq,
        effect_scope=effect_scope,
        capability_snapshot_hash=(
            str(envelope_payload.get("capability_snapshot_hash", capability_snapshot_hash))
        ),
        host_kind=normalized_host_kind,
        peer_operation_id=_read_optional_string(envelope_payload, "peer_operation_id"),
        policy_snapshot_ref=_read_optional_string(envelope_payload, "policy_snapshot_ref"),
        rule_bundle_hash=_read_optional_string(envelope_payload, "rule_bundle_hash"),
    )


def _build_execution_context(
    input_value: TurnInput,
    action: Action,
    snapshot: CapabilitySnapshot,
    turn_identity: _TurnIdentity,
    admission_result: Any,
    envelope: IdempotencyEnvelope,
) -> ExecutionContext:
    """Build execution context envelope for executor/recovery traceability."""
    policy_snapshot_ref = getattr(admission_result, "policy_snapshot_ref", None)
    grant_ref = getattr(admission_result, "grant_ref", None)
    rule_bundle_hash = envelope.rule_bundle_hash
    declarative_bundle_digest = None
    if snapshot.declarative_bundle_digest is not None:
        declarative_bundle_digest = {
            "bundle_ref": snapshot.declarative_bundle_digest.bundle_ref,
            "semantics_version": snapshot.declarative_bundle_digest.semantics_version,
            "content_hash": snapshot.declarative_bundle_digest.content_hash,
            "compile_hash": snapshot.declarative_bundle_digest.compile_hash,
        }
    return ExecutionContext(
        run_id=input_value.run_id,
        action_id=action.action_id,
        causation_id=turn_identity.intent_commit_ref,
        correlation_id=turn_identity.decision_ref,
        lineage_id=turn_identity.decision_fingerprint,
        capability_snapshot_ref=snapshot.snapshot_ref,
        capability_snapshot_hash=snapshot.snapshot_hash,
        context_binding_ref=snapshot.context_binding_ref,
        grant_ref=grant_ref if isinstance(grant_ref, str) else None,
        policy_snapshot_ref=(policy_snapshot_ref if isinstance(policy_snapshot_ref, str) else None),
        rule_bundle_hash=rule_bundle_hash,
        declarative_bundle_digest=declarative_bundle_digest,
        timeout_ms=action.timeout_ms,
        budget_ref=snapshot.budget_ref,
    )


def _execution_context_payload(
    execution_context: ExecutionContext,
) -> dict[str, Any]:
    """Build minimal replay-safe execution context payload."""
    return {
        "run_id": execution_context.run_id,
        "action_id": execution_context.action_id,
        "causation_id": execution_context.causation_id,
        "correlation_id": execution_context.correlation_id,
        "lineage_id": execution_context.lineage_id,
        "capability_snapshot_ref": execution_context.capability_snapshot_ref,
        "capability_snapshot_hash": execution_context.capability_snapshot_hash,
        "context_binding_ref": execution_context.context_binding_ref,
        "grant_ref": execution_context.grant_ref,
        "policy_snapshot_ref": execution_context.policy_snapshot_ref,
        "rule_bundle_hash": execution_context.rule_bundle_hash,
        "declarative_bundle_digest": execution_context.declarative_bundle_digest,
        "timeout_ms": execution_context.timeout_ms,
        "budget_ref": execution_context.budget_ref,
    }


async def _execute_with_context(
    executor: ExecutorPort,
    action: Action,
    snapshot: CapabilitySnapshot,
    envelope: IdempotencyEnvelope,
    execution_context: ExecutionContext,
) -> dict[str, Any]:
    """Execute action and passes execution context when executor supports it."""
    execute_method = executor.execute
    try:
        signature = inspect.signature(execute_method)
    except TypeError, ValueError:
        signature = None

    if signature is not None and "execution_context" in signature.parameters:
        return await execute_method(
            action,
            snapshot,
            envelope,
            execution_context=execution_context,
        )
    return await execute_method(action, snapshot, envelope)


def _reserve_with_degradation(
    dedupe_store: DedupeStorePort,
    envelope: IdempotencyEnvelope,
    action: Action,
    host_kind: HostKind,
    emitted_events: list[TurnStateEvent],
) -> tuple[DedupeReservation, IdempotencyEnvelope, bool]:
    """Atomically reserves-and-dispatches dedupe key; degrades on store failure.

    Uses ``reserve_and_dispatch()`` to close the non-atomic window between
    reservation and dispatch state update.  Falls back to graceful degradation
    when the store is unavailable (idempotent_write effect class only).
    """
    try:
        return dedupe_store.reserve_and_dispatch(envelope), envelope, True
    except Exception as error:  # pylint: disable=broad-exception-caught
        if action.effect_class != "idempotent_write":
            raise
        downgraded_effect_scope = _resolve_dedupe_downgrade_scope(host_kind=host_kind)
        downgraded_envelope = IdempotencyEnvelope(
            dispatch_idempotency_key=envelope.dispatch_idempotency_key,
            operation_fingerprint=envelope.operation_fingerprint,
            attempt_seq=envelope.attempt_seq,
            effect_scope=downgraded_effect_scope,
            capability_snapshot_hash=envelope.capability_snapshot_hash,
            host_kind=envelope.host_kind,
            peer_operation_id=envelope.peer_operation_id,
            policy_snapshot_ref=envelope.policy_snapshot_ref,
            rule_bundle_hash=envelope.rule_bundle_hash,
        )
        _logger.warning(
            "dedupe_store unavailable run=%s action=%s effect_class=%s 鈥?degrading",
            action.run_id,
            action.action_id,
            action.effect_class,
        )
        emitted_events.append(
            TurnStateEvent(
                state="dedupe_degraded",
                reason=type(error).__name__,
                metadata={
                    "from_effect_scope": "idempotent_write",
                    "to_effect_scope": downgraded_effect_scope,
                },
            )
        )
        return DedupeReservation(accepted=True, reason="accepted"), downgraded_envelope, False


def _resolve_dedupe_downgrade_scope(host_kind: HostKind) -> str:
    """Resolve degraded effect scope when dedupe persistence is unavailable."""
    if host_kind == "remote_service":
        return "irreversible_write"
    return "compensatable_write"


def _resolve_snapshot_input(
    input_value: TurnInput,
    action: Action,
    resolver: SnapshotInputResolverPort | None,
    require_declared_snapshot_inputs: bool,
) -> CapabilitySnapshotInput:
    """Resolve snapshot input from resolver or action payload.

    When ``require_declared_snapshot_inputs`` is enabled, missing declared
    payload raises ``CapabilitySnapshotBuildError`` to enforce v6.4 strict mode.

    Args:
        input_value: Turn input providing run identity and offsets.
        action: Action carrying optional declared snapshot payload.
        resolver: Optional resolver override for snapshot input.
        require_declared_snapshot_inputs: Enforces strict mode when True.

    Returns:
        Normalized snapshot input for capability snapshot construction.

    Raises:
        CapabilitySnapshotBuildError: When strict mode is enabled and
            no declared snapshot payload is present in the action.

    """
    if resolver is not None:
        return resolver.resolve(input_value, action)

    input_json = action.input_json if isinstance(action.input_json, dict) else {}
    declared_payload = input_json.get("capability_snapshot_input")
    if isinstance(declared_payload, dict):
        return CapabilitySnapshotInput(
            run_id=input_value.run_id,
            based_on_offset=input_value.based_on_offset,
            tenant_policy_ref=str(declared_payload.get("tenant_policy_ref", "")),
            permission_mode=str(declared_payload.get("permission_mode", "")),
            tool_bindings=list(declared_payload.get("tool_bindings", [])),
            mcp_bindings=list(declared_payload.get("mcp_bindings", [])),
            skill_bindings=list(declared_payload.get("skill_bindings", [])),
            feature_flags=list(declared_payload.get("feature_flags", [])),
            context_binding_ref=declared_payload.get("context_binding_ref"),
            context_content_hash=declared_payload.get("context_content_hash"),
            budget_ref=declared_payload.get("budget_ref"),
            quota_ref=declared_payload.get("quota_ref"),
            session_mode=declared_payload.get("session_mode"),
            approval_state=declared_payload.get("approval_state"),
        )

    if require_declared_snapshot_inputs:
        raise CapabilitySnapshotBuildError("capability_snapshot_input is required in strict mode.")
    return CapabilitySnapshotInput(
        run_id=input_value.run_id,
        based_on_offset=input_value.based_on_offset,
        tenant_policy_ref="policy:default",
        permission_mode="strict",
    )


def _build_turn_identity(
    input_value: TurnInput,
    action: Action,
    snapshot_hash: str | None = None,
) -> _TurnIdentity:
    """Build deterministic identity fields for one turn.

    Args:
        input_value: Turn input with run identity and offsets.
        action: Action providing action identity for fingerprinting.
        snapshot_hash: Optional snapshot hash used by idempotency policy.

    Returns:
        Immutable turn identity with decision references and dedupe key.

    """
    return _TurnIdentity(
        decision_ref=f"decision:{input_value.run_id}:{input_value.based_on_offset}",
        decision_fingerprint=(
            f"{input_value.run_id}:"
            f"{input_value.trigger_type}:"
            f"{action.action_id}:"
            f"{input_value.based_on_offset}"
        ),
        intent_commit_ref=f"intent:{action.action_id}:{input_value.through_offset}",
        dispatch_dedupe_key=(
            IdempotencyKeyPolicy.generate(
                run_id=input_value.run_id,
                action=action,
                snapshot_hash=snapshot_hash,
            )
            if snapshot_hash is not None
            else _legacy_dispatch_dedupe_key(
                run_id=input_value.run_id,
                action_id=action.action_id,
                based_on_offset=input_value.based_on_offset,
            )
        ),
    )


def _legacy_dispatch_dedupe_key(run_id: str, action_id: str, based_on_offset: int) -> str:
    """Return historical dispatch key format used by pre-policy snapshots."""
    return f"{run_id}:{action_id}:{based_on_offset}"


def _resolve_dispatch_policy(action: Action) -> _DispatchPolicy:
    """Resolve dispatch host and enforces remote idempotency safeguards.

    Conservative v6.4 enforcement:
      - Explicit local hosts bypass remote contract checks.
      - Remote side-effect dispatch evaluates remote idempotency policy.
      - Guaranteed claims are blocked when remote contract cannot prove them.

    Args:
        action: Candidate action to evaluate dispatch policy for.

    Returns:
        Dispatch policy with host kind and optional block decision.

    """
    host_kind = _resolve_dispatch_host_kind(action)
    if host_kind != "remote_service" or action.effect_class == "read_only":
        return _DispatchPolicy(host_kind=host_kind, should_block=False)

    remote_contract = _extract_remote_service_contract(action.input_json)
    remote_policy = evaluate_remote_service_policy(
        external_level=action.external_idempotency_level,
        contract=remote_contract,
    )
    requested_guaranteed = action.external_idempotency_level == "guaranteed"
    if requested_guaranteed and not remote_policy.can_claim_guaranteed:
        return _DispatchPolicy(
            host_kind=host_kind,
            should_block=True,
            block_reason="idempotency_contract_insufficient",
            remote_policy_decision=remote_policy,
        )
    return _DispatchPolicy(
        host_kind=host_kind,
        should_block=False,
        remote_policy_decision=remote_policy,
    )


def _resolve_dispatch_host_kind(action: Action) -> HostKind:
    """Resolve dispatch host kind from explicit hints and effect metadata.

    Args:
        action: Action with policy tags, payload, and effect metadata.

    Returns:
        Resolved host kind, defaulting to ``"local_cli"``.

    """
    explicit_host_kind = _resolve_explicit_host_kind(action)
    if explicit_host_kind is not None:
        return explicit_host_kind
    # Side-effect actions with declared external idempotency are remote by intent.
    if action.effect_class != "read_only" and action.external_idempotency_level is not None:
        return "remote_service"
    return "local_cli"


def _resolve_explicit_host_kind(action: Action) -> HostKind | None:
    """Resolve explicit host kind from policy tags and action payload.

    Args:
        action: Action carrying policy tags and input payload.

    Returns:
        Explicit host kind when found, otherwise ``None``.

    """
    host_kind_from_tags = _extract_host_kind_from_policy_tags(action.policy_tags)
    if host_kind_from_tags is not None:
        return host_kind_from_tags

    payload = action.input_json if isinstance(action.input_json, dict) else {}
    host_kind_from_payload = _extract_host_kind_from_payload(payload)
    if host_kind_from_payload is not None:
        return host_kind_from_payload
    return None


def _extract_host_kind_from_policy_tags(policy_tags: list[str]) -> HostKind | None:
    """Extract host kind from policy tags when present.

    Args:
        policy_tags: List of policy tag strings to search.

    Returns:
        Normalized host kind when a valid tag is found, else ``None``.

    """
    for tag in policy_tags:
        normalized_tag = tag.strip().lower()
        for prefix in (
            "host:",
            "host_kind:",
            "dispatch_host:",
            "dispatch_host_kind:",
        ):
            if not normalized_tag.startswith(prefix):
                continue
            host_kind = _normalize_host_kind(normalized_tag.removeprefix(prefix))
            if host_kind is not None:
                return host_kind
    return None


def _extract_host_kind_from_payload(payload: dict[str, Any]) -> HostKind | None:
    """Extract host kind from action payload using conservative key aliases.

    Args:
        payload: Action input payload dictionary.

    Returns:
        Normalized host kind when a valid value is found, else ``None``.

    """
    for key in ("host_kind", "dispatch_host_kind", "host", "dispatch_host"):
        host_kind = _normalize_host_kind(payload.get(key))
        if host_kind is not None:
            return host_kind

    dispatch_payload = payload.get("dispatch")
    if isinstance(dispatch_payload, dict):
        for key in ("host_kind", "dispatch_host_kind", "host", "dispatch_host"):
            host_kind = _normalize_host_kind(dispatch_payload.get(key))
            if host_kind is not None:
                return host_kind
    return None


def _normalize_host_kind(value: Any) -> HostKind | None:
    """Normalize one host kind value to canonical literal form.

    Args:
        value: Raw host kind value, typically a string.

    Returns:
        Canonical host kind literal, or ``None`` when invalid.

    """
    if not isinstance(value, str):
        return None
    normalized_value = value.strip().lower()
    if normalized_value in (
        "local_cli",
        "local_process",
        "remote_service",
        "cli_process",
        "in_process_python",
    ):
        return normalized_value
    return None


def _extract_remote_service_contract(
    input_json: dict[str, Any] | None,
) -> RemoteServiceIdempotencyContract | None:
    """Extract remote-service contract from action payload if available.

    Args:
        input_json: Action input payload, may be ``None``.

    Returns:
        Parsed contract when a valid candidate is found, else ``None``.

    """
    if not isinstance(input_json, dict):
        return None

    candidates: list[Any] = [
        input_json.get("remote_service_idempotency_contract"),
        input_json.get("remote_idempotency_contract"),
        input_json.get("idempotency_contract"),
    ]
    remote_service_payload = input_json.get("remote_service")
    if isinstance(remote_service_payload, dict):
        candidates.extend(
            [
                remote_service_payload.get("idempotency_contract"),
                remote_service_payload.get("contract"),
            ]
        )

    for candidate in candidates:
        parsed_contract = _parse_remote_service_contract(candidate)
        if parsed_contract is not None:
            return parsed_contract
    return None


def _parse_remote_service_contract(
    payload: Any,
) -> RemoteServiceIdempotencyContract | None:
    """Parse RemoteServiceIdempotencyContract from dict payload.

    Args:
        payload: Candidate dict payload with contract fields.

    Returns:
        Typed contract when all required fields are valid, else ``None``.

    """
    if not isinstance(payload, dict):
        return None

    accepts_dispatch_key = payload.get("accepts_dispatch_idempotency_key")
    returns_stable_ack = payload.get("returns_stable_ack")
    peer_retry_model = payload.get("peer_retry_model")
    default_retry_policy = payload.get("default_retry_policy")
    if not isinstance(accepts_dispatch_key, bool):
        return None
    if not isinstance(returns_stable_ack, bool):
        return None
    if peer_retry_model not in (
        "unknown",
        "at_most_once",
        "at_least_once",
        "exactly_once_claimed",
    ):
        return None
    if default_retry_policy not in ("no_auto_retry", "bounded_retry"):
        return None

    return RemoteServiceIdempotencyContract(
        accepts_dispatch_idempotency_key=accepts_dispatch_key,
        returns_stable_ack=returns_stable_ack,
        peer_retry_model=peer_retry_model,
        default_retry_policy=default_retry_policy,
    )


def _read_optional_string(payload: dict[str, Any], key: str) -> str | None:
    """Read one non-empty string field from a payload dict.

    Args:
        payload: Dictionary to read from.
        key: Key to look up.

    Returns:
        Trimmed non-empty string value, or ``None`` when absent or empty.

    """
    value = payload.get(key)
    if isinstance(value, str):
        normalized_value = value.strip()
        if normalized_value != "":
            return normalized_value
    return None


def _build_recovery_pending_turn_result(
    input_value: TurnInput,
    action: Action,
    turn_identity: _TurnIdentity,
    execute_result: dict[str, Any],
    host_kind: HostKind,
    remote_policy_decision: RemoteDispatchPolicyDecision | None,
    emitted_events: list[TurnStateEvent],
) -> TurnResult:
    """Build recovery-pending result with normalized failure evidence fields.

    Args:
        input_value: Original turn input.
        action: Action that produced ambiguous side-effect evidence.
        turn_identity: Deterministic identity for the turn.
        execute_result: Raw execution result from the executor.
        host_kind: Resolved dispatch host kind.
        remote_policy_decision: Optional remote policy evaluation result.
        emitted_events: Accumulated state-transition events.

    Returns:
        Turn result in ``recovery_pending`` state with failure envelope.

    """
    external_ack_ref = _read_optional_string(execute_result, "external_ack_ref")
    evidence_ref = _read_optional_string(execute_result, "evidence_ref")
    local_inference = (
        _read_optional_string(
            execute_result,
            "local_inference",
        )
        or "turn_engine:effect_unknown_without_ack"
    )
    failure_envelope = FailureEnvelope(
        run_id=input_value.run_id,
        action_id=action.action_id,
        failed_stage="execution",
        failed_component="executor",
        failure_code="effect_unknown",
        failure_class="unknown",
        evidence_ref=evidence_ref,
        external_ack_ref=external_ack_ref,
        local_inference=local_inference,
        human_escalation_hint="manual_verification_required",
    )
    return TurnResult(
        state="recovery_pending",
        outcome_kind="recovery_pending",
        decision_ref=turn_identity.decision_ref,
        decision_fingerprint=turn_identity.decision_fingerprint,
        dispatch_dedupe_key=turn_identity.dispatch_dedupe_key,
        intent_commit_ref=turn_identity.intent_commit_ref,
        host_kind=host_kind,
        remote_policy_decision=remote_policy_decision,
        recovery_input=apply_failure_evidence_priority(failure_envelope),
        emitted_events=emitted_events,
    )
