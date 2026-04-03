"""Provides minimal in-memory implementations for core kernel services.

This module intentionally implements the smallest executable subset required
by the agent_kernel architecture:
  - Runtime event truth storage,
  - Projection catch-up and readiness checks,
  - Dispatch admission gate,
  - Executor entrypoint,
  - Recovery gate,
  - Decision deduplication.

These implementations are designed for PoC and local tests. They preserve
contract semantics and strict boundaries, but they are not production storage.
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from agent_kernel.kernel.capability_snapshot import CapabilitySnapshot
from agent_kernel.kernel.contracts import (
    Action,
    ActionCommit,
    AdmissionResult,
    DecisionDeduper,
    DecisionProjectionService,
    DispatchAdmissionService,
    ExecutorService,
    KernelRuntimeEventLog,
    MCPActivityInput,
    RecoveryDecision,
    RecoveryGateService,
    RecoveryInput,
    RecoveryOutcome,
    RecoveryOutcomeStore,
    RemoteServiceIdempotencyContract,
    RunProjection,
    RuntimeEvent,
    SandboxGrant,
    TemporalActivityGateway,
    ToolActivityInput,
)
from agent_kernel.kernel.remote_service_policy import evaluate_remote_service_policy

AsyncActionHandler = Callable[[Action, str | None], Awaitable[Any]]


class InMemoryKernelRuntimeEventLog(KernelRuntimeEventLog):
    """Stores authoritative runtime events in-process for one test runtime.

    The implementation enforces monotonically increasing offsets per run and
    returns committed events in strict offset order.
    """

    def __init__(self) -> None:
        self._events_by_run: dict[str, list[RuntimeEvent]] = {}
        self._next_offset_by_run: dict[str, int] = {}
        self._commit_sequence: int = 0

    async def append_action_commit(self, commit: ActionCommit) -> str:
        """Appends one action commit and normalizes event offsets.

        Args:
            commit: Commit envelope to append.

        Returns:
            A synthetic commit reference string.

        Raises:
            ValueError: If commit has no events.
        """
        if not commit.events:
            raise ValueError("ActionCommit.events must contain at least one event.")

        run_events = self._events_by_run.setdefault(commit.run_id, [])
        next_offset = self._next_offset_by_run.get(commit.run_id, 1)
        for event in commit.events:
            normalized_offset = next_offset
            run_events.append(
                RuntimeEvent(
                    run_id=event.run_id,
                    event_id=event.event_id,
                    commit_offset=normalized_offset,
                    event_type=event.event_type,
                    event_class=event.event_class,
                    event_authority=event.event_authority,
                    ordering_key=event.ordering_key,
                    wake_policy=event.wake_policy,
                    created_at=event.created_at,
                    idempotency_key=event.idempotency_key,
                    payload_ref=event.payload_ref,
                    payload_json=event.payload_json,
                )
            )
            next_offset += 1

        self._next_offset_by_run[commit.run_id] = next_offset
        self._commit_sequence += 1
        return f"commit-ref-{self._commit_sequence}"

    async def load(self, run_id: str, after_offset: int = 0) -> list[RuntimeEvent]:
        """Loads run events after an offset in ascending order.

        Args:
            run_id: Run identifier to load events for.
            after_offset: Exclusive lower bound offset.

        Returns:
            Ordered list of runtime events after the specified offset.
        """
        run_events = self._events_by_run.get(run_id, [])
        return [event for event in run_events if event.commit_offset > after_offset]


class InMemoryDecisionProjectionService(DecisionProjectionService):
    """Builds authoritative run projection by replaying runtime events."""

    def __init__(
        self,
        event_log: KernelRuntimeEventLog,
        reject_derived_diagnostic_authority_input: bool = True,
    ) -> None:
        """Initializes projection service with backing event log.

        Args:
            event_log: Authoritative event log for replay.
            reject_derived_diagnostic_authority_input: When ``True``, rejects
                ``derived_diagnostic`` events from entering projection
                authority replay path.
        """
        self._event_log = event_log
        self._projection_by_run: dict[str, RunProjection] = {}
        self._reject_derived_diagnostic_authority_input = (
            reject_derived_diagnostic_authority_input
        )

    async def catch_up(self, run_id: str, through_offset: int) -> RunProjection:
        """Catches up projection state through the requested offset.

        Args:
            run_id: Run identifier to catch up.
            through_offset: Target offset to replay through.

        Returns:
            Updated projection at or past ``through_offset``.
        """
        projection = self._projection_by_run.get(run_id, _default_projection(run_id))
        unseen_events = await self._event_log.load(run_id, after_offset=projection.projected_offset)
        for event in unseen_events:
            if event.commit_offset > through_offset:
                break
            should_replay = _validate_projection_authority_input(
                event=event,
                reject_derived_diagnostic_authority_input=(
                    self._reject_derived_diagnostic_authority_input
                ),
            )
            if not should_replay:
                continue
            projection = _apply_projection_event(projection, event)
        self._projection_by_run[run_id] = projection
        return projection

    async def readiness(self, run_id: str, required_offset: int) -> bool:
        """Returns whether projection has reached required offset.

        Args:
            run_id: Run identifier to check.
            required_offset: Minimum required offset.

        Returns:
            ``True`` when projection has reached the required offset.
        """
        projection = await self.get(run_id)
        return projection.projected_offset >= required_offset

    async def get(self, run_id: str) -> RunProjection:
        """Returns latest projection state by fully replaying unseen events.

        Args:
            run_id: Run identifier to project.

        Returns:
            Current authoritative projection for the run.
        """
        projection = self._projection_by_run.get(run_id, _default_projection(run_id))
        unseen_events = await self._event_log.load(run_id, after_offset=projection.projected_offset)
        for event in unseen_events:
            should_replay = _validate_projection_authority_input(
                event=event,
                reject_derived_diagnostic_authority_input=(
                    self._reject_derived_diagnostic_authority_input
                ),
            )
            if not should_replay:
                continue
            projection = _apply_projection_event(projection, event)
        self._projection_by_run[run_id] = projection
        return projection


class StaticDispatchAdmissionService(DispatchAdmissionService):
    """Implements the strict minimal admission gate for PoC execution."""

    async def admit(self, action: Action, snapshot: CapabilitySnapshot) -> AdmissionResult:
        """Admits action using frozen capability snapshot as primary policy input.

        Args:
            action: Candidate action to evaluate.
            snapshot: Frozen capability snapshot for policy evaluation.

        Returns:
            Admission result with grant and execution envelope when admitted.
        """
        del snapshot
        denied_result = _evaluate_action_policy_denies(action)
        if denied_result is not None:
            return denied_result
        return _build_admitted_result(action)

    async def check(self, action: Action, projection: RunProjection) -> AdmissionResult:
        """Applies minimal policy denies, then admits only dispatch-ready projections.

        Args:
            action: Candidate action to evaluate.
            projection: Current authoritative run projection.

        Returns:
            Admission result with deny reason or granted admission.
        """
        denied_result = _evaluate_action_policy_denies(action)
        if denied_result is not None:
            return denied_result
        if (
            projection.lifecycle_state in ("ready", "dispatching")
            and projection.ready_for_dispatch
            and not projection.waiting_external
        ):
            return _build_admitted_result(action)
        return AdmissionResult(admitted=False, reason_code="dependency_not_ready")


class AsyncExecutorService(ExecutorService):
    """Runs admitted actions via an injected async handler."""

    def __init__(self, handler: AsyncActionHandler | None = None) -> None:
        """Initializes executor with optional async handler.

        Args:
            handler: Optional async action handler. When ``None``,
                a deterministic default result is returned.
        """
        self._handler = handler

    async def execute(self, action: Action, grant_ref: str | None = None) -> Any:
        """Executes action via handler or a deterministic default result.

        Args:
            action: Admitted action to execute.
            grant_ref: Optional grant reference from admission.

        Returns:
            Execution result from handler or deterministic default dict.
        """
        if self._handler is None:
            return {
                "action_id": action.action_id,
                "grant_ref": grant_ref,
                "effect_observed": True,
            }
        return await self._handler(action, grant_ref)


ActivityExecutionRoute = Literal["tool", "mcp"]


class ActivityBackedExecutorService(ExecutorService):
    """Executes actions by delegating side effects to Temporal activity gateway.

    Routing rules are intentionally deterministic and narrow so they can be
    reasoned about in tests and audited during failures:
      1. If ``action.input_json`` contains a dict under key ``"mcp"``, route to MCP.
      2. Else if ``action.action_type`` is ``"mcp"`` or starts with ``"mcp."``,
         route to MCP.
      3. Else route to tool execution.

    Boundary notes:
      - This service only performs route + DTO translation.
      - It does not mutate kernel truth or evaluate admission policy.
      - ``grant_ref`` is accepted for contract compatibility but not interpreted
        by this minimal substrate adapter.
    """

    def __init__(self, activity_gateway: TemporalActivityGateway) -> None:
        """Initializes executor with activity gateway for tool/MCP dispatch.

        Args:
            activity_gateway: Gateway for delegating side-effect execution.
        """
        self._activity_gateway = activity_gateway

    async def execute(self, action: Action, grant_ref: str | None = None) -> Any:
        """Routes action to tool/MCP activity execution and returns raw result.

        Args:
            action: Admitted action to route and execute.
            grant_ref: Accepted for contract compatibility, not used.

        Returns:
            Raw execution result from the activity gateway.
        """
        del grant_ref
        route = _resolve_activity_execution_route(action)
        if route == "mcp":
            return await self._activity_gateway.execute_mcp(_build_mcp_activity_input(action))
        return await self._activity_gateway.execute_tool(_build_tool_activity_input(action))


@dataclass(frozen=True, slots=True)
class StaticRecoveryPolicy:
    """Configures fixed recovery behavior for the minimal runtime.

    Attributes:
        mode: Recovery mode to apply (``"abort"``, ``"static_compensation"``,
            or ``"human_escalation"``).
        reason_prefix: Prefix for generated recovery reason strings.
    """

    mode: str = "abort"
    reason_prefix: str = "recovery"


class StaticRecoveryGateService(RecoveryGateService):
    """Selects recovery outcome from static policy with deterministic reason."""

    def __init__(self, policy: StaticRecoveryPolicy | None = None) -> None:
        """Initializes recovery gate with static policy.

        Args:
            policy: Optional recovery policy override. Defaults to abort mode.
        """
        self._policy = policy or StaticRecoveryPolicy()

    async def decide(self, recovery_input: RecoveryInput) -> RecoveryDecision:
        """Returns policy-driven recovery decision.

        Args:
            recovery_input: Failure envelope for recovery evaluation.

        Returns:
            Deterministic recovery decision based on static policy.
        """
        return RecoveryDecision(
            run_id=recovery_input.run_id,
            mode=self._policy.mode,
            reason=f"{self._policy.reason_prefix}:{recovery_input.reason_code}",
        )


class InMemoryDecisionDeduper(DecisionDeduper):
    """Tracks decision fingerprints in-memory for idempotent decision rounds."""

    def __init__(self) -> None:
        self._seen_fingerprints: set[str] = set()

    async def seen(self, fingerprint: str) -> bool:
        """Returns whether fingerprint has already been processed.

        Args:
            fingerprint: Decision fingerprint to check.

        Returns:
            ``True`` when the fingerprint has been seen before.
        """
        return fingerprint in self._seen_fingerprints

    async def mark(self, fingerprint: str) -> None:
        """Marks fingerprint as processed.

        Args:
            fingerprint: Decision fingerprint to record.
        """
        self._seen_fingerprints.add(fingerprint)


class InMemoryRecoveryOutcomeStore(RecoveryOutcomeStore):
    """Stores recovery outcomes in memory for PoC recovery-closure tests."""

    def __init__(self) -> None:
        self._outcomes_by_run: dict[str, list[RecoveryOutcome]] = {}

    async def write_outcome(self, outcome: RecoveryOutcome) -> None:
        """Appends one recovery outcome for the run.

        Args:
            outcome: Recovery outcome to persist.
        """
        run_outcomes = self._outcomes_by_run.setdefault(outcome.run_id, [])
        run_outcomes.append(outcome)

    async def latest_for_run(self, run_id: str) -> RecoveryOutcome | None:
        """Returns latest outcome or ``None`` when run has no records.

        Args:
            run_id: Run identifier to look up.

        Returns:
            Most recent recovery outcome, or ``None`` if none exists.
        """
        run_outcomes = self._outcomes_by_run.get(run_id, [])
        if not run_outcomes:
            return None
        return run_outcomes[-1]


DispatchHostKind = Literal["local_cli", "local_process", "remote_service"]


def _requires_remote_idempotency_contract_block(action: Action) -> bool:
    """Returns whether admission must block a remote guaranteed claim.

    This is a conservative second safety net on top of TurnEngine. Admission
    blocks only when the action requests a remote guaranteed side-effect claim
    and the provided contract cannot prove required capabilities.
    """
    if action.effect_class == "read_only":
        return False
    if action.external_idempotency_level != "guaranteed":
        return False
    if _resolve_dispatch_host_kind(action) != "remote_service":
        return False

    remote_contract = _extract_remote_service_contract(action.input_json)
    remote_policy = evaluate_remote_service_policy(
        external_level=action.external_idempotency_level,
        contract=remote_contract,
    )
    return not remote_policy.can_claim_guaranteed


def _resolve_dispatch_host_kind(action: Action) -> DispatchHostKind:
    """Resolves dispatch host kind using explicit hints and safe defaults."""
    explicit_host_kind = _resolve_explicit_host_kind(action)
    if explicit_host_kind is not None:
        return explicit_host_kind
    if (
        action.effect_class != "read_only"
        and action.external_idempotency_level is not None
    ):
        return "remote_service"
    return "local_cli"


def _resolve_explicit_host_kind(action: Action) -> DispatchHostKind | None:
    """Resolves explicit host kind from policy tags or input payload."""
    host_kind_from_tags = _extract_host_kind_from_policy_tags(action.policy_tags)
    if host_kind_from_tags is not None:
        return host_kind_from_tags

    payload = action.input_json if isinstance(action.input_json, dict) else {}
    return _extract_host_kind_from_payload(payload)


def _extract_host_kind_from_policy_tags(
    policy_tags: list[str],
) -> DispatchHostKind | None:
    """Extracts host kind from policy tags when supported aliases are present."""
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


def _extract_host_kind_from_payload(
    payload: dict[str, Any],
) -> DispatchHostKind | None:
    """Extracts host kind from direct payload keys and dispatch envelope."""
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


def _normalize_host_kind(value: Any) -> DispatchHostKind | None:
    """Normalizes host kind string to canonical literal."""
    if not isinstance(value, str):
        return None
    normalized_value = value.strip().lower()
    if normalized_value in ("local_cli", "local_process", "remote_service"):
        return normalized_value
    return None


def _extract_remote_service_contract(
    input_json: dict[str, Any] | None,
) -> RemoteServiceIdempotencyContract | None:
    """Extracts first valid remote-service idempotency contract from payload."""
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
    """Parses ``RemoteServiceIdempotencyContract`` from dict payload."""
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


def _evaluate_action_policy_denies(action: Action) -> AdmissionResult | None:
    """Evaluates action-level deny checks shared by admit/check paths."""
    if "requires_human_review" in action.policy_tags:
        return AdmissionResult(admitted=False, reason_code="policy_denied")
    if action.timeout_ms is not None and action.timeout_ms > 0 and action.timeout_ms > 300000:
        return AdmissionResult(admitted=False, reason_code="policy_denied")
    max_cost = _extract_max_cost_from_policy_tags(action.policy_tags)
    estimated_cost = _extract_estimated_cost(action.input_json)
    if (
        max_cost is not None
        and estimated_cost is not None
        and estimated_cost > max_cost
    ):
        return AdmissionResult(admitted=False, reason_code="quota_exceeded")
    if _requires_remote_idempotency_contract_block(action):
        return AdmissionResult(
            admitted=False,
            reason_code="idempotency_contract_insufficient",
        )
    return None


def _build_admitted_result(action: Action) -> AdmissionResult:
    """Builds canonical admitted result with grant and execution envelope."""
    host_kind = _resolve_sandbox_host_kind(action)
    grant_ref = f"grant:{action.action_id}"
    return AdmissionResult(
        admitted=True,
        reason_code="ok",
        grant_ref=grant_ref,
        sandbox_grant=SandboxGrant(
            grant_ref=grant_ref,
            host_kind=host_kind,
            sandbox_profile_ref=f"sandbox:{host_kind}:default",
            allowed_mounts=["workspace:/"],
            denied_mounts=[],
            network_policy="deny_all" if host_kind != "remote_service" else "allow_list",
            allowed_hosts=[],
        ),
        idempotency_envelope={
            "dispatch_idempotency_key": f"dispatch:{action.run_id}:{action.action_id}",
            "operation_fingerprint": f"{action.run_id}:{action.action_id}",
            "attempt_seq": 1,
            "effect_scope": action.effect_class,
            "host_kind": host_kind,
        },
    )


def _resolve_sandbox_host_kind(
    action: Action,
) -> Literal["local_process", "local_cli", "remote_service"]:
    """Resolves conservative sandbox host kind from action hints."""
    normalized_tags = {tag.strip().lower() for tag in action.policy_tags}
    for candidate, host_kind in (
        ("host:local_process", "local_process"),
        ("host:local_cli", "local_cli"),
        ("host:remote_service", "remote_service"),
        ("host_kind:local_process", "local_process"),
        ("host_kind:local_cli", "local_cli"),
        ("host_kind:remote_service", "remote_service"),
    ):
        if candidate in normalized_tags:
            return host_kind
    if action.external_idempotency_level is not None and action.effect_class != "read_only":
        return "remote_service"
    return "local_cli"


def _extract_max_cost_from_policy_tags(policy_tags: list[str]) -> float | None:
    """Extracts the first valid ``max_cost:<number>`` value from policy tags."""
    for tag in policy_tags:
        if not tag.startswith("max_cost:"):
            continue
        raw_value = tag.removeprefix("max_cost:").strip()
        parsed_value = _parse_finite_float(raw_value)
        if parsed_value is not None:
            return parsed_value
    return None


def _extract_estimated_cost(input_json: dict[str, Any] | None) -> float | None:
    """Extracts numeric ``estimated_cost`` from action input when valid."""
    if not isinstance(input_json, dict):
        return None
    return _coerce_finite_number(input_json.get("estimated_cost"))


def _parse_finite_float(raw_value: str) -> float | None:
    """Parses a finite float from text and returns ``None`` on failure."""
    try:
        parsed_value = float(raw_value)
    except ValueError:
        return None
    if not math.isfinite(parsed_value):
        return None
    return parsed_value


def _coerce_finite_number(value: Any) -> float | None:
    """Returns finite float when value is a numeric input, else ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    return None


