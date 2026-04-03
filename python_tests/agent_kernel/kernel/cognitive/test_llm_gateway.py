"""Tests for LLM Gateway implementations.

Tests MUST NOT import openai or anthropic packages directly.
Provider-dependent tests use mock/patch to avoid network calls.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from unittest.mock import MagicMock, patch

import pytest

from agent_kernel.kernel.cognitive.llm_gateway import (
    BaseLLMGateway,
    EchoLLMGateway,
    LLMProviderError,
    LLMRateLimitError,
)
from agent_kernel.kernel.contracts import (
    ContextWindow,
    InferenceConfig,
    TokenBudget,
    ToolDefinition,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(model_ref: str = "test-model", max_output: int = 512) -> InferenceConfig:
    """Builds a minimal InferenceConfig for tests."""
    return InferenceConfig(
        model_ref=model_ref,
        token_budget=TokenBudget(max_input=8192, max_output=max_output),
        temperature=0.0,
    )


def _make_context(
    system_instructions: str = "You are a test agent.",
    tool_names: list[str] | None = None,
    history: list[dict] | None = None,
) -> ContextWindow:
    """Builds a minimal ContextWindow for tests."""
    tool_definitions = tuple(
        ToolDefinition(
            name=name,
            description=f"Test tool: {name}",
            input_schema={"type": "object", "properties": {}},
        )
        for name in (tool_names or [])
    )
    return ContextWindow(
        system_instructions=system_instructions,
        tool_definitions=tool_definitions,
        history=tuple(history or []),
    )


# ---------------------------------------------------------------------------
# Error taxonomy tests
# ---------------------------------------------------------------------------


class TestLLMProviderError:
    """Tests for LLMProviderError and LLMRateLimitError."""

    def test_provider_error_attributes(self) -> None:
        """LLMProviderError should expose provider, status_code, message."""
        exc = LLMProviderError("openai", 500, "Internal server error")
        assert exc.provider == "openai"
        assert exc.status_code == 500
        assert exc.message == "Internal server error"

    def test_provider_error_str_contains_status(self) -> None:
        """str(LLMProviderError) should contain the status code."""
        exc = LLMProviderError("openai", 500, "oops")
        assert "500" in str(exc)

    def test_rate_limit_is_subclass_of_provider_error(self) -> None:
        """LLMRateLimitError should be a subclass of LLMProviderError."""
        exc = LLMRateLimitError("openai", 429, "rate limit")
        assert isinstance(exc, LLMProviderError)

    def test_rate_limit_error_attributes(self) -> None:
        """LLMRateLimitError should expose provider, status_code, message."""
        exc = LLMRateLimitError("anthropic", 429, "too many requests")
        assert exc.provider == "anthropic"
        assert exc.status_code == 429

    def test_rate_limit_error_retry_after_defaults_to_none(self) -> None:
        """LLMRateLimitError.retry_after_s defaults to None when not provided."""
        exc = LLMRateLimitError("openai", 429, "rate limited")
        assert exc.retry_after_s is None

    def test_rate_limit_error_accepts_retry_after_s(self) -> None:
        """LLMRateLimitError accepts an explicit retry_after_s value."""
        exc = LLMRateLimitError("openai", 429, "rate limited", retry_after_s=30.0)
        assert exc.retry_after_s == 30.0


# ---------------------------------------------------------------------------
# EchoLLMGateway tests
# ---------------------------------------------------------------------------


class TestEchoLLMGateway:
    """Tests for EchoLLMGateway."""

    def test_infer_returns_model_output(self) -> None:
        """infer() should return a ModelOutput instance."""
        from agent_kernel.kernel.contracts import ModelOutput

        gateway = EchoLLMGateway()
        context = _make_context()
        config = _make_config()

        result = asyncio.run(gateway.infer(context, config, "key-abc"))

        assert isinstance(result, ModelOutput)

    def test_infer_stop_when_no_tools(self) -> None:
        """finish_reason should be 'stop' when no tool definitions are present."""
        gateway = EchoLLMGateway()
        context = _make_context(tool_names=[])
        config = _make_config()

        result = asyncio.run(gateway.infer(context, config, "key-abc"))

        assert result.finish_reason == "stop"
        assert result.tool_calls == []

    def test_infer_tool_calls_when_tools_defined(self) -> None:
        """finish_reason should be 'tool_calls' when tool_definitions are present."""
        gateway = EchoLLMGateway()
        context = _make_context(tool_names=["search", "write_file"])
        config = _make_config()

        result = asyncio.run(gateway.infer(context, config, "key-abc"))

        assert result.finish_reason == "tool_calls"
        assert len(result.tool_calls) == 1

    def test_infer_echoes_first_tool_only(self) -> None:
        """EchoLLMGateway should only echo the FIRST tool definition."""
        gateway = EchoLLMGateway()
        context = _make_context(tool_names=["search", "write_file", "delete"])
        config = _make_config()

        result = asyncio.run(gateway.infer(context, config, "key-xyz"))

        assert len(result.tool_calls) == 1
        assert result.tool_calls[0]["name"] == "search"

    def test_infer_tool_call_has_required_keys(self) -> None:
        """Each tool_call in ModelOutput should have id, name, arguments."""
        gateway = EchoLLMGateway()
        context = _make_context(tool_names=["fetch"])
        config = _make_config()

        result = asyncio.run(gateway.infer(context, config, "key-001"))

        tc = result.tool_calls[0]
        assert "id" in tc
        assert "name" in tc
        assert "arguments" in tc
        assert tc["name"] == "fetch"

    def test_infer_usage_has_token_counts(self) -> None:
        """usage dict should contain input_tokens and output_tokens."""
        gateway = EchoLLMGateway()
        context = _make_context(system_instructions="sys", tool_names=[])
        config = _make_config()

        result = asyncio.run(gateway.infer(context, config, "key-002"))

        assert "input_tokens" in result.usage
        assert "output_tokens" in result.usage
        assert result.usage["output_tokens"] == 10

    def test_infer_input_tokens_positive(self) -> None:
        """input_tokens should be at least 1."""
        gateway = EchoLLMGateway()
        context = _make_context(system_instructions="Hello world")
        config = _make_config()

        result = asyncio.run(gateway.infer(context, config, "key-003"))

        assert result.usage["input_tokens"] >= 1

    def test_infer_raw_text_echoes_key_when_no_tools(self) -> None:
        """raw_text should contain the idempotency_key when no tools are defined."""
        gateway = EchoLLMGateway()
        context = _make_context(tool_names=[])
        config = _make_config()
        key = "unique-key-123"

        result = asyncio.run(gateway.infer(context, config, key))

        assert key in result.raw_text

    def test_count_tokens_returns_positive_int(self) -> None:
        """count_tokens() should return a positive integer."""
        gateway = EchoLLMGateway()
        context = _make_context(system_instructions="Hello world")

        result = asyncio.run(gateway.count_tokens(context, "test-model"))

        assert isinstance(result, int)
        assert result >= 1

    def test_count_tokens_scales_with_content(self) -> None:
        """Longer system instructions should yield higher token estimate."""
        gateway = EchoLLMGateway()
        short_ctx = _make_context(system_instructions="Hi")
        long_ctx = _make_context(system_instructions="Hello " * 100)

        short_count = asyncio.run(gateway.count_tokens(short_ctx, "model"))
        long_count = asyncio.run(gateway.count_tokens(long_ctx, "model"))

        assert long_count > short_count

    def test_idempotency_key_embedded_in_tool_call_id(self) -> None:
        """Tool call id should contain a prefix derived from idempotency_key."""
        gateway = EchoLLMGateway()
        context = _make_context(tool_names=["my_tool"])
        config = _make_config()

        result = asyncio.run(gateway.infer(context, config, "abcdefgh-1234"))

        tc_id = result.tool_calls[0]["id"]
        assert tc_id.startswith("echo-")


# ---------------------------------------------------------------------------
# OpenAILLMGateway — import-guard test (no openai package required)
# ---------------------------------------------------------------------------


class TestOpenAILLMGatewayImportGuard:
    """Tests that OpenAILLMGateway raises ImportError when openai is absent."""

    def test_raises_import_error_when_openai_not_installed(self) -> None:
        """OpenAILLMGateway.__init__ should raise ImportError if openai missing."""
        with patch.dict(sys.modules, {"openai": None}):
            # Force re-import to pick up the patched modules
            import importlib

            from agent_kernel.kernel.cognitive import llm_gateway

            importlib.reload(llm_gateway)
            with pytest.raises(ImportError, match="openai"):
                llm_gateway.OpenAILLMGateway(api_key="test-key")


# ---------------------------------------------------------------------------
# AnthropicLLMGateway — import-guard test (no anthropic package required)
# ---------------------------------------------------------------------------


class TestAnthropicLLMGatewayImportGuard:
    """Tests that AnthropicLLMGateway raises ImportError when anthropic is absent."""

    def test_raises_import_error_when_anthropic_not_installed(self) -> None:
        """AnthropicLLMGateway.__init__ should raise ImportError if anthropic missing."""
        with patch.dict(sys.modules, {"anthropic": None}):
            import importlib

            from agent_kernel.kernel.cognitive import llm_gateway

            importlib.reload(llm_gateway)
            with pytest.raises(ImportError, match="anthropic"):
                llm_gateway.AnthropicLLMGateway(api_key="test-key")


# ---------------------------------------------------------------------------
# OpenAILLMGateway — normalise_response unit tests (via mock)
# ---------------------------------------------------------------------------


class TestOpenAILLMGatewayNormalise:
    """Unit tests for OpenAI response normalisation using mocks."""

    def _make_mock_openai_module(self) -> MagicMock:
        """Builds a minimal mock of the openai module."""
        mock_openai = MagicMock()
        mock_client = MagicMock()
        mock_openai.AsyncOpenAI.return_value = mock_client
        mock_openai.RateLimitError = type("RateLimitError", (Exception,), {})
        mock_openai.APIStatusError = type("APIStatusError", (Exception,), {"status_code": 500})
        return mock_openai

    def test_normalise_stop_response(self) -> None:
        """_normalise_response should handle a plain stop response."""
        mock_openai = self._make_mock_openai_module()

        with patch.dict(sys.modules, {"openai": mock_openai}):
            import importlib

            from agent_kernel.kernel.cognitive import llm_gateway

            importlib.reload(llm_gateway)

            mock_response = MagicMock()
            mock_response.choices = [MagicMock()]
            mock_response.choices[0].message.content = "Hello, world!"
            mock_response.choices[0].message.tool_calls = None
            mock_response.choices[0].finish_reason = "stop"
            mock_response.usage.prompt_tokens = 10
            mock_response.usage.completion_tokens = 5

            result = llm_gateway.OpenAILLMGateway._normalise_response(mock_response)

            assert result.raw_text == "Hello, world!"
            assert result.finish_reason == "stop"
            assert result.tool_calls == []
            assert result.usage["input_tokens"] == 10
            assert result.usage["output_tokens"] == 5

    def test_normalise_tool_call_response(self) -> None:
        """_normalise_response should parse tool_calls correctly."""
        import json as _json

        mock_openai = self._make_mock_openai_module()

        with patch.dict(sys.modules, {"openai": mock_openai}):
            import importlib

            from agent_kernel.kernel.cognitive import llm_gateway

            importlib.reload(llm_gateway)

            mock_tc = MagicMock()
            mock_tc.id = "call-001"
            mock_tc.function.name = "search"
            mock_tc.function.arguments = _json.dumps({"query": "test"})

            mock_response = MagicMock()
            mock_response.choices = [MagicMock()]
            mock_response.choices[0].message.content = ""
            mock_response.choices[0].message.tool_calls = [mock_tc]
            mock_response.choices[0].finish_reason = "tool_calls"
            mock_response.usage.prompt_tokens = 20
            mock_response.usage.completion_tokens = 15

            result = llm_gateway.OpenAILLMGateway._normalise_response(mock_response)

            assert result.finish_reason == "tool_calls"
            assert len(result.tool_calls) == 1
            tc = result.tool_calls[0]
            assert tc["id"] == "call-001"
            assert tc["name"] == "search"
            assert tc["arguments"] == {"query": "test"}


# ---------------------------------------------------------------------------
# AnthropicLLMGateway — normalise_response unit tests (via mock)
# ---------------------------------------------------------------------------


class TestAnthropicLLMGatewayNormalise:
    """Unit tests for Anthropic response normalisation using mocks."""

    def _make_mock_anthropic_module(self) -> MagicMock:
        """Builds a minimal mock of the anthropic module."""
        mock_anthropic = MagicMock()
        mock_client = MagicMock()
        mock_anthropic.AsyncAnthropic.return_value = mock_client
        mock_anthropic.RateLimitError = type("RateLimitError", (Exception,), {})
        mock_anthropic.APIStatusError = type("APIStatusError", (Exception,), {"status_code": 500})
        return mock_anthropic

    def test_normalise_text_response(self) -> None:
        """_normalise_response should handle a plain text response."""
        mock_anthropic = self._make_mock_anthropic_module()

        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            import importlib

            from agent_kernel.kernel.cognitive import llm_gateway

            importlib.reload(llm_gateway)

            text_block = MagicMock()
            text_block.type = "text"
            text_block.text = "Hello from Anthropic"

            mock_response = MagicMock()
            mock_response.content = [text_block]
            mock_response.stop_reason = "end_turn"
            mock_response.usage.input_tokens = 12
            mock_response.usage.output_tokens = 8

            result = llm_gateway.AnthropicLLMGateway._normalise_response(mock_response)

            assert result.raw_text == "Hello from Anthropic"
            assert result.finish_reason == "stop"
            assert result.tool_calls == []
            assert result.usage["input_tokens"] == 12
            assert result.usage["output_tokens"] == 8

    def test_normalise_tool_use_response(self) -> None:
        """_normalise_response should parse tool_use blocks correctly."""
        mock_anthropic = self._make_mock_anthropic_module()

        with patch.dict(sys.modules, {"anthropic": mock_anthropic}):
            import importlib

            from agent_kernel.kernel.cognitive import llm_gateway

            importlib.reload(llm_gateway)

            tool_block = MagicMock()
            tool_block.type = "tool_use"
            tool_block.id = "tu-001"
            tool_block.name = "fetch_url"
            tool_block.input = {"url": "https://example.com"}

            mock_response = MagicMock()
            mock_response.content = [tool_block]
            mock_response.stop_reason = "tool_use"
            mock_response.usage.input_tokens = 30
            mock_response.usage.output_tokens = 20

            result = llm_gateway.AnthropicLLMGateway._normalise_response(mock_response)

            assert result.finish_reason == "tool_calls"
            assert len(result.tool_calls) == 1
            tc = result.tool_calls[0]
            assert tc["id"] == "tu-001"
            assert tc["name"] == "fetch_url"
            assert tc["arguments"] == {"url": "https://example.com"}


# ---------------------------------------------------------------------------
# P3e — _parse_retry_after and _with_rate_limit_retry with Retry-After header
# ---------------------------------------------------------------------------


class TestParseRetryAfter:
    """Tests for the _parse_retry_after helper."""

    def test_returns_none_for_none_response(self) -> None:
        from agent_kernel.kernel.cognitive.llm_gateway import _parse_retry_after

        assert _parse_retry_after(None) is None

    def test_returns_none_when_no_headers(self) -> None:
        from agent_kernel.kernel.cognitive.llm_gateway import _parse_retry_after

        resp = MagicMock(spec=[])  # no .headers attribute
        assert _parse_retry_after(resp) is None

    def test_returns_float_from_integer_header(self) -> None:
        from agent_kernel.kernel.cognitive.llm_gateway import _parse_retry_after

        resp = MagicMock()
        resp.headers = {"retry-after": "30"}
        assert _parse_retry_after(resp) == 30.0

    def test_returns_float_from_float_header(self) -> None:
        from agent_kernel.kernel.cognitive.llm_gateway import _parse_retry_after

        resp = MagicMock()
        resp.headers = {"retry-after": "5.5"}
        assert _parse_retry_after(resp) == 5.5

    def test_returns_none_for_unparseable_header(self) -> None:
        from agent_kernel.kernel.cognitive.llm_gateway import _parse_retry_after

        resp = MagicMock()
        resp.headers = {"retry-after": "Thu, 01 Jan 2099 00:00:00 GMT"}
        assert _parse_retry_after(resp) is None

    def test_returns_none_when_header_absent(self) -> None:
        from agent_kernel.kernel.cognitive.llm_gateway import _parse_retry_after

        resp = MagicMock()
        resp.headers = {}
        assert _parse_retry_after(resp) is None


class TestWithRateLimitRetryHeader:
    """Tests that _with_rate_limit_retry uses Retry-After over exponential backoff."""

    def test_uses_retry_after_when_present(self) -> None:
        """When exc.retry_after_s is set, sleep should use that value, not backoff."""
        from agent_kernel.kernel.cognitive.llm_gateway import (
            LLMRateLimitError,
            _with_rate_limit_retry,
        )

        calls: list[float] = []
        exc_with_header = LLMRateLimitError("openai", 429, "limited", retry_after_s=7.0)

        async def _factory() -> None:
            raise exc_with_header

        async def _run() -> None:
            with patch("agent_kernel.kernel.cognitive.llm_gateway.asyncio.sleep") as mock_sleep:
                mock_sleep.return_value = None
                with contextlib.suppress(LLMRateLimitError):
                    await _with_rate_limit_retry(_factory, "openai")
                for call in mock_sleep.call_args_list:
                    calls.append(call.args[0])

        asyncio.run(_run())
        # All sleeps should use Retry-After (7.0), not the 1.0/2.0 exponential defaults
        assert all(delay == 7.0 for delay in calls), f"Expected 7.0, got {calls}"

    def test_falls_back_to_exponential_without_retry_after(self) -> None:
        """When retry_after_s is None, backoff should use exponential schedule."""
        from agent_kernel.kernel.cognitive.llm_gateway import (
            LLMRateLimitError,
            _with_rate_limit_retry,
        )

        calls: list[float] = []
        exc_no_header = LLMRateLimitError("openai", 429, "limited", retry_after_s=None)

        async def _factory() -> None:
            raise exc_no_header

        async def _run() -> None:
            with patch("agent_kernel.kernel.cognitive.llm_gateway.asyncio.sleep") as mock_sleep:
                mock_sleep.return_value = None
                with contextlib.suppress(LLMRateLimitError):
                    await _with_rate_limit_retry(_factory, "openai")
                for call in mock_sleep.call_args_list:
                    calls.append(call.args[0])

        asyncio.run(_run())
        # First sleep = 1.0, second sleep = 2.0
        assert calls == [1.0, 2.0], f"Expected [1.0, 2.0], got {calls}"


# ---------------------------------------------------------------------------
# R4b — BaseLLMGateway shared base class
# ---------------------------------------------------------------------------


class TestBaseLLMGateway:
    """Verifies the shared base class behaviour inherited by both concrete gateways."""

    def _make_base(self) -> BaseLLMGateway:
        return BaseLLMGateway()

    def test_count_tokens_non_zero(self) -> None:
        base = self._make_base()
        ctx = _make_context(system_instructions="Hello, world!")
        result = asyncio.run(base.count_tokens(ctx, "test-model"))
        assert result >= 1

    def test_count_tokens_proportional_to_input_length(self) -> None:
        base = self._make_base()
        short_ctx = _make_context(system_instructions="Hi")
        long_ctx = _make_context(system_instructions="Hi " * 200)
        short_result = asyncio.run(base.count_tokens(short_ctx, "test-model"))
        long_result = asyncio.run(base.count_tokens(long_ctx, "test-model"))
        assert long_result > short_result

    def test_count_tokens_includes_history(self) -> None:
        base = self._make_base()
        ctx_no_history = _make_context(system_instructions="sys", history=[])
        ctx_with_history = _make_context(
            system_instructions="sys", history=[{"role": "user", "content": "x" * 400}]
        )
        no_hist = asyncio.run(base.count_tokens(ctx_no_history, "test-model"))
        with_hist = asyncio.run(base.count_tokens(ctx_with_history, "test-model"))
        assert with_hist > no_hist

    def test_call_with_retry_returns_value_on_success(self) -> None:
        base = self._make_base()

        async def _factory() -> str:
            return "ok"

        result = asyncio.run(base._call_with_retry(_factory, "test"))
        assert result == "ok"

    def test_call_with_retry_propagates_rate_limit_after_exhaustion(self) -> None:
        base = self._make_base()

        async def _factory() -> None:
            raise LLMRateLimitError("test", 429, "limited", retry_after_s=0.0)

        async def _run() -> None:
            with patch("agent_kernel.kernel.cognitive.llm_gateway.asyncio.sleep"):
                await base._call_with_retry(_factory, "test")

        with pytest.raises(LLMRateLimitError):
            asyncio.run(_run())

    def test_openai_gateway_inherits_base(self) -> None:
        import importlib

        import agent_kernel.kernel.cognitive.llm_gateway as _mod

        _mod = importlib.reload(_mod)
        assert issubclass(_mod.OpenAILLMGateway, _mod.BaseLLMGateway)

    def test_anthropic_gateway_inherits_base(self) -> None:
        import importlib

        import agent_kernel.kernel.cognitive.llm_gateway as _mod

        _mod = importlib.reload(_mod)
        assert issubclass(_mod.AnthropicLLMGateway, _mod.BaseLLMGateway)

    def test_openai_gateway_count_tokens_uses_base(self) -> None:
        """OpenAI gateway count_tokens is inherited from base — not overridden."""
        from agent_kernel.kernel.cognitive.llm_gateway import OpenAILLMGateway

        # Verify the method is *not* defined on the concrete class itself.
        assert "count_tokens" not in OpenAILLMGateway.__dict__

    def test_anthropic_gateway_count_tokens_uses_base(self) -> None:
        """Anthropic gateway count_tokens is inherited from base — not overridden."""
        from agent_kernel.kernel.cognitive.llm_gateway import AnthropicLLMGateway

        assert "count_tokens" not in AnthropicLLMGateway.__dict__
