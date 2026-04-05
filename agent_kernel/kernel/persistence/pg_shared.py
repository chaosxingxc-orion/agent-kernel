"""Shared asyncpg bridge utilities for PostgreSQL-backed persistence.

The kernel has mixed async and sync persistence interfaces. This module hosts
an internal bridge that runs an asyncio loop in a background thread so both:
- async callers can await database operations, and
- sync callers can blockingly invoke database operations
against the same asyncpg pool.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import Future
from typing import Any


class AsyncPGBridge:
    """Run asyncpg operations on a dedicated background event loop."""

    def __init__(
        self,
        *,
        dsn: str,
        pool_min: int = 2,
        pool_max: int = 10,
    ) -> None:
        """Initialize bridge and create asyncpg pool.

        Args:
            dsn: PostgreSQL DSN.
            pool_min: Pool minimum size.
            pool_max: Pool maximum size.
        """
        self._dsn = dsn
        self._pool_min = pool_min
        self._pool_max = pool_max
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="agent-kernel-asyncpg-loop",
            daemon=True,
        )
        self._thread.start()
        self._pool: Any = self.run_sync(self._create_pool())
        self._closed = False

    @property
    def pool(self) -> Any:
        """Return underlying asyncpg pool."""
        return self._pool

    async def run_async(self, coro: Any) -> Any:
        """Run coroutine on bridge loop and await its result."""
        future: Future[Any] = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return await asyncio.wrap_future(future)

    def run_sync(self, coro: Any) -> Any:
        """Run coroutine on bridge loop and block until completion."""
        future: Future[Any] = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()

    def close(self) -> None:
        """Close pool and stop background event loop."""
        if self._closed:
            return
        self.run_sync(self._pool.close())
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5.0)
        self._closed = True

    async def _create_pool(self) -> Any:
        """Create asyncpg pool lazily with helpful dependency error."""
        try:
            import asyncpg
        except ImportError as exc:  # pragma: no cover - optional dependency path
            raise RuntimeError(
                "PostgreSQL backend requires asyncpg. Install with: pip install asyncpg"
            ) from exc

        return await asyncpg.create_pool(
            dsn=self._dsn,
            min_size=self._pool_min,
            max_size=self._pool_max,
        )

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()