def _default_projection(run_id: str) -> RunProjection:
    """Creates the initial projection baseline for a run."""
    return RunProjection(
        run_id=run_id,
        lifecycle_state="created",
        projected_offset=0,
        waiting_external=False,
        ready_for_dispatch=False,
    )


def _apply_projection_event(projection: RunProjection, event: RuntimeEvent) -> RunProjection:
    """Applies one runtime event to projection state.

    The mapping intentionally covers only the minimal event taxonomy needed by
    current PoC flows. Unknown events still advance offset to preserve replay
    progress while leaving lifecycle unchanged.
    """
    (
        lifecycle_state,
        waiting_external,
        ready_for_dispatch,
        recovery_mode,
        recovery_reason,
    ) = _resolve_projection_transition(
        projection,
        event,
    )
    active_child_runs = _resolve_active_child_runs(projection, event)

    return RunProjection(
        run_id=projection.run_id,
        lifecycle_state=lifecycle_state,
        projected_offset=event.commit_offset,
        waiting_external=waiting_external,
        ready_for_dispatch=ready_for_dispatch,
        current_action_id=projection.current_action_id,
        recovery_mode=recovery_mode,
        recovery_reason=recovery_reason,
        active_child_runs=active_child_runs,
    )


def _validate_projection_authority_input(
    event: RuntimeEvent,
    reject_derived_diagnostic_authority_input: bool,
) -> bool:
    """Returns whether the event should participate in projection replay.

    ``derived_diagnostic`` events are intentionally excluded from projection
    replay (architecture invariant: "never replayed").  They may appear in the
    same ``ActionCommit`` as authoritative facts and must be silently skipped
    rather than raising an error, so observability events do not break replay.

    Args:
        event: Runtime event to evaluate.
        reject_derived_diagnostic_authority_input: When ``True``, silently
            skips ``derived_diagnostic`` events.  When ``False``, all events
            are processed (used in tests that inspect raw event streams).

    Returns:
        ``True`` when the event should be replayed, ``False`` to skip.
    """
    return not (
        reject_derived_diagnostic_authority_input and event.event_authority == "derived_diagnostic"
    )


