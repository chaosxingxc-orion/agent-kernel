"""ReflectionBridge: builds LLM context from task failure history.

When RestartPolicyEngine decides action="reflect", the caller should invoke
ReflectionBridge.build_context() to construct a structured prompt fragment
describing what was attempted, what failed, and what the model should decide.

The output is passed to ReasoningLoop (or directly to the LLM gateway) so the
model can choose: retry with modified parameters, spawn a different task kind,
or escalate to a human operator.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent_kernel.kernel.task_manager.contracts import TaskAttempt, TaskDescriptor

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReflectionContext:
    """Structured context passed to the model for reflect_and_replace decisions.

    Attributes:
        task_id: The task under reflection.
        goal_description: What the task was supposed to accomplish.
        attempt_count: How many attempts were made.
        failure_summary: Human-readable summary of all failures.
        failure_details: Structured list of per-attempt failure data.
        suggested_actions: Hint strings for the model (retry/replace/escalate).
        prompt_fragment: Ready-to-use prompt text for injection into LLM context.

    """

    task_id: str
    goal_description: str
    attempt_count: int
    failure_summary: str
    failure_details: list[dict[str, Any]]
    suggested_actions: list[str]
    prompt_fragment: str


class ReflectionBridge:
    """Builds ReflectionContext from TaskDescriptor + attempt history.

    Stateless helper — instantiate once and reuse across tasks.
    """

    def build_context(
        self,
        descriptor: TaskDescriptor,
        attempts: list[TaskAttempt],
    ) -> ReflectionContext:
        """Construct a ReflectionContext for a fully-failed task.

        Args:
            descriptor: Task descriptor with goal and policy.
            attempts: All recorded attempts (all should be in failed/cancelled state).

        Returns:
            ReflectionContext ready for injection into ReasoningLoop.

        """
        failure_details = self._extract_failure_details(attempts)
        failure_summary = self._summarize_failures(failure_details)
        suggested_actions = self._suggest_actions(descriptor, attempts)
        prompt_fragment = self._build_prompt(
            descriptor=descriptor,
            attempt_count=len(attempts),
            failure_summary=failure_summary,
            suggested_actions=suggested_actions,
        )
        return ReflectionContext(
            task_id=descriptor.task_id,
            goal_description=descriptor.goal_description,
            attempt_count=len(attempts),
            failure_summary=failure_summary,
            failure_details=failure_details,
            suggested_actions=suggested_actions,
            prompt_fragment=prompt_fragment,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _extract_failure_details(self, attempts: list[TaskAttempt]) -> list[dict[str, Any]]:
        details = []
        for a in attempts:
            entry: dict[str, Any] = {
                "attempt_seq": a.attempt_seq,
                "run_id": a.run_id,
                "outcome": a.outcome or "unknown",
            }
            if a.failure is not None:
                failure = a.failure
                entry.update(
                    {
                        "failed_stage": getattr(failure, "failed_stage", "unknown"),
                        "failure_code": getattr(failure, "failure_code", "unknown"),
                        "failure_class": getattr(failure, "failure_class", "unknown"),
                        "retryability": getattr(failure, "retryability", "unknown"),
                        "local_inference": getattr(failure, "local_inference", None),
                    }
                )
            details.append(entry)
        return details

    def _summarize_failures(self, details: list[dict[str, Any]]) -> str:
        if not details:
            return "No attempts recorded."
        codes = [d.get("failure_code", "unknown") for d in details if d.get("outcome") == "failed"]
        if not codes:
            return f"{len(details)} attempt(s) made, outcomes unclear."
        unique_codes = list(dict.fromkeys(codes))  # stable dedup
        return (
            f"{len(details)} attempt(s) failed. Failure codes observed: {', '.join(unique_codes)}."
        )

    def _suggest_actions(
        self,
        descriptor: TaskDescriptor,
        attempts: list[TaskAttempt],
    ) -> list[str]:
        suggestions = [
            "retry_with_modified_parameters: adjust input or timeout and retry",
            "spawn_alternative_task: replace this task with a different approach",
            "escalate_to_human: request human review and intervention",
        ]
        if descriptor.restart_policy.max_attempts > len(attempts):
            suggestions.insert(0, "force_retry: attempt again with same parameters")
        return suggestions

    def _build_prompt(
        self,
        descriptor: TaskDescriptor,
        attempt_count: int,
        failure_summary: str,
        suggested_actions: list[str],
    ) -> str:
        actions_text = "\n".join(f"  - {a}" for a in suggested_actions)
        return (
            f"Task '{descriptor.task_id}' has failed after {attempt_count} attempt(s).\n"
            f"Goal: {descriptor.goal_description}\n"
            f"Summary: {failure_summary}\n"
            f"Suggested recovery actions:\n{actions_text}\n"
            f"Please decide the best recovery action and provide your reasoning."
        )
