"""Tests for ReasoningLoop orchestration."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_kernel.kernel.capability_snapshot import CapabilitySnapshot
from agent_kernel.kernel.cognitive.context_port import InMemoryContextPort
from agent_kernel.kernel.cognitive.llm_gateway import EchoLLMGateway
from agent_kernel.kernel.cognitive.output_parser import ToolCallOutputParser
from agent_kernel.kernel.contracts import (
    Action,
    ContextWindow,
    InferenceConfig,
    ModelOutput,
    RuntimeEvent,
)
from agent_kernel.kernel.reasoning_loop import ReasoningLoop, ReasoningResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_snapshot(tool_bindings: list[str] | None = None) -> CapabilitySnapshot:
    """Builds a minimal CapabilitySnapshot for tests."""
    from agent_kernel.kernel.capability_snapshot import (
        CapabilitySnapshotBuilder,
        CapabilitySnapshotInput,
    )

    return CapabilitySnapshotBuilder().build(
        CapabilitySnapshotInput(
            run_id="run-test",
            based_on_offset=0,
            tenant_policy_ref="policy:test",
            permission_mode="strict",
            tool_bindings=tool_bindings or [],
        )
    )


def _make_inference_config() -> InferenceConfig:
    return InferenceConfig(model_ref="echo")


# ---------------------------------------------------------------------------
# Test: run_once returns ReasoningResult with actions
# ---------------------------------------------------------------------------


class TestReasoningLoopRunOnce:
    """Tests for the core run_once method."""

    def test_run_once_returns_reasoning_result_type(self) -> None:
        """run_once should return a ReasoningResult instance."""
        context_port = InMemoryContextPort()
        llm_gateway = EchoLLMGateway()
        output_parser = ToolCallOutputParser()
        loop = ReasoningLoop(
            context_port=context_port,
            llm_gateway=llm_gateway,
            output_parser=output_parser,
        )
        snapshot = _make_snapshot(tool_bindings=["my_tool"])
        result = asyncio.run(
            loop.run_once(
                run_id="run-1",
                snapshot=snapshot,
                history=[],
                inference_config=_make_inference_config(),
            )
        )
        assert isinstance(result, ReasoningResult)

    def test_run_once_produces_actions_when_tools_present(self) -> None:
        """run_once should parse tool calls from EchoLLMGateway into Actions."""
        context_port = InMemoryContextPort()
        llm_gateway = EchoLLMGateway()
        output_parser = ToolCallOutputParser()
        loop = ReasoningLoop(
            context_port=context_port,
            llm_gateway=llm_gateway,
            output_parser=output_parser,
        )
        # EchoLLMGateway returns a tool_call when tools are defined.
        snapshot = _make_snapshot(tool_bindings=["echo_tool"])
        result = asyncio.run(
            loop.run_once(
                run_id="run-1",
                snapshot=snapshot,
                history=[],
                inference_config=_make_inference_config(),
            )
        )
        assert len(result.actions) == 1
        assert result.actions[0].action_type == "echo_tool"
        assert isinstance(result.actions[0], Action)

    def test_run_once_empty_actions_when_no_tools(self) -> None:
        """When EchoLLMGateway produces no tool calls, actions should be empty."""
        context_port = InMemoryContextPort()
        llm_gateway = EchoLLMGateway()
        output_parser = ToolCallOutputParser()
        loop = ReasoningLoop(
            context_port=context_port,
            llm_gateway=llm_gateway,
            output_parser=output_parser,
        )
        # No tool_bindings → EchoLLMGateway returns stop finish_reason, no tool_calls.
        snapshot = _make_snapshot(tool_bindings=[])
        result = asyncio.run(
            loop.run_once(
                run_id="run-1",
                snapshot=snapshot,
                history=[],
                inference_config=_make_inference_config(),
            )
        )
        assert result.actions == []

    def test_run_once_preserves_model_output(self) -> None:
        """ReasoningResult should preserve the raw ModelOutput."""
        context_port = InMemoryContextPort()
        llm_gateway = EchoLLMGateway()
        output_parser = ToolCallOutputParser()
        loop = ReasoningLoop(
            context_port=context_port,
            llm_gateway=llm_gateway,
            output_parser=output_parser,
        )
        snapshot = _make_snapshot(tool_bindings=[])
        result = asyncio.run(
            loop.run_once(
                run_id="run-2",
                snapshot=snapshot,
                history=[],
                inference_config=_make_inference_config(),
            )
        )
        assert isinstance(result.model_output, ModelOutput)
        assert result.model_output.finish_reason == "stop"

    def test_run_once_preserves_context_window(self) -> None:
        """ReasoningResult should preserve the assembled ContextWindow."""
        context_port = InMemoryContextPort()
        llm_gateway = EchoLLMGateway()
        output_parser = ToolCallOutputParser()
        loop = ReasoningLoop(
            context_port=context_port,
            llm_gateway=llm_gateway,
            output_parser=output_parser,
        )
        snapshot = _make_snapshot(tool_bindings=[])
        result = asyncio.run(
            loop.run_once(
                run_id="run-2",
                snapshot=snapshot,
                history=[],
                inference_config=_make_inference_config(),
            )
        )
        assert isinstance(result.context_window, ContextWindow)

    def test_run_once_preserves_inference_config(self) -> None:
        """ReasoningResult should preserve the InferenceConfig."""
        context_port = InMemoryContextPort()
        llm_gateway = EchoLLMGateway()
        output_parser = ToolCallOutputParser()
        loop = ReasoningLoop(
            context_port=context_port,
            llm_gateway=llm_gateway,
            output_parser=output_parser,
        )
        snapshot = _make_snapshot()
        config = InferenceConfig(model_ref="my-model")
        result = asyncio.run(
            loop.run_once(
                run_id="run-3",
                snapshot=snapshot,
                history=[],
                inference_config=config,
            )
        )
        assert result.inference_config is config


# ---------------------------------------------------------------------------
# Test: idempotency_key is passed to LLMGateway
# ---------------------------------------------------------------------------


class TestIdempotencyKeyPassthrough:
    """Tests that idempotency_key is forwarded to the LLM gateway."""

    def test_idempotency_key_passed_to_gateway(self) -> None:
        """run_once should forward the idempotency_key to LLMGateway.infer."""
        captured: list[str] = []

        class CapturingGateway:
            async def infer(
                self,
                context: ContextWindow,
                config: InferenceConfig,
                idempotency_key: str,
            ) -> ModelOutput:
                captured.append(idempotency_key)
                return ModelOutput(
                    raw_text="",
                    tool_calls=[],
                    finish_reason="stop",
                    usage={},
                )

        context_port = InMemoryContextPort()
        output_parser = ToolCallOutputParser()
        loop = ReasoningLoop(
            context_port=context_port,
            llm_gateway=CapturingGateway(),
            output_parser=output_parser,
        )
        snapshot = _make_snapshot()
        asyncio.run(
            loop.run_once(
                run_id="run-1",
                snapshot=snapshot,
                history=[],
                inference_config=_make_inference_config(),
                idempotency_key="test-key-abc",
            )
        )
        assert captured == ["test-key-abc"]

    def test_generated_key_when_none_provided(self) -> None:
        """When idempotency_key is None, a non-empty UUID hex should be used."""
        captured: list[str] = []

        class CapturingGateway:
            async def infer(
                self,
                context: ContextWindow,
                config: InferenceConfig,
                idempotency_key: str,
            ) -> ModelOutput:
                captured.append(idempotency_key)
                return ModelOutput(
                    raw_text="",
                    tool_calls=[],
                    finish_reason="stop",
                    usage={},
                )

        context_port = InMemoryContextPort()
        output_parser = ToolCallOutputParser()
        loop = ReasoningLoop(
            context_port=context_port,
            llm_gateway=CapturingGateway(),
            output_parser=output_parser,
        )
        snapshot = _make_snapshot()
        asyncio.run(
            loop.run_once(
                run_id="run-1",
                snapshot=snapshot,
                history=[],
                inference_config=_make_inference_config(),
                idempotency_key=None,
            )
        )
        assert len(captured) == 1
        assert len(captured[0]) > 0  # UUID hex is non-empty.


# ---------------------------------------------------------------------------
# Test: recovery_context is passed to ContextPort
# ---------------------------------------------------------------------------


class TestRecoveryContextPassthrough:
    """Tests that recovery_context is forwarded to the context port."""

    def test_recovery_context_passed_to_context_port(self) -> None:
        """run_once should forward recovery_context to ContextPort.assemble."""
        captured_context: list[dict | None] = []

        class CapturingContextPort:
            async def assemble(
                self,
                run_id: str,
                snapshot: Any,
                history: list[Any],
                inference_config: InferenceConfig | None = None,
                recovery_context: dict | None = None,
            ) -> ContextWindow:
                captured_context.append(recovery_context)
                return ContextWindow(system_instructions="")

        loop = ReasoningLoop(
            context_port=CapturingContextPort(),
            llm_gateway=EchoLLMGateway(),
            output_parser=ToolCallOutputParser(),
        )
        snapshot = _make_snapshot()
        rc = {"failure_kind": "runtime_error", "reflection_round": 1}
        asyncio.run(
            loop.run_once(
                run_id="run-1",
                snapshot=snapshot,
                history=[],
                inference_config=_make_inference_config(),
                recovery_context=rc,
            )
        )
        assert captured_context == [rc]

    def test_none_recovery_context_when_not_provided(self) -> None:
        """When recovery_context is omitted, ContextPort receives None."""
        captured_context: list[dict | None] = []

        class CapturingContextPort:
            async def assemble(
                self,
                run_id: str,
                snapshot: Any,
                history: list[Any],
                inference_config: InferenceConfig | None = None,
                recovery_context: dict | None = None,
            ) -> ContextWindow:
                captured_context.append(recovery_context)
                return ContextWindow(system_instructions="")

        loop = ReasoningLoop(
            context_port=CapturingContextPort(),
            llm_gateway=EchoLLMGateway(),
            output_parser=ToolCallOutputParser(),
        )
        snapshot = _make_snapshot()
        asyncio.run(
            loop.run_once(
                run_id="run-1",
                snapshot=snapshot,
                history=[],
                inference_config=_make_inference_config(),
            )
        )
        assert captured_context == [None]
