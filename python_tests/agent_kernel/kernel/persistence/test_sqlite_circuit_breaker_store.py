"""Tests for SQLiteCircuitBreakerStore."""

from __future__ import annotations

from agent_kernel.kernel.persistence.sqlite_circuit_breaker_store import (
    SQLiteCircuitBreakerStore,
)


class TestSQLiteCircuitBreakerStoreGetState:
    def test_returns_zero_state_for_unknown_effect_class(self) -> None:
        store = SQLiteCircuitBreakerStore()
        count, ts = store.get_state("payment.charge")
        assert count == 0
        assert ts == 0.0

    def test_returns_recorded_state_after_failure(self) -> None:
        store = SQLiteCircuitBreakerStore()
        store.record_failure("payment.charge")
        count, ts = store.get_state("payment.charge")
        assert count == 1
        assert ts > 0.0

    def test_effect_classes_are_independent(self) -> None:
        store = SQLiteCircuitBreakerStore()
        store.record_failure("email.send")
        count_a, _ = store.get_state("email.send")
        count_b, _ = store.get_state("payment.charge")
        assert count_a == 1
        assert count_b == 0


class TestSQLiteCircuitBreakerStoreRecordFailure:
    def test_first_failure_returns_one(self) -> None:
        store = SQLiteCircuitBreakerStore()
        new_count = store.record_failure("payment.charge")
        assert new_count == 1

    def test_consecutive_failures_accumulate(self) -> None:
        store = SQLiteCircuitBreakerStore()
        store.record_failure("payment.charge")
        store.record_failure("payment.charge")
        new_count = store.record_failure("payment.charge")
        assert new_count == 3

    def test_timestamp_updated_on_each_failure(self) -> None:
        store = SQLiteCircuitBreakerStore()
        store.record_failure("payment.charge")
        _, ts1 = store.get_state("payment.charge")
        store.record_failure("payment.charge")
        _, ts2 = store.get_state("payment.charge")
        assert ts2 >= ts1


class TestSQLiteCircuitBreakerStoreReset:
    def test_reset_clears_recorded_failures(self) -> None:
        store = SQLiteCircuitBreakerStore()
        store.record_failure("payment.charge")
        store.record_failure("payment.charge")
        store.reset("payment.charge")
        count, ts = store.get_state("payment.charge")
        assert count == 0
        assert ts == 0.0

    def test_reset_unknown_effect_class_is_noop(self) -> None:
        store = SQLiteCircuitBreakerStore()
        store.reset("nonexistent.class")
        count, ts = store.get_state("nonexistent.class")
        assert count == 0
        assert ts == 0.0

    def test_reset_does_not_affect_other_effect_classes(self) -> None:
        store = SQLiteCircuitBreakerStore()
        store.record_failure("email.send")
        store.record_failure("payment.charge")
        store.reset("email.send")
        count_a, _ = store.get_state("email.send")
        count_b, _ = store.get_state("payment.charge")
        assert count_a == 0
        assert count_b == 1


class TestSQLiteCircuitBreakerStoreGateIntegration:
    """Verifies that RecoveryGate uses the store when provided."""

    def test_gate_open_after_threshold_with_persistent_store(self) -> None:
        from agent_kernel.kernel.contracts import CircuitBreakerPolicy
        from agent_kernel.kernel.recovery.gate import PlannedRecoveryGateService as RecoveryGate

        store = SQLiteCircuitBreakerStore()
        policy = CircuitBreakerPolicy(threshold=2, half_open_after_ms=60_000)
        gate = RecoveryGate(
            circuit_breaker_policy=policy,
            circuit_breaker_store=store,
        )
        store.record_failure("payment.charge")
        store.record_failure("payment.charge")
        assert gate._is_circuit_open("payment.charge") is True

    def test_gate_closed_after_reset_via_on_action_success(self) -> None:
        from agent_kernel.kernel.contracts import CircuitBreakerPolicy
        from agent_kernel.kernel.recovery.gate import PlannedRecoveryGateService as RecoveryGate

        store = SQLiteCircuitBreakerStore()
        policy = CircuitBreakerPolicy(threshold=1, half_open_after_ms=60_000)
        gate = RecoveryGate(
            circuit_breaker_policy=policy,
            circuit_breaker_store=store,
        )
        store.record_failure("payment.charge")
        assert gate._is_circuit_open("payment.charge") is True
        gate.on_action_success("payment.charge")
        assert gate._is_circuit_open("payment.charge") is False
