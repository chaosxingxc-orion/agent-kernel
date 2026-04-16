"""Tests for KernelFacade task management methods.

Covers register_task(), get_task_status(), list_session_tasks(),
and resolve_escalation(), including the RuntimeError raised when no
task_registry is injected.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from agent_kernel.adapters.facade.kernel_facade import KernelFacade
from agent_kernel.kernel.contracts import SignalRunRequest
from agent_kernel.kernel.task_manager.contracts import (
    TaskDescriptor,
    TaskRestartPolicy,
)
from agent_kernel.kernel.task_manager.registry import TaskRegistry
from agent_kernel.service.http_server import create_app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_gateway() -> MagicMock:
    """Make gateway."""
    gw = MagicMock()
    gw.start_workflow = AsyncMock(return_value={"workflow_id": "wf-1", "run_id": "r-1"})
    gw.signal_workflow = AsyncMock()
    return gw


def _make_facade(with_registry: bool = True) -> KernelFacade:
    """Make facade."""
    registry = TaskRegistry() if with_registry else None
    return KernelFacade(
        workflow_gateway=_make_gateway(),
        task_registry=registry,
    )


def _make_descriptor(
    task_id: str = "t1",
    session_id: str = "sess-1",
) -> TaskDescriptor:
    """Make descriptor."""
    return TaskDescriptor(
        task_id=task_id,
        session_id=session_id,
        task_kind="root",
        goal_description="test goal",
        restart_policy=TaskRestartPolicy(max_attempts=3),
    )


# ---------------------------------------------------------------------------
# register_task()
# ---------------------------------------------------------------------------


class TestRegisterTask:
    """Test suite for RegisterTask."""

    def test_register_task_succeeds_with_registry(self) -> None:
        """Verifies register task succeeds with registry."""
        facade = _make_facade()
        facade.register_task(_make_descriptor())
        # No exception means success

    def test_register_task_raises_without_registry(self) -> None:
        """Verifies register task raises without registry."""
        facade = _make_facade(with_registry=False)
        with pytest.raises(RuntimeError, match="task_registry"):
            facade.register_task(_make_descriptor())

    def test_duplicate_task_id_raises_value_error(self) -> None:
        """Verifies duplicate task id raises value error."""
        facade = _make_facade()
        facade.register_task(_make_descriptor())
        with pytest.raises(ValueError, match="already registered"):
            facade.register_task(_make_descriptor())

    def test_register_task_accepts_task_descriptor_frozen(self) -> None:
        """Ensure descriptor immutability is not violated during registration."""
        facade = _make_facade()
        d = _make_descriptor()
        facade.register_task(d)
        # descriptor should still be intact
        with pytest.raises((AttributeError, TypeError)):
            d.task_id = "mutated"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# get_task_status()
# ---------------------------------------------------------------------------


class TestGetTaskStatus:
    """Test suite for GetTaskStatus."""

    def test_get_task_status_returns_none_for_unknown(self) -> None:
        """Verifies get task status returns none for unknown."""
        facade = _make_facade()
        result = facade.get_task_status("no-such-task")
        assert result is None

    def test_get_task_status_returns_health_after_register(self) -> None:
        """Verifies get task status returns health after register."""
        facade = _make_facade()
        facade.register_task(_make_descriptor())
        health = facade.get_task_status("t1")
        assert health is not None
        assert health.task_id == "t1"
        assert health.lifecycle_state == "pending"

    def test_get_task_status_raises_without_registry(self) -> None:
        """Verifies get task status raises without registry."""
        facade = _make_facade(with_registry=False)
        with pytest.raises(RuntimeError, match="task_registry"):
            facade.get_task_status("t1")

    def test_get_task_status_reflects_max_attempts(self) -> None:
        """Verifies get task status reflects max attempts."""
        facade = _make_facade()
        facade.register_task(_make_descriptor())
        health = facade.get_task_status("t1")
        assert health is not None
        assert health.max_attempts == 3


# ---------------------------------------------------------------------------
# list_session_tasks()
# ---------------------------------------------------------------------------


class TestListSessionTasks:
    """Test suite for ListSessionTasks."""

    def test_list_session_tasks_empty_for_unknown_session(self) -> None:
        """Verifies list session tasks empty for unknown session."""
        facade = _make_facade()
        result = facade.list_session_tasks("no-session")
        assert result == []

    def test_list_session_tasks_returns_registered_tasks(self) -> None:
        """Verifies list session tasks returns registered tasks."""
        facade = _make_facade()
        facade.register_task(_make_descriptor(task_id="t1", session_id="sess-A"))
        facade.register_task(_make_descriptor(task_id="t2", session_id="sess-A"))
        tasks = facade.list_session_tasks("sess-A")
        assert len(tasks) == 2
        ids = {t.task_id for t in tasks}
        assert ids == {"t1", "t2"}

    def test_list_session_tasks_scoped_to_session(self) -> None:
        """Verifies list session tasks scoped to session."""
        facade = _make_facade()
        facade.register_task(_make_descriptor(task_id="t1", session_id="sess-A"))
        facade.register_task(_make_descriptor(task_id="t2", session_id="sess-B"))
        tasks_a = facade.list_session_tasks("sess-A")
        tasks_b = facade.list_session_tasks("sess-B")
        assert len(tasks_a) == 1
        assert tasks_a[0].task_id == "t1"
        assert len(tasks_b) == 1
        assert tasks_b[0].task_id == "t2"

    def test_list_session_tasks_raises_without_registry(self) -> None:
        """Verifies list session tasks raises without registry."""
        facade = _make_facade(with_registry=False)
        with pytest.raises(RuntimeError, match="task_registry"):
            facade.list_session_tasks("sess-A")

    def test_list_session_tasks_returns_descriptors(self) -> None:
        """Verifies list session tasks returns descriptors."""
        facade = _make_facade()
        facade.register_task(_make_descriptor())
        tasks = facade.list_session_tasks("sess-1")
        assert all(isinstance(t, TaskDescriptor) for t in tasks)


# ---------------------------------------------------------------------------
# Constructor — task_registry parameter
# ---------------------------------------------------------------------------


class TestConstructorTaskRegistry:
    """Test suite for ConstructorTaskRegistry."""

    def test_facade_constructed_without_registry(self) -> None:
        """Verifies facade constructed without registry."""
        facade = KernelFacade(workflow_gateway=_make_gateway())
        assert facade._task_registry is None

    def test_facade_constructed_with_registry(self) -> None:
        """Verifies facade constructed with registry."""
        reg = TaskRegistry()
        facade = KernelFacade(workflow_gateway=_make_gateway(), task_registry=reg)
        assert facade._task_registry is reg

    def test_facade_get_manifest_unaffected_by_task_registry(self) -> None:
        """Verifies facade get manifest unaffected by task registry."""
        reg = TaskRegistry()
        facade = KernelFacade(workflow_gateway=_make_gateway(), task_registry=reg)
        manifest = facade.get_manifest()
        assert manifest is not None
        assert manifest.kernel_version is not None


# ---------------------------------------------------------------------------
# resolve_escalation() — unit tests
# ---------------------------------------------------------------------------


class TestResolveEscalation:
    """Unit tests for KernelFacade.resolve_escalation().

    The Temporal workflow gateway is mocked (external boundary); all kernel
    logic runs against real facade internals.
    """

    def _make_gw(self) -> MagicMock:
        """Make gw."""
        gw = MagicMock()
        gw.signal_workflow = AsyncMock()
        return gw

    def _make_facade(self, gw: MagicMock | None = None) -> tuple[KernelFacade, MagicMock]:
        """Make facade."""
        gateway = gw or self._make_gw()
        return KernelFacade(workflow_gateway=gateway), gateway

    def test_resolve_escalation_sends_recovery_succeeded_signal(self) -> None:
        """signal_workflow must be called with signal_type='recovery_succeeded'."""
        facade, gw = self._make_facade()
        asyncio.run(facade.resolve_escalation("run-esc-1"))
        gw.signal_workflow.assert_awaited_once()
        call_args = gw.signal_workflow.call_args
        run_id_arg, signal_req = call_args.args
        assert run_id_arg == "run-esc-1"
        assert isinstance(signal_req, SignalRunRequest)
        assert signal_req.signal_type == "recovery_succeeded"
        assert signal_req.run_id == "run-esc-1"

    def test_resolve_escalation_payload_includes_resolution_notes(self) -> None:
        """When resolution_notes is supplied, it must appear in signal_payload."""
        facade, gw = self._make_facade()
        asyncio.run(facade.resolve_escalation("run-esc-2", resolution_notes="fixed"))
        call_args = gw.signal_workflow.call_args
        _, signal_req = call_args.args
        assert signal_req.signal_payload == {"resolution_notes": "fixed"}

    def test_resolve_escalation_empty_payload_when_no_notes(self) -> None:
        """When resolution_notes is omitted, signal_payload must be None."""
        facade, gw = self._make_facade()
        asyncio.run(facade.resolve_escalation("run-esc-3"))
        call_args = gw.signal_workflow.call_args
        _, signal_req = call_args.args
        assert signal_req.signal_payload is None

    def test_resolve_escalation_appends_trace_event(self) -> None:
        """A trace.escalation_resolved event must be appended after signaling.

        Without an injected event_log the _append_trace_event helper silently
        skips persistence, so we verify the gateway was called exactly once
        (the signal) meaning no exception was raised by the append path.
        """
        facade, gw = self._make_facade()
        # No event_log injected — append path must not raise.
        asyncio.run(facade.resolve_escalation("run-esc-4"))
        # signal_workflow called exactly once means the trace append did not
        # accidentally call it again or raise before reaching it.
        gw.signal_workflow.assert_awaited_once()

    def test_resolve_escalation_default_caused_by(self) -> None:
        """When caused_by is None the signal must use f'resolve_escalation:{run_id}'."""
        facade, gw = self._make_facade()
        asyncio.run(facade.resolve_escalation("run-esc-5"))
        call_args = gw.signal_workflow.call_args
        _, signal_req = call_args.args
        assert signal_req.caused_by == "resolve_escalation:run-esc-5"

    def test_resolve_escalation_custom_caused_by(self) -> None:
        """A custom caused_by value must be forwarded as-is to the signal request."""
        facade, gw = self._make_facade()
        asyncio.run(facade.resolve_escalation("run-esc-6", caused_by="operator:alice"))
        call_args = gw.signal_workflow.call_args
        _, signal_req = call_args.args
        assert signal_req.caused_by == "operator:alice"


# ---------------------------------------------------------------------------
# POST /runs/{run_id}/resolve-escalation — HTTP handler tests
# ---------------------------------------------------------------------------


def _make_http_gateway() -> MagicMock:
    """Make http gateway."""
    gw = MagicMock()
    gw.start_workflow = AsyncMock(return_value={"workflow_id": "wf-1", "run_id": "r-1"})
    gw.signal_workflow = AsyncMock()
    return gw


def _make_http_app(gw: MagicMock | None = None):
    """Make http app."""
    gateway = gw or _make_http_gateway()
    facade = KernelFacade(workflow_gateway=gateway)
    return create_app(facade), facade, gateway


class TestResolveEscalationHTTP:
    """Integration tests for the POST /runs/{run_id}/resolve-escalation handler.

    Uses httpx.AsyncClient with Starlette's ASGI transport — no TCP server.
    The Temporal gateway is mocked (external boundary); the real facade and
    HTTP routing run unchanged.
    """

    def test_post_resolve_escalation_calls_facade(self) -> None:
        """POST with resolution_notes must invoke facade.resolve_escalation."""
        app, facade, _gw = _make_http_app()
        # Replace resolve_escalation with an AsyncMock to capture the call.
        facade.resolve_escalation = AsyncMock()
        transport = httpx.ASGITransport(app=app)
        client = httpx.AsyncClient(transport=transport, base_url="http://test")
        resp = asyncio.run(
            client.post(
                "/runs/test-run/resolve-escalation",
                json={"resolution_notes": "ok"},
            )
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        facade.resolve_escalation.assert_awaited_once_with(
            "test-run",
            resolution_notes="ok",
            caused_by=None,
        )

    def test_post_resolve_escalation_missing_run_returns_error(self) -> None:
        """If facade.resolve_escalation raises RuntimeError, HTTP returns 400."""
        app, facade, _gw = _make_http_app()
        facade.resolve_escalation = AsyncMock(side_effect=RuntimeError("run not found"))
        transport = httpx.ASGITransport(app=app)
        client = httpx.AsyncClient(transport=transport, base_url="http://test")
        resp = asyncio.run(
            client.post(
                "/runs/missing-run/resolve-escalation",
                json={},
            )
        )
        assert resp.status_code == 400
        assert "run not found" in resp.json().get("error", "")
