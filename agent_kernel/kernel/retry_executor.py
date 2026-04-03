"""RetryingExecutorService — lightweight transient-retry wrapper for ExecutorService.

Implements P3b of the agent-kernel quality plan: wraps any ExecutorService
with up to ``max_attempts`` retries for ``TransientExecutionError``, adding
jittered exponential backoff so bursts of retries don't collide.

Design notes:
  - Only ``TransientExecutionError`` triggers a retry; all other exceptions
    propagate immediately (permanent failures, auth errors, etc.).
  - The wrapper is transparent: it passes all arguments through to the inner
    executor unchanged.
  - The default ``max_attempts=2`` means one automatic retry on the first
    transient failure; increase for longer retry budgets.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

from agent_kernel.kernel.contracts import TransientExecutionError


class RetryingExecutorService:
    """Wraps any ExecutorService with lightweight transient-failure retry.

    Only ``TransientExecutionError`` triggers a retry.  All other exceptions
    propagate immediately on the first attempt.

    Args:
        inner: The underlying executor to delegate to.
        max_attempts: Maximum total attempts (including the first).  Must be
            at least 1.  Defaults to 2 (one automatic retry).
        base_delay_ms: Base sleep before the first retry in milliseconds.
            Subsequent retries double this value (exponential backoff).
            Defaults to 100 ms.
        jitter_ms: Maximum additional random jitter added to each delay in
            milliseconds.  Helps spread out concurrent retries.
            Defaults to 50 ms.
    """

    def __init__(
        self,
        inner: Any,
        max_attempts: int = 2,
        base_delay_ms: int = 100,
        jitter_ms: int = 50,
    ) -> None:
        if max_attempts < 1:
            raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")
        self._inner = inner
        self._max_attempts = max_attempts
        self._base_delay_ms = base_delay_ms
        self._jitter_ms = jitter_ms

    async def execute(self, action: Any, **kwargs: Any) -> Any:
        """Executes *action* via the inner executor, retrying on transient errors.

        Args:
            action: The action to execute.
            **kwargs: Additional keyword arguments forwarded to the inner
                executor's ``execute()`` method (e.g. ``grant_ref``).

        Returns:
            Whatever the inner executor returns on success.

        Raises:
            TransientExecutionError: When all attempts are exhausted.
            Any other exception raised by the inner executor immediately.
        """
        last_exc: TransientExecutionError | None = None
        for attempt in range(self._max_attempts):
            try:
                return await self._inner.execute(action, **kwargs)
            except TransientExecutionError as exc:
                last_exc = exc
                if attempt < self._max_attempts - 1:
                    delay_ms = self._base_delay_ms * (2**attempt) + random.randint(
                        0, self._jitter_ms
                    )
                    await asyncio.sleep(delay_ms / 1000.0)
        raise last_exc  # type: ignore[misc]
