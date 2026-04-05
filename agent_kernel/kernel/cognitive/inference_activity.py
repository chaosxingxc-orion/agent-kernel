"""Inference activity logic for the cognitive layer.

Provides ``execute_inference`` 鈥?the pure business logic that would run
inside a Temporal Activity.  This module intentionally contains no Temporal
SDK imports so that the logic can be unit-tested without a running Temporal
cluster.

The Temporal Activity wrapper (registered in the worker) delegates to this
function after dependency injection.

Responsibility split:
  - Temporal Activity  鈫?durability, retry, heartbeat, serialization.
  - ``execute_inference`` 鈫?provider abstraction, token budget, output.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_kernel.kernel.contracts import (
        InferenceActivityInput,
        LLMGateway,
        ModelOutput,
    )

_LOG = logging.getLogger(__name__)


async def execute_inference(
    input_value: InferenceActivityInput,
    gateway: LLMGateway,
) -> ModelOutput:
    """Execute one LLM inference call using the provided gateway.

    This function contains the authoritative inference business logic.
    It is intentionally free of Temporal SDK imports and can be
    exercised directly in unit tests.

    Token budget enforcement: if the assembled context window exceeds
    ``config.token_budget.max_input``, a warning is logged.  The call
    proceeds 鈥?budget enforcement beyond logging is the responsibility
    of the ``ContextPort`` that assembled the window.

    Args:
        input_value: Inference activity input containing the run/turn
            identifiers, assembled context window, inference config, and
            a stable idempotency key.
        gateway: ``LLMGateway`` implementation to delegate the provider call to.

    Returns:
        Normalised ``ModelOutput`` from the provider.

    Raises:
        Exception: Any exception raised by the gateway propagates unchanged
            so the Temporal Activity retry policy can classify it.

    """
    _LOG.debug(
        "execute_inference: run_id=%s turn_id=%s model_ref=%s idempotency_key=%s",
        input_value.run_id,
        input_value.turn_id,
        input_value.config.model_ref,
        input_value.idempotency_key,
    )

    estimated_tokens = await gateway.count_tokens(
        input_value.context_window, input_value.config.model_ref
    )
    max_input = input_value.config.token_budget.max_input
    if estimated_tokens > max_input:
        _LOG.warning(
            "execute_inference: estimated_tokens=%d exceeds max_input=%d for run_id=%s turn_id=%s",
            estimated_tokens,
            max_input,
            input_value.run_id,
            input_value.turn_id,
        )

    output = await gateway.infer(
        context=input_value.context_window,
        config=input_value.config,
        idempotency_key=input_value.idempotency_key,
    )

    _LOG.debug(
        "execute_inference: done run_id=%s turn_id=%s finish_reason=%s "
        "tool_calls=%d output_tokens=%d",
        input_value.run_id,
        input_value.turn_id,
        output.finish_reason,
        len(output.tool_calls),
        output.usage.get("output_tokens", 0),
    )

    return output
