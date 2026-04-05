"""Tests for DrainCoordinator graceful-drain in-flight tracking."""

from __future__ import annotations

import asyncio

import pytest

from agent_kernel.runtime.drain_coordinator import DrainCoordinator


@pytest.mark.asyncio
async def test_wait_returns_true_when_no_in_flight() -> None:
    coordinator = DrainCoordinator()
    assert coordinator.in_flight_count == 0
    assert await coordinator.wait(timeout_s=0.01) is True


@pytest.mark.asyncio
async def test_wait_times_out_when_in_flight_remains() -> None:
    coordinator = DrainCoordinator()
    await coordinator.enter()
    assert coordinator.in_flight_count == 1
    assert await coordinator.wait(timeout_s=0.01) is False


@pytest.mark.asyncio
async def test_exit_unblocks_waiters() -> None:
    coordinator = DrainCoordinator()
    await coordinator.enter()

    async def _release() -> None:
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
    coordinator = DrainCoordinator()
    await coordinator.exit()
    assert coordinator.in_flight_count == 0
    assert await coordinator.wait(timeout_s=0.01) is True


@pytest.mark.asyncio
async def test_concurrent_enter_exit_balances_to_zero() -> None:
    coordinator = DrainCoordinator()

    async def _work() -> None:
        await coordinator.enter()
        await asyncio.sleep(0)
        await coordinator.exit()

    await asyncio.gather(*[_work() for _ in range(50)])
    assert coordinator.in_flight_count == 0
    assert await coordinator.wait(timeout_s=0.1) is True
