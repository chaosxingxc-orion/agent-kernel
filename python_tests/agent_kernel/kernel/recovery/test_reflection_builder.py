"""Tests for ReflectionContextBuilder."""

from __future__ import annotations

import pytest

from agent_kernel.kernel.contracts import (
    BranchResult,
    ContextWindow,
    InferenceConfig,
    ScriptFailureEvidence,
    SkillSummary,
    ToolDefinition,
)
from agent_kernel.kernel.recovery.reflection_builder import ReflectionContextBuilder

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_evidence(
    failure_kind: str = "runtime_error",
    budget_consumed_ratio: float = 0.5,
    suspected_cause: str | None = "null pointer",
    partial_output: str | None = "some output",
    original_script: str = "print('hello')",
    stderr_tail: str | None = "Traceback ...",
) -> ScriptFailureEvidence:
    return ScriptFailureEvidence(
        script_id="script-1",
        failure_kind=failure_kind,  # type: ignore[arg-type]
        budget_consumed_ratio=budget_consumed_ratio,
        output_produced=bool(partial_output),
        suspected_cause=suspected_cause,
        partial_output=partial_output,
        original_script=original_script,
        stderr_tail=stderr_tail,
    )


def _make_base_context(
    system_instructions: str = "You are a helpful agent.",
) -> ContextWindow:
    return ContextWindow(
        system_instructions=system_instructions,
        tool_definitions=(
            ToolDefinition(
                name="run_script",
                description="Runs a script",
                input_schema={"type": "object"},
            ),
        ),
        skill_definitions=(
            SkillSummary(skill_id="skill-1", description="A skill"),
        ),
        history=({"role": "user", "content": "hello"},),
        current_state={"turn": 2},
        memory_ref="mem-ref-1",
        inference_config=InferenceConfig(model_ref="echo"),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestReflectionContextBuilderBuild:
    """Tests for ReflectionContextBuilder.build()."""

    def test_build_returns_context_window(self) -> None:
        """build() should return a ContextWindow instance."""
        builder = ReflectionContextBuilder()
        evidence = _make_evidence()
        result = builder.build(
            evidence=evidence,
            successful_branches=[],
            base_context=_make_base_context(),
        )
        assert isinstance(result, ContextWindow)

    def test_recovery_context_failure_kind(self) -> None:
        """failure_kind from evidence should appear in recovery_context."""
        builder = ReflectionContextBuilder()
        evidence = _make_evidence(failure_kind="heartbeat_timeout")
        result = builder.build(
            evidence=evidence,
            successful_branches=[],
            base_context=_make_base_context(),
        )
        assert result.recovery_context is not None
        assert result.recovery_context["failure_kind"] == "heartbeat_timeout"

    def test_recovery_context_suspected_cause(self) -> None:
        """suspected_cause from evidence should appear in recovery_context."""
        builder = ReflectionContextBuilder()
        evidence = _make_evidence(suspected_cause="infinite loop detected")
        result = builder.build(
            evidence=evidence,
            successful_branches=[],
            base_context=_make_base_context(),
        )
        assert result.recovery_context is not None
        assert result.recovery_context["suspected_cause"] == "infinite loop detected"

    def test_recovery_context_budget_consumed_ratio(self) -> None:
        """budget_consumed_ratio from evidence should appear in recovery_context."""
        builder = ReflectionContextBuilder()
        evidence = _make_evidence(budget_consumed_ratio=0.95)
        result = builder.build(
            evidence=evidence,
            successful_branches=[],
            base_context=_make_base_context(),
        )
        assert result.recovery_context is not None
        assert result.recovery_context["budget_consumed_ratio"] == pytest.approx(0.95)

    def test_recovery_context_original_script(self) -> None:
        """original_script from evidence should appear in recovery_context."""
        builder = ReflectionContextBuilder()
        evidence = _make_evidence(original_script="for i in range(10):\n    pass")
        result = builder.build(
            evidence=evidence,
            successful_branches=[],
            base_context=_make_base_context(),
        )
        assert result.recovery_context is not None
        assert result.recovery_context["original_script"] == "for i in range(10):\n    pass"

    def test_recovery_context_partial_output(self) -> None:
        """partial_output from evidence should appear in recovery_context."""
        builder = ReflectionContextBuilder()
        evidence = _make_evidence(partial_output="step 1 done\n")
        result = builder.build(
            evidence=evidence,
            successful_branches=[],
            base_context=_make_base_context(),
        )
        assert result.recovery_context is not None
        assert result.recovery_context["partial_output"] == "step 1 done\n"

    def test_recovery_context_stderr_tail(self) -> None:
        """stderr_tail from evidence should appear in recovery_context."""
        builder = ReflectionContextBuilder()
        evidence = _make_evidence(stderr_tail="ZeroDivisionError: division by zero")
        result = builder.build(
            evidence=evidence,
            successful_branches=[],
            base_context=_make_base_context(),
        )
        assert result.recovery_context is not None
        assert result.recovery_context["stderr_tail"] == "ZeroDivisionError: division by zero"

    def test_recovery_context_successful_branch_ids(self) -> None:
        """Successful branch action IDs should appear in recovery_context."""
        builder = ReflectionContextBuilder()
        evidence = _make_evidence()
        branches = [
            BranchResult(action_id="act-a"),
            BranchResult(action_id="act-b"),
        ]
        result = builder.build(
            evidence=evidence,
            successful_branches=branches,
            base_context=_make_base_context(),
        )
        assert result.recovery_context is not None
        assert result.recovery_context["successful_branch_ids"] == ["act-a", "act-b"]

    def test_recovery_context_empty_successful_branches(self) -> None:
        """Empty successful_branches produces empty list in recovery_context."""
        builder = ReflectionContextBuilder()
        result = builder.build(
            evidence=_make_evidence(),
            successful_branches=[],
            base_context=_make_base_context(),
        )
        assert result.recovery_context is not None
        assert result.recovery_context["successful_branch_ids"] == []

    def test_recovery_context_reflection_round_default(self) -> None:
        """Default reflection_round=1 should appear in recovery_context."""
        builder = ReflectionContextBuilder()
        result = builder.build(
            evidence=_make_evidence(),
            successful_branches=[],
            base_context=_make_base_context(),
        )
        assert result.recovery_context is not None
        assert result.recovery_context["reflection_round"] == 1

    def test_recovery_context_reflection_round_custom(self) -> None:
        """Custom reflection_round should appear in recovery_context."""
        builder = ReflectionContextBuilder()
        result = builder.build(
            evidence=_make_evidence(),
            successful_branches=[],
            base_context=_make_base_context(),
            reflection_round=3,
        )
        assert result.recovery_context is not None
        assert result.recovery_context["reflection_round"] == 3

    def test_recovery_context_instruction_present(self) -> None:
        """The instruction key should always be present in recovery_context."""
        builder = ReflectionContextBuilder()
        result = builder.build(
            evidence=_make_evidence(),
            successful_branches=[],
            base_context=_make_base_context(),
        )
        assert result.recovery_context is not None
        assert "instruction" in result.recovery_context
        assert len(result.recovery_context["instruction"]) > 0

    def test_base_context_system_instructions_preserved(self) -> None:
        """system_instructions from base_context should be preserved."""
        builder = ReflectionContextBuilder()
        base = _make_base_context(system_instructions="System: do this.")
        result = builder.build(
            evidence=_make_evidence(),
            successful_branches=[],
            base_context=base,
        )
        assert result.system_instructions == "System: do this."

    def test_base_context_tool_definitions_preserved(self) -> None:
        """tool_definitions from base_context should be preserved."""
        builder = ReflectionContextBuilder()
        base = _make_base_context()
        result = builder.build(
            evidence=_make_evidence(),
            successful_branches=[],
            base_context=base,
        )
        assert result.tool_definitions == base.tool_definitions

    def test_base_context_skill_definitions_preserved(self) -> None:
        """skill_definitions from base_context should be preserved."""
        builder = ReflectionContextBuilder()
        base = _make_base_context()
        result = builder.build(
            evidence=_make_evidence(),
            successful_branches=[],
            base_context=base,
        )
        assert result.skill_definitions == base.skill_definitions

    def test_base_context_history_preserved(self) -> None:
        """history from base_context should be preserved."""
        builder = ReflectionContextBuilder()
        base = _make_base_context()
        result = builder.build(
            evidence=_make_evidence(),
            successful_branches=[],
            base_context=base,
        )
        assert result.history == base.history

    def test_base_context_memory_ref_preserved(self) -> None:
        """memory_ref from base_context should be preserved."""
        builder = ReflectionContextBuilder()
        base = _make_base_context()
        result = builder.build(
            evidence=_make_evidence(),
            successful_branches=[],
            base_context=base,
        )
        assert result.memory_ref == "mem-ref-1"

    def test_base_context_inference_config_preserved(self) -> None:
        """inference_config from base_context should be preserved."""
        builder = ReflectionContextBuilder()
        base = _make_base_context()
        result = builder.build(
            evidence=_make_evidence(),
            successful_branches=[],
            base_context=base,
        )
        assert result.inference_config == base.inference_config

    def test_result_is_immutable(self) -> None:
        """The returned ContextWindow should be frozen (immutable)."""
        from dataclasses import FrozenInstanceError

        builder = ReflectionContextBuilder()
        result = builder.build(
            evidence=_make_evidence(),
            successful_branches=[],
            base_context=_make_base_context(),
        )
        with pytest.raises((FrozenInstanceError, AttributeError)):
            result.system_instructions = "mutated"  # type: ignore[misc]
