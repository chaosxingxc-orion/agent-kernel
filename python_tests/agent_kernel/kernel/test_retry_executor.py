"""Tests for RetryingExecutorService (P3b)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from agent_kernel.kernel.contracts import TransientExecutionError
from agent_kernel.kernel.retry_executor import RetryingExecutorService


@dataclass(slots=True)
class _CountingExecutor:
    """Inner executor stub that records calls and can inject failures."""

    results: list[Any]
    calls: int = 0

    async def execute(self, action: Any, **kwargs: Any) -> Any:
        result = self.results[self.calls]
        self.calls += 1
        if isinstance(result, BaseException):
            raise result
        return result


class TestRetryingExecutorServiceInit:
    def test_max_attempts_zero_raises(self) -> None:
        inner = _CountingExecutor(results=[])
        with pytest.raises(ValueError):
            RetryingExecutorService(inner, max_attempts=0)

    def test_max_attempts_one_is_valid(self) -> None:
        inner = _CountingExecutor(results=[{"ok": True}])
        svc = RetryingExecutorService(inner, max_attempts=1)
        result = asyncio.run(svc.execute(object()))
        assert result == {"ok": True}


class TestRetryingExecutorServiceSuccessPaths:
    def test_succeeds_on_first_attempt(self) -> None:
        inner = _CountingExecutor(results=[{"ack": True}])
        svc = RetryingExecutorService(inner, max_attempts=2, base_delay_ms=0, jitter_ms=0)
        result = asyncio.run(svc.execute(object()))
        assert result == {"ack": True}
        assert inner.calls == 1

    def test_succeeds_after_one_transient_failure(self) -> None:
        inner = _CountingExecutor(
            results=[TransientExecutionError("flaky"), {"ack": True}]
        )
        svc = RetryingExecutorService(inner, max_attempts=2, base_delay_ms=0, jitter_ms=0)
        result = asyncio.run(svc.execute(object()))
        assert result == {"ack": True}
        assert inner.calls == 2

    def test_succeeds_after_two_transient_failures(self) -> None:
        inner = _CountingExecutor(
            results=[
                TransientExecutionError("flaky"),
                TransientExecutionError("flaky again"),
                {"ack": True},
            ]
        )
        svc = RetryingExecutorService(inner, max_attempts=3, base_delay_ms=0, jitter_ms=0)
        result = asyncio.run(svc.execute(object()))
        assert result == {"ack": True}
        assert inner.calls == 3


class TestRetryingExecutorServiceFailurePaths:
    def test_raises_transient_error_after_all_attempts_exhausted(self) -> None:
        inner = _CountingExecutor(
            results=[
                TransientExecutionError("always fails"),
                TransientExecutionError("still fails"),
            ]
        )
        svc = RetryingExecutorService(inner, max_attempts=2, base_delay_ms=0, jitter_ms=0)
        with pytest.raises(TransientExecutionError):
            asyncio.run(svc.execute(object()))
        assert inner.calls == 2

    def test_non_transient_error_propagates_immediately(self) -> None:
        inner = _CountingExecutor(results=[RuntimeError("permanent"), {"ack": True}])
        svc = RetryingExecutorService(inner, max_attempts=3, base_delay_ms=0, jitter_ms=0)
        with pytest.raises(RuntimeError):
            asyncio.run(svc.execute(object()))
        # Should not retry non-transient errors
        assert inner.calls == 1

    def test_kwargs_forwarded_to_inner_executor(self) -> None:
        received_kwargs: list[dict[str, Any]] = []

        class _KwargsCapturingExecutor:
            async def execute(self, action: Any, **kwargs: Any) -> Any:
                received_kwargs.append(kwargs)
                return {"ok": True}

        svc = RetryingExecutorService(_KwargsCapturingExecutor(), max_attempts=2)
        asyncio.run(svc.execute(object(), grant_ref="ref-123"))
        assert received_kwargs == [{"grant_ref": "ref-123"}]
