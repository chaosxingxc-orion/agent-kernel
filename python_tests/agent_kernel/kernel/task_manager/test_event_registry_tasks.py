"""Tests for task.* event types in KERNEL_EVENT_REGISTRY."""

from __future__ import annotations

import pytest

from agent_kernel.kernel.event_registry import KERNEL_EVENT_REGISTRY

# The 7 task-lifecycle event types that must be registered.
_EXPECTED_TASK_EVENTS = {
    "task.registered",
    "task.attempt_started",
    "task.attempt_completed",
    "task.attempt_failed",
    "task.restarting",
    "task.reflecting",
    "task.completed",
}


class TestTaskEventTypes:
    def test_all_task_event_types_registered(self) -> None:
        known = KERNEL_EVENT_REGISTRY.known_types()
        missing = _EXPECTED_TASK_EVENTS - known
        assert not missing, f"Missing task event types: {missing}"

    @pytest.mark.parametrize("event_type", sorted(_EXPECTED_TASK_EVENTS))
    def test_task_event_type_has_descriptor(self, event_type: str) -> None:
        descriptor = KERNEL_EVENT_REGISTRY.get(event_type)
        assert descriptor is not None, f"No descriptor for {event_type}"

    @pytest.mark.parametrize("event_type", sorted(_EXPECTED_TASK_EVENTS))
    def test_task_event_authority_is_task_manager(self, event_type: str) -> None:
        descriptor = KERNEL_EVENT_REGISTRY.get(event_type)
        assert descriptor is not None
        assert descriptor.authority == "TaskManager", (
            f"{event_type} has authority={descriptor.authority!r}, expected 'TaskManager'"
        )

    def test_task_registered_affects_replay(self) -> None:
        d = KERNEL_EVENT_REGISTRY.get("task.registered")
        assert d is not None
        assert d.affects_replay is True

    def test_task_completed_affects_replay(self) -> None:
        d = KERNEL_EVENT_REGISTRY.get("task.completed")
        assert d is not None
        assert d.affects_replay is True

    def test_task_attempt_started_affects_replay(self) -> None:
        d = KERNEL_EVENT_REGISTRY.get("task.attempt_started")
        assert d is not None
        assert d.affects_replay is True

    def test_task_event_descriptions_non_empty(self) -> None:
        for event_type in _EXPECTED_TASK_EVENTS:
            d = KERNEL_EVENT_REGISTRY.get(event_type)
            assert d is not None
            assert d.description, f"{event_type} has empty description"

    def test_known_types_returns_frozenset(self) -> None:
        known = KERNEL_EVENT_REGISTRY.known_types()
        assert isinstance(known, frozenset)

    def test_task_events_are_subset_of_all_known(self) -> None:
        known = KERNEL_EVENT_REGISTRY.known_types()
        assert _EXPECTED_TASK_EVENTS.issubset(known)
