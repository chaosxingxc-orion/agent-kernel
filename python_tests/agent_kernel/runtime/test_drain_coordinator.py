"""Verifies for draincoordinator graceful-drain in-flight tracking."""

from __future__ import annotations

import asyncio

import pytest

from agent_kernel.runtime.drain_coordinator import DrainCoordinator


@pytest.mark.asyncio
async def test_wait_returns_true_when_no_in_flight() -> None:
    """Verifies wait returns true when no in flight."""
    coordinator = DrainCoordinator()
    assert coordinator.in_flight_count == 0
    assert await coordinator.wait(timeout_s=0.01) is True


@pytest.mark.asyncio
async def test_wait_times_out_when_in_flight_remains() -> None:
    """Verifies wait times out when in flight remains."""
    coordinator = DrainCoordinator()
    await coordinator.enter()
    assert coordinator.in_flight_count == 1
    assert await coordinator.wait(timeout_s=0.01) is False


@pytest.mark.asyncio
async def test_exit_unblocks_waiters() -> None:
    """Verifies exit unblocks waiters."""
    coordinator = DrainCoordinator()
    await coordinator.enter()

    async def _release() -> None:
        """Releases the synchronization primitive."""
        await asyncio.sleep(0.01)
        await coordinator.exit()

    release_task = asyncio.create_task(_release())
    try:
        assert await coordinator.wait(timeout_s=1.0) is True
        assert coordinator.in_flight_count == 0
    finally:
        await release_task


@pytest.mark.asyncio
async def test_exit_is_safe_when_count_already_zero() -> None:
    """Verifies exit is safe when count already zero."""
    coordinator = DrainCoordinator()
    await coordinator.exit()
    assert coordinator.in_flight_count == 0
    assert await coordinator.wait(timeout_s=0.01) is True


@pytest.mark.asyncio
async def test_concurrent_enter_exit_balances_to_zero() -> None:
    """Verifies concurrent enter exit balances to zero."""
    coordinator = DrainCoordinator()

    async def _work() -> None:
        """Runs work for the test case."""
        await coordinator.enter()
        await asyncio.sleep(0)
        await coordinator.exit()

    await asyncio.gather(*[_work() for _ in range(50)])
    assert coordinator.in_flight_count == 0
    assert await coordinator.wait(timeout_s=0.1) is True
