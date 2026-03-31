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

import inspect
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from agent_kernel.kernel.capability_snapshot import (
    CapabilitySnapshot,
    CapabilitySnapshotBuildError,
    CapabilitySnapshotInput,
)
from agent_kernel.kernel.contracts import (
    Action,
    ExecutionContext,
    FailureEnvelope,
    RemoteServiceIdempotencyContract,
)
from agent_kernel.kernel.dedupe_store import (
    DedupeReservation,
    DedupeStorePort,
    HostKind,
    IdempotencyEnvelope,
)
from agent_kernel.kernel.failure_evidence import apply_failure_evidence_priority
from agent_kernel.kernel.remote_service_policy import (
    RemoteDispatchPolicyDecision,
    evaluate_remote_service_policy,
)

TurnTriggerType = Literal["start", "signal", "child_join", "recovery_resume"]
TurnState = Literal[
    "collecting",
    "intent_committed",
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
class TurnInput:
    """Represents one turn execution trigger for a run.

    Attributes:
        run_id: Kernel run identifier.
        through_offset: Upper-bound offset for this turn.
        based_on_offset: Baseline offset the turn replays from.
        trigger_type: Discriminator for the turn trigger origin.
    """

    run_id: str
    through_offset: int
    based_on_offset: int
    trigger_type: TurnTriggerType


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
    emitted_events: list[dict[str, Any]] = field(default_factory=list)


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
        """Builds one capability snapshot.

        Args:
            input_value: Normalized snapshot input payload.

        Returns:
            Immutable capability snapshot with deterministic hash.
        """


class AdmissionPort(Protocol):
    """Protocol for admission checks in turn execution."""

    async def admit(self, action: Action, snapshot: CapabilitySnapshot) -> Any:
        """Returns whether action is admitted for dispatch under snapshot."""

    async def check(self, action: Action, snapshot: CapabilitySnapshot) -> Any:
        """Returns whether action is admitted for dispatch.

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
        """Executes one action and returns execution evidence payload.

        Args:
            action: Admitted action to execute.
            snapshot: Capability snapshot governing execution.
            envelope: Idempotency envelope for dispatch deduplication.

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
        """Resolves one snapshot input object.

        Args:
            input_value: Turn input providing run identity and offsets.
            action: Action carrying declared snapshot payload.

        Returns:
            Normalized snapshot input for capability snapshot builder.
        """


class TurnEngine:
    """Runs one canonical turn with strict v6.4 guardrails."""

    def __init__(
        self,
        snapshot_builder: SnapshotBuilderPort,
        admission_service: AdmissionPort,
        dedupe_store: DedupeStorePort,
        executor: ExecutorPort,
        snapshot_input_resolver: SnapshotInputResolverPort | None = None,
        require_declared_snapshot_inputs: bool = False,
    ) -> None:
        """Initializes TurnEngine with required service dependencies.

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
        """
        self._snapshot_builder = snapshot_builder
        self._admission_service = admission_service
        self._dedupe_store = dedupe_store
        self._executor = executor
        self._snapshot_input_resolver = snapshot_input_resolver
        self._require_declared_snapshot_inputs = require_declared_snapshot_inputs

    async def run_turn(
        self,
        input_value: TurnInput,
        action: Action,
        admission_subject: Any | None = None,
    ) -> TurnResult:
        """Runs one turn across snapshot, admission, dedupe, and execution.

        Args:
            input_value: Turn trigger and offset context.
            action: Candidate action selected by upstream decision path.
            admission_subject: Optional admission subject override. When
                ``None``, the capability snapshot is used.

        Returns:
            Deterministic turn result with explicit outcome class.

        Raises:
            TypeError: If the admission result is an unawaited awaitable.
        """
        turn_identity = _build_turn_identity(input_value=input_value, action=action)
        emitted_events: list[dict[str, Any]] = [
            {"state": "collecting"},
            {"state": "intent_committed"},
        ]
        authoritative_dispatch_attempts = 0

        try:
            snapshot_input = _resolve_snapshot_input(
                input_value=input_value,
                action=action,
                resolver=self._snapshot_input_resolver,
                require_declared_snapshot_inputs=self._require_declared_snapshot_inputs,
            )
            snapshot = self._snapshot_builder.build(snapshot_input)
            emitted_events.append({"state": "snapshot_built"})
        except CapabilitySnapshotBuildError:
            emitted_events.append({"state": "completed_noop"})
            return TurnResult(
                state="completed_noop",
                outcome_kind="noop",
                decision_ref=turn_identity.decision_ref,
                decision_fingerprint=turn_identity.decision_fingerprint,
                dispatch_dedupe_key=turn_identity.dispatch_dedupe_key,
                intent_commit_ref=turn_identity.intent_commit_ref,
                emitted_events=emitted_events,
            )

        admission_result, used_legacy_check_fallback = await _evaluate_admission(
            admission_service=self._admission_service,
            action=action,
            snapshot=snapshot,
            admission_subject=admission_subject,
        )
        if used_legacy_check_fallback:
            emitted_events.append(_legacy_check_fallback_event())
        emitted_events.append({"state": "admission_checked"})
        if not _is_admitted(admission_result):
            emitted_events.append({"state": "dispatch_blocked"})
            return TurnResult(
                state="dispatch_blocked",
                outcome_kind="blocked",
                decision_ref=turn_identity.decision_ref,
                decision_fingerprint=turn_identity.decision_fingerprint,
                dispatch_dedupe_key=turn_identity.dispatch_dedupe_key,
                intent_commit_ref=turn_identity.intent_commit_ref,
                emitted_events=emitted_events,
            )

        dispatch_policy = _resolve_dispatch_policy(action=action)
        if dispatch_policy.should_block:
            emitted_events.append(
                {
                    "state": "dispatch_blocked",
                    "reason": dispatch_policy.block_reason,
                }
            )
            return TurnResult(
                state="dispatch_blocked",
                outcome_kind="blocked",
                decision_ref=turn_identity.decision_ref,
                decision_fingerprint=turn_identity.decision_fingerprint,
                dispatch_dedupe_key=turn_identity.dispatch_dedupe_key,
                intent_commit_ref=turn_identity.intent_commit_ref,
                host_kind=dispatch_policy.host_kind,
                remote_policy_decision=dispatch_policy.remote_policy_decision,
                emitted_events=emitted_events,
            )

        envelope = _resolve_idempotency_envelope(
            admission_result=admission_result,
            turn_identity=turn_identity,
            action=action,
            snapshot=snapshot,
            host_kind=dispatch_policy.host_kind,
        )
        reservation, envelope, dedupe_available = _reserve_with_degradation(
            dedupe_store=self._dedupe_store,
            envelope=envelope,
            action=action,
            host_kind=dispatch_policy.host_kind,
            emitted_events=emitted_events,
        )
        if not reservation.accepted:
            emitted_events.append({"state": "dispatch_blocked"})
            return TurnResult(
                state="dispatch_blocked",
                outcome_kind="blocked",
                decision_ref=turn_identity.decision_ref,
                decision_fingerprint=turn_identity.decision_fingerprint,
                dispatch_dedupe_key=turn_identity.dispatch_dedupe_key,
                intent_commit_ref=turn_identity.intent_commit_ref,
                host_kind=dispatch_policy.host_kind,
                remote_policy_decision=dispatch_policy.remote_policy_decision,
                emitted_events=emitted_events,
            )

        authoritative_dispatch_attempts += 1
        if authoritative_dispatch_attempts > 1:
            raise RuntimeError("Single turn must not perform multiple dispatch attempts.")
        if dedupe_available:
            self._dedupe_store.mark_dispatched(turn_identity.dispatch_dedupe_key)
        emitted_events.append({"state": "dispatched"})
        execution_context = _build_execution_context(
            input_value=input_value,
            action=action,
            snapshot=snapshot,
            turn_identity=turn_identity,
            admission_result=admission_result,
            envelope=envelope,
        )
        execute_result = await _execute_with_context(
            executor=self._executor,
            action=action,
            snapshot=snapshot,
            envelope=envelope,
            execution_context=execution_context,
        )
        acknowledged = bool(execute_result.get("acknowledged", True))
        if acknowledged:
            if dedupe_available:
                self._dedupe_store.mark_acknowledged(turn_identity.dispatch_dedupe_key)
            emitted_events.append({"state": "dispatch_acknowledged"})
            return TurnResult(
                state="dispatch_acknowledged",
                outcome_kind="dispatched",
                decision_ref=turn_identity.decision_ref,
                decision_fingerprint=turn_identity.decision_fingerprint,
                dispatch_dedupe_key=turn_identity.dispatch_dedupe_key,
                intent_commit_ref=turn_identity.intent_commit_ref,
                host_kind=dispatch_policy.host_kind,
                remote_policy_decision=dispatch_policy.remote_policy_decision,
                action_commit={
                    "action_id": action.action_id,
                    "run_id": input_value.run_id,
                    "committed_at": _utc_now_iso(),
                    "execution_context": _execution_context_payload(execution_context),
                },
                emitted_events=emitted_events,
            )

        if dedupe_available:
            self._dedupe_store.mark_unknown_effect(turn_identity.dispatch_dedupe_key)
        emitted_events.extend(
            [{"state": "effect_unknown"}, {"state": "recovery_pending"}]
        )
        return _build_recovery_pending_turn_result(
            input_value=input_value,
            action=action,
            turn_identity=turn_identity,
            execute_result=execute_result,
            host_kind=dispatch_policy.host_kind,
            remote_policy_decision=dispatch_policy.remote_policy_decision,
            emitted_events=emitted_events,
        )


def _utc_now_iso() -> str:
    """Returns an RFC3339 UTC timestamp.

    Returns:
        UTC timestamp string in ``YYYY-MM-DDTHH:MM:SSZ`` format.
    """
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def _is_admitted(admission_result: Any) -> bool:
    """Resolves admitted bool from varied admission return shapes.

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
    admission_subject: Any | None,
) -> tuple[Any, bool]:
    """Evaluates admission preferring ``admit(action, snapshot)`` semantics."""
    admit_method = getattr(admission_service, "admit", None)
    if callable(admit_method):
        return await admit_method(action, snapshot), False

    check_method = getattr(admission_service, "check", None)
    if callable(check_method):
        subject = admission_subject if admission_subject is not None else snapshot
        return await check_method(action, subject), True
    raise TypeError("Admission service must implement admit() or check().")


def _resolve_idempotency_envelope(
    admission_result: Any,
    turn_identity: _TurnIdentity,
    action: Action,
    snapshot: CapabilitySnapshot,
    host_kind: HostKind,
) -> IdempotencyEnvelope:
    """Resolves idempotency envelope from admission result or deterministic fallback."""
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
) -> IdempotencyEnvelope | None:
    """Parses envelope from admission-provided payload when valid."""
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
    """Builds execution context envelope for executor/recovery traceability."""
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
        policy_snapshot_ref=(
            policy_snapshot_ref if isinstance(policy_snapshot_ref, str) else None
        ),
        rule_bundle_hash=rule_bundle_hash,
        declarative_bundle_digest=declarative_bundle_digest,
        timeout_ms=action.timeout_ms,
        budget_ref=snapshot.budget_ref,
    )


def _execution_context_payload(
    execution_context: ExecutionContext,
) -> dict[str, Any]:
    """Builds minimal replay-safe execution context payload."""
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
    """Executes action and passes execution context when executor supports it."""
    execute_method = executor.execute
    try:
        signature = inspect.signature(execute_method)
    except (TypeError, ValueError):
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
    emitted_events: list[dict[str, Any]],
) -> tuple[DedupeReservation, IdempotencyEnvelope, bool]:
    """Reserves dedupe key and applies v6.4 degradation when store unavailable."""
    try:
        return dedupe_store.reserve(envelope), envelope, True
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
        emitted_events.append(
            {
                "state": "dedupe_degraded",
                "reason": type(error).__name__,
                "from_effect_scope": "idempotent_write",
                "to_effect_scope": downgraded_effect_scope,
            }
        )
        return DedupeReservation(accepted=True, reason="accepted"), downgraded_envelope, False


def _resolve_dedupe_downgrade_scope(host_kind: HostKind) -> str:
    """Resolves degraded effect scope when dedupe persistence is unavailable."""
    if host_kind == "remote_service":
        return "irreversible_write"
    return "compensatable_write"


def _legacy_check_fallback_event() -> dict[str, str]:
    """Builds structured deprecation signal for legacy ``check`` fallback path."""
    return {
        "state": "admission_legacy_check_fallback",
        "severity": "warning",
        "deprecation_phase": "soft",
        "target_removal_version": "0.2",
    }


def _resolve_snapshot_input(
    input_value: TurnInput,
    action: Action,
    resolver: SnapshotInputResolverPort | None,
    require_declared_snapshot_inputs: bool,
) -> CapabilitySnapshotInput:
    """Resolves snapshot input from resolver or action payload.

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
        raise CapabilitySnapshotBuildError(
            "capability_snapshot_input is required in strict mode."
        )
    return CapabilitySnapshotInput(
        run_id=input_value.run_id,
        based_on_offset=input_value.based_on_offset,
        tenant_policy_ref="policy:default",
        permission_mode="strict",
    )


def _build_turn_identity(input_value: TurnInput, action: Action) -> _TurnIdentity:
    """Builds deterministic identity fields for one turn.

    Args:
        input_value: Turn input with run identity and offsets.
        action: Action providing action identity for fingerprinting.

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
            f"{input_value.run_id}:{action.action_id}:{input_value.based_on_offset}"
        ),
    )


def _resolve_dispatch_policy(action: Action) -> _DispatchPolicy:
    """Resolves dispatch host and enforces remote idempotency safeguards.

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
    """Resolves dispatch host kind from explicit hints and effect metadata.

    Args:
        action: Action with policy tags, payload, and effect metadata.

    Returns:
        Resolved host kind, defaulting to ``"local_cli"``.
    """
    explicit_host_kind = _resolve_explicit_host_kind(action)
    if explicit_host_kind is not None:
        return explicit_host_kind
    # Side-effect actions with declared external idempotency are remote by intent.
    if (
        action.effect_class != "read_only"
        and action.external_idempotency_level is not None
    ):
        return "remote_service"
    return "local_cli"


def _resolve_explicit_host_kind(action: Action) -> HostKind | None:
    """Resolves explicit host kind from policy tags and action payload.

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
    """Extracts host kind from policy tags when present.

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
    """Extracts host kind from action payload using conservative key aliases.

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
    """Normalizes one host kind value to canonical literal form.

    Args:
        value: Raw host kind value, typically a string.

    Returns:
        Canonical host kind literal, or ``None`` when invalid.
    """
    if not isinstance(value, str):
        return None
    normalized_value = value.strip().lower()
    if normalized_value in ("local_cli", "local_process", "remote_service"):
        return normalized_value
    return None


def _extract_remote_service_contract(
    input_json: dict[str, Any] | None,
) -> RemoteServiceIdempotencyContract | None:
    """Extracts remote-service contract from action payload if available.

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
    """Parses RemoteServiceIdempotencyContract from dict payload.

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
    """Reads one non-empty string field from a payload dict.

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
    emitted_events: list[dict[str, Any]],
) -> TurnResult:
    """Builds recovery-pending result with normalized failure evidence fields.

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
    local_inference = _read_optional_string(
        execute_result,
        "local_inference",
    ) or "turn_engine:effect_unknown_without_ack"
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
