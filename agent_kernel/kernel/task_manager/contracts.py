"""Contracts for task-level lifecycle management in agent-kernel.

A *task* is a semantic unit of work that may require multiple run attempts.
The task_id stays stable across retries; each attempt gets its own attempt_id
and is linked to one run_id.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# All possible lifecycle states a task can be in.
TaskLifecycleState = Literal[
    "pending",  # registered, not yet started
    "running",  # an active run is executing this task
    "completed",  # goal achieved
    "failed",  # current attempt failed, within retry budget
    "restarting",  # RestartPolicy decided to retry; new run being launched
    "reflecting",  # retry budget exhausted; awaiting model decision
    "escalated",  # handed to human operator
    "aborted",  # retry budget exhausted and no reflection configured
]

# Which action to take when retry budget is exhausted.
ExhaustedPolicy = Literal["reflect", "escalate", "abort"]


@dataclass(frozen=True, slots=True)
class TaskRestartPolicy:
    """Configures how many retries a task gets and what happens at exhaustion.

    Attributes:
        max_attempts: Total allowed attempts (first try + retries).
            Must be >= 1.
        backoff_base_ms: Base backoff in milliseconds between attempts.
            Actual delay is backoff_base_ms * attempt_seq (linear).
        on_exhausted: Action taken when attempt_seq reaches max_attempts.
            "reflect" passes failure history to ReflectionBridge for
            model-driven recovery; "escalate" triggers human_escalation;
            "abort" terminates the task immediately.
        heartbeat_timeout_ms: How long a task may stay in "running" without
            a state transition before TaskWatchdog considers it stalled.

    """

    max_attempts: int = 3
    backoff_base_ms: int = 2000
    on_exhausted: ExhaustedPolicy = "reflect"
    heartbeat_timeout_ms: int = 300_000  # 5 minutes


@dataclass(frozen=True, slots=True)
class TaskDescriptor:
    """Stable identity and policy for one task goal.

    The task_id is stable across all attempts.  The descriptor is registered
    once and never mutated; attempts are tracked separately via TaskAttempt.

    Attributes:
        task_id: Unique, stable task identifier (UUID recommended).
        session_id: Session that owns this task.
        task_kind: Structural role of this task in its parent plan.
            "plan_step" = step in SequentialPlan or DependencyGraph node.
            "parallel_branch" = one branch in a ParallelPlan group.
            "speculative_candidate" = one candidate in a SpeculativePlan.
            "root" = top-level task not part of a larger plan.
        goal_description: Human/model-readable description of what this task
            should accomplish.  Used by ReflectionBridge as LLM context.
        parent_task_id: Optional parent task for hierarchical plans.
        dependency_task_ids: Task ids that must complete before this task
            may start (for DependencyGraph plans).
        restart_policy: Restart and exhaustion policy for this task.
        metadata: Optional caller-defined key/value metadata.

    """

    task_id: str
    session_id: str
    task_kind: Literal[
        "root",
        "plan_step",
        "parallel_branch",
        "speculative_candidate",
    ]
    goal_description: str
    parent_task_id: str | None = None
    dependency_task_ids: tuple[str, ...] = field(default_factory=tuple)
    restart_policy: TaskRestartPolicy = field(default_factory=TaskRestartPolicy)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TaskAttempt:
    """Records one execution attempt for a task.

    A new TaskAttempt is created each time RestartPolicyEngine decides to
    retry.  attempt_seq starts at 1 (first try) and increments.

    Attributes:
        attempt_id: Unique identifier for this attempt.
        task_id: The parent task this attempt belongs to.
        run_id: The kernel Run executing this attempt.
        attempt_seq: 1-based sequence number within the task.
        started_at: ISO-8601 UTC timestamp when this attempt began.
        completed_at: ISO-8601 UTC timestamp when this attempt ended,
            or None if still in progress.
        outcome: Terminal outcome of this attempt, or None if running.
        failure: FailureEnvelope if this attempt failed, else None.
        reflection_output: Model decision text if this attempt was
            started as a result of reflect_and_replace, else None.

    """

    attempt_id: str
    task_id: str
    run_id: str
    attempt_seq: int
    started_at: str
    completed_at: str | None = None
    outcome: Literal["completed", "failed", "cancelled"] | None = None
    failure: Any | None = None  # FailureEnvelope — Any avoids circular import
    reflection_output: str | None = None


@dataclass(frozen=True, slots=True)
class TaskHealthStatus:
    """Point-in-time health snapshot for one task.

    Attributes:
        task_id: Task identifier.
        lifecycle_state: Current lifecycle state.
        current_run_id: Run id of the active attempt, or None.
        attempt_seq: Current attempt sequence number (1-based).
        max_attempts: Maximum allowed attempts from policy.
        last_heartbeat_ms: Epoch-ms of last observed activity, or None.
        consecutive_missed_beats: Number of watchdog sweeps with no activity.
        is_stalled: True when watchdog considers this task timed out.

    """

    task_id: str
    lifecycle_state: TaskLifecycleState
    current_run_id: str | None
    attempt_seq: int
    max_attempts: int
    last_heartbeat_ms: int | None = None
    consecutive_missed_beats: int = 0
    is_stalled: bool = False
