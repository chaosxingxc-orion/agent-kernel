"""Defines the core contracts for the agent_kernel Temporal kernel PoC.

This module mirrors the refreshed architecture documents and intentionally
contains only typed contracts plus placeholder interfaces. The goal of this
stage is to let the test suite define the required behavior before the runtime
implementation is introduced.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from agent_kernel.kernel.capability_snapshot import CapabilitySnapshot

RunLifecycleState = Literal[
    "created",
    "ready",
    "dispatching",
    "waiting_result",
    "waiting_external",
    "recovering",
    "completed",
    "failed",
    "aborted",
]


class EffectClass(StrEnum):
    """Classification of an action's effect on external state."""

    READ_ONLY = "read_only"
    IDEMPOTENT_WRITE = "idempotent_write"
    COMPENSATABLE_WRITE = "compensatable_write"
    IRREVERSIBLE_WRITE = "irreversible_write"


ExternalIdempotencyLevel = Literal["guaranteed", "best_effort", "unknown"]
RecoveryMode = Literal["static_compensation", "human_escalation", "abort", "reflect_and_retry"]
CancellationPolicy = Literal["abandon", "compensate_then_continue"]

InteractionTarget = Literal[
    "agent_peer",  # another agent kernel: A2A or any peer-agent protocol
    "it_service",  # traditional IT: REST / gRPC / GraphQL / enterprise system
    "data_system",  # database, vector store, data lake, streaming platform
    "tool_executor",  # MCP, function call, CLI, sandbox, code execution
    "human_actor",  # approval gate, feedback loop, human escalation
    "event_stream",  # Kafka, Redis Streams, pub/sub, message queue
]


class SideEffectClass(StrEnum):
    """Classification of an action's side-effect scope."""

    READ_ONLY = "read_only"
    LOCAL_WRITE = "local_write"
    EXTERNAL_WRITE = "external_write"
    IRREVERSIBLE_SUBMIT = "irreversible_submit"


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    """Represents one append-only kernel runtime event.

    The agent_kernel architecture treats runtime events as the
    only authoritative kernel truth for domain state changes.
    These fields therefore intentionally track ordering,
    wake-up semantics, and replay-safe payload references.

    Attributes:
        run_id: Run identifier that owns this event.
        event_id: Unique event identifier within the run.
        commit_offset: Monotonic commit offset for ordering.
        event_type: Domain event type discriminator.
        event_class: Event classification for authority tracking.
        event_authority: Authority level for replay and audit.
        ordering_key: Deterministic ordering key within the commit.
        wake_policy: Whether this event wakes the actor.
        created_at: RFC3339 UTC creation timestamp.
        idempotency_key: Optional idempotency key for dedup.
        payload_ref: Optional reference to stored payload.
        payload_json: Optional inline event payload.

    """

    run_id: str
    event_id: str
    commit_offset: int
    event_type: str
    event_class: Literal["fact", "derived"]
    event_authority: Literal[
        "authoritative_fact",
        "derived_replayable",
        "derived_diagnostic",
    ]
    ordering_key: str
    wake_policy: Literal["wake_actor", "projection_only"]
    created_at: str
    idempotency_key: str | None = None
    payload_ref: str | None = None
    payload_json: dict[str, Any] | None = None
    schema_version: str = "1"


@dataclass(frozen=True, slots=True)
class Action:
    """Represents one dispatchable unit selected by the actor.

    Actions are resolved by the decision path, checked by
    admission, and then executed by the executor. The contract
    keeps effect semantics explicit so the runtime can enforce
    side-effect governance.

    Attributes:
        action_id: Unique action identifier.
        run_id: Run identifier that owns this action.
        action_type: Action type discriminator.
        effect_class: Declared side-effect class.
        external_idempotency_level: Optional external idempotency guarantee.
        interaction_target: Optional classification of the external target being
            contacted.  Orthogonal to ``host_kind`` (which describes execution
            *mechanism*); this describes *who* the agent is talking to.
            Enables routing, policy, and observability by target category.
        input_ref: Optional input reference string.
        input_json: Optional input payload dictionary.
        policy_tags: Policy tags for dispatch routing hints.
        timeout_ms: Optional execution timeout in milliseconds.

    """

    action_id: str
    run_id: str
    action_type: str
    effect_class: EffectClass
    external_idempotency_level: ExternalIdempotencyLevel | None = None
    interaction_target: InteractionTarget | None = None
    input_ref: str | None = None
    input_json: dict[str, Any] | None = None
    policy_tags: list[str] = field(default_factory=list)
    timeout_ms: int | None = None
    side_effect_class: SideEffectClass | None = None
    """Blast-radius governance dimension (TRACE 搂6.8).
    Orthogonal to effect_class which governs idempotency/recovery."""


