"""Tests for TurnEngine reasoning state and optional action support."""

from __future__ import annotations

import asyncio
from typing import Any

from agent_kernel.kernel.capability_snapshot import (
    CapabilitySnapshotBuilder,
)
from agent_kernel.kernel.cognitive.context_port import InMemoryContextPort
from agent_kernel.kernel.cognitive.llm_gateway import EchoLLMGateway
from agent_kernel.kernel.cognitive.output_parser import ToolCallOutputParser
from agent_kernel.kernel.contracts import Action, InferenceConfig
from agent_kernel.kernel.dedupe_store import InMemoryDedupeStore
from agent_kernel.kernel.minimal_runtime import (
    StaticDispatchAdmissionService,
)
from agent_kernel.kernel.reasoning_loop import ReasoningLoop
from agent_kernel.kernel.turn_engine import TurnEngine, TurnInput

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_turn_engine(reasoning_loop: ReasoningLoop | None = None) -> TurnEngine:
    """Creates a TurnEngine with minimal in-memory services."""
    return TurnEngine(
        snapshot_builder=CapabilitySnapshotBuilder(),
        admission_service=StaticDispatchAdmissionService(),
        dedupe_store=InMemoryDedupeStore(),
        executor=_SimpleExecutor(),
        require_declared_snapshot_inputs=False,
        reasoning_loop=reasoning_loop,
    )


def _make_turn_input(run_id: str = "run-1") -> TurnInput:
    return TurnInput(
        run_id=run_id,
        through_offset=1,
        based_on_offset=1,
        trigger_type="signal",
    )


def _make_action(run_id: str = "run-1") -> Action:
    return Action(
        action_id="act-1",
        run_id=run_id,
        action_type="test_action",
        effect_class="read_only",
    )


class _SimpleExecutor:
    """Minimal executor that always returns acknowledged."""

    async def execute(
        self,
        action: Any,
        snapshot: Any,
        envelope: Any,
        execution_context: Any | None = None,
    ) -> dict[str, Any]:
        return {"acknowledged": True}


def _make_echo_reasoning_loop(tool_bindings: list[str] | None = None) -> ReasoningLoop:
    """Creates a ReasoningLoop using EchoLLMGateway and InMemoryContextPort."""
    return ReasoningLoop(
        context_port=InMemoryContextPort(),
        llm_gateway=EchoLLMGateway(),
        output_parser=ToolCallOutputParser(),
    )


# ---------------------------------------------------------------------------
# Test: action=None + no reasoning_loop -> completed_noop
# ---------------------------------------------------------------------------


class TestNoActionNoLoop:
    """Tests for action=None without a reasoning loop."""

    def test_returns_completed_noop_when_no_action_and_no_loop(self) -> None:
        """Should return completed_noop when action=None and no reasoning_loop."""
        engine = _make_turn_engine(reasoning_loop=None)
        result = asyncio.run(engine.run_turn(_make_turn_input()))
        assert result.state == "completed_noop"
        assert result.outcome_kind == "noop"

    def test_emitted_events_contain_completed_noop(self) -> None:
        """emitted_events should contain completed_noop state."""
        engine = _make_turn_engine(reasoning_loop=None)
        result = asyncio.run(engine.run_turn(_make_turn_input()))
        states = [e.state for e in result.emitted_events]
        assert "completed_noop" in states

    def test_no_action_no_loop_decision_ref_is_populated(self) -> None:
        """decision_ref should be non-empty even for noop."""
        engine = _make_turn_engine(reasoning_loop=None)
        result = asyncio.run(engine.run_turn(_make_turn_input()))
        assert result.decision_ref != ""


# ---------------------------------------------------------------------------
# Test: action=None + reasoning_loop -> reasoning state emitted
# ---------------------------------------------------------------------------


class TestReasoningStateEmitted:
    """Tests that the 'reasoning' state is emitted when a loop is used."""

    def test_reasoning_state_emitted_in_events(self) -> None:
        """When using a reasoning loop, 'reasoning' state should appear in events."""
        # EchoLLMGateway with no tool bindings → returns stop, no tool_calls → noop.
        loop = _make_echo_reasoning_loop(tool_bindings=[])
        engine = _make_turn_engine(reasoning_loop=loop)
        result = asyncio.run(engine.run_turn(_make_turn_input()))
        states = [e.state for e in result.emitted_events]
        assert "reasoning" in states

    def test_reasoning_state_before_completed_noop(self) -> None:
        """'reasoning' should appear before 'completed_noop' in emitted events."""
        loop = _make_echo_reasoning_loop(tool_bindings=[])
        engine = _make_turn_engine(reasoning_loop=loop)
        result = asyncio.run(engine.run_turn(_make_turn_input()))
        states = [e.state for e in result.emitted_events]
        reasoning_idx = states.index("reasoning")
        noop_idx = states.index("completed_noop")
        assert reasoning_idx < noop_idx


