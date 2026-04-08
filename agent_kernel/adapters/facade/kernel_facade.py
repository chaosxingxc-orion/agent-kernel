"""Defines the KernelFacade placeholder for the agent_kernel interface contract.

The facade is the only allowed entrypoint from the platform layer into the
kernel. It must export kernel-safe request and response objects without leaking
Temporal-specific substrate details.
"""

from __future__ import annotations

import contextlib
import inspect
import threading
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator

    from agent_kernel.kernel.task_manager.contracts import TaskDescriptor, TaskHealthStatus
    from agent_kernel.kernel.task_manager.registry import TaskRegistry

from agent_kernel.adapters.agent_core.checkpoint_adapter import AgentCoreResumeInput
from agent_kernel.adapters.agent_core.context_adapter import AgentCoreContextInput
from agent_kernel.kernel.action_type_registry import KERNEL_ACTION_TYPE_REGISTRY
from agent_kernel.kernel.contracts import (
    Action,
    ApprovalRequest,
    AsyncActionHandler,
    BranchStateUpdateRequest,
    CancelRunRequest,
    ChildRunSummary,
    HumanGateRequest,
    HumanGateResolution,
    KernelManifest,
    OpenBranchRequest,
    QueryRunDashboardResponse,
    QueryRunRequest,
    QueryRunResponse,
    ResumeRunRequest,
    RunPolicyVersions,
    RunPostmortemView,
    RuntimeEvent,
    SignalRunRequest,
    SpawnChildRunRequest,
    SpawnChildRunResponse,
    StartRunRequest,
    StartRunResponse,
    TaskViewRecord,
    TemporalWorkflowGateway,
    TraceBranchView,
    TraceRuntimeView,
    TraceStageState,
    TraceStageView,
)
from agent_kernel.kernel.event_registry import KERNEL_EVENT_REGISTRY
from agent_kernel.kernel.recovery.mode_registry import KERNEL_RECOVERY_MODE_REGISTRY
from agent_kernel.kernel.turn_engine import TurnResult

_KERNEL_VERSION = "0.2.0"
_PROTOCOL_VERSION = "1.0.0"
_SUBSTRATE_TEMPORAL = "temporal"
_INTERACTION_TARGETS: frozenset[str] = frozenset(
    {
        "agent_peer",
        "it_service",
        "data_system",
        "tool_executor",
        "human_actor",
        "event_stream",
    }
)
_GOVERNANCE_FEATURES: frozenset[str] = frozenset(
    {
        "approval_gate",
        "at_most_once_dedupe",
        "speculation_mode",
        "replay_fidelity",
        "recovery_gate",
        "capability_snapshot_sha256",
    }
)

# Substrate-specific limitations surfaced in KernelManifest so platforms can
# adapt without trial-and-error.  LocalFSM runs entirely in-process and cannot
# provide Child Workflow isolation or durable Temporal History.
_SUBSTRATE_LIMITATIONS: dict[str, frozenset[str]] = {
    "local_fsm": frozenset(
        {
            "no_child_workflow_isolation",
            "no_temporal_history",
            "no_cross_process_speculation",
        }
    ),
    "temporal": frozenset(),
}


def _substrate_limitations(substrate_type: str) -> frozenset[str]:
    """Return the known limitations frozenset for *substrate_type*.

    Args:
        substrate_type: Substrate identifier (e.g. ``"temporal"``).

    Returns:
        Frozenset of limitation tokens, empty when no limitations apply.

    """
    return _SUBSTRATE_LIMITATIONS.get(substrate_type, frozenset())


