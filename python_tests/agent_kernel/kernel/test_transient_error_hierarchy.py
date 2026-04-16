"""Verifies for transient-error hierarchy and error-aware retry behavior."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_kernel.kernel.contracts import (
    ConnectionTransientError,
    RateLimitTransientError,
    ServiceOverloadTransientError,
    TimeoutTransientError,
    TransientExecutionError,
)
from agent_kernel.kernel.retry_executor import RetryingExecutorService


class TestTransientErrorHierarchy:
    """Validate transient error metadata and base-class compatibility."""

    def test_connection_error_marks_not_executed(self) -> None:
        """Verifies connection error marks not executed."""
        err = ConnectionTransientError("tcp reset")
        assert err.may_have_executed is False
        assert isinstance(err, TransientExecutionError)

    def test_timeout_error_marks_may_have_executed(self) -> None:
        """Verifies timeout error marks may have executed."""
        err = TimeoutTransientError("timeout")
        assert err.may_have_executed is True

    def test_rate_limit_sets_retry_hint(self) -> None:
        """Verifies rate limit sets retry hint."""
        err = RateLimitTransientError("429", retry_after_ms=5000)
        assert err.retry_after_ms == 5000
        assert err.backoff_hint_ms == 5000
        assert err.may_have_executed is False

    def test_service_overload_keeps_backward_compatible_base_type(self) -> None:
        """Verifies service overload keeps backward compatible base type."""
        err = ServiceOverloadTransientError("503")
        assert isinstance(err, TransientExecutionError)
        assert err.may_have_executed is False


class TestRetryingExecutorErrorAwareBackoff:
    """Validate retry delay adapts to transient error subclasses."""

    @pytest.mark.asyncio
    async def test_connection_error_uses_short_backoff(self) -> None:
        """Verifies connection error uses short backoff."""
        inner = MagicMock()
        inner.execute = AsyncMock(side_effect=[ConnectionTransientError("fail"), "ok"])
        svc = RetryingExecutorService(inner, max_attempts=2, base_delay_ms=100, jitter_ms=0)
        with patch("asyncio.sleep", new_callable=AsyncMock) as sleep_mock:
            result = await svc.execute(MagicMock())
        assert result == "ok"
        sleep_mock.assert_awaited_once_with(0.1)

    @pytest.mark.asyncio
    async def test_timeout_error_uses_longer_backoff(self) -> None:
        """Verifies timeout error uses longer backoff."""
        inner = MagicMock()
        inner.execute = AsyncMock(side_effect=[TimeoutTransientError("timeout"), "ok"])
        svc = RetryingExecutorService(inner, max_attempts=2, base_delay_ms=100, jitter_ms=0)
        with patch("asyncio.sleep", new_callable=AsyncMock) as sleep_mock:
            await svc.execute(MagicMock())
        sleep_mock.assert_awaited_once_with(0.4)

    @pytest.mark.asyncio
    async def test_rate_limit_respects_retry_after_hint(self) -> None:
        """Verifies rate limit respects retry after hint."""
        inner = MagicMock()
        inner.execute = AsyncMock(
            side_effect=[RateLimitTransientError("429", retry_after_ms=2000), "ok"]
        )
        svc = RetryingExecutorService(inner, max_attempts=2, base_delay_ms=100, jitter_ms=0)
        with patch("asyncio.sleep", new_callable=AsyncMock) as sleep_mock:
            await svc.execute(MagicMock())
        sleep_mock.assert_awaited_once_with(2.0)

    @pytest.mark.asyncio
    async def test_service_overload_notifies_observability_hook(self) -> None:
        """Verifies service overload notifies observability hook."""
        hook = MagicMock()
        inner = MagicMock()
        inner.execute = AsyncMock(side_effect=[ServiceOverloadTransientError("503"), "ok"])
        svc = RetryingExecutorService(
            inner,
            max_attempts=2,
            base_delay_ms=100,
            jitter_ms=0,
            observability_hook=hook,
        )
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await svc.execute(MagicMock(run_id="run-1", effect_class="read_only"))
        hook.on_circuit_breaker_trip.assert_called_once()

    @pytest.mark.asyncio
    async def test_base_transient_error_remains_supported(self) -> None:
        """Verifies base transient error remains supported."""
        inner = MagicMock()
        inner.execute = AsyncMock(side_effect=[TransientExecutionError("old"), "ok"])
        svc = RetryingExecutorService(inner, max_attempts=2, base_delay_ms=10, jitter_ms=0)
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await svc.execute(MagicMock())
        assert result == "ok"
