"""Tests for ReflectionBridge: context building from failure history."""

from __future__ import annotations

from unittest.mock import MagicMock

from agent_kernel.kernel.task_manager.contracts import (
    TaskAttempt,
    TaskDescriptor,
    TaskRestartPolicy,
)
from agent_kernel.kernel.task_manager.reflection_bridge import ReflectionBridge, ReflectionContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_descriptor(
    task_id: str = "t1",
    goal: str = "achieve the goal",
    max_attempts: int = 3,
) -> TaskDescriptor:
    return TaskDescriptor(
        task_id=task_id,
        session_id="s1",
        task_kind="root",
        goal_description=goal,
        restart_policy=TaskRestartPolicy(max_attempts=max_attempts),
    )


def _make_attempt(
    seq: int = 1,
    outcome: str = "failed",
    failure: object | None = None,
) -> TaskAttempt:
    return TaskAttempt(
        attempt_id=f"a{seq}",
        task_id="t1",
        run_id=f"r{seq}",
        attempt_seq=seq,
        started_at="2026-01-01T00:00:00+00:00",
        completed_at="2026-01-01T00:01:00+00:00",
        outcome=outcome,  # type: ignore[arg-type]
        failure=failure,
    )


def _make_failure(
    code: str = "TIMEOUT",
    retryability: str = "retryable",
    stage: str = "execution",
) -> MagicMock:
    f = MagicMock()
    f.failure_code = code
    f.retryability = retryability
    f.failed_stage = stage
    f.failure_class = "transient"
    f.local_inference = None
    return f


# ---------------------------------------------------------------------------
# ReflectionContext structure
# ---------------------------------------------------------------------------


class TestBuildContext:
    def test_returns_reflection_context(self) -> None:
        bridge = ReflectionBridge()
        descriptor = _make_descriptor()
        attempts = [_make_attempt(seq=1)]
        ctx = bridge.build_context(descriptor, attempts)
        assert isinstance(ctx, ReflectionContext)

    def test_reflection_context_is_frozen(self) -> None:
        bridge = ReflectionBridge()
        ctx = bridge.build_context(_make_descriptor(), [_make_attempt()])
        import pytest

        with pytest.raises((AttributeError, TypeError)):
            ctx.task_id = "mutated"  # type: ignore[misc]

    def test_task_id_matches_descriptor(self) -> None:
        bridge = ReflectionBridge()
        ctx = bridge.build_context(_make_descriptor(task_id="my-task"), [_make_attempt()])
        assert ctx.task_id == "my-task"

    def test_goal_description_preserved(self) -> None:
        bridge = ReflectionBridge()
        ctx = bridge.build_context(_make_descriptor(goal="finish the analysis"), [_make_attempt()])
        assert ctx.goal_description == "finish the analysis"

    def test_attempt_count_correct(self) -> None:
        bridge = ReflectionBridge()
        attempts = [_make_attempt(seq=i) for i in range(1, 4)]
        ctx = bridge.build_context(_make_descriptor(), attempts)
        assert ctx.attempt_count == 3

    def test_empty_attempts(self) -> None:
        bridge = ReflectionBridge()
        ctx = bridge.build_context(_make_descriptor(), [])
        assert ctx.attempt_count == 0
        assert "No attempts recorded" in ctx.failure_summary

    def test_failure_summary_mentions_codes(self) -> None:
        bridge = ReflectionBridge()
        failure = _make_failure(code="NETWORK_ERROR")
        attempts = [_make_attempt(seq=1, failure=failure)]
        ctx = bridge.build_context(_make_descriptor(), attempts)
        assert "NETWORK_ERROR" in ctx.failure_summary

    def test_failure_details_contains_attempt_seq(self) -> None:
        bridge = ReflectionBridge()
        attempts = [_make_attempt(seq=1), _make_attempt(seq=2)]
        ctx = bridge.build_context(_make_descriptor(), attempts)
        seqs = [d["attempt_seq"] for d in ctx.failure_details]
        assert seqs == [1, 2]

    def test_failure_details_includes_failure_code(self) -> None:
        bridge = ReflectionBridge()
        failure = _make_failure(code="DISK_FULL")
        attempts = [_make_attempt(seq=1, failure=failure)]
        ctx = bridge.build_context(_make_descriptor(), attempts)
        assert ctx.failure_details[0]["failure_code"] == "DISK_FULL"

    def test_failure_details_no_failure_object(self) -> None:
        bridge = ReflectionBridge()
        attempts = [_make_attempt(seq=1, failure=None)]
        ctx = bridge.build_context(_make_descriptor(), attempts)
        # No extra failure keys when failure is None
        assert "failure_code" not in ctx.failure_details[0]

    def test_suggested_actions_non_empty(self) -> None:
        bridge = ReflectionBridge()
        ctx = bridge.build_context(_make_descriptor(), [_make_attempt()])
        assert len(ctx.suggested_actions) > 0

    def test_force_retry_suggested_when_budget_remaining(self) -> None:
        bridge = ReflectionBridge()
        # 3 max_attempts, only 1 attempt made
        ctx = bridge.build_context(_make_descriptor(max_attempts=3), [_make_attempt(seq=1)])
        has_force_retry = any("force_retry" in a for a in ctx.suggested_actions)
        assert has_force_retry

    def test_force_retry_not_suggested_when_budget_exhausted(self) -> None:
        bridge = ReflectionBridge()
        attempts = [_make_attempt(seq=i) for i in range(1, 4)]
        ctx = bridge.build_context(_make_descriptor(max_attempts=3), attempts)
        has_force_retry = any("force_retry" in a for a in ctx.suggested_actions)
        assert not has_force_retry

    def test_prompt_fragment_contains_task_id(self) -> None:
        bridge = ReflectionBridge()
        ctx = bridge.build_context(_make_descriptor(task_id="task-xyz"), [_make_attempt()])
        assert "task-xyz" in ctx.prompt_fragment

    def test_prompt_fragment_contains_goal(self) -> None:
        bridge = ReflectionBridge()
        ctx = bridge.build_context(_make_descriptor(goal="run report"), [_make_attempt()])
        assert "run report" in ctx.prompt_fragment

    def test_prompt_fragment_contains_suggested_actions(self) -> None:
        bridge = ReflectionBridge()
        ctx = bridge.build_context(_make_descriptor(), [_make_attempt()])
        for action in ctx.suggested_actions:
            action_name = action.split(":")[0]
            assert action_name in ctx.prompt_fragment

    def test_deduplication_of_failure_codes_in_summary(self) -> None:
        bridge = ReflectionBridge()
        failure = _make_failure(code="TIMEOUT")
        attempts = [_make_attempt(seq=i, failure=failure) for i in range(1, 4)]
        ctx = bridge.build_context(_make_descriptor(), attempts)
        # Summary should not repeat TIMEOUT multiple times
        assert ctx.failure_summary.count("TIMEOUT") == 1
