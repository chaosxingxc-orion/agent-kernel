"""Tests for the config-based AnthropicLLMGateway adapter.

Uses mocked Anthropic SDK client — no real API calls are made.
Mocking rationale: unit-test isolation of gateway mapping logic; the Anthropic
SDK is an optional external dependency that may not be installed in CI.
"""

from __future__ import annotations

import pytest

anthropic = pytest.importorskip("anthropic", reason="anthropic SDK not installed")

from unittest.mock import AsyncMock, MagicMock, patch

from agent_kernel.kernel.cognitive.llm_gateway_anthropic import AnthropicLLMGateway
from agent_kernel.kernel.cognitive.llm_gateway_config import LLMGatewayConfig
from agent_kernel.kernel.contracts import (
    ContextWindow,
    InferenceConfig,
    TokenBudget,
    ToolDefinition,
)

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

FAKE_CONFIG = LLMGatewayConfig(
    provider="anthropic",
    model="claude-sonnet-4-6",
    api_key="test-key",
)


def _make_inference_config() -> InferenceConfig:
    return InferenceConfig(
        model_ref="claude-sonnet-4-6",
        token_budget=TokenBudget(max_input=8192, max_output=512),
        temperature=0.0,
    )


def _make_context(tool_names: list[str] | None = None) -> ContextWindow:
    tool_definitions = tuple(
        ToolDefinition(
            name=name,
            description=f"Tool: {name}",
            input_schema={"type": "object", "properties": {}},
        )
        for name in (tool_names or [])
    )
    return ContextWindow(
        system_instructions="You are a test agent.",
        tool_definitions=tool_definitions,
        history=({"role": "user", "content": "hello"},),
    )


def _make_fake_tool_use_response() -> MagicMock:
    """Build a mock Anthropic Message with one tool_use content block."""
    block = MagicMock()
    block.type = "tool_use"
    block.id = "toolu_abc123"
    block.name = "my_tool"
    block.input = {"param": "value"}

    response = MagicMock()
    response.content = [block]
    response.stop_reason = "tool_use"
    response.usage = MagicMock(input_tokens=100, output_tokens=20)
    return response


def _make_fake_text_response() -> MagicMock:
    """Build a mock Anthropic Message with one text content block."""
    block = MagicMock()
    block.type = "text"
    block.text = "Hello from Anthropic!"

    response = MagicMock()
    response.content = [block]
    response.stop_reason = "end_turn"
    response.usage = MagicMock(input_tokens=50, output_tokens=10)
    return response


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAnthropicLLMGatewayInfer:
    """Tests for AnthropicLLMGateway.infer()."""

    @pytest.mark.asyncio
    async def test_infer_returns_model_output_with_tool_call(self) -> None:
        """infer() maps a tool_use response block into ModelOutput.tool_calls."""
        fake_response = _make_fake_tool_use_response()
        context = _make_context(tool_names=["my_tool"])
        inference_cfg = _make_inference_config()

        with patch("anthropic.AsyncAnthropic") as mock_cls:
            mock_client = MagicMock()
            mock_client.messages.create = AsyncMock(return_value=fake_response)
            mock_cls.return_value = mock_client

            gateway = AnthropicLLMGateway(FAKE_CONFIG)
            # Inject the patched client directly
            gateway._client = mock_client

            result = await gateway.infer(context, inference_cfg, idempotency_key="idem-001")

        assert result.finish_reason == "tool_calls"
        assert len(result.tool_calls) == 1
        tc = result.tool_calls[0]
        assert tc["id"] == "toulu_abc123" or tc["id"] == "toolu_abc123"
        assert tc["name"] == "my_tool"
        assert tc["arguments"] == {"param": "value"}
        assert result.token_usage is not None
        assert result.token_usage.input_tokens == 100

    @pytest.mark.asyncio
    async def test_infer_returns_model_output_with_text(self) -> None:
        """infer() maps a text response block into ModelOutput.raw_text."""
        fake_response = _make_fake_text_response()
        context = _make_context()
        inference_cfg = _make_inference_config()

        with patch("anthropic.AsyncAnthropic") as mock_cls:
            mock_client = MagicMock()
            mock_client.messages.create = AsyncMock(return_value=fake_response)
            mock_cls.return_value = mock_client

            gateway = AnthropicLLMGateway(FAKE_CONFIG)
            gateway._client = mock_client

            result = await gateway.infer(context, inference_cfg, idempotency_key="idem-002")

        assert result.finish_reason == "stop"
        assert result.raw_text == "Hello from Anthropic!"
        assert result.tool_calls == []
        assert result.token_usage is not None
        assert result.token_usage.output_tokens == 10

    @pytest.mark.asyncio
    async def test_infer_raises_on_sdk_error(self) -> None:
        """infer() propagates SDK errors without silencing them."""
        context = _make_context()
        inference_cfg = _make_inference_config()

        with patch("anthropic.AsyncAnthropic") as mock_cls:
            mock_client = MagicMock()
            mock_client.messages.create = AsyncMock(
                side_effect=Exception("Simulated provider failure")
            )
            mock_cls.return_value = mock_client

            gateway = AnthropicLLMGateway(FAKE_CONFIG)
            gateway._client = mock_client

            with pytest.raises(Exception, match="Simulated provider failure"):
                await gateway.infer(context, inference_cfg, idempotency_key="idem-003")


class TestAnthropicLLMGatewayCountTokens:
    """Tests for AnthropicLLMGateway.count_tokens()."""

    @pytest.mark.asyncio
    async def test_count_tokens_returns_int_from_api(self) -> None:
        """count_tokens() returns the input_tokens value from the API response."""
        context = _make_context()

        count_response = MagicMock()
        count_response.input_tokens = 42

        with patch("anthropic.AsyncAnthropic") as mock_cls:
            mock_client = MagicMock()
            mock_client.messages.count_tokens = AsyncMock(return_value=count_response)
            mock_cls.return_value = mock_client

            gateway = AnthropicLLMGateway(FAKE_CONFIG)
            gateway._client = mock_client

            result = await gateway.count_tokens(context, model_ref="claude-sonnet-4-6")

        assert isinstance(result, int)
        assert result == 42

    @pytest.mark.asyncio
    async def test_count_tokens_falls_back_to_heuristic_on_error(self) -> None:
        """count_tokens() falls back to character heuristic when API raises."""
        context = _make_context()

        with patch("anthropic.AsyncAnthropic") as mock_cls:
            mock_client = MagicMock()
            mock_client.messages.count_tokens = AsyncMock(
                side_effect=Exception("count_tokens API unavailable")
            )
            mock_cls.return_value = mock_client

            gateway = AnthropicLLMGateway(FAKE_CONFIG)
            gateway._client = mock_client

            result = await gateway.count_tokens(context, model_ref="claude-sonnet-4-6")

        assert isinstance(result, int)
        assert result >= 1
