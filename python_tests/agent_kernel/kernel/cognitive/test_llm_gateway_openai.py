"""Tests for the config-based OpenAILLMGateway adapter.

Uses mocked OpenAI SDK client — no real API calls are made.
Mocking rationale: unit-test isolation of gateway mapping logic; the OpenAI
SDK is an optional external dependency that may not be installed in CI.
"""

from __future__ import annotations

import pytest

openai = pytest.importorskip("openai", reason="openai SDK not installed")

from unittest.mock import AsyncMock, MagicMock, patch

from agent_kernel.kernel.cognitive.llm_gateway_config import LLMGatewayConfig
from agent_kernel.kernel.cognitive.llm_gateway_openai import OpenAILLMGateway
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
    provider="openai",
    model="gpt-4o",
    api_key="test-key",
)


def _make_inference_config() -> InferenceConfig:
    return InferenceConfig(
        model_ref="gpt-4o",
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


def _make_fake_tool_call_response() -> MagicMock:
    """Build a mock OpenAI ChatCompletion with one tool_call."""
    tool_call = MagicMock()
    tool_call.id = "call_xyz789"
    tool_call.function = MagicMock()
    tool_call.function.name = "my_tool"
    tool_call.function.arguments = '{"param": "value"}'

    message = MagicMock()
    message.content = None
    message.tool_calls = [tool_call]

    choice = MagicMock()
    choice.message = message
    choice.finish_reason = "tool_calls"

    response = MagicMock()
    response.choices = [choice]
    response.usage = MagicMock(prompt_tokens=100, completion_tokens=20)
    return response


def _make_fake_text_response() -> MagicMock:
    """Build a mock OpenAI ChatCompletion with plain text content."""
    message = MagicMock()
    message.content = "Hello from OpenAI!"
    message.tool_calls = None

    choice = MagicMock()
    choice.message = message
    choice.finish_reason = "stop"

    response = MagicMock()
    response.choices = [choice]
    response.usage = MagicMock(prompt_tokens=50, completion_tokens=10)
    return response


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestOpenAILLMGatewayInfer:
    """Tests for OpenAILLMGateway.infer()."""

    @pytest.mark.asyncio
    async def test_infer_returns_model_output_with_tool_call(self) -> None:
        """infer() maps tool_calls on the completion message into ModelOutput.tool_calls."""
        fake_response = _make_fake_tool_call_response()
        context = _make_context(tool_names=["my_tool"])
        inference_cfg = _make_inference_config()

        with patch("openai.AsyncOpenAI") as mock_cls:
            mock_client = MagicMock()
            mock_client.chat = MagicMock()
            mock_client.chat.completions = MagicMock()
            mock_client.chat.completions.create = AsyncMock(return_value=fake_response)
            mock_cls.return_value = mock_client

            gateway = OpenAILLMGateway(FAKE_CONFIG)
            gateway._client = mock_client

            result = await gateway.infer(context, inference_cfg, idempotency_key="idem-001")

        assert result.finish_reason == "tool_calls"
        assert len(result.tool_calls) == 1
        tc = result.tool_calls[0]
        assert tc["id"] == "call_xyz789"
        assert tc["name"] == "my_tool"
        assert tc["arguments"] == {"param": "value"}
        assert result.token_usage is not None
        assert result.token_usage.input_tokens == 100

    @pytest.mark.asyncio
    async def test_infer_returns_model_output_with_text(self) -> None:
        """infer() maps plain text content into ModelOutput.raw_text."""
        fake_response = _make_fake_text_response()
        context = _make_context()
        inference_cfg = _make_inference_config()

        with patch("openai.AsyncOpenAI") as mock_cls:
            mock_client = MagicMock()
            mock_client.chat = MagicMock()
            mock_client.chat.completions = MagicMock()
            mock_client.chat.completions.create = AsyncMock(return_value=fake_response)
            mock_cls.return_value = mock_client

            gateway = OpenAILLMGateway(FAKE_CONFIG)
            gateway._client = mock_client

            result = await gateway.infer(context, inference_cfg, idempotency_key="idem-002")

        assert result.finish_reason == "stop"
        assert result.raw_text == "Hello from OpenAI!"
        assert result.tool_calls == []
        assert result.token_usage is not None
        assert result.token_usage.output_tokens == 10

    @pytest.mark.asyncio
    async def test_infer_raises_on_sdk_error(self) -> None:
        """infer() propagates SDK errors without silencing them."""
        context = _make_context()
        inference_cfg = _make_inference_config()

        with patch("openai.AsyncOpenAI") as mock_cls:
            mock_client = MagicMock()
            mock_client.chat = MagicMock()
            mock_client.chat.completions = MagicMock()
            mock_client.chat.completions.create = AsyncMock(
                side_effect=Exception("Simulated provider failure")
            )
            mock_cls.return_value = mock_client

            gateway = OpenAILLMGateway(FAKE_CONFIG)
            gateway._client = mock_client

            with pytest.raises(Exception, match="Simulated provider failure"):
                await gateway.infer(context, inference_cfg, idempotency_key="idem-003")


class TestOpenAILLMGatewayCountTokens:
    """Tests for OpenAILLMGateway.count_tokens()."""

    @pytest.mark.asyncio
    async def test_count_tokens_returns_int(self) -> None:
        """count_tokens() returns a positive integer regardless of tiktoken availability."""
        context = _make_context()

        with patch("openai.AsyncOpenAI") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client

            gateway = OpenAILLMGateway(FAKE_CONFIG)
            gateway._client = mock_client

            result = await gateway.count_tokens(context, model_ref="gpt-4o")

        assert isinstance(result, int)
        assert result >= 1

    @pytest.mark.asyncio
    async def test_count_tokens_falls_back_to_heuristic_when_tiktoken_absent(self) -> None:
        """count_tokens() uses heuristic when tiktoken raises an ImportError."""
        context = _make_context()

        with patch("openai.AsyncOpenAI") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client

            gateway = OpenAILLMGateway(FAKE_CONFIG)
            gateway._client = mock_client

            # Simulate tiktoken being unavailable by patching the import
            with patch.dict("sys.modules", {"tiktoken": None}):
                result = await gateway.count_tokens(context, model_ref="gpt-4o")

        assert isinstance(result, int)
        assert result >= 1