def _resolve_projection_transition(
    projection: RunProjection,
    event: RuntimeEvent,
) -> tuple[str, bool, bool, str | None, str | None]:
    """Resolves lifecycle, waiting flag, and dispatch readiness for one event.

    Lifecycle authority lives in projection replay, not at signal ingress.
    This function is therefore the single boundary where ``run.*`` facts,
    including cancellation requests, are allowed to transition run state.
    """
    unchanged_transition = _projection_unchanged_state(projection)
    transition = unchanged_transition
    blocked_by_terminal_or_priority = (
        projection.lifecycle_state == "completed"
        or _is_lower_priority_operational_conflict(projection=projection, event=event)
        or (
            projection.lifecycle_state == "aborted"
            and event.event_type != "run.cancel_requested"
        )
    )
    if blocked_by_terminal_or_priority:
        return transition

    if event.event_type == "run.cancel_requested":
        recovery_reason = _derive_cancel_reason(event=event, fallback=projection.recovery_reason)
        transition = ("aborted", False, False, "abort", recovery_reason)
    elif event.event_type == "reconcile.failed":
        recovery_reason = _derive_recovery_reason(event=event, fallback=projection.recovery_reason)
        transition = ("waiting_external", True, False, "human_escalation", recovery_reason)
    else:
        static_transition = _STATIC_EVENT_TRANSITIONS.get(event.event_type)
        if static_transition is not None:
            recovery_reason = _derive_recovery_reason(
                event=event,
                fallback=projection.recovery_reason,
            )
            transition = (*static_transition, recovery_reason)
        else:
            dynamic_transition = _resolve_dynamic_transition(event.event_type)
            if dynamic_transition is not None:
                transition = (*dynamic_transition, projection.recovery_reason)

    return transition


