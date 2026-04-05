"""ReflectionOrchestrator: wires ReflectionBridge output into ReasoningLoop.

When RestartPolicyEngine decides action="reflect", callers use this class to
drive the full reflect-and-retry cycle:

1. ReflectionBridge.build_context(descriptor, attempts) 鈫?ReflectionContext
2. Convert ReflectionContext to a recovery_context dict
3. ReasoningLoop.run_once(recovery_context=...) 鈫?ReasoningResult

ReflectionOrchestrator is a **non-authority** coordinator.  It has no write
access to the event log or run state; it only assembles context and calls the
model.  The result (ReasoningResult) must be passed back to the caller who
owns the run lifecycle.

Typical caller flow::

    orchestrator = ReflectionOrchestrator(bridge=bridge, loop=loop)
    result = await orchestrator.reflect_and_infer(
        descriptor=descriptor,
        attempts=attempts,
        run_id=run_id,
        snapshot=snapshot,
        history=history,
        inference_config=inference_config,
    )
    # result.actions contains model decisions for the caller to dispatch
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent_kernel.kernel.capability_snapshot import CapabilitySnapshot
    from agent_kernel.kernel.contracts import InferenceConfig, RuntimeEvent
    from agent_kernel.kernel.reasoning_loop import ReasoningLoop, ReasoningResult
    from agent_kernel.kernel.task_manager.contracts import TaskAttempt, TaskDescriptor
    from agent_kernel.kernel.task_manager.reflection_bridge import (
        ReflectionBridge,
        ReflectionContext,
    )

_logger = logging.getLogger(__name__)


def reflection_context_to_recovery_dict(ctx: ReflectionContext) -> dict[str, Any]:
    """Convert a ReflectionContext into a recovery_context dict for ReasoningLoop.

    The dict is passed as ``recovery_context`` to ``ReasoningLoop.run_once()``.
    ContextPort implementations can read ``recovery_context["reflection"]`` to
    inject failure history into the context window.

    Args:
        ctx: ReflectionContext produced by ReflectionBridge.

    Returns:
        Dict with a ``"reflection"`` key containing structured failure context
        and a ``"prompt_fragment"`` key with the ready-to-use LLM text.

    """
    return {
        "reflection": {
            "task_id": ctx.task_id,
            "goal_description": ctx.goal_description,
            "attempt_count": ctx.attempt_count,
            "failure_summary": ctx.failure_summary,
            "failure_details": ctx.failure_details,
            "suggested_actions": ctx.suggested_actions,
        },
        "prompt_fragment": ctx.prompt_fragment,
        "recovery_kind": "task_reflection",
    }


class ReflectionOrchestrator:
    """Coordinates ReflectionBridge 鈫?ReasoningLoop for reflect-and-retry cycles.

    Stateless; safe to share across concurrent reflect requests.

    Args:
        bridge: ReflectionBridge used to build the reflection context.
        loop: ReasoningLoop used to run the model inference cycle.

    """

    def __init__(
        self,
        bridge: ReflectionBridge,
        loop: ReasoningLoop,
    ) -> None:
        """Initialize the instance with configured dependencies."""
        self._bridge = bridge
        self._loop = loop

    async def reflect_and_infer(
        self,
        *,
        descriptor: TaskDescriptor,
        attempts: list[TaskAttempt],
        run_id: str,
        snapshot: CapabilitySnapshot,
        history: list[RuntimeEvent],
        inference_config: InferenceConfig,
        idempotency_key: str | None = None,
    ) -> ReasoningResult:
        """Build reflection context and run one ReasoningLoop cycle.

        Steps:
        1. Build ``ReflectionContext`` from descriptor + attempt history.
        2. Convert to ``recovery_context`` dict.
        3. Call ``ReasoningLoop.run_once(recovery_context=...)``.

        Args:
            descriptor: TaskDescriptor of the reflecting task.
            attempts: All recorded attempts (should all be failed/cancelled).
            run_id: Kernel run_id for the reflection turn.
            snapshot: Frozen capability snapshot for context assembly.
            history: Ordered event history for conversation reconstruction.
            inference_config: Inference configuration for this cycle.
            idempotency_key: Optional stable dedup key for the inference call.

        Returns:
            ReasoningResult with model-decided actions.

        """
        ctx: ReflectionContext = self._bridge.build_context(descriptor, attempts)
        recovery_context = reflection_context_to_recovery_dict(ctx)

        _logger.info(
            "task.reflection_inference task_id=%s attempts=%d run_id=%s",
            descriptor.task_id,
            ctx.attempt_count,
            run_id,
        )

        result: ReasoningResult = await self._loop.run_once(
            run_id=run_id,
            snapshot=snapshot,
            history=history,
            inference_config=inference_config,
            recovery_context=recovery_context,
            idempotency_key=idempotency_key,
        )
        return result
