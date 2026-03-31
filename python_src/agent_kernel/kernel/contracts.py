"""Defines the core contracts for the agent_kernel Temporal kernel PoC.

This module mirrors the refreshed architecture documents and intentionally
contains only typed contracts plus placeholder interfaces. The goal of this
stage is to let the test suite define the required behavior before the runtime
implementation is introduced.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from agent_kernel.kernel.capability_snapshot import CapabilitySnapshot

RunLifecycleState = Literal[
    "created",
    "ready",
    "dispatching",
    "waiting_result",
    "waiting_external",
    "recovering",
    "completed",
    "aborted",
]
EffectClass = Literal[
    "read_only",
    "idempotent_write",
    "compensatable_write",
    "irreversible_write",
]
ExternalIdempotencyLevel = Literal["guaranteed", "best_effort", "unknown"]
RecoveryMode = Literal["static_compensation", "human_escalation", "abort"]


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
    input_ref: str | None = None
    input_json: dict[str, Any] | None = None
    policy_tags: list[str] = field(default_factory=list)
    timeout_ms: int | None = None


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
    recovery_mode: Literal["static_compensation", "human_escalation", "abort"] | None = None
    recovery_reason: str | None = None
    active_child_runs: list[str] = field(default_factory=list)


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
class RecoveryDecision:
    """Represents the only allowed recovery exit from a failed action.

    Recovery decisions stay deliberately narrow so that only the Recovery gate
    can choose the next failure handling mode.
    """

    run_id: str
    mode: RecoveryMode
    reason: str
    compensation_action_id: str | None = None
    escalation_channel_ref: str | None = None


@dataclass(frozen=True, slots=True)
class RecoveryInput:
    """Represents the failure envelope handed into Recovery.

    Recovery consumes both the failure reason and the
    authoritative projection view so it can choose a safe exit
    path without reaching around the actor.
    """

    run_id: str
    reason_code: str
    lifecycle_state: RunLifecycleState
    projection: RunProjection
    failed_action_id: str | None = None


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
    outcome_state: Literal["executed", "scheduled", "escalated", "aborted"]
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
    """

    initiator: Literal["user", "agent_core_runner", "system"]
    run_kind: str
    session_id: str | None = None
    input_ref: str | None = None
    input_json: dict[str, Any] | None = None
    context_ref: str | None = None
    parent_run_id: str | None = None


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
    recovery_mode: Literal["static_compensation", "human_escalation", "abort"] | None = None
    recovery_reason: str | None = None
    active_child_runs: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class QueryRunDashboardResponse:
    """Represents a dashboard-oriented run read model."""

    run_id: str
    lifecycle_state: RunLifecycleState
    projected_offset: int
    waiting_external: bool
    recovery_mode: Literal["static_compensation", "human_escalation", "abort"] | None = None
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
        """Starts one durable workflow for a run.

        Args:
            request: Kernel-safe run start request.

        Returns:
            A minimal substrate response containing a workflow identifier.
        """
        ...

    async def signal_workflow(self, run_id: str, signal: SignalRunRequest) -> None:
        """Sends one signal into a running workflow.

        Args:
            run_id: Run identifier mapped to the workflow.
            signal: Signal payload to deliver.
        """
        ...

    async def cancel_workflow(self, run_id: str, reason: str) -> None:
        """Cancels one workflow by run identifier.

        Args:
            run_id: Run identifier mapped to the workflow.
            reason: Cancellation reason string.
        """
        ...

    async def query_projection(self, run_id: str) -> RunProjection:
        """Queries the current authoritative run projection.

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
        """Starts one durable child workflow.

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
        """Executes one admission activity.

        Args:
            request: Admission activity input payload.

        Returns:
            Admission decision result.
        """
        ...

    async def execute_tool(self, request: ToolActivityInput) -> Any:
        """Executes one tool activity.

        Args:
            request: Tool activity input payload.

        Returns:
            Tool execution result.
        """
        ...

    async def execute_mcp(self, request: MCPActivityInput) -> Any:
        """Executes one MCP activity.

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
        """Executes one verification activity.

        Args:
            request: Verification activity input payload.

        Returns:
            Verification result.
        """
        ...

    async def execute_reconciliation(self, request: ReconciliationActivityInput) -> Any:
        """Executes one reconciliation activity.

        Args:
            request: Reconciliation activity input payload.

        Returns:
            Reconciliation result.
        """
        ...


class KernelRuntimeEventLog(Protocol):
    """Abstracts the authoritative domain event log."""

    async def append_action_commit(self, commit: ActionCommit) -> str:
        """Appends one action-level commit to the event log.

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
        """Loads events for a run after a given offset.

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
        """Checks whether projection has reached a required offset.

        Args:
            run_id: Run identifier to check.
            required_offset: Minimum required offset.

        Returns:
            ``True`` when projection has reached the required offset.
        """
        ...

    async def get(self, run_id: str) -> RunProjection:
        """Gets the latest projection state for a run.

        Args:
            run_id: Run identifier to look up.

        Returns:
            Current authoritative projection for the run.
        """
        ...


class DispatchAdmissionService(Protocol):
    """Abstracts dispatch-time admission checks."""

    async def admit(
        self,
        action: Action,
        snapshot: CapabilitySnapshot,
    ) -> AdmissionResult:
        """Evaluates whether an action may execute under capability snapshot.

        Args:
            action: Candidate action to evaluate.
            snapshot: Frozen capability snapshot used for policy checks.

        Returns:
            Admission result with deny reason or granted admission.
        """
        ...

    async def check(self, action: Action, projection: RunProjection) -> AdmissionResult:
        """Evaluates whether an action may execute under current projection.

        Args:
            action: Candidate action to evaluate.
            projection: Current authoritative run projection.

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
        """Executes one admitted action.

        Args:
            action: Admitted action to execute.
            grant_ref: Optional grant reference from admission.

        Returns:
            Execution result from the action handler.
        """
        ...


