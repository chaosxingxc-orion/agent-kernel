"""LLM gateway configuration and factory."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from agent_kernel.kernel.cognitive.llm_gateway import AnthropicLLMGateway, OpenAILLMGateway

    LLMGateway = AnthropicLLMGateway | OpenAILLMGateway


@dataclass(frozen=True, slots=True)
class LLMGatewayConfig:
    """Configuration for an LLM gateway instance.

    Attributes:
        provider: LLM provider name; must be ``"anthropic"`` or ``"openai"``.
        model: Model identifier string passed to the provider API.
        api_key: Provider API key used for authentication.
        max_tokens: Maximum output token budget (default 4096).
        timeout_s: Per-request timeout in seconds (default 60.0).
        temperature: Sampling temperature (default 0.0 for deterministic output).

    """

    provider: Literal["anthropic", "openai"]
    model: str
    api_key: str
    max_tokens: int = 4096
    timeout_s: float = 60.0
    temperature: float = 0.0

    @classmethod
    def from_env(cls) -> LLMGatewayConfig:
        """Build configuration from environment variables.

        Required env vars:
            AGENT_KERNEL_LLM_PROVIDER: ``"anthropic"`` or ``"openai"``.
            AGENT_KERNEL_LLM_MODEL: Model name (e.g. ``"claude-sonnet-4-6"``).

        Optional env vars:
            AGENT_KERNEL_LLM_API_KEY: API key (priority over provider-specific vars).
            ANTHROPIC_API_KEY: Fallback when provider is ``"anthropic"``.
            OPENAI_API_KEY: Fallback when provider is ``"openai"``.
            AGENT_KERNEL_LLM_MAX_TOKENS: int, default 4096.
            AGENT_KERNEL_LLM_TIMEOUT_S: float, default 60.0.
            AGENT_KERNEL_LLM_TEMPERATURE: float, default 0.0.

        Returns:
            Fully populated ``LLMGatewayConfig`` instance.

        Raises:
            KeyError: When ``AGENT_KERNEL_LLM_PROVIDER`` or
                ``AGENT_KERNEL_LLM_MODEL`` are not set.

        """
        provider = os.environ["AGENT_KERNEL_LLM_PROVIDER"]
        model = os.environ["AGENT_KERNEL_LLM_MODEL"]
        api_key = os.environ.get("AGENT_KERNEL_LLM_API_KEY") or (
            os.environ.get("ANTHROPIC_API_KEY")
            if provider == "anthropic"
            else os.environ.get("OPENAI_API_KEY", "")
        )
        return cls(
            provider=provider,  # type: ignore[arg-type]
            model=model,
            api_key=api_key or "",
            max_tokens=int(os.environ.get("AGENT_KERNEL_LLM_MAX_TOKENS", "4096")),
            timeout_s=float(os.environ.get("AGENT_KERNEL_LLM_TIMEOUT_S", "60.0")),
            temperature=float(os.environ.get("AGENT_KERNEL_LLM_TEMPERATURE", "0.0")),
        )


def create_llm_gateway(config: LLMGatewayConfig) -> AnthropicLLMGateway | OpenAILLMGateway:
    """Factory: instantiate the appropriate gateway from *config*.

    Args:
        config: Gateway configuration specifying provider, model, and credentials.

    Returns:
        A concrete gateway instance ready to call ``infer()`` and ``count_tokens()``.

    Raises:
        ValueError: When ``config.provider`` is not a recognised provider name.
        ImportError: When the required provider SDK is not installed.

    """
    if config.provider == "anthropic":
        from agent_kernel.kernel.cognitive.llm_gateway_anthropic import (
            AnthropicLLMGateway as ConfigAnthropicGateway,
        )

        return ConfigAnthropicGateway(config)
    if config.provider == "openai":
        from agent_kernel.kernel.cognitive.llm_gateway_openai import (
            OpenAILLMGateway as ConfigOpenAIGateway,
        )

        return ConfigOpenAIGateway(config)
    raise ValueError(f"Unknown LLM provider: {config.provider!r}")