def _projection_unchanged_state(
    projection: RunProjection,
) -> tuple[str, bool, bool, str | None, str | None]:
    """Returns tuple that preserves current projection fields unchanged."""
    return (
        projection.lifecycle_state,
        projection.waiting_external,
        projection.ready_for_dispatch,
        projection.recovery_mode,
        projection.recovery_reason,
    )


def _resolve_dynamic_transition(event_type: str) -> tuple[str, bool, bool, str | None] | None:
    """Resolves transition for dynamic event families."""
    # Child lifecycle propagation uses signal envelopes in current workflow wiring.
    # These signals should only update child-run tracking and must not force
    # readiness transitions on the parent run.
    if event_type in ("signal.child_spawned", "signal.child_completed"):
        return None
    if event_type.startswith("signal."):
        return ("ready", False, True, None)
    return None


def _is_lower_priority_operational_conflict(
    projection: RunProjection,
    event: RuntimeEvent,
) -> bool:
    """Returns whether incoming operational class loses to existing projection class.

    v6.4 operational precedence is explicit and deterministic:
    ``Cancel > HardFailure > Timeout > ExternalCallback``.
    """
    current_priority = _projection_operational_priority(projection)
    incoming_priority = _event_operational_priority(event.event_type)
    if current_priority is None or incoming_priority is None:
        return False
    return _OPERATIONAL_PRIORITY_RANK[incoming_priority] < _OPERATIONAL_PRIORITY_RANK[
        current_priority
    ]


