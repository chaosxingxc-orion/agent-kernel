"""Lightweight in-process metrics collector for kernel observability.

No external dependencies (no prometheus, no opentelemetry).
Designed as a building block -- production deployments export via
a /metrics HTTP endpoint or push to an external collector.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class MetricPoint:
    """One metric sample."""

    name: str
    value: float
    labels: dict[str, str]
    timestamp_ms: int


# Terminal FSM states that map to run-completion outcomes.
_TERMINAL_LIFECYCLE_STATES: dict[str, str] = {
    "completed": "completed",
    "aborted": "aborted",
    "failed": "failed",
}

# TurnEngine FSM terminal states mapped to outcome labels.
_TURN_OUTCOME_MAP: dict[str, str] = {
    "dispatched": "dispatched",
    "completed_noop": "noop",
    "dispatch_blocked": "blocked",
    "recovery_pending": "recovery_pending",
}


def _label_key(labels: dict[str, str] | None) -> frozenset[tuple[str, str]]:
    """Convert a label dict to a hashable frozenset key."""
    if not labels:
        return frozenset()
    return frozenset(labels.items())


@dataclass(slots=True)
class KernelMetricsCollector:
    """Thread-safe in-process metrics collector.

    Implements ``ObservabilityHook`` so it receives FSM transitions.
    Collects counters and gauges for:

    - ``runs_started_total`` (counter)
    - ``runs_completed_total`` (counter, label: outcome=completed|aborted|failed)
    - ``turns_executed_total`` (counter, label: outcome=dispatched|noop|blocked|recovery_pending)
    - ``turn_phase_duration_ms`` (histogram-like: stores last N samples)
    - ``active_runs`` (gauge)
    - ``recovery_decisions_total`` (counter, label:
      mode=static_compensation|human_escalation|abort|reflect_and_retry)
    """

    max_samples: int = 10_000
    _counters: dict[str, dict[frozenset[tuple[str, str]], int]] = field(
        default_factory=dict,
    )
    _gauges: dict[str, dict[frozenset[tuple[str, str]], float]] = field(
        default_factory=dict,
    )
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # ------------------------------------------------------------------
    # Counter methods
    # ------------------------------------------------------------------

    def inc(self, name: str, labels: dict[str, str] | None = None) -> None:
        """Increment a counter by 1.  Counters are monotonically increasing."""
        key = _label_key(labels)
        with self._lock:
            bucket = self._counters.setdefault(name, {})
            bucket[key] = bucket.get(key, 0) + 1

    def get_counter(self, name: str, labels: dict[str, str] | None = None) -> int:
        """Return current value of a counter (0 if not yet incremented)."""
        key = _label_key(labels)
        with self._lock:
            return self._counters.get(name, {}).get(key, 0)

    # ------------------------------------------------------------------
    # Gauge methods
    # ------------------------------------------------------------------

    def set_gauge(
        self,
        name: str,
        value: float,
        labels: dict[str, str] | None = None,
    ) -> None:
        """Set a gauge to an arbitrary value."""
        key = _label_key(labels)
        with self._lock:
            bucket = self._gauges.setdefault(name, {})
            bucket[key] = value

    def get_gauge(
        self,
        name: str,
        labels: dict[str, str] | None = None,
    ) -> float:
        """Return current value of a gauge (0.0 if not yet set)."""
        key = _label_key(labels)
        with self._lock:
            return self._gauges.get(name, {}).get(key, 0.0)

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    def snapshot(self) -> list[MetricPoint]:
        """Return all current metrics as a list of ``MetricPoint``."""
        now_ms = int(time.time() * 1000)
        points: list[MetricPoint] = []
        with self._lock:
            for name, buckets in self._counters.items():
                for label_key, value in buckets.items():
                    points.append(
                        MetricPoint(
                            name=name,
                            value=float(value),
                            labels=dict(label_key),
                            timestamp_ms=now_ms,
                        )
                    )
            for name, buckets in self._gauges.items():
                for label_key, value in buckets.items():
                    points.append(
                        MetricPoint(
                            name=name,
                            value=value,
                            labels=dict(label_key),
                            timestamp_ms=now_ms,
                        )
                    )
        return points

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
        """Increment ``turns_executed_total`` when reaching a terminal turn state."""
        outcome = _TURN_OUTCOME_MAP.get(to_state)
        if outcome:
            self.inc("turns_executed_total", {"outcome": outcome})

    def on_turn_phase(
        self,
        *,
        run_id: str,
        action_id: str,
        phase_name: str,
        elapsed_ms: int,
    ) -> None:
        """Record a turn-phase duration sample (counter of phase invocations)."""
        self.inc("turn_phase_invocations_total", {"phase": phase_name})

    def on_run_lifecycle_transition(
        self,
        *,
        run_id: str,
        from_state: str,
        to_state: str,
        timestamp_ms: int,
    ) -> None:
        """Track run lifecycle: starts, completions, and active-run gauge."""
        if to_state == "ready":
            self.inc("runs_started_total")
            current = self.get_gauge("active_runs")
            self.set_gauge("active_runs", current + 1)

        terminal_outcome = _TERMINAL_LIFECYCLE_STATES.get(to_state)
        if terminal_outcome:
            self.inc("runs_completed_total", {"outcome": terminal_outcome})
            current = self.get_gauge("active_runs")
            self.set_gauge("active_runs", max(0.0, current - 1))

    def on_llm_call(
        self,
        *,
        run_id: str,
        model_ref: str,
        latency_ms: int,
        token_usage: object | None = None,
    ) -> None:
        """Track LLM call counts per model."""
        self.inc("llm_calls_total", {"model_ref": model_ref})

    def on_action_dispatch(
        self,
        *,
        run_id: str,
        action_id: str,
        action_type: str,
        outcome_kind: str,
        latency_ms: int,
    ) -> None:
        """Track action dispatch counts per outcome."""
        self.inc("action_dispatches_total", {"outcome": outcome_kind})

    def on_recovery_triggered(
        self,
        *,
        run_id: str,
        reason_code: str,
        mode: str,
    ) -> None:
        """Increment ``recovery_decisions_total`` keyed by mode."""
        self.inc("recovery_decisions_total", {"mode": mode})

    def on_admission_evaluated(
        self,
        *,
        run_id: str,
        action_id: str,
        admitted: bool,
        latency_ms: int,
    ) -> None:
        """Track admission evaluation counts."""
        outcome = "admitted" if admitted else "rejected"
        self.inc("admission_evaluations_total", {"outcome": outcome})

    def on_dispatch_attempted(
        self,
        *,
        run_id: str,
        action_id: str,
        dedupe_outcome: str,
        latency_ms: int,
    ) -> None:
        """Track dispatch attempt counts by dedupe outcome."""
        self.inc("dispatch_attempts_total", {"dedupe_outcome": dedupe_outcome})

    def on_parallel_branch_result(
        self,
        *,
        run_id: str,
        group_idempotency_key: str,
        action_id: str,
        outcome: str,
        failure_code: str | None = None,
    ) -> None:
        """Track parallel branch results by outcome."""
        self.inc("parallel_branch_results_total", {"outcome": outcome})

    def on_dedupe_hit(
        self,
        *,
        run_id: str,
        action_id: str,
        outcome: str,
    ) -> None:
        """Track dedupe reservation outcomes."""
        self.inc("dedupe_hits_total", {"outcome": outcome})

    def on_reflection_round(
        self,
        *,
        run_id: str,
        action_id: str,
        round_num: int,
        corrected: bool,
    ) -> None:
        """Track reflection round counts."""
        self.inc(
            "reflection_rounds_total",
            {"corrected": str(corrected).lower()},
        )

    def on_circuit_breaker_trip(
        self,
        *,
        run_id: str,
        effect_class: str,
        failure_count: int,
        tripped: bool,
    ) -> None:
        """Track circuit breaker trips."""
        if tripped:
            self.inc("circuit_breaker_trips_total", {"effect_class": effect_class})

    def on_branch_rollback_triggered(
        self,
        *,
        run_id: str,
        group_idempotency_key: str,
        action_id: str,
        join_strategy: str,
    ) -> None:
        """Track branch rollback triggers."""
        self.inc("branch_rollbacks_total", {"join_strategy": join_strategy})
