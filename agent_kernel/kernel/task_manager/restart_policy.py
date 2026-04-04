"""RestartPolicyEngine: decides whether to retry or escalate a failed task.

Called by TaskWatchdog after a task attempt fails.  Consults TaskDescriptor's
TaskRestartPolicy to determine the next action.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from agent_kernel.kernel.task_manager.registry import TaskRegistry

_logger = logging.getLogger(__name__)

RestartAction = Literal["retry", "reflect", "escalate", "abort"]


@dataclass(frozen=True, slots=True)
class RestartDecision:
    """Output of RestartPolicyEngine.decide().

    Attributes:
        task_id: The task this decision applies to.
        action: What the engine decided to do next.
        next_attempt_seq: Sequence number for the next attempt (retry only).
        reason: Human-readable explanation.
    """

    task_id: str
    action: RestartAction
    next_attempt_seq: int | None
    reason: str


class RestartPolicyEngine:
    """Decides retry vs reflect vs abort for failed/stalled tasks.

    Args:
        registry: TaskRegistry used to update task state and create new attempts.
        facade: KernelFacade-compatible object with start_run() for launching
            new run attempts.  Typed as Any to avoid circular imports; must
            implement async start_run(StartRunRequest) -> StartRunResponse.
    """

    def __init__(self, registry: TaskRegistry, facade: Any) -> None:
        self._registry = registry
        self._facade = facade

    async def handle_failure(
        self,
        task_id: str,
        failed_run_id: str,
        failure: Any | None = None,
    ) -> RestartDecision:
        """Handle a task attempt failure and take the appropriate action.

        Records the attempt failure, evaluates the restart policy, and either
        launches a new run attempt or transitions the task to a terminal state.

        Args:
            task_id: Task that failed.
            failed_run_id: Run id of the failed attempt.
            failure: Optional FailureEnvelope from the run.

        Returns:
            RestartDecision describing what was done.
        """
        descriptor = self._registry.get(task_id)
        if descriptor is None:
            _logger.warning("handle_failure: unknown task_id=%s", task_id)
            return RestartDecision(
                task_id=task_id,
                action="abort",
                next_attempt_seq=None,
                reason="task_id not found in registry",
            )

        self._registry.complete_attempt(task_id, failed_run_id, "failed")
        attempts = self._registry.get_attempts(task_id)
        attempt_seq = len(attempts)
        policy = descriptor.restart_policy

        decision = self._decide(policy, attempt_seq, failure)

        if decision.action == "retry":
            self._registry.update_state(task_id, "restarting")
            await self._launch_retry(descriptor, attempt_seq + 1)
        elif decision.action == "reflect":
            self._registry.update_state(task_id, "reflecting")
        elif decision.action == "escalate":
            self._registry.update_state(task_id, "escalated")
        else:
            self._registry.update_state(task_id, "aborted")

        _logger.info(
            "task.restart_decision task_id=%s action=%s attempt_seq=%d/%d",
            task_id,
            decision.action,
            attempt_seq,
            policy.max_attempts,
        )
        return RestartDecision(
            task_id=task_id,
            action=decision.action,
            next_attempt_seq=decision.next_attempt_seq,
            reason=decision.reason,
        )

    def _decide(
        self,
        policy: Any,
        attempt_seq: int,
        failure: Any | None,
    ) -> RestartDecision:
        """Pure decision logic — no side effects.

        Args:
            policy: TaskRestartPolicy.
            attempt_seq: Number of attempts already made.
            failure: Optional FailureEnvelope.

        Returns:
            RestartDecision with action and reason.
        """
        # Check if failure is non-retryable
        retryability = getattr(failure, "retryability", "unknown") if failure else "unknown"
        if retryability == "non_retryable":
            action: RestartAction = policy.on_exhausted  # type: ignore[assignment]
            return RestartDecision(
                task_id="",
                action=action,
                next_attempt_seq=None,
                reason=(
                    f"failure marked non_retryable: {getattr(failure, 'failure_code', 'unknown')}"
                ),
            )

        if attempt_seq < policy.max_attempts:
            return RestartDecision(
                task_id="",
                action="retry",
                next_attempt_seq=attempt_seq + 1,
                reason=f"attempt {attempt_seq}/{policy.max_attempts} failed; retrying",
            )

        on_exhausted = policy.on_exhausted
        return RestartDecision(
            task_id="",
            action=on_exhausted,  # type: ignore[arg-type]
            next_attempt_seq=None,
            reason=(
                f"retry budget exhausted ({attempt_seq}/{policy.max_attempts}); "
                f"on_exhausted={on_exhausted}"
            ),
        )

    async def _launch_retry(self, descriptor: Any, next_seq: int) -> None:
        """Launch a new Run attempt for this task.

        Args:
            descriptor: TaskDescriptor of the task.
            next_seq: Attempt sequence number for the new attempt.
        """
        import datetime

        try:
            from agent_kernel.kernel.contracts import StartRunRequest
        except ImportError:
            _logger.error("task_manager: cannot import StartRunRequest; retry aborted")
            return

        new_run_id = f"task-retry-{descriptor.task_id}-{next_seq}-{uuid.uuid4().hex[:8]}"
        try:
            response = await self._facade.start_run(
                StartRunRequest(
                    initiator="system",
                    run_kind="task_retry",
                    session_id=descriptor.session_id,
                )
            )
            actual_run_id = response.run_id if hasattr(response, "run_id") else new_run_id
        except Exception as exc:
            _logger.error(
                "task_manager: retry launch failed task_id=%s seq=%d error=%s",
                descriptor.task_id,
                next_seq,
                exc,
            )
            self._registry.update_state(descriptor.task_id, "aborted")
            return

        from agent_kernel.kernel.task_manager.contracts import TaskAttempt

        attempt = TaskAttempt(
            attempt_id=uuid.uuid4().hex,
            task_id=descriptor.task_id,
            run_id=actual_run_id,
            attempt_seq=next_seq,
            started_at=datetime.datetime.now(datetime.UTC).isoformat(),
        )
        self._registry.start_attempt(attempt)
        # start_attempt transitions state to "running"; re-assert "restarting"
        # so callers that inspect state immediately after handle_failure() see
        # the correct intermediate state.
        self._registry.update_state(descriptor.task_id, "restarting")
        _logger.info(
            "task.attempt_started task_id=%s seq=%d run_id=%s",
            descriptor.task_id,
            next_seq,
            actual_run_id,
        )
