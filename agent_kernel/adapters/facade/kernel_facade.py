"""Defines the KernelFacade placeholder for the agent_kernel interface contract.

The facade is the only allowed entrypoint from the platform layer into the
kernel. It must export kernel-safe request and response objects without leaking
Temporal-specific substrate details.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import replace
from typing import Any

from agent_kernel.adapters.agent_core.checkpoint_adapter import AgentCoreResumeInput
from agent_kernel.adapters.agent_core.context_adapter import AgentCoreContextInput
from agent_kernel.kernel.contracts import (
    CancelRunRequest,
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
    ) -> None:
        """Initializes facade with substrate gateway and optional adapters.

        Args:
            workflow_gateway: Gateway that speaks to the Temporal substrate.
            context_adapter: Optional context adapter for session binding.
            checkpoint_adapter: Optional checkpoint adapter for resume
                operations.
        """
        self._workflow_gateway = workflow_gateway
        self._context_adapter = context_adapter
        self._checkpoint_adapter = checkpoint_adapter

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
            Runtime events allowed by facade stream contract.
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
        """
        if self._checkpoint_adapter is None:
            raise RuntimeError(
                "checkpoint_adapter is required for resume_run."
            )
        kernel_resume_request = (
            await self._checkpoint_adapter.import_resume_request(
                AgentCoreResumeInput(
                    run_id=request.run_id,
                    snapshot_id=request.snapshot_id,
                ),
            )
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
        child_run_id = str(
            workflow.get("run_id")
            or workflow.get("workflow_id", "")
        )
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

    @staticmethod
    def _is_expected_cancel_race_error(error: Exception) -> bool:
        """Returns whether cancel failure is expected due to completion race."""
        message = str(error).lower()
        return any(
            token in message
            for token in ("not found", "already completed", "not running", "closed")
        )