def _projection_operational_priority(
    projection: RunProjection,
) -> str | None:
    """Returns current projection operational class used for conflict resolution."""
    if projection.lifecycle_state == "aborted":
        if _is_cancel_reason(projection.recovery_reason):
            return "cancel"
        return "hard_failure"
    if projection.lifecycle_state == "waiting_external":
        return "timeout"
    return None


def _event_operational_priority(event_type: str) -> str | None:
    """Returns operational class for an incoming event type."""
    if event_type == "run.cancel_requested":
        return "cancel"
    if event_type in ("run.recovery_aborted", "run.aborted"):
        return "hard_failure"
    if event_type in ("run.waiting_external", "reconcile.failed"):
        return "timeout"
    if event_type in ("signal.external_callback", "signal.callback"):
        return "external_callback"
    return None


def _is_cancel_reason(reason: str | None) -> bool:
    """Returns whether a recovery reason represents cancellation semantics."""
    if not reason:
        return False
    return "cancel" in reason.lower()


def _resolve_active_child_runs(projection: RunProjection, event: RuntimeEvent) -> list[str]:
    """Resolves active child runs for one event.

    Child run tracking is modeled as a simple set-like list on projection:
      - Spawn events add ``child_run_id`` if present and not already tracked.
      - Completion events remove ``child_run_id`` if present.

    The event taxonomy includes both ``run.*`` and ``signal.*`` child event
    names for backward compatibility with current workflow signal mapping.
    """
    if projection.lifecycle_state in ("aborted", "completed"):
        return list(projection.active_child_runs)

    child_run_id = _extract_child_run_id(event)
    if child_run_id is None:
        return list(projection.active_child_runs)

    if event.event_type in ("run.child_spawned", "signal.child_spawned"):
        if child_run_id in projection.active_child_runs:
            return list(projection.active_child_runs)
        return [*projection.active_child_runs, child_run_id]
    if event.event_type in ("run.child_completed", "signal.child_completed"):
        return [run_id for run_id in projection.active_child_runs if run_id != child_run_id]
    return list(projection.active_child_runs)


