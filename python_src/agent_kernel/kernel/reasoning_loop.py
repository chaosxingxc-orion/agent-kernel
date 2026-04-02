"""ReasoningLoop: orchestrates one ContextPort -> LLMGateway -> OutputParser cycle.

This is NOT an authority. It assembles context, calls the model, and translates
output into Actions. The TurnEngine remains the sole execution authority.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent_kernel.kernel.capability_snapshot import CapabilitySnapshot

from agent_kernel.kernel.contracts import (
    Action,
    ContextWindow,
    InferenceConfig,
    ModelOutput,
    RuntimeEvent,
)


@dataclass(frozen=True, slots=True)
class ReasoningResult:
    """Holds all intermediate values produced by one ReasoningLoop cycle.

    Attributes:
        actions: Parsed actions ready for TurnEngine dispatch.
        model_output: Raw normalised model output from the LLM gateway.
        context_window: Assembled context window passed to the model.
        inference_config: Inference configuration used for this cycle.
    """

    actions: list[Action]
    model_output: ModelOutput
    context_window: ContextWindow
    inference_config: InferenceConfig


class ReasoningLoop:
    """Orchestrates one ContextPort -> LLMGateway -> OutputParser cycle.

    This class is not an authority. It assembles context, calls the model, and
    translates output into Actions. The TurnEngine remains the sole execution
    authority.

    Args:
        context_port: Protocol implementation that assembles the context window.
        llm_gateway: Protocol implementation that runs model inference.
        output_parser: Protocol implementation that parses model output into
            Actions.
    """

    def __init__(
        self,
        context_port: Any,
        llm_gateway: Any,
        output_parser: Any,
    ) -> None:
        self._context_port = context_port
        self._llm_gateway = llm_gateway
        self._output_parser = output_parser

    async def run_once(
        self,
        run_id: str,
        snapshot: CapabilitySnapshot,
        history: list[RuntimeEvent],
        inference_config: InferenceConfig,
        recovery_context: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> ReasoningResult:
        """Assembles context, calls model, parses output into Actions.

        Steps:
        1. ``context_port.assemble(run_id, snapshot, history, inference_config,
           recovery_context)``
        2. ``llm_gateway.infer(context, inference_config, idempotency_key)``
        3. ``output_parser.parse(output, run_id)`` → list[Action]

        Args:
            run_id: Kernel run identifier for this reasoning turn.
            snapshot: Frozen capability snapshot for context assembly.
            history: Ordered event history for conversation reconstruction.
            inference_config: Inference configuration for this cycle.
            recovery_context: Optional structured recovery context for
                reflect_and_retry turns.
            idempotency_key: Optional stable dedup key for the inference call.
                When ``None``, a fresh UUID hex string is generated.

        Returns:
            ReasoningResult containing actions, model output, context window,
            and the inference config used.
        """
        resolved_key = idempotency_key or uuid.uuid4().hex

        context_window: ContextWindow = await self._context_port.assemble(
            run_id,
            snapshot,
            history,
            inference_config,
            recovery_context,
        )

        model_output: ModelOutput = await self._llm_gateway.infer(
            context_window,
            inference_config,
            resolved_key,
        )

        actions: list[Action] = self._output_parser.parse(model_output, run_id)

        return ReasoningResult(
            actions=actions,
            model_output=model_output,
            context_window=context_window,
            inference_config=inference_config,
        )
