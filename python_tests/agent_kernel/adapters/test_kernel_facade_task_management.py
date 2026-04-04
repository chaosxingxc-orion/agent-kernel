"""Tests for KernelFacade task management methods.

Covers register_task(), get_task_status(), and list_session_tasks(),
including the RuntimeError raised when no task_registry is injected.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_kernel.adapters.facade.kernel_facade import KernelFacade
from agent_kernel.kernel.task_manager.contracts import (
    TaskDescriptor,
    TaskRestartPolicy,
)
from agent_kernel.kernel.task_manager.registry import TaskRegistry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_gateway() -> MagicMock:
    gw = MagicMock()
    gw.start_workflow = AsyncMock(return_value={"workflow_id": "wf-1", "run_id": "r-1"})
    gw.signal_workflow = AsyncMock()
    return gw


def _make_facade(with_registry: bool = True) -> KernelFacade:
    registry = TaskRegistry() if with_registry else None
    return KernelFacade(
        workflow_gateway=_make_gateway(),
        task_registry=registry,
    )


def _make_descriptor(
    task_id: str = "t1",
    session_id: str = "sess-1",
) -> TaskDescriptor:
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
    def test_register_task_succeeds_with_registry(self) -> None:
        facade = _make_facade()
        facade.register_task(_make_descriptor())
        # No exception means success

    def test_register_task_raises_without_registry(self) -> None:
        facade = _make_facade(with_registry=False)
        with pytest.raises(RuntimeError, match="task_registry"):
            facade.register_task(_make_descriptor())

    def test_duplicate_task_id_raises_value_error(self) -> None:
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
    def test_get_task_status_returns_none_for_unknown(self) -> None:
        facade = _make_facade()
        result = facade.get_task_status("no-such-task")
        assert result is None

    def test_get_task_status_returns_health_after_register(self) -> None:
        facade = _make_facade()
        facade.register_task(_make_descriptor())
        health = facade.get_task_status("t1")
        assert health is not None
        assert health.task_id == "t1"
        assert health.lifecycle_state == "pending"

    def test_get_task_status_raises_without_registry(self) -> None:
        facade = _make_facade(with_registry=False)
        with pytest.raises(RuntimeError, match="task_registry"):
            facade.get_task_status("t1")

    def test_get_task_status_reflects_max_attempts(self) -> None:
        facade = _make_facade()
        facade.register_task(_make_descriptor())
        health = facade.get_task_status("t1")
        assert health is not None
        assert health.max_attempts == 3


# ---------------------------------------------------------------------------
# list_session_tasks()
# ---------------------------------------------------------------------------


class TestListSessionTasks:
    def test_list_session_tasks_empty_for_unknown_session(self) -> None:
        facade = _make_facade()
        result = facade.list_session_tasks("no-session")
        assert result == []

    def test_list_session_tasks_returns_registered_tasks(self) -> None:
        facade = _make_facade()
        facade.register_task(_make_descriptor(task_id="t1", session_id="sess-A"))
        facade.register_task(_make_descriptor(task_id="t2", session_id="sess-A"))
        tasks = facade.list_session_tasks("sess-A")
        assert len(tasks) == 2
        ids = {t.task_id for t in tasks}
        assert ids == {"t1", "t2"}

    def test_list_session_tasks_scoped_to_session(self) -> None:
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
        facade = _make_facade(with_registry=False)
        with pytest.raises(RuntimeError, match="task_registry"):
            facade.list_session_tasks("sess-A")

    def test_list_session_tasks_returns_descriptors(self) -> None:
        facade = _make_facade()
        facade.register_task(_make_descriptor())
        tasks = facade.list_session_tasks("sess-1")
        assert all(isinstance(t, TaskDescriptor) for t in tasks)


# ---------------------------------------------------------------------------
# Constructor — task_registry parameter
# ---------------------------------------------------------------------------


class TestConstructorTaskRegistry:
    def test_facade_constructed_without_registry(self) -> None:
        facade = KernelFacade(workflow_gateway=_make_gateway())
        assert facade._task_registry is None

    def test_facade_constructed_with_registry(self) -> None:
        reg = TaskRegistry()
        facade = KernelFacade(workflow_gateway=_make_gateway(), task_registry=reg)
        assert facade._task_registry is reg

    def test_facade_get_manifest_unaffected_by_task_registry(self) -> None:
        reg = TaskRegistry()
        facade = KernelFacade(workflow_gateway=_make_gateway(), task_registry=reg)
        manifest = facade.get_manifest()
        assert manifest is not None
        assert manifest.kernel_version is not None