def _extract_child_run_id(event: RuntimeEvent) -> str | None:
    """Extracts child run id from event payload when present and valid."""
    payload = event.payload_json or {}
    child_run_id = payload.get("child_run_id")
    if isinstance(child_run_id, str) and child_run_id:
        return child_run_id
    return None


def _derive_recovery_reason(event: RuntimeEvent, fallback: str | None) -> str | None:
    """Extracts recovery reason from event payload when available."""
    payload = event.payload_json or {}
    reason_value = payload.get("reason")
    if isinstance(reason_value, str):
        return reason_value
    if event.event_type in ("run.ready", "run.completed", "run.created"):
        return None
    return fallback


def _derive_cancel_reason(event: RuntimeEvent, fallback: str | None) -> str:
    """Derives cancellation reason with deterministic fallback marker."""
    recovery_reason = _derive_recovery_reason(event=event, fallback=fallback)
    if recovery_reason is not None:
        return recovery_reason
    return "cancel_requested"


def _resolve_activity_execution_route(action: Action) -> ActivityExecutionRoute:
    """Resolves whether an action should execute via tool or MCP activity path."""
    payload = action.input_json or {}
    if isinstance(payload.get("mcp"), dict):
        return "mcp"
    if action.action_type == "mcp" or action.action_type.startswith("mcp."):
        return "mcp"
    return "tool"


