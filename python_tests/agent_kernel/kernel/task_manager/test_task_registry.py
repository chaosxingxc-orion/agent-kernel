"""Tests for TaskRegistry: registration, attempt tracking, health, eviction."""

from __future__ import annotations

import time

import pytest

from agent_kernel.kernel.task_manager.contracts import (
    TaskAttempt,
    TaskDescriptor,
    TaskRestartPolicy,
)
from agent_kernel.kernel.task_manager.registry import TaskRegistry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_descriptor(
    task_id: str = "t1",
    session_id: str = "s1",
    task_kind: str = "root",
    goal: str = "do something",
    max_attempts: int = 3,
    heartbeat_timeout_ms: int = 300_000,
) -> TaskDescriptor:
    return TaskDescriptor(
        task_id=task_id,
        session_id=session_id,
        task_kind=task_kind,  # type: ignore[arg-type]
        goal_description=goal,
        restart_policy=TaskRestartPolicy(
            max_attempts=max_attempts,
            heartbeat_timeout_ms=heartbeat_timeout_ms,
        ),
    )


def _make_attempt(
    task_id: str = "t1",
    run_id: str = "r1",
    attempt_seq: int = 1,
) -> TaskAttempt:
    return TaskAttempt(
        attempt_id=f"a-{task_id}-{attempt_seq}",
        task_id=task_id,
        run_id=run_id,
        attempt_seq=attempt_seq,
        started_at="2026-01-01T00:00:00+00:00",
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_register_and_get(self) -> None:
        reg = TaskRegistry()
        d = _make_descriptor()
        reg.register(d)
        assert reg.get("t1") == d

    def test_get_unknown_returns_none(self) -> None:
        reg = TaskRegistry()
        assert reg.get("no-such-task") is None

    def test_duplicate_registration_raises(self) -> None:
        reg = TaskRegistry()
        reg.register(_make_descriptor())
        with pytest.raises(ValueError, match="already registered"):
            reg.register(_make_descriptor())

    def test_initial_lifecycle_state_is_pending(self) -> None:
        reg = TaskRegistry()
        reg.register(_make_descriptor())
        health = reg.get_health("t1")
        assert health is not None
        assert health.lifecycle_state == "pending"

    def test_session_index_populated(self) -> None:
        reg = TaskRegistry()
        reg.register(_make_descriptor(task_id="t1", session_id="sess-A"))
        reg.register(_make_descriptor(task_id="t2", session_id="sess-A"))
        tasks = reg.list_session_tasks("sess-A")
        assert len(tasks) == 2
        ids = {t.task_id for t in tasks}
        assert ids == {"t1", "t2"}

    def test_list_session_tasks_unknown_session(self) -> None:
        reg = TaskRegistry()
        assert reg.list_session_tasks("no-session") == []


# ---------------------------------------------------------------------------
# Attempt tracking
# ---------------------------------------------------------------------------


class TestAttemptTracking:
    def test_start_attempt_transitions_to_running(self) -> None:
        reg = TaskRegistry()
        reg.register(_make_descriptor())
        reg.start_attempt(_make_attempt())
        health = reg.get_health("t1")
        assert health is not None
        assert health.lifecycle_state == "running"
        assert health.current_run_id == "r1"

    def test_start_attempt_unknown_task_raises(self) -> None:
        reg = TaskRegistry()
        with pytest.raises(KeyError):
            reg.start_attempt(_make_attempt(task_id="ghost"))

    def test_complete_attempt_completed(self) -> None:
        reg = TaskRegistry()
        reg.register(_make_descriptor())
        reg.start_attempt(_make_attempt())
        reg.complete_attempt("t1", "r1", "completed")
        health = reg.get_health("t1")
        assert health is not None
        assert health.lifecycle_state == "completed"
        assert health.current_run_id is None

    def test_complete_attempt_failed(self) -> None:
        reg = TaskRegistry()
        reg.register(_make_descriptor())
        reg.start_attempt(_make_attempt())
        reg.complete_attempt("t1", "r1", "failed")
        health = reg.get_health("t1")
        assert health is not None
        assert health.lifecycle_state == "failed"

    def test_complete_attempt_unknown_task_is_noop(self) -> None:
        reg = TaskRegistry()
        # Should not raise
        reg.complete_attempt("no-such", "r1", "completed")

    def test_complete_attempt_wrong_run_id_does_not_transition_state(self) -> None:
        reg = TaskRegistry()
        reg.register(_make_descriptor())
        reg.start_attempt(_make_attempt())
        reg.complete_attempt("t1", "wrong-run", "completed")
        health = reg.get_health("t1")
        assert health is not None
        assert health.lifecycle_state == "running"
        assert health.current_run_id == "r1"

    def test_get_attempts_empty_for_new_task(self) -> None:
        reg = TaskRegistry()
        reg.register(_make_descriptor())
        assert reg.get_attempts("t1") == []

    def test_get_attempts_reflects_history(self) -> None:
        reg = TaskRegistry()
        reg.register(_make_descriptor())
        a1 = _make_attempt(attempt_seq=1)
        reg.start_attempt(a1)
        reg.complete_attempt("t1", "r1", "failed")
        a2 = _make_attempt(run_id="r2", attempt_seq=2)
        reg.start_attempt(a2)
        attempts = reg.get_attempts("t1")
        assert len(attempts) == 2
        assert attempts[0].attempt_seq == 1
        assert attempts[1].attempt_seq == 2

    def test_attempt_seq_reflected_in_health(self) -> None:
        reg = TaskRegistry()
        reg.register(_make_descriptor())
        reg.start_attempt(_make_attempt(attempt_seq=1))
        health = reg.get_health("t1")
        assert health is not None
        assert health.attempt_seq == 1


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------


class TestUpdateState:
    def test_update_state_to_restarting(self) -> None:
        reg = TaskRegistry()
        reg.register(_make_descriptor())
        reg.update_state("t1", "restarting")
        health = reg.get_health("t1")
        assert health is not None
        assert health.lifecycle_state == "restarting"

    def test_update_state_unknown_task_is_noop(self) -> None:
        reg = TaskRegistry()
        reg.update_state("ghost", "aborted")  # should not raise

    def test_all_terminal_states(self) -> None:
        for state in ("completed", "aborted", "escalated", "reflecting"):
            reg = TaskRegistry()
            reg.register(_make_descriptor())
            reg.update_state("t1", state)
            health = reg.get_health("t1")
            assert health is not None
            assert health.lifecycle_state == state


# ---------------------------------------------------------------------------
# Heartbeat & stall detection
# ---------------------------------------------------------------------------


class TestHeartbeat:
    def test_heartbeat_resets_missed_beats(self) -> None:
        reg = TaskRegistry()
        reg.register(_make_descriptor())
        reg.start_attempt(_make_attempt())
        reg.heartbeat("t1")
        health = reg.get_health("t1")
        assert health is not None
        assert health.consecutive_missed_beats == 0

    def test_heartbeat_for_run(self) -> None:
        reg = TaskRegistry()
        reg.register(_make_descriptor())
        reg.start_attempt(_make_attempt())
        reg.heartbeat_for_run("r1")
        health = reg.get_health("t1")
        assert health is not None
        assert health.last_heartbeat_ms is not None

    def test_heartbeat_unknown_task_is_noop(self) -> None:
        reg = TaskRegistry()
        reg.heartbeat("ghost")  # should not raise

    def test_heartbeat_for_unknown_run_is_noop(self) -> None:
        reg = TaskRegistry()
        reg.heartbeat_for_run("no-run")  # should not raise

    def test_stall_detection_when_timeout_exceeded(self) -> None:
        reg = TaskRegistry()
        reg.register(_make_descriptor(heartbeat_timeout_ms=1))
        reg.start_attempt(_make_attempt())
        # Force last_heartbeat_ms to be very old
        entry = reg._tasks["t1"]
        entry.last_heartbeat_ms = int(time.monotonic() * 1000) - 10_000
        stalled = reg.get_stalled_tasks()
        assert any(h.task_id == "t1" for h in stalled)

    def test_stall_detection_includes_restarting_state(self) -> None:
        reg = TaskRegistry()
        reg.register(_make_descriptor(heartbeat_timeout_ms=1))
        reg.start_attempt(_make_attempt())
        reg.update_state("t1", "restarting")
        entry = reg._tasks["t1"]
        entry.last_heartbeat_ms = int(time.monotonic() * 1000) - 10_000
        stalled = reg.get_stalled_tasks()
        assert any(h.task_id == "t1" for h in stalled)

    def test_no_stall_for_completed_task(self) -> None:
        reg = TaskRegistry()
        reg.register(_make_descriptor(heartbeat_timeout_ms=1))
        reg.start_attempt(_make_attempt())
        reg.complete_attempt("t1", "r1", "completed")
        stalled = reg.get_stalled_tasks()
        assert not any(h.task_id == "t1" for h in stalled)

    def test_is_stalled_flag_in_get_health(self) -> None:
        reg = TaskRegistry()
        reg.register(_make_descriptor(heartbeat_timeout_ms=1))
        reg.start_attempt(_make_attempt())
        entry = reg._tasks["t1"]
        entry.last_heartbeat_ms = int(time.monotonic() * 1000) - 10_000
        health = reg.get_health("t1")
        assert health is not None
        assert health.is_stalled is True


# ---------------------------------------------------------------------------
# Eviction
# ---------------------------------------------------------------------------


class TestEviction:
    def test_eviction_on_max_tasks_exceeded(self) -> None:
        reg = TaskRegistry(max_tasks=3)
        for i in range(4):
            d = _make_descriptor(task_id=f"t{i}")
            reg.register(d)
            if i < 3:
                # Mark first 3 as terminal so they are eviction candidates
                reg.update_state(f"t{i}", "completed")
        # After registering t3, eviction should have removed some completed tasks
        # Registry should have <= max_tasks tasks
        assert len(reg._tasks) <= 3

    def test_non_terminal_tasks_not_evicted(self) -> None:
        reg = TaskRegistry(max_tasks=2)
        reg.register(_make_descriptor(task_id="active"))
        reg.start_attempt(_make_attempt(task_id="active"))
        # Register a second one to trigger eviction attempt
        reg.register(_make_descriptor(task_id="pending"))
        # Active (running) task must not be evicted
        assert reg.get("active") is not None
