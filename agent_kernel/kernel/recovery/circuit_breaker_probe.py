"""Background half-open probe scheduler for recovery circuit breaker."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from agent_kernel.kernel.contracts import CircuitBreakerPolicy, CircuitBreakerStore


class CircuitBreakerProbeScheduler:
    """Periodically probe OPEN circuits and close them on successful health checks."""

    def __init__(
        self,
        circuit_breaker_store: CircuitBreakerStore,
        policy: CircuitBreakerPolicy,
        probe_fns: dict[str, Callable[[], Awaitable[bool]]],
        interval_s: float = 60.0,
    ) -> None:
        """Initialize probe scheduler.

        Args:
            circuit_breaker_store: Breaker state store.
            policy: Circuit-breaker policy with threshold and half-open interval.
            probe_fns: Mapping ``effect_class -> async health-check function``.
            interval_s: Probe sweep interval in seconds.

        """
        self._circuit_breaker_store = circuit_breaker_store
        self._policy = policy
        self._probe_fns = probe_fns
        self._interval_s = interval_s
        self._task: asyncio.Task[Any] | None = None

    def start(self) -> asyncio.Task[Any]:
        """Start background probe loop."""
        if self._task is not None and not self._task.done():
            return self._task

        async def _loop() -> None:
            try:
                while True:
                    await asyncio.sleep(self._interval_s)
                    await self.probe_once()
            except asyncio.CancelledError:
                return

        self._task = asyncio.get_running_loop().create_task(_loop(), name="circuit_breaker_probe")
        return self._task

    async def probe_once(self) -> list[str]:
        """Run one probe sweep and return closed effect classes."""
        closed: list[str] = []
        now = time.time()
        for effect_class in self._iter_effect_classes():
            if effect_class not in self._probe_fns:
                continue
            count, last_failure_ts = self._circuit_breaker_store.get_state(effect_class)
            if count < self._policy.threshold:
                continue
            elapsed_ms = (now - last_failure_ts) * 1000.0
            if elapsed_ms < self._policy.half_open_after_ms:
                continue
            probe_ok = await self._probe_fns[effect_class]()
            if probe_ok:
                self._circuit_breaker_store.reset(effect_class)
                closed.append(effect_class)
        return closed

    def _iter_effect_classes(self) -> list[str]:
        """Resolve known effect classes from probe functions and optional store API."""
        classes = set(self._probe_fns.keys())
        if hasattr(self._circuit_breaker_store, "list_effect_classes"):
            try:
                listed = self._circuit_breaker_store.list_effect_classes()
                classes.update(str(value) for value in listed)
            except Exception:
                pass
        return sorted(classes)
