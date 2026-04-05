"""Contract-first tests for the agent_kernel Temporal integration interfaces."""

from __future__ import annotations

from dataclasses import fields, is_dataclass

import agent_kernel.kernel.contracts as kernel_contracts
from agent_kernel.adapters.facade.kernel_facade import KernelFacade


def test_kernel_facade_exposes_only_documented_entrypoints() -> None:
    """KernelFacade must provide all agent_kernel facade entrypoints."""

    expected = {
        "start_run",
        "signal_run",
        "cancel_run",
        "query_run",
        "spawn_child_run",
        "resume_run",
    }
    for method_name in expected:
        assert hasattr(KernelFacade, method_name), method_name


def test_cancel_run_request_matches_agent_kernel_contract_shape() -> None:
    """CancelRunRequest must exist and follow the interface contract fields."""

    cancel_run_request = kernel_contracts.CancelRunRequest
    assert is_dataclass(cancel_run_request)
    assert [field.name for field in fields(cancel_run_request)] == [
        "run_id",
        "reason",
        "caused_by",
    ]


def test_resume_run_request_matches_agent_kernel_contract_shape() -> None:
    """ResumeRunRequest must expose run identity and optional snapshot reference."""

    resume_run_request = kernel_contracts.ResumeRunRequest
    assert is_dataclass(resume_run_request)
    assert [field.name for field in fields(resume_run_request)] == [
        "run_id",
        "snapshot_id",
        "caused_by",
    ]


def test_query_run_response_includes_recovery_mode_for_observability() -> None:
    """QueryRunResponse should expose recovery mode in projection-safe shape."""
    query_run_response = kernel_contracts.QueryRunResponse
    assert is_dataclass(query_run_response)
    assert [field.name for field in fields(query_run_response)] == [
        "run_id",
        "lifecycle_state",
        "projected_offset",
        "waiting_external",
        "current_action_id",
        "recovery_mode",
        "recovery_reason",
        "active_child_runs",
        "policy_versions",
        "active_stage_id",
    ]


def test_temporal_activity_input_contracts_expose_minimal_semantic_fields() -> None:
    """Activity input DTOs must expose stable contract-first field ordering."""

    assert [field.name for field in fields(kernel_contracts.AdmissionActivityInput)] == [
        "run_id",
        "action",
        "projection",
    ]
    assert [field.name for field in fields(kernel_contracts.ToolActivityInput)] == [
        "run_id",
        "action_id",
        "tool_name",
        "arguments",
    ]
    assert [field.name for field in fields(kernel_contracts.MCPActivityInput)] == [
        "run_id",
        "action_id",
        "server_name",
        "operation",
        "arguments",
    ]
    assert [field.name for field in fields(kernel_contracts.VerificationActivityInput)] == [
        "run_id",
        "action_id",
        "verification_kind",
        "evidence",
    ]
    assert [field.name for field in fields(kernel_contracts.ReconciliationActivityInput)] == [
        "run_id",
        "action_id",
        "expected_state",
        "observed_state",
    ]


def test_temporal_activity_gateway_contract_defines_all_execution_entrypoints() -> None:
    """TemporalActivityGateway must define all activity execution methods."""

    temporal_activity_gateway = kernel_contracts.TemporalActivityGateway
    for method_name in (
        "execute_admission",
        "execute_tool",
        "execute_mcp",
        "execute_verification",
        "execute_reconciliation",
    ):
        assert hasattr(temporal_activity_gateway, method_name), method_name


def test_execution_context_contract_contains_v64_trace_fields() -> None:
    """ExecutionContext should carry non-authority execution trace envelope."""
    execution_context = kernel_contracts.ExecutionContext
    assert is_dataclass(execution_context)
    assert [field.name for field in fields(execution_context)] == [
        "run_id",
        "action_id",
        "causation_id",
        "correlation_id",
        "lineage_id",
        "capability_snapshot_ref",
        "capability_snapshot_hash",
        "context_binding_ref",
        "grant_ref",
        "policy_snapshot_ref",
        "rule_bundle_hash",
        "declarative_bundle_digest",
        "timeout_ms",
        "budget_ref",
        "trace_context",
    ]


def test_admission_result_contract_exposes_sandbox_and_idempotency_extensions() -> None:
    """AdmissionResult should expose sandbox grant and idempotency envelope fields."""
    admission_result = kernel_contracts.AdmissionResult
    assert is_dataclass(admission_result)
    assert [field.name for field in fields(admission_result)] == [
        "admitted",
        "reason_code",
        "expires_at",
        "grant_ref",
        "policy_snapshot_ref",
        "sandbox_grant",
        "idempotency_envelope",
    ]


def test_adapter_protocols_are_defined_for_kernel_boundary_abstraction() -> None:
    """Kernel contracts should define ingress/capability/checkpoint/context ports."""
    for protocol_name, methods in (
        ("IngressAdapter", ("from_runner_start", "from_session_signal", "from_callback")),
        ("ContextBindingPort", ("bind_context",)),
        ("CheckpointResumePort", ("export_checkpoint", "import_resume")),
        (
            "CapabilityAdapter",
            (
                "resolve_tool_bindings",
                "resolve_mcp_bindings",
                "resolve_skill_bindings",
                "resolve_declarative_bundle",
            ),
        ),
    ):
        protocol_value = getattr(kernel_contracts, protocol_name)
        for method_name in methods:
            assert hasattr(protocol_value, method_name), f"{protocol_name}.{method_name}"


def test_adapter_protocols_define_expected_boundary_methods() -> None:
    """Adapter protocols should expose v6.4 ingress/capability/checkpoint/context methods."""
    ingress_adapter = kernel_contracts.IngressAdapter
    for method_name in ("from_runner_start", "from_session_signal", "from_callback"):
        assert hasattr(ingress_adapter, method_name), method_name

    context_binding_port = kernel_contracts.ContextBindingPort
    assert hasattr(context_binding_port, "bind_context")

    checkpoint_resume_port = kernel_contracts.CheckpointResumePort
    for method_name in ("export_checkpoint", "import_resume"):
        assert hasattr(checkpoint_resume_port, method_name), method_name

    capability_adapter = kernel_contracts.CapabilityAdapter
    for method_name in (
        "resolve_tool_bindings",
        "resolve_mcp_bindings",
        "resolve_skill_bindings",
        "resolve_declarative_bundle",
    ):
        assert hasattr(capability_adapter, method_name), method_name
