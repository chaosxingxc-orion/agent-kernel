"""OpenAPI 3.1 specification for the agent-kernel HTTP API."""

from __future__ import annotations


def generate_openapi_spec() -> dict:
    """Generate OpenAPI 3.1 spec from kernel HTTP routes.

    Returns a dict suitable for json.dumps() or serving at /openapi.json.
    """
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Agent Kernel API",
            "version": "0.2.0",
            "description": ("HTTP interface to the agent-kernel six-authority lifecycle protocol."),
        },
        "paths": _build_paths(),
    }


def _ok_response(description: str = "Success") -> dict:
    """Builds the OpenAPI success response schema."""
    return {
        "description": description,
        "content": {"application/json": {"schema": {"type": "object"}}},
    }


def _error_responses(*codes: int) -> dict:
    """Builds standard OpenAPI error response schemas."""
    labels = {
        400: "Bad request",
        401: "Unauthorized",
        404: "Not found",
        501: "Not implemented",
        503: "Service unavailable",
    }
    return {
        str(code): {
            "description": labels.get(code, "Error"),
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {"error": {"type": "string"}},
                    },
                },
            },
        }
        for code in codes
    }


def _json_request_body(description: str = "JSON payload") -> dict:
    """Json request body."""
    return {
        "required": True,
        "description": description,
        "content": {"application/json": {"schema": {"type": "object"}}},
    }


def _run_id_param() -> dict:
    """Run id param."""
    return {
        "name": "run_id",
        "in": "path",
        "required": True,
        "schema": {"type": "string"},
        "description": "Run identifier.",
    }


