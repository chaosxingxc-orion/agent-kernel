"""Starlette HTTP service wrapping KernelFacade 1:1.

Provides cross-process access to all KernelFacade methods via REST endpoints.
SSE streaming for run events.  Designed for hi-agent integration.

Usage::

    from agent_kernel.service.http_server import create_app

    app = create_app(kernel_facade)
    uvicorn.run(app, host="0.0.0.0", port=8400)
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from agent_kernel.config import KernelConfig
from agent_kernel.kernel.contracts import QueryRunRequest
from agent_kernel.service.auth_middleware import ApiKeyMiddleware
from agent_kernel.service.serialization import (
    deserialize_approval,
    deserialize_branch_state_update,
    deserialize_cancel_run,
    deserialize_human_gate,
    deserialize_open_branch,
    deserialize_resume_run,
    deserialize_signal_run,
    deserialize_spawn_child_run,
    deserialize_start_run,
    deserialize_task_view,
    serialize_dataclass,
)

if TYPE_CHECKING:
    from agent_kernel.adapters.facade.kernel_facade import KernelFacade

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Error helpers
# ---------------------------------------------------------------------------


def _error(status: int, detail: str) -> JSONResponse:
    return JSONResponse({"error": detail}, status_code=status)


_MAX_REQUEST_BODY_BYTES = 1_048_576  # 1 MB — default; overridden via app.state


async def _json_body(request: Request) -> dict[str, Any]:
    """Parse request body as JSON, returning {} for empty bodies."""
    max_bytes: int = getattr(
        getattr(request, "app", None) and request.app.state,
        "max_body_bytes",
        _MAX_REQUEST_BODY_BYTES,
    )
    body = await request.body()
    if not body:
        return {}
    if len(body) > max_bytes:
        raise ValueError(f"request body too large: {len(body)} bytes (max {max_bytes})")
    return json.loads(body)


# ---------------------------------------------------------------------------
# Route handlers — each maps 1:1 to a KernelFacade method
# ---------------------------------------------------------------------------


async def post_runs(request: Request) -> JSONResponse:
    """POST /runs — start_run."""
    facade: KernelFacade = request.app.state.facade
    data = await _json_body(request)
    try:
        req = deserialize_start_run(data)
        resp = await facade.start_run(req)
        return JSONResponse(serialize_dataclass(resp), status_code=201)
    except Exception as exc:
        logger.exception("start_run failed")
        return _error(400, str(exc))


async def get_run(request: Request) -> JSONResponse:
    """GET /runs/{run_id} — query_run."""
    facade: KernelFacade = request.app.state.facade
    run_id = request.path_params["run_id"]
    try:
        resp = await facade.query_run(QueryRunRequest(run_id=run_id))
        return JSONResponse(serialize_dataclass(resp))
    except Exception as exc:
        logger.exception("query_run failed")
        return _error(404, str(exc))


async def get_run_dashboard(request: Request) -> JSONResponse:
    """GET /runs/{run_id}/dashboard — query_run_dashboard."""
    facade: KernelFacade = request.app.state.facade
    run_id = request.path_params["run_id"]
    try:
        resp = await facade.query_run_dashboard(run_id)
        return JSONResponse(serialize_dataclass(resp))
    except Exception as exc:
        logger.exception("query_run_dashboard failed")
        return _error(404, str(exc))


async def get_run_trace(request: Request) -> JSONResponse:
    """GET /runs/{run_id}/trace — query_trace_runtime."""
    facade: KernelFacade = request.app.state.facade
    run_id = request.path_params["run_id"]
    try:
        resp = await facade.query_trace_runtime(run_id)
        return JSONResponse(serialize_dataclass(resp))
    except Exception as exc:
        logger.exception("query_trace_runtime failed")
        return _error(404, str(exc))


async def get_run_postmortem(request: Request) -> JSONResponse:
    """GET /runs/{run_id}/postmortem — query_run_postmortem."""
    facade: KernelFacade = request.app.state.facade
    run_id = request.path_params["run_id"]
    try:
        resp = await facade.query_run_postmortem(run_id)
        return JSONResponse(serialize_dataclass(resp))
    except Exception as exc:
        logger.exception("query_run_postmortem failed")
        return _error(404, str(exc))


async def get_run_events(request: Request) -> Response:
    """GET /runs/{run_id}/events — stream_run_events (SSE)."""
    from starlette.responses import StreamingResponse

    facade: KernelFacade = request.app.state.facade
    run_id = request.path_params["run_id"]
    include_diagnostic = (
        request.query_params.get(
            "include_derived_diagnostic",
            "false",
        ).lower()
        == "true"
    )

    async def event_generator():
        try:
            async for event in facade.stream_run_events(
                run_id,
                include_derived_diagnostic=include_diagnostic,
            ):
                data = json.dumps(serialize_dataclass(event))
                yield f"data: {data}\n\n"
        except Exception as exc:
            logger.exception("stream_run_events failed")
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def post_run_signal(request: Request) -> JSONResponse:
    """POST /runs/{run_id}/signal — signal_run."""
    facade: KernelFacade = request.app.state.facade
    run_id = request.path_params["run_id"]
    data = await _json_body(request)
    try:
        req = deserialize_signal_run(run_id, data)
        await facade.signal_run(req)
        return JSONResponse({"ok": True})
    except Exception as exc:
        logger.exception("signal_run failed")
        return _error(400, str(exc))


async def post_run_cancel(request: Request) -> JSONResponse:
    """POST /runs/{run_id}/cancel — cancel_run."""
    facade: KernelFacade = request.app.state.facade
    run_id = request.path_params["run_id"]
    data = await _json_body(request)
    try:
        req = deserialize_cancel_run(run_id, data)
        await facade.cancel_run(req)
        return JSONResponse({"ok": True})
    except Exception as exc:
        logger.exception("cancel_run failed")
        return _error(400, str(exc))


async def post_run_resume(request: Request) -> JSONResponse:
    """POST /runs/{run_id}/resume — resume_run."""
    facade: KernelFacade = request.app.state.facade
    run_id = request.path_params["run_id"]
    data = await _json_body(request)
    try:
        req = deserialize_resume_run(run_id, data)
        await facade.resume_run(req)
        return JSONResponse({"ok": True})
    except Exception as exc:
        logger.exception("resume_run failed")
        return _error(400, str(exc))


async def post_run_children(request: Request) -> JSONResponse:
    """POST /runs/{run_id}/children — spawn_child_run."""
    facade: KernelFacade = request.app.state.facade
    run_id = request.path_params["run_id"]
    data = await _json_body(request)
    try:
        req = deserialize_spawn_child_run(run_id, data)
        resp = await facade.spawn_child_run(req)
        return JSONResponse(serialize_dataclass(resp), status_code=201)
    except Exception as exc:
        logger.exception("spawn_child_run failed")
        return _error(400, str(exc))


async def get_run_children(request: Request) -> JSONResponse:
    """GET /runs/{run_id}/children — query_child_runs."""
    facade: KernelFacade = request.app.state.facade
    run_id = request.path_params["run_id"]
    try:
        children = await facade.query_child_runs(run_id)
        return JSONResponse([serialize_dataclass(c) for c in children])
    except Exception as exc:
        logger.exception("query_child_runs failed")
        return _error(404, str(exc))


async def post_run_approval(request: Request) -> JSONResponse:
    """POST /runs/{run_id}/approval — submit_approval."""
    facade: KernelFacade = request.app.state.facade
    run_id = request.path_params["run_id"]
    data = await _json_body(request)
    try:
        req = deserialize_approval(run_id, data)
        await facade.submit_approval(req)
        return JSONResponse({"ok": True})
    except Exception as exc:
        logger.exception("submit_approval failed")
        return _error(400, str(exc))


async def post_run_stage_open(request: Request) -> JSONResponse:
    """POST /runs/{run_id}/stages/{stage_id}/open — open_stage."""
    facade: KernelFacade = request.app.state.facade
    run_id = request.path_params["run_id"]
    stage_id = request.path_params["stage_id"]
    data = await _json_body(request)
    try:
        await facade.open_stage(
            stage_id=stage_id,
            run_id=run_id,
            branch_id=data.get("branch_id"),
        )
        return JSONResponse({"ok": True}, status_code=201)
    except Exception as exc:
        logger.exception("open_stage failed")
        return _error(400, str(exc))


async def put_run_stage_state(request: Request) -> JSONResponse:
    """PUT /runs/{run_id}/stages/{stage_id}/state — mark_stage_state."""
    facade: KernelFacade = request.app.state.facade
    run_id = request.path_params["run_id"]
    stage_id = request.path_params["stage_id"]
    data = await _json_body(request)
    try:
        await facade.mark_stage_state(
            run_id=run_id,
            stage_id=stage_id,
            new_state=data["new_state"],
            failure_code=data.get("failure_code"),
        )
        return JSONResponse({"ok": True})
    except Exception as exc:
        logger.exception("mark_stage_state failed")
        return _error(400, str(exc))


async def post_run_branches(request: Request) -> JSONResponse:
    """POST /runs/{run_id}/branches — open_branch."""
    facade: KernelFacade = request.app.state.facade
    run_id = request.path_params["run_id"]
    data = await _json_body(request)
    try:
        req = deserialize_open_branch(run_id, data)
        await facade.open_branch(req)
        return JSONResponse({"ok": True}, status_code=201)
    except Exception as exc:
        logger.exception("open_branch failed")
        return _error(400, str(exc))


async def put_run_branch_state(request: Request) -> JSONResponse:
    """PUT /runs/{run_id}/branches/{branch_id}/state — mark_branch_state."""
    facade: KernelFacade = request.app.state.facade
    run_id = request.path_params["run_id"]
    branch_id = request.path_params["branch_id"]
    data = await _json_body(request)
    try:
        req = deserialize_branch_state_update(run_id, branch_id, data)
        await facade.mark_branch_state(req)
        return JSONResponse({"ok": True})
    except Exception as exc:
        logger.exception("mark_branch_state failed")
        return _error(400, str(exc))


async def post_run_human_gates(request: Request) -> JSONResponse:
    """POST /runs/{run_id}/human-gates — open_human_gate."""
    facade: KernelFacade = request.app.state.facade
    run_id = request.path_params["run_id"]
    data = await _json_body(request)
    try:
        req = deserialize_human_gate(run_id, data)
        await facade.open_human_gate(req)
        return JSONResponse({"ok": True}, status_code=201)
    except Exception as exc:
        logger.exception("open_human_gate failed")
        return _error(400, str(exc))


async def post_run_resolve_escalation(request: Request) -> JSONResponse:
    """POST /runs/{run_id}/resolve-escalation — resolve_escalation."""
    facade: KernelFacade = request.app.state.facade
    run_id = request.path_params["run_id"]
    data = await _json_body(request)
    try:
        await facade.resolve_escalation(
            run_id,
            resolution_notes=data.get("resolution_notes"),
            caused_by=data.get("caused_by"),
        )
        return JSONResponse({"ok": True})
    except Exception as exc:
        logger.exception("resolve_escalation failed")
        return _error(400, str(exc))


async def post_run_task_views(request: Request) -> JSONResponse:
    """POST /runs/{run_id}/task-views — record_task_view."""
    facade: KernelFacade = request.app.state.facade
    run_id = request.path_params["run_id"]
    data = await _json_body(request)
    try:
        record = deserialize_task_view(run_id, data)
        tv_id = facade.record_task_view(record)
        return JSONResponse({"task_view_id": tv_id}, status_code=201)
    except Exception as exc:
        logger.exception("record_task_view failed")
        return _error(400, str(exc))


async def put_task_view_decision(request: Request) -> JSONResponse:
    """PUT /task-views/{task_view_id}/decision — bind_task_view_to_decision."""
    facade: KernelFacade = request.app.state.facade
    task_view_id = request.path_params["task_view_id"]
    data = await _json_body(request)
    try:
        facade.bind_task_view_to_decision(task_view_id, data["decision_ref"])
        return JSONResponse({"ok": True})
    except Exception as exc:
        logger.exception("bind_task_view_to_decision failed")
        return _error(400, str(exc))


async def post_run_turn(request: Request) -> JSONResponse:
    """POST /runs/{run_id}/turn — execute_turn (in-process only)."""
    return _error(501, "execute_turn requires in-process mode; use KernelDirectAdapter")


async def post_tasks(request: Request) -> JSONResponse:
    """POST /tasks — register_task."""
    facade: KernelFacade = request.app.state.facade
    data = await _json_body(request)
    try:
        from agent_kernel.kernel.task_manager.contracts import TaskDescriptor

        descriptor = TaskDescriptor(**data)
        facade.register_task(descriptor)
        return JSONResponse({"ok": True}, status_code=201)
    except Exception as exc:
        logger.exception("register_task failed")
        return _error(400, str(exc))


async def get_task_status(request: Request) -> JSONResponse:
    """GET /tasks/{task_id}/status — get_task_status."""
    facade: KernelFacade = request.app.state.facade
    task_id = request.path_params["task_id"]
    try:
        status = facade.get_task_status(task_id)
        if status is None:
            return _error(404, f"task {task_id!r} not found")
        return JSONResponse(serialize_dataclass(status))
    except Exception as exc:
        logger.exception("get_task_status failed")
        return _error(400, str(exc))


async def get_manifest(request: Request) -> JSONResponse:
    """GET /manifest — get_manifest."""
    facade: KernelFacade = request.app.state.facade
    manifest = facade.get_manifest()
    return JSONResponse(serialize_dataclass(manifest))


async def get_health_liveness(request: Request) -> JSONResponse:
    """GET /health/liveness — basic liveness probe."""
    return JSONResponse({"status": "alive"})


async def get_health_readiness(request: Request) -> JSONResponse:
    """GET /health/readiness — readiness probe via get_health."""
    facade: KernelFacade = request.app.state.facade
    try:
        health = facade.get_health()
        return JSONResponse(serialize_dataclass(health))
    except Exception as exc:
        return _error(503, str(exc))


async def get_metrics(request: Request) -> JSONResponse:
    """GET /metrics -- lightweight in-process metrics snapshot."""
    collector = getattr(request.app.state, "metrics", None)
    if collector is None:
        return JSONResponse([], status_code=200)
    from dataclasses import asdict

    points = collector.snapshot()
    return JSONResponse([asdict(p) for p in points])


async def get_action_state(request: Request) -> JSONResponse:
    """GET /actions/{key}/state — get_action_state."""
    facade: KernelFacade = request.app.state.facade
    key = request.path_params["key"]
    state = facade.get_action_state(key)
    if state is None:
        return _error(404, "action not found")
    return JSONResponse({"state": state})


async def get_openapi(request: Request) -> JSONResponse:
    """GET /openapi.json — OpenAPI specification."""
    from agent_kernel.service.openapi import generate_openapi_spec

    return JSONResponse(generate_openapi_spec())


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def _build_routes() -> list[Route]:
    """Return the canonical route list shared by all app factories."""
    return [
        # Run lifecycle
        Route("/runs", post_runs, methods=["POST"]),
        Route("/runs/{run_id}", get_run, methods=["GET"]),
        Route("/runs/{run_id}/dashboard", get_run_dashboard, methods=["GET"]),
        Route("/runs/{run_id}/trace", get_run_trace, methods=["GET"]),
        Route("/runs/{run_id}/postmortem", get_run_postmortem, methods=["GET"]),
        Route("/runs/{run_id}/events", get_run_events, methods=["GET"]),
        Route("/runs/{run_id}/signal", post_run_signal, methods=["POST"]),
        Route("/runs/{run_id}/cancel", post_run_cancel, methods=["POST"]),
        Route("/runs/{run_id}/resume", post_run_resume, methods=["POST"]),
        # Child runs
        Route("/runs/{run_id}/children", post_run_children, methods=["POST"]),
        Route("/runs/{run_id}/children", get_run_children, methods=["GET"]),
        # Plan and approval
        Route("/runs/{run_id}/approval", post_run_approval, methods=["POST"]),
        # Stage lifecycle
        Route(
            "/runs/{run_id}/stages/{stage_id}/open",
            post_run_stage_open,
            methods=["POST"],
        ),
        Route(
            "/runs/{run_id}/stages/{stage_id}/state",
            put_run_stage_state,
            methods=["PUT"],
        ),
        # Branch lifecycle
        Route("/runs/{run_id}/branches", post_run_branches, methods=["POST"]),
        Route(
            "/runs/{run_id}/branches/{branch_id}/state",
            put_run_branch_state,
            methods=["PUT"],
        ),
        # Human gates
        Route(
            "/runs/{run_id}/human-gates",
            post_run_human_gates,
            methods=["POST"],
        ),
        # Escalation resolution
        Route(
            "/runs/{run_id}/resolve-escalation",
            post_run_resolve_escalation,
            methods=["POST"],
        ),
        # Task views
        Route(
            "/runs/{run_id}/task-views",
            post_run_task_views,
            methods=["POST"],
        ),
        Route(
            "/task-views/{task_view_id}/decision",
            put_task_view_decision,
            methods=["PUT"],
        ),
        # Turn (in-process only)
        Route("/runs/{run_id}/turn", post_run_turn, methods=["POST"]),
        # Task registry
        Route("/tasks", post_tasks, methods=["POST"]),
        Route("/tasks/{task_id}/status", get_task_status, methods=["GET"]),
        # Manifest and health
        Route("/manifest", get_manifest, methods=["GET"]),
        Route("/health/liveness", get_health_liveness, methods=["GET"]),
        Route("/health/readiness", get_health_readiness, methods=["GET"]),
        # Action state
        Route("/actions/{key}/state", get_action_state, methods=["GET"]),
        # Metrics
        Route("/metrics", get_metrics, methods=["GET"]),
        # OpenAPI spec
        Route("/openapi.json", get_openapi, methods=["GET"]),
    ]


def create_app(
    facade: KernelFacade,
    *,
    api_key: str | None = None,
    max_body_bytes: int = _MAX_REQUEST_BODY_BYTES,
    metrics_collector: object | None = None,
) -> Starlette:
    """Create a Starlette ASGI app wrapping the given KernelFacade.

    Args:
        facade: The KernelFacade instance to expose via HTTP.
        api_key: Optional API key for Bearer-token authentication.
            When *None*, all endpoints are open (no auth).
        metrics_collector: Optional ``KernelMetricsCollector`` instance.
            When provided, ``GET /metrics`` returns a JSON snapshot.

    Returns:
        A Starlette application ready to be served by uvicorn.

    """
    app = Starlette(routes=_build_routes())
    app.state.facade = facade
    app.state.max_body_bytes = max_body_bytes
    app.state.metrics = metrics_collector
    app = ApiKeyMiddleware(app, api_key=api_key)
    return app


def create_app_default(
    config: KernelConfig | None = None,
) -> Starlette:
    """Create an ASGI app with default in-memory runtime.

    Intended for container / uvicorn entrypoint when no external
    facade is injected.  Uses LocalWorkflowGateway + in-memory stores.

    Args:
        config: Optional ``KernelConfig`` override.  When *None*,
            ``KernelConfig.from_env()`` is used.

    Returns:
        A Starlette ASGI application.

    """
    from agent_kernel.adapters.facade.kernel_facade import KernelFacade
    from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore
    from agent_kernel.kernel.minimal_runtime import (
        AsyncExecutorService,
        InMemoryDecisionDeduper,
        InMemoryDecisionProjectionService,
        InMemoryKernelRuntimeEventLog,
        StaticDispatchAdmissionService,
        StaticRecoveryGateService,
    )
    from agent_kernel.runtime.metrics import KernelMetricsCollector
    from agent_kernel.substrate.local.adaptor import LocalWorkflowGateway
    from agent_kernel.substrate.temporal.run_actor_workflow import (
        RunActorDependencyBundle,
        RunActorStrictModeConfig,
    )

    if config is None:
        config = KernelConfig.from_env()

    collector = KernelMetricsCollector()
    event_log = InMemoryKernelRuntimeEventLog()
    projection = InMemoryDecisionProjectionService(event_log)
    deps = RunActorDependencyBundle(
        event_log=event_log,
        projection=projection,
        admission=StaticDispatchAdmissionService(),
        executor=AsyncExecutorService(),
        recovery=StaticRecoveryGateService(),
        deduper=InMemoryDecisionDeduper(),
        dedupe_store=InMemoryDedupeStore(),
        strict_mode=RunActorStrictModeConfig(enabled=False),
        observability_hook=collector,
    )
    gateway = LocalWorkflowGateway(deps, max_cache_size=config.max_turn_cache_size)
    facade = KernelFacade(workflow_gateway=gateway)
    return create_app(
        facade,
        api_key=config.api_key,
        max_body_bytes=config.max_request_body_bytes,
        metrics_collector=collector,
    )


def create_app_temporal(
    config: KernelConfig | None = None,
) -> Starlette:
    """Create an ASGI app backed by a real Temporal cluster.

    Reads Temporal connection details and storage paths from ``KernelConfig``
    (or environment via ``KernelConfig.from_env()``).  Starts a
    ``TemporalKernelWorker`` as an asyncio background task inside the
    Starlette lifespan so that both the HTTP server and the workflow worker
    share the same process and event loop.

    Storage defaults to SQLite files under ``AGENT_KERNEL_DATA_DIR``
    (default ``/app/data``).  Mount a persistent volume at that path in
    production containers.

    Args:
        config: Optional ``KernelConfig`` override.  When *None*,
            ``KernelConfig.from_env()`` is used.

    Returns:
        A Starlette ASGI application with Temporal substrate wired in.

    Raises:
        RuntimeError: If the ``temporalio`` SDK is not installed.

    """
    import asyncio
    import os
    from contextlib import asynccontextmanager

    from agent_kernel.runtime.bundle import (
        AgentKernelRuntimeBundle,
        RuntimeDedupeConfig,
        RuntimeEventLogConfig,
        RuntimeRecoveryOutcomeConfig,
        RuntimeTurnIntentLogConfig,
    )
    from agent_kernel.runtime.metrics import KernelMetricsCollector
    from agent_kernel.substrate.temporal.client import (
        TemporalClientConfig,
        create_temporal_client,
    )
    from agent_kernel.substrate.temporal.worker import TemporalWorkerConfig

    if config is None:
        config = KernelConfig.from_env()

    data_dir = os.environ.get("AGENT_KERNEL_DATA_DIR", "/app/data")

    @asynccontextmanager
    async def _lifespan(app: Starlette):  # type: ignore[type-arg]
        os.makedirs(data_dir, exist_ok=True)

        client = await create_temporal_client(
            TemporalClientConfig(
                target_host=config.temporal_host,
                namespace=config.temporal_namespace,
            )
        )
        bundle = AgentKernelRuntimeBundle.build_minimal_complete(
            temporal_client=client,
            event_log_config=RuntimeEventLogConfig(
                backend="sqlite",
                sqlite_database_path=f"{data_dir}/event_log.db",
            ),
            dedupe_config=RuntimeDedupeConfig(
                backend="sqlite",
                sqlite_database_path=f"{data_dir}/dedupe.db",
            ),
            recovery_outcome_config=RuntimeRecoveryOutcomeConfig(
                backend="sqlite",
                sqlite_database_path=f"{data_dir}/recovery.db",
            ),
            turn_intent_log_config=RuntimeTurnIntentLogConfig(
                backend="sqlite",
                sqlite_database_path=f"{data_dir}/turn_intent.db",
            ),
        )
        worker = bundle.create_temporal_worker(
            client,
            config=TemporalWorkerConfig(task_queue=config.temporal_task_queue),
        )
        collector = KernelMetricsCollector()

        app.state.facade = bundle.facade
        app.state.metrics = collector
        app.state.max_body_bytes = config.max_request_body_bytes

        logger.info(
            "Temporal worker starting — host=%s namespace=%s task_queue=%s",
            config.temporal_host,
            config.temporal_namespace,
            config.temporal_task_queue,
        )
        worker_task = asyncio.create_task(worker.run(), name="temporal-worker")
        try:
            yield
        finally:
            import contextlib

            worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker_task
            logger.info("Temporal worker stopped.")

    inner = Starlette(routes=_build_routes(), lifespan=_lifespan)
    # ApiKeyMiddleware forwards non-HTTP scopes (lifespan) to the inner app,
    # so the lifespan context manager fires correctly even when wrapped.
    return ApiKeyMiddleware(inner, api_key=config.api_key)  # type: ignore[return-value]


def _server_main() -> None:
    """Synchronous entry point for the ``agent-kernel-server`` CLI command.

    Starts uvicorn serving ``create_app_temporal`` on the port and host
    configured via environment variables.
    """
    import uvicorn

    cfg = KernelConfig.from_env()
    uvicorn.run(
        "agent_kernel.service.http_server:create_app_temporal",
        host="0.0.0.0",
        port=cfg.http_port,
        factory=True,
    )
