"""Tests for InMemoryTaskEventLog, TaskEventAppender, and registry recovery.

Covers:
- TaskRegistry emits events via TaskEventAppender on all mutations
- InMemoryTaskEventLog stores events in order with eviction
- replay_into_registry() restores registry state faithfully
- Recovery is idempotent (double-replay does not corrupt state)
"""

from __future__ import annotations

import datetime
import uuid

from agent_kernel.kernel.task_manager.contracts import (
    TaskAttempt,
    TaskDescriptor,
    TaskRestartPolicy,
)
from agent_kernel.kernel.task_manager.event_log import InMemoryTaskEventLog
from agent_kernel.kernel.task_manager.registry import TaskRegistry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _descriptor(
    task_id: str | None = None,
    session_id: str = "sess-1",
    goal: str = "do something",
) -> TaskDescriptor:
    """Builds a task descriptor fixture."""
    return TaskDescriptor(
        task_id=task_id or uuid.uuid4().hex,
        session_id=session_id,
        task_kind="root",
        goal_description=goal,
        restart_policy=TaskRestartPolicy(max_attempts=3),
    )


def _attempt(task_id: str, seq: int = 1, run_id: str | None = None) -> TaskAttempt:
    """Builds a task attempt fixture."""
    return TaskAttempt(
        attempt_id=uuid.uuid4().hex,
        task_id=task_id,
        run_id=run_id or f"run-{uuid.uuid4().hex[:6]}",
        attempt_seq=seq,
        started_at=datetime.datetime.now(datetime.UTC).isoformat(),
    )


def _registry_with_log() -> tuple[TaskRegistry, InMemoryTaskEventLog]:
    """Registry with log."""
    log = InMemoryTaskEventLog()
    reg = TaskRegistry(event_appender=log)
    return reg, log


# ---------------------------------------------------------------------------
# InMemoryTaskEventLog basic behaviour
# ---------------------------------------------------------------------------


class TestInMemoryTaskEventLog:
    """Test suite for InMemoryTaskEventLog."""

    def test_append_and_retrieve(self) -> None:
        """Verifies append and retrieve."""
        log = InMemoryTaskEventLog()
        log.append_task_event("task.registered", {"task_id": "t1"})
        log.append_task_event("task.attempt_started", {"task_id": "t1", "run_id": "r1"})
        events = log.all_events()
        assert len(events) == 2
        assert events[0]["event_type"] == "task.registered"
        assert events[1]["event_type"] == "task.attempt_started"

    def test_all_events_returns_snapshot(self) -> None:
        """Verifies all events returns snapshot."""
        log = InMemoryTaskEventLog()
        log.append_task_event("task.registered", {"task_id": "t1"})
        snap1 = log.all_events()
        log.append_task_event("task.attempt_started", {"task_id": "t1"})
        snap2 = log.all_events()
        assert len(snap1) == 1
        assert len(snap2) == 2

    def test_eviction_on_overflow(self) -> None:
        """Verifies eviction on overflow."""
        log = InMemoryTaskEventLog(max_events=10)
        for i in range(15):
            log.append_task_event("task.registered", {"task_id": f"t{i}"})
        # Should have evicted oldest events; total < 15
        assert len(log.all_events()) < 15

    def test_eviction_cap_respected(self) -> None:
        """Verifies eviction cap respected."""
        cap = 8
        log = InMemoryTaskEventLog(max_events=cap)
        for i in range(20):
            log.append_task_event("task.registered", {"task_id": f"t{i}"})
        assert len(log.all_events()) <= cap


# ---------------------------------------------------------------------------
# Registry emits events via appender
# ---------------------------------------------------------------------------