# ---------------------------------------------------------------------------
# Test: action=None + reasoning_loop with tools -> dispatched
# ---------------------------------------------------------------------------


class TestReasoningLoopDispatches:
    """Tests that the reasoning loop produces a dispatchable action."""

    def test_reasoning_loop_with_tools_leads_to_dispatch(self) -> None:
        """When EchoLLMGateway returns a tool call, the engine should dispatch."""
        # We need a snapshot with a tool binding so EchoLLMGateway returns a tool call.
        # Override the context port to inject a tool definition.
        from agent_kernel.kernel.contracts import ContextWindow, ToolDefinition

        class ToolAwareContextPort:
            async def assemble(
                self,
                run_id: str,
                snapshot: Any,
                history: list[Any],
                inference_config: InferenceConfig | None = None,
                recovery_context: dict | None = None,
            ) -> ContextWindow:
                return ContextWindow(
                    system_instructions="",
                    tool_definitions=(
                        ToolDefinition(
                            name="test_tool",
                            description="test",
                            input_schema={"type": "object"},
                        ),
                    ),
                )

        loop = ReasoningLoop(
            context_port=ToolAwareContextPort(),
            llm_gateway=EchoLLMGateway(),
            output_parser=ToolCallOutputParser(),
        )
        engine = _make_turn_engine(reasoning_loop=loop)
        result = asyncio.run(engine.run_turn(_make_turn_input()))
        # The echo gateway produces a tool call → parser produces an Action → dispatch.
        assert result.outcome_kind == "dispatched"
        assert result.state == "dispatch_acknowledged"

    def test_reasoning_emitted_before_dispatch(self) -> None:
        """'reasoning' state should appear before 'dispatched' in events."""
        from agent_kernel.kernel.contracts import ContextWindow, ToolDefinition

        class ToolAwareContextPort:
            async def assemble(
                self,
                run_id: str,
                snapshot: Any,
                history: list[Any],
                inference_config: InferenceConfig | None = None,
                recovery_context: dict | None = None,
            ) -> ContextWindow:
                return ContextWindow(
                    system_instructions="",
                    tool_definitions=(
                        ToolDefinition(
                            name="my_tool",
                            description="",
                            input_schema={"type": "object"},
                        ),
                    ),
                )

        loop = ReasoningLoop(
            context_port=ToolAwareContextPort(),
            llm_gateway=EchoLLMGateway(),
            output_parser=ToolCallOutputParser(),
        )
        engine = _make_turn_engine(reasoning_loop=loop)
        result = asyncio.run(engine.run_turn(_make_turn_input()))
        states = [e.state for e in result.emitted_events]
        assert "reasoning" in states
        assert "dispatched" in states
        reasoning_idx = states.index("reasoning")
        dispatched_idx = states.index("dispatched")
        assert reasoning_idx < dispatched_idx


# ---------------------------------------------------------------------------
# Test: backward compatibility — explicit action still works
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    """Tests that passing an explicit action still works correctly."""

    def test_explicit_action_dispatches_without_reasoning_loop(self) -> None:
        """Passing an explicit Action without a loop should dispatch normally."""
        engine = _make_turn_engine(reasoning_loop=None)
        result = asyncio.run(
            engine.run_turn(_make_turn_input(), action=_make_action())
        )
        assert result.outcome_kind == "dispatched"
        assert result.state == "dispatch_acknowledged"

    def test_explicit_action_with_reasoning_loop_uses_action_not_loop(self) -> None:
        """Passing an explicit Action with a loop should use the action, not the loop."""
        called: list[bool] = []

        class SpyContextPort:
            async def assemble(self, *args: Any, **kwargs: Any) -> Any:
                called.append(True)
                from agent_kernel.kernel.contracts import ContextWindow

                return ContextWindow(system_instructions="")

        loop = ReasoningLoop(
            context_port=SpyContextPort(),
            llm_gateway=EchoLLMGateway(),
            output_parser=ToolCallOutputParser(),
        )
        engine = _make_turn_engine(reasoning_loop=loop)
        result = asyncio.run(
            engine.run_turn(_make_turn_input(), action=_make_action())
        )
        # The loop context port should NOT be called when an action is supplied.
        assert called == []
        assert result.outcome_kind == "dispatched"

    def test_reasoning_state_not_in_events_when_action_provided(self) -> None:
        """'reasoning' state should NOT appear when an explicit action is provided."""
        engine = _make_turn_engine(reasoning_loop=None)
        result = asyncio.run(
            engine.run_turn(_make_turn_input(), action=_make_action())
        )
        states = [e.state for e in result.emitted_events]
        assert "reasoning" not in states
