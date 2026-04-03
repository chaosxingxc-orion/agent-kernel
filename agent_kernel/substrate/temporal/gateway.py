"""Temporal workflow gateway implementation for the kernel facade.

Design principles:
  - Temporal is treated as *durable runtime substrate* only.
  - Kernel semantics (lifecycle truth, projection truth,
    recovery truth) stay in kernel contracts and services.
  - This gateway only translates kernel-safe requests into
    Temporal SDK calls.

Why this module exists:
  - ``KernelFacade`` depends on ``TemporalWorkflowGateway``
    abstraction.
  - The abstraction allows deterministic unit tests against mocks.
  - The concrete implementation is isolated, so SDK-specific
    behavior does not leak into domain modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

from agent_kernel.kernel.contracts import (
    QueryRunResponse,
    RunProjection,
    RuntimeEvent,
    SignalRunRequest,
    SpawnChildRunRequest,
    StartRunRequest,
    TemporalWorkflowGateway,
)
from agent_kernel.substrate.temporal.run_actor_workflow import (
    RunActorWorkflow,
    RunInput,
)


@dataclass(frozen=True, slots=True)
class TemporalGatewayConfig:
    """Configuration for workflow id composition and task routing.

    Attributes:
        task_queue: Temporal task queue where kernel workflow workers listen.
        workflow_id_prefix: Prefix used to build workflow ids from run ids.
        child_workflow_suffix: Suffix namespace used for child workflow ids.
        workflow_run_callable: Workflow entry callable for start_workflow.
        signal_method_name: Workflow signal method name used by handles.
        query_method_name: Workflow query method name used by handles.
        event_stream_query_method_name: Optional workflow query method used to
            fetch runtime event stream payloads.
    """

    task_queue: str = "agent-kernel"
    workflow_id_prefix: str = "run"
    child_workflow_suffix: str = "child"
    workflow_run_callable: Any = RunActorWorkflow.run
    signal_method_name: str = "signal"
    query_method_name: str = "query"
    event_stream_query_method_name: str | None = None


class TemporalSDKWorkflowGateway(TemporalWorkflowGateway):
    """Concrete ``TemporalWorkflowGateway`` based on Temporal Python SDK.

    Contract boundary:
      - Input and output remain kernel DTOs.
      - Temporal handles/history objects are never returned to callers.
    """

    def __init__(
        self,
        temporal_client: Any,
        config: TemporalGatewayConfig | None = None,
    ) -> None:
        """Initializes gateway with Temporal client and optional config.

        Args:
            temporal_client: Temporal SDK client instance.
            config: Configuration for workflow id composition and task routing.
        """
        self._client = temporal_client
        self._config = config or TemporalGatewayConfig()

    async def start_workflow(self, request: StartRunRequest) -> dict[str, str]:
        """Starts one run workflow and returns a facade-safe workflow id.
        Args:
            request: (description)
        Returns:
            dict[str, str]: (description)
        """
        run_id = self._build_run_id(request)
        workflow_id = self._workflow_id_for_run(run_id)
        await self._client.start_workflow(
            self._config.workflow_run_callable,
            RunInput(
                run_id=run_id,
                session_id=request.session_id,
                parent_run_id=request.parent_run_id,
                input_ref=request.input_ref,
                input_json=request.input_json,
                runtime_context_ref=request.context_ref,
            ),
            id=workflow_id,
            task_queue=self._config.task_queue,
        )
        return {"workflow_id": workflow_id, "run_id": run_id}

    async def signal_workflow(
        self,
        run_id: str,
        signal: SignalRunRequest,
    ) -> None:
        """Routes one kernel signal into the Temporal workflow.
        Args:
            run_id: (description)
            signal: (description)
        """
        handle = self._client.get_workflow_handle(
            self._workflow_id_for_run(run_id),
        )
        await handle.signal(
            self._config.signal_method_name,
            {
                "signal_type": signal.signal_type,
                "signal_payload": signal.signal_payload,
                "caused_by": signal.caused_by,
            },
        )

    async def cancel_workflow(
        self,
        run_id: str,
        reason: str,
    ) -> None:
        """Cancels one run workflow.

        Compatibility note:
        Some Temporal Python SDK versions accept
        ``cancel(reason=...)``, while others expose ``cancel()``
        only. The gateway keeps reasoned cancel as the preferred
        path and falls back to no-arg cancel for older SDKs.

        Args:
            run_id: (description)
            reason: (description)
        """
        handle = self._client.get_workflow_handle(
            self._workflow_id_for_run(run_id),
        )
        try:
            await handle.cancel(reason=reason)
        except TypeError:
            await handle.cancel()

    async def query_projection(self, run_id: str) -> RunProjection:
        """Queries projection from workflow and normalizes result shape.

        Args:
            run_id: Target run identifier.

        Returns:
            Normalized ``RunProjection`` snapshot.

        Raises:
            ValueError: If the query response shape is unexpected.
        """
        handle = self._client.get_workflow_handle(
            self._workflow_id_for_run(run_id),
        )
        projection_like = await handle.query(
            self._config.query_method_name,
        )
        if isinstance(projection_like, RunProjection):
            return projection_like
        if isinstance(projection_like, QueryRunResponse):
            return RunProjection(
                run_id=projection_like.run_id,
                lifecycle_state=projection_like.lifecycle_state,
                projected_offset=projection_like.projected_offset,
                waiting_external=projection_like.waiting_external,
                current_action_id=projection_like.current_action_id,
                recovery_mode=projection_like.recovery_mode,
                recovery_reason=projection_like.recovery_reason,
                active_child_runs=projection_like.active_child_runs,
                ready_for_dispatch=False,
            )
        if isinstance(projection_like, dict):
            return RunProjection(
                run_id=str(
                    projection_like.get("run_id", run_id),
                ),
                lifecycle_state=projection_like.get(
                    "lifecycle_state",
                    "created",
                ),
                projected_offset=int(
                    projection_like.get("projected_offset", 0),
                ),
                waiting_external=bool(
                    projection_like.get(
                        "waiting_external",
                        False,
                    ),
                ),
                current_action_id=projection_like.get(
                    "current_action_id",
                ),
                recovery_mode=projection_like.get(
                    "recovery_mode",
                ),
                recovery_reason=projection_like.get(
                    "recovery_reason",
                ),
                active_child_runs=list(
                    projection_like.get(
                        "active_child_runs",
                        [],
                    ),
                ),
                ready_for_dispatch=bool(
                    projection_like.get(
                        "ready_for_dispatch",
                        False,
                    ),
                ),
            )
        raise TypeError("Unsupported projection payload returned by Temporal query.")

    async def start_child_workflow(
        self,
        parent_run_id: str,
        request: SpawnChildRunRequest,
    ) -> dict[str, str]:
        """Starts one child run workflow linked to parent run id.
        Args:
            parent_run_id: (description)
            request: (description)
        Returns:
            dict[str, str]: (description)
        """
        child_run_id = request.input_json.get("child_run_id") if request.input_json else None
        if not isinstance(child_run_id, str) or not child_run_id:
            child_run_id = f"{parent_run_id}:{request.child_kind}"
        workflow_id = self._workflow_id_for_child(parent_run_id, child_run_id)
        await self._client.start_workflow(
            self._config.workflow_run_callable,
            RunInput(
                run_id=child_run_id,
                parent_run_id=parent_run_id,
                input_ref=request.input_ref,
                input_json=request.input_json,
            ),
            id=workflow_id,
            task_queue=self._config.task_queue,
        )
        return {"workflow_id": workflow_id, "run_id": child_run_id}

    def _build_run_id(
        self,
        request: StartRunRequest,
    ) -> str:
        """Resolves deterministic run id from request inputs.

        Args:
            request: Start run request with identity hints.

        Returns:
            Resolved run identifier string.
        """
        if request.input_json and isinstance(
            request.input_json.get("run_id"),
            str,
        ):
            return request.input_json["run_id"]
        if request.parent_run_id:
            return f"{request.parent_run_id}:{request.run_kind}"
        if request.session_id:
            return f"{request.session_id}:{request.run_kind}"
        return request.run_kind

    def _workflow_id_for_run(self, run_id: str) -> str:
        """Builds Temporal workflow id from run id.

        Args:
            run_id: Kernel run identifier.

        Returns:
            Temporal workflow id string with configured prefix.
        """
        return f"{self._config.workflow_id_prefix}:{run_id}"

    def _workflow_id_for_child(
        self,
        parent_run_id: str,
        child_run_id: str,
    ) -> str:
        return (
            f"{self._config.workflow_id_prefix}"
            f":{self._config.child_workflow_suffix}"
            f":{parent_run_id}:{child_run_id}"
        )

    def stream_run_events(self, run_id: str) -> AsyncIterator[RuntimeEvent]:
        """Streams runtime events for one run through optional query hook.

        Args:
            run_id: Target run identifier.

        Returns:
            Async iterator of ``RuntimeEvent`` instances.
        """
        return self._stream_run_events(run_id)

    async def _stream_run_events(self, run_id: str) -> AsyncIterator[RuntimeEvent]:
        """Yields runtime events returned by configured workflow query hook.

        If no stream query method is configured, the stream is empty.
        """
        query_name = self._config.event_stream_query_method_name
        if query_name is None:
            return
        handle = self._client.get_workflow_handle(
            self._workflow_id_for_run(run_id),
        )
        stream_payload = await handle.query(query_name)
        for event in self._normalize_event_stream_payload(run_id, stream_payload):
            yield event

    def _normalize_event_stream_payload(
        self,
        run_id: str,
        stream_payload: Any,
    ) -> list[RuntimeEvent]:
        """Normalizes stream query payload into ``RuntimeEvent`` objects."""
        if stream_payload is None:
            return []
        if isinstance(stream_payload, dict):
            stream_payload = stream_payload.get("events")
        if not isinstance(stream_payload, list):
            raise TypeError("Unsupported runtime event stream payload returned by Temporal query.")
        return [
            self._coerce_runtime_event(
                run_id=run_id,
                event_payload=event_payload,
            )
            for event_payload in stream_payload
        ]

    def _coerce_runtime_event(
        self,
        run_id: str,
        event_payload: Any,
    ) -> RuntimeEvent:
        """Coerces one stream payload item to ``RuntimeEvent``."""
        if isinstance(event_payload, RuntimeEvent):
            return event_payload
        if not isinstance(event_payload, dict):
            raise TypeError("Unsupported runtime event item returned by Temporal query.")
        payload_json = event_payload.get("payload_json")
        idempotency_key = event_payload.get("idempotency_key")
        payload_ref = event_payload.get("payload_ref")
        return RuntimeEvent(
            run_id=str(event_payload.get("run_id", run_id)),
            event_id=str(event_payload.get("event_id", "")),
            commit_offset=int(event_payload.get("commit_offset", 0)),
            event_type=str(event_payload.get("event_type", "")),
            event_class=str(event_payload.get("event_class", "fact")),
            event_authority=str(
                event_payload.get("event_authority", "authoritative_fact"),
            ),
            ordering_key=str(event_payload.get("ordering_key", run_id)),
            wake_policy=str(event_payload.get("wake_policy", "projection_only")),
            created_at=str(event_payload.get("created_at", "")),
            idempotency_key=str(idempotency_key) if isinstance(idempotency_key, str) else None,
            payload_ref=str(payload_ref) if isinstance(payload_ref, str) else None,
            payload_json=payload_json if isinstance(payload_json, dict) else None,
        )
