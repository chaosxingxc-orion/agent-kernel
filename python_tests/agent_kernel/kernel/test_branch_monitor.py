"""Verifies for branchmonitor per-branch heartbeat tracking."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agent_kernel.kernel.branch_monitor import BranchHeartbeat, BranchMonitor
from agent_kernel.kernel.contracts import ScriptFailureEvidence

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _back_dated_iso(ms: int) -> str:
    """Returns an ISO timestamp offset ms milliseconds into the past."""
    dt = datetime.now(UTC) - timedelta(milliseconds=ms)
    return dt.isoformat()


# ---------------------------------------------------------------------------
# Registration and heartbeat recording tests
# ---------------------------------------------------------------------------


class TestBranchRegistration:
    """BranchMonitor branch registration tests."""

    def test_register_new_branch_succeeds(self) -> None:
        """Verifies register new branch succeeds."""
        monitor = BranchMonitor()
        monitor.register_branch("a1", expected_interval_ms=1000)
        # Not stalled immediately after registration.
        stalled = monitor.get_stalled_branches()
        assert "a1" not in stalled

    def test_register_duplicate_raises(self) -> None:
        """Verifies register duplicate raises."""
        monitor = BranchMonitor()
        monitor.register_branch("a1", expected_interval_ms=1000)
        with pytest.raises(ValueError, match="a1"):
            monitor.register_branch("a1", expected_interval_ms=1000)

    def test_complete_branch_excludes_from_stall_detection(self) -> None:
        """Verifies complete branch excludes from stall detection."""
        monitor = BranchMonitor()
        monitor.register_branch("a1", expected_interval_ms=1)
        # Manually back-date the heartbeat to simulate expiry.
        state = monitor._branches["a1"]
        state.last_heartbeat_at = _back_dated_iso(10_000)
        # Complete it — should not appear in stalled list.
        monitor.complete_branch("a1")
        assert "a1" not in monitor.get_stalled_branches()


# ---------------------------------------------------------------------------
# Stall detection tests
# ---------------------------------------------------------------------------


class TestStallDetection:
    """BranchMonitor stall detection tests."""

    def test_not_stalled_immediately_after_registration(self) -> None:
        """Verifies not stalled immediately after registration."""
        monitor = BranchMonitor()
        monitor.register_branch("a1", expected_interval_ms=5000)
        assert monitor.get_stalled_branches() == []

    def test_stalled_when_heartbeat_overdue(self) -> None:
        """Branch with last_heartbeat_at well before grace period is stalled."""
        monitor = BranchMonitor()
        monitor.register_branch("a1", expected_interval_ms=100)
        # Back-date last heartbeat by 10 seconds (much more than 100*2 = 200ms).
        state = monitor._branches["a1"]
        state.last_heartbeat_at = _back_dated_iso(10_000)

        stalled = monitor.get_stalled_branches()
        assert "a1" in stalled

    def test_not_stalled_after_fresh_heartbeat(self) -> None:
        """Verifies not stalled after fresh heartbeat."""
        monitor = BranchMonitor()
        monitor.register_branch("a1", expected_interval_ms=100)
        # Back-date first, then record a fresh heartbeat.
        state = monitor._branches["a1"]
        state.last_heartbeat_at = _back_dated_iso(10_000)

        monitor.record_heartbeat("a1", budget_consumed_ratio=0.1)

        assert "a1" not in monitor.get_stalled_branches()

    def test_multiple_branches_only_stalled_ones_returned(self) -> None:
        """Verifies multiple branches only stalled ones returned."""
        monitor = BranchMonitor()
        monitor.register_branch("a1", expected_interval_ms=100)
        monitor.register_branch("a2", expected_interval_ms=100)
        # Back-date a1 only.
        monitor._branches["a1"].last_heartbeat_at = _back_dated_iso(10_000)

        stalled = monitor.get_stalled_branches()
        assert "a1" in stalled
        assert "a2" not in stalled


# ---------------------------------------------------------------------------
# Dead-loop detection tests
# ---------------------------------------------------------------------------


class TestDeadLoopDetection:
    """BranchMonitor dead-loop heuristic tests."""

    def test_not_dead_loop_when_low_ratio(self) -> None:
        """Verifies not dead loop when low ratio."""
        monitor = BranchMonitor()
        monitor.register_branch("a1", expected_interval_ms=1000)
        monitor.record_heartbeat("a1", budget_consumed_ratio=0.3)
        assert monitor.is_suspected_dead_loop("a1") is False

    def test_not_dead_loop_when_high_ratio_with_output(self) -> None:
        """Verifies not dead loop when high ratio with output."""
        monitor = BranchMonitor()
        monitor.register_branch("a1", expected_interval_ms=1000)
        monitor.record_heartbeat("a1", budget_consumed_ratio=0.95, output_produced=True)
        assert monitor.is_suspected_dead_loop("a1") is False

    def test_dead_loop_when_high_ratio_no_output(self) -> None:
        """budget_consumed_ratio >= 0.9 and no output produced → suspected dead loop."""
        monitor = BranchMonitor()
        monitor.register_branch("a1", expected_interval_ms=1000)
        monitor.record_heartbeat("a1", budget_consumed_ratio=0.95)
        assert monitor.is_suspected_dead_loop("a1") is True

    def test_dead_loop_threshold_exactly_0_9(self) -> None:
        """Exactly 0.9 should trigger the dead-loop flag."""
        monitor = BranchMonitor()
        monitor.register_branch("a1", expected_interval_ms=1000)
        monitor.record_heartbeat("a1", budget_consumed_ratio=0.9)
        assert monitor.is_suspected_dead_loop("a1") is True

    def test_dead_loop_below_threshold(self) -> None:
        """0.89 should NOT trigger dead-loop flag."""
        monitor = BranchMonitor()
        monitor.register_branch("a1", expected_interval_ms=1000)
        monitor.record_heartbeat("a1", budget_consumed_ratio=0.89)
        assert monitor.is_suspected_dead_loop("a1") is False

    def test_unregistered_branch_raises_key_error(self) -> None:
        """Verifies unregistered branch raises key error."""
        monitor = BranchMonitor()
        with pytest.raises(KeyError):
            monitor.is_suspected_dead_loop("missing")


# ---------------------------------------------------------------------------
# ScriptFailureEvidence building tests
# ---------------------------------------------------------------------------


class TestBuildScriptFailureEvidence:
    """BranchMonitor.build_script_failure_evidence tests."""

    def test_basic_heartbeat_timeout_evidence(self) -> None:
        """Verifies basic heartbeat timeout evidence."""
        monitor = BranchMonitor()
        monitor.register_branch("a1", expected_interval_ms=1000)
        monitor.record_heartbeat("a1", budget_consumed_ratio=0.5)

        evidence = monitor.build_script_failure_evidence(
            action_id="a1",
            script_id="script:v1",
            original_script="print('hello')",
        )

        assert isinstance(evidence, ScriptFailureEvidence)
        assert evidence.script_id == "script:v1"
        assert evidence.failure_kind == "heartbeat_timeout"
        assert evidence.budget_consumed_ratio == 0.5
        assert evidence.original_script == "print('hello')"
        assert evidence.suspected_cause is None

    def test_dead_loop_evidence_sets_suspected_cause(self) -> None:
        """Verifies dead loop evidence sets suspected cause."""
        monitor = BranchMonitor()
        monitor.register_branch("a1", expected_interval_ms=1000)
        monitor.record_heartbeat("a1", budget_consumed_ratio=0.95)

        evidence = monitor.build_script_failure_evidence(
            action_id="a1",
            script_id="script:loop",
            original_script="while True: pass",
        )

        assert evidence.suspected_cause == "possible_infinite_loop"
        assert evidence.output_produced is False

    def test_partial_output_sets_output_produced(self) -> None:
        """Verifies partial output sets output produced."""
        monitor = BranchMonitor()
        monitor.register_branch("a1", expected_interval_ms=1000)
        monitor.record_heartbeat("a1", budget_consumed_ratio=0.7)

        evidence = monitor.build_script_failure_evidence(
            action_id="a1",
            script_id="script:partial",
            original_script="print('starting...')",
            partial_output="starting...",
            stderr_tail="Error: out of memory",
        )

        assert evidence.output_produced is True
        assert evidence.partial_output == "starting..."
        assert evidence.stderr_tail == "Error: out of memory"

    def test_output_produced_flag_propagates_from_heartbeat(self) -> None:
        """Verifies output produced flag propagates from heartbeat."""
        monitor = BranchMonitor()
        monitor.register_branch("a1", expected_interval_ms=1000)
        monitor.record_heartbeat("a1", budget_consumed_ratio=0.95, output_produced=True)

        evidence = monitor.build_script_failure_evidence(
            action_id="a1",
            script_id="script:mixed",
            original_script="for i in range(10**9): print(i)",
        )

        # output_produced=True from heartbeat, so NOT a dead loop.
        assert evidence.output_produced is True
        assert evidence.suspected_cause is None


# ---------------------------------------------------------------------------
# BranchHeartbeat DTO tests
# ---------------------------------------------------------------------------


class TestBranchHeartbeat:
    """BranchHeartbeat frozen dataclass tests."""

    def test_frozen_instance(self) -> None:
        """Verifies frozen instance."""
        from dataclasses import FrozenInstanceError

        hb = BranchHeartbeat(
            action_id="a1",
            last_heartbeat_at="2026-04-03T00:00:00+00:00",
            expected_interval_ms=5000,
            budget_consumed_ratio=0.3,
        )
        with pytest.raises((FrozenInstanceError, AttributeError)):
            hb.action_id = "other"  # type: ignore[misc]

    def test_default_budget_ratio(self) -> None:
        """Verifies default budget ratio."""
        hb = BranchHeartbeat(
            action_id="a1",
            last_heartbeat_at="2026-04-03T00:00:00+00:00",
            expected_interval_ms=5000,
        )
        assert hb.budget_consumed_ratio == 0.0
