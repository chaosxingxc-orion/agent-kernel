"""Run-level and kernel-level heartbeat detection for agent-kernel.

Why heartbeat matters for long-running agents
----------------------------------------------
A kernel run can silently get stuck: an external callback never arrives,
a tool activity hangs past the Temporal activity timeout, or the event log
falls behind.  Temporal's activity-level heartbeat (``activity.heartbeat()``)
covers the *activity* side.  This module covers the *run* side — whether a
run as a whole is still making forward progress.

Two components
--------------
1. **``RunHeartbeatMonitor``** — per-run liveness tracker.

   Implements ``ObservabilityHook`` so it receives every FSM state transition
   without additional wiring.  A background watchdog calls
   ``get_timed_out_runs()`` and injects a ``heartbeat_timeout`` signal through
   the canonical signal pathway; the existing Recovery authority handles the
   rest.  No new kernel authority is introduced.

2. **``KernelSelfHeartbeat``** — kernel component liveness.

   Verifies that event log and projection service are responsive.  Caches
   results so the sync ``HealthCheckFn`` interface (required by
   ``KernelHealthProbe``) is never blocked on an async I/O call.

Architecture invariants preserved
-----------------------------------
- Heartbeat state is *ephemeral* (in-memory).  On worker restart, runs
  re-establish heartbeat or get timed out fresh.  Persisting heartbeat
  state would create a distributed coordination problem for no safety gain.
- On timeout a ``heartbeat_timeout`` signal is injected via the existing
  signal path → RunActor → TurnEngine → Recovery.  The six-authority
  invariant is never broken.
- ``RunHeartbeatMonitor`` is NOT a seventh authority.  It is an operational
  watchdog that observes via the hook side-channel and acts via the substrate
  gateway signal interface.

Typical wiring::

    from agent_kernel.runtime.heartbeat import (
        HeartbeatPolicy,
        RunHeartbeatMonitor,
        KernelSelfHeartbeat,
    )
    from agent_kernel.runtime.observability_hooks import (
        CompositeObservabilityHook,
        LoggingObservabilityHook,
    )

    policy = HeartbeatPolicy()
    monitor = RunHeartbeatMonitor(policy=policy)

    bundle = RunActorDependencyBundle(
        ...
        observability_hook=CompositeObservabilityHook(
            [LoggingObservabilityHook(), monitor]
        ),
    )

    # In a background worker task:
    await monitor.watchdog_once(gateway=temporal_gateway)

    # Wire kernel self-check into health probe:
    self_hb = KernelSelfHeartbeat()
    health_probe.register_check("kernel_event_log", self_hb.event_log_check())
    health_probe.register_check("kernel_projection", self_hb.projection_check())
    await self_hb.refresh(event_log=event_log, projection=projection)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from agent_kernel.runtime.health import HealthCheckFn, HealthStatus

if TYPE_CHECKING:
    from agent_kernel.kernel.contracts import (
        DecisionProjectionService,
        KernelRuntimeEventLog,
        TemporalWorkflowGateway,
    )

_logger = logging.getLogger(__name__)

# Lifecycle states that are "active but potentially stuck".
# Idle-safe states (ready, created) and terminal states (completed, aborted)
# are intentionally excluded.
_MONITORED_STATES: frozenset[str] = frozenset(
    {
        "dispatching",
        # executor called; Temporal activity governs activity side
        "waiting_result",  # awaiting external tool/MCP result
        "waiting_external",  # external callback; has its own ceiling
        "waiting_human_input",
        # human approval/clarification; very long ceiling
        "recovering",  # recovery gate active; should complete quickly
    }
)

# Default timeout per monitored state (seconds).
# Override via HeartbeatPolicy.state_timeout_s.
_DEFAULT_STATE_TIMEOUTS: dict[str, int] = {
    "dispatching": 300,  # 5 min: executor should not block workflow thread
    "waiting_result": 600,  # 10 min: activity timeout should fire first
    "waiting_external": 3600,  # 1 hr: external callback ceiling
    "waiting_human_input": 86400,
    # 24 hr: human interaction can take a full day
    "recovering": 180,  # 3 min: recovery planner call should be fast
}


@dataclass(frozen=True, slots=True)
class HeartbeatPolicy:
    """Configures heartbeat timeout thresholds.

    Attributes:
        state_timeout_s: Override map from lifecycle state to timeout in
            seconds.  States not in this map fall back to built-in defaults
            defined in ``_DEFAULT_STATE_TIMEOUTS``.
        min_heartbeat_interval_s: Minimum wall-clock gap between consecutive
            ``record_heartbeat`` calls for the same run before the monitor
            considers it "active".  Prevents spurious alerts during rapid
            FSM cycling.
        stale_check_age_s: How old a cached ``KernelSelfHeartbeat`` result
            can be before ``is_stale()`` returns ``True``.
    """

    state_timeout_s: dict[str, int] = field(default_factory=dict)
    min_heartbeat_interval_s: int = 5
    stale_check_age_s: int = 60

    def timeout_for(self, lifecycle_state: str) -> int | None:
        """Return timeout in seconds for a given lifecycle state.

        Args:
            lifecycle_state: RunLifecycleState string.

        Returns:
            Timeout in seconds, or ``None`` if the state is not monitored.
        """
        if lifecycle_state not in _MONITORED_STATES:
            return None
        return self.state_timeout_s.get(
            lifecycle_state,
            _DEFAULT_STATE_TIMEOUTS.get(lifecycle_state, 600),
        )


@dataclass(slots=True)
class _RunHeartbeatEntry:
    """Internal: per-run liveness tracking entry."""

    run_id: str
    last_seen_ms: int  # epoch milliseconds
    lifecycle_state: str  # last observed state from ObservabilityHook
    heartbeat_count: int = 0


class RunHeartbeatMonitor:
    """Per-run liveness tracker; implements ObservabilityHook.

    Lifecycle state transitions (received via the observability hook) update
    the "last seen" timestamp for each run.  The ``watchdog_once()`` method
    scans all tracked runs and injects a ``heartbeat_timeout`` signal for any
    run that has been silent beyond the policy threshold.

    Thread-safety: all public methods are protected by a ``threading.Lock``
    so the monitor is safe to use from both the Temporal workflow coroutine
    and an external asyncio watchdog task.

    Args:
        policy: Heartbeat policy with per-state timeout thresholds.
    """

    def __init__(self, policy: HeartbeatPolicy | None = None) -> None:
        self._policy = policy or HeartbeatPolicy()
        self._entries: dict[str, _RunHeartbeatEntry] = {}
        self._lock = threading.Lock()
        self._timed_out: set[str] = set()  # runs already signalled; avoid repeat

    # ------------------------------------------------------------------
    # ObservabilityHook implementation
    # ------------------------------------------------------------------

    def on_turn_state_transition(
        self,
        *,
        run_id: str,
        action_id: str,
        from_state: str,
        to_state: str,
        turn_offset: int,
        timestamp_ms: int,
    ) -> None:
        """Record activity for a run on every TurnEngine FSM transition."""
        self._touch(run_id, lifecycle_state=None, timestamp_ms=timestamp_ms)

    def on_run_lifecycle_transition(
        self,
        *,
        run_id: str,
        from_state: str,
        to_state: str,
        timestamp_ms: int,
    ) -> None:
        """Record activity for a run on every RunActor lifecycle transition."""
        self._touch(run_id, lifecycle_state=to_state, timestamp_ms=timestamp_ms)
        if to_state in ("completed", "aborted"):
            self.clear(run_id)

    def on_llm_call(
        self,
        *,
        run_id: str,
        model_ref: str,
        latency_ms: int,
        token_usage: Any,
    ) -> None:
        """Record activity heartbeat on LLM call — no other action needed."""
        self._touch(run_id, lifecycle_state=None, timestamp_ms=_now_ms())

    def on_action_dispatch(
        self,
        *,
        run_id: str,
        action_id: str,
        action_type: str,
        outcome_kind: str,
        latency_ms: int,
    ) -> None:
        """Record activity heartbeat on action dispatch — no other action needed."""
        self._touch(run_id, lifecycle_state=None, timestamp_ms=_now_ms())

    def on_recovery_triggered(
        self,
        *,
        run_id: str,
        reason_code: str,
        mode: str,
    ) -> None:
        """No-op — recovery events do not reset the heartbeat timer."""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record_heartbeat(self, run_id: str) -> None:
        """Explicitly record a heartbeat for a run.

        Call this from within a Temporal activity's heartbeat callback or
        any other external liveness signal source.

        Args:
            run_id: Run to record heartbeat for.
        """
        self._touch(run_id, lifecycle_state=None, timestamp_ms=_now_ms())

    def is_alive(self, run_id: str) -> bool:
        """Return ``True`` if the run has been seen within its policy timeout.

        Args:
            run_id: Run to check.

        Returns:
            ``True`` when alive or when the run is not in a monitored state.
        """
        with self._lock:
            entry = self._entries.get(run_id)
            if entry is None:
                return True  # unknown run is not our responsibility
            timeout_s = self._policy.timeout_for(entry.lifecycle_state)
            if timeout_s is None:
                return True  # state is not monitored
            age_s = (_now_ms() - entry.last_seen_ms) / 1000.0
            return age_s < timeout_s

    def last_seen_age_s(self, run_id: str) -> float | None:
        """Return seconds since last activity for a run, or ``None`` if unknown.

        Args:
            run_id: Run to check.

        Returns:
            Age in seconds, or ``None`` when run is not tracked.
        """
        with self._lock:
            entry = self._entries.get(run_id)
            if entry is None:
                return None
            return (_now_ms() - entry.last_seen_ms) / 1000.0

    def get_timed_out_runs(self) -> list[str]:
        """Return run IDs that have exceeded their heartbeat policy timeout.

        Runs that have already been signalled are not returned again until
        they record fresh activity (to prevent signal storm on a stuck run).

        Returns:
            List of run IDs to signal with ``heartbeat_timeout``.
        """
        now = _now_ms()
        timed_out: list[str] = []
        with self._lock:
            for run_id, entry in self._entries.items():
                if run_id in self._timed_out:
                    continue
                timeout_s = self._policy.timeout_for(entry.lifecycle_state)
                if timeout_s is None:
                    continue
                age_s = (now - entry.last_seen_ms) / 1000.0
                if age_s >= timeout_s:
                    timed_out.append(run_id)
                    self._timed_out.add(run_id)
        return timed_out

    def clear(self, run_id: str) -> None:
        """Remove tracking entry for a terminated run.

        Args:
            run_id: Run to clear.
        """
        with self._lock:
            self._entries.pop(run_id, None)
            self._timed_out.discard(run_id)

    async def watchdog_once(self, gateway: TemporalWorkflowGateway) -> None:
        """Check all tracked runs once and signal any that have timed out.

        This method is intended to be called from a background asyncio task
        at a regular interval (e.g. every 30 seconds).

        Args:
            gateway: Temporal workflow gateway for signal delivery.
        """
        from agent_kernel.kernel.contracts import SignalRunRequest

        timed_out = self.get_timed_out_runs()
        for run_id in timed_out:
            age_s = self.last_seen_age_s(run_id) or 0.0
            _logger.warning(
                "Run heartbeat timeout: run_id=%s silent_s=%.1f — injecting"
                "heartbeat_timeout signal",
                run_id,
                age_s,
            )
            try:
                await gateway.signal_workflow(
                    run_id,
                    SignalRunRequest(
                        run_id=run_id,
                        signal_type="heartbeat_timeout",
                        signal_payload={"silent_s": round(age_s, 1)},
                        caused_by=f"heartbeat_monitor:{run_id}",
                    ),
                )
            except Exception as exc:
                # Signal delivery is best-effort; log but never crash watchdog
                _logger.warning(
                    "Failed to deliver heartbeat_timeout signal to run_id=%s:%s",
                    run_id,
                    exc,
                )

    def start_watchdog(
        self,
        gateway: TemporalWorkflowGateway,
        interval_s: float = 30.0,
    ) -> asyncio.Task:  # type: ignore[type-arg]
        """Start a background asyncio Task that calls ``watchdog_once`` periodically.

        The task runs until cancelled (e.g. on worker shutdown).  Cancellation
        is handled gracefully — the task exits without logging an error.

        Typical wiring::

            watchdog_task = monitor.start_watchdog(gateway, interval_s=30)
            # On shutdown:
            watchdog_task.cancel()

        Args:
            gateway: Temporal workflow gateway for signal delivery.
            interval_s: Seconds between consecutive watchdog sweeps.

        Returns:
            The running ``asyncio.Task``.  The caller should retain a reference
            and cancel it on worker shutdown to prevent resource leaks.
        """

        async def _loop() -> None:
            try:
                while True:
                    await asyncio.sleep(interval_s)
                    await self.watchdog_once(gateway)
            except asyncio.CancelledError:
                pass  # clean shutdown

        return asyncio.get_running_loop().create_task(_loop(), name="heartbeat_watchdog")

    def make_health_check_fn(self) -> HealthCheckFn:
        """Return a sync ``HealthCheckFn`` for registration in
        ``KernelHealthProbe``.

        Reports ``DEGRADED`` when any tracked run is within 20% of its timeout
        threshold; ``UNHEALTHY`` when any run has already timed out and the
        signal could not be delivered.

        Returns:
            Callable ``() -> (HealthStatus,
                message)`` for ``KernelHealthProbe``.
        """
        monitor = self

        def _check() -> tuple[HealthStatus, str]:
            now = _now_ms()
            near_timeout: list[str] = []
            already_timed_out: list[str] = []
            with monitor._lock:
                for run_id, entry in monitor._entries.items():
                    timeout_s = monitor._policy.timeout_for(entry.lifecycle_state)
                    if timeout_s is None:
                        continue
                    age_s = (now - entry.last_seen_ms) / 1000.0
                    if age_s >= timeout_s and run_id in monitor._timed_out:
                        already_timed_out.append(run_id)
                    elif age_s >= timeout_s * 0.8:
                        near_timeout.append(run_id)
            if already_timed_out:
                return (
                    HealthStatus.UNHEALTHY,
                    f"Runs timed out (signal injected): {already_timed_out}",
                )
            if near_timeout:
                return (
                    HealthStatus.DEGRADED,
                    f"Runs approaching heartbeat timeout: {near_timeout}",
                )
            return (
                HealthStatus.OK,
                "All monitored runs within heartbeat policy",
            )

        return _check

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _touch(
        self,
        run_id: str,
        lifecycle_state: str | None,
        timestamp_ms: int,
    ) -> None:
        """Update or create heartbeat entry for a run."""
        with self._lock:
            entry = self._entries.get(run_id)
            if entry is None:
                self._entries[run_id] = _RunHeartbeatEntry(
                    run_id=run_id,
                    last_seen_ms=timestamp_ms,
                    lifecycle_state=lifecycle_state or "ready",
                    heartbeat_count=1,
                )
            else:
                entry.last_seen_ms = timestamp_ms
                entry.heartbeat_count += 1
                if lifecycle_state is not None:
                    entry.lifecycle_state = lifecycle_state
            # Fresh activity resets timeout signal so watchdog can re-signal
            # if the run gets stuck again after recovering.
            self._timed_out.discard(run_id)


@dataclass(slots=True)
class _KernelSelfHeartbeatState:
    """Mutable cached state for the last async self-check."""

    event_log_status: HealthStatus = HealthStatus.UNHEALTHY
    event_log_message: str = "not yet checked"
    projection_status: HealthStatus = HealthStatus.UNHEALTHY
    projection_message: str = "not yet checked"
    last_refresh_at_ms: int = 0


class KernelSelfHeartbeat:
    """Verifies that kernel components are responsive; plugs into
    KernelHealthProbe.

    ``refresh()`` performs async I/O checks and caches the results.
    ``event_log_check()`` and ``projection_check()`` return sync
    ``HealthCheckFn`` callables that read from the cached state — required
    because ``KernelHealthProbe`` (and Kubernetes probes) are synchronous.

    Typical usage::

        self_hb = KernelSelfHeartbeat()
        health_probe.register_check("kernel_event_log",
            self_hb.event_log_check())
        health_probe.register_check("kernel_projection",
            self_hb.projection_check())

        # Call periodically from a background task:
        await self_hb.refresh(event_log=event_log, projection=projection)
    """

    _PROBE_RUN_ID: str = "__kernel_self_probe__"

    def __init__(self, stale_age_s: int = 60) -> None:
        """Initialise with configurable staleness threshold.

        Args:
            stale_age_s: Cached result age in seconds before it is considered
                stale.  A stale check is reported as ``DEGRADED``.
        """
        self._stale_age_s = stale_age_s
        self._state = _KernelSelfHeartbeatState()
        self._lock = threading.Lock()

    async def refresh(
        self,
        event_log: KernelRuntimeEventLog,
        projection: DecisionProjectionService,
    ) -> None:
        """Perform async component checks and cache results.

        A read-only query is used for both checks so no probe data is written
        into the kernel event log.  The event log is queried with
        ``load(probe_run_id, after_offset=0)`` to verify the read path;
        the projection is queried with ``get(probe_run_id)`` to verify the
        projection service.

        Args:
            event_log: Kernel event log to probe.
            projection: Decision projection service to probe.
        """
        ts = _now_ms()
        el_status, el_msg = await self._probe_event_log(event_log)
        pr_status, pr_msg = await self._probe_projection(projection)
        with self._lock:
            self._state.event_log_status = el_status
            self._state.event_log_message = el_msg
            self._state.projection_status = pr_status
            self._state.projection_message = pr_msg
            self._state.last_refresh_at_ms = ts

    def event_log_check(self) -> HealthCheckFn:
        """Return sync HealthCheckFn for event log responsiveness.

        Returns:
            Callable for ``KernelHealthProbe.register_check()``.
        """
        state = self._state
        lock = self._lock
        stale_age_s = self._stale_age_s

        def _check() -> tuple[HealthStatus, str]:
            with lock:
                if state.last_refresh_at_ms == 0:
                    return (
                        HealthStatus.UNHEALTHY,
                        "kernel self-check has not run yet",
                    )
                age_s = (_now_ms() - state.last_refresh_at_ms) / 1000.0
                if age_s > stale_age_s:
                    return (
                        HealthStatus.DEGRADED,
                        f"event log check is stale ({age_s:.0f}s old); call refresh()",
                    )
                return (state.event_log_status, state.event_log_message)

        return _check

    def projection_check(self) -> HealthCheckFn:
        """Return sync HealthCheckFn for projection service responsiveness.

        Returns:
            Callable for ``KernelHealthProbe.register_check()``.
        """
        state = self._state
        lock = self._lock
        stale_age_s = self._stale_age_s

        def _check() -> tuple[HealthStatus, str]:
            with lock:
                if state.last_refresh_at_ms == 0:
                    return (
                        HealthStatus.UNHEALTHY,
                        "kernel self-check has not run yet",
                    )
                age_s = (_now_ms() - state.last_refresh_at_ms) / 1000.0
                if age_s > stale_age_s:
                    return (
                        HealthStatus.DEGRADED,
                        f"projection check is stale ({age_s:.0f}s old); call refresh()",
                    )
                return (state.projection_status, state.projection_message)

        return _check

    def is_stale(self) -> bool:
        """Return ``True`` if the last refresh is older than ``stale_age_s``.

        Returns:
            ``True`` when a fresh ``refresh()`` call is needed.
        """
        with self._lock:
            if self._state.last_refresh_at_ms == 0:
                return True
            return (_now_ms() - self._state.last_refresh_at_ms) / 1000.0 > self._stale_age_s

    # ------------------------------------------------------------------
    # Internal async probes
    # ------------------------------------------------------------------

    async def _probe_event_log(
        self,
        event_log: KernelRuntimeEventLog,
    ) -> tuple[HealthStatus, str]:
        try:
            await event_log.load(self._PROBE_RUN_ID, after_offset=0)
            return (HealthStatus.OK, "event log read path responsive")
        except Exception as exc:
            return (HealthStatus.UNHEALTHY, f"event log probe failed: {exc}")

    async def _probe_projection(
        self,
        projection: DecisionProjectionService,
    ) -> tuple[HealthStatus, str]:
        try:
            await projection.get(self._PROBE_RUN_ID)
            return (HealthStatus.OK, "projection service responsive")
        except Exception as exc:
            return (HealthStatus.UNHEALTHY, f"projection probe failed: {exc}")


class HeartbeatWatchdog:
    """Runs ``RunHeartbeatMonitor.watchdog_once()`` on a configurable interval.

    Encapsulates the asyncio scheduling so callers don't need to manage the
    background task lifecycle.  Designed to integrate with the
    ``TemporalKernelWorker`` startup/shutdown cycle::

        watchdog = HeartbeatWatchdog(monitor, gateway, interval_s=30)
        await watchdog.start()
        ...
        await watchdog.stop()  # called on SIGTERM / graceful shutdown

    Args:
        monitor: The ``RunHeartbeatMonitor`` to scan on each tick.
        gateway: Temporal workflow gateway for signal delivery.
        interval_s: Scan interval in seconds (default: 30).
    """

    def __init__(
        self,
        monitor: RunHeartbeatMonitor,
        gateway: TemporalWorkflowGateway,
        interval_s: int = 30,
    ) -> None:
        self._monitor = monitor
        self._gateway = gateway
        self._interval_s = interval_s
        self._task: Any = None  # asyncio.Task, typed as Any to avoid import

    async def start(self) -> None:
        """Start the background watchdog asyncio task."""
        if self._task is not None and not self._task.done():
            _logger.warning("HeartbeatWatchdog already running; ignoring start().")
            return
        self._task = asyncio.ensure_future(self._loop())
        _logger.info("HeartbeatWatchdog started with interval_s=%d", self._interval_s)

    async def stop(self) -> None:
        """Cancel the background watchdog task and wait for clean shutdown."""
        if self._task is None or self._task.done():
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        _logger.info("HeartbeatWatchdog stopped.")

    async def _loop(self) -> None:
        """Internal: periodic watchdog scan loop."""
        while True:
            await asyncio.sleep(self._interval_s)
            try:
                await self._monitor.watchdog_once(self._gateway)
            except Exception as exc:
                _logger.warning("HeartbeatWatchdog scan error: %s", exc)


def _now_ms() -> int:
    """Return current UTC epoch time in milliseconds."""
    return int(time.time() * 1000)
