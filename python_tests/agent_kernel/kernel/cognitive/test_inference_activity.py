"""Verifies for execute inference activity logic."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_kernel.kernel.cognitive.inference_activity import execute_inference
from agent_kernel.kernel.cognitive.llm_gateway import EchoLLMGateway
from agent_kernel.kernel.contracts import (
    ContextWindow,
    InferenceActivityInput,
    InferenceConfig,
    ModelOutput,
    TokenBudget,
    ToolDefinition,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(
    model_ref: str = "test-model",
    max_input: int = 8192,
    max_output: int = 512,
) -> InferenceConfig:
    """Builds a minimal InferenceConfig for tests."""
    return InferenceConfig(
        model_ref=model_ref,
        token_budget=TokenBudget(max_input=max_input, max_output=max_output),
    )


def _make_context(
    system_instructions: str = "You are a test agent.",
    tool_names: list[str] | None = None,
) -> ContextWindow:
    """Builds a minimal ContextWindow for tests."""
    tool_definitions = tuple(
        ToolDefinition(
            name=name,
            description=f"Tool: {name}",
            input_schema={"type": "object", "properties": {}},
        )
        for name in (tool_names or [])
    )
    return ContextWindow(
        system_instructions=system_instructions,
        tool_definitions=tool_definitions,
    )


def _make_input(
    run_id: str = "run-1",
    turn_id: str = "turn-1",
    context: ContextWindow | None = None,
    config: InferenceConfig | None = None,
    idempotency_key: str = "idem-001",
) -> InferenceActivityInput:
    """Builds a minimal InferenceActivityInput for tests."""
    return InferenceActivityInput(
        run_id=run_id,
        turn_id=turn_id,
        context_window=context or _make_context(),
        config=config or _make_config(),
        idempotency_key=idempotency_key,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestExecuteInferenceBasic:
    """Basic behaviour tests for execute_inference."""

    def test_returns_model_output(self) -> None:
        """execute_inference should return a ModelOutput."""
        gateway = EchoLLMGateway()
        inp = _make_input()

        result = asyncio.run(execute_inference(inp, gateway))

        assert isinstance(result, ModelOutput)

    def test_delegates_to_gateway_infer(self) -> None:
        """execute_inference should call gateway.infer() exactly once."""
        mock_output = ModelOutput(raw_text="ok", finish_reason="stop")
        mock_gateway = MagicMock()
        mock_gateway.infer = AsyncMock(return_value=mock_output)
        mock_gateway.count_tokens = AsyncMock(return_value=100)

        inp = _make_input()
        result = asyncio.run(execute_inference(inp, mock_gateway))

        mock_gateway.infer.assert_awaited_once()
        assert result is mock_output

    def test_passes_idempotency_key_to_gateway(self) -> None:
        """execute_inference should forward idempotency_key to gateway.infer."""
        mock_output = ModelOutput(raw_text="ok", finish_reason="stop")
        mock_gateway = MagicMock()
        mock_gateway.infer = AsyncMock(return_value=mock_output)
        mock_gateway.count_tokens = AsyncMock(return_value=50)

        inp = _make_input(idempotency_key="key-xyz")
        asyncio.run(execute_inference(inp, mock_gateway))

        call_kwargs = mock_gateway.infer.call_args.kwargs
        assert call_kwargs.get("idempotency_key") == "key-xyz"

    def test_passes_config_to_gateway(self) -> None:
        """execute_inference should forward InferenceConfig to gateway.infer."""
        mock_output = ModelOutput(raw_text="ok", finish_reason="stop")
        mock_gateway = MagicMock()
        mock_gateway.infer = AsyncMock(return_value=mock_output)
        mock_gateway.count_tokens = AsyncMock(return_value=50)

        config = _make_config(model_ref="gpt-4o")
        inp = _make_input(config=config)
        asyncio.run(execute_inference(inp, mock_gateway))

        call_kwargs = mock_gateway.infer.call_args.kwargs
        assert call_kwargs.get("config") is config

    def test_passes_context_window_to_gateway(self) -> None:
        """execute_inference should forward the ContextWindow to gateway.infer."""
        mock_output = ModelOutput(raw_text="ok", finish_reason="stop")
        mock_gateway = MagicMock()
        mock_gateway.infer = AsyncMock(return_value=mock_output)
        mock_gateway.count_tokens = AsyncMock(return_value=50)

        context = _make_context(system_instructions="special instructions")
        inp = _make_input(context=context)
        asyncio.run(execute_inference(inp, mock_gateway))

        call_kwargs = mock_gateway.infer.call_args.kwargs
        assert call_kwargs.get("context") is context

    def test_finish_reason_preserved(self) -> None:
        """finish_reason from gateway output should be preserved in result."""
        gateway = EchoLLMGateway()
        context = _make_context(tool_names=["search"])
        inp = _make_input(context=context)

        result = asyncio.run(execute_inference(inp, gateway))

        assert result.finish_reason == "tool_calls"

    def test_tool_calls_preserved(self) -> None:
        """tool_calls from gateway output should be preserved in result."""
        gateway = EchoLLMGateway()
        context = _make_context(tool_names=["search"])
        inp = _make_input(context=context)

        result = asyncio.run(execute_inference(inp, gateway))

        assert len(result.tool_calls) == 1
        assert result.tool_calls[0]["name"] == "search"


class TestExecuteInferenceTokenBudget:
    """Token budget enforcement tests."""

    def test_calls_count_tokens_on_gateway(self) -> None:
        """execute_inference should call gateway.count_tokens()."""
        mock_output = ModelOutput(raw_text="ok", finish_reason="stop")
        mock_gateway = MagicMock()
        mock_gateway.infer = AsyncMock(return_value=mock_output)
        mock_gateway.count_tokens = AsyncMock(return_value=100)

        inp = _make_input()
        asyncio.run(execute_inference(inp, mock_gateway))

        mock_gateway.count_tokens.assert_awaited_once()

    def test_proceeds_even_when_token_budget_exceeded(self) -> None:
        """execute_inference should NOT raise when estimated tokens exceed budget.

        Budget enforcement is the responsibility of the ContextPort.
        This test verifies the activity proceeds and returns a result.
        """
        mock_output = ModelOutput(raw_text="ok", finish_reason="stop")
        mock_gateway = MagicMock()
        mock_gateway.infer = AsyncMock(return_value=mock_output)
        # Return a count far above the budget
        mock_gateway.count_tokens = AsyncMock(return_value=999_999)

        config = _make_config(max_input=100)
        inp = _make_input(config=config)

        # Should not raise — just log a warning
        result = asyncio.run(execute_inference(inp, mock_gateway))

        assert isinstance(result, ModelOutput)


class TestExecuteInferenceErrorPropagation:
    """Verifies that gateway errors propagate unchanged."""

    def test_gateway_exception_propagates(self) -> None:
        """Exceptions from gateway.infer() should propagate to the caller."""
        mock_gateway = MagicMock()
        mock_gateway.count_tokens = AsyncMock(return_value=50)
        mock_gateway.infer = AsyncMock(side_effect=RuntimeError("provider down"))

        inp = _make_input()

        with pytest.raises(RuntimeError, match="provider down"):
            asyncio.run(execute_inference(inp, mock_gateway))
