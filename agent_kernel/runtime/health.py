"""Kubernetes-style health probes for agent-kernel.

Factory functions ``sqlite_dedupe_store_health_check`` and
``event_log_health_check`` provide ready-made ``HealthCheckFn`` callables that
can be registered with ``KernelHealthProbe.register_check()``::

    from agent_kernel.runtime.health import (
        KernelHealthProbe,
        sqlite_dedupe_store_health_check,
        event_log_health_check,
    )

    probe = KernelHealthProbe()
    probe.register_check("dedupe_store", sqlite_dedupe_store_health_check(my_store))
    probe.register_check("event_log", event_log_health_check(my_event_log))


Provides liveness and readiness probes compatible with K8s HTTP probe
conventions.  The ``KernelHealthProbe`` aggregates component health checks
and exposes a simple dict-based status payload.

Usage::

    from agent_kernel.runtime.health import KernelHealthProbe, HealthStatus

    probe = KernelHealthProbe()
    probe.register_check("sqlite", my_sqlite_check)

    status = probe.liveness()   # {"status": "ok", "checks": {...}}
    status = probe.readiness()  # same shape, stricter gate
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


class HealthStatus(StrEnum):
    """Aggregate health status for a probe response.

    Values match Kubernetes probe conventions:
    - ``OK``: Component is healthy.
    - ``DEGRADED``: Component is functional but impaired (readiness fails).
    - ``UNHEALTHY``: Component is not functional (both probes fail).
    """

    OK = "ok"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


HealthCheckFn = Callable[[], tuple[HealthStatus, str]]
"""Signature for a health check function.

Returns:
    A ``(HealthStatus, message)`` tuple.
"""


@dataclass(slots=True)
class KernelHealthProbe:
    """Aggregates component health checks for K8s-style probes.

    Liveness probe: passes when no check reports ``UNHEALTHY``.
    Readiness probe: passes when all checks report ``OK``.

    Args:
        component_name: Identifier included in all probe responses.
    """

    component_name: str = "agent-kernel"
    _checks: dict[str, HealthCheckFn] = field(default_factory=dict)

    def register_check(self, name: str, check_fn: HealthCheckFn) -> None:
        """Register a named health check function.

        Args:
            name: Unique name for this check (e.g. ``"sqlite"``,
                ``"temporal"``).
            check_fn: Callable returning ``(HealthStatus, message)``.

        Raises:
            ValueError: When ``name`` is already registered.
        """
        if name in self._checks:
            raise ValueError(f"Health check '{name}' is already registered.")
        self._checks[name] = check_fn

    def liveness(self) -> dict:
        """Run all checks; pass unless any reports UNHEALTHY.

        Returns:
            Dict with keys ``"component"``, ``"status"``, ``"checks"``,
            ``"timestamp_ms"``.
        """
        results = self._run_all()
        aggregate = (
            HealthStatus.UNHEALTHY
            if any(s == HealthStatus.UNHEALTHY for s, _ in results.values())
            else HealthStatus.OK
        )
        return self._format_response(aggregate, results)

    def readiness(self) -> dict:
        """Run all checks; pass only when all report OK.

        Returns:
            Dict with same shape as ``liveness()``.
        """
        results = self._run_all()
        statuses = {s for s, _ in results.values()}
        if statuses == {HealthStatus.OK} or not statuses:
            aggregate = HealthStatus.OK
        elif HealthStatus.UNHEALTHY in statuses:
            aggregate = HealthStatus.UNHEALTHY
        else:
            aggregate = HealthStatus.DEGRADED
        return self._format_response(aggregate, results)

    def _run_all(self) -> dict[str, tuple[HealthStatus, str]]:
        results: dict[str, tuple[HealthStatus, str]] = {}
        for name, check_fn in self._checks.items():
            try:
                results[name] = check_fn()
            except Exception as exc:
                logger.warning("Health check '%s' raised: %s", name, exc)
                results[name] = (HealthStatus.UNHEALTHY, str(exc))
        return results

    def _format_response(
        self,
        aggregate: HealthStatus,
        results: dict[str, tuple[HealthStatus, str]],
    ) -> dict:
        return {
            "component": self.component_name,
            "status": aggregate.value,
            "checks": {
                name: {"status": s.value, "message": msg} for name, (s, msg) in results.items()
            },
            "timestamp_ms": int(time.time() * 1000),
        }


# ---------------------------------------------------------------------------
# Factory functions for common health checks
# ---------------------------------------------------------------------------


def sqlite_dedupe_store_health_check(store: Any) -> HealthCheckFn:
    """Returns a ``HealthCheckFn`` that probes a SQLite-backed DedupeStore.

    The check executes a lightweight ``SELECT 1`` via the store's internal
    connection.  Register it with ``KernelHealthProbe.register_check()``.

    Args:
        store: A ``SQLiteDedupeStore`` instance (or any object with a
            ``_conn`` SQLite connection attribute).

    Returns:
        Health check callable returning ``(HealthStatus, message)``.
    """

    def _check() -> tuple[HealthStatus, str]:
        try:
            store._conn.execute("SELECT 1").fetchone()
            return HealthStatus.OK, "SQLiteDedupeStore reachable"
        except Exception as exc:  # pylint: disable=broad-exception-caught
            return HealthStatus.UNHEALTHY, f"SQLiteDedupeStore unreachable: {exc}"

    return _check


def event_log_health_check(event_log: Any) -> HealthCheckFn:
    """Returns a ``HealthCheckFn`` that probes a RuntimeEventLog.

    The check reads the ``events`` attribute (or calls ``list_events()`` if
    available) to verify the log is accessible.  Register it with
    ``KernelHealthProbe.register_check()``.

    Args:
        event_log: A ``RuntimeEventLog``-compatible instance.

    Returns:
        Health check callable returning ``(HealthStatus, message)``.
    """

    def _check() -> tuple[HealthStatus, str]:
        try:
            if hasattr(event_log, "list_events"):
                count = len(event_log.list_events())
            elif hasattr(event_log, "events"):
                count = len(event_log.events)
            else:
                return HealthStatus.OK, "EventLog reachable (no count available)"
            return HealthStatus.OK, f"EventLog reachable ({count} events)"
        except Exception as exc:  # pylint: disable=broad-exception-caught
            return HealthStatus.UNHEALTHY, f"EventLog unreachable: {exc}"

    return _check
