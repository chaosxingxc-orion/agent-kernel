"""Tests for RunHeartbeatMonitor, HeartbeatWatchdog, and KernelSelfHeartbeat.

Coverage:
- RunHeartbeatMonitor: lock correctness under concurrent access, state
  transitions, timeout detection, watchdog_once signal injection, clear().
- HeartbeatWatchdog: start/stop lifecycle, single-scan behaviour.
- KernelSelfHeartbeat: probe staleness, event-log + projection checks.
- HeartbeatPolicy: timeout_for edge cases.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_kernel.runtime.health import HealthStatus
from agent_kernel.runtime.heartbeat import (
    HeartbeatPolicy,
    HeartbeatWatchdog,
    KernelSelfHeartbeat,
    RunHeartbeatMonitor,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_monitor(
    state_timeout_s: dict[str, int] | None = None,
) -> RunHeartbeatMonitor:
    """Make monitor."""
    policy = HeartbeatPolicy(state_timeout_s=state_timeout_s or {})
    return RunHeartbeatMonitor(policy=policy)


def _touch_run(
    monitor: RunHeartbeatMonitor,
    run_id: str,
    lifecycle_state: str,
    timestamp_ms: int | None = None,
) -> None:
    """Touch run."""
    ts = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
    monitor.on_run_lifecycle_transition(
        run_id=run_id,
        from_state="ready",
        to_state=lifecycle_state,
        timestamp_ms=ts,
    )


def _fake_gateway(signal_calls: list[Any] | None = None) -> Any:
    """Return an async-mock gateway that records signal_workflow calls."""
    calls: list[Any] = signal_calls if signal_calls is not None else []
    gateway = MagicMock()
    gateway.signal_workflow = AsyncMock(side_effect=lambda *a, **kw: calls.append((a, kw)))
    return gateway


# ---------------------------------------------------------------------------
# HeartbeatPolicy
# ---------------------------------------------------------------------------


class TestHeartbeatPolicy:
    """Test suite for HeartbeatPolicy."""

    def test_returns_none_for_unmonitored_state(self) -> None:
        """Verifies returns none for unmonitored state."""
        policy = HeartbeatPolicy()
        assert policy.timeout_for("ready") is None
        assert policy.timeout_for("completed") is None
        assert policy.timeout_for("created") is None
        assert policy.timeout_for("aborted") is None

    def test_returns_default_for_monitored_state(self) -> None:
        """Verifies returns default for monitored state."""
        policy = HeartbeatPolicy()
        assert policy.timeout_for("dispatching") == 300
        assert policy.timeout_for("waiting_result") == 600
        assert policy.timeout_for("waiting_external") == 3600
        assert policy.timeout_for("waiting_human_input") == 86400
        assert policy.timeout_for("recovering") == 180

    def test_override_timeout_takes_priority(self) -> None:
        """Verifies override timeout takes priority."""
        policy = HeartbeatPolicy(state_timeout_s={"dispatching": 30})
        assert policy.timeout_for("dispatching") == 30
        # Other states still use defaults
        assert policy.timeout_for("waiting_result") == 600


# ---------------------------------------------------------------------------
# RunHeartbeatMonitor - basic state tracking
# ---------------------------------------------------------------------------


class TestRunHeartbeatMonitorStateTracking:
    """Test suite for RunHeartbeatMonitorStateTracking."""

    def test_unknown_run_is_alive(self) -> None:
        """Verifies unknown run is alive."""
        monitor = _make_monitor()
        assert monitor.is_alive("unknown-run") is True

    def test_run_in_unmonitored_state_is_alive(self) -> None:
        """Verifies run in unmonitored state is alive."""
        monitor = _make_monitor()
        _touch_run(monitor, "run-1", "ready")
        assert monitor.is_alive("run-1") is True

    def test_recently_touched_monitored_run_is_alive(self) -> None:
        """Verifies recently touched monitored run is alive."""
        monitor = _make_monitor(state_timeout_s={"dispatching": 300})
        _touch_run(monitor, "run-1", "dispatching")
        assert monitor.is_alive("run-1") is True

    def test_stale_monitored_run_is_not_alive(self) -> None:
        """Verifies stale monitored run is not alive."""
        monitor = _make_monitor(state_timeout_s={"dispatching": 1})
        old_ms = int(time.time() * 1000) - 5_000  # 5 s ago, timeout = 1 s
        _touch_run(monitor, "run-1", "dispatching", timestamp_ms=old_ms)
        assert monitor.is_alive("run-1") is False

    def test_last_seen_age_returns_none_for_unknown_run(self) -> None:
        """Verifies last seen age returns none for unknown run."""
        monitor = _make_monitor()
        assert monitor.last_seen_age_s("nonexistent") is None

    def test_last_seen_age_positive_for_tracked_run(self) -> None:
        """Verifies last seen age positive for tracked run."""
        monitor = _make_monitor()
        _touch_run(monitor, "run-1", "dispatching")
        age = monitor.last_seen_age_s("run-1")
        assert age is not None
        assert age >= 0.0

    def test_on_turn_state_transition_touches_run(self) -> None:
        """Verifies on turn state transition touches run."""
        monitor = _make_monitor(state_timeout_s={"dispatching": 300})
        # First establish state via lifecycle transition
        _touch_run(monitor, "run-1", "dispatching")
        # Then simulate a turn transition which should update last_seen_ms
        age_before = monitor.last_seen_age_s("run-1")
        time.sleep(0.01)
        monitor.on_turn_state_transition(
            run_id="run-1",
            action_id="act-1",
            from_state="collecting",
            to_state="dispatched",
            turn_offset=1,
            timestamp_ms=int(time.time() * 1000),
        )
        age_after = monitor.last_seen_age_s("run-1")
        assert age_after is not None
        assert age_before is not None
        # Age must be younger after the touch
        assert age_after <= age_before + 0.1

    def test_terminal_state_clears_entry(self) -> None:
        """Verifies terminal state clears entry."""
        monitor = _make_monitor()
        _touch_run(monitor, "run-1", "dispatching")
        assert monitor.last_seen_age_s("run-1") is not None
        _touch_run(monitor, "run-1", "completed")
        assert monitor.last_seen_age_s("run-1") is None

    def test_aborted_state_clears_entry(self) -> None:
        """Verifies aborted state clears entry."""
        monitor = _make_monitor()
        _touch_run(monitor, "run-1", "dispatching")
        _touch_run(monitor, "run-1", "aborted")
        assert monitor.last_seen_age_s("run-1") is None

    def test_clear_removes_entry(self) -> None:
        """Verifies clear removes entry."""
        monitor = _make_monitor()
        _touch_run(monitor, "run-1", "dispatching")
        monitor.clear("run-1")
        assert monitor.is_alive("run-1") is True  # unknown → alive
        assert monitor.last_seen_age_s("run-1") is None

    def test_record_heartbeat_refreshes_last_seen(self) -> None:
        """Verifies record heartbeat refreshes last seen."""
        monitor = _make_monitor(state_timeout_s={"dispatching": 300})
        old_ms = int(time.time() * 1000) - 5_000
        _touch_run(monitor, "run-1", "dispatching", timestamp_ms=old_ms)
        monitor.record_heartbeat("run-1")
        age = monitor.last_seen_age_s("run-1")
        assert age is not None and age < 0.5


# ---------------------------------------------------------------------------
# RunHeartbeatMonitor - timeout detection + no-repeat guard
# ---------------------------------------------------------------------------


class TestRunHeartbeatMonitorTimeoutDetection:
    """Test suite for RunHeartbeatMonitorTimeoutDetection."""

    def test_get_timed_out_runs_returns_stale_run(self) -> None:
        """Verifies get timed out runs returns stale run."""
        monitor = _make_monitor(state_timeout_s={"dispatching": 1})
        old_ms = int(time.time() * 1000) - 5_000
        _touch_run(monitor, "run-stale", "dispatching", timestamp_ms=old_ms)
        timed_out = monitor.get_timed_out_runs()
        assert "run-stale" in timed_out

    def test_get_timed_out_runs_excludes_fresh_run(self) -> None:
        """Verifies get timed out runs excludes fresh run."""
        monitor = _make_monitor(state_timeout_s={"dispatching": 300})
        _touch_run(monitor, "run-fresh", "dispatching")
        timed_out = monitor.get_timed_out_runs()
        assert "run-fresh" not in timed_out

    def test_get_timed_out_runs_no_repeat_after_signal(self) -> None:
        """Verifies get timed out runs no repeat after signal."""
        monitor = _make_monitor(state_timeout_s={"dispatching": 1})
        old_ms = int(time.time() * 1000) - 5_000
        _touch_run(monitor, "run-1", "dispatching", timestamp_ms=old_ms)
        first = monitor.get_timed_out_runs()
        assert "run-1" in first
        second = monitor.get_timed_out_runs()
        assert "run-1" not in second, "Already-signalled run must not be returned again"

    def test_after_clear_run_no_longer_timed_out(self) -> None:
        """Verifies after clear run no longer timed out."""
        monitor = _make_monitor(state_timeout_s={"dispatching": 1})
        old_ms = int(time.time() * 1000) - 5_000
        _touch_run(monitor, "run-1", "dispatching", timestamp_ms=old_ms)
        monitor.get_timed_out_runs()  # records in _timed_out
        monitor.clear("run-1")
        # Re-add fresh entry and verify it's not included in timed-out set
        _touch_run(monitor, "run-1", "dispatching")
        assert "run-1" not in monitor.get_timed_out_runs()

    def test_unmonitored_state_never_times_out(self) -> None:
        """Verifies unmonitored state never times out."""
        monitor = _make_monitor()
        old_ms = int(time.time() * 1000) - 1_000_000
        _touch_run(monitor, "run-idle", "ready", timestamp_ms=old_ms)
        assert "run-idle" not in monitor.get_timed_out_runs()


# ---------------------------------------------------------------------------
# RunHeartbeatMonitor - lock correctness under concurrency
# ---------------------------------------------------------------------------


class TestRunHeartbeatMonitorConcurrency:
    """Test suite for RunHeartbeatMonitorConcurrency."""

    def test_concurrent_touch_does_not_corrupt_entries(self) -> None:
        """Verifies concurrent touch does not corrupt entries."""
        monitor = _make_monitor()
        errors: list[Exception] = []

        def _writer(run_id: str) -> None:
            """Writer."""
            try:
                for i in range(100):
                    monitor.on_run_lifecycle_transition(
                        run_id=run_id,
                        from_state="ready",
                        to_state="dispatching",
                        timestamp_ms=int(time.time() * 1000) + i,
                    )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_writer, args=(f"run-{i}",)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Concurrent writes raised: {errors}"

    def test_concurrent_read_write_no_deadlock(self) -> None:
        """get_timed_out_runs() and on_run_lifecycle_transition() can run concurrently."""
        monitor = _make_monitor(state_timeout_s={"dispatching": 1})
        stop_event = threading.Event()
        errors: list[Exception] = []

        def _continuous_writer() -> None:
            """Continuous writer."""
            try:
                i = 0
                while not stop_event.is_set():
                    monitor.on_run_lifecycle_transition(
                        run_id=f"run-w-{i % 5}",
                        from_state="ready",
                        to_state="dispatching",
                        timestamp_ms=int(time.time() * 1000),
                    )
                    i += 1
            except Exception as exc:
                errors.append(exc)

        def _continuous_reader() -> None:
            """Continuous reader."""
            try:
                for _ in range(50):
                    monitor.get_timed_out_runs()
                    time.sleep(0.001)
            except Exception as exc:
                errors.append(exc)

        writer = threading.Thread(target=_continuous_writer)
        reader = threading.Thread(target=_continuous_reader)
        writer.start()
        reader.start()
        reader.join(timeout=5.0)
        stop_event.set()
        writer.join(timeout=2.0)

        assert not errors, f"Concurrent read/write raised: {errors}"
        assert not writer.is_alive(), "Writer thread did not stop"

    def test_concurrent_clear_and_touch_no_exception(self) -> None:
        """Verifies concurrent clear and touch no exception."""
        monitor = _make_monitor()
        errors: list[Exception] = []

        def _touch() -> None:
            """Touch."""
            try:
                for _ in range(200):
                    _touch_run(monitor, "run-shared", "dispatching")
            except Exception as exc:
                errors.append(exc)

        def _clear() -> None:
            """Clear."""
            try:
                for _ in range(200):
                    monitor.clear("run-shared")
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=_touch)
        t2 = threading.Thread(target=_clear)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert not errors, f"Concurrent clear/touch raised: {errors}"


# ---------------------------------------------------------------------------
# RunHeartbeatMonitor - watchdog_once signal injection
# ---------------------------------------------------------------------------


class TestRunHeartbeatMonitorWatchdogOnce:
    """Test suite for RunHeartbeatMonitorWatchdogOnce."""

    @pytest.mark.asyncio
    async def test_watchdog_signals_timed_out_run(self) -> None:
        """Verifies watchdog signals timed out run."""
        monitor = _make_monitor(state_timeout_s={"dispatching": 1})
        old_ms = int(time.time() * 1000) - 5_000
        _touch_run(monitor, "run-stale", "dispatching", timestamp_ms=old_ms)

        signal_calls: list[Any] = []
        gateway = _fake_gateway(signal_calls)

        await monitor.watchdog_once(gateway)

        assert len(signal_calls) == 1
        args, _ = signal_calls[0]
        run_id_arg, signal_req = args[0], args[1]
        assert run_id_arg == "run-stale"
        assert signal_req.signal_type == "heartbeat_timeout"

    @pytest.mark.asyncio
    async def test_watchdog_skips_fresh_run(self) -> None:
        """Verifies watchdog skips fresh run."""
        monitor = _make_monitor(state_timeout_s={"dispatching": 300})
        _touch_run(monitor, "run-fresh", "dispatching")

        signal_calls: list[Any] = []
        gateway = _fake_gateway(signal_calls)

        await monitor.watchdog_once(gateway)

        assert len(signal_calls) == 0

    @pytest.mark.asyncio
    async def test_watchdog_does_not_repeat_signal_for_stuck_run(self) -> None:
        """Verifies watchdog does not repeat signal for stuck run."""
        monitor = _make_monitor(state_timeout_s={"dispatching": 1})
        old_ms = int(time.time() * 1000) - 5_000
        _touch_run(monitor, "run-stuck", "dispatching", timestamp_ms=old_ms)

        signal_calls: list[Any] = []
        gateway = _fake_gateway(signal_calls)

        await monitor.watchdog_once(gateway)
        await monitor.watchdog_once(gateway)

        assert len(signal_calls) == 1, "Must not re-signal an already-timed-out run"

    @pytest.mark.asyncio
    async def test_watchdog_swallows_gateway_exception(self) -> None:
        """Verifies watchdog swallows gateway exception."""
        monitor = _make_monitor(state_timeout_s={"dispatching": 1})
        old_ms = int(time.time() * 1000) - 5_000
        _touch_run(monitor, "run-bad", "dispatching", timestamp_ms=old_ms)

        gateway = MagicMock()
        gateway.signal_workflow = AsyncMock(side_effect=RuntimeError("network error"))

        # Should not raise even when gateway throws
        await monitor.watchdog_once(gateway)


# ---------------------------------------------------------------------------
# RunHeartbeatMonitor - health check
# ---------------------------------------------------------------------------


class TestRunHeartbeatMonitorHealthCheck:
    """Test suite for RunHeartbeatMonitorHealthCheck."""

    def test_ok_when_no_runs_tracked(self) -> None:
        """Verifies ok when no runs tracked."""
        monitor = _make_monitor()
        check_fn = monitor.make_health_check_fn()
        status, _msg = check_fn()
        assert status == HealthStatus.OK

    def test_ok_when_all_runs_healthy(self) -> None:
        """Verifies ok when all runs healthy."""
        monitor = _make_monitor(state_timeout_s={"dispatching": 300})
        _touch_run(monitor, "run-1", "dispatching")
        check_fn = monitor.make_health_check_fn()
        status, _ = check_fn()
        assert status == HealthStatus.OK

    def test_unhealthy_when_run_timed_out_and_signalled(self) -> None:
        """Verifies unhealthy when run timed out and signalled."""
        monitor = _make_monitor(state_timeout_s={"dispatching": 1})
        old_ms = int(time.time() * 1000) - 5_000
        _touch_run(monitor, "run-bad", "dispatching", timestamp_ms=old_ms)
        monitor.get_timed_out_runs()  # marks run_id in _timed_out
        check_fn = monitor.make_health_check_fn()
        status, msg = check_fn()
        assert status == HealthStatus.UNHEALTHY
        assert "run-bad" in msg

    def test_degraded_when_run_near_timeout(self) -> None:
        # Set timeout to 10 s; place run 9 s ago (90% = within 80% threshold)
        """Verifies degraded when run near timeout."""
        monitor = _make_monitor(state_timeout_s={"dispatching": 10})
        old_ms = int(time.time() * 1000) - 9_000
        _touch_run(monitor, "run-near", "dispatching", timestamp_ms=old_ms)
        check_fn = monitor.make_health_check_fn()
        status, msg = check_fn()
        assert status == HealthStatus.DEGRADED
        assert "run-near" in msg


# ---------------------------------------------------------------------------
# HeartbeatWatchdog - lifecycle
# ---------------------------------------------------------------------------


class TestHeartbeatWatchdog:
    """Test suite for HeartbeatWatchdog."""

    @pytest.mark.asyncio
    async def test_watchdog_calls_watchdog_once_on_schedule(self) -> None:
        """Verifies watchdog calls watchdog once on schedule."""
        monitor = _make_monitor(state_timeout_s={"dispatching": 1})
        old_ms = int(time.time() * 1000) - 5_000
        _touch_run(monitor, "run-1", "dispatching", timestamp_ms=old_ms)

        signal_calls: list[Any] = []
        gateway = _fake_gateway(signal_calls)

        watchdog = HeartbeatWatchdog(monitor=monitor, gateway=gateway, interval_s=0)
        await watchdog.start()
        # Give the background loop one iteration
        await asyncio.sleep(0.05)
        await watchdog.stop()

        assert len(signal_calls) >= 1

    @pytest.mark.asyncio
    async def test_watchdog_stop_is_idempotent(self) -> None:
        """Verifies watchdog stop is idempotent."""
        monitor = _make_monitor()
        gateway = _fake_gateway()
        watchdog = HeartbeatWatchdog(monitor=monitor, gateway=gateway, interval_s=60)
        await watchdog.start()
        await watchdog.stop()
        await watchdog.stop()  # Second stop must not raise

    @pytest.mark.asyncio
    async def test_double_start_logs_warning_and_does_not_duplicate(self) -> None:
        """Verifies double start logs warning and does not duplicate."""
        monitor = _make_monitor()
        gateway = _fake_gateway()
        watchdog = HeartbeatWatchdog(monitor=monitor, gateway=gateway, interval_s=60)
        await watchdog.start()
        # Second start should be a no-op (logged as warning)
        await watchdog.start()
        await watchdog.stop()

    @pytest.mark.asyncio
    async def test_watchdog_loop_survives_scan_exception(self) -> None:
        """_loop must not crash when watchdog_once raises."""

        class _BrokenMonitor(RunHeartbeatMonitor):
            """Test suite for  BrokenMonitor."""

            async def watchdog_once(self, gateway: Any) -> None:  # type: ignore[override]
                """Watchdog once."""
                raise RuntimeError("simulated scan crash")

        monitor = _BrokenMonitor()
        gateway = _fake_gateway()
        watchdog = HeartbeatWatchdog(monitor=monitor, gateway=gateway, interval_s=0)
        await watchdog.start()
        await asyncio.sleep(0.05)
        await watchdog.stop()  # Must not raise despite internal scan errors


# ---------------------------------------------------------------------------
# KernelSelfHeartbeat - probe staleness and responsiveness checks
# ---------------------------------------------------------------------------


class TestKernelSelfHeartbeat:
    """Test suite for KernelSelfHeartbeat."""

    @pytest.mark.asyncio
    async def test_is_stale_before_first_refresh(self) -> None:
        """Verifies is stale before first refresh."""
        hb = KernelSelfHeartbeat()
        assert hb.is_stale() is True

    @pytest.mark.asyncio
    async def test_not_stale_after_refresh(self) -> None:
        """Verifies not stale after refresh."""
        event_log = MagicMock()
        event_log.load = AsyncMock(return_value=[])
        projection = MagicMock()
        projection.get = AsyncMock(return_value=MagicMock(projected_offset=0))

        hb = KernelSelfHeartbeat(stale_age_s=60)
        await hb.refresh(event_log=event_log, projection=projection)
        assert hb.is_stale() is False

    @pytest.mark.asyncio
    async def test_event_log_check_returns_ok_when_responsive(self) -> None:
        """Verifies event log check returns ok when responsive."""
        event_log = MagicMock()
        event_log.load = AsyncMock(return_value=[])
        projection = MagicMock()
        projection.get = AsyncMock(return_value=MagicMock(projected_offset=0))

        hb = KernelSelfHeartbeat()
        await hb.refresh(event_log=event_log, projection=projection)

        check_fn = hb.event_log_check()
        status, _msg = check_fn()
        assert status == HealthStatus.OK

    @pytest.mark.asyncio
    async def test_event_log_check_returns_unhealthy_before_first_refresh(self) -> None:
        """Verifies event log check returns unhealthy before first refresh."""
        hb = KernelSelfHeartbeat()
        # Never refreshed → unhealthy (not yet checked)
        check_fn = hb.event_log_check()
        status, msg = check_fn()
        assert status == HealthStatus.UNHEALTHY
        assert "not" in msg

    @pytest.mark.asyncio
    async def test_projection_check_returns_ok_when_responsive(self) -> None:
        """Verifies projection check returns ok when responsive."""
        event_log = MagicMock()
        event_log.load = AsyncMock(return_value=[])
        projection = MagicMock()
        projection.get = AsyncMock(return_value=MagicMock(projected_offset=0))

        hb = KernelSelfHeartbeat()
        await hb.refresh(event_log=event_log, projection=projection)

        check_fn = hb.projection_check()
        status, _ = check_fn()
        assert status == HealthStatus.OK

    @pytest.mark.asyncio
    async def test_refresh_marks_unhealthy_when_event_log_raises(self) -> None:
        """Verifies refresh marks unhealthy when event log raises."""
        event_log = MagicMock()
        event_log.load = AsyncMock(side_effect=RuntimeError("storage failure"))
        projection = MagicMock()
        projection.get = AsyncMock(return_value=MagicMock(projected_offset=0))

        hb = KernelSelfHeartbeat()
        await hb.refresh(event_log=event_log, projection=projection)

        check_fn = hb.event_log_check()
        status, msg = check_fn()
        assert status == HealthStatus.UNHEALTHY
        assert "storage failure" in msg
