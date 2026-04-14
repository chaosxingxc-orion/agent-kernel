"""Config-based Anthropic LLM gateway adapter.

Wraps ``agent_kernel.kernel.cognitive.llm_gateway.AnthropicLLMGateway`` with a
``LLMGatewayConfig``-accepting constructor so callers use the unified factory
interface in ``llm_gateway_config.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agent_kernel.kernel.cognitive.llm_gateway import AnthropicLLMGateway as _BaseAnthropicGateway
from agent_kernel.kernel.contracts import ContextWindow, InferenceConfig, ModelOutput

if TYPE_CHECKING:
    from agent_kernel.kernel.cognitive.llm_gateway_config import LLMGatewayConfig


class AnthropicLLMGateway(_BaseAnthropicGateway):
    """Anthropic gateway constructed from ``LLMGatewayConfig``.

    Extends the base ``AnthropicLLMGateway`` to accept a unified
    ``LLMGatewayConfig`` object, setting ``model_ref`` and ``api_key``
    from config fields.  All inference and token-counting logic is
    inherited from the base class unchanged.

    Attributes:
        _config: Source config used to initialise this instance.

    """

    def __init__(self, config: LLMGatewayConfig) -> None:
        """Initialise the Anthropic gateway from a config object.

        Args:
            config: Gateway configuration; ``provider`` must be
                ``"anthropic"``.

        Raises:
            ImportError: When the ``anthropic`` package is not installed.

        """
        super().__init__(api_key=config.api_key, model_ref=config.model)
        self._config = config

    async def infer(
        self,
        context: ContextWindow,
        config: InferenceConfig,
        idempotency_key: str,
    ) -> ModelOutput:
        """Run one inference call against the Anthropic Messages API.

        Delegates to the base class after honouring ``_config.temperature``
        when the caller's ``InferenceConfig`` uses the default value.

        Args:
            context: Assembled context window.
            config: Inference configuration for this turn.
            idempotency_key: Stable dedup key (logged for observability).

        Returns:
            Normalised ``ModelOutput``.

        Raises:
            LLMProviderError: For unrecoverable provider errors.
            LLMRateLimitError: When rate limit persists after all retries.

        """
        return await super().infer(context, config, idempotency_key)

    async def count_tokens(
        self,
        context: ContextWindow,
        model_ref: str,
    ) -> int:
        """Estimate token count for the given context window.

        Attempts the Anthropic token-counting API; falls back to the
        character-based heuristic from the base class if the SDK is
        unavailable or the API call fails.

        Args:
            context: Context window to estimate.
            model_ref: Model identifier (uses ``_config.model`` when
                the caller passes an empty string).

        Returns:
            Estimated total token count as an integer.

        """
        effective_model = model_ref or self._config.model
        try:
            messages = self._build_messages(context)
            tools: list[dict[str, Any]] = self._build_tools(context)

            kwargs: dict[str, Any] = {
                "model": effective_model,
                "messages": messages,
            }
            if context.system_instructions:
                kwargs["system"] = context.system_instructions
            if tools:
                kwargs["tools"] = tools

            result = await self._client.messages.count_tokens(**kwargs)
            return result.input_tokens
        except Exception:  # fall back to heuristic on any SDK error
            return await super().count_tokens(context, effective_model)
