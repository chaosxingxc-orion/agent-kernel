"""Verifies for taskregistry: registration, attempt tracking, health, eviction."""

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
    """Make descriptor."""
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
    """Make attempt."""
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
    """Test suite for Registration."""

    def test_register_and_get(self) -> None:
        """Verifies register and get."""
        reg = TaskRegistry()
        d = _make_descriptor()
        reg.register(d)
        assert reg.get("t1") == d

    def test_get_unknown_returns_none(self) -> None:
        """Verifies get unknown returns none."""
        reg = TaskRegistry()
        assert reg.get("no-such-task") is None

    def test_duplicate_registration_raises(self) -> None:
        """Verifies duplicate registration raises."""
        reg = TaskRegistry()
        reg.register(_make_descriptor())
        with pytest.raises(ValueError, match="already registered"):
            reg.register(_make_descriptor())

    def test_initial_lifecycle_state_is_pending(self) -> None:
        """Verifies initial lifecycle state is pending."""
        reg = TaskRegistry()
        reg.register(_make_descriptor())
        health = reg.get_health("t1")
        assert health is not None
        assert health.lifecycle_state == "pending"

    def test_session_index_populated(self) -> None:
        """Verifies session index populated."""
        reg = TaskRegistry()
        reg.register(_make_descriptor(task_id="t1", session_id="sess-A"))
        reg.register(_make_descriptor(task_id="t2", session_id="sess-A"))
        tasks = reg.list_session_tasks("sess-A")
        assert len(tasks) == 2
        ids = {t.task_id for t in tasks}
        assert ids == {"t1", "t2"}

    def test_list_session_tasks_unknown_session(self) -> None:
        """Verifies list session tasks unknown session."""
        reg = TaskRegistry()
        assert reg.list_session_tasks("no-session") == []


# ---------------------------------------------------------------------------
# Attempt tracking
# ---------------------------------------------------------------------------