class KernelFacade:
    """Provides the only allowed platform entrypoint into the kernel.

    Args:
        workflow_gateway: Gateway that speaks to the Temporal substrate.

    """

    def __init__(
        self,
        workflow_gateway: TemporalWorkflowGateway,
        context_adapter: Any | None = None,
        checkpoint_adapter: Any | None = None,
        health_probe: Any | None = None,
        substrate_type: str = _SUBSTRATE_TEMPORAL,
        kernel_version: str = _KERNEL_VERSION,
        task_registry: TaskRegistry | None = None,
        task_view_log: Any | None = None,
        dedupe_store: Any | None = None,
        drain_coordinator: Any | None = None,
    ) -> None:
        """Initialize facade with substrate gateway and optional adapters.

        Args:
            workflow_gateway: Gateway that speaks to the Temporal substrate.
            context_adapter: Optional context adapter for session binding.
            checkpoint_adapter: Optional checkpoint adapter for resume
                operations.
            health_probe: Optional ``KernelHealthProbe`` instance.  When
                provided, ``get_health()`` delegates to its ``liveness()``
                method.  When ``None``, ``get_health()`` returns a minimal
                static response.
            substrate_type: Substrate identifier reported in ``get_manifest()``.
                Defaults to ``"temporal"``.
            kernel_version: Override for the kernel semantic version string
                reported in ``get_manifest()``.  Defaults to the module-level
                ``_KERNEL_VERSION`` constant.
            task_registry: Optional ``TaskRegistry`` for task-level lifecycle
                management.  When provided, enables ``register_task()``,
                ``get_task_status()``, and ``list_session_tasks()``.
            task_view_log: Optional task view log store (e.g.
                ``SQLiteTaskViewLog``).  When provided, enables
                ``record_task_view()``, ``get_task_view_record()``, and
                ``get_task_view_by_decision()``.
            dedupe_store: Optional dedupe store for ``get_action_state()``.
                When ``None``, ``get_action_state()`` always returns ``None``.
            drain_coordinator: Optional in-flight tracking coordinator used for
                graceful runtime draining.

        """
        self._workflow_gateway = workflow_gateway
        self._context_adapter = context_adapter
        self._checkpoint_adapter = checkpoint_adapter
        self._health_probe = health_probe
        self._substrate_type = substrate_type
        self._kernel_version = kernel_version
        self._task_registry = task_registry
        self._task_view_log = task_view_log
        self._dedupe_store = dedupe_store
        self._drain_coordinator = drain_coordinator
        self._draining = False
        # In-memory branch registry: run_id 鈫?{branch_id: TraceBranchView}.
        # Branch lifecycle events are also forwarded to the workflow as signals.
        self._branch_registry: dict[str, dict[str, TraceBranchView]] = {}
        self._branch_lock = threading.Lock()
        # In-memory stage registry: run_id 鈫?{stage_id: TraceStageView}.
        # Stage lifecycle events are also forwarded to the workflow as signals.
        self._stage_registry: dict[str, dict[str, TraceStageView]] = {}
        self._stage_lock = threading.Lock()
        # Per-instance approval dedup gate: tracks (run_id, approval_ref) pairs
        # that have already been forwarded to the substrate.  Prevents duplicate
        # approval signals from the same facade instance (e.g. accidental double
        # submit from a UI).  Note: this is an in-memory gate scoped to one
        # facade instance 鈥?cross-process dedup lives in the event log.
        self._submitted_approvals: set[tuple[str, str]] = set()
        self._approvals_lock = threading.Lock()
        # Human gate registry: run_id 鈫?set of gate_refs opened via
        # open_human_gate().  Used to derive review_state in query_trace_runtime().
        # "_resolved" suffix tracks gate_refs resolved by submit_approval().
        self._open_human_gates: dict[str, set[str]] = {}
        self._resolved_human_gates: dict[str, set[str]] = {}
        self._human_gate_lock = threading.Lock()

    @property
    def is_draining(self) -> bool:
        """Return whether facade is rejecting new mutation requests."""
        return self._draining

    def set_draining(self, draining: bool) -> None:
        """Set draining mode for mutation request acceptance."""
        self._draining = draining

    @asynccontextmanager
    async def _guard_write_operation(self, operation: str) -> Any:
        """Guard write operations during draining and track in-flight work."""
        if self._draining:
            raise RuntimeError(f"Kernel is draining; '{operation}' is temporarily unavailable.")
        if self._drain_coordinator is not None:
            await self._drain_coordinator.enter()
        try:
            yield
        finally:
            if self._drain_coordinator is not None:
                await self._drain_coordinator.exit()

    @staticmethod
    def _build_signal_observability(
        *,
        operation: str,
        run_id: str,
        caused_by: str | None,
    ) -> dict[str, str]:
        """Build stable observability fields for facade-originated signals.

        Args:
            operation: Logical facade operation name emitting the signal.
            run_id: Target run id for the signal.
            caused_by: Optional upstream cause identifier from request context.

        Returns:
            A payload fragment with ``correlation_id`` and ``event_ref``.

        """
        correlation_id = caused_by or f"{operation}:{run_id}"
        return {
            "correlation_id": correlation_id,
            "event_ref": f"facade:{operation}:{run_id}",
        }

    @staticmethod
    def _build_dashboard_correlation_hint(response: QueryRunResponse) -> str:
        """Build a stable dashboard correlation hint from run query output.

        Args:
            response: Canonical run projection view returned by ``query_run``.

        Returns:
            A best-effort hint string for dashboard correlation and grouping.

        """
        if response.current_action_id:
            return f"action:{response.current_action_id}"
        if response.recovery_reason:
            return f"recovery:{response.recovery_reason}"
        if response.waiting_external:
            return f"external_wait:{response.run_id}:{response.projected_offset}"
        return f"state:{response.lifecycle_state}:{response.run_id}"

    async def start_run(
        self,
        request: StartRunRequest,
    ) -> StartRunResponse:
        """Start one run through the substrate gateway.

        Args:
            request: Platform-safe run start request.

        Returns:
            A facade-safe start response.

        """
        request_for_gateway = request
        binding_ref: str | None = None
        if self._context_adapter is not None:
            context_binding = self._context_adapter.bind_context(
                AgentCoreContextInput(
                    session_id=request.session_id,
                    context_ref=request.context_ref,
                    context_json=None,
                )
            )
            binding_ref = context_binding.binding_ref
            request_for_gateway = replace(request, context_ref=binding_ref)

        workflow = await self._workflow_gateway.start_workflow(request_for_gateway)
        workflow_id = workflow.get("workflow_id", "")
        run_id = str(workflow.get("run_id") or workflow_id)
        if self._context_adapter is not None and binding_ref is not None:
            self._context_adapter.bind_run_context(run_id, binding_ref)
        return StartRunResponse(
            run_id=run_id,
            temporal_workflow_id=workflow_id,
            lifecycle_state="created",
        )

    async def signal_run(self, request: SignalRunRequest) -> None:
        """Routes one external signal into the substrate gateway.

        Args:
            request: Signal request produced by the platform or adapters.

        """
        await self._workflow_gateway.signal_workflow(request.run_id, request)

    @staticmethod
    def _is_stream_event_exposed(
        event: RuntimeEvent,
        include_derived_diagnostic: bool,
    ) -> bool:
        """Return whether one runtime event is exposed by facade stream contract.

        Contract policy:
          - Always expose authoritative lifecycle facts.
          - Optionally pass through ``derived_diagnostic`` events for observability.
          - Keep other derived classes internal until explicit read-model contracts
            are introduced.

        Args:
            event: Runtime event observed from substrate stream.
            include_derived_diagnostic: Enables diagnostic pass-through mode.

        Returns:
            True when event should be emitted through ``stream_run_events``.

        """
        return event.event_authority == "authoritative_fact" or (
            include_derived_diagnostic and event.event_authority == "derived_diagnostic"
        )

    async def stream_run_events(
        self,
        run_id: str,
        *,
        include_derived_diagnostic: bool = False,
    ) -> AsyncGenerator[RuntimeEvent]:
        """Streams facade-safe run events from substrate in observed order.

        Args:
            run_id: Run identifier whose events should be streamed.
            include_derived_diagnostic: Includes diagnostic derived events when True.

        Yields:
            RuntimeEvent: Events allowed by facade stream contract.

        Returns:
            AsyncGenerator[RuntimeEvent]: Async generator of runtime events.

        """
        async for event in self._workflow_gateway.stream_run_events(run_id):
            if self._is_stream_event_exposed(
                event,
                include_derived_diagnostic,
            ):
                yield event

    async def cancel_run(
        self,
        request: CancelRunRequest,
    ) -> None:
        """Cancel one run through the substrate gateway.

        The facade emits a replayable cancellation intent signal
        before forwarding Temporal workflow cancellation.
        This ordering is intentional: the signal gives workflow logic a
        chance to persist authoritative cancellation context
        (reason and provenance) before hard cancellation interrupts
        in-flight work. If workflow finishes between the two calls,
        cancellation may become a no-op and is treated as expected race.

        Args:
            request: Cancellation request produced by the platform.

        Raises:
            Exception:

        """
        await self._workflow_gateway.signal_workflow(
            request.run_id,
            SignalRunRequest(
                run_id=request.run_id,
                signal_type="cancel_requested",
                signal_payload={
                    "reason": request.reason,
                    **self._build_signal_observability(
                        operation="cancel",
                        run_id=request.run_id,
                        caused_by=request.caused_by,
                    ),
                },
                caused_by=request.caused_by,
            ),
        )
        try:
            await self._workflow_gateway.cancel_workflow(request.run_id, request.reason)
        except Exception as error:  # pylint: disable=broad-exception-caught
            if not self._is_expected_cancel_race_error(error):
                raise

    async def resume_run(self, request: ResumeRunRequest) -> None:
        """Resumes one run from a checkpoint snapshot through workflow signal.

        The facade keeps resume semantics explicit and routes recovery wake-up
        through the same signal pathway used by external callbacks.

        Raises:
            RuntimeError: If checkpoint adapter is not configured.

        Args:
            request: Resume request carrying run and optional snapshot identity.

        """
        if self._checkpoint_adapter is None:
            raise RuntimeError("checkpoint_adapter is required for resume_run.")
        kernel_resume_request = await self._checkpoint_adapter.import_resume_request(
            AgentCoreResumeInput(
                run_id=request.run_id,
                snapshot_id=request.snapshot_id,
            ),
        )
        signal_payload: dict[str, Any] = {
            "snapshot_id": kernel_resume_request.snapshot_id,
            **self._build_signal_observability(
                operation="resume",
                run_id=kernel_resume_request.run_id,
                caused_by=request.caused_by,
            ),
        }
        snapshot_offset = getattr(kernel_resume_request, "snapshot_offset", None)
        if snapshot_offset is not None:
            signal_payload["snapshot_offset"] = snapshot_offset
        await self._workflow_gateway.signal_workflow(
            kernel_resume_request.run_id,
            SignalRunRequest(
                run_id=kernel_resume_request.run_id,
                signal_type="resume_from_snapshot",
                signal_payload=signal_payload,
                caused_by=request.caused_by,
            ),
        )

    async def escalate_recovery(
        self,
        run_id: str,
        reason: str,
        caused_by: str | None = None,
    ) -> None:
        """Signals one run that manual recovery escalation is required.

        Args:
            run_id: Target run identifier.
            reason: Human-readable escalation reason.
            caused_by: Optional provenance marker for observability.

        """
        await self._workflow_gateway.signal_workflow(
            run_id,
            SignalRunRequest(
                run_id=run_id,
                signal_type="recovery_escalation",
                signal_payload={
                    "reason": reason,
                    **self._build_signal_observability(
                        operation="recovery_escalation",
                        run_id=run_id,
                        caused_by=caused_by,
                    ),
                },
                caused_by=caused_by,
            ),
        )

    async def query_run(self, request: QueryRunRequest) -> QueryRunResponse:
        """Query one run projection without exposing substrate internals.

        Args:
            request: Projection query request.

        Returns:
            A kernel-safe projection response.

        """
        projection = await self._workflow_gateway.query_projection(request.run_id)
        if self._checkpoint_adapter is not None:
            self._checkpoint_adapter.bind_projection(projection)
        return QueryRunResponse(
            run_id=projection.run_id,
            lifecycle_state=projection.lifecycle_state,
            projected_offset=projection.projected_offset,
            waiting_external=projection.waiting_external,
            current_action_id=projection.current_action_id,
            recovery_mode=projection.recovery_mode,
            recovery_reason=projection.recovery_reason,
            active_child_runs=projection.active_child_runs,
            policy_versions=projection.policy_versions,
            active_stage_id=getattr(projection, "active_stage_id", None),
        )

    async def query_run_dashboard(self, run_id: str) -> QueryRunDashboardResponse:
        """Query one run and returns a dashboard-friendly read model.

        Args:
            run_id: Run identifier to query.

        Returns:
            A dashboard-oriented read model with aggregation-focused fields.

        """
        query_response = await self.query_run(QueryRunRequest(run_id=run_id))
        return QueryRunDashboardResponse(
            run_id=query_response.run_id,
            lifecycle_state=query_response.lifecycle_state,
            projected_offset=query_response.projected_offset,
            waiting_external=query_response.waiting_external,
            recovery_mode=query_response.recovery_mode,
            recovery_reason=query_response.recovery_reason,
            active_child_runs_count=len(query_response.active_child_runs),
            correlation_hint=self._build_dashboard_correlation_hint(query_response),
        )

    async def spawn_child_run(self, request: SpawnChildRunRequest) -> SpawnChildRunResponse:
        """Create one child run through the substrate gateway.

        Args:
            request: Child run creation request.

        Returns:
            A child run response that hides substrate handles.

        """
        # Inherit parent's context binding into the child request so the child
        # run has access to the same workspace context without requiring the
        # caller to manually thread binding_ref through spawn requests.
        request_for_gateway = request
        if self._context_adapter is not None and request.context_ref is None:
            parent_binding = self._context_adapter.resolve_run_context(request.parent_run_id)
            if parent_binding is not None:
                request_for_gateway = replace(request, context_ref=parent_binding)

        # Inherit parent's policy versions into the child run when requested.
        if request.inherit_policy_versions:
            try:
                parent_resp = await self.query_run(QueryRunRequest(run_id=request.parent_run_id))
                parent_pvs = parent_resp.policy_versions
                if parent_pvs is not None:
                    # Apply selective overrides on top of inherited versions.
                    if request.policy_version_overrides:
                        inherited = {
                            k: v
                            for k, v in {
                                "route_policy_version": parent_pvs.route_policy_version,
                                "acceptance_policy_version": parent_pvs.acceptance_policy_version,
                                "memory_policy_version": parent_pvs.memory_policy_version,
                                "skill_policy_version": parent_pvs.skill_policy_version,
                                "evaluation_policy_version": parent_pvs.evaluation_policy_version,
                                "task_view_policy_version": parent_pvs.task_view_policy_version,
                            }.items()
                            if v is not None
                        }
                        inherited.update(request.policy_version_overrides)
                        child_pvs = RunPolicyVersions(**inherited)
                    else:
                        child_pvs = parent_pvs
                    # Thread inherited policy versions into the gateway request
                    # via input_json so the child workflow receives them at start.
                    child_input = dict(request_for_gateway.input_json or {})
                    child_input["_inherited_policy_versions"] = {
                        "route_policy_version": child_pvs.route_policy_version,
                        "acceptance_policy_version": child_pvs.acceptance_policy_version,
                        "memory_policy_version": child_pvs.memory_policy_version,
                        "skill_policy_version": child_pvs.skill_policy_version,
                        "evaluation_policy_version": child_pvs.evaluation_policy_version,
                        "task_view_policy_version": child_pvs.task_view_policy_version,
                    }
                    request_for_gateway = replace(request_for_gateway, input_json=child_input)
            except Exception:  # pylint: disable=broad-exception-caught
                pass  # Best-effort: proceed without inheritance on failure.

        workflow = await self._workflow_gateway.start_child_workflow(
            request.parent_run_id,
            request_for_gateway,
        )
        child_run_id = str(workflow.get("run_id") or workflow.get("workflow_id", ""))
        # Minimal closed-loop behavior: notify parent run projection that a new
        # child has been created. The parent workflow converts this signal into
        # a replayable runtime event, so projection can track active children.
        await self._workflow_gateway.signal_workflow(
            request.parent_run_id,
            SignalRunRequest(
                run_id=request.parent_run_id,
                signal_type="child_spawned",
                signal_payload={
                    "child_run_id": child_run_id,
                    **self._build_signal_observability(
                        operation="spawn_child",
                        run_id=request.parent_run_id,
                        caused_by=None,
                    ),
                },
                caused_by="kernel_facade.spawn_child_run",
            ),
        )
        lifecycle_state = await self._resolve_child_lifecycle_state(child_run_id)
        return SpawnChildRunResponse(
            child_run_id=child_run_id,
            lifecycle_state=lifecycle_state,
        )

    async def _resolve_child_lifecycle_state(self, child_run_id: str) -> str:
        """Best-effort resolves child lifecycle state from projection query."""
        try:
            projection = await self._workflow_gateway.query_projection(child_run_id)
        except Exception:  # pylint: disable=broad-exception-caught
            return "created"
        lifecycle_state = getattr(projection, "lifecycle_state", None)
        if isinstance(lifecycle_state, str) and lifecycle_state != "":
            return lifecycle_state
        return "created"

    def get_manifest(self) -> KernelManifest:
        """Return a frozen capability declaration for this kernel instance.

        Aggregates ``KERNEL_ACTION_TYPE_REGISTRY``, ``KERNEL_EVENT_REGISTRY``,
        and ``KERNEL_RECOVERY_MODE_REGISTRY`` into a single
        ``KernelManifest`` snapshot.  Platform integrators call this once at
        startup to detect which features the kernel supports.

        Returns:
            Immutable ``KernelManifest`` describing kernel capabilities.

        """
        return KernelManifest(
            kernel_version=self._kernel_version,
            protocol_version=_PROTOCOL_VERSION,
            supported_action_types=KERNEL_ACTION_TYPE_REGISTRY.known_types(),
            supported_interaction_targets=_INTERACTION_TARGETS,
            supported_recovery_modes=frozenset(KERNEL_RECOVERY_MODE_REGISTRY.known_actions()),
            supported_governance_features=_GOVERNANCE_FEATURES,
            supported_event_types=KERNEL_EVENT_REGISTRY.known_types(),
            substrate_type=self._substrate_type,
            substrate_limitations=_substrate_limitations(self._substrate_type),
            supported_trace_features=frozenset(
                {
                    "run_policy_version_freeze",
                    "trace_runtime_view",
                    "task_view_record",
                    "task_view_late_bind",
                    "branch_lifecycle",
                    "stage_lifecycle",
                    "human_gate",
                    "action_state_query",
                    "trace_failure_code",
                    "side_effect_class",
                    "evolve_postmortem",
                    "child_run_orchestration",
                }
            ),
        )

    async def execute_turn(
        self,
        run_id: str,
        action: Action,
        handler: AsyncActionHandler,
        *,
        idempotency_key: str,
    ) -> TurnResult:
        """Execute one atomic turn through the substrate gateway.

        This is the primary execution primitive for hi-agent's AsyncTaskScheduler.
        Kernel guarantees exactly-once execution via idempotency_key.

        The LocalFSM substrate executes the handler in-process.
        The Temporal substrate raises NotImplementedError until full Activity
        integration is implemented.

        Args:
            run_id: Target run identifier.
            action: The action being dispatched.
            handler: Async callable provided by hi-agent business logic.
            idempotency_key: Stable key for exactly-once execution guarantee.

        Returns:
            TurnResult with outcome_kind="dispatched".

        """
        async with self._guard_write_operation("execute_turn"):
            return await self._workflow_gateway.execute_turn(
                run_id,
                action,
                handler,
                idempotency_key=idempotency_key,
            )

    async def subscribe_events(
        self,
        run_id: str,
    ) -> AsyncIterator[RuntimeEvent]:
        """Stream events for a run. Protocol-compatible alias for stream_run_events().

        Allows MockKernelFacade and real KernelFacade to implement the same
        protocol so hi-agent's AsyncTaskScheduler can use either without
        code changes.

        Args:
            run_id: Target run identifier.

        Yields:
            RuntimeEvent instances as they are emitted.

        """
        async for event in self.stream_run_events(run_id):
            yield event

    async def submit_approval(self, request: ApprovalRequest) -> None:
        """Submit a human actor approval decision for a gated action.

        Routes the approval through the standard signal pathway so it is
        recorded as a replayable authoritative fact in the event log.

        Duplicate submissions for the same ``(run_id, approval_ref)`` pair are
        silently dropped by the in-process dedup gate.  This prevents accidental
        double-submit from the same facade instance (e.g. UI retry).

        Args:
            request: Typed approval request from the human review interface.

        """
        async with self._guard_write_operation("submit_approval"):
            dedup_key = (request.run_id, request.approval_ref)
            with self._approvals_lock:
                if dedup_key in self._submitted_approvals:
                    return
                self._submitted_approvals.add(dedup_key)
            # Mark the gate as resolved so query_trace_runtime() can derive
            # review_state = "completed" instead of "pending".
            with self._human_gate_lock:
                self._resolved_human_gates.setdefault(request.run_id, set()).add(
                    request.approval_ref
                )
            await self._workflow_gateway.signal_workflow(
                request.run_id,
                SignalRunRequest(
                    run_id=request.run_id,
                    signal_type="approval_submitted",
                    signal_payload={
                        "approval_ref": request.approval_ref,
                        "approved": request.approved,
                        "reviewer_id": request.reviewer_id,
                        "reason": request.reason,
                        **self._build_signal_observability(
                            operation="submit_approval",
                            run_id=request.run_id,
                            caused_by=request.caused_by,
                        ),
                    },
                    caused_by=request.caused_by or f"approval:{request.approval_ref}",
                ),
            )

    def get_health(self) -> dict[str, Any]:
        """Return a liveness health status for this kernel instance.

        Delegates to the injected ``KernelHealthProbe`` when available.
        Returns a minimal static ``{"status": "ok"}`` payload otherwise,
        which is safe for environments without a configured health probe
        (e.g. tests, minimal local setups).

        Returns:
            Dict with at minimum ``{"status": "ok" | "degraded" | "unhealthy"}``.

        """
        if self._health_probe is not None:
            probe_result = getattr(self._health_probe, "liveness", None)
            if callable(probe_result):
                if inspect.iscoroutinefunction(probe_result):
                    raise TypeError(
                        "KernelHealthProbe.liveness must be synchronous; got an async method. "
                        "Wrap it in a sync adapter before injecting."
                    )
                return probe_result()  # type: ignore[no-any-return]
        return {"status": "ok", "substrate": self._substrate_type}

    def get_health_readiness(self) -> dict[str, Any]:
        """Return a readiness health status for this kernel instance.

        Readiness is a stricter gate than liveness: the kernel is ready only
        when all backing stores (event log, dedupe store) are available and
        responding within expected latency bounds.

        Delegates to the injected ``KernelHealthProbe.readiness()`` when
        available.  Falls back to the same minimal static response as
        ``get_health()`` when no probe is injected 鈥?callers should treat
        the absence of a probe as ``"ready"`` for PoC/test environments.

        Returns:
            Dict with at minimum ``{"status": "ok" | "degraded" | "unhealthy"}``.

        """
        if self._health_probe is not None:
            probe_result = getattr(self._health_probe, "readiness", None)
            if callable(probe_result):
                if inspect.iscoroutinefunction(probe_result):
                    raise TypeError(
                        "KernelHealthProbe.readiness must be synchronous; got an async method. "
                        "Wrap it in a sync adapter before injecting."
                    )
                return probe_result()  # type: ignore[no-any-return]
        return {"status": "ok", "substrate": self._substrate_type}

    def register_task(self, descriptor: TaskDescriptor) -> None:
        """Register a new task descriptor in the task registry.

        Convenience wrapper that delegates to the injected ``TaskRegistry``.
        Callers must supply a ``task_registry`` at construction time.

        Args:
            descriptor: Task descriptor to register.  ``task_id`` must be
                unique within the registry.

        Raises:
            RuntimeError: If no ``task_registry`` was provided at construction.
            ValueError: If ``task_id`` is already registered.

        """
        if self._task_registry is None:
            raise RuntimeError(
                "register_task() requires a task_registry injected at KernelFacade construction."
            )
        self._task_registry.register(descriptor)

    def get_task_status(self, task_id: str) -> TaskHealthStatus | None:
        """Return the current health snapshot for a task.

        Delegates to the injected ``TaskRegistry.get_health()``.

        Args:
            task_id: Task identifier to look up.

        Returns:
            ``TaskHealthStatus`` snapshot, or ``None`` if not found.

        Raises:
            RuntimeError: If no ``task_registry`` was provided at construction.

        """
        if self._task_registry is None:
            raise RuntimeError(
                "get_task_status() requires a task_registry injected at KernelFacade construction."
            )
        return self._task_registry.get_health(task_id)

    def list_session_tasks(self, session_id: str) -> list[TaskDescriptor]:
        """Return all task descriptors registered for a session.

        Delegates to the injected ``TaskRegistry.list_session_tasks()``.

        Args:
            session_id: Session identifier to query.

        Returns:
            List of ``TaskDescriptor`` objects in registration order.

        Raises:
            RuntimeError: If no ``task_registry`` was provided at construction.

        """
        if self._task_registry is None:
            raise RuntimeError(
                "list_session_tasks() requires a task_registry injected at "
                "KernelFacade construction."
            )
        return self._task_registry.list_session_tasks(session_id)

    # ------------------------------------------------------------------
    # TRACE alignment methods (Gap A-I from hi-agent architecture review)
    # ------------------------------------------------------------------

    async def query_trace_runtime(self, run_id: str) -> TraceRuntimeView:
        """Return a TRACE-facing aggregated runtime view for one run.

        Builds a ``TraceRuntimeView`` from the kernel's ``RunProjection`` plus
        the in-memory branch registry.  The branch list reflects calls to
        ``open_branch()`` and ``mark_branch_*()`` since this facade instance
        was created.

        Args:
            run_id: Target run identifier.

        Returns:
            ``TraceRuntimeView`` derived from the current run projection.

        Raises:
            ValueError: If the run is not found.

        """
        import datetime

        response = await self.query_run(QueryRunRequest(run_id=run_id))
        # Map kernel lifecycle state to TRACE run state.
        _lifecycle_to_trace: dict[str, str] = {
            "created": "created",
            "running": "active",
            "waiting_callback": "waiting",
            "waiting_human": "waiting",
            "completed": "completed",
            "failed": "failed",
            "cancelled": "aborted",
        }
        run_state = _lifecycle_to_trace.get(response.lifecycle_state, "active")
        wait_state: str = "none"
        if response.waiting_external:
            wait_state = "external_callback"
        with self._human_gate_lock:
            open_gates = self._open_human_gates.get(run_id, set())
            resolved_gates = self._resolved_human_gates.get(run_id, set())
        if open_gates:
            unresolved = open_gates - resolved_gates
            review_state: str = "pending" if unresolved else "completed"
        else:
            review_state = "not_required"

        with self._branch_lock:
            branches = list((self._branch_registry.get(run_id) or {}).values())

        with self._stage_lock:
            stages = list((self._stage_registry.get(run_id) or {}).values())

        return TraceRuntimeView(
            run_id=run_id,
            run_state=run_state,  # type: ignore[arg-type]
            wait_state=wait_state,  # type: ignore[arg-type]
            review_state=review_state,  # type: ignore[arg-type]
            active_stage_id=response.active_stage_id,
            branches=branches,
            policy_versions=response.policy_versions,
            projected_at=datetime.datetime.now(datetime.UTC).isoformat(),
            stages=stages,
        )

    async def query_run_postmortem(self, run_id: str) -> RunPostmortemView:
        """Aggregate run data for post-run analysis by hi-agent evolve.

        Scans the run's event log to aggregate action counts, failure codes,
        timestamps, and human gate outcomes.  hi-agent's evolve layer enriches
        this with task_family, quality_score, efficiency_score, and
        trajectory_summary which are hi-agent-owned semantics.

        Args:
            run_id: Target run identifier.

        Returns:
            ``RunPostmortemView`` with aggregated run metrics.

        Raises:
            ValueError: If the run is not found.

        """
        import datetime

        response = await self.query_run(QueryRunRequest(run_id=run_id))
        trace = await self.query_trace_runtime(run_id)

        # Aggregate failure codes from stages and branches.
        failure_codes: list[str] = []
        for stage in trace.stages:
            if stage.failure_code:
                failure_codes.append(stage.failure_code)
        for branch in trace.branches:
            if branch.failure_code:
                failure_codes.append(branch.failure_code)

        # Aggregate human gate resolutions.
        gate_resolutions: list[HumanGateResolution] = []
        with self._human_gate_lock:
            resolved = self._resolved_human_gates.get(run_id, set())
            for gate_ref in resolved:
                gate_resolutions.append(
                    HumanGateResolution(
                        gate_ref=gate_ref,
                        gate_type="final_approval",
                        resolution="approved",
                    )
                )

        # Count dispatched actions via dedupe store.
        total_action_count = 0
        if self._dedupe_store is not None:
            with contextlib.suppress(NotImplementedError):
                total_action_count = self._dedupe_store.count_by_run(run_id)

        # Derive timing.
        # NOTE: The facade does not hold a reference to the event log, so we
        # cannot derive created_at / completed_at from the first/last event
        # timestamps.  We fall back to the current wall-clock time.  A future
        # enhancement could accept an optional event_log dependency or extend
        # TemporalWorkflowGateway with an event-timestamp query.
        now = datetime.datetime.now(datetime.UTC).isoformat()
        created_at = now
        completed_at: str | None = None
        if trace.run_state in ("completed", "failed", "aborted"):
            completed_at = now
        duration_ms = 0

        # task_id: QueryRunResponse does not carry task_id or
        # task_contract_ref.  Best-effort extraction via getattr so callers
        # that extend the response (e.g. hi-agent) still propagate the value.
        task_id: str | None = getattr(response, "task_contract_ref", None) or getattr(
            response, "task_id", None
        )

        # run_kind: QueryRunResponse does not have a run_kind field.
        # Default to "default"; callers may enrich post-hoc.
        run_kind: str = getattr(response, "run_kind", "default")

        return RunPostmortemView(
            run_id=run_id,
            task_id=task_id,
            run_kind=run_kind,
            outcome=trace.run_state,
            stages=list(trace.stages),
            branches=list(trace.branches),
            total_action_count=total_action_count,
            failure_codes=failure_codes,
            duration_ms=duration_ms,
            human_gate_resolutions=gate_resolutions,
            policy_versions=trace.policy_versions,
            event_count=response.projected_offset,
            created_at=created_at,
            completed_at=completed_at,
        )

    async def query_child_runs(self, parent_run_id: str) -> list[ChildRunSummary]:
        """Return summary status of all child runs spawned by a parent.

        Queries the parent run's projection for active_child_runs, then
        resolves each child's current lifecycle state and outcome.

        Args:
            parent_run_id: The parent run identifier.

        Returns:
            List of ``ChildRunSummary`` for each known child run.

        """
        import datetime

        response = await self.query_run(QueryRunRequest(run_id=parent_run_id))
        child_run_ids = response.active_child_runs

        summaries: list[ChildRunSummary] = []
        for child_id in child_run_ids:
            try:
                child_resp = await self.query_run(QueryRunRequest(run_id=child_id))
                # Map lifecycle to trace outcome.
                _terminal = {"completed", "failed", "aborted"}
                outcome = None
                if child_resp.lifecycle_state in _terminal:
                    outcome = child_resp.lifecycle_state

                # child_kind: We do not have per-child metadata stored at
                # the kernel level; default to "child".  hi-agent may enrich
                # this with task-specific kinds post-hoc.
                # task_id: Same limitation as query_run_postmortem — the
                # QueryRunResponse does not expose task_id.
                # created_at / completed_at: The facade lacks event-log
                # access, so we use wall-clock time as a placeholder.
                child_task_id: str | None = getattr(
                    child_resp, "task_contract_ref", None
                ) or getattr(child_resp, "task_id", None)
                child_now = datetime.datetime.now(datetime.UTC).isoformat()
                child_completed: str | None = child_now if outcome else None
                summaries.append(
                    ChildRunSummary(
                        child_run_id=child_id,
                        child_kind="child",
                        task_id=child_task_id,
                        lifecycle_state=child_resp.lifecycle_state,
                        outcome=outcome,
                        created_at=child_now,
                        completed_at=child_completed,
                    )
                )
            except Exception:  # pylint: disable=broad-exception-caught
                summaries.append(
                    ChildRunSummary(
                        child_run_id=child_id,
                        child_kind="child",
                        task_id=None,
                        lifecycle_state="created",
                        outcome=None,
                        created_at=datetime.datetime.now(datetime.UTC).isoformat(),
                        completed_at=None,
                    )
                )
        return summaries

    def record_task_view(self, record: TaskViewRecord) -> str:
        """Persist a TaskViewRecord to the injected task_view_log store.

        Args:
            record: The task view record to persist.

        Returns:
            The ``task_view_id`` of the persisted record.

        Raises:
            RuntimeError: If no ``task_view_log`` was provided at construction.

        """
        if self._task_view_log is None:
            raise RuntimeError(
                "record_task_view() requires a task_view_log injected at KernelFacade construction."
            )
        self._task_view_log.write(record)
        return record.task_view_id

    def get_task_view_record(self, task_view_id: str) -> TaskViewRecord | None:
        """Return a TaskViewRecord by its task_view_id.

        Args:
            task_view_id: Identifier to look up.

        Returns:
            Matching ``TaskViewRecord``, or ``None`` if not found.

        Raises:
            RuntimeError: If no ``task_view_log`` was provided at construction.

        """
        if self._task_view_log is None:
            raise RuntimeError(
                "get_task_view_record() requires a task_view_log injected at "
                "KernelFacade construction."
            )
        return self._task_view_log.get_by_id(task_view_id)

    def get_task_view_by_decision(self, run_id: str, decision_ref: str) -> TaskViewRecord | None:
        """Return the latest TaskViewRecord for one run + decision_ref pair.

        Args:
            run_id: Target run identifier.
            decision_ref: Decision reference to look up.

        Returns:
            Matching ``TaskViewRecord``, or ``None`` if not found.

        Raises:
            RuntimeError: If no ``task_view_log`` was provided at construction.

        """
        if self._task_view_log is None:
            raise RuntimeError(
                "get_task_view_by_decision() requires a task_view_log injected at "
                "KernelFacade construction."
            )
        return self._task_view_log.get_by_decision(run_id, decision_ref)

    def bind_task_view_to_decision(self, task_view_id: str, decision_ref: str) -> None:
        """Binds a persisted TaskViewRecord to its resulting decision_ref.

        Delegates to the injected task_view_log's ``bind_to_decision()`` method.
        Call this after the model responds to late-bind the decision reference.

        Args:
            task_view_id: The task view record identifier to update.
            decision_ref: Decision reference from TurnIntentRecord.intent_commit_ref.

        Raises:
            RuntimeError: If no ``task_view_log`` was provided at construction.

        """
        if self._task_view_log is None:
            raise RuntimeError(
                "bind_task_view_to_decision() requires a task_view_log injected at "
                "KernelFacade construction."
            )
        self._task_view_log.bind_to_decision(task_view_id, decision_ref)

    async def open_stage(self, stage_id: str, run_id: str, branch_id: str | None = None) -> None:
        """Open a new TRACE stage within a run.

        Records the stage in the in-memory stage registry and forwards a
        ``stage_opened`` signal to the workflow for durable event logging.

        Args:
            stage_id: Stage identifier (e.g. ``"route"``, ``"capture"``).
            run_id: Target run identifier.
            branch_id: Optional branch this stage belongs to.

        """
        import datetime

        view = TraceStageView(
            stage_id=stage_id,
            state="active",  # type: ignore[arg-type]
            entered_at=datetime.datetime.now(datetime.UTC).isoformat(),
            branch_id=branch_id,
        )
        with self._stage_lock:
            self._stage_registry.setdefault(run_id, {})[stage_id] = view
        await self._workflow_gateway.signal_run(
            SignalRunRequest(
                run_id=run_id,
                signal_type="stage_opened",
                signal_payload={
                    "stage_id": stage_id,
                    "branch_id": branch_id,
                },
                caused_by="kernel_facade.open_stage",
            )
        )

    async def mark_stage_state(
        self,
        run_id: str,
        stage_id: str,
        new_state: TraceStageState,
        failure_code: str | None = None,
    ) -> None:
        """Update a stage's lifecycle state.

        Updates the in-memory stage registry and forwards a
        ``stage_state_updated`` signal to the workflow.

        Args:
            run_id: Target run identifier.
            stage_id: Stage identifier to update.
            new_state: New ``TraceStageState`` value.
            failure_code: Optional failure code when transitioning to ``"failed"``.

        Raises:
            KeyError: If the stage_id is not found for this run.

        """
        import dataclasses
        import datetime

        with self._stage_lock:
            run_stages = self._stage_registry.get(run_id, {})
            existing = run_stages.get(stage_id)
            if existing is None:
                raise KeyError(f"Stage {stage_id!r} not found for run {run_id!r}.")
            exited_at: str | None = None
            if new_state in ("completed", "failed"):
                exited_at = datetime.datetime.now(datetime.UTC).isoformat()
            updated = dataclasses.replace(
                existing,
                state=new_state,
                exited_at=exited_at if exited_at else existing.exited_at,
                failure_code=failure_code if failure_code is not None else existing.failure_code,
            )
            run_stages[stage_id] = updated
        await self._workflow_gateway.signal_run(
            SignalRunRequest(
                run_id=run_id,
                signal_type="stage_state_updated",
                signal_payload={
                    "stage_id": stage_id,
                    "new_state": new_state,
                    "failure_code": failure_code,
                },
                caused_by="kernel_facade.mark_stage_state",
            )
        )

    async def open_branch(self, request: OpenBranchRequest) -> None:
        """Open a new trajectory branch within a run.

        Records the branch in the in-memory branch registry and forwards a
        ``branch_opened`` signal to the workflow for durable event logging.

        Args:
            request: Branch open request.

        """
        import datetime

        view = TraceBranchView(
            branch_id=request.branch_id,
            stage_id=request.stage_id,
            state="active",  # type: ignore[arg-type]
            opened_at=datetime.datetime.now(datetime.UTC).isoformat(),
            parent_branch_id=request.parent_branch_id,
            proposed_by=request.proposed_by,
        )
        with self._branch_lock:
            self._branch_registry.setdefault(request.run_id, {})[request.branch_id] = view
        await self._workflow_gateway.signal_run(
            SignalRunRequest(
                run_id=request.run_id,
                signal_type="branch_opened",
                signal_payload={
                    "branch_id": request.branch_id,
                    "stage_id": request.stage_id,
                    "parent_branch_id": request.parent_branch_id,
                    "proposed_by": request.proposed_by,
                },
                caused_by="kernel_facade.open_branch",
            )
        )

    async def mark_branch_state(self, request: BranchStateUpdateRequest) -> None:
        """Update a branch's lifecycle state.

        Updates the in-memory branch registry and forwards a
        ``branch_state_updated`` signal to the workflow.

        Args:
            request: Branch state update request.

        Raises:
            KeyError: If the branch_id is not found for this run.

        """
        with self._branch_lock:
            run_branches = self._branch_registry.get(request.run_id, {})
            existing = run_branches.get(request.branch_id)
            if existing is None:
                raise KeyError(
                    f"Branch {request.branch_id!r} not found for run {request.run_id!r}."
                )
            import dataclasses

            updated = dataclasses.replace(
                existing,
                state=request.new_state,
                failure_code=str(request.failure_code.value)
                if request.failure_code
                else existing.failure_code,
            )
            run_branches[request.branch_id] = updated
        await self._workflow_gateway.signal_run(
            SignalRunRequest(
                run_id=request.run_id,
                signal_type="branch_state_updated",
                signal_payload={
                    "branch_id": request.branch_id,
                    "new_state": request.new_state,
                    "failure_code": request.failure_code.value if request.failure_code else None,
                    "reason": request.reason,
                },
                caused_by="kernel_facade.mark_branch_state",
            )
        )

    async def open_human_gate(self, request: HumanGateRequest) -> None:
        """Open a human review gate within a run.

        Forwards a ``human_gate_opened`` signal to the workflow for durable
        event logging.  The workflow records it as an ``authoritative_fact``
        and blocks auto-resume while review_state != "approved".

        Args:
            request: Human gate open request.

        """
        with self._human_gate_lock:
            self._open_human_gates.setdefault(request.run_id, set()).add(request.gate_ref)
        await self._workflow_gateway.signal_run(
            SignalRunRequest(
                run_id=request.run_id,
                signal_type="human_gate_opened",
                signal_payload={
                    "gate_ref": request.gate_ref,
                    "gate_type": request.gate_type,
                    "trigger_reason": request.trigger_reason,
                    "trigger_source": request.trigger_source,
                    "stage_id": request.stage_id,
                    "branch_id": request.branch_id,
                    "artifact_ref": request.artifact_ref,
                    "caused_by": request.caused_by,
                },
                caused_by="kernel_facade.open_human_gate",
            )
        )

    def get_action_state(self, dispatch_idempotency_key: str) -> str | None:
        """Return the current dedupe state for an action's idempotency key.

        Args:
            dispatch_idempotency_key: Dispatch idempotency key to look up.

        Returns:
            State string (e.g. ``"reserved"``, ``"dispatched"``,
            ``"acknowledged"``, ``"succeeded"``, ``"unknown_effect"``), or
            ``None`` if the key is unknown or no dedupe_store was injected.

        """
        if self._dedupe_store is None:
            return None
        record = self._dedupe_store.get(dispatch_idempotency_key)
        return record.state if record is not None else None

    @staticmethod
    def _is_expected_cancel_race_error(error: Exception) -> bool:
        """Return whether cancel failure is expected due to completion race."""
        message = str(error).lower()
        return any(
            token in message
            for token in ("not found", "already completed", "not running", "closed")
        )


def _serialize_action(action: Action) -> dict[str, Any]:
    """Serialize one Action to signal-safe plain JSON."""
    return {
        "action_id": action.action_id,
        "run_id": action.run_id,
        "action_type": action.action_type,
        "effect_class": action.effect_class,
        "external_idempotency_level": action.external_idempotency_level,
        "interaction_target": action.interaction_target,
        "input_ref": action.input_ref,
        "input_json": action.input_json,
        "policy_tags": list(action.policy_tags),
        "timeout_ms": action.timeout_ms,
        "side_effect_class": action.side_effect_class,
    }
