"""Tests for PlannedRecoveryGateService reflect_and_retry support."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent_kernel.kernel.capability_snapshot import (
    CapabilitySnapshotBuilder,
    CapabilitySnapshotInput,
)
from agent_kernel.kernel.cognitive.context_port import InMemoryContextPort
from agent_kernel.kernel.cognitive.llm_gateway import EchoLLMGateway
from agent_kernel.kernel.cognitive.output_parser import ToolCallOutputParser
from agent_kernel.kernel.contracts import (
    Action,
    ContextWindow,
    InferenceConfig,
    RecoveryDecision,
    RecoveryInput,
    ReflectionPolicy,
    RunProjection,
    ToolDefinition,
)
from agent_kernel.kernel.reasoning_loop import ReasoningLoop
from agent_kernel.kernel.recovery.gate import PlannedRecoveryGateService
from agent_kernel.kernel.recovery.reflection_builder import ReflectionContextBuilder


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_projection(run_id: str = "run-1") -> RunProjection:
    return RunProjection(
        run_id=run_id,
        lifecycle_state="dispatching",
        projected_offset=1,
        waiting_external=False,
        ready_for_dispatch=True,
    )


def _make_recovery_input(
    run_id: str = "run-1",
    reason_code: str = "runtime_error",
    failed_action_id: str | None = "act-1",
    reflection_round: int = 0,
) -> RecoveryInput:
    return RecoveryInput(
        run_id=run_id,
        reason_code=reason_code,
        lifecycle_state="dispatching",
        projection=_make_projection(run_id),
        failed_action_id=failed_action_id,
        reflection_round=reflection_round,
    )


def _make_permissive_policy(
    max_rounds: int = 3,
    escalate_on_exhaustion: bool = True,
) -> ReflectionPolicy:
    """Policy that allows all standard failure kinds."""
    return ReflectionPolicy(
        max_rounds=max_rounds,
        escalate_on_exhaustion=escalate_on_exhaustion,
    )


class _ToolAwareContextPort:
    """Context port that always returns a context with one tool definition."""

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
                    name="corrected_tool",
                    description="corrected",
                    input_schema={"type": "object"},
                ),
            ),
            recovery_context=recovery_context,
        )


def _make_reasoning_loop_with_tools() -> ReasoningLoop:
    return ReasoningLoop(
        context_port=_ToolAwareContextPort(),
        llm_gateway=EchoLLMGateway(),
        output_parser=ToolCallOutputParser(),
    )


def _make_reasoning_loop_no_tools() -> ReasoningLoop:
    """Reasoning loop that always returns empty actions (no tools)."""
    return ReasoningLoop(
        context_port=InMemoryContextPort(),
        llm_gateway=EchoLLMGateway(),
        output_parser=ToolCallOutputParser(),
    )


# ---------------------------------------------------------------------------
# Test: reflect_and_retry mode when all three are set
# ---------------------------------------------------------------------------


class TestReflectAndRetryMode:
    """Tests that reflect_and_retry is chosen when prerequisites are met."""

    def test_reflect_and_retry_mode_when_all_present(self) -> None:
        """When policy, loop, and builder are set, a reflectable failure should yield
        reflect_and_retry mode."""
        gate = PlannedRecoveryGateService(
            reflection_policy=_make_permissive_policy(),
            reasoning_loop=_make_reasoning_loop_with_tools(),
            reflection_builder=ReflectionContextBuilder(),
        )
        recovery_input = _make_recovery_input(
            reason_code="runtime_error",
            reflection_round=0,
        )
        decision = asyncio.run(gate.decide(recovery_input))
        assert decision.mode == "reflect_and_retry"

    def test_corrected_action_present_in_decision(self) -> None:
        """reflect_and_retry decision should carry a corrected_action."""
        gate = PlannedRecoveryGateService(
            reflection_policy=_make_permissive_policy(),
            reasoning_loop=_make_reasoning_loop_with_tools(),
            reflection_builder=ReflectionContextBuilder(),
        )
        recovery_input = _make_recovery_input(
            reason_code="runtime_error",
            reflection_round=0,
        )
        decision = asyncio.run(gate.decide(recovery_input))
        assert decision.corrected_action is not None
        assert isinstance(decision.corrected_action, Action)

    def test_corrected_action_is_from_reasoning_loop(self) -> None:
        """The corrected_action should match the action produced by the loop."""
        gate = PlannedRecoveryGateService(
            reflection_policy=_make_permissive_policy(),
            reasoning_loop=_make_reasoning_loop_with_tools(),
            reflection_builder=ReflectionContextBuilder(),
        )
        recovery_input = _make_recovery_input(
            reason_code="runtime_error",
            reflection_round=0,
        )
        decision = asyncio.run(gate.decide(recovery_input))
        # EchoLLMGateway uses the first tool; ToolCallOutputParser names it.
        assert decision.corrected_action is not None
        assert decision.corrected_action.action_type == "corrected_tool"


# ---------------------------------------------------------------------------
# Test: fallback to abort when not reflectable
# ---------------------------------------------------------------------------


class TestNonReflectableFailure:
    """Tests fallback behaviour for non-reflectable failure kinds."""

    def test_non_reflectable_failure_does_not_reflect(self) -> None:
        """A non-reflectable failure kind should not trigger reflect_and_retry."""
        gate = PlannedRecoveryGateService(
            reflection_policy=_make_permissive_policy(),
            reasoning_loop=_make_reasoning_loop_with_tools(),
            reflection_builder=ReflectionContextBuilder(),
        )
        # "permission_denied" is in non_reflectable_failure_kinds by default.
        recovery_input = _make_recovery_input(
            reason_code="permission_denied",
            reflection_round=0,
        )
        decision = asyncio.run(gate.decide(recovery_input))
        assert decision.mode != "reflect_and_retry"

    def test_non_reflectable_failure_falls_back_to_planner_mode(self) -> None:
        """Non-reflectable failures should use planner-derived mode."""
        gate = PlannedRecoveryGateService(
            reflection_policy=_make_permissive_policy(),
            reasoning_loop=_make_reasoning_loop_with_tools(),
            reflection_builder=ReflectionContextBuilder(),
        )
        recovery_input = _make_recovery_input(reason_code="permission_denied")
        decision = asyncio.run(gate.decide(recovery_input))
        # permission_denied → fatal → abort.
        assert decision.mode == "abort"

    def test_no_reflection_policy_does_not_reflect(self) -> None:
        """Without a reflection_policy, reflect_and_retry should never be chosen."""
        gate = PlannedRecoveryGateService(
            reflection_policy=None,
            reasoning_loop=_make_reasoning_loop_with_tools(),
            reflection_builder=ReflectionContextBuilder(),
        )
        recovery_input = _make_recovery_input(reason_code="runtime_error")
        decision = asyncio.run(gate.decide(recovery_input))
        assert decision.mode != "reflect_and_retry"

    def test_no_reasoning_loop_does_not_reflect(self) -> None:
        """Without a reasoning_loop, reflect_and_retry should never be chosen."""
        gate = PlannedRecoveryGateService(
            reflection_policy=_make_permissive_policy(),
            reasoning_loop=None,
            reflection_builder=ReflectionContextBuilder(),
        )
        recovery_input = _make_recovery_input(reason_code="runtime_error")
        decision = asyncio.run(gate.decide(recovery_input))
        assert decision.mode != "reflect_and_retry"

    def test_no_reflection_builder_does_not_reflect(self) -> None:
        """Without a reflection_builder, reflect_and_retry should never be chosen."""
        gate = PlannedRecoveryGateService(
            reflection_policy=_make_permissive_policy(),
            reasoning_loop=_make_reasoning_loop_with_tools(),
            reflection_builder=None,
        )
        recovery_input = _make_recovery_input(reason_code="runtime_error")
        decision = asyncio.run(gate.decide(recovery_input))
        assert decision.mode != "reflect_and_retry"


# ---------------------------------------------------------------------------
# Test: fallback to escalation when rounds exhausted
# ---------------------------------------------------------------------------


class TestRoundsExhausted:
    """Tests fallback behaviour when reflection rounds are exhausted."""

    def test_fallback_to_escalation_when_rounds_exhausted_with_escalate_flag(
        self,
    ) -> None:
        """When max_rounds reached and escalate_on_exhaustion=True, should escalate."""
        # Policy with max_rounds=2; loop returns empty actions → fallback.
        policy = ReflectionPolicy(
            max_rounds=2,
            escalate_on_exhaustion=True,
        )
        gate = PlannedRecoveryGateService(
            reflection_policy=policy,
            reasoning_loop=_make_reasoning_loop_no_tools(),  # Returns no actions.
            reflection_builder=ReflectionContextBuilder(),
        )
        # reflection_round=0 < max_rounds=2 → try to reflect, but loop returns no actions.
        recovery_input = _make_recovery_input(
            reason_code="runtime_error",
            reflection_round=0,
        )
        decision = asyncio.run(gate.decide(recovery_input))
        # No tools → EchoLLMGateway returns stop → empty actions → fallback.
        assert decision.mode == "human_escalation"

    def test_fallback_to_abort_when_rounds_exhausted_without_escalate_flag(
        self,
    ) -> None:
        """When max_rounds reached and escalate_on_exhaustion=False, should abort."""
        policy = ReflectionPolicy(
            max_rounds=2,
            escalate_on_exhaustion=False,
        )
        gate = PlannedRecoveryGateService(
            reflection_policy=policy,
            reasoning_loop=_make_reasoning_loop_no_tools(),
            reflection_builder=ReflectionContextBuilder(),
        )
        recovery_input = _make_recovery_input(
            reason_code="runtime_error",
            reflection_round=0,
        )
        decision = asyncio.run(gate.decide(recovery_input))
        assert decision.mode == "abort"

    def test_skip_reflection_when_round_exceeds_max(self) -> None:
        """When reflection_round >= max_rounds, should skip reflection entirely."""
        policy = ReflectionPolicy(max_rounds=2)
        gate = PlannedRecoveryGateService(
            reflection_policy=policy,
            reasoning_loop=_make_reasoning_loop_with_tools(),
            reflection_builder=ReflectionContextBuilder(),
        )
        # reflection_round=2 >= max_rounds=2 → should NOT reflect.
        recovery_input = _make_recovery_input(
            reason_code="runtime_error",
            reflection_round=2,
        )
        decision = asyncio.run(gate.decide(recovery_input))
        assert decision.mode != "reflect_and_retry"


# ---------------------------------------------------------------------------
# Test: corrected_action absent in non-reflect decisions
# ---------------------------------------------------------------------------


class TestCorrectedActionAbsent:
    """Tests that non-reflect decisions do not carry a corrected_action."""

    def test_no_corrected_action_for_abort(self) -> None:
        """abort decisions should not carry corrected_action."""
        gate = PlannedRecoveryGateService()
        recovery_input = _make_recovery_input(reason_code="permission_denied")
        decision = asyncio.run(gate.decide(recovery_input))
        assert decision.corrected_action is None

    def test_no_corrected_action_for_escalation(self) -> None:
        """human_escalation decisions should not carry corrected_action."""
        gate = PlannedRecoveryGateService()
        recovery_input = _make_recovery_input(reason_code="waiting_external_approval")
        decision = asyncio.run(gate.decide(recovery_input))
        assert decision.corrected_action is None


# ---------------------------------------------------------------------------
# Test: RecoveryDecision backward compatibility
# ---------------------------------------------------------------------------


class TestRecoveryDecisionBackwardCompatibility:
    """Tests that RecoveryDecision can be constructed without corrected_action."""

    def test_recovery_decision_without_corrected_action(self) -> None:
        """RecoveryDecision construction without corrected_action should work."""
        decision = RecoveryDecision(
            run_id="run-1",
            mode="abort",
            reason="test",
        )
        assert decision.corrected_action is None

    def test_recovery_decision_with_corrected_action(self) -> None:
        """RecoveryDecision construction with corrected_action should work."""
        action = Action(
            action_id="act-corrected",
            run_id="run-1",
            action_type="corrected",
            effect_class="read_only",
        )
        decision = RecoveryDecision(
            run_id="run-1",
            mode="reflect_and_retry",
            reason="reflected",
            corrected_action=action,
        )
        assert decision.corrected_action is action
