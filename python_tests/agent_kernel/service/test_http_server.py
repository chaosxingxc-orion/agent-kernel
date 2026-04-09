"""Tests for the HTTP service layer wrapping KernelFacade.

Uses httpx.AsyncClient with Starlette's ASGI transport for in-process testing
(no actual TCP server needed).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from agent_kernel.adapters.facade.kernel_facade import KernelFacade
from agent_kernel.kernel.contracts import (
    KernelManifest,
    RunProjection,
    SpawnChildRunResponse,
    StartRunResponse,
)
from agent_kernel.service.http_server import create_app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_gateway() -> MagicMock:
    gw = MagicMock()
    gw.query_projection = AsyncMock(
        return_value=RunProjection(
            run_id="run-1",
            lifecycle_state="running",
            projected_offset=5,
            waiting_external=False,
            ready_for_dispatch=True,
        ),
    )
    gw.signal_run = AsyncMock()
    gw.signal_workflow = AsyncMock()
    gw.start_workflow = AsyncMock(
        return_value={"run_id": "run-1", "workflow_id": "wf-1"},
    )
    gw.start_child_workflow = AsyncMock(
        return_value={"run_id": "child-1", "workflow_id": "cwf-1"},
    )
    gw.cancel_workflow = AsyncMock()
    return gw


def _make_facade() -> KernelFacade:
    return KernelFacade(workflow_gateway=_make_gateway())


@pytest.fixture()
def app():
    facade = _make_facade()
    return create_app(facade)


@pytest.fixture()
def client(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# ---------------------------------------------------------------------------
# Manifest and health
# ---------------------------------------------------------------------------


class TestManifestAndHealth:
    def test_get_manifest(self, client) -> None:
        resp = asyncio.run(client.get("/manifest"))
        assert resp.status_code == 200
        data = resp.json()
        assert data["trace_protocol_version"] == "2.8"
        assert "evolve_postmortem" in data["supported_trace_features"]
        assert "child_run_orchestration" in data["supported_trace_features"]

    def test_health_liveness(self, client) -> None:
        resp = asyncio.run(client.get("/health/liveness"))
        assert resp.status_code == 200
        assert resp.json()["status"] == "alive"

    def test_health_readiness(self, client) -> None:
        resp = asyncio.run(client.get("/health/readiness"))
        # Without a health_probe, returns minimal static response.
        assert resp.status_code in (200, 503)


# ---------------------------------------------------------------------------
# Run lifecycle
# ---------------------------------------------------------------------------


class TestRunLifecycle:
    def test_post_runs_creates_run(self, client, app) -> None:
        # Mock start_run on the facade.
        facade = app.state.facade
        facade.start_run = AsyncMock(
            return_value=StartRunResponse(
                run_id="run-new",
                temporal_workflow_id="wf-new",
                lifecycle_state="created",
            ),
        )
        resp = asyncio.run(
            client.post(
                "/runs",
                json={
                    "run_id": "run-new",
                    "run_kind": "default",
                },
            ),
        )
        assert resp.status_code == 201
        assert resp.json()["run_id"] == "run-new"

    def test_get_run(self, client) -> None:
        resp = asyncio.run(client.get("/runs/run-1"))
        assert resp.status_code == 200
        data = resp.json()
        assert data["run_id"] == "run-1"

    def test_post_signal(self, client, app) -> None:
        facade = app.state.facade
        facade.signal_run = AsyncMock()
        resp = asyncio.run(
            client.post(
                "/runs/run-1/signal",
                json={
                    "signal_type": "resume_from_snapshot",
                    "signal_payload": {},
                },
            ),
        )
        assert resp.status_code == 200

    def test_post_cancel(self, client, app) -> None:
        facade = app.state.facade
        facade.cancel_run = AsyncMock()
        resp = asyncio.run(
            client.post("/runs/run-1/cancel", json={"reason": "test"}),
        )
        assert resp.status_code == 200

    def test_post_resume(self, client, app) -> None:
        facade = app.state.facade
        facade.resume_run = AsyncMock()
        resp = asyncio.run(
            client.post("/runs/run-1/resume", json={}),
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# TRACE endpoints
# ---------------------------------------------------------------------------


class TestTraceEndpoints:
    def test_get_trace(self, client) -> None:
        resp = asyncio.run(client.get("/runs/run-1/trace"))
        assert resp.status_code == 200
        data = resp.json()
        assert data["run_id"] == "run-1"
        assert "run_state" in data

    def test_get_postmortem(self, client) -> None:
        resp = asyncio.run(client.get("/runs/run-1/postmortem"))
        assert resp.status_code == 200
        data = resp.json()
        assert data["run_id"] == "run-1"
        assert "failure_codes" in data


# ---------------------------------------------------------------------------
# Child runs
# ---------------------------------------------------------------------------


class TestChildRuns:
    def test_get_children_empty(self, client) -> None:
        resp = asyncio.run(client.get("/runs/run-1/children"))
        assert resp.status_code == 200
        assert resp.json() == []

    def test_post_children(self, client, app) -> None:
        facade = app.state.facade
        facade.spawn_child_run = AsyncMock(
            return_value=SpawnChildRunResponse(
                child_run_id="child-1",
                lifecycle_state="created",
            ),
        )
        resp = asyncio.run(
            client.post(
                "/runs/run-1/children",
                json={
                    "child_kind": "plan_step",
                    "task_id": "task-1",
                },
            ),
        )
        assert resp.status_code == 201
        assert resp.json()["child_run_id"] == "child-1"


# ---------------------------------------------------------------------------
# Stage and branch lifecycle
# ---------------------------------------------------------------------------


class TestStageAndBranch:
    def test_open_stage(self, client) -> None:
        resp = asyncio.run(
            client.post("/runs/run-1/stages/s1/open", json={}),
        )
        assert resp.status_code == 201

    def test_mark_stage_state(self, client) -> None:
        # First open a stage.
        asyncio.run(client.post("/runs/run-1/stages/s1/open", json={}))
        resp = asyncio.run(
            client.put(
                "/runs/run-1/stages/s1/state",
                json={"new_state": "active"},
            ),
        )
        assert resp.status_code == 200

    def test_open_branch(self, client) -> None:
        resp = asyncio.run(
            client.post(
                "/runs/run-1/branches",
                json={
                    "branch_id": "b1",
                    "stage_id": "s1",
                },
            ),
        )
        assert resp.status_code == 201

    def test_mark_branch_state(self, client) -> None:
        # First open branch.
        asyncio.run(
            client.post(
                "/runs/run-1/branches",
                json={
                    "branch_id": "b1",
                    "stage_id": "s1",
                },
            ),
        )
        resp = asyncio.run(
            client.put(
                "/runs/run-1/branches/b1/state",
                json={"new_state": "active"},
            ),
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Human gates
# ---------------------------------------------------------------------------


class TestHumanGates:
    def test_open_human_gate(self, client) -> None:
        resp = asyncio.run(
            client.post(
                "/runs/run-1/human-gates",
                json={
                    "gate_ref": "gate-1",
                    "gate_type": "final_approval",
                    "trigger_reason": "high risk action",
                    "trigger_source": "system",
                },
            ),
        )
        assert resp.status_code == 201

    def test_submit_approval(self, client, app) -> None:
        facade = app.state.facade
        facade.submit_approval = AsyncMock()
        resp = asyncio.run(
            client.post(
                "/runs/run-1/approval",
                json={
                    "approval_ref": "apr-1",
                    "approved": True,
                },
            ),
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Task views
# ---------------------------------------------------------------------------


class TestTaskViews:
    def test_record_task_view(self, client, app) -> None:
        from agent_kernel.kernel.persistence.sqlite_task_view_log import (
            SQLiteTaskViewLog,
        )

        tv_log = SQLiteTaskViewLog(":memory:")
        app.state.facade._task_view_log = tv_log
        resp = asyncio.run(
            client.post(
                "/runs/run-1/task-views",
                json={
                    "task_view_id": "tv-1",
                    "selected_model_role": "heavy_reasoning",
                    "assembled_at": "2026-04-07T00:00:00Z",
                },
            ),
        )
        assert resp.status_code == 201
        assert resp.json()["task_view_id"] == "tv-1"


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


class TestSerialization:
    def test_serialize_dataclass_handles_frozenset(self) -> None:
        from agent_kernel.service.serialization import serialize_dataclass

        manifest = KernelManifest(
            kernel_version="0.2.0",
            protocol_version="1.0.0",
            supported_action_types=frozenset({"tool_call"}),
            supported_interaction_targets=frozenset({"human_actor"}),
            supported_recovery_modes=frozenset({"abort"}),
            supported_governance_features=frozenset({"approval_gate"}),
            supported_event_types=frozenset({"run.created"}),
            substrate_type="temporal",
        )
        result = serialize_dataclass(manifest)
        # frozensets should be converted to sorted lists.
        assert isinstance(result["supported_action_types"], list)
        assert result["supported_action_types"] == ["tool_call"]

    def test_serialize_none_returns_empty_dict(self) -> None:
        from agent_kernel.service.serialization import serialize_dataclass

        assert serialize_dataclass(None) == {}


# ---------------------------------------------------------------------------
# KernelConfig wiring
# ---------------------------------------------------------------------------


class TestKernelConfigWiring:
    def test_create_app_default_uses_kernel_config(self) -> None:
        """Verify create_app_default() propagates KernelConfig values."""
        from agent_kernel.config import KernelConfig
        from agent_kernel.service.http_server import create_app_default

        cfg = KernelConfig(
            api_key="test-key-42",
            max_request_body_bytes=512,
            max_turn_cache_size=99,
        )
        app = create_app_default(config=cfg)

        # max_body_bytes is stored on the inner Starlette app's state.
        # ApiKeyMiddleware wraps Starlette, so unwrap to reach state.
        inner = app.app if hasattr(app, "app") else app
        assert inner.state.max_body_bytes == 512

        # Verify the gateway received the custom cache size.
        facade = inner.state.facade
        gateway = facade._workflow_gateway
        assert gateway._max_cache_size == 99

    def test_create_app_default_defaults_to_from_env(self) -> None:
        """Without explicit config, from_env() is used (all defaults)."""
        from agent_kernel.service.http_server import create_app_default

        app = create_app_default()
        inner = app.app if hasattr(app, "app") else app
        assert inner.state.max_body_bytes == 1_048_576