class RecoveryGateService(Protocol):
    """Abstracts the recovery gate."""

    async def decide(self, recovery_input: RecoveryInput) -> RecoveryDecision:
        """Selects a recovery exit for one failure envelope.

        Args:
            recovery_input: Failure envelope and projection context.

        Returns:
            Recovery decision with mode and optional compensation or escalation.
        """
        ...


class RecoveryOutcomeStore(Protocol):
    """Abstracts persistence for recovery outcomes."""

    async def write_outcome(self, outcome: RecoveryOutcome) -> None:
        """Persists one recovery outcome record.

        Args:
            outcome: Recovery outcome to persist.
        """
        ...

    async def latest_for_run(self, run_id: str) -> RecoveryOutcome | None:
        """Returns latest recovery outcome for one run.

        Args:
            run_id: Run identifier to look up.

        Returns:
            Latest recovery outcome, or ``None`` if no outcome exists.
        """
        ...


class TurnIntentLog(Protocol):
    """Abstracts persistence of turn intent commit metadata."""

    async def write_intent(self, intent: TurnIntentRecord) -> None:
        """Persists one turn intent record.

        Args:
            intent: Turn intent metadata to persist.
        """
        ...

    async def latest_for_run(self, run_id: str) -> TurnIntentRecord | None:
        """Returns latest turn intent for one run.

        Args:
            run_id: Run identifier to query.

        Returns:
            Most recent turn intent record, or ``None`` when absent.
        """
        ...


class IngressAdapter(Protocol):
    """Abstracts ingress translation from platform events into kernel DTOs."""

    def from_runner_start(self, input_value: Any) -> StartRunRequest:
        """Builds start-run request from runner-originated input."""
        ...

    def from_session_signal(self, input_value: Any) -> SignalRunRequest:
        """Builds signal request from session-level input."""
        ...

    def from_callback(self, input_value: Any) -> SignalRunRequest:
        """Builds signal request from callback input."""
        ...


class ContextBindingPort(Protocol):
    """Abstracts context binding at kernel boundary."""

    def bind_context(self, input_value: Any) -> Any:
        """Resolves runtime context binding from platform input."""
        ...


class CheckpointResumePort(Protocol):
    """Abstracts checkpoint export and resume import at kernel boundary."""

    async def export_checkpoint(self, run_id: str) -> Any:
        """Exports platform-facing checkpoint view for one run."""
        ...

    async def import_resume(self, input_value: Any) -> Any:
        """Imports platform resume payload into kernel-safe request."""
        ...


class CapabilityAdapter(Protocol):
    """Abstracts capability bindings resolution from platform metadata."""

    async def resolve_tool_bindings(self, action: Action) -> list[Any]:
        """Resolves tool bindings for one action."""
        ...

    async def resolve_mcp_bindings(self, action: Action) -> list[Any]:
        """Resolves MCP bindings for one action."""
        ...

    async def resolve_skill_bindings(self, action: Action) -> list[str]:
        """Resolves skill bindings for one action."""
        ...

    async def resolve_declarative_bundle(self, action: Action) -> dict[str, str] | None:
        """Resolves declarative bundle digest payload for one action."""
        ...


class DecisionDeduper(Protocol):
    """Abstracts decision fingerprint de-duplication.

    Boundary note:
      - This protocol de-duplicates *decision rounds* at workflow level.
      - It is intentionally separate from ``DedupeStorePort`` which governs
        dispatch idempotency state transitions at executor boundary.
    """

    async def seen(self, fingerprint: str) -> bool:
        """Returns whether a decision fingerprint has already been processed.

        Args:
            fingerprint: Decision fingerprint to check.

        Returns:
            ``True`` if the fingerprint was previously marked.
        """
        ...

    async def mark(self, fingerprint: str) -> None:
        """Marks a decision fingerprint as processed.

        Args:
            fingerprint: Decision fingerprint to mark as seen.
        """
        ...
