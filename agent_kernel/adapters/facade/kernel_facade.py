"""Defines the KernelFacade placeholder for the agent_kernel interface contract.

The facade is the only allowed entrypoint from the platform layer into the
kernel. It must export kernel-safe request and response objects without leaking
Temporal-specific substrate details.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

from agent_kernel.adapters.agent_core.checkpoint_adapter import AgentCoreResumeInput
from agent_kernel.adapters.agent_core.context_adapter import AgentCoreContextInput
from agent_kernel.kernel.action_type_registry import KERNEL_ACTION_TYPE_REGISTRY
from agent_kernel.kernel.contracts import (
    ApprovalRequest,
    CancelRunRequest,
    ExecutionPlan,
    KernelManifest,
    PlanSubmissionResponse,
    QueryRunDashboardResponse,
    QueryRunRequest,
    QueryRunResponse,
    ResumeRunRequest,
    RuntimeEvent,
    SignalRunRequest,
    SpawnChildRunRequest,
    SpawnChildRunResponse,
    StartRunRequest,
    StartRunResponse,
    TemporalWorkflowGateway,
)
from agent_kernel.kernel.event_registry import KERNEL_EVENT_REGISTRY
from agent_kernel.kernel.plan_type_registry import KERNEL_PLAN_TYPE_REGISTRY
from agent_kernel.kernel.recovery.mode_registry import KERNEL_RECOVERY_MODE_REGISTRY

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
    ) -> None:
        """Initializes facade with substrate gateway and optional adapters.

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
        """
        self._workflow_gateway = workflow_gateway
        self._context_adapter = context_adapter
        self._checkpoint_adapter = checkpoint_adapter
        self._health_probe = health_probe
        self._substrate_type = substrate_type
        self._kernel_version = kernel_version
        # Per-instance approval dedup gate: tracks (run_id, approval_ref) pairs
        # that have already been forwarded to the substrate.  Prevents duplicate
        # approval signals from the same facade instance (e.g. accidental double
        # submit from a UI).  Note: this is an in-memory gate scoped to one
        # facade instance — cross-process dedup lives in the event log.
        self._submitted_approvals: set[tuple[str, str]] = set()

    @staticmethod
    def _build_signal_observability(
        *,
        operation: str,
        run_id: str,
        caused_by: str | None,
    ) -> dict[str, str]:
        """Builds stable observability fields for facade-originated signals.

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
        """Builds a stable dashboard correlation hint from run query output.

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
        """Starts one run through the substrate gateway.

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
        """Returns whether one runtime event is exposed by facade stream contract.

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
        """Cancels one run through the substrate gateway.

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
            request:
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
        """Queries one run projection without exposing substrate internals.

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
        )

    async def query_run_dashboard(self, run_id: str) -> QueryRunDashboardResponse:
        """Queries one run and returns a dashboard-friendly read model.

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
        """Creates one child run through the substrate gateway.

        Args:
            request: Child run creation request.

        Returns:
            A child run response that hides substrate handles.

        """
        workflow = await self._workflow_gateway.start_child_workflow(
            request.parent_run_id,
            request,
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
        """Returns a frozen capability declaration for this kernel instance.

        Aggregates ``KERNEL_ACTION_TYPE_REGISTRY``, ``KERNEL_EVENT_REGISTRY``,
        ``KERNEL_RECOVERY_MODE_REGISTRY``, and ``KERNEL_PLAN_TYPE_REGISTRY``
        into a single ``KernelManifest`` snapshot.  Platform integrators call
        this once at startup to detect which features the kernel supports.

        Returns:
            Immutable ``KernelManifest`` describing kernel capabilities.
        """
        return KernelManifest(
            kernel_version=self._kernel_version,
            protocol_version=_PROTOCOL_VERSION,
            supported_plan_types=KERNEL_PLAN_TYPE_REGISTRY.known_types(),
            supported_action_types=KERNEL_ACTION_TYPE_REGISTRY.known_types(),
            supported_interaction_targets=_INTERACTION_TARGETS,
            supported_recovery_modes=frozenset(KERNEL_RECOVERY_MODE_REGISTRY.known_actions()),
            supported_governance_features=_GOVERNANCE_FEATURES,
            supported_event_types=KERNEL_EVENT_REGISTRY.known_types(),
            substrate_type=self._substrate_type,
            substrate_limitations=_substrate_limitations(self._substrate_type),
        )

    async def submit_plan(
        self,
        run_id: str,
        plan: ExecutionPlan,
    ) -> PlanSubmissionResponse:
        """Submits a typed ExecutionPlan to an active run.

        Validates the plan type against ``KERNEL_PLAN_TYPE_REGISTRY`` before
        signalling the workflow.  Unknown plan types are rejected immediately
        without reaching the substrate, giving platforms early feedback.

        Args:
            run_id: Target run identifier.
            plan: An ``ExecutionPlan`` instance (any registered subtype).

        Returns:
            ``PlanSubmissionResponse`` indicating acceptance or rejection.
        """
        # Normalise class name → registry key (e.g. ConditionalPlan → conditional)
        _type_map = {
            "sequentialplan": "sequential",
            "parallelplan": "parallel",
            "conditionalplan": "conditional",
            "dependencygraph": "dependency_graph",
            "speculativeplan": "speculative",
        }
        plan_type_key = _type_map.get(type(plan).__name__.lower(), type(plan).__name__)
        descriptor = KERNEL_PLAN_TYPE_REGISTRY.get(plan_type_key)
        if descriptor is None:
            return PlanSubmissionResponse(
                run_id=run_id,
                plan_type=plan_type_key,
                accepted=False,
                rejection_reason=(
                    f"Plan type '{plan_type_key}' is not registered in KERNEL_PLAN_TYPE_REGISTRY."
                ),
            )
        await self._workflow_gateway.signal_workflow(
            run_id,
            SignalRunRequest(
                run_id=run_id,
                signal_type="plan_submitted",
                signal_payload={
                    "plan_type": plan_type_key,
                    **self._build_signal_observability(
                        operation="submit_plan",
                        run_id=run_id,
                        caused_by=None,
                    ),
                },
                caused_by="kernel_facade.submit_plan",
            ),
        )
        return PlanSubmissionResponse(
            run_id=run_id,
            plan_type=plan_type_key,
            accepted=True,
        )

    async def submit_approval(self, request: ApprovalRequest) -> None:
        """Submits a human actor approval decision for a gated action.

        Routes the approval through the standard signal pathway so it is
        recorded as a replayable authoritative fact in the event log.

        Duplicate submissions for the same ``(run_id, approval_ref)`` pair are
        silently dropped by the in-process dedup gate.  This prevents accidental
        double-submit from the same facade instance (e.g. UI retry).

        Args:
            request: Typed approval request from the human review interface.
        """
        dedup_key = (request.run_id, request.approval_ref)
        if dedup_key in self._submitted_approvals:
            return
        self._submitted_approvals.add(dedup_key)
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

    async def commit_speculation(
        self,
        run_id: str,
        winner_candidate_id: str,
    ) -> None:
        """Signals which speculative candidate won and should be committed.

        The kernel uses this signal to promote the winning Child Workflow from
        ``speculative`` to ``committed`` mode and cancel all other candidates.

        Args:
            run_id: The run executing the ``SpeculativePlan``.
            winner_candidate_id: ``candidate_id`` of the winning
                ``SpeculativeCandidate``.
        """
        await self._workflow_gateway.signal_workflow(
            run_id,
            SignalRunRequest(
                run_id=run_id,
                signal_type="speculation_committed",
                signal_payload={
                    "winner_candidate_id": winner_candidate_id,
                    **self._build_signal_observability(
                        operation="commit_speculation",
                        run_id=run_id,
                        caused_by=None,
                    ),
                },
                caused_by="kernel_facade.commit_speculation",
            ),
        )

    def get_health(self) -> dict[str, Any]:
        """Returns a liveness health status for this kernel instance.

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
                return probe_result()  # type: ignore[no-any-return]
        return {"status": "ok", "substrate": self._substrate_type}

    def get_health_readiness(self) -> dict[str, Any]:
        """Returns a readiness health status for this kernel instance.

        Readiness is a stricter gate than liveness: the kernel is ready only
        when all backing stores (event log, dedupe store) are available and
        responding within expected latency bounds.

        Delegates to the injected ``KernelHealthProbe.readiness()`` when
        available.  Falls back to the same minimal static response as
        ``get_health()`` when no probe is injected — callers should treat
        the absence of a probe as ``"ready"`` for PoC/test environments.

        Returns:
            Dict with at minimum ``{"status": "ok" | "degraded" | "unhealthy"}``.
        """
        if self._health_probe is not None:
            probe_result = getattr(self._health_probe, "readiness", None)
            if callable(probe_result):
                return probe_result()  # type: ignore[no-any-return]
        return {"status": "ok", "substrate": self._substrate_type}

    @staticmethod
    def _is_expected_cancel_race_error(error: Exception) -> bool:
        """Returns whether cancel failure is expected due to completion race."""
        message = str(error).lower()
        return any(
            token in message
            for token in ("not found", "already completed", "not running", "closed")
        )
