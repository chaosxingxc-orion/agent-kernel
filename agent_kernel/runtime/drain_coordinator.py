"""In-flight operation coordinator for graceful runtime draining."""

from __future__ import annotations

import asyncio


class DrainCoordinator:
    """Track in-flight operations and wait for drain completion."""

    def __init__(self) -> None:
        """Initialize empty in-flight counter."""
        self._lock = asyncio.Lock()
        self._in_flight_count = 0
        self._drained = asyncio.Event()
        self._drained.set()

    async def enter(self) -> None:
        """Mark operation start."""
        async with self._lock:
            self._in_flight_count += 1
            if self._in_flight_count > 0:
                self._drained.clear()

    async def exit(self) -> None:
        """Mark operation completion."""
        async with self._lock:
            self._in_flight_count = max(self._in_flight_count - 1, 0)
            if self._in_flight_count == 0:
                self._drained.set()

    async def wait(self, timeout_s: float) -> bool:
        """Wait until all in-flight operations are completed."""
        try:
            await asyncio.wait_for(self._drained.wait(), timeout=max(timeout_s, 0.0))
            return True
        except TimeoutError:
            return False

    @property
    def in_flight_count(self) -> int:
        """Return current number of in-flight operations."""
        return self._in_flight_count