def _build_paths() -> dict:
    """Build the OpenAPI paths dict from the HTTP server route definitions."""
    return {
        "/runs": {
            "post": {
                "operationId": "post_runs",
                "summary": "POST /runs -- start_run",
                "requestBody": _json_request_body("StartRunRequest payload"),
                "responses": {
                    "201": _ok_response("Run created"),
                    **_error_responses(400, 401),
                },
            },
        },
        "/runs/{run_id}": {
            "get": {
                "operationId": "get_run",
                "summary": "GET /runs/{run_id} -- query_run",
                "parameters": [_run_id_param()],
                "responses": {
                    "200": _ok_response("Run projection"),
                    **_error_responses(401, 404),
                },
            },
        },
        "/runs/{run_id}/dashboard": {
            "get": {
                "operationId": "get_run_dashboard",
                "summary": "GET /runs/{run_id}/dashboard -- query_run_dashboard",
                "parameters": [_run_id_param()],
                "responses": {
                    "200": _ok_response("Run dashboard"),
                    **_error_responses(401, 404),
                },
            },
        },
        "/runs/{run_id}/trace": {
            "get": {
                "operationId": "get_run_trace",
                "summary": "GET /runs/{run_id}/trace -- query_trace_runtime",
                "parameters": [_run_id_param()],
                "responses": {
                    "200": _ok_response("Trace runtime"),
                    **_error_responses(401, 404),
                },
            },
        },
        "/runs/{run_id}/postmortem": {
            "get": {
                "operationId": "get_run_postmortem",
                "summary": "GET /runs/{run_id}/postmortem -- query_run_postmortem",
                "parameters": [_run_id_param()],
                "responses": {
                    "200": _ok_response("Run postmortem"),
                    **_error_responses(401, 404),
                },
            },
        },
        "/runs/{run_id}/events": {
            "get": {
                "operationId": "get_run_events",
                "summary": "GET /runs/{run_id}/events -- stream_run_events (SSE)",
                "parameters": [
                    _run_id_param(),
                    {
                        "name": "include_derived_diagnostic",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "string", "default": "false"},
                        "description": ("Include derived_diagnostic events in the stream."),
                    },
                ],
                "responses": {
                    "200": {
                        "description": "SSE event stream",
                        "content": {"text/event-stream": {"schema": {"type": "string"}}},
                    },
                    **_error_responses(401, 404),
                },
            },
        },
        "/runs/{run_id}/signal": {
            "post": {
                "operationId": "post_run_signal",
                "summary": "POST /runs/{run_id}/signal -- signal_run",
                "parameters": [_run_id_param()],
                "requestBody": _json_request_body("Signal payload"),
                "responses": {
                    "200": _ok_response("Signal accepted"),
                    **_error_responses(400, 401),
                },
            },
        },
        "/runs/{run_id}/cancel": {
            "post": {
                "operationId": "post_run_cancel",
                "summary": "POST /runs/{run_id}/cancel -- cancel_run",
                "parameters": [_run_id_param()],
                "requestBody": _json_request_body("Cancel payload"),
                "responses": {
                    "200": _ok_response("Run cancelled"),
                    **_error_responses(400, 401),
                },
            },
        },
        "/runs/{run_id}/resume": {
            "post": {
                "operationId": "post_run_resume",
                "summary": "POST /runs/{run_id}/resume -- resume_run",
                "parameters": [_run_id_param()],
                "requestBody": _json_request_body("Resume payload"),
                "responses": {
                    "200": _ok_response("Run resumed"),
                    **_error_responses(400, 401),
                },
            },
        },
        "/runs/{run_id}/children": {
            "post": {
                "operationId": "post_run_children",
                "summary": "POST /runs/{run_id}/children -- spawn_child_run",
                "parameters": [_run_id_param()],
                "requestBody": _json_request_body("SpawnChildRunRequest payload"),
                "responses": {
                    "201": _ok_response("Child run spawned"),
                    **_error_responses(400, 401),
                },
            },
            "get": {
                "operationId": "get_run_children",
                "summary": "GET /runs/{run_id}/children -- query_child_runs",
                "parameters": [_run_id_param()],
                "responses": {
                    "200": _ok_response("List of child runs"),
                    **_error_responses(401, 404),
                },
            },
        },
        "/runs/{run_id}/approval": {
            "post": {
                "operationId": "post_run_approval",
                "summary": "POST /runs/{run_id}/approval -- submit_approval",
                "parameters": [_run_id_param()],
                "requestBody": _json_request_body("Approval payload"),
                "responses": {
                    "200": _ok_response("Approval submitted"),
                    **_error_responses(400, 401),
                },
            },
        },
        "/runs/{run_id}/stages/{stage_id}/open": {
            "post": {
                "operationId": "post_run_stage_open",
                "summary": "POST /runs/{run_id}/stages/{stage_id}/open -- open_stage",
                "parameters": [
                    _run_id_param(),
                    {
                        "name": "stage_id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                        "description": "Stage identifier.",
                    },
                ],
                "requestBody": _json_request_body("Stage open payload"),
                "responses": {
                    "201": _ok_response("Stage opened"),
                    **_error_responses(400, 401),
                },
            },
        },
        "/runs/{run_id}/stages/{stage_id}/state": {
            "put": {
                "operationId": "put_run_stage_state",
                "summary": ("PUT /runs/{run_id}/stages/{stage_id}/state -- mark_stage_state"),
                "parameters": [
                    _run_id_param(),
                    {
                        "name": "stage_id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                        "description": "Stage identifier.",
                    },
                ],
                "requestBody": _json_request_body("Stage state payload"),
                "responses": {
                    "200": _ok_response("Stage state updated"),
                    **_error_responses(400, 401),
                },
            },
        },
        "/runs/{run_id}/branches": {
            "post": {
                "operationId": "post_run_branches",
                "summary": "POST /runs/{run_id}/branches -- open_branch",
                "parameters": [_run_id_param()],
                "requestBody": _json_request_body("OpenBranchRequest payload"),
                "responses": {
                    "201": _ok_response("Branch opened"),
                    **_error_responses(400, 401),
                },
            },
        },
        "/runs/{run_id}/branches/{branch_id}/state": {
            "put": {
                "operationId": "put_run_branch_state",
                "summary": ("PUT /runs/{run_id}/branches/{branch_id}/state -- mark_branch_state"),
                "parameters": [
                    _run_id_param(),
                    {
                        "name": "branch_id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                        "description": "Branch identifier.",
                    },
                ],
                "requestBody": _json_request_body("Branch state payload"),
                "responses": {
                    "200": _ok_response("Branch state updated"),
                    **_error_responses(400, 401),
                },
            },
        },
        "/runs/{run_id}/human-gates": {
            "post": {
                "operationId": "post_run_human_gates",
                "summary": "POST /runs/{run_id}/human-gates -- open_human_gate",
                "parameters": [_run_id_param()],
                "requestBody": _json_request_body("HumanGate payload"),
                "responses": {
                    "201": _ok_response("Human gate opened"),
                    **_error_responses(400, 401),
                },
            },
        },
        "/runs/{run_id}/task-views": {
            "post": {
                "operationId": "post_run_task_views",
                "summary": "POST /runs/{run_id}/task-views -- record_task_view",
                "parameters": [_run_id_param()],
                "requestBody": _json_request_body("TaskView payload"),
                "responses": {
                    "201": _ok_response("Task view recorded"),
                    **_error_responses(400, 401),
                },
            },
        },
        "/task-views/{task_view_id}/decision": {
            "put": {
                "operationId": "put_task_view_decision",
                "summary": (
                    "PUT /task-views/{task_view_id}/decision -- bind_task_view_to_decision"
                ),
                "parameters": [
                    {
                        "name": "task_view_id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                        "description": "Task view identifier.",
                    },
                ],
                "requestBody": _json_request_body("Decision binding payload"),
                "responses": {
                    "200": _ok_response("Decision bound"),
                    **_error_responses(400, 401),
                },
            },
        },
        "/runs/{run_id}/turn": {
            "post": {
                "operationId": "post_run_turn",
                "summary": ("POST /runs/{run_id}/turn -- execute_turn (in-process only)"),
                "parameters": [_run_id_param()],
                "responses": {
                    **_error_responses(501),
                },
            },
        },
        "/tasks": {
            "post": {
                "operationId": "post_tasks",
                "summary": "POST /tasks -- register_task",
                "requestBody": _json_request_body("TaskDescriptor payload"),
                "responses": {
                    "201": _ok_response("Task registered"),
                    **_error_responses(400, 401),
                },
            },
        },
        "/tasks/{task_id}/status": {
            "get": {
                "operationId": "get_task_status",
                "summary": "GET /tasks/{task_id}/status -- get_task_status",
                "parameters": [
                    {
                        "name": "task_id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                        "description": "Task identifier.",
                    },
                ],
                "responses": {
                    "200": _ok_response("Task status"),
                    **_error_responses(400, 401, 404),
                },
            },
        },
        "/manifest": {
            "get": {
                "operationId": "get_manifest",
                "summary": "GET /manifest -- get_manifest",
                "responses": {
                    "200": _ok_response("Kernel manifest"),
                },
            },
        },
        "/health/liveness": {
            "get": {
                "operationId": "get_health_liveness",
                "summary": "GET /health/liveness -- basic liveness probe",
                "responses": {
                    "200": _ok_response("Alive"),
                },
            },
        },
        "/health/readiness": {
            "get": {
                "operationId": "get_health_readiness",
                "summary": "GET /health/readiness -- readiness probe via get_health",
                "responses": {
                    "200": _ok_response("Ready"),
                    **_error_responses(503),
                },
            },
        },
        "/actions/{key}/state": {
            "get": {
                "operationId": "get_action_state",
                "summary": "GET /actions/{key}/state -- get_action_state",
                "parameters": [
                    {
                        "name": "key",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                        "description": "Action deduplication key.",
                    },
                ],
                "responses": {
                    "200": _ok_response("Action state"),
                    **_error_responses(401, 404),
                },
            },
        },
        "/metrics": {
            "get": {
                "operationId": "get_metrics",
                "summary": "GET /metrics -- lightweight in-process metrics snapshot",
                "responses": {
                    "200": _ok_response("Metrics snapshot"),
                },
            },
        },
        "/openapi.json": {
            "get": {
                "operationId": "get_openapi",
                "summary": "GET /openapi.json -- OpenAPI specification",
                "responses": {
                    "200": _ok_response("OpenAPI 3.1 spec"),
                },
            },
        },
    }
