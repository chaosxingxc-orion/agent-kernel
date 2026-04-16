"""Verifies for agent kernel.runtime.metrics."""

from __future__ import annotations

import concurrent.futures

from agent_kernel.runtime.metrics import KernelMetricsCollector, MetricPoint


class TestCounterIncrementAndGet:
    """Counter basics: increment and read back."""

    def test_counter_increment_and_get(self) -> None:
        """Verifies counter increment and get."""
        c = KernelMetricsCollector()
        assert c.get_counter("x") == 0
        c.inc("x")
        assert c.get_counter("x") == 1
        c.inc("x")
        c.inc("x")
        assert c.get_counter("x") == 3


class TestGaugeSetAndGet:
    """Gauge basics: set to arbitrary value and read back."""

    def test_gauge_set_and_get(self) -> None:
        """Verifies gauge set and get."""
        c = KernelMetricsCollector()
        assert c.get_gauge("g") == 0.0
        c.set_gauge("g", 42.5)
        assert c.get_gauge("g") == 42.5
        c.set_gauge("g", -1.0)
        assert c.get_gauge("g") == -1.0


class TestCounterWithLabels:
    """Counters with different label sets are independent."""

    def test_counter_with_labels(self) -> None:
        """Verifies counter with labels."""
        c = KernelMetricsCollector()
        c.inc("req", {"method": "GET"})
        c.inc("req", {"method": "GET"})
        c.inc("req", {"method": "POST"})

        assert c.get_counter("req", {"method": "GET"}) == 2
        assert c.get_counter("req", {"method": "POST"}) == 1
        # No labels -- separate bucket.
        assert c.get_counter("req") == 0


class TestSnapshotReturnsAllMetrics:
    """snapshot() returns MetricPoint for every counter and gauge."""

    def test_snapshot_returns_all_metrics(self) -> None:
        """Verifies snapshot returns all metrics."""
        c = KernelMetricsCollector()
        c.inc("a")
        c.inc("a", {"x": "1"})
        c.set_gauge("g", 7.0)

        points = c.snapshot()
        assert len(points) == 3
        assert all(isinstance(p, MetricPoint) for p in points)

        names = {p.name for p in points}
        assert names == {"a", "g"}

        # Verify labels are plain dicts.
        labeled = [p for p in points if p.labels]
        assert len(labeled) >= 1
        assert labeled[0].labels == {"x": "1"}


class TestOnRunLifecycleIncrementsCounters:
    """ObservabilityHook lifecycle events drive counters and gauge."""

    def test_on_run_lifecycle_increments_counters(self) -> None:
        """Verifies on run lifecycle increments counters."""
        c = KernelMetricsCollector()
        ts = 1_000_000

        # Start two runs.
        c.on_run_lifecycle_transition(
            run_id="r1",
            from_state="init",
            to_state="ready",
            timestamp_ms=ts,
        )
        c.on_run_lifecycle_transition(
            run_id="r2",
            from_state="init",
            to_state="ready",
            timestamp_ms=ts,
        )
        assert c.get_counter("runs_started_total") == 2
        assert c.get_gauge("active_runs") == 2.0

        # Complete one run.
        c.on_run_lifecycle_transition(
            run_id="r1",
            from_state="ready",
            to_state="completed",
            timestamp_ms=ts,
        )
        assert c.get_counter("runs_completed_total", {"outcome": "completed"}) == 1
        assert c.get_gauge("active_runs") == 1.0

        # Abort the other.
        c.on_run_lifecycle_transition(
            run_id="r2",
            from_state="ready",
            to_state="aborted",
            timestamp_ms=ts,
        )
        assert c.get_counter("runs_completed_total", {"outcome": "aborted"}) == 1
        assert c.get_gauge("active_runs") == 0.0

    def test_turn_state_transition_increments_turns(self) -> None:
        """Verifies turn state transition increments turns."""
        c = KernelMetricsCollector()
        c.on_turn_state_transition(
            run_id="r1",
            action_id="a1",
            from_state="admission_checked",
            to_state="dispatched",
            turn_offset=0,
            timestamp_ms=1,
        )
        c.on_turn_state_transition(
            run_id="r1",
            action_id="a2",
            from_state="collecting",
            to_state="completed_noop",
            turn_offset=1,
            timestamp_ms=2,
        )
        assert c.get_counter("turns_executed_total", {"outcome": "dispatched"}) == 1
        assert c.get_counter("turns_executed_total", {"outcome": "noop"}) == 1

    def test_recovery_triggered_increments(self) -> None:
        """Verifies recovery triggered increments."""
        c = KernelMetricsCollector()
        c.on_recovery_triggered(run_id="r1", reason_code="timeout", mode="abort")
        c.on_recovery_triggered(
            run_id="r1",
            reason_code="error",
            mode="human_escalation",
        )
        assert c.get_counter("recovery_decisions_total", {"mode": "abort"}) == 1
        assert (
            c.get_counter(
                "recovery_decisions_total",
                {"mode": "human_escalation"},
            )
            == 1
        )


class TestThreadSafety:
    """Concurrent increments from multiple threads produce correct totals."""

    def test_thread_safety(self) -> None:
        """Verifies thread safety."""
        c = KernelMetricsCollector()
        n_threads = 8
        n_increments = 1_000

        def _worker() -> None:
            """Runs worker behavior for the test."""
            for _ in range(n_increments):
                c.inc("concurrent_counter")

        with concurrent.futures.ThreadPoolExecutor(max_workers=n_threads) as pool:
            futures = [pool.submit(_worker) for _ in range(n_threads)]
            for f in futures:
                f.result()

        assert c.get_counter("concurrent_counter") == n_threads * n_increments