class TestAttemptTracking:
    """Test suite for AttemptTracking."""

    def test_start_attempt_transitions_to_running(self) -> None:
        """Verifies start attempt transitions to running."""
        reg = TaskRegistry()
        reg.register(_make_descriptor())
        reg.start_attempt(_make_attempt())
        health = reg.get_health("t1")
        assert health is not None
        assert health.lifecycle_state == "running"
        assert health.current_run_id == "r1"

    def test_start_attempt_unknown_task_raises(self) -> None:
        """Verifies start attempt unknown task raises."""
        reg = TaskRegistry()
        with pytest.raises(KeyError):
            reg.start_attempt(_make_attempt(task_id="ghost"))

    def test_complete_attempt_completed(self) -> None:
        """Verifies complete attempt completed."""
        reg = TaskRegistry()
        reg.register(_make_descriptor())
        reg.start_attempt(_make_attempt())
        reg.complete_attempt("t1", "r1", "completed")
        health = reg.get_health("t1")
        assert health is not None
        assert health.lifecycle_state == "completed"
        assert health.current_run_id is None

    def test_complete_attempt_failed(self) -> None:
        """Verifies complete attempt failed."""
        reg = TaskRegistry()
        reg.register(_make_descriptor())
        reg.start_attempt(_make_attempt())
        reg.complete_attempt("t1", "r1", "failed")
        health = reg.get_health("t1")
        assert health is not None
        assert health.lifecycle_state == "failed"

    def test_complete_attempt_unknown_task_is_noop(self) -> None:
        """Verifies complete attempt unknown task is noop."""
        reg = TaskRegistry()
        # Should not raise
        reg.complete_attempt("no-such", "r1", "completed")

    def test_complete_attempt_wrong_run_id_does_not_transition_state(self) -> None:
        """Verifies complete attempt wrong run id does not transition state."""
        reg = TaskRegistry()
        reg.register(_make_descriptor())
        reg.start_attempt(_make_attempt())
        reg.complete_attempt("t1", "wrong-run", "completed")
        health = reg.get_health("t1")
        assert health is not None
        assert health.lifecycle_state == "running"
        assert health.current_run_id == "r1"

    def test_get_attempts_empty_for_new_task(self) -> None:
        """Verifies get attempts empty for new task."""
        reg = TaskRegistry()
        reg.register(_make_descriptor())
        assert reg.get_attempts("t1") == []

    def test_get_attempts_reflects_history(self) -> None:
        """Verifies get attempts reflects history."""
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
        """Verifies attempt seq reflected in health."""
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
    """Test suite for UpdateState."""

    def test_update_state_to_restarting(self) -> None:
        """Verifies update state to restarting."""
        reg = TaskRegistry()
        reg.register(_make_descriptor())
        reg.update_state("t1", "restarting")
        health = reg.get_health("t1")
        assert health is not None
        assert health.lifecycle_state == "restarting"

    def test_update_state_unknown_task_is_noop(self) -> None:
        """Verifies update state unknown task is noop."""
        reg = TaskRegistry()
        reg.update_state("ghost", "aborted")  # should not raise

    def test_all_terminal_states(self) -> None:
        """Verifies all terminal states."""
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
    """Test suite for Heartbeat."""

    def test_heartbeat_resets_missed_beats(self) -> None:
        """Verifies heartbeat resets missed beats."""
        reg = TaskRegistry()
        reg.register(_make_descriptor())
        reg.start_attempt(_make_attempt())
        reg.heartbeat("t1")
        health = reg.get_health("t1")
        assert health is not None
        assert health.consecutive_missed_beats == 0

    def test_heartbeat_for_run(self) -> None:
        """Verifies heartbeat for run."""
        reg = TaskRegistry()
        reg.register(_make_descriptor())
        reg.start_attempt(_make_attempt())
        reg.heartbeat_for_run("r1")
        health = reg.get_health("t1")
        assert health is not None
        assert health.last_heartbeat_ms is not None

    def test_heartbeat_unknown_task_is_noop(self) -> None:
        """Verifies heartbeat unknown task is noop."""
        reg = TaskRegistry()
        reg.heartbeat("ghost")  # should not raise

    def test_heartbeat_for_unknown_run_is_noop(self) -> None:
        """Verifies heartbeat for unknown run is noop."""
        reg = TaskRegistry()
        reg.heartbeat_for_run("no-run")  # should not raise

    def test_stall_detection_when_timeout_exceeded(self) -> None:
        """Verifies stall detection when timeout exceeded."""
        reg = TaskRegistry()
        reg.register(_make_descriptor(heartbeat_timeout_ms=1))
        reg.start_attempt(_make_attempt())
        # Force last_heartbeat_ms to be very old
        entry = reg._tasks["t1"]
        entry.last_heartbeat_ms = int(time.monotonic() * 1000) - 10_000
        stalled = reg.get_stalled_tasks()
        assert any(h.task_id == "t1" for h in stalled)

    def test_stall_detection_includes_restarting_state(self) -> None:
        """Verifies stall detection includes restarting state."""
        reg = TaskRegistry()
        reg.register(_make_descriptor(heartbeat_timeout_ms=1))
        reg.start_attempt(_make_attempt())
        reg.update_state("t1", "restarting")
        entry = reg._tasks["t1"]
        entry.last_heartbeat_ms = int(time.monotonic() * 1000) - 10_000
        stalled = reg.get_stalled_tasks()
        assert any(h.task_id == "t1" for h in stalled)

    def test_no_stall_for_completed_task(self) -> None:
        """Verifies no stall for completed task."""
        reg = TaskRegistry()
        reg.register(_make_descriptor(heartbeat_timeout_ms=1))
        reg.start_attempt(_make_attempt())
        reg.complete_attempt("t1", "r1", "completed")
        stalled = reg.get_stalled_tasks()
        assert not any(h.task_id == "t1" for h in stalled)

    def test_is_stalled_flag_in_get_health(self) -> None:
        """Verifies is stalled flag in get health."""
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
    """Test suite for Eviction."""

    def test_eviction_on_max_tasks_exceeded(self) -> None:
        """Verifies eviction on max tasks exceeded."""
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
        """Verifies non terminal tasks not evicted."""
        reg = TaskRegistry(max_tasks=2)
        reg.register(_make_descriptor(task_id="active"))
        reg.start_attempt(_make_attempt(task_id="active"))
        # Register a second one to trigger eviction attempt
        reg.register(_make_descriptor(task_id="pending"))
        # Active (running) task must not be evicted
        assert reg.get("active") is not None
