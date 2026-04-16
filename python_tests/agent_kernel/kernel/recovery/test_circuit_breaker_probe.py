"""Test suite for CircuitBreakerProbeScheduler."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import pytest

from agent_kernel.kernel.contracts import CircuitBreakerPolicy
from agent_kernel.kernel.recovery.circuit_breaker_probe import CircuitBreakerProbeScheduler


@dataclass
class _StoreStub:
    """Test suite for  StoreStub."""

    states: dict[str, tuple[int, float]] = field(default_factory=dict)
    resets: list[str] = field(default_factory=list)

    def get_state(self, effect_class: str) -> tuple[int, float]:
        """Get state."""
        return self.states.get(effect_class, (0, 0.0))

    def reset(self, effect_class: str) -> None:
        """Resets test state."""
        self.resets.append(effect_class)
        self.states[effect_class] = (0, 0.0)

    def list_effect_classes(self) -> list[str]:
        """List effect classes."""
        return sorted(self.states.keys())


@pytest.mark.asyncio
async def test_probe_once_resets_open_breaker_when_probe_succeeds() -> None:
    """Verifies probe once resets open breaker when probe succeeds."""
    now = time.time()
    store = _StoreStub(states={"write": (5, now - 60)})
    policy = CircuitBreakerPolicy(threshold=3, half_open_after_ms=1_000)

    async def _probe() -> bool:
        """Probes a test dependency."""
        return True

    scheduler = CircuitBreakerProbeScheduler(
        circuit_breaker_store=store,  # type: ignore[arg-type]
        policy=policy,
        probe_fns={"write": _probe},
    )
    closed = await scheduler.probe_once()
    assert closed == ["write"]
    assert store.resets == ["write"]


@pytest.mark.asyncio
async def test_probe_once_skips_when_under_threshold() -> None:
    """Verifies probe once skips when under threshold."""
    store = _StoreStub(states={"write": (1, time.time() - 60)})
    policy = CircuitBreakerPolicy(threshold=3, half_open_after_ms=1)
    called = False

    async def _probe() -> bool:
        """Probes a test dependency."""
        nonlocal called
        called = True
        return True

    scheduler = CircuitBreakerProbeScheduler(
        circuit_breaker_store=store,  # type: ignore[arg-type]
        policy=policy,
        probe_fns={"write": _probe},
    )
    closed = await scheduler.probe_once()
    assert closed == []
    assert called is False
    assert store.resets == []


@pytest.mark.asyncio
async def test_probe_once_skips_when_no_probe_function_registered() -> None:
    """Verifies probe once skips when no probe function registered."""
    store = _StoreStub(states={"write": (5, time.time() - 60)})
    policy = CircuitBreakerPolicy(threshold=3, half_open_after_ms=1)
    scheduler = CircuitBreakerProbeScheduler(
        circuit_breaker_store=store,  # type: ignore[arg-type]
        policy=policy,
        probe_fns={},
    )
    closed = await scheduler.probe_once()
    assert closed == []
    assert store.resets == []


@pytest.mark.asyncio
async def test_start_returns_same_task_when_already_running() -> None:
    """Verifies start returns same task when already running."""
    store = _StoreStub()
    policy = CircuitBreakerPolicy()

    async def _probe() -> bool:
        """Probes a test dependency."""
        return True

    scheduler = CircuitBreakerProbeScheduler(
        circuit_breaker_store=store,  # type: ignore[arg-type]
        policy=policy,
        probe_fns={"write": _probe},
        interval_s=60.0,
    )
    task_1 = scheduler.start()
    task_2 = scheduler.start()
    assert task_1 is task_2
    task_1.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task_1