# Type alias used by execute_turn() across substrates.
# A handler receives (action, sandbox_grant) and returns any result.
AsyncActionHandler = Callable[["Action", str | None], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class SandboxGrant:
    """Represents a sandbox authorization returned by admission.

    Attributes:
        grant_ref: Stable grant identifier.
        host_kind: Authorized host kind for execution.
        sandbox_profile_ref: Optional sandbox profile reference.
        allowed_mounts: Allowed mount paths.
        denied_mounts: Denied mount paths.
        network_policy: Outbound network policy mode.
        allowed_hosts: Explicitly allowed outbound hosts.

    """

    grant_ref: str
    host_kind: Literal[
        "local_process",
        "local_cli",
        "cli_process",
        "in_process_python",
        "remote_service",
    ]
    sandbox_profile_ref: str | None = None
    allowed_mounts: list[str] = field(default_factory=list)
    denied_mounts: list[str] = field(default_factory=list)
    network_policy: Literal["deny_all", "allow_list", "allow_all"] = "deny_all"
    allowed_hosts: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """Carries non-authority execution context for dispatch and recovery.

    Attributes:
        run_id: Run identifier.
        action_id: Action identifier.
        causation_id: Optional causation chain identifier.
        correlation_id: Optional correlation identifier.
        lineage_id: Optional lineage identifier.
        capability_snapshot_ref: Snapshot reference.
        capability_snapshot_hash: Snapshot hash.
        context_binding_ref: Optional context binding reference.
        grant_ref: Optional admission grant reference.
        policy_snapshot_ref: Optional policy snapshot reference.
        rule_bundle_hash: Optional rule bundle hash.
        declarative_bundle_digest: Optional declarative bundle digest.
        timeout_ms: Optional timeout value in milliseconds.
        budget_ref: Optional budget reference.
        trace_context: W3C ``traceparent`` header value (e.g.
            ``"00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"``).
            When present, observability backends restore the parent span so that
            cross-service calls join the originating distributed trace.

    """

    run_id: str
    action_id: str
    causation_id: str | None = None
    correlation_id: str | None = None
    lineage_id: str | None = None
    capability_snapshot_ref: str | None = None
    capability_snapshot_hash: str | None = None
    context_binding_ref: str | None = None
    grant_ref: str | None = None
    policy_snapshot_ref: str | None = None
    rule_bundle_hash: str | None = None
    declarative_bundle_digest: dict[str, str] | None = None
    timeout_ms: int | None = None
    budget_ref: str | None = None
    trace_context: str | None = None


@dataclass(frozen=True, slots=True)
class ActionCommit:
    """Represents the action-level event commit boundary.

    The architecture requires all events produced by a single action to enter
    the runtime event log under one explicit commit boundary.

    Attributes:
        run_id: Run identifier that owns this commit.
        commit_id: Unique commit identifier.
        events: Ordered list of runtime events in this commit.
        created_at: RFC3339 UTC creation timestamp.
        action: Optional action that produced this commit.
        caused_by: Optional causal reference for provenance tracing.

    """

    run_id: str
    commit_id: str
    events: list[RuntimeEvent]
    created_at: str
    action: Action | None = None
    caused_by: str | None = None


@dataclass(frozen=True, slots=True)
class RunPolicyVersions:
    """Policy versions frozen at run creation for TRACE policy version pinning.

    hi-agent provides these at start_run time.  agent-kernel freezes them
    into the run metadata so waiting runs resume under the same policies
    that were active when the run was created (TRACE arbitration Rule A3).
    """

    route_policy_version: str | None = None
    acceptance_policy_version: str | None = None
    memory_policy_version: str | None = None
    skill_policy_version: str | None = None
    evaluation_policy_version: str | None = None
    task_view_policy_version: str | None = None
    pinned_at: str = ""


@dataclass(frozen=True, slots=True)
class RunProjection:
    """Represents the minimal authoritative decision projection.

    The actor is only allowed to make decisions based on
    projection state. This object therefore captures the minimum
    fields needed for safe dispatch.
    """

    run_id: str
    lifecycle_state: RunLifecycleState
    projected_offset: int
    waiting_external: bool
    ready_for_dispatch: bool
    current_action_id: str | None = None
    recovery_mode: RecoveryMode | None = None
    recovery_reason: str | None = None
    active_child_runs: list[str] = field(default_factory=list)
    policy_versions: RunPolicyVersions | None = None
    task_contract_ref: str | None = None


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    """Represents the executor admission decision.

    Admission is the only allowed execution gate before any external side
    effect. The result carries both the decision and the references needed to
    prove what policy snapshot authorized the action.
    """

    admitted: bool
    reason_code: Literal[
        "ok",
        "permission_denied",
        "quota_exceeded",
        "policy_denied",
        "dependency_not_ready",
        "stale_policy",
        "idempotency_contract_insufficient",
    ]
    expires_at: str | None = None
    grant_ref: str | None = None
    policy_snapshot_ref: str | None = None
    sandbox_grant: SandboxGrant | None = None
    idempotency_envelope: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class AdmissionActivityInput:
    """Represents one admission activity invocation payload.

    The admission activity executes policy and readiness checks for one action
    under the current authoritative projection snapshot.
    """

    run_id: str
    action: Action
    projection: RunProjection


@dataclass(frozen=True, slots=True)
class ToolActivityInput:
    """Represents one tool activity invocation payload.

    Tool activities execute user-selected tool work as substrate
    side effects. The payload keeps only execution identity and
    tool arguments.
    """

    run_id: str
    action_id: str
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MCPActivityInput:
    """Represents one MCP activity invocation payload.

    MCP activities target a specific MCP server operation with
    serialized request arguments.
    """

    run_id: str
    action_id: str
    server_name: str
    operation: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class VerificationActivityInput:
    """Represents one verification activity invocation payload.

    Verification activities assert post-conditions against
    execution artifacts and return structured verification
    outcomes.
    """

    run_id: str
    action_id: str
    verification_kind: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ReconciliationActivityInput:
    """Represents one reconciliation activity invocation payload.

    Reconciliation activities align observed external state with
    expected kernel state after tool or MCP execution.
    """

    run_id: str
    action_id: str
    expected_state: dict[str, Any] = field(default_factory=dict)
    observed_state: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CircuitBreakerPolicy:
    """Policy governing the cross-run circuit breaker in the recovery gate.

    The circuit breaker tracks consecutive failures per ``effect_class``.
    When failures reach ``threshold``, the circuit opens and the recovery gate
    forces ``abort`` with reason ``circuit_open`` without consulting the planner.
    After ``half_open_after_ms`` of silence, one probe request is allowed
    through (half-open state); on success the counter resets (closed state).

    Attributes:
        threshold: Consecutive failures per ``effect_class`` before opening
            the circuit.  Defaults to ``5``.
        half_open_after_ms: Milliseconds after the last failure before the
            circuit transitions to half-open and allows one probe.
            Defaults to ``30 000`` (30 s).

    """

    threshold: int = 5
    half_open_after_ms: int = 30_000


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    """Represents the only allowed recovery exit from a failed action.

    Recovery decisions stay deliberately narrow so that only the Recovery gate
    can choose the next failure handling mode.

    Attributes:
        run_id: Run identifier.
        mode: Recovery mode selected.
        reason: Human-readable reason string.
        compensation_action_id: Optional compensation action identifier when
            mode is ``static_compensation``.
        escalation_channel_ref: Optional escalation channel reference when
            mode is ``human_escalation``.
        corrected_action: Optional corrected action when mode is
            ``reflect_and_retry``.
        retry_after_ms: Optional backoff delay in milliseconds before the
            caller should retry.  ``0`` means retry immediately.  ``None``
            means no retry (terminal decision or retry timing is caller's
            responsibility).
        failure_count: Monotonic count of consecutive failures for this
            run+action pair.  Used by callers to implement exponential backoff
            or circuit-break logic.  ``0`` when not tracked.

    """

    run_id: str
    mode: RecoveryMode
    reason: str
    compensation_action_id: str | None = None
    escalation_channel_ref: str | None = None
    corrected_action: Any | None = None
    retry_after_ms: int | None = None
    failure_count: int = 0


@dataclass(frozen=True, slots=True)
class RecoveryInput:
    """Represents the failure envelope handed into Recovery.

    Recovery consumes both the failure reason and the
    authoritative projection view so it can choose a safe exit
    path without reaching around the actor.

    Attributes:
        run_id: Run identifier that owns this recovery input.
        reason_code: Failure reason discriminator string.
        lifecycle_state: Run lifecycle state at failure time.
        projection: Authoritative projection snapshot at failure time.
        failed_action_id: Optional failed action identifier.
        reflection_round: Number of prior reflect-and-retry rounds for this failure.
        capability_snapshot: Optional pre-built snapshot passed from the TurnEngine.
            When provided, RecoveryGateService uses it directly for the reasoning
            loop rather than building a new one, keeping the gate within its own
            authority boundary.

    """

    run_id: str
    reason_code: str
    lifecycle_state: RunLifecycleState
    projection: RunProjection
    failed_action_id: str | None = None
    reflection_round: int = 0
    capability_snapshot: Any | None = None
    failed_effect_class: str | None = None


class TraceFailureCode(StrEnum):
    """TRACE-normalized failure taxonomy (v1.2.1 搂6.9).

    Used in FailureEnvelope.trace_failure_code for postmortem,
    route pruning, and evolution trigger routing.
    """

    MISSING_EVIDENCE = "missing_evidence"
    INVALID_CONTEXT = "invalid_context"
    HARNESS_DENIED = "harness_denied"
    MODEL_OUTPUT_INVALID = "model_output_invalid"
    MODEL_REFUSAL = "model_refusal"
    CALLBACK_TIMEOUT = "callback_timeout"
    NO_PROGRESS = "no_progress"
    CONTRADICTORY_EVIDENCE = "contradictory_evidence"
    UNSAFE_ACTION_BLOCKED = "unsafe_action_blocked"
    EXPLORATION_BUDGET_EXHAUSTED = "exploration_budget_exhausted"
    """CTS exploration budget 鈥?hi-agent decides, kernel reports the signal."""
    EXECUTION_BUDGET_EXHAUSTED = "execution_budget_exhausted"
    """Kernel-owned execution/runtime/timeout budget 鈥?kernel reports directly."""

    @classmethod
    def is_budget_exhausted(cls, code: TraceFailureCode) -> bool:
        """Return True for any budget-exhaustion failure code."""
        return code in (cls.EXPLORATION_BUDGET_EXHAUSTED, cls.EXECUTION_BUDGET_EXHAUSTED)


@dataclass(frozen=True, slots=True)
class FailureEnvelope:
    """Represents v6.4 failure evidence payload consumed by Recovery.

    The envelope keeps failure stage, classification, and evidence references in
    one immutable object so recovery policy can be deterministic and auditable.
    Evidence resolution follows v6.4 precedence:
    ``external_ack_ref > evidence_ref > local_inference``.
    """

    run_id: str
    action_id: str | None
    failed_stage: Literal[
        "admission",
        "execution",
        "verification",
        "reconciliation",
        "callback",
    ]
    failed_component: str
    failure_code: str
    failure_class: Literal[
        "deterministic",
        "transient",
        "policy",
        "side_effect",
        "unknown",
    ]
    evidence_ref: str | None = None
    external_ack_ref: str | None = None
    local_inference: str | None = None
    evidence_priority_source: Literal[
        "external_ack_ref",
        "evidence_ref",
        "local_inference",
        "none",
    ] = "none"
    evidence_priority_ref: str | None = None
    retryability: Literal["retryable", "non_retryable", "unknown"] = "unknown"
    compensation_hint: str | None = None
    human_escalation_hint: str | None = None
    trace_failure_code: TraceFailureCode | None = None
    """TRACE-normalized failure code.  Set by RecoveryPlanner when writing
    RecoveryOutcome.  Enables hi-agent to route pruning and evolve triggers
    without parsing kernel-internal failure_code strings."""


@dataclass(frozen=True, slots=True)
class RecoveryOutcome:
    """Represents persisted outcome after recovery action execution/escalation.

    Attributes:
        run_id: Run identifier that owns this outcome.
        action_id: Optional action identifier for compensation tracking.
        recovery_mode: Recovery mode applied.
        outcome_state: Final state of the recovery action.
        written_at: RFC3339 UTC persistence timestamp.
        operator_escalation_ref: Optional escalation channel reference.
        emitted_event_ids: Ordered list of emitted event identifiers.

    """

    run_id: str
    action_id: str | None
    recovery_mode: RecoveryMode
    outcome_state: Literal["executed", "scheduled", "escalated", "aborted", "reflected"]
    written_at: str
    operator_escalation_ref: str | None = None
    emitted_event_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class TurnIntentRecord:
    """Represents persisted turn intent metadata for replay/resume recovery.

    Attributes:
        run_id: Run identifier that owns the turn intent.
        intent_commit_ref: Intent commit reference from TurnEngine.
        decision_ref: Deterministic decision reference.
        decision_fingerprint: Deterministic decision fingerprint.
        dispatch_dedupe_key: Optional dispatch dedupe key.
        host_kind: Optional resolved host kind.
        outcome_kind: Turn outcome kind discriminator.
        written_at: RFC3339 UTC persistence timestamp.

    """

    run_id: str
    intent_commit_ref: str
    decision_ref: str
    decision_fingerprint: str
    dispatch_dedupe_key: str | None
    host_kind: str | None
    outcome_kind: str
    written_at: str
    reflection_round: int = 0


@dataclass(frozen=True, slots=True)
class RemoteServiceIdempotencyContract:
    """Represents remote-service idempotency/authentication capability contract.

    Attributes:
        accepts_dispatch_idempotency_key: Whether the remote service accepts
            dispatch idempotency keys for deduplication.
        returns_stable_ack: Whether the remote service returns stable
            acknowledgement references.
        peer_retry_model: Remote service retry model.
        default_retry_policy: Default retry policy for the remote dispatch.

    """

    accepts_dispatch_idempotency_key: bool
    returns_stable_ack: bool
    peer_retry_model: Literal[
        "unknown",
        "at_most_once",
        "at_least_once",
        "exactly_once_claimed",
    ]
    default_retry_policy: Literal["no_auto_retry", "bounded_retry"]


@dataclass(frozen=True, slots=True)
class StartRunRequest:
    """Represents the platform-facing request to start a run.

    Attributes:
        initiator: Run initiator discriminator.
        run_kind: Logical run kind string.
        session_id: Optional session identifier for binding.
        input_ref: Optional input reference string.
        input_json: Optional input payload dictionary.
        context_ref: Optional context binding reference.
        parent_run_id: Optional parent run identifier for causation.
        trace_context: W3C ``traceparent`` header value propagated from the
            caller's trace context.  Injected into ``ExecutionContext`` for all
            downstream Temporal activities so the full request chain appears as
            a single distributed trace.

    """

    initiator: Literal["user", "agent_core_runner", "system"]
    run_kind: str
    session_id: str | None = None
    input_ref: str | None = None
    input_json: dict[str, Any] | None = None
    context_ref: str | None = None
    parent_run_id: str | None = None
    trace_context: str | None = None
    task_contract_ref: str | None = None
    initial_stage_id: str | None = None
    route_policy_version: str | None = None
    skill_policy_version: str | None = None
    evaluation_policy_version: str | None = None
    task_view_policy_version: str | None = None


@dataclass(frozen=True, slots=True)
class StartRunResponse:
    """Represents the facade-safe response returned to the platform.

    Attributes:
        run_id: Kernel run identifier.
        temporal_workflow_id: Temporal workflow id for the started run.
        lifecycle_state: Initial lifecycle state of the run.

    """

    run_id: str
    temporal_workflow_id: str
    lifecycle_state: RunLifecycleState


@dataclass(frozen=True, slots=True)
class SignalRunRequest:
    """Represents one external signal routed back into a run.

    Attributes:
        run_id: Target run identifier.
        signal_type: Signal type discriminator.
        signal_payload: Optional signal payload dictionary.

    """

    run_id: str
    signal_type: str
    signal_payload: dict[str, Any] | None = None
    caused_by: str | None = None


@dataclass(frozen=True, slots=True)
class CancelRunRequest:
    """Represents one run cancellation request from the platform."""

    run_id: str
    reason: str
    caused_by: str | None = None


@dataclass(frozen=True, slots=True)
class ResumeRunRequest:
    """Represents one run resume request routed through checkpoint mapping."""

    run_id: str
    snapshot_id: str | None = None
    caused_by: str | None = None


@dataclass(frozen=True, slots=True)
class QueryRunRequest:
    """Represents a projection query request."""

    run_id: str


@dataclass(frozen=True, slots=True)
class QueryRunResponse:
    """Represents the projection view exported from the facade."""

    run_id: str
    lifecycle_state: RunLifecycleState
    projected_offset: int
    waiting_external: bool
    current_action_id: str | None = None
    recovery_mode: RecoveryMode | None = None
    recovery_reason: str | None = None
    active_child_runs: list[str] = field(default_factory=list)
    policy_versions: RunPolicyVersions | None = None
    active_stage_id: str | None = None


@dataclass(frozen=True, slots=True)
class QueryRunDashboardResponse:
    """Represents a dashboard-oriented run read model."""

    run_id: str
    lifecycle_state: RunLifecycleState
    projected_offset: int
    waiting_external: bool
    recovery_mode: RecoveryMode | None = None
    recovery_reason: str | None = None
    active_child_runs_count: int = 0
    correlation_hint: str = ""


@dataclass(frozen=True, slots=True)
class SpawnChildRunRequest:
    """Represents the request to create a child run."""

    parent_run_id: str
    child_kind: str
    input_ref: str | None = None
    input_json: dict[str, Any] | None = None
    context_ref: str | None = None  # Inherited from parent run by KernelFacade
    task_id: str | None = None
    """Bind child run to a task registry entry (e.g. plan_step, parallel_branch)."""
    inherit_policy_versions: bool = True
    """Inherit parent's RunPolicyVersions into child.  Set False to use defaults."""
    policy_version_overrides: dict[str, str] | None = None
    """Selective overrides applied on top of inherited policy versions."""
    notify_parent_on_complete: bool = True
    """Signal parent run when child reaches a terminal state."""


@dataclass(frozen=True, slots=True)
class SpawnChildRunResponse:
    """Represents the facade-safe child run response."""

    child_run_id: str
    lifecycle_state: RunLifecycleState


class TemporalWorkflowGateway(Protocol):
    """Abstracts Temporal as a substrate, not as business truth.

    The runtime uses this gateway to access durable execution primitives while
    keeping Temporal-specific handles and SDK objects outside kernel contracts.
    """

    async def start_workflow(self, request: StartRunRequest) -> dict[str, str]:
        """Start one durable workflow for a run.

        Args:
            request: Kernel-safe run start request.

        Returns:
            A minimal substrate response containing a workflow identifier.

        """
        ...

    async def signal_workflow(self, run_id: str, signal: SignalRunRequest) -> None:
        """Send one signal into a running workflow.

        Args:
            run_id: Run identifier mapped to the workflow.
            signal: Signal payload to deliver.

        """
        ...

    async def cancel_workflow(self, run_id: str, reason: str) -> None:
        """Cancel one workflow by run identifier.

        Args:
            run_id: Run identifier mapped to the workflow.
            reason: Cancellation reason string.

        """
        ...

    async def query_projection(self, run_id: str) -> RunProjection:
        """Query the current authoritative run projection.

        Args:
            run_id: Run identifier to query.

        Returns:
            The current authoritative projection.

        """
        ...

    async def start_child_workflow(
        self,
        parent_run_id: str,
        request: SpawnChildRunRequest,
    ) -> dict[str, str]:
        """Start one durable child workflow.

        Args:
            parent_run_id: Parent run identifier.
            request: Child run request payload.

        Returns:
            A minimal substrate response containing a workflow identifier.

        """
        ...

    def stream_run_events(self, run_id: str) -> AsyncIterator[RuntimeEvent]:
        """Streams runtime events for one run as an async iterator.

        Args:
            run_id: Run identifier whose event stream should be consumed.

        Returns:
            Async iterator of runtime events in substrate-observed order.

        """
        ...


class TemporalActivityGateway(Protocol):
    """Abstracts Temporal activity execution as a substrate adapter.

    The adapter receives kernel-safe DTOs and delegates to activity callables.
    Business semantics remain in kernel services and contracts, not in adapter
    orchestration logic.
    """

    async def execute_admission(self, request: AdmissionActivityInput) -> AdmissionResult:
        """Execute one admission activity.

        Args:
            request: Admission activity input payload.

        Returns:
            Admission decision result.

        """
        ...

    async def execute_tool(self, request: ToolActivityInput) -> Any:
        """Execute one tool activity.

        Args:
            request: Tool activity input payload.

        Returns:
            Tool execution result.

        """
        ...

    async def execute_mcp(self, request: MCPActivityInput) -> Any:
        """Execute one MCP activity.

        Args:
            request: MCP activity input payload.

        Returns:
            MCP execution result.

        """
        ...

    async def execute_verification(
        self,
        request: VerificationActivityInput,
    ) -> Any:
        """Execute one verification activity.

        Args:
            request: Verification activity input payload.

        Returns:
            Verification result.

        """
        ...

    async def execute_reconciliation(self, request: ReconciliationActivityInput) -> Any:
        """Execute one reconciliation activity.

        Args:
            request: Reconciliation activity input payload.

        Returns:
            Reconciliation result.

        """
        ...

    async def execute_inference(self, request: InferenceActivityInput) -> ModelOutput:
        """Execute one LLM inference activity.

        Args:
            request: Inference activity input payload.

        Returns:
            Normalised model output.

        """
        ...

    async def execute_skill_script(self, request: ScriptActivityInput) -> ScriptResult:
        """Execute one skill script activity.

        Args:
            request: Script activity input payload.

        Returns:
            Script execution result.

        """
        ...


class KernelRuntimeEventLog(Protocol):
    """Abstracts the authoritative domain event log."""

    async def append_action_commit(self, commit: ActionCommit) -> str:
        """Append one action-level commit to the event log.

        Args:
            commit: Commit to append.

        Returns:
            Commit reference identifier.

        """
        ...

    async def load(
        self,
        run_id: str,
        after_offset: int = 0,
    ) -> list[RuntimeEvent]:
        """Load events for a run after a given offset.

        Args:
            run_id: Run identifier.
            after_offset: Exclusive lower bound offset.

        Returns:
            Ordered list of runtime events.

        """
        ...


class DecisionProjectionService(Protocol):
    """Abstracts the minimal authoritative decision projection."""

    async def catch_up(self, run_id: str, through_offset: int) -> RunProjection:
        """Catches up projection state through a target offset.

        Args:
            run_id: Run identifier to catch up.
            through_offset: Target offset to replay through.

        Returns:
            Updated projection at or past ``through_offset``.

        """
        ...

    async def readiness(self, run_id: str, required_offset: int) -> bool:
        """Check whether projection has reached a required offset.

        Args:
            run_id: Run identifier to check.
            required_offset: Minimum required offset.

        Returns:
            ``True`` when projection has reached the required offset.

        """
        ...

    async def get(self, run_id: str) -> RunProjection:
        """Get the latest projection state for a run.

        Args:
            run_id: Run identifier to look up.

        Returns:
            Current authoritative projection for the run.

        """
        ...


class DispatchAdmissionService(Protocol):
    """Abstracts dispatch-time admission checks.

    All implementations must provide ``admit(action, snapshot)``.  The older
    ``check(action, projection)`` interface is intentionally absent from this
    Protocol; it is retained on ``StaticDispatchAdmissionService`` only for
    backward compatibility with existing callers that have not yet migrated.
    """

    async def admit(
        self,
        action: Action,
        snapshot: CapabilitySnapshot,
    ) -> AdmissionResult:
        """Evaluate whether an action may execute under capability snapshot.

        Args:
            action: Candidate action to evaluate.
            snapshot: Frozen capability snapshot used for policy checks.

        Returns:
            Admission result with deny reason or granted admission.

        """
        ...


class ExecutorService(Protocol):
    """Abstracts the executor service."""

    async def execute(
        self,
        action: Action,
        grant_ref: str | None = None,
    ) -> Any:
        """Execute one admitted action.

        Args:
            action: Admitted action to execute.
            grant_ref: Optional grant reference from admission.

        Returns:
            Execution result from the action handler.

        """
        ...


class TransientExecutionError(Exception):
    """Base class for retryable transient execution failures.

    Subclasses can attach metadata consumed by ``RetryingExecutorService`` to
    adapt retry delay and idempotent replay behavior.

    Attributes:
        backoff_hint_ms: Optional minimum backoff hint in milliseconds.
        may_have_executed: Whether the action may have partially executed.

    """

    def __init__(
        self,
        message: str = "",
        *,
        backoff_hint_ms: int | None = None,
        may_have_executed: bool = False,
    ) -> None:
        """Initialize transient execution error metadata."""
        super().__init__(message)
        self.backoff_hint_ms = backoff_hint_ms
        self.may_have_executed = may_have_executed


class ConnectionTransientError(TransientExecutionError):
    """Connection-level transient failure (DNS/TCP/TLS).

    The request did not reach the remote service.
    """

    def __init__(self, message: str = "", **kwargs: Any) -> None:
        """Initialize connection-transient error with safe defaults."""
        super().__init__(message, may_have_executed=False, **kwargs)


class TimeoutTransientError(TransientExecutionError):
    """Request timeout transient failure.

    Timeout can mean the remote service may have executed the action.
    """

    def __init__(self, message: str = "", **kwargs: Any) -> None:
        """Initialize timeout-transient error with replay-aware defaults."""
        super().__init__(message, may_have_executed=True, **kwargs)


class RateLimitTransientError(TransientExecutionError):
    """Rate-limit transient failure (e.g. HTTP 429).

    Attributes:
        retry_after_ms: Server-provided retry window, when available.

    """

    def __init__(
        self,
        message: str = "",
        *,
        retry_after_ms: int | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize rate-limit transient error with retry-after metadata."""
        hint = retry_after_ms or kwargs.pop("backoff_hint_ms", None)
        super().__init__(
            message,
            backoff_hint_ms=hint,
            may_have_executed=False,
            **kwargs,
        )
        self.retry_after_ms = retry_after_ms


class ServiceOverloadTransientError(TransientExecutionError):
    """Service-overload transient failure (e.g. HTTP 503)."""

    def __init__(self, message: str = "", **kwargs: Any) -> None:
        """Initialize service-overload transient error defaults."""
        super().__init__(message, may_have_executed=False, **kwargs)


class CircuitBreakerStore(Protocol):
    """Persistent state store for the cross-run circuit breaker in RecoveryGate.

    Implementations must be thread-safe and may be backed by any durable store
    (SQLite, Redis, etc.).  Concrete implementations should document their
    consistency guarantees.
    """

    def get_state(self, effect_class: str) -> tuple[int, float]:
        """Return ``(failure_count, last_failure_epoch_s)`` for ``effect_class``.

        Args:
            effect_class: The action effect class to query.

        Returns:
            A tuple of ``(failure_count, last_failure_epoch_s)`` where
            ``last_failure_epoch_s`` is a Unix epoch float (0.0 when unknown).

        """
        ...

    def record_failure(self, effect_class: str) -> int:
        """Increments failure count and records the current time.

        Args:
            effect_class: The action effect class that just failed.

        Returns:
            The new failure count after incrementing.

        """
        ...

    def reset(self, effect_class: str) -> None:
        """Clear all failure state for ``effect_class`` after a success.

        Args:
            effect_class: The action effect class that just succeeded.

        """
        ...


class RecoveryGateService(Protocol):
    """Abstracts the recovery gate."""

    async def decide(self, recovery_input: RecoveryInput) -> RecoveryDecision:
        """Select a recovery exit for one failure envelope.

        Args:
            recovery_input: Failure envelope and projection context.

        Returns:
            Recovery decision with mode and optional compensation or escalation.

        """
        ...


class RecoveryOutcomeStore(Protocol):
    """Abstracts persistence for recovery outcomes."""

    async def write_outcome(self, outcome: RecoveryOutcome) -> None:
        """Persist one recovery outcome record.

        Args:
            outcome: Recovery outcome to persist.

        """
        ...

    async def latest_for_run(self, run_id: str) -> RecoveryOutcome | None:
        """Return latest recovery outcome for one run.

        Args:
            run_id: Run identifier to look up.

        Returns:
            Latest recovery outcome, or ``None`` if no outcome exists.

        """
        ...


class TurnIntentLog(Protocol):
    """Abstracts persistence of turn intent commit metadata."""

    async def write_intent(self, intent: TurnIntentRecord) -> None:
        """Persist one turn intent record.

        Args:
            intent: Turn intent metadata to persist.

        """
        ...

    async def latest_for_run(self, run_id: str) -> TurnIntentRecord | None:
        """Return latest turn intent for one run.

        Args:
            run_id: Run identifier to query.

        Returns:
            Most recent turn intent record, or ``None`` when absent.

        """
        ...


class IngressAdapter(Protocol):
    """Abstracts ingress translation from platform events into kernel DTOs."""

    def from_runner_start(self, input_value: Any) -> StartRunRequest:
        """Build start-run request from runner-originated input.

        Args:
            input_value: Platform-specific input payload.

        Returns:
            StartRunRequest: A ``StartRunRequest`` ready for kernel submission.

        """
        ...

    def from_session_signal(self, input_value: Any) -> SignalRunRequest:
        """Build signal request from session-level input.

        Args:
            input_value: Platform-specific input payload.

        Returns:
            SignalRunRequest: A ``SignalRunRequest`` ready for kernel signal dispatch.

        """
        ...

    def from_callback(self, input_value: Any) -> SignalRunRequest:
        """Build signal request from callback input.

        Args:
            input_value: Platform-specific input payload.

        Returns:
            SignalRunRequest: A ``SignalRunRequest`` ready for kernel signal dispatch.

        """
        ...


class ContextBindingPort(Protocol):
    """Abstracts context binding at kernel boundary."""

    def bind_context(self, input_value: Any) -> Any:
        """Resolve runtime context binding from platform input.

        Args:
            input_value: Platform-specific input payload.

        Returns:
            Any: Platform-specific context or result object.

        """
        ...


class CheckpointResumePort(Protocol):
    """Abstracts checkpoint export and resume import at kernel boundary."""

    async def export_checkpoint(self, run_id: str) -> Any:
        """Export platform-facing checkpoint view for one run.

        Args:
            run_id: Identifier of the target run.

        Returns:
            Any: Platform-specific context or result object.

        """
        ...

    async def import_resume(self, input_value: Any) -> Any:
        """Import platform resume payload into kernel-safe request.

        Args:
            input_value: Platform-specific input payload.

        Returns:
            Any: Platform-specific context or result object.

        """
        ...


class CapabilityAdapter(Protocol):
    """Abstracts capability bindings resolution from platform metadata."""

    async def resolve_tool_bindings(self, action: Action) -> list[Any]:
        """Resolve tool bindings for one action.

        Args:
            action: The action whose tool bindings are being resolved.

        Returns:
            List of resolved tool binding descriptors.

        """
        ...

    async def resolve_mcp_bindings(self, action: Action) -> list[Any]:
        """Resolve MCP bindings for one action.

        Args:
            action: The action whose MCP bindings are being resolved.

        Returns:
            List of resolved MCP binding descriptors.

        """
        ...

    async def resolve_skill_bindings(self, action: Action) -> list[str]:
        """Resolve skill bindings for one action.

        Args:
            action: The action whose skill bindings are being resolved.

        Returns:
            List of resolved skill identifiers.

        """
        ...

    async def resolve_declarative_bundle(self, action: Action) -> dict[str, str] | None:
        """Resolve declarative bundle digest payload for one action.

        Args:
            action: The action whose declarative bundle is being resolved.

        Returns:
            Key-value digest map, or ``None`` when no bundle is declared.

        """
        ...


class DecisionDeduper(Protocol):
    """Abstracts decision fingerprint de-duplication.

    Boundary note:
      - This protocol de-duplicates *decision rounds* at workflow level.
      - It is intentionally separate from ``DedupeStorePort`` which governs
        dispatch idempotency state transitions at executor boundary.
    """

    async def seen(self, fingerprint: str) -> bool:
        """Return whether a decision fingerprint has already been processed.

        Args:
            fingerprint: Decision fingerprint to check.

        Returns:
            ``True`` if the fingerprint was previously marked.

        """
        ...

    async def mark(self, fingerprint: str) -> None:
        """Mark a decision fingerprint as processed.

        Args:
            fingerprint: Decision fingerprint to mark as seen.

        """
        ...


class EventExportPort(Protocol):
    """Platform-facing async export sink for kernel ActionCommit events.

    The kernel fires ``export_commit`` after each ``ActionCommit`` is durably
    written to the operational event log.  The call is fire-and-forget:

    - The kernel never awaits the result on the execution critical path.
    - Export failures are swallowed by the wrapper and logged at WARNING.
    - The platform may apply any TTL, indexing, or streaming strategy it
      needs without coupling to kernel internals.

    Design boundary:
      - Kernel operational log (correctness) 鈫?``KernelRuntimeEventLog``
      - Platform evolution store (analytics/training) 鈫?``EventExportPort``

    Implementations must not raise.  Use ``InMemoryRunTraceStore`` for
    development and integration tests.
    """

    async def export_commit(self, commit: ActionCommit) -> None:
        """Receives one durably-written commit for platform processing.

        Args:
            commit: The ``ActionCommit`` that was just appended to the
                kernel event log.  The commit is immutable and safe to
                retain across async boundaries.

        """
        ...


class ObservabilityHook(Protocol):
    """Receives FSM state transition events for observability purposes.

    Implementations must be synchronous and fast. The hook is called
    on the hot path of every FSM state transition 鈥?slow or blocking
    implementations will add latency to every turn.

    Use ``CompositeObservabilityHook`` to fan-out to multiple backends.
    Use ``NoOpObservabilityHook`` as the zero-cost default.
    """

    def on_turn_state_transition(
        self,
        *,
        run_id: str,
        action_id: str,
        from_state: str,
        to_state: str,
        turn_offset: int,
        timestamp_ms: int,
    ) -> None:
        """Call on every TurnEngine FSM state transition.

        Args:
            run_id: Run identifier.
            action_id: Action/turn identifier.
            from_state: Previous FSM state.
            to_state: New FSM state.
            turn_offset: Monotonic turn offset.
            timestamp_ms: UTC epoch milliseconds.

        """
        ...

    def on_run_lifecycle_transition(
        self,
        *,
        run_id: str,
        from_state: str,
        to_state: str,
        timestamp_ms: int,
    ) -> None:
        """Call on every run lifecycle state transition.

        Args:
            run_id: Run identifier.
            from_state: Previous lifecycle state.
            to_state: New lifecycle state.
            timestamp_ms: UTC epoch milliseconds.

        """
        ...

    def on_llm_call(
        self,
        *,
        run_id: str,
        model_ref: str,
        latency_ms: int,
        token_usage: TokenUsage | None,
    ) -> None:
        """Call after each LLM inference call completes.

        Implementations should record latency histograms and token counters.

        Args:
            run_id: Run identifier.
            model_ref: Provider-qualified model identifier.
            latency_ms: Wall-clock latency of the inference call in milliseconds.
            token_usage: Typed token consumption, or ``None`` when unavailable.

        """
        ...

    def on_action_dispatch(
        self,
        *,
        run_id: str,
        action_id: str,
        action_type: str,
        outcome_kind: str,
        latency_ms: int,
    ) -> None:
        """Call after each action dispatch attempt completes.

        Implementations should record dispatch counters and latency histograms.

        Args:
            run_id: Run identifier.
            action_id: Action/turn identifier.
            action_type: Discriminator string for the action class.
            outcome_kind: Outcome label (e.g. ``"dispatched"``, ``"blocked"``,
                ``"error"``).
            latency_ms: Wall-clock latency of the dispatch call.

        """
        ...

    def on_recovery_triggered(
        self,
        *,
        run_id: str,
        reason_code: str,
        mode: str,
    ) -> None:
        """Call each time a recovery decision is triggered.

        Implementations should increment a recovery counter keyed by mode.

        Args:
            run_id: Run identifier.
            reason_code: Failure reason code that triggered recovery.
            mode: Recovery mode selected (e.g. ``"abort"``,
                ``"static_compensation"``, ``"reflect_and_retry"``).

        """
        ...

    def on_admission_evaluated(
        self,
        *,
        run_id: str,
        action_id: str,
        admitted: bool,
        latency_ms: int,
    ) -> None:
        """Call after the admission gate evaluates an action.

        Implementations may record a sub-span or counter for the admission step.

        Args:
            run_id: Run identifier.
            action_id: Action being admitted or rejected.
            admitted: ``True`` when admission was granted, ``False`` when blocked.
            latency_ms: Wall-clock duration of the admission check.

        """
        ...

    def on_dispatch_attempted(
        self,
        *,
        run_id: str,
        action_id: str,
        dedupe_outcome: str,
        latency_ms: int,
    ) -> None:
        """Call after the DedupeStore reservation and executor dispatch.

        Implementations may record a sub-span or counter for the dispatch step.

        Args:
            run_id: Run identifier.
            action_id: Action being dispatched.
            dedupe_outcome: Outcome of the dedupe reservation (e.g.
                ``"accepted"``, ``"duplicate"``, ``"degraded"``).
            latency_ms: Wall-clock duration from reservation to executor return.

        """
        ...

    def on_parallel_branch_result(
        self,
        *,
        run_id: str,
        group_idempotency_key: str,
        action_id: str,
        outcome: str,
        failure_code: str | None = None,
    ) -> None:
        """Call for each branch in a parallel group when the branch completes.

        Implementations may record per-branch counters or update a metric
        labelled by outcome (``"acknowledged"``, ``"failed"``, ``"timeout"``).

        Args:
            run_id: Run identifier.
            group_idempotency_key: Stable key for the parallel group.
            action_id: Branch action identifier.
            outcome: Branch outcome label.
            failure_code: Exception type or failure discriminator when the
                branch failed.  ``None`` for successful branches.

        """
        ...

    def on_dedupe_hit(
        self,
        *,
        run_id: str,
        action_id: str,
        outcome: str,
    ) -> None:
        """Call after a DedupeStore reservation attempt.

        Implementations should increment a counter keyed by *outcome* so
        operators can distinguish fresh dispatches from replayed duplicates.

        Args:
            run_id: Run identifier.
            action_id: Action being dispatched.
            outcome: Reservation result: ``"accepted"``, ``"duplicate"``, or
                ``"degraded"`` (store unavailable 鈥?fallback dispatch).

        """
        ...

    def on_reflection_round(
        self,
        *,
        run_id: str,
        action_id: str,
        round_num: int,
        corrected: bool,
    ) -> None:
        """Call each time the reflection loop completes one round.

        Implementations should record a counter to track how often the LLM
        self-corrects during recovery and whether it produces a valid action.

        Args:
            run_id: Run identifier.
            action_id: Action that triggered the recovery.
            round_num: Zero-based reflection round index.
            corrected: ``True`` when the reasoning loop produced a corrected
                action; ``False`` when it returned an empty or invalid result.

        """
        ...

    def on_circuit_breaker_trip(
        self,
        *,
        run_id: str,
        effect_class: str,
        failure_count: int,
        tripped: bool,
    ) -> None:
        """Call when the circuit breaker records a failure or resets.

        Implementations should maintain a gauge of open circuits and a counter
        of trips to help operators detect cascading failures.

        Args:
            run_id: Run identifier.
            effect_class: Effect class whose circuit breaker was updated.
            failure_count: Current failure count after this update.
            tripped: ``True`` when the circuit transitioned to OPEN (or
                stayed OPEN); ``False`` when failure count was reset (CLOSED).

        """
        ...

    def on_branch_rollback_triggered(
        self,
        *,
        run_id: str,
        group_idempotency_key: str,
        action_id: str,
        join_strategy: str,
    ) -> None:
        """Call for each succeeded branch when the group join fails.

        This hook records rollback intent so operators can observe and trigger
        compensating transactions.  It does NOT perform the rollback itself.

        Args:
            run_id: Run identifier.
            group_idempotency_key: Stable key for the parallel group.
            action_id: Identifier of the succeeded branch that needs rollback.
            join_strategy: Join strategy that was not satisfied (e.g. ``"all"``).

        """
        ...

    def on_turn_phase(
        self,
        *,
        run_id: str,
        action_id: str,
        phase_name: str,
        elapsed_ms: int,
    ) -> None:
        """Call after each TurnEngine phase completes.

        Implementations may create sub-spans or record per-phase histograms
        to provide fine-grained FSM observability.

        Args:
            run_id: Run identifier.
            action_id: Action/turn identifier, or empty string during the
                reasoning phase when no action has been derived yet.
            phase_name: Internal phase method name (e.g. ``"_phase_snapshot"``).
            elapsed_ms: Wall-clock duration of the phase in milliseconds.

        """
        ...


# ---------------------------------------------------------------------------
# Phase 1 鈥?Cognitive Foundation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TokenBudget:
    """Per-turn token budget for LLM inference.

    Attributes:
        max_input: Maximum input tokens for context window.
        max_output: Maximum output tokens for model response.
        reasoning_budget: Optional extended thinking budget (provider-specific).

    """

    max_input: int = 32_768
    max_output: int = 4_096
    reasoning_budget: int | None = None


@dataclass(frozen=True, slots=True)
class InferenceConfig:
    """Configuration for one LLM inference call.

    Enters the CapabilitySnapshot SHA256 audit chain to ensure every
    model call is traceable to the exact configuration that authorized it.

    Attributes:
        model_ref: Provider-qualified model identifier (e.g. ``gpt-4o``).
        token_budget: Per-turn token allocation.
        temperature: Sampling temperature. ``0.0`` for deterministic output.
        stop_sequences: Optional stop sequences for response termination.
        turn_kind_overrides: Per-turn-kind token budget overrides.
            Keys are turn kind labels (``"reasoning"``, ``"tool_selection"``).

    """

    model_ref: str
    token_budget: TokenBudget = field(default_factory=TokenBudget)
    temperature: float = 0.0
    stop_sequences: tuple[str, ...] = field(default_factory=tuple)
    turn_kind_overrides: dict[str, TokenBudget] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Defines one tool exposed to the model in a ContextWindow.

    Attributes:
        name: Tool name as presented to the model.
        description: Human-readable description of the tool's purpose.
        input_schema: JSON Schema dict for tool input validation.

    """

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SkillSummary:
    """Summarises one skill for model consumption in a ContextWindow.

    Attributes:
        skill_id: Unique skill identifier.
        description: Human-readable description.
        script_ids: Ordered list of script identifiers within the skill.
        input_schema: Optional JSON Schema for skill invocation.

    """

    skill_id: str
    description: str
    script_ids: tuple[str, ...] = field(default_factory=tuple)
    input_schema: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ContextWindow:
    """Assembled model input produced by ContextPort.

    Immutable snapshot of everything the model sees for one Turn.
    The quality of this object is the upper bound on model output quality.

    Attributes:
        system_instructions: System-level instructions derived from
            capability_scope and policy.
        tool_definitions: Ordered tool definitions available to the model.
        skill_definitions: Ordered skill summaries available to the model.
        history: Conversation history as message dicts (provider-neutral).
        current_state: Current run state digest from ProjectionService.
        memory_ref: Optional memory binding reference (platform-owned).
        recovery_context: Optional structured recovery context when
            assembling for a reflect_and_retry turn.
        inference_config: Inference configuration governing this context.

    """

    system_instructions: str
    tool_definitions: tuple[ToolDefinition, ...] = field(default_factory=tuple)
    skill_definitions: tuple[SkillSummary, ...] = field(default_factory=tuple)
    history: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    current_state: dict[str, Any] = field(default_factory=dict)
    memory_ref: str | None = None
    recovery_context: dict[str, Any] | None = None
    inference_config: InferenceConfig | None = None


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Typed token consumption for one LLM inference call.

    Attributes:
        input_tokens: Tokens consumed by the prompt/context.
        output_tokens: Tokens produced in the model response.
        reasoning_tokens: Extended-thinking tokens (provider-specific; 0 when
            not applicable).

    """

    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0


@dataclass(frozen=True, slots=True)
class ModelOutput:
    """Normalised model response from any LLM provider.

    Attributes:
        raw_text: Raw text content of the model response.
        tool_calls: Ordered list of provider-neutral tool call dicts.
            Each dict has keys: ``id``, ``name``, ``arguments`` (dict).
        finish_reason: Reason the model stopped generating.
        usage: Token usage statistics dict (``input_tokens``, ``output_tokens``).
        latency_ms: Wall-clock latency of the inference call in milliseconds.
            Zero when not measured.
        token_usage: Typed token consumption.  When set, supersedes ``usage``
            for metrics purposes.

    """

    raw_text: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: Literal["stop", "tool_calls", "length", "error"] = "stop"
    usage: dict[str, int] = field(default_factory=dict)
    latency_ms: int = 0
    token_usage: TokenUsage | None = None


@dataclass(frozen=True, slots=True)
class InferenceActivityInput:
    """Input payload for the execute_inference Temporal Activity.

    Attributes:
        run_id: Run identifier.
        turn_id: Turn identifier within the run.
        context_window: Assembled model input.
        config: Inference configuration for this call.
        idempotency_key: Stable key for Temporal Activity-level dedup.
            Prevents double-billing on Temporal retry.

    """

    run_id: str
    turn_id: str
    context_window: ContextWindow
    config: InferenceConfig
    idempotency_key: str


class LLMGateway(Protocol):
    """Southbound abstraction for LLM provider calls.

    Positioned below the Temporal Activity boundary. The Activity
    provides durability; the Gateway provides provider abstraction.
    Two independent retry levels must not be merged:
      - Temporal Activity retry  鈫?kernel-level (process crash, timeout)
      - Gateway-internal retry   鈫?provider-level (rate limits, 5xx)

    Implementations must never raise on provider-level errors directly;
    they must classify errors into ``ModelOutput(finish_reason="error")``
    or raise ``LLMProviderError`` for the Activity to handle.
    """

    async def infer(
        self,
        context: ContextWindow,
        config: InferenceConfig,
        idempotency_key: str,
    ) -> ModelOutput:
        """Run one synchronous inference call.

        Args:
            context: Assembled context window.
            config: Inference configuration.
            idempotency_key: Stable dedup key for provider-side caching.

        Returns:
            Normalised model output.

        """
        ...

    async def count_tokens(
        self,
        context: ContextWindow,
        model_ref: str,
    ) -> int:
        """Estimates token count for the context window.

        Args:
            context: Context window to estimate.
            model_ref: Model for which to estimate tokens.

        Returns:
            Estimated total token count.

        """
        ...


class ContextPort(Protocol):
    """Northbound interface for context window assembly.

    The kernel calls this port before each ReasoningLoop turn.
    The platform provides the implementation; the kernel owns the contract.
    Platform MUST NOT bypass ContextPort to inject prompts directly.

    Invariant 9: All model input passes through ContextPort.
    """

    async def assemble(
        self,
        run_id: str,
        snapshot: CapabilitySnapshot,
        history: list[RuntimeEvent],
        inference_config: InferenceConfig | None = None,
        recovery_context: dict[str, Any] | None = None,
    ) -> ContextWindow:
        """Assembles one context window for a Turn.

        Args:
            run_id: Run identifier.
            snapshot: Frozen capability snapshot for tool/skill enumeration.
            history: Ordered event history for conversation reconstruction.
            inference_config: Optional inference config to embed in window.
            recovery_context: Optional structured recovery context for
                reflect_and_retry turns.

        Returns:
            Immutable context window ready for model inference.

        """
        ...


class OutputParser(Protocol):
    """Parses raw model output into kernel-executable Actions.

    The parser bridges the cognitive layer (model output) and the
    execution layer (kernel Actions). It must not embed business logic, only
    structural translation from model output format to kernel DTOs.
    """

    def parse(
        self,
        output: ModelOutput,
        run_id: str,
    ) -> list[Action]:
        """Parse model output into a flat list of Actions.

        Args:
            output: Normalised model output.
            run_id: Run identifier for action construction.

        Returns:
            Ordered list of kernel Actions for sequential execution.

        """
        ...


@dataclass(frozen=True, slots=True)
class BranchResult:
    """Result from one successfully completed parallel branch.

    Attributes:
        action_id: Action identifier for this branch.
        output_json: Optional structured output from the branch.
        acknowledged: Whether the action was positively acknowledged.

    """

    action_id: str
    output_json: dict[str, Any] | None = None
    acknowledged: bool = True


@dataclass(frozen=True, slots=True)
class BranchFailure:
    """Failure record from one parallel branch.

    Attributes:
        action_id: Action identifier for the failed branch.
        failure_kind: Failure category for recovery routing.
        failure_code: Specific failure code.
        evidence: Optional FailureEnvelope for detailed evidence.

    """

    action_id: str
    failure_kind: str
    failure_code: str
    evidence: FailureEnvelope | None = None


@dataclass(frozen=True, slots=True)
class ParallelJoinResult:
    """Aggregate result after parallel barrier completion.

    Attributes:
        group_idempotency_key: Group identifier.
        successes: List of successful branch results.
        failures: List of failed branch results.
        join_satisfied: Whether the join_strategy was satisfied.

    """

    group_idempotency_key: str
    successes: list[BranchResult]
    failures: list[BranchFailure]
    join_satisfied: bool


# ---------------------------------------------------------------------------
# Phase 4 鈥?Script Execution Infrastructure
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScriptResult:
    """Result from one script execution.

    Attributes:
        script_id: Script identifier.
        exit_code: Process exit code (0 = success).
        stdout: Captured standard output.
        stderr: Captured standard error.
        output_json: Optional structured JSON output parsed from stdout.
        execution_ms: Actual execution duration in milliseconds.

    """

    script_id: str
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    output_json: dict[str, Any] | None = None
    execution_ms: int = 0


@dataclass(frozen=True, slots=True)
class ScriptActivityInput:
    """Input payload for the execute_skill_script Temporal Activity.

    Attributes:
        run_id: Run identifier.
        action_id: Action identifier.
        script_id: Script identifier within the skill.
        script_content: Executable script content (not a reference).
        host_kind: Execution mechanism (``local_process``, ``in_process_python``,
            ``remote_service``).
        parameters: Runtime parameters injected into the script.
        timeout_ms: Maximum execution time before heartbeat_timeout.
        heartbeat_interval_ms: How often the Activity sends heartbeats.

    """

    run_id: str
    action_id: str
    script_id: str
    script_content: str
    host_kind: str
    parameters: dict[str, Any] = field(default_factory=dict)
    timeout_ms: int = 30_000
    heartbeat_interval_ms: int = 5_000


@dataclass(frozen=True, slots=True)
class ScriptFailureEvidence:
    """Structured evidence from a failed script execution.

    Used by ReflectionContextBuilder to assemble failure context for the
    model. The ``budget_consumed_ratio`` + ``output_produced=False``
    combination is the heuristic signature of a dead loop.

    Attributes:
        script_id: Script identifier.
        failure_kind: Failure category.
        budget_consumed_ratio: Fraction of timeout budget consumed (0.0-1.0).
            Value 鈮?1.0 with ``output_produced=False`` 鈫?suspected dead loop.
        output_produced: Whether any stdout output was produced before failure.
        suspected_cause: Optional human-readable root cause hypothesis.
        partial_output: Partial stdout captured before timeout/failure.
        original_script: Original script content for model inspection.
        stderr_tail: Last N lines of stderr for model inspection.

    """

    script_id: str
    failure_kind: Literal[
        "heartbeat_timeout",
        "runtime_error",
        "permission_denied",
        "resource_exhausted",
        "output_validation_failed",
    ]
    budget_consumed_ratio: float
    output_produced: bool
    suspected_cause: str | None = None
    partial_output: str | None = None
    original_script: str = ""
    stderr_tail: str | None = None


class ScriptRuntime(Protocol):
    """Southbound abstraction for script execution environments.

    Routes execution to the correct host based on ``host_kind``:
    - ``local_process``   鈫?subprocess with stdout/stderr capture
    - ``in_process_python`` 鈫?exec() in isolated namespace
    - ``remote_service``  鈫?HTTP/gRPC call to remote executor
    """

    async def execute_script(
        self,
        input_value: ScriptActivityInput,
    ) -> ScriptResult:
        """Execute one script and returns a result.

        Args:
            input_value: Script execution payload with content and parameters.

        Returns:
            Script result with exit code and captured output.

        """
        ...

    async def validate_script(
        self,
        script_content: str,
        host_kind: str,
    ) -> bool:
        """Validate that a script is safe to execute.

        Args:
            script_content: Script source to validate.
            host_kind: Target execution mechanism.

        Returns:
            ``True`` when validation passes.

        """
        ...


# ---------------------------------------------------------------------------
# Phase 5 鈥?Reflection Loop
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReflectionPolicy:
    """Governs the reflect_and_retry recovery loop.

    Prevents infinite reflection cycles by bounding round count and
    classifying which failure kinds are eligible for model reflection.

    Attributes:
        max_rounds: Maximum reflection attempts before escalation/abort.
        reflection_timeout_ms: Per-reflection-turn timeout.
        reflectable_failure_kinds: Set of failure kinds that may trigger
            a reflection round.
        non_reflectable_failure_kinds: Failure kinds that always bypass
            reflection (go directly to human_escalation or abort).
        escalate_on_exhaustion: When ``True`` and ``max_rounds`` is reached,
            fall back to ``human_escalation`` instead of ``abort``.

    """

    max_rounds: int = 3
    reflection_timeout_ms: int = 60_000
    reflectable_failure_kinds: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {
                "heartbeat_timeout",
                "runtime_error",
                "output_validation_failed",
            }
        )
    )
    non_reflectable_failure_kinds: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {
                "resource_exhausted",
                "permission_denied",
            }
        )
    )
    escalate_on_exhaustion: bool = True

    def is_reflectable(self, failure_kind: str) -> bool:
        """Return whether a failure kind is eligible for reflection.

        Args:
            failure_kind: Failure kind string to check.

        Returns:
            ``True`` when the failure kind should trigger reflection.

        """
        if failure_kind in self.non_reflectable_failure_kinds:
            return False
        return failure_kind in self.reflectable_failure_kinds


# ---------------------------------------------------------------------------
# Facade DTOs 鈥?platform-facing request/response contracts
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """Approval decision submitted by a human actor via KernelFacade.

    Attributes:
        run_id: Run awaiting the approval decision.
        approval_ref: Unique reference for this approval decision.  Used as
            idempotency key so duplicate submissions are safe.
        approved: ``True`` to allow the gated action; ``False`` to deny it.
        reviewer_id: Identifier of the human reviewer.
        reason: Optional human-readable reason for the decision.
        caused_by: Optional provenance marker for observability.

    """

    run_id: str
    approval_ref: str
    approved: bool
    reviewer_id: str
    reason: str | None = None
    caused_by: str | None = None


@dataclass(frozen=True, slots=True)
class KernelManifest:
    """Capability declaration for platform-layer feature discovery.

    Aggregates all kernel registries into a single frozen snapshot that
    platform integrators query at startup to detect supported features.
    Pattern inspired by the LSP ServerCapabilities initialization handshake:
    the server declares capabilities once and clients adapt accordingly.

    Attributes:
        kernel_version: Semantic version of the kernel implementation.
        protocol_version: Interface protocol version.  Increment on any
            breaking change to the KernelFacade contract.
        supported_action_types: ``action_type`` strings from
            KERNEL_ACTION_TYPE_REGISTRY.
        supported_interaction_targets: ``InteractionTarget`` literal values
            this kernel can dispatch to.
        supported_recovery_modes: Recovery mode strings from
            KERNEL_RECOVERY_MODE_REGISTRY.
        supported_governance_features: Named governance capabilities.
            e.g. ``{"approval_gate", "dedupe", "speculation_mode"}``.
        supported_event_types: ``event_type`` strings from
            KERNEL_EVENT_REGISTRY.
        substrate_type: Execution substrate identifier.
            e.g. ``"temporal"`` or ``"local_fsm"``.
        capability_snapshot_schema_version: CapabilitySnapshot schema version
            required by this kernel (``"2"`` for model/memory/session bindings).

    """

    kernel_version: str
    protocol_version: str
    supported_action_types: frozenset[str]
    supported_interaction_targets: frozenset[str]
    supported_recovery_modes: frozenset[str]
    supported_governance_features: frozenset[str]
    supported_event_types: frozenset[str]
    substrate_type: str
    capability_snapshot_schema_version: str = "2"
    substrate_limitations: frozenset[str] = field(default_factory=frozenset)
    trace_protocol_version: str = "2.8"
    """TRACE architecture protocol version this kernel implements."""
    supported_trace_features: frozenset[str] = field(default_factory=frozenset)
    """TRACE-specific capabilities: branch_protocol, task_view_record,
    policy_version_pinning, evolve_postmortem, child_run_orchestration, etc."""


# ---------------------------------------------------------------------------
# TRACE Runtime Truth DTOs
# ---------------------------------------------------------------------------

TraceRunState = Literal[
    "created", "active", "waiting", "recovering", "completed", "failed", "aborted"
]
TraceStageState = Literal["pending", "active", "blocked", "completed", "failed"]
TraceBranchState = Literal["proposed", "active", "waiting", "pruned", "succeeded", "failed"]
TraceWaitState = Literal["none", "external_callback", "human_review", "scheduled_resume"]
TraceReviewState = Literal["not_required", "requested", "in_review", "approved", "rejected"]


@dataclass(frozen=True, slots=True)
class TraceBranchView:
    """Snapshot of one branch's state within a TraceRuntimeView."""

    branch_id: str
    stage_id: str
    state: TraceBranchState
    opened_at: str
    parent_branch_id: str | None = None
    proposed_by: str | None = None
    failure_code: str | None = None
    policy_versions: RunPolicyVersions | None = None


@dataclass(frozen=True, slots=True)
class TraceStageView:
    """Snapshot of one stage's state within a TraceRuntimeView.

    Stages are TRACE's formal phase objects (route, capture, evaluate, evolve).
    agent-kernel records stage lifecycle events but does not own stage semantics.
    """

    stage_id: str
    state: TraceStageState
    entered_at: str
    exited_at: str | None = None
    branch_id: str | None = None
    failure_code: str | None = None


@dataclass(frozen=True, slots=True)
class TraceRuntimeView:
    """TRACE-facing aggregated runtime truth.  Derived from RunProjection + branch events.

    This is a derived view built on top of RunProjection (which remains the
    authoritative kernel-internal projection).  hi-agent consumes this view
    for routing, pruning, and evolution decisions.
    """

    run_id: str
    run_state: TraceRunState
    wait_state: TraceWaitState
    review_state: TraceReviewState
    active_stage_id: str | None
    branches: list[TraceBranchView]
    policy_versions: RunPolicyVersions | None
    projected_at: str
    stages: list[TraceStageView] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class TaskViewRecord:
    """Records what context references were shown to the model for one decision.

    hi-agent assembles Task View content and calls record_task_view() before
    each model invocation.  agent-kernel stores the references and policy
    versions so the decision can be replayed and attributed during postmortem.

    agent-kernel stores REFERENCES only 鈥?content lives in agent-core / storage.
    """

    task_view_id: str
    run_id: str
    selected_model_role: str
    """\"heavy_reasoning\" | \"light_processing\" | \"evaluation\""""
    assembled_at: str
    decision_ref: str | None = None
    """Late-bound after the model call via bind_to_decision().

    Matches ``TurnIntentRecord.intent_commit_ref`` once bound.
    """
    stage_id: str | None = None
    branch_id: str | None = None
    task_contract_ref: str | None = None
    evidence_refs: list[str] = field(default_factory=list)
    memory_refs: list[str] = field(default_factory=list)
    knowledge_refs: list[str] = field(default_factory=list)
    policy_versions: RunPolicyVersions | None = None
    schema_version: str = "1"


@dataclass(frozen=True, slots=True)
class OpenBranchRequest:
    """Request to open a new trajectory branch within a run.

    Branch ID generation and uniqueness within the run is hi-agent's
    responsibility.
    """

    run_id: str
    branch_id: str
    stage_id: str
    parent_branch_id: str | None = None
    proposed_by: str | None = None
    """\"model\" | \"human\" | \"system\""""


@dataclass(frozen=True, slots=True)
class BranchStateUpdateRequest:
    """Request to update a branch's lifecycle state."""

    run_id: str
    branch_id: str
    new_state: TraceBranchState
    failure_code: TraceFailureCode | None = None
    reason: str | None = None


HumanGateType = Literal[
    "contract_correction",  # Gate A: task contract conflict
    "route_direction",  # Gate B: budget/ambiguous exploration
    "artifact_review",  # Gate C: intermediate artifact quality
    "final_approval",  # Gate D: irreversible submission
]


@dataclass(frozen=True, slots=True)
class HumanGateRequest:
    """Request to open a human review gate within a run.

    Human gates are first-class lifecycle events, not just approval signals.
    The system may trigger them proactively (system-triggered) or they may
    originate from user requests.
    """

    gate_ref: str
    gate_type: HumanGateType
    run_id: str
    trigger_reason: str
    trigger_source: Literal["system", "human"]
    stage_id: str | None = None
    branch_id: str | None = None
    artifact_ref: str | None = None
    caused_by: str | None = None


@dataclass(frozen=True, slots=True)
class HumanGateResolution:
    """Snapshot of one human gate resolution for postmortem aggregation."""

    gate_ref: str
    gate_type: HumanGateType
    resolution: Literal["approved", "rejected"]
    resolved_by: str | None = None
    resolved_at: str | None = None
    stage_id: str | None = None
    branch_id: str | None = None


@dataclass(frozen=True, slots=True)
class RunPostmortemView:
    """Aggregated run data for post-run analysis by hi-agent evolve.

    This is a projection that scans the run's event log to aggregate action
    counts, failure codes, timestamps, and human gate outcomes.  hi-agent's
    evolve layer enriches it with task_family, quality_score, efficiency_score,
    and trajectory_summary which are hi-agent-owned semantics.
    """

    run_id: str
    task_id: str | None
    run_kind: str
    outcome: TraceRunState
    stages: list[TraceStageView]
    branches: list[TraceBranchView]
    total_action_count: int
    failure_codes: list[str]
    duration_ms: int
    human_gate_resolutions: list[HumanGateResolution]
    policy_versions: RunPolicyVersions | None
    event_count: int
    created_at: str
    completed_at: str | None


@dataclass(frozen=True, slots=True)
class ChildRunSummary:
    """Summary status of one child run for parent aggregation queries."""

    child_run_id: str
    child_kind: str
    task_id: str | None
    lifecycle_state: RunLifecycleState
    outcome: TraceRunState | None
    created_at: str
    completed_at: str | None


# ---------------------------------------------------------------------------
# TRACE Runtime Arbitration Semantics (Gap E)
#
# These rules are enforced inside RunActorWorkflow and documented here as the
# public kernel contract so hi-agent can rely on them without reading internals.
#
# Rule A1 (callback_beats_timeout):
#   If a callback with valid (action_id, callback_id) arrives AFTER a
#   heartbeat_timeout signal for the same action, the callback result wins.
#   The timeout event is demoted to derived_diagnostic and does NOT override
#   the callback result.
#
# Rule A2 (effect_unknown_recovery_path):
#   An action reaching effect_unknown enters RecoveryGate before any retry.
#   For side_effect_class "irreversible_submit": NO automatic re-dispatch.
#   For "external_write": query external state before re-dispatch.
#
# Rule A3 (policy_version_freeze_on_resume):
#   A waiting run resumes under the policy_versions pinned at run creation.
#   No mid-run policy version change without an explicit change_record event.
#
# Rule A4 (human_review_blocks_auto_resume):
#   While TraceReviewState != "approved", scheduled_resume is deferred.
#   Actions with side_effect_class "irreversible_submit" require approval.
# ---------------------------------------------------------------------------
