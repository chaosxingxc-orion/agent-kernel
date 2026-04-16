"""LLM Gateway implementations for the cognitive layer.

Provides:
  - ``LLMProviderError`` / ``LLMRateLimitError`` 鈥?error taxonomy.
  - ``BaseLLMGateway`` 鈥?shared base with token-count heuristic + retry helper.
  - ``EchoLLMGateway`` 鈥?deterministic test/PoC gateway; no external calls.
  - ``OpenAILLMGateway`` 鈥?OpenAI API gateway (lazy import; ``openai`` optional).
  - ``AnthropicLLMGateway`` 鈥?Anthropic API gateway (lazy import; ``anthropic`` optional).

Two retry levels must stay independent:
  - Temporal Activity retry  鈫?kernel-level (process crash, timeout).
  - Gateway-internal retry   鈫?provider-level (rate limits, 5xx).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from agent_kernel.kernel.contracts import (
    ContextWindow,
    InferenceConfig,
    ModelOutput,
    TokenUsage,
)

_LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Error taxonomy
# ---------------------------------------------------------------------------


class LLMProviderError(Exception):
    """Raised when a provider returns an unrecoverable error.

    Attributes:
        provider: Provider name (e.g. ``"openai"``, ``"anthropic"``).
        status_code: HTTP status code from the provider response.
        message: Human-readable error message.

    """

    def __init__(self, provider: str, status_code: int, message: str) -> None:
        """Initialize the instance with configured dependencies."""
        super().__init__(f"[{provider}] HTTP {status_code}: {message}")
        self.provider = provider
        self.status_code = status_code
        self.message = message


class LLMRateLimitError(LLMProviderError):
    """Raised for provider rate-limit (HTTP 429) responses.

    Subclasses ``LLMProviderError`` so callers can catch either the
    specific rate-limit case or any provider error.

    Attributes:
        retry_after_s: Server-requested wait time in seconds from the
            ``Retry-After`` response header, or ``None`` if not present.

    """

    def __init__(
        self,
        provider: str,
        status_code: int,
        message: str,
        retry_after_s: float | None = None,
    ) -> None:
        """Initialize the instance with configured dependencies."""
        super().__init__(provider, status_code, message)
        self.retry_after_s = retry_after_s


def _parse_retry_after(response: Any) -> float | None:
    """Extract the ``Retry-After`` wait time in seconds from an HTTP response.

    Supports both integer-seconds and HTTP-date formats.  Returns ``None``
    when the header is absent or cannot be parsed.

    Args:
        response: An HTTP response object that may have a ``headers`` mapping.

    Returns:
        Wait time in seconds, or ``None``.

    """
    if response is None:
        return None
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except TypeError, ValueError:
        return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_MAX_RETRIES = 3
_RETRY_BASE_DELAY_S = 1.0


async def _with_rate_limit_retry(
    coro_factory: Any,
    provider: str,
) -> Any:
    """Retries a coroutine factory up to ``_MAX_RETRIES`` times on 429.

    Uses the ``Retry-After`` header when present; falls back to exponential
    back-off (delay doubles after each attempt).

    Args:
        coro_factory: Zero-argument async callable that returns the result.
        provider: Provider name used in error messages.

    Returns:
        The successful result from ``coro_factory``.

    Raises:
        LLMRateLimitError: When all retries are exhausted and the last
            attempt still returns a rate-limit response.
        LLMProviderError: For non-retryable provider errors.

    """
    backoff = _RETRY_BASE_DELAY_S
    last_exc: LLMRateLimitError | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            return await coro_factory()
        except LLMRateLimitError as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES - 1:
                delay = exc.retry_after_s if exc.retry_after_s is not None else backoff
                _LOG.warning(
                    "Rate limit from %s (attempt %d/%d); retrying in %.1fs",
                    provider,
                    attempt + 1,
                    _MAX_RETRIES,
                    delay,
                )
                await asyncio.sleep(delay)
                backoff *= 2
    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# BaseLLMGateway 鈥?shared token heuristic + retry helper
# ---------------------------------------------------------------------------


class BaseLLMGateway:
    """Abstract base providing shared behaviour for all LLM gateway implementations.

    Subclasses must implement ``infer()``.  ``count_tokens()`` is provided here
    using a 4-chars-per-token heuristic that is accurate enough for budget
    enforcement without a full tokeniser round-trip.

    The ``_call_with_retry()`` helper wraps ``_with_rate_limit_retry()`` so
    subclasses don't have to reference the module-level function directly.
    """

    async def count_tokens(
        self,
        context: ContextWindow,
        model_ref: str,
    ) -> int:
        """Estimates token count using a character-based heuristic.

        Args:
            context: Context window to estimate.
            model_ref: Model identifier (reserved for future tiktoken integration).

        Returns:
            Estimated total token count as an integer.

        """
        total_chars = len(context.system_instructions)
        for msg in context.history:
            total_chars += sum(len(str(v)) for v in msg.values())
        return max(1, total_chars // 4)

    async def _call_with_retry(self, coro_factory: Any, provider: str) -> Any:
        """Delegate to ``_with_rate_limit_retry`` for provider-level retry.

        Args:
            coro_factory: Zero-argument async callable that performs the API call.
            provider: Provider name used in log/error messages.

        Returns:
            The successful result from *coro_factory*.

        Raises:
            LLMRateLimitError: When all retries are exhausted.
            LLMProviderError: For non-retryable provider errors.

        """
        return await _with_rate_limit_retry(coro_factory, provider)


# ---------------------------------------------------------------------------
# EchoLLMGateway
# ---------------------------------------------------------------------------


class EchoLLMGateway:
    """Test/PoC gateway that echoes inputs without calling any provider.

    Returns a ``ModelOutput`` whose tool_calls are derived from the first
    tool definition in the context window.  Designed for deterministic unit
    tests that must not make network calls.
    """

    async def infer(
        self,
        context: ContextWindow,
        config: InferenceConfig,
        idempotency_key: str,
    ) -> ModelOutput:
        """Produce a deterministic echo response from the context window.

        Args:
            context: Assembled context window.
            config: Inference configuration (used to estimate input tokens).
            idempotency_key: Stable dedup key (not used by echo gateway).

        Returns:
            ``ModelOutput`` echoing the first tool definition if tools are
            present, otherwise a plain ``"stop"`` response.

        """
        estimated_input_tokens = self._estimate_tokens(context)

        echo_token_usage = TokenUsage(input_tokens=estimated_input_tokens, output_tokens=10)
        if context.tool_definitions:
            first_tool = context.tool_definitions[0]
            tool_calls = [
                {
                    "id": f"echo-{idempotency_key[:8]}",
                    "name": first_tool.name,
                    "arguments": {},
                }
            ]
            return ModelOutput(
                raw_text="",
                tool_calls=tool_calls,
                finish_reason="tool_calls",
                usage={"input_tokens": estimated_input_tokens, "output_tokens": 10},
                token_usage=echo_token_usage,
            )

        return ModelOutput(
            raw_text=f"echo:{idempotency_key}",
            tool_calls=[],
            finish_reason="stop",
            usage={"input_tokens": estimated_input_tokens, "output_tokens": 10},
            token_usage=echo_token_usage,
        )

    async def count_tokens(
        self,
        context: ContextWindow,
        model_ref: str,
    ) -> int:
        """Estimates token count without calling a provider.

        Args:
            context: Context window to estimate.
            model_ref: Model identifier (ignored by echo gateway).

        Returns:
            Estimated total token count based on instruction length.

        """
        return self._estimate_tokens(context)

    @staticmethod
    def _estimate_tokens(context: ContextWindow) -> int:
        """Estimates input token count from context window string lengths.

        Uses a rough 4-characters-per-token heuristic suitable for tests.

        Args:
            context: Context window to estimate.

        Returns:
            Estimated token count as an integer.

        """
        total_chars = len(context.system_instructions)
        for msg in context.history:
            total_chars += sum(len(str(v)) for v in msg.values())
        return max(1, total_chars // 4)


# ---------------------------------------------------------------------------
# OpenAILLMGateway
# ---------------------------------------------------------------------------


class OpenAILLMGateway(BaseLLMGateway):
    """OpenAI API gateway with provider-level rate-limit retry.

    Requires the ``openai`` package.  Raises ``ImportError`` at construction
    time when the package is not installed.

    Rate-limit retry (HTTP 429) is handled internally with exponential
    back-off up to ``_MAX_RETRIES`` attempts.  Temporal Activity-level retry
    is handled externally and must not be merged with this layer.

    Attributes:
        model_ref: Default model identifier override.  When ``None`` the
            model is taken from ``InferenceConfig.model_ref``.

    """

    def __init__(self, api_key: str, model_ref: str | None = None) -> None:
        """Initialise the OpenAI gateway.

        Args:
            api_key: OpenAI API key.
            model_ref: Optional default model identifier.

        Raises:
            ImportError: When the ``openai`` package is not installed.

        """
        try:
            import openai
        except ImportError as exc:
            raise ImportError(
                "openai package is required for OpenAILLMGateway. "
                "Install it with: pip install openai"
            ) from exc

        self._client = openai.AsyncOpenAI(api_key=api_key)
        self._model_ref = model_ref

    async def infer(
        self,
        context: ContextWindow,
        config: InferenceConfig,
        idempotency_key: str,
    ) -> ModelOutput:
        """Run one inference call against the OpenAI API.

        Args:
            context: Assembled context window.
            config: Inference configuration.
            idempotency_key: Stable dedup key passed as a request ID header.

        Returns:
            Normalised ``ModelOutput``.

        Raises:
            LLMProviderError: For unrecoverable provider errors.
            LLMRateLimitError: When rate limit persists after all retries.

        """
        model = self._model_ref or config.model_ref
        messages = self._build_messages(context)
        tools = self._build_tools(context)

        async def _call() -> Any:
            """Calls the backend and returns raw model output."""
            try:
                import openai

                kwargs: dict[str, Any] = {
                    "model": model,
                    "messages": messages,
                    "max_tokens": config.token_budget.max_output,
                    "temperature": config.temperature,
                }
                if tools:
                    kwargs["tools"] = tools
                if config.stop_sequences:
                    kwargs["stop"] = list(config.stop_sequences)

                return await self._client.chat.completions.create(**kwargs)
            except openai.RateLimitError as exc:
                retry_after = _parse_retry_after(getattr(exc, "response", None))
                raise LLMRateLimitError("openai", 429, str(exc), retry_after_s=retry_after) from exc
            except openai.APIStatusError as exc:
                raise LLMProviderError("openai", exc.status_code, str(exc)) from exc

        _start_ns = time.monotonic_ns()
        response = await self._call_with_retry(_call, "openai")
        _latency_ms = (time.monotonic_ns() - _start_ns) // 1_000_000
        return self._normalise_response(response, latency_ms=_latency_ms)

    @staticmethod
    def _build_messages(context: ContextWindow) -> list[dict[str, Any]]:
        """Convert the context window into OpenAI chat message format.

        Args:
            context: Assembled context window.

        Returns:
            List of message dicts in OpenAI chat format.

        """
        messages: list[dict[str, Any]] = []
        if context.system_instructions:
            messages.append({"role": "system", "content": context.system_instructions})
        messages.extend({"role": "user", "content": str(m)} for m in context.history)
        return messages

    @staticmethod
    def _build_tools(context: ContextWindow) -> list[dict[str, Any]]:
        """Convert tool definitions to OpenAI tools format.

        Args:
            context: Assembled context window.

        Returns:
            List of tool dicts in OpenAI function-calling format.

        """
        return [
            {
                "type": "function",
                "function": {
                    "name": td.name,
                    "description": td.description,
                    "parameters": td.input_schema,
                },
            }
            for td in context.tool_definitions
        ]

    @staticmethod
    def _normalise_response(response: Any, latency_ms: int = 0) -> ModelOutput:
        """Normalises an OpenAI chat completion response to ``ModelOutput``.

        Args:
            response: Raw OpenAI ``ChatCompletion`` object.
            latency_ms: Wall-clock latency of the inference call in milliseconds.

        Returns:
            Normalised ``ModelOutput``.

        """
        import json

        choice = response.choices[0]
        message = choice.message

        raw_text = message.content or ""
        finish_reason_raw = choice.finish_reason or "stop"
        finish_reason: Any = (
            finish_reason_raw if finish_reason_raw in ("stop", "tool_calls", "length") else "stop"
        )

        tool_calls: list[dict[str, Any]] = []
        if message.tool_calls:
            for tc in message.tool_calls:
                try:
                    arguments = json.loads(tc.function.arguments)
                except json.JSONDecodeError, TypeError:
                    arguments = {}
                tool_calls.append({"id": tc.id, "name": tc.function.name, "arguments": arguments})

        usage: dict[str, int] = {}
        token_usage: TokenUsage | None = None
        if response.usage:
            usage = {
                "input_tokens": response.usage.prompt_tokens,
                "output_tokens": response.usage.completion_tokens,
            }
            token_usage = TokenUsage(
                input_tokens=response.usage.prompt_tokens,
                output_tokens=response.usage.completion_tokens,
            )

        return ModelOutput(
            raw_text=raw_text,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
            latency_ms=latency_ms,
            token_usage=token_usage,
        )


# ---------------------------------------------------------------------------
# AnthropicLLMGateway
# ---------------------------------------------------------------------------


class AnthropicLLMGateway(BaseLLMGateway):
    """Anthropic API gateway with provider-level rate-limit retry.

    Requires the ``anthropic`` package.  Raises ``ImportError`` at
    construction time when the package is not installed.

    Rate-limit retry (HTTP 429) is handled internally with exponential
    back-off up to ``_MAX_RETRIES`` attempts.  Temporal Activity-level retry
    is handled externally and must not be merged with this layer.

    Attributes:
        model_ref: Default model identifier override.  When ``None`` the
            model is taken from ``InferenceConfig.model_ref``.

    """

    def __init__(self, api_key: str, model_ref: str | None = None) -> None:
        """Initialise the Anthropic gateway.

        Args:
            api_key: Anthropic API key.
            model_ref: Optional default model identifier.

        Raises:
            ImportError: When the ``anthropic`` package is not installed.

        """
        try:
            import anthropic
        except ImportError as exc:
            raise ImportError(
                "anthropic package is required for AnthropicLLMGateway. "
                "Install it with: pip install anthropic"
            ) from exc

        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._model_ref = model_ref

    async def infer(
        self,
        context: ContextWindow,
        config: InferenceConfig,
        idempotency_key: str,
    ) -> ModelOutput:
        """Run one inference call against the Anthropic Messages API.

        Args:
            context: Assembled context window.
            config: Inference configuration.
            idempotency_key: Stable dedup key (logged for observability).

        Returns:
            Normalised ``ModelOutput``.

        Raises:
            LLMProviderError: For unrecoverable provider errors.
            LLMRateLimitError: When rate limit persists after all retries.

        """
        model = self._model_ref or config.model_ref
        messages = self._build_messages(context)
        tools = self._build_tools(context)

        async def _call() -> Any:
            """Calls the backend and returns raw model output."""
            try:
                import anthropic

                kwargs: dict[str, Any] = {
                    "model": model,
                    "messages": messages,
                    "max_tokens": config.token_budget.max_output,
                }
                if context.system_instructions:
                    kwargs["system"] = context.system_instructions
                if tools:
                    kwargs["tools"] = tools
                if config.temperature != 0.0:
                    kwargs["temperature"] = config.temperature

                return await self._client.messages.create(**kwargs)
            except anthropic.RateLimitError as exc:
                retry_after = _parse_retry_after(getattr(exc, "response", None))
                raise LLMRateLimitError(
                    "anthropic", 429, str(exc), retry_after_s=retry_after
                ) from exc
            except anthropic.APIStatusError as exc:
                raise LLMProviderError("anthropic", exc.status_code, str(exc)) from exc

        _start_ns = time.monotonic_ns()
        response = await self._call_with_retry(_call, "anthropic")
        _latency_ms = (time.monotonic_ns() - _start_ns) // 1_000_000
        return self._normalise_response(response, latency_ms=_latency_ms)

    @staticmethod
    def _build_messages(context: ContextWindow) -> list[dict[str, Any]]:
        """Convert the context window into Anthropic Messages API format.

        Anthropic requires alternating user/assistant roles; this PoC
        wraps all history entries as user messages for simplicity.

        Args:
            context: Assembled context window.

        Returns:
            List of message dicts in Anthropic Messages format.

        """
        if not context.history:
            return [{"role": "user", "content": "Begin."}]
        return [{"role": "user", "content": str(m)} for m in context.history]

    @staticmethod
    def _build_tools(context: ContextWindow) -> list[dict[str, Any]]:
        """Convert tool definitions to Anthropic tool format.

        Args:
            context: Assembled context window.

        Returns:
            List of tool dicts in Anthropic tool-use format.

        """
        return [
            {
                "name": td.name,
                "description": td.description,
                "input_schema": td.input_schema,
            }
            for td in context.tool_definitions
        ]

    @staticmethod
    def _normalise_response(response: Any, latency_ms: int = 0) -> ModelOutput:
        """Normalises an Anthropic Messages response to ``ModelOutput``.

        Args:
            response: Raw Anthropic ``Message`` object.
            latency_ms: Wall-clock latency of the inference call in milliseconds.

        Returns:
            Normalised ``ModelOutput``.

        """
        raw_text = ""
        tool_calls: list[dict[str, Any]] = []

        for block in response.content:
            if block.type == "text":
                raw_text += block.text
            elif block.type == "tool_use":
                tool_calls.append(
                    {
                        "id": block.id,
                        "name": block.name,
                        "arguments": block.input if isinstance(block.input, dict) else {},
                    }
                )

        stop_reason = getattr(response, "stop_reason", "end_turn")
        if stop_reason == "tool_use":
            finish_reason: Any = "tool_calls"
        elif stop_reason == "max_tokens":
            finish_reason = "length"
        else:
            finish_reason = "stop"

        usage: dict[str, int] = {}
        token_usage: TokenUsage | None = None
        if hasattr(response, "usage") and response.usage:
            usage = {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            }
            token_usage = TokenUsage(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )

        return ModelOutput(
            raw_text=raw_text,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
            latency_ms=latency_ms,
            token_usage=token_usage,
        )
