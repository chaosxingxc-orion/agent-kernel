"""Config-based OpenAI LLM gateway adapter.

Wraps ``agent_kernel.kernel.cognitive.llm_gateway.OpenAILLMGateway`` with a
``LLMGatewayConfig``-accepting constructor so callers use the unified factory
interface in ``llm_gateway_config.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_kernel.kernel.cognitive.llm_gateway import OpenAILLMGateway as _BaseOpenAIGateway
from agent_kernel.kernel.contracts import ContextWindow, InferenceConfig, ModelOutput

if TYPE_CHECKING:
    from agent_kernel.kernel.cognitive.llm_gateway_config import LLMGatewayConfig


class OpenAILLMGateway(_BaseOpenAIGateway):
    """OpenAI gateway constructed from ``LLMGatewayConfig``.

    Extends the base ``OpenAILLMGateway`` to accept a unified
    ``LLMGatewayConfig`` object, setting ``model_ref`` and ``api_key``
    from config fields.  All inference and token-counting logic is
    inherited from the base class unchanged.

    Attributes:
        _config: Source config used to initialise this instance.

    """

    def __init__(self, config: LLMGatewayConfig) -> None:
        """Initialise the OpenAI gateway from a config object.

        Args:
            config: Gateway configuration; ``provider`` must be
                ``"openai"``.

        Raises:
            ImportError: When the ``openai`` package is not installed.

        """
        super().__init__(api_key=config.api_key, model_ref=config.model)
        self._config = config

    async def infer(
        self,
        context: ContextWindow,
        config: InferenceConfig,
        idempotency_key: str,
    ) -> ModelOutput:
        """Run one inference call against the OpenAI Chat Completions API.

        Delegates to the base class.

        Args:
            context: Assembled context window.
            config: Inference configuration for this turn.
            idempotency_key: Stable dedup key passed as request ID header.

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
        """Estimate token count using tiktoken when available.

        Attempts to use ``tiktoken`` for accurate tokenisation; falls back
        to the character-based heuristic from the base class when tiktoken
        is not installed or encounters an error.

        Args:
            context: Context window to estimate.
            model_ref: Model identifier (uses ``_config.model`` when
                the caller passes an empty string).

        Returns:
            Estimated total token count as an integer.

        """
        effective_model = model_ref or self._config.model
        try:
            import tiktoken

            encoding = tiktoken.encoding_for_model(effective_model)
            total_chars = context.system_instructions
            for msg in context.history:
                total_chars += " ".join(str(v) for v in msg.values())
            return max(1, len(encoding.encode(total_chars)))
        except Exception:  # tiktoken absent or model unknown
            return await super().count_tokens(context, effective_model)