def _build_tool_activity_input(action: Action) -> ToolActivityInput:
    """Builds ToolActivityInput from action contract with deterministic fallbacks."""
    payload = action.input_json or {}
    tool_name = _first_non_empty_string(payload.get("tool_name"), payload.get("name"))
    if tool_name is None:
        tool_name = action.action_type

    arguments = payload.get("arguments")
    if not isinstance(arguments, dict):
        arguments = payload

    return ToolActivityInput(
        run_id=action.run_id,
        action_id=action.action_id,
        tool_name=tool_name,
        arguments=arguments,
    )


def _build_mcp_activity_input(action: Action) -> MCPActivityInput:
    """Builds MCPActivityInput from action payload with explicit defaulting."""
    payload = action.input_json or {}
    mcp_payload = payload.get("mcp")
    mcp_dict = mcp_payload if isinstance(mcp_payload, dict) else {}

    server_name = _first_non_empty_string(
        mcp_dict.get("server_name"),
        mcp_dict.get("server_id"),
        payload.get("server_name"),
        payload.get("server_id"),
    )
    if server_name is None:
        server_name = "default_mcp_server"

    operation = _first_non_empty_string(
        mcp_dict.get("operation"),
        mcp_dict.get("capability_id"),
        mcp_dict.get("name"),
        payload.get("operation"),
        payload.get("capability_id"),
    )
    if operation is None:
        operation = action.action_type

    arguments = mcp_dict.get("arguments")
    if not isinstance(arguments, dict):
        fallback_arguments = payload.get("arguments")
        arguments = fallback_arguments if isinstance(fallback_arguments, dict) else {}

    return MCPActivityInput(
        run_id=action.run_id,
        action_id=action.action_id,
        server_name=server_name,
        operation=operation,
        arguments=arguments,
    )


