"""Tests for ReflectionOrchestrator and reflection_context_to_recovery_dict.

Covers:
- reflection_context_to_recovery_dict produces correct shape
- ReflectionOrchestrator.reflect_and_infer() calls ReasoningLoop.run_once
  with recovery_context populated from ReflectionBridge output
- Idempotency key is forwarded to the loop
- Works when attempt list is empty (edge case)
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_kernel.kernel.task_manager.contracts import (
    TaskAttempt,
    TaskDescriptor,
    TaskRestartPolicy,
)
from agent_kernel.kernel.task_manager.reflection_bridge import ReflectionBridge
from agent_kernel.kernel.task_manager.reflection_orchestrator import (
    ReflectionOrchestrator,
    reflection_context_to_recovery_dict,
)

# ---------------------------------------------------------------------------
# Helpers / stubs
# ---------------------------------------------------------------------------


def _descriptor(task_id: str = "task-orch-1") -> TaskDescriptor:
    return TaskDescriptor(
        task_id=task_id,
        session_id="sess-orch",
        task_kind="root",
        goal_description="accomplish something important",
        restart_policy=TaskRestartPolicy(max_attempts=3),
    )


def _attempt(task_id: str, seq: int, run_id: str | None = None) -> TaskAttempt:
    return TaskAttempt(
        attempt_id=uuid.uuid4().hex,
        task_id=task_id,
        run_id=run_id or f"run-{uuid.uuid4().hex[:6]}",
        attempt_seq=seq,
        started_at=datetime.datetime.now(datetime.UTC).isoformat(),
        outcome="failed",
    )


@dataclass
class _FakeReasoningResult:
    actions: list[Any]
    model_output: Any
    context_window: Any
    inference_config: Any


def _make_loop_mock(result: Any | None = None) -> AsyncMock:
    """Return an AsyncMock for ReasoningLoop with run_once returning result."""
    loop = MagicMock()
    loop.run_once = AsyncMock(
        return_value=result
        or _FakeReasoningResult(
            actions=[],
            model_output=None,
            context_window=None,
            inference_config=None,
        )
    )
    return loop


def _make_snapshot() -> MagicMock:
    return MagicMock()


def _make_inference_config() -> MagicMock:
    return MagicMock()


# ---------------------------------------------------------------------------
# reflection_context_to_recovery_dict
# ---------------------------------------------------------------------------


class TestReflectionContextToRecoveryDict:
    def test_has_reflection_key(self) -> None:
        bridge = ReflectionBridge()
        desc = _descriptor()
        attempts = [_attempt(desc.task_id, 1), _attempt(desc.task_id, 2)]
        ctx = bridge.build_context(desc, attempts)
        d = reflection_context_to_recovery_dict(ctx)
        assert "reflection" in d

    def test_reflection_contains_task_id(self) -> None:
        bridge = ReflectionBridge()
        desc = _descriptor("task-dict-check")
        ctx = bridge.build_context(desc, [])
        d = reflection_context_to_recovery_dict(ctx)
        assert d["reflection"]["task_id"] == "task-dict-check"

    def test_reflection_contains_attempt_count(self) -> None:
        bridge = ReflectionBridge()
        desc = _descriptor()
        attempts = [_attempt(desc.task_id, i + 1) for i in range(3)]
        ctx = bridge.build_context(desc, attempts)
        d = reflection_context_to_recovery_dict(ctx)
        assert d["reflection"]["attempt_count"] == 3

    def test_has_prompt_fragment(self) -> None:
        bridge = ReflectionBridge()
        desc = _descriptor()
        ctx = bridge.build_context(desc, [])
        d = reflection_context_to_recovery_dict(ctx)
        assert "prompt_fragment" in d
        assert len(d["prompt_fragment"]) > 0

    def test_recovery_kind_is_task_reflection(self) -> None:
        bridge = ReflectionBridge()
        ctx = bridge.build_context(_descriptor(), [])
        d = reflection_context_to_recovery_dict(ctx)
        assert d["recovery_kind"] == "task_reflection"

    def test_suggested_actions_forwarded(self) -> None:
        bridge = ReflectionBridge()
        desc = _descriptor()
        ctx = bridge.build_context(desc, [])
        d = reflection_context_to_recovery_dict(ctx)
        assert isinstance(d["reflection"]["suggested_actions"], list)
        assert len(d["reflection"]["suggested_actions"]) > 0


# ---------------------------------------------------------------------------
# ReflectionOrchestrator
# ---------------------------------------------------------------------------


class TestReflectionOrchestrator:
    @pytest.mark.asyncio
    async def test_calls_reasoning_loop_run_once(self) -> None:
        bridge = ReflectionBridge()
        loop = _make_loop_mock()
        orch = ReflectionOrchestrator(bridge=bridge, loop=loop)
        desc = _descriptor()
        attempts = [_attempt(desc.task_id, 1)]
        await orch.reflect_and_infer(
            descriptor=desc,
            attempts=attempts,
            run_id="run-test-1",
            snapshot=_make_snapshot(),
            history=[],
            inference_config=_make_inference_config(),
        )
        loop.run_once.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_recovery_context_passed_to_loop(self) -> None:
        bridge = ReflectionBridge()
        loop = _make_loop_mock()
        orch = ReflectionOrchestrator(bridge=bridge, loop=loop)
        desc = _descriptor()
        attempts = [_attempt(desc.task_id, 1), _attempt(desc.task_id, 2)]
        await orch.reflect_and_infer(
            descriptor=desc,
            attempts=attempts,
            run_id="run-test-2",
            snapshot=_make_snapshot(),
            history=[],
            inference_config=_make_inference_config(),
        )
        call_kwargs = loop.run_once.call_args.kwargs
        assert "recovery_context" in call_kwargs
        rc = call_kwargs["recovery_context"]
        assert rc is not None
        assert "reflection" in rc
        assert rc["reflection"]["attempt_count"] == 2

    @pytest.mark.asyncio
    async def test_run_id_forwarded(self) -> None:
        bridge = ReflectionBridge()
        loop = _make_loop_mock()
        orch = ReflectionOrchestrator(bridge=bridge, loop=loop)
        desc = _descriptor()
        await orch.reflect_and_infer(
            descriptor=desc,
            attempts=[],
            run_id="specific-run-id",
            snapshot=_make_snapshot(),
            history=[],
            inference_config=_make_inference_config(),
        )
        call_kwargs = loop.run_once.call_args.kwargs
        assert call_kwargs["run_id"] == "specific-run-id"

    @pytest.mark.asyncio
    async def test_idempotency_key_forwarded(self) -> None:
        bridge = ReflectionBridge()
        loop = _make_loop_mock()
        orch = ReflectionOrchestrator(bridge=bridge, loop=loop)
        desc = _descriptor()
        idem_key = "stable-key-abc"
        await orch.reflect_and_infer(
            descriptor=desc,
            attempts=[],
            run_id="run-idem",
            snapshot=_make_snapshot(),
            history=[],
            inference_config=_make_inference_config(),
            idempotency_key=idem_key,
        )
        call_kwargs = loop.run_once.call_args.kwargs
        assert call_kwargs["idempotency_key"] == idem_key

    @pytest.mark.asyncio
    async def test_returns_reasoning_result(self) -> None:
        bridge = ReflectionBridge()
        sentinel_result = _FakeReasoningResult(
            actions=["action-a"],
            model_output=None,
            context_window=None,
            inference_config=None,
        )
        loop = _make_loop_mock(sentinel_result)
        orch = ReflectionOrchestrator(bridge=bridge, loop=loop)
        desc = _descriptor()
        result = await orch.reflect_and_infer(
            descriptor=desc,
            attempts=[],
            run_id="run-ret",
            snapshot=_make_snapshot(),
            history=[],
            inference_config=_make_inference_config(),
        )
        assert result is sentinel_result

    @pytest.mark.asyncio
    async def test_empty_attempts_does_not_raise(self) -> None:
        bridge = ReflectionBridge()
        loop = _make_loop_mock()
        orch = ReflectionOrchestrator(bridge=bridge, loop=loop)
        desc = _descriptor()
        # Should not raise even with no attempts
        result = await orch.reflect_and_infer(
            descriptor=desc,
            attempts=[],
            run_id="run-empty",
            snapshot=_make_snapshot(),
            history=[],
            inference_config=_make_inference_config(),
        )
        assert result is not None

    @pytest.mark.asyncio
    async def test_reflection_content_in_recovery_context(self) -> None:
        bridge = ReflectionBridge()
        loop = _make_loop_mock()
        orch = ReflectionOrchestrator(bridge=bridge, loop=loop)
        desc = _descriptor("task-content-check")
        attempts = [_attempt(desc.task_id, 1)]
        await orch.reflect_and_infer(
            descriptor=desc,
            attempts=attempts,
            run_id="run-content",
            snapshot=_make_snapshot(),
            history=[],
            inference_config=_make_inference_config(),
        )
        rc = loop.run_once.call_args.kwargs["recovery_context"]
        assert rc["reflection"]["task_id"] == "task-content-check"
        assert rc["reflection"]["goal_description"] == desc.goal_description
        assert rc["recovery_kind"] == "task_reflection"
