"""Tests for FailureCodeRegistry and TraceFailureCode.is_budget_exhausted."""

from agent_kernel.kernel.contracts import TraceFailureCode
from agent_kernel.kernel.failure_code_registry import FailureCodeRegistry


class TestFailureCodeRegistry:
    """Unit tests for FailureCodeRegistry."""

    def test_register_and_get_recovery(self) -> None:
        reg = FailureCodeRegistry()
        reg.register_recovery(TraceFailureCode.MODEL_REFUSAL, "retry_with_fallback")
        assert reg.get_recovery(TraceFailureCode.MODEL_REFUSAL) == "retry_with_fallback"

    def test_register_and_get_gate(self) -> None:
        reg = FailureCodeRegistry()
        reg.register_gate(TraceFailureCode.UNSAFE_ACTION_BLOCKED, "human_review")
        assert reg.get_gate(TraceFailureCode.UNSAFE_ACTION_BLOCKED) == "human_review"

    def test_get_unmapped_returns_none(self) -> None:
        reg = FailureCodeRegistry()
        assert reg.get_recovery(TraceFailureCode.NO_PROGRESS) is None
        assert reg.get_gate(TraceFailureCode.NO_PROGRESS) is None

    def test_register_batch(self) -> None:
        reg = FailureCodeRegistry()
        reg.register_batch(
            recovery={
                TraceFailureCode.CALLBACK_TIMEOUT: "escalate",
                TraceFailureCode.INVALID_CONTEXT: "abort",
            },
            gates={
                TraceFailureCode.CALLBACK_TIMEOUT: "ops_gate",
            },
        )
        assert reg.get_recovery(TraceFailureCode.CALLBACK_TIMEOUT) == "escalate"
        assert reg.get_recovery(TraceFailureCode.INVALID_CONTEXT) == "abort"
        assert reg.get_gate(TraceFailureCode.CALLBACK_TIMEOUT) == "ops_gate"

    def test_has_mapping(self) -> None:
        reg = FailureCodeRegistry()
        assert not reg.has_mapping(TraceFailureCode.HARNESS_DENIED)
        reg.register_recovery(TraceFailureCode.HARNESS_DENIED, "abort")
        assert reg.has_mapping(TraceFailureCode.HARNESS_DENIED)

        # Gate-only mapping also counts.
        reg2 = FailureCodeRegistry()
        reg2.register_gate(TraceFailureCode.MISSING_EVIDENCE, None)
        assert reg2.has_mapping(TraceFailureCode.MISSING_EVIDENCE)


class TestTraceFailureCodeBudgetHelper:
    """Unit tests for TraceFailureCode.is_budget_exhausted."""

    def test_is_budget_exhausted_true_cases(self) -> None:
        assert TraceFailureCode.is_budget_exhausted(TraceFailureCode.EXPLORATION_BUDGET_EXHAUSTED)
        assert TraceFailureCode.is_budget_exhausted(TraceFailureCode.EXECUTION_BUDGET_EXHAUSTED)

    def test_is_budget_exhausted_false_cases(self) -> None:
        non_budget_codes = [
            TraceFailureCode.MISSING_EVIDENCE,
            TraceFailureCode.INVALID_CONTEXT,
            TraceFailureCode.HARNESS_DENIED,
            TraceFailureCode.MODEL_OUTPUT_INVALID,
            TraceFailureCode.MODEL_REFUSAL,
            TraceFailureCode.CALLBACK_TIMEOUT,
            TraceFailureCode.NO_PROGRESS,
            TraceFailureCode.CONTRADICTORY_EVIDENCE,
            TraceFailureCode.UNSAFE_ACTION_BLOCKED,
        ]
        for code in non_budget_codes:
            assert not TraceFailureCode.is_budget_exhausted(code), code
