"""Per-branch heartbeat monitoring for parallel execution.

Implements Phase 3 (Parallel Execution) 鈥?BranchMonitor component.

BranchMonitor is NOT an authority.  It does not write to the EventLog.
It is a cooperative diagnostic tool used by PlanExecutor to detect stalled
or dead-loop parallel branches before the TurnEngine's RecoveryGateService
is invoked.

Thread-safety note: BranchMonitor is NOT thread-safe.  It is designed for
cooperative asyncio use within a single event loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from agent_kernel.kernel.contracts import ScriptFailureEvidence


@dataclass(frozen=True, slots=True)
class BranchHeartbeat:
    """Captures one heartbeat snapshot for a parallel branch.

    Attributes:
        action_id: Action identifier for the branch.
        last_heartbeat_at: ISO 8601 UTC timestamp of the most recent heartbeat.
        expected_interval_ms: Expected time between heartbeats in milliseconds.
        budget_consumed_ratio: Fraction of the execution budget consumed (0.0-1.0).

    """

    action_id: str
    last_heartbeat_at: str
    expected_interval_ms: int
    budget_consumed_ratio: float = 0.0


@dataclass
class _BranchState:
    """Mutable internal state for one tracked branch.

    Not exposed publicly; accessed only through BranchMonitor methods.
    """

    action_id: str
    expected_interval_ms: int
    last_heartbeat_at: str
    budget_consumed_ratio: float = 0.0
    completed: bool = False
    output_produced: bool = False


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(UTC).isoformat()


def _now_ms() -> float:
    """Return the current epoch time in milliseconds."""
    return datetime.now(UTC).timestamp() * 1000.0


def _parse_ms(iso: str) -> float:
    """Parse an ISO 8601 UTC string to epoch milliseconds.

    Args:
        iso: ISO 8601 timestamp string (may include timezone offset).

    Returns:
        Epoch milliseconds as a float.

    """
    return datetime.fromisoformat(iso).timestamp() * 1000.0


# Ratio threshold above which a branch is suspected to be in a dead loop.
_DEAD_LOOP_RATIO_THRESHOLD: float = 0.9


class BranchMonitor:
    """Tracks per-branch heartbeat for parallel execution.

    Used by PlanExecutor to detect stalled or dead-loop branches.
    NOT an authority 鈥?does not write to EventLog.

    The monitor tracks registered branches and detects:
    - Missing heartbeats (no update within ``expected_interval_ms * 2``).
    - Dead-loop signature: ``budget_consumed_ratio 鈮?1.0`` with no output.
    """

    def __init__(self) -> None:
        """Initialize the instance with configured dependencies."""
        self._branches: dict[str, _BranchState] = {}

    def register_branch(self, action_id: str, expected_interval_ms: int) -> None:
        """Register a new parallel branch for monitoring.

        Args:
            action_id: Unique action identifier for the branch.
            expected_interval_ms: Expected maximum time between heartbeats.

        Raises:
            ValueError: If a branch with the same action_id is already registered.

        """
        if action_id in self._branches:
            raise ValueError(f"Branch already registered: {action_id!r}")
        self._branches[action_id] = _BranchState(
            action_id=action_id,
            expected_interval_ms=expected_interval_ms,
            last_heartbeat_at=_utc_now_iso(),
        )

    def record_heartbeat(
        self,
        action_id: str,
        budget_consumed_ratio: float = 0.0,
        *,
        output_produced: bool = False,
    ) -> None:
        """Record a heartbeat for an active branch.

        Args:
            action_id: Branch identifier.
            budget_consumed_ratio: Current fraction of budget consumed (0.0-1.0).
            output_produced: Whether any stdout output has been produced so far.

        Raises:
            KeyError: If the action_id has not been registered.

        """
        state = self._branches[action_id]
        state.last_heartbeat_at = _utc_now_iso()
        state.budget_consumed_ratio = budget_consumed_ratio
        if output_produced:
            state.output_produced = True

    def complete_branch(self, action_id: str) -> None:
        """Mark a branch as completed (no further stall detection).

        Args:
            action_id: Branch identifier.

        Raises:
            KeyError: If the action_id has not been registered.

        """
        self._branches[action_id].completed = True

    def get_stalled_branches(self) -> list[str]:
        """Return action_ids of branches whose heartbeats are overdue.

        A branch is considered stalled when the time since its last heartbeat
        exceeds ``expected_interval_ms * 2``.

        Returns:
            List of stalled action_ids (excludes completed branches).

        """
        now = _now_ms()
        stalled: list[str] = []
        for state in self._branches.values():
            if state.completed:
                continue
            elapsed_ms = now - _parse_ms(state.last_heartbeat_at)
            grace_period_ms = state.expected_interval_ms * 2
            if elapsed_ms > grace_period_ms:
                stalled.append(state.action_id)
        return stalled

    def is_suspected_dead_loop(self, action_id: str) -> bool:
        """Return True when the branch shows a dead-loop signature.

        The heuristic: ``budget_consumed_ratio >= 0.9`` and no output produced.

        Args:
            action_id: Branch identifier.

        Returns:
            True when the branch is suspected to be in an infinite loop.

        Raises:
            KeyError: If the action_id has not been registered.

        """
        state = self._branches[action_id]
        return (
            state.budget_consumed_ratio >= _DEAD_LOOP_RATIO_THRESHOLD and not state.output_produced
        )

    def build_script_failure_evidence(
        self,
        action_id: str,
        script_id: str,
        original_script: str,
        partial_output: str | None = None,
        stderr_tail: str | None = None,
    ) -> ScriptFailureEvidence:
        """Build structured failure evidence for a failed or timed-out branch.

        Inspects the branch state to determine whether the failure looks like
        a dead loop (high budget consumption, no output) or a heartbeat timeout.

        Args:
            action_id: Branch identifier.
            script_id: Script identifier within the skill.
            original_script: Script source content for model inspection.
            partial_output: Optional partial stdout captured before failure.
            stderr_tail: Optional last N lines of stderr.

        Returns:
            Structured ScriptFailureEvidence for ReflectionContextBuilder.

        Raises:
            KeyError: If the action_id has not been registered.

        """
        state = self._branches[action_id]
        suspected_dead_loop = (
            state.budget_consumed_ratio >= _DEAD_LOOP_RATIO_THRESHOLD and not state.output_produced
        )

        failure_kind: Literal[
            "heartbeat_timeout",
            "runtime_error",
            "permission_denied",
            "resource_exhausted",
            "output_validation_failed",
        ] = "heartbeat_timeout"

        suspected_cause: str | None = None
        if suspected_dead_loop:
            suspected_cause = "possible_infinite_loop"

        output_produced = bool(partial_output) or state.output_produced

        return ScriptFailureEvidence(
            script_id=script_id,
            failure_kind=failure_kind,
            budget_consumed_ratio=state.budget_consumed_ratio,
            output_produced=output_produced,
            suspected_cause=suspected_cause,
            partial_output=partial_output,
            original_script=original_script,
            stderr_tail=stderr_tail,
        )
