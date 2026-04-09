"""Standard failure-code recovery and gate mappings for the TRACE architecture.

These are reference mappings that provide default recovery actions and human
gate assignments for each TraceFailureCode.  Upper-layer agents (e.g. hi-agent)
may override these mappings with domain-specific policies.
"""

from __future__ import annotations

from agent_kernel.kernel.contracts import TraceFailureCode

# Failure code → recommended recovery action (can be overridden by upper layer)
FAILURE_RECOVERY_MAP: dict[TraceFailureCode, str] = {
    TraceFailureCode.MISSING_EVIDENCE: "task_view_degradation",
    TraceFailureCode.INVALID_CONTEXT: "pre_call_abort",
    TraceFailureCode.HARNESS_DENIED: "approval_escalation",
    TraceFailureCode.MODEL_OUTPUT_INVALID: "retry_or_downgrade_model",
    TraceFailureCode.MODEL_REFUSAL: "alternate_model_or_human",
    TraceFailureCode.CALLBACK_TIMEOUT: "recovery_path",
    TraceFailureCode.NO_PROGRESS: "watchdog_handling",
    TraceFailureCode.CONTRADICTORY_EVIDENCE: "human_gate_c",
    TraceFailureCode.UNSAFE_ACTION_BLOCKED: "human_gate_approval",
    TraceFailureCode.EXPLORATION_BUDGET_EXHAUSTED: "cts_termination_or_gate_b",
    TraceFailureCode.EXECUTION_BUDGET_EXHAUSTED: "cts_termination_or_gate_b",
}

# Failure code → Human Gate type (None = no human gate required)
FAILURE_GATE_MAP: dict[TraceFailureCode, str | None] = {
    TraceFailureCode.MISSING_EVIDENCE: None,
    TraceFailureCode.INVALID_CONTEXT: None,
    TraceFailureCode.HARNESS_DENIED: "gate_d",
    TraceFailureCode.MODEL_OUTPUT_INVALID: None,
    TraceFailureCode.MODEL_REFUSAL: None,
    TraceFailureCode.CALLBACK_TIMEOUT: None,
    TraceFailureCode.NO_PROGRESS: "gate_b",
    TraceFailureCode.CONTRADICTORY_EVIDENCE: "gate_a",
    TraceFailureCode.UNSAFE_ACTION_BLOCKED: "gate_d",
    TraceFailureCode.EXPLORATION_BUDGET_EXHAUSTED: "gate_b",
    TraceFailureCode.EXECUTION_BUDGET_EXHAUSTED: "gate_b",
}