class TestRegistryEmitsEvents:
    """Test suite for RegistryEmitsEvents."""

    def test_register_emits_task_registered(self) -> None:
        """Verifies register emits task registered."""
        reg, log = _registry_with_log()
        desc = _descriptor("task-emit-1")
        reg.register(desc)
        events = log.all_events()
        assert any(e["event_type"] == "task.registered" for e in events)
        reg_evt = next(e for e in events if e["event_type"] == "task.registered")
        assert reg_evt["payload"]["task_id"] == "task-emit-1"

    def test_start_attempt_emits_event(self) -> None:
        """Verifies start attempt emits event."""
        reg, log = _registry_with_log()
        desc = _descriptor("task-emit-2")
        reg.register(desc)
        attempt = _attempt("task-emit-2", seq=1)
        reg.start_attempt(attempt)
        types = [e["event_type"] for e in log.all_events()]
        assert "task.attempt_started" in types

    def test_complete_attempt_success_emits_completed(self) -> None:
        """Verifies complete attempt success emits completed."""
        reg, log = _registry_with_log()
        desc = _descriptor("task-emit-3")
        reg.register(desc)
        att = _attempt("task-emit-3", seq=1, run_id="run-ok")
        reg.start_attempt(att)
        reg.complete_attempt("task-emit-3", "run-ok", "completed")
        types = [e["event_type"] for e in log.all_events()]
        assert "task.attempt_completed" in types

    def test_complete_attempt_failure_emits_failed(self) -> None:
        """Verifies complete attempt failure emits failed."""
        reg, log = _registry_with_log()
        desc = _descriptor("task-emit-4")
        reg.register(desc)
        att = _attempt("task-emit-4", seq=1, run_id="run-fail")
        reg.start_attempt(att)
        reg.complete_attempt("task-emit-4", "run-fail", "failed")
        types = [e["event_type"] for e in log.all_events()]
        assert "task.attempt_failed" in types

    def test_update_state_reflecting_emits_event(self) -> None:
        """Verifies update state reflecting emits event."""
        reg, log = _registry_with_log()
        desc = _descriptor("task-emit-5")
        reg.register(desc)
        reg.update_state("task-emit-5", "reflecting")
        types = [e["event_type"] for e in log.all_events()]
        assert "task.reflecting" in types

    def test_update_state_restarting_emits_event(self) -> None:
        """Verifies update state restarting emits event."""
        reg, log = _registry_with_log()
        desc = _descriptor("task-emit-6")
        reg.register(desc)
        reg.update_state("task-emit-6", "restarting")
        types = [e["event_type"] for e in log.all_events()]
        assert "task.restarting" in types

    def test_update_state_completed_emits_event(self) -> None:
        """Verifies update state completed emits event."""
        reg, log = _registry_with_log()
        desc = _descriptor("task-emit-7")
        reg.register(desc)
        reg.update_state("task-emit-7", "completed")
        types = [e["event_type"] for e in log.all_events()]
        assert "task.completed" in types

    def test_no_appender_does_not_raise(self) -> None:
        """Verifies no appender does not raise."""
        reg = TaskRegistry()  # no event_appender
        desc = _descriptor("task-nolog")
        reg.register(desc)
        att = _attempt("task-nolog", seq=1)
        reg.start_attempt(att)
        reg.complete_attempt("task-nolog", att.run_id, "completed")
        # No exception — appender is optional


# ---------------------------------------------------------------------------
# replay_into_registry — state recovery
# ---------------------------------------------------------------------------


class TestReplayIntoRegistry:
    """Test suite for ReplayIntoRegistry."""

    def test_replay_restores_registered_task(self) -> None:
        """Verifies replay restores registered task."""
        reg, log = _registry_with_log()
        desc = _descriptor("task-replay-1")
        reg.register(desc)

        fresh = TaskRegistry()
        replayed = log.replay_into_registry(fresh)
        assert replayed >= 1
        assert fresh.get("task-replay-1") is not None

    def test_replay_restores_attempt_history(self) -> None:
        """Verifies replay restores attempt history."""
        reg, log = _registry_with_log()
        desc = _descriptor("task-replay-2")
        reg.register(desc)
        att = _attempt("task-replay-2", seq=1, run_id="run-r2")
        reg.start_attempt(att)

        fresh = TaskRegistry()
        log.replay_into_registry(fresh)
        attempts = fresh.get_attempts("task-replay-2")
        assert len(attempts) == 1
        assert attempts[0].run_id == "run-r2"

    def test_replay_restores_lifecycle_state(self) -> None:
        """Verifies replay restores lifecycle state."""
        reg, log = _registry_with_log()
        desc = _descriptor("task-replay-3")
        reg.register(desc)
        att = _attempt("task-replay-3", seq=1, run_id="run-r3")
        reg.start_attempt(att)
        reg.complete_attempt("task-replay-3", "run-r3", "failed")
        reg.update_state("task-replay-3", "reflecting")

        fresh = TaskRegistry()
        log.replay_into_registry(fresh)
        health = fresh.get_health("task-replay-3")
        assert health is not None
        assert health.lifecycle_state == "reflecting"

    def test_replay_is_idempotent(self) -> None:
        """Verifies replay is idempotent."""
        reg, log = _registry_with_log()
        desc = _descriptor("task-replay-idem")
        reg.register(desc)
        reg.start_attempt(_attempt("task-replay-idem", seq=1, run_id="run-idem-1"))

        fresh = TaskRegistry()
        log.replay_into_registry(fresh)
        # Second replay should not raise or duplicate
        log.replay_into_registry(fresh)
        assert fresh.get("task-replay-idem") is not None
        attempts = fresh.get_attempts("task-replay-idem")
        assert len(attempts) == 1
        assert attempts[0].run_id == "run-idem-1"

    def test_replay_multiple_tasks(self) -> None:
        """Verifies replay multiple tasks."""
        reg, log = _registry_with_log()
        ids = [f"task-multi-{i}" for i in range(5)]
        for tid in ids:
            reg.register(_descriptor(tid))

        fresh = TaskRegistry()
        log.replay_into_registry(fresh)
        for tid in ids:
            assert fresh.get(tid) is not None

    def test_replay_restores_session_index(self) -> None:
        """Verifies replay restores session index."""
        reg, log = _registry_with_log()
        for i in range(3):
            reg.register(_descriptor(f"task-sess-{i}", session_id="sess-shared"))

        fresh = TaskRegistry()
        log.replay_into_registry(fresh)
        session_tasks = fresh.list_session_tasks("sess-shared")
        assert len(session_tasks) == 3

    def test_replay_returns_event_count(self) -> None:
        """Verifies replay returns event count."""
        reg, log = _registry_with_log()
        for i in range(4):
            reg.register(_descriptor(f"task-count-{i}"))
        fresh = TaskRegistry()
        n = log.replay_into_registry(fresh)
        # At minimum 4 task.registered events
        assert n >= 4