def _first_non_empty_string(*values: Any) -> str | None:
    """Returns the first non-empty string from candidate values."""
    for value in values:
        if isinstance(value, str) and value:
            return value
    return None


_STATIC_EVENT_TRANSITIONS: dict[str, tuple[str, bool, bool, str | None]] = {
    "run.created": ("created", False, False, None),
    "run.ready": ("ready", False, True, None),
    "run.resume_requested": ("recovering", False, False, "static_compensation"),
    # ``run.cancel_requested`` is an authoritative lifecycle request fact:
    # projection commits the terminal ``aborted`` transition here so cancel
    # semantics stay replay-safe and consistent with other run.* transitions.
    "run.cancel_requested": ("aborted", False, False, "abort"),
    "run.recovery_succeeded": ("ready", False, True, None),
    "run.recovery_aborted": ("aborted", False, False, "abort"),
    "run.waiting_external": ("waiting_external", True, False, "human_escalation"),
    "run.dispatching": ("dispatching", False, True, None),
    "run.recovering": ("recovering", False, False, "static_compensation"),
    "run.completed": ("completed", False, False, None),
    "run.aborted": ("aborted", False, False, "abort"),
}

_OPERATIONAL_PRIORITY_RANK: dict[str, int] = {
    "external_callback": 1,
    "timeout": 2,
    "hard_failure": 3,
    "cancel": 4,
}
